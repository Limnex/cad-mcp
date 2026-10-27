"""Strict public-tool tests: exercise real DXF persistence and failure rollback."""

import asyncio
import copy
from dataclasses import replace
import json
import threading
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cad_mcp import server
from cad_mcp.config import ConfigError, load_settings, save_config
from cad_mcp.core.contracts import EnvironmentModel
from cad_mcp.core.compiler import compile_environment_model
from cad_mcp.core.semantics import (
    DIRECT_BACKEND, SemanticBinding, SemanticObject, drawing_entities,
    CREATE_TOOLS, _acad_named_item, model_objects, protect_tool, read_object, semantic_report,
)
from cad_mcp.core.registry import (
    SEMANTIC_IDENTITY_FIELDS, XDATA_APP_NAME, operation_fingerprint, pack_metadata,
)
from cad_mcp.drawing.autocad import AcadBackend
from cad_mcp.drawing.document import CadDocument


@pytest.fixture
def strict(tmp_path, monkeypatch):
    cad = CadDocument(tmp_path)
    cad.new_drawing("semantic.dxf")
    monkeypatch.setattr(server, "_backend", cad)
    monkeypatch.setattr(server, "_SETTINGS", replace(server._SETTINGS, semantic_policy="strict"))
    return cad


def register(object_id="tank-01", **kwargs):
    obj = SemanticObject(project_id="P1", object_id=object_id, name="二沉池 01",
                         object_type="secondary_clarifier", source="user_design", **kwargs)
    result = json.loads(server.cad_register_semantic_object(server.RegisterSemanticObjectInput(object=obj)))
    assert result["ok"], result
    return obj


def binding(object_id="tank-01", role="池体外轮廓"):
    return SemanticBinding(project_id="P1", object_id=object_id, role=role, view_id="plan")


def draw(semantic=None):
    return json.loads(server.cad_draw_circle(server.DrawCircleInput(cx=0, cy=0, radius=6000, semantic=semantic)))


def test_default_policy_and_operator_only_configuration(tmp_path):
    path = tmp_path / "config.json"
    assert load_settings(path=path, env={}).semantic_policy == "strict"
    save_config({"semantic_policy": "legacy"}, path)
    assert load_settings(path=path, env={}).semantic_policy == "legacy"
    assert load_settings(path=path, env={"CAD_SEMANTIC_POLICY": "strict"}).semantic_policy == "strict"
    with pytest.raises(ConfigError):
        load_settings(path=path, env={"CAD_SEMANTIC_POLICY": "off"})
    with pytest.raises(ConfigError):
        save_config({"semantic_policy": "off"}, path)
    assert not any("semantic_policy" in t.parameters.get("properties", {}) for t in server._FULL_TOOL_REGISTRY.values())


def test_missing_identity_and_unregistered_object_create_nothing(strict):
    before = set(drawing_entities(strict))
    missing = draw()
    unknown = draw(binding())
    assert missing["error_detail"]["code"] == "SEMANTIC_REQUIRED"
    assert unknown["error_detail"]["code"] == "SEMANTIC_NOT_FOUND"
    assert set(drawing_entities(strict)) == before


@pytest.mark.parametrize("field", ["project_id", "object_id", "name", "object_type", "source"])
def test_empty_object_information_is_rejected(field):
    payload = dict(project_id="P1", object_id="T1", name="二沉池", object_type="tank", source="user")
    payload[field] = "  "
    with pytest.raises(ValidationError):
        SemanticObject(**payload)


def test_draw_binds_immediately_and_properties_expose_meaning(strict):
    register()
    result = draw(binding())
    assert result["ok"]
    handle = result["handle"]
    metadata = strict.get_entity_metadata(handle)
    assert metadata["semantic_type"] == "secondary_clarifier"
    assert metadata["semantic_name"] == "二沉池 01"
    assert metadata["role"] == "池体外轮廓"
    assert metadata["entity_uid"]
    assert metadata["materialized_hash"] == strict.get_entity_state_hash(handle)
    props = json.loads(server.cad_get_entity_properties(server.GetEntityPropertiesInput(handle=handle)))
    assert props["semantic"]["status"] == "registered"
    assert props["semantic"]["object"]["name"] == "二沉池 01"
    assert DIRECT_BACKEND.get() is None


