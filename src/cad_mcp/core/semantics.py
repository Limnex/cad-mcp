"""Server-enforced semantic identity, persisted inside the drawing.

The public tool boundary owns the transaction, including legacy operations that
return text instead of handles. New entities are discovered from the drawing,
never guessed from a tool's result. Block contents inherit the inserted object's
identity; top-level model/paper space entities carry their own identity.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import io
import json
from typing import Literal
from uuid import uuid4

import ezdxf
from pydantic import Field

from cad_mcp.core.contracts import StrictModel, CadOperation
from cad_mcp.core.registry import canonical_json, metadata_for_operation, pack_metadata, unpack_metadata
from cad_mcp.core.runtime import CadMcpError, failure_json


DIRECT_BACKEND = ContextVar("semantic_transaction_backend", default=None)
OBJECT_DICTIONARY = "CAD_MCP_OBJECTS"
MAX_SCAN_ENTITIES = 100_000


class SemanticObject(StrictModel):
    project_id: str = Field(min_length=1, max_length=120)
    object_id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=200)
    object_type: str = Field(min_length=1, max_length=120)
    parent_id: str | None = Field(default=None, min_length=1, max_length=120)
    source: str = Field(min_length=1, max_length=160)
    status: Literal["declared", "confirmed", "pending"] = "declared"


class SemanticBinding(StrictModel):
    project_id: str = Field(min_length=1, max_length=120)
    object_id: str = Field(min_length=1, max_length=120)
    role: str = Field(min_length=1, max_length=80)
    view_id: str | None = Field(default=None, min_length=1, max_length=120)


def semantic_error(message, code="SEMANTIC_REQUIRED"):
    return CadMcpError(message, code=code,
                       hint="先登记工程对象，再在绘图参数 semantic 中提供 project_id、object_id、role；不得事后补填或编造用途")


def is_acad(backend):
    return not isinstance(backend.doc, ezdxf.document.Drawing)


@contextmanager
def semantic_read_backend(backend):
    """Hold one CAD lock across both geometry and semantic reads.

    Nested reads in an existing guarded operation use its already-held lock;
    acquiring/releasing the proxy lock again could release the outer lock early.
    """
    raw = getattr(backend, "_b", backend)
    if DIRECT_BACKEND.get() is raw or not is_acad(raw):
        yield raw
        return
    raw._acquire_autocad_lock()
    token = None
    try:
        raw._wait_operation_ready()
        token = DIRECT_BACKEND.set(raw)
        yield raw
    finally:
        if token is not None:
            DIRECT_BACKEND.reset(token)
        raw._release_autocad_lock()


def _acad_named_item(collection, name):
    """Look up the key, never assume every named object has a Name property.

    Dictionaries.Item may expose a specialized collection (e.g. ACAD_LAYOUT),
    rather than IAcadDictionary. Only AutoCAD's exact key-not-found status is
    absence; dispatch errors, busy calls and unreadable objects must propagate.
    """
    import pythoncom

    try:
        return collection.Item(name)
    except pythoncom.com_error as exc:
        statuses = [exc.hresult]
        if exc.excepinfo and len(exc.excepinfo) >= 6:
            statuses.append(exc.excepinfo[5])
        if any(isinstance(status, int) and status & 0xFFFFFFFF == 0x80200014
               for status in statuses):
            return None
        raise


def _dictionary(backend, *, create=False):
    backend._check_doc()
    if not is_acad(backend):
        root = backend.doc.rootdict
        if OBJECT_DICTIONARY in root:
            return root[OBJECT_DICTIONARY]
        return root.add_new_dict(OBJECT_DICTIONARY) if create else None
    dictionaries = backend.doc.Dictionaries
    item = _acad_named_item(dictionaries, OBJECT_DICTIONARY)
    if item is not None:
        if str(item.ObjectName) != "AcDbDictionary":
            raise semantic_error("工程对象登记名称已被非字典对象占用", "SEMANTIC_CONFLICT")
        return item
    return dictionaries.Add(OBJECT_DICTIONARY) if create else None


def _object_key(project_id, object_id):
    return hashlib.sha256(canonical_json([project_id, object_id]).encode("utf-8")).hexdigest()


def read_object(backend, project_id, object_id):
    dictionary = _dictionary(backend)
    if dictionary is None:
        return None
    key = _object_key(project_id, object_id)
    if not is_acad(backend):
        if key not in dictionary:
            return None
        data = unpack_metadata([tag.value for tag in dictionary[key].tags if tag.code == 1])
    else:
        record = None
        for index in range(dictionary.Count):
            item = dictionary.Item(index)
            if str(dictionary.GetName(item)) == key:
                record = item
                break
        if record is None:
            return None
        codes, values = record.GetXRecordData()
        data = unpack_metadata([v for c, v in zip(codes, values) if int(c) == 1])
    obj = SemanticObject.model_validate(data)
    if (obj.project_id, obj.object_id) != (project_id, object_id):
        raise semantic_error("工程对象登记内容与索引不一致", "SEMANTIC_CONFLICT")
    return obj


def register_object(backend, obj):
    existing = read_object(backend, obj.project_id, obj.object_id)
    if existing is not None:
        if existing != obj:
            raise semantic_error(f"对象 {obj.object_id} 已有不同登记，不能静默改名、改类型或用途", "SEMANTIC_CONFLICT")
        return False
    if obj.parent_id is not None and (
        obj.parent_id == obj.object_id or read_object(backend, obj.project_id, obj.parent_id) is None
    ):
        raise semantic_error("所属工程对象尚未登记，或对象试图归属自身", "SEMANTIC_NOT_FOUND")
    dictionary = _dictionary(backend, create=True)
    key = _object_key(obj.project_id, obj.object_id)
    chunks = pack_metadata(obj.model_dump())
    if not is_acad(backend):
        record = backend.doc.objects.add_xrecord(owner=dictionary.dxf.handle)
        record.reset([(1, chunk) for chunk in chunks])
        dictionary.add(key, record)
    else:
        import pythoncom
        import win32com.client
        record = dictionary.AddXRecord(key)
        record.SetXRecordData(
            win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_I2, [1] * len(chunks)),
            win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_VARIANT, chunks))
    if read_object(backend, obj.project_id, obj.object_id) != obj:
        raise semantic_error("对象登记回读失败", "SEMANTIC_VERIFY_FAILED")
    return True


def drawing_entities(backend):
    """Only top-level graphical entities, across all layouts. No hidden writes."""
    backend._check_doc()
    result = {}
    if not is_acad(backend):
        for layout in backend.doc.layouts:
            for entity in layout:
                result[backend._handle_str(entity.dxf.handle)] = entity
                if len(result) > MAX_SCAN_ENTITIES:
                    raise semantic_error("图元数量超出语义扫描上限", "SEMANTIC_SCAN_LIMIT")
    else:
        for index in range(backend.doc.Layouts.Count):
            block = backend.doc.Layouts.Item(index).Block
            for entity_index in range(block.Count):
                backend._scan_checkpoint(entity_index)
                entity = block.Item(entity_index)
                result[backend._handle_str(entity.Handle)] = entity
                if len(result) > MAX_SCAN_ENTITIES:
                    raise semantic_error("图元数量超出语义扫描上限", "SEMANTIC_SCAN_LIMIT")
    return result


def entity_metadata(backend, entity):
    """Do not conflate unreadable/corrupt data with missing data."""
    if not is_acad(backend):
        from cad_mcp.core.registry import XDATA_APP_NAME
        if not entity.has_xdata(XDATA_APP_NAME):
            return None
        return unpack_metadata([tag.value for tag in entity.get_xdata(XDATA_APP_NAME) if tag.code == 1000])
    from cad_mcp.core.registry import XDATA_APP_NAME
    from cad_mcp.drawing.com_read import out_pair
    chunks = backend._xdata_chunks(out_pair(entity,"GetXData",leading=(XDATA_APP_NAME,)))
    return unpack_metadata(chunks) if chunks else None


def object_metadata(obj):
    return {"semantic_name": obj.name, "semantic_type": obj.object_type,
            "semantic_source": obj.source, "semantic_status": obj.status,
            "parent_id": obj.parent_id}


def _verify_metadata(backend, handle, expected, entity):
    actual = entity_metadata(backend, entity)
    if actual != expected:
        raise semantic_error(f"图元 {handle} 的身份信息回读失败", "SEMANTIC_VERIFY_FAILED")


def _transaction_state(backend):
    entities = drawing_entities(backend)
    state = {handle: [backend._entity_state_hash(entity), entity_metadata(backend, entity)]
             for handle, entity in entities.items()}
    dictionary = _dictionary(backend)
    objects = None
    if dictionary is not None:
        objects = {}
        if is_acad(backend):
            for index in range(dictionary.Count):
                record = dictionary.Item(index)
                codes, values = record.GetXRecordData()
                objects[str(dictionary.GetName(record))] = list(zip(codes, values))
        else:
            objects = {key: [(tag.code, tag.value) for tag in record.tags]
                       for key, record in dictionary.items()}
    return canonical_json([state, objects])


@contextmanager
def drawing_transaction(backend):
    """One boundary for geometry, XData and object registration, without saving."""
    acad = is_acad(backend)
    snapshot = None
    started = False
    marker = None
    marker_name = "CAD_MCP_TXN_" + uuid4().hex
    filename = None
    active = {name: getattr(backend, name, None) for name in
              ("active_layer", "active_textstyle", "active_dimstyle", "_environment_revision")}
    if acad:
        before_state = _transaction_state(backend)
        started = backend._begin_undo()
        if not started:
            raise semantic_error("无法为语义操作建立独立撤销组，未执行写入", "SEMANTIC_TRANSACTION_REQUIRED")
    else:
        snapshot = io.StringIO()
        filename = backend.doc.filename
        backend.doc.write(snapshot)
    try:
        if acad:
            # A guaranteed change in this undo group prevents U on an empty
            # group from accidentally undoing the preceding successful command.
            marker = backend.doc.Dictionaries.Add(marker_name)
        yield
        if acad:
            marker.Delete()
            backend._commit_undo(started)
    except Exception as exc:
        if acad:
            if marker is None:
                backend._commit_undo(started)
                rolled_back = False
            else:
                rolled_back = backend._rollback_undo(started)
                if rolled_back is True:
                    try:
                        dictionaries = backend.doc.Dictionaries
                        marker_gone = _acad_named_item(dictionaries, marker_name) is None
                        rolled_back = marker_gone and _transaction_state(backend) == before_state
                    except Exception:
                        rolled_back = False
                    probe = getattr(backend, "_layout_rollback_probe", None)
                    if probe is not None:
                        try:
                            rolled_back = rolled_back and bool(probe())
                        except Exception:
                            rolled_back = False
            exc.rollback_status = "succeeded" if rolled_back is True else "failed_or_unverified"
        else:
            snapshot.seek(0)
            backend.doc = ezdxf.read(snapshot)
            backend.doc.filename = filename
            backend.msp = backend.doc.modelspace()
            for name, value in active.items():
                if value is not None:
                    setattr(backend, name, value)
            exc.rollback_status = "succeeded"
        raise
    finally:
        if hasattr(backend, "_layout_rollback_probe"):
            delattr(backend, "_layout_rollback_probe")


# Every tool is classified before exposing it, including the compact gateway.
# Future mutating tools fail registration unless deliberately classified here.
CREATE_TOOLS = {
    "cad_draw_mline", "cad_trim", "cad_draw_rectangle", "cad_draw_circle",
    "cad_draw_line", "cad_draw_arc", "cad_draw_polyline", "cad_draw_hatch",
    "cad_fill_closed_boundary_solid", "cad_draw_text", "cad_draw_dimension",
    "cad_draw_dimension_at", "cad_draw_angular_dimension", "cad_draw_radius_dimension",
    "cad_draw_diameter_dimension", "cad_explode", "cad_join_polyline",
    "cad_copy_entity", "cad_mirror_entity", "cad_make_block", "cad_insert_block",
    "cad_draw_title_block", "cad_array_rect", "cad_array_polar", "cad_draw_centerline",
    "cad_offset", "cad_copy_linear", "cad_draw_table", "cad_insert_template_sheet",
    "cad_create_viewport", "cad_fill_template_fields", "cad_draw_mtext", "cad_insert_coordinate_label",
    "cad_insert_elevation_label", "cad_create_layout", "cad_apply_dwt_profile", "cad_apply_sheet_layout",
}
EDIT_TOOLS = {"cad_edit_entity_properties", "cad_delete_entity", "cad_move_entity",
              "cad_rotate_entity", "cad_set_block_attributes",
              "cad_bind_block_attribute_field"}
SPECIAL_TOOLS = {"cad_apply_environment_drawing", "cad_register_semantic_object"}
NON_GEOMETRY_TOOLS = {
    "cad_new_drawing", "cad_open_drawing", "cad_save_drawing", "cad_save_as",
    "cad_clone_template_drawing", "cad_create_layer", "cad_set_layer_props",
    "cad_set_active_layer", "cad_create_or_update_textstyle", "cad_create_or_update_dimstyle",
    "cad_set_linetype_settings", "cad_set_linetype_scale", "cad_regen", "cad_health",
    "cad_export_pdf", "cad_configure_layout_plot", "cad_publish_layout_pdf",
    "cad_set_drawing_settings", "cad_convert_plot_style_mode", "cad_rename_layout",
    "cad_create_or_update_page_setup", "cad_rename_layer",
    "cad_delete_layer", "cad_delete_layout", "cad_create_or_update_attribute_block",
    "cad_import_block_definitions", "cad_export_block_library", "cad_create_or_update_ctb",
    "cad_switch_mode", "cad_close_drawing", "cad_quit",
}


class _FailedResult(Exception):
    def __init__(self, result):
        self.result = result
        super().__init__("工具返回失败，撤销整个语义操作")


def protect_tool(fn, *, read_only, settings, backend):
    name = fn.__name__
    if read_only:
        return fn
    if name in NON_GEOMETRY_TOOLS:
        @wraps(fn)
        def infrastructure(params):
            raw = backend()
            raw = getattr(raw, "_b", raw)
            if name != "cad_health" and getattr(raw, "_semantic_write_blocked", False):
                return failure_json(semantic_error(
                    "上次语义回滚未能确认，禁止后续写入、保存、关闭和切换模式；请人工核对后重启 MCP",
                    "SEMANTIC_ROLLBACK_UNKNOWN"), name)
            return fn(params)
        return infrastructure
    if name not in CREATE_TOOLS | EDIT_TOOLS | SPECIAL_TOOLS:
        raise RuntimeError(f"工具 {name} 尚未声明语义写入策略，拒绝注册")

    @wraps(fn)
    def guarded(params):
        if settings().semantic_policy == "legacy" and name != "cad_register_semantic_object":
            return fn(params)
        raw = backend()
        raw = getattr(raw, "_b", raw)
        acquired = False
        token = None
        try:
            binding = getattr(params, "semantic", None)
            preview = getattr(params, "dry_run", False) is True
            if name in CREATE_TOOLS and binding is None and not (preview and name == "cad_apply_dwt_profile"):
                raise semantic_error("严格模式禁止创建没有工程归属和图元作用的图元")
            # Preview is read-only, but must still have valid binding when supplied.
            if getattr(params, "dry_run", False) is True:
                if binding is not None:
                    if read_object(raw, binding.project_id, binding.object_id) is None:
                        raise semantic_error("工程对象尚未登记", "SEMANTIC_NOT_FOUND")
                return fn(params)
            if getattr(raw, "_semantic_write_blocked", False):
                raise semantic_error("上次语义回滚未能确认，禁止后续写入；请人工核对后重启 MCP", "SEMANTIC_ROLLBACK_UNKNOWN")
            if getattr(raw, "doc", None) is None:
                raise semantic_error("请先新建或绑定目标图纸", "PRECONDITION_FAILED")
            if is_acad(raw):
                raw._acquire_autocad_lock()
                acquired = True
                raw._wait_operation_ready()
            obj = None
            if binding is not None:
                obj = read_object(raw, binding.project_id, binding.object_id)
                if obj is None:
                    raise semantic_error(f"工程对象 {binding.object_id} 尚未登记", "SEMANTIC_NOT_FOUND")
            before = drawing_entities(raw)
            token = DIRECT_BACKEND.set(raw)
            with drawing_transaction(raw):
                result = fn(params)
                try:
                    payload = json.loads(result)
                except (TypeError, ValueError):
                    payload = None
                if isinstance(payload, dict) and payload.get("ok") is False:
                    raise _FailedResult(result)
                after = drawing_entities(raw)
                new_handles = [h for h in after if h not in before]
                if name in CREATE_TOOLS:
                    operation = CadOperation(
                        kind=name, semantic_id=binding.object_id, role=binding.role,
                        view_id=binding.view_id,
                        params=params.model_dump(mode="json", exclude={"semantic"}),
                        metadata={**object_metadata(obj), "origin": "direct_tool"})
                    for index, handle in enumerate(new_handles):
                        metadata = metadata_for_operation(operation, "", binding.project_id,
                                                          entity_index=index, entity_count=len(new_handles))
                        metadata["entity_uid"] = str(uuid4())
                        metadata["materialized_hash"] = raw._entity_state_hash(after[handle])
                        raw.set_entity_metadata(handle, metadata)
                        _verify_metadata(raw, handle, metadata, after[handle])
                else:
                    for handle in new_handles:
                        data = entity_metadata(raw, after[handle])
                        if not data or not all(data.get(k) for k in
                            ("project_id", "semantic_id", "role", "semantic_name", "semantic_type", "semantic_source")):
                            raise semantic_error(f"操作产生未绑定身份的新图元 {handle}", "SEMANTIC_VERIFY_FAILED")
                return result
        except Exception as exc:
            if getattr(exc, "rollback_status", None) == "failed_or_unverified":
                raw._semantic_write_blocked = True
            if isinstance(exc, _FailedResult):
                payload = json.loads(exc.result)
                payload["semantic_rollback_status"] = getattr(exc, "rollback_status", "not_started")
                return canonical_json(payload)
            return failure_json(exc, name)
        finally:
            if token is not None:
                DIRECT_BACKEND.reset(token)
            if acquired:
                raw._release_autocad_lock()
    return guarded


def model_objects(model):
    """Use actual model labels/types; never infer a process from geometry."""
    def obj(item, name, kind, parent=None):
        return SemanticObject(project_id=model.project_id, object_id=item.id, name=name,
                              object_type=kind, parent_id=parent, source="design_model")
    objects = []
    for facility in model.facilities:
        objects.append(obj(facility, facility.name, facility.facility_type))
        objects.extend(obj(c, c.id, c.kind, facility.id) for c in facility.components)
    for system in model.network_systems:
        objects.append(obj(system, system.name, "network_system"))
        objects.extend(obj(n, n.id, n.node_type, system.id) for n in system.nodes)
        objects.extend(obj(s, s.id, s.segment_type or system.default_segment_type, system.id) for s in system.segments)
    for profile in model.profiles:
        objects.append(obj(profile, profile.name, "profile"))
        objects.extend(obj(s, s.id, s.series_type, profile.id) for s in profile.series)
    objects.extend(obj(a, a.id, a.annotation_type) for a in model.annotations)
    objects.extend(obj(s, s.title, "schedule") for s in model.schedules)
    objects.extend(obj(v, v.name, v.view_type) for v in model.views)
    objects.extend(obj(s, s.title, "drawing_sheet") for s in model.sheets)
    return objects


def semantic_report(backend, handle=None):
    with semantic_read_backend(backend) as raw:
        return _semantic_report(raw, handle)


def _semantic_report(backend, handle=None):
    entities = drawing_entities(backend)
    if handle is not None and handle not in entities:
        raise semantic_error(f"未找到图元 {handle}", "NOT_FOUND")
    uids = {}
    entries = []
    issues = []
    for current, entity in entities.items():
        try:
            data = entity_metadata(backend, entity)
            if data and data.get("entity_uid") is not None and (
                not isinstance(data["entity_uid"], str) or not data["entity_uid"]
            ):
                raise ValueError("entity_uid 必须是非空字符串")
        except (ValueError, TypeError) as exc:
            issue = {"handle": current, "status": "invalid_metadata", "error": str(exc)}
            issues.append(issue)
            if handle is None or current == handle:
                entries.append({**issue, "geometry_modified": None, "object": None, "metadata": None})
            continue
        if data and data.get("entity_uid"):
            uids.setdefault(data["entity_uid"], []).append(current)
        if handle is not None and current != handle:
            continue
        status = "unregistered"
        obj = None
        changed = None
        if data:
            try:
                obj = read_object(backend, data.get("project_id", ""), data.get("semantic_id", ""))
            except (ValueError, TypeError) as exc:
                issue = {"handle": current, "status": "invalid_registry", "error": str(exc)}
                issues.append(issue)
                entries.append({**issue, "geometry_modified": None, "object": None, "metadata": data})
                continue
            if obj is None:
                status = "unresolved"
            elif not data.get("role") or any(data.get(k) != v for k, v in object_metadata(obj).items()):
                status = "conflict"
            else:
                baseline = data.get("materialized_hash")
                changed = baseline != backend._entity_state_hash(entity) if baseline else None
                status = "modified" if changed else "registered" if baseline else "unverified"
        entries.append({"handle": current, "status": status, "geometry_modified": changed,
                        "object": obj.model_dump() if obj else None, "metadata": data})
    duplicate_handles = {h for handles in uids.values() if len(handles) > 1 for h in handles}
    for entry in entries:
        entry["duplicate_check_complete"] = not issues
        if entry["handle"] in duplicate_handles:
            entry["status"] = "duplicate_identity"
    return {"count": len(entries), "entities": entries,
            "complete": not issues, "issues": issues,
            "scope": "top_level_model_and_paper_space; block_contents_inherit_instance_identity",
            "verification": "registered_meaning_and_selected_geometry_properties; not_engineering_truth"}