def test_registry_is_idempotent_and_conflicting_registration_is_rejected(strict):
    obj = register()
    repeated = json.loads(server.cad_register_semantic_object(server.RegisterSemanticObjectInput(object=obj)))
    assert repeated["created"] is False
    conflict = obj.model_copy(update={"object_type": "thickener"})
    result = json.loads(server.cad_register_semantic_object(server.RegisterSemanticObjectInput(object=conflict)))
    assert result["error_detail"]["code"] == "SEMANTIC_CONFLICT"
    assert read_object(strict, "P1", "tank-01") == obj


@pytest.mark.parametrize("failure", ["raise", "silent"])
def test_metadata_failure_rolls_back_all_new_geometry(strict, monkeypatch, failure):
    obj = register()
    existing = strict.draw_line(1, 2, 3, 4)["handle"]
    before = set(drawing_entities(strict))
    def fail(*args):
        if failure == "raise":
            raise RuntimeError("injected XData failure")
    monkeypatch.setattr(strict, "set_entity_metadata", fail)
    result = draw(binding())
    assert result["ok"] is False
    assert set(drawing_entities(strict)) == before
    assert strict._get_entity(existing) is not None
    assert read_object(strict, "P1", "tank-01") == obj
    assert DIRECT_BACKEND.get() is None


def test_geometry_and_object_registry_survive_save_reopen(strict):
    obj = register()
    handle = draw(binding())["handle"]
    strict.save()
    filename = str(strict.current_path)
    strict.open_drawing(filename)
    assert read_object(strict, "P1", "tank-01") == obj
    assert semantic_report(strict, handle)["entities"][0]["status"] == "registered"


def test_manual_geometry_change_does_not_reclassify_object(strict):
    register()
    handle = draw(binding())["handle"]
    strict._get_entity(handle).dxf.radius = 7500
    report = semantic_report(strict, handle)["entities"][0]
    assert report["status"] == "modified"
    assert report["object"]["object_type"] == "secondary_clarifier"
    strict.draw_line(1, 1, 2, 2)
    assert any(e["status"] == "unregistered" for e in semantic_report(strict)["entities"])


def test_copy_requires_explicit_target_identity_and_gets_fresh_uid(strict):
    register()
    handle = draw(binding())["handle"]
    before = set(drawing_entities(strict))
    rejected = json.loads(server.cad_copy_entity(server.CopyEntityInput(handle=handle, dx=15000, dy=0)))
    assert rejected["error_detail"]["code"] == "SEMANTIC_REQUIRED"
    assert set(drawing_entities(strict)) == before
    register("tank-02")
    result = json.loads(server.cad_copy_entity(server.CopyEntityInput(
        handle=handle, dx=15000, dy=0, semantic=binding("tank-02"))))
    assert result["ok"], result
    added = (set(drawing_entities(strict)) - before).pop()
    assert strict.get_entity_metadata(added)["semantic_id"] == "tank-02"
    assert strict.get_entity_metadata(added)["entity_uid"] != strict.get_entity_metadata(handle)["entity_uid"]


def test_manual_copy_duplicate_identity_is_reported(strict):
    register()
    handle = draw(binding())["handle"]
    strict.copy_entity(handle, 15000, 0)
    entries = semantic_report(strict)["entities"]
    assert len(entries) == 2
    assert all(e["status"] == "duplicate_identity" for e in entries)


def test_paper_space_identity_and_rollback(strict, monkeypatch):
    register()
    original = strict.draw_circle
    def in_paper(*args):
        entity = strict.doc.layouts.get("Layout1").add_circle((0, 0), 100)
        return {"handle": strict._handle_str(entity.dxf.handle)}
    monkeypatch.setattr(strict, "draw_circle", in_paper)
    result = draw(binding())
    assert result["ok"], result
    assert semantic_report(strict, result["handle"])["entities"][0]["status"] == "registered"
    monkeypatch.setattr(strict, "draw_circle", original)


def test_gateway_cannot_bypass_semantics(strict):
    result = json.loads(asyncio.run(server._gateway_call(server.GatewayCallParams(
        tool_name="cad_draw_circle", arguments={"params": {"cx": 0, "cy": 0, "radius": 10}}))))
    assert result["error_detail"]["code"] == "SEMANTIC_REQUIRED"
    assert not strict.msp


def test_unknown_future_mutating_tool_fails_registration(strict):
    def cad_future_geometry(params):
        raise AssertionError("must never be invoked")
    with pytest.raises(RuntimeError, match="拒绝注册"):
        protect_tool(cad_future_geometry, read_only=False,
                     settings=lambda: server._SETTINGS, backend=lambda: strict)


def test_bulk_model_registers_and_binds_all_operations_in_one_call(strict):
    model = EnvironmentModel.model_validate({
        "project_id": "P1", "project_name": "测试项目",
        "facilities": [{"id": "T1", "name": "二沉池 01", "facility_type": "secondary_clarifier",
                        "origin": [0, 0], "outline": {"shape": "circle", "radius": 6000}}],
        "views": [{"id": "V1", "name": "总平面", "view_type": "plan", "scale_denominator": 100,
                   "facility_ids": ["T1"]}]})
    assert len(model_objects(model)) == 2
    params = server.ApplyEnvironmentDrawingInput(model=model, dry_run=True)
    preview = json.loads(server.cad_apply_environment_drawing(params))
    assert preview["ok"], preview
    assert read_object(strict, "P1", "T1") is None
    assert not strict.msp
    result = json.loads(server.cad_apply_environment_drawing(params.model_copy(update={"dry_run": False})))
    assert result["ok"], result
    entries = semantic_report(strict)["entities"]
    assert entries and all(e["status"] == "registered" for e in entries)


def test_semantic_edit_does_not_erase_baseline(strict):
    register()
    handle = draw(binding())["handle"]
    result = json.loads(server.cad_edit_entity_properties(server.EditEntityPropertiesInput(
        handle=handle, properties={"radius": 7000}, dry_run=False)))
    assert result["ok"], result
    assert semantic_report(strict, handle)["entities"][0]["status"] == "modified"


def test_every_creation_tool_advertises_semantic_binding():
    import inspect
    for name in CREATE_TOOLS:
        model = inspect.signature(getattr(server, name)).parameters["params"].annotation
        assert issubclass(model, server.SemanticToolInput), name
        assert "semantic" in model.model_fields, name


@pytest.mark.parametrize("tool", ["fillet", "chamfer"])
@pytest.mark.parametrize("policy", ["strict", "legacy"])
@pytest.mark.parametrize("identity_failure", [False, True])
def test_topology_tools_share_identity_transaction(strict, monkeypatch, tool, policy, identity_failure):
    from cad_mcp.core.semantics import TRANSACTION_BACKEND, _transaction_state
    cad = _AcadDouble()
    monkeypatch.setattr(server, "_backend", cad)
    monkeypatch.setattr(server, "_backend_mode", lambda backend: "acad")
    register()
    first, second = draw(binding())["handle"], draw(binding())["handle"]
    original_state = _transaction_state(cad)
    original_starts = cad.starts
    original_metadata = copy.deepcopy(cad.doc.Layouts.Item(0).Block.items[first].metadata)
    def native(*args, **kwargs):
        assert TRANSACTION_BACKEND.get() is cad
        block = cad.doc.Layouts.Item(0).Block
        source = block.items[first]
        added = _Circle(999, 5, 5, 2)
        added.metadata = copy.deepcopy(source.metadata)  # Native XData inheritance.
        source.Radius += 1
        block.items["999"] = added
        return {"applied": True, "verified": True}
    monkeypatch.setattr(cad, tool, native, raising=False)
    monkeypatch.setattr(server, "_SETTINGS", replace(server._SETTINGS, semantic_policy=policy))
    if identity_failure:
        cad.fail_metadata = True
    if tool == "fillet":
        params = server.FilletInput(handles=[first, second], pick_points=[(0, 0, 0), (1, 1, 0)], radius=2, dry_run=False)
        result = json.loads(server.cad_fillet(params))
    else:
        params = server.ChamferInput(targets=[{"mode": "pair", "first": {"handle": first, "point": [0, 0, 0]},
            "second": {"handle": second, "point": [1, 1, 0]}}], distance1=2, dry_run=False)
        result = json.loads(server.cad_chamfer(params))
    assert cad.starts == original_starts + 1 and cad.lock_depth == 0
    if identity_failure:
        assert not result["ok"] and _transaction_state(cad) == original_state
        assert not getattr(cad, "_semantic_write_blocked", False)
    else:
        assert result["ok"], result
        block = cad.doc.Layouts.Item(0).Block
        source, added = block.items[first].metadata, block.items["999"].metadata
        assert added["entity_uid"] != source["entity_uid"] == original_metadata["entity_uid"]
        assert source["materialized_hash"] != original_metadata["materialized_hash"]
        assert (added["project_id"], added["semantic_id"]) == (source["project_id"], source["semantic_id"])


@pytest.mark.parametrize("reported", ["false", "exception"])
def test_actual_restored_state_is_verified_after_uncertain_undo_reply(strict, monkeypatch, reported):
    from cad_mcp.core.write_safety import block_error, mark_unknown_write
    cad = _AcadDouble()
    monkeypatch.setattr(server, "_backend", cad)
    register()
    original_rollback = cad._rollback_undo
    def rollback(started):
        original_rollback(started)
        if reported == "exception":
            raise RuntimeError("undo completed but reply failed")
        return False
    def fail(*args, **kwargs):
        mark_unknown_write()
        raise block_error()
    monkeypatch.setattr(cad, "_rollback_undo", rollback)
    monkeypatch.setattr(cad, "draw_circle", fail)
    result = draw(binding())
    assert not result["ok"]
    assert result["error_detail"]["details"]["rollback_status"] == "succeeded", result
    assert not getattr(cad, "_semantic_write_blocked", False)


def test_native_setting_probe_failure_blocks_even_when_geometry_was_restored(strict, monkeypatch):
    cad = _AcadDouble()
    monkeypatch.setattr(server, "_backend", cad)
    monkeypatch.setattr(server, "_backend_mode", lambda backend: "acad")
    register()
    first, second = draw(binding())["handle"], draw(binding())["handle"]
    def native(**kwargs):
        cad._native_rollback_probe = lambda: False
        raise RuntimeError("native settings could not be restored")
    monkeypatch.setattr(cad, "fillet", native, raising=False)
    result = json.loads(server.cad_fillet(server.FilletInput(handles=[first, second],
        pick_points=[(0, 0, 0), (1, 1, 0)], radius=2, dry_run=False)))
    assert not result["ok"] and cad._semantic_write_blocked
    assert result["error_detail"]["details"]["rollback_status"] == "failed_or_unverified"


def test_returned_backend_failure_restores_geometry_and_registration(strict, monkeypatch):
    register()
    before = set(drawing_entities(strict))
    def fail_after_creating(*args):
        strict.msp.add_circle((10, 10), 5)
        raise RuntimeError("failure after creating geometry")
    monkeypatch.setattr(strict, "draw_circle", fail_after_creating)
    result = draw(binding())
    assert result["ok"] is False
    assert result["semantic_rollback_status"] == "succeeded"
    assert set(drawing_entities(strict)) == before


def test_bulk_failure_also_removes_new_object_records(strict, monkeypatch):
    model = EnvironmentModel.model_validate({
        "project_id": "P1", "project_name": "测试项目",
        "facilities": [{"id": "T1", "name": "二沉池", "facility_type": "secondary_clarifier",
                        "origin": [0, 0], "outline": {"shape": "circle", "radius": 6000}}],
        "views": [{"id": "V1", "name": "总平面", "view_type": "plan", "scale_denominator": 100,
                   "facility_ids": ["T1"]}]})
    def fail(*args):
        raise RuntimeError("injected model metadata failure")
    monkeypatch.setattr(strict, "set_entity_metadata", fail)
    result = json.loads(server.cad_apply_environment_drawing(
        server.ApplyEnvironmentDrawingInput(model=model, dry_run=False)))
    assert result["ok"] is False
    assert not strict.msp
    assert read_object(strict, "P1", "T1") is None


class _Collection:
    def __init__(self):
        self.items = {}

    @property
    def Count(self):
        return len(self.items)

    def Item(self, key):
        return list(self.items.values())[key] if isinstance(key, int) else self.items[key]


class _Dictionary(_Collection):
    ObjectName = "AcDbDictionary"

    def __init__(self, name, owner):
        super().__init__()
        self._key = name
        self.owner = owner

    def AddXRecord(self, key):
        record = _Record()
        self.items[key] = record
        return record

    def GetName(self, record):
        return next(key for key, value in self.items.items() if value is record)

    def Delete(self):
        del self.owner.items[self._key]


class _Dictionaries(_Collection):
    def __init__(self):
        super().__init__()
        # The real named-object collection includes specialized objects with
        # no Name property. Iterating it and reading item.Name is invalid.
        self.items["ACAD_LAYOUT"] = SimpleNamespace(ObjectName="AcDbDictionary")

    def Item(self, key):
        if isinstance(key, str) and key not in self.items:
            import pythoncom
            raise pythoncom.com_error(-2147352567, "Exception occurred.",
                (0, "AutoCAD.Application", "Key not found", None, 0, -2145386476), None)
        return super().Item(key)

    def Add(self, name):
        item = _Dictionary(name, self)
        self.items[name] = item
        return item


class _Record:
    def SetXRecordData(self, codes, values):
        self.codes, self.values = codes.value, values.value

    def GetXRecordData(self):
        return self.codes, self.values


class _Circle:
    EntityName = "AcDbCircle"
    Layer = "0"

    def __init__(self, handle, cx, cy, radius):
        self.Handle = format(handle, "X")
        self.Center, self.Radius = (cx, cy, 0), radius
        self.metadata = None

    def GetXData(self, app):
        if self.metadata is None:
            return None, None
        chunks = pack_metadata(self.metadata)
        return [1000] * len(chunks), chunks


class _AcadDouble:
    """COM-shaped in-memory double; never starts or connects to AutoCAD."""
    _handle_str = staticmethod(lambda value: str(int(value, 16)))
    _xdata_chunks = staticmethod(AcadBackend._xdata_chunks)
    _entity_state_hash = AcadBackend._entity_state_hash
    _com_state_value = staticmethod(AcadBackend._com_state_value)

    def __init__(self):
        block = _Collection()
        layouts = _Collection()
        layouts.items["Model"] = SimpleNamespace(Block=block)
        self.doc = SimpleNamespace(Layouts=layouts, Dictionaries=_Dictionaries())
        self.lock_depth = self.starts = self.commits = self.rollbacks = 0
        self.fail_metadata = self.fail_rollback = False

    def _check_doc(self):
        assert self.doc is not None

    def _scan_checkpoint(self, index):
        pass

    def _acquire_autocad_lock(self):
        self.lock_depth += 1

    def _release_autocad_lock(self):
        self.lock_depth -= 1

    def _wait_operation_ready(self):
        pass

    def _begin_undo(self):
        self.starts += 1
        self.snapshot = copy.deepcopy(self.doc)
        return True

    def _commit_undo(self, started):
        self.commits += 1

    def _rollback_undo(self, started):
        self.rollbacks += 1
        if self.fail_rollback:
            return False
        self.doc = self.snapshot
        return True

    def draw_circle(self, cx, cy, radius, layer):
        block = self.doc.Layouts.Item(0).Block
        handle = len(block.items) + 100
        block.items[str(handle)] = _Circle(handle, cx, cy, radius)
        return {"handle": str(handle)}

    def set_entity_metadata(self, handle, metadata):
        if self.fail_metadata:
            raise RuntimeError("injected COM metadata failure")
        self.doc.Layouts.Item(0).Block.items[handle].metadata = metadata


def test_acad_transaction_keeps_one_lock_and_undo_group(strict, monkeypatch):
    cad = _AcadDouble()
    monkeypatch.setattr(server, "_backend", cad)
    register()
    assert (cad.starts, cad.commits, cad.lock_depth) == (1, 1, 0)
    assert not any(key.startswith("CAD_MCP_TXN_") for key in cad.doc.Dictionaries.items)
    result = draw(binding())
    assert result["ok"], result
    assert (cad.starts, cad.commits, cad.rollbacks, cad.lock_depth) == (2, 2, 0, 0)
    before = set(drawing_entities(cad))
    cad.fail_metadata = True
    result = draw(binding())
    assert result["ok"] is False
    assert set(drawing_entities(cad)) == before
    assert cad.rollbacks == 1 and cad.lock_depth == 0
    assert not any(key.startswith("CAD_MCP_TXN_") for key in cad.doc.Dictionaries.items)


def test_unverified_acad_rollback_blocks_save_close_and_mode_change(strict, monkeypatch):
    cad = _AcadDouble()
    monkeypatch.setattr(server, "_backend", cad)
    register()
    cad.fail_metadata = cad.fail_rollback = True
    assert draw(binding())["ok"] is False
    calls = [
        (server.cad_save_drawing, server.SaveDrawingInput()),
        (server.cad_close_drawing, server.CloseDrawingInput()),
        (server.cad_switch_mode, server.SwitchModeInput(mode="dxf")),
    ]
    for fn, params in calls:
        result = json.loads(fn(params))
        assert result["error_detail"]["code"] == "SEMANTIC_ROLLBACK_UNKNOWN"


def test_successful_undo_return_without_restored_state_is_not_trusted(strict, monkeypatch):
    cad = _AcadDouble()
    monkeypatch.setattr(server, "_backend", cad)
    register()
    cad.fail_metadata = True
    monkeypatch.setattr(cad, "_rollback_undo", lambda started: True)
    result = draw(binding())
    assert result["ok"] is False
    assert result["error_detail"]["details"]["rollback_status"] == "failed_or_unverified"
    assert cad._semantic_write_blocked is True


def test_named_dictionary_lookup_does_not_read_name_of_unrelated_objects():
    dictionaries = _Dictionaries()
    assert not hasattr(dictionaries.Item(0), "Name")
    assert _acad_named_item(dictionaries, "CAD_MCP_OBJECTS") is None
    expected = dictionaries.Add("CAD_MCP_OBJECTS")
    assert not hasattr(expected, "Name")
    assert _acad_named_item(dictionaries, "CAD_MCP_OBJECTS") is expected


@pytest.mark.parametrize("hresult,status", [(-2147418111, None), (-2147352567, -2147024891)])
def test_dictionary_errors_other_than_missing_key_are_not_suppressed(hresult, status):
    import pythoncom
    error = pythoncom.com_error(hresult, "failure", (0, "AutoCAD", "failure", None, 0, status), None)
    class BrokenCollection:
        def Item(self, name):
            raise error
    with pytest.raises(pythoncom.com_error) as raised:
        _acad_named_item(BrokenCollection(), "CAD_MCP_OBJECTS")
    assert raised.value is error


def test_unsupported_dictionary_interface_is_not_reported_as_missing():
    class Unsupported:
        def Item(self, name):
            raise AttributeError("Item is unavailable")
    with pytest.raises(AttributeError, match="unavailable"):
        _acad_named_item(Unsupported(), "CAD_MCP_OBJECTS")


def _upgrade_model():
    return EnvironmentModel.model_validate({
        "project_id": "P1", "project_name": "升级测试",
        "facilities": [{"id": "T1", "name": "二沉池01", "facility_type": "secondary_clarifier",
                        "origin": [0, 0], "outline": {"shape": "circle", "radius": 6000}}],
        "views": [{"id": "V1", "name": "总平面", "view_type": "plan", "scale_denominator": 100,
                   "facility_ids": ["T1"]}]})


def _seed_upgrade_drawing(cad, model, baseline):
    plan, _ = compile_environment_model(model)
    if baseline == "pre_semantics":
        for operation in plan.operations:
            operation.metadata = {k: v for k, v in operation.metadata.items()
                                  if k not in SEMANTIC_IDENTITY_FIELDS}
    cad.apply_operation_plan(plan)
    if baseline == "enriched_fingerprint":
        for record in cad.list_managed_entities():
            operation = next(op for op in plan.operations
                             if op.semantic_id == record.semantic_id and op.role == record.role)
            metadata = cad.get_entity_metadata(record.handle)
            fingerprint = operation_fingerprint(operation, include_identity_metadata=True)
            metadata.update(geometry_hash=fingerprint, operation_group=fingerprint)
            cad.set_entity_metadata(record.handle, metadata)
    return next(entity for entity in cad.msp if entity.dxftype() == "CIRCLE")


@pytest.mark.parametrize("policy", ["legacy", "strict"])
@pytest.mark.parametrize("baseline", ["pre_semantics", "enriched_fingerprint"])
def test_upgrade_updates_identity_in_place_without_replacing_geometry(strict, monkeypatch, policy, baseline):
    monkeypatch.setattr(server, "_SETTINGS", replace(server._SETTINGS, semantic_policy=policy))
    model = _upgrade_model()
    circle = _seed_upgrade_drawing(strict, model, baseline)
    circle.dxf.color = 3
    handles = {r.handle for r in strict.list_managed_entities()}
    params = server.ApplyEnvironmentDrawingInput(model=model)
    preview = json.loads(server.cad_apply_environment_drawing(params))
    assert preview["ok"] and preview["difference"]["replace"] == 0, preview
    result = json.loads(server.cad_apply_environment_drawing(params.model_copy(update={"dry_run": False})))
    assert result["ok"] and result["difference"]["replace"] == 0, result
    assert result["execution"]["entity_count"] == 0
    assert {r.handle for r in strict.list_managed_entities()} == handles
    assert circle.is_alive and circle.dxf.color == 3
    for record in strict.list_managed_entities():
        assert record.metadata["semantic_name"] == "二沉池01"
        assert record.metadata["entity_uid"]
    repeated = json.loads(server.cad_apply_environment_drawing(params.model_copy(update={"dry_run": False})))
    assert repeated["ok"] and repeated["metadata_updates"] == []
    if policy == "strict":
        assert all(row["status"] == "registered" for row in semantic_report(strict)["entities"])


def test_real_geometry_change_is_not_mistaken_for_identity_migration(strict):
    model = _upgrade_model()
    circle = _seed_upgrade_drawing(strict, model, "pre_semantics")
    model.facilities[0].outline.radius = 7000
    result = json.loads(server.cad_apply_environment_drawing(
        server.ApplyEnvironmentDrawingInput(model=model, dry_run=False)))
    assert result["ok"] and result["difference"]["replace"] == 1, result
    assert not circle.is_alive
    assert next(e for e in strict.msp if e.dxftype() == "CIRCLE").dxf.radius == 7000


def test_identity_migration_failure_restores_old_metadata_and_registry(strict, monkeypatch):
    model = _upgrade_model()
    _seed_upgrade_drawing(strict, model, "pre_semantics").dxf.color = 3
    before = {r.handle: r.metadata for r in strict.list_managed_entities()}
    original = strict.set_entity_metadata
    def fail_after_write(handle, metadata):
        original(handle, metadata)
        raise RuntimeError("injected in-place identity migration failure")
    monkeypatch.setattr(strict, "set_entity_metadata", fail_after_write)
    result = json.loads(server.cad_apply_environment_drawing(
        server.ApplyEnvironmentDrawingInput(model=model, dry_run=False)))
    assert result["ok"] is False
    assert {r.handle: r.metadata for r in strict.list_managed_entities()} == before
    assert next(e for e in strict.msp if e.dxftype() == "CIRCLE").dxf.color == 3
    assert read_object(strict, "P1", "T1") is None


@pytest.mark.parametrize("bad_value", ["{invalid json", '{"entity_uid": [1, 2]}'])
def test_bad_metadata_on_other_entity_does_not_break_property_query(strict, bad_value):
    register()
    good = draw(binding())["handle"]
    bad = strict.draw_circle(20000, 0, 10)["handle"]
    strict._get_entity(bad).set_xdata(XDATA_APP_NAME, [(1000, bad_value)])
    result = json.loads(server.cad_get_entity_properties(server.GetEntityPropertiesInput(handle=good)))
    assert result["ok"] and result["properties"]["radius"] == 6000
    assert result["semantic"]["status"] == "registered"
    assert result["semantic"]["duplicate_check_complete"] is False
    audit = json.loads(server.cad_check_semantics(server.CheckSemanticsInput()))
    assert audit["ok"] and audit["complete"] is False
    assert audit["issues"][0]["handle"] == bad
    assert next(e for e in audit["entities"] if e["handle"] == bad)["status"] == "invalid_metadata"
    selected_bad = json.loads(server.cad_get_entity_properties(server.GetEntityPropertiesInput(handle=bad)))
    assert selected_bad["ok"] and selected_bad["semantic"]["status"] == "invalid_metadata"


def test_invalid_registry_does_not_hide_readable_geometry(strict):
    register()
    handle = draw(binding())["handle"]
    dictionary = strict.doc.rootdict["CAD_MCP_OBJECTS"]
    record = next(value for _, value in dictionary.items())
    record.reset([(1, "{invalid registry")])
    result = json.loads(server.cad_get_entity_properties(server.GetEntityPropertiesInput(handle=handle)))
    assert result["ok"] and result["properties"]["radius"] == 6000
    assert result["semantic"]["status"] == "invalid_registry"


@pytest.mark.parametrize("tool", ["properties", "semantics", "audit"])
def test_semantic_read_holds_exactly_one_acad_lock_without_undo(strict, monkeypatch, tool):
    cad = _AcadDouble()
    handle = cad.draw_circle(0, 0, 1, None)["handle"]
    events = []
    acquire, release = cad._acquire_autocad_lock, cad._release_autocad_lock
    def locked():
        assert cad.lock_depth == 0
        acquire()
        events.append("acquire")
    def unlocked():
        assert cad.lock_depth == 1
        events.append("release")
        release()
    def ready():
        assert cad.lock_depth == 1
        events.append("ready")
    def properties(requested):
        assert requested == handle and cad.lock_depth == 1
        return {"handle": handle, "properties": {"radius": 1}}
    monkeypatch.setattr(cad, "_acquire_autocad_lock", locked)
    monkeypatch.setattr(cad, "_release_autocad_lock", unlocked)
    monkeypatch.setattr(cad, "_wait_operation_ready", ready)
    monkeypatch.setattr(cad, "_scan_checkpoint", lambda index: properties(handle))
    monkeypatch.setattr(cad, "get_entity_properties", properties, raising=False)
    monkeypatch.setattr(server, "_backend", server._AcadLockProxy(cad))
    params = server.GetEntityPropertiesInput(handle=handle)
    if tool == "properties":
        result = server.cad_get_entity_properties(params)
    elif tool == "semantics":
        result = server.cad_get_entity_semantics(params)
    else:
        result = server.cad_check_semantics(server.CheckSemanticsInput())
    assert json.loads(result)["ok"], result
    assert events == ["acquire", "ready", "release"]
    assert cad.lock_depth == 0 and cad.starts == 0
    assert DIRECT_BACKEND.get() is None


def test_semantic_read_releases_lock_on_backend_error(strict, monkeypatch):
    cad = _AcadDouble()
    cad.draw_circle(0, 0, 1, None)
    def unreadable(app):
        raise RuntimeError("injected COM read failure")
    entity = cad.doc.Layouts.Item(0).Block.Item(0)
    monkeypatch.setattr(entity, "GetXData", unreadable)
    monkeypatch.setattr(server, "_backend", server._AcadLockProxy(cad))
    result = json.loads(server.cad_check_semantics(server.CheckSemanticsInput()))
    assert result["ok"] is False
    assert cad.lock_depth == 0 and DIRECT_BACKEND.get() is None


def test_semantic_query_waits_for_another_client_to_finish(strict, monkeypatch):
    cad = _AcadDouble()
    handle = cad.draw_circle(0, 0, 1, None)["handle"]
    gate = threading.Lock()
    attempted, read = threading.Event(), threading.Event()
    results = []
    gate.acquire()  # A different client's write is still in progress.
    def acquire():
        attempted.set()
        if not gate.acquire(timeout=2):
            raise RuntimeError("test CAD lock timeout")
    monkeypatch.setattr(cad, "_acquire_autocad_lock", acquire)
    monkeypatch.setattr(cad, "_release_autocad_lock", gate.release)
    monkeypatch.setattr(cad, "_scan_checkpoint", lambda index: read.set())
    monkeypatch.setattr(server, "_backend", server._AcadLockProxy(cad))
    reader = threading.Thread(target=lambda: results.append(json.loads(server.cad_get_entity_semantics(
        server.GetEntityPropertiesInput(handle=handle)))))
    reader.start()
    try:
        assert attempted.wait(timeout=1)
        assert not read.is_set()
    finally:
        gate.release()
        reader.join(timeout=2)
    assert not reader.is_alive()
    assert read.is_set() and results[0]["ok"]



def test_template_anchor_text_is_bound_during_strict_creation(strict, monkeypatch):
    register()
    paper = strict.doc.layouts.new("TEST-PAPER")
    def fill(*args):
        entity = paper.add_text("实机字段填写")
        return {"handles": [strict._handle_str(entity.dxf.handle)]}
    monkeypatch.setattr(strict, "fill_template_fields", fill, raising=False)
    params = dict(template_id="test", sheet_id="A2", layout_name="TEST-PAPER", values={"title":"实机字段填写"})
    rejected = json.loads(server.cad_fill_template_fields(server.FillTemplateFieldsInput(**params)))
    assert rejected["error_detail"]["code"] == "SEMANTIC_REQUIRED"
    assert len(paper) == 0
    accepted = json.loads(server.cad_fill_template_fields(server.FillTemplateFieldsInput(**params, semantic=binding(role="图框标题"))))
    assert accepted["ok"], accepted
    entity = strict.doc.entitydb[format(int(accepted["handles"][0]), "X")]
    from cad_mcp.core.semantics import entity_metadata
    metadata = entity_metadata(strict, entity)
    assert metadata["project_id"] == "P1" and metadata["role"] == "图框标题"
