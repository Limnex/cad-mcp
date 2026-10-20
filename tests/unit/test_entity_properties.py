import copy
import json
import math
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cad_mcp import server
from cad_mcp.core.runtime import CadMcpError, UnsupportedOperationError
from cad_mcp.drawing.document import CadDocument
from cad_mcp.drawing.autocad import AcadBackend
from cad_mcp.drawing.entity_properties import EntityAccess, EntityPropertyPatch


@pytest.fixture
def drawing(tmp_path):
    cad = CadDocument(tmp_path)
    cad.new_drawing("properties.dxf")
    cad.create_layer("EDIT", color=2)
    return cad


def dump(cad):
    # Document.write itself regenerates GUIDs; inspect the entity database instead.
    return json.dumps([(key, entity.dxftype(), entity.dxf.all_existing_dxf_attribs(),
                        entity.text if entity.dxftype() == "MTEXT" else None)
                       for key, entity in cad.doc.entitydb.items() if entity.is_alive],
                      default=str, sort_keys=True)


@pytest.mark.parametrize("properties", [
    {}, {"color": None}, {"bogus": 1}, {"color": True}, {"color": 257},
    {"color": "1"}, {"height": 0}, {"radius": -1}, {"rotation": float("nan")},
    {"linetype_scale": float("inf")}, {"lineweight": 24}, {"layer": " "},
    {"text": ""}, {"start": [0, 1]}, {"end": [0, 1, float("inf")]},
    {"height": True}, {"height": "3"},
])
def test_patch_rejects_invalid_values(properties):
    with pytest.raises(ValidationError):
        EntityPropertyPatch.model_validate(properties)


def test_nested_tool_serialization_preserves_omitted_properties():
    params = server.EditEntityPropertiesInput(handle="123", properties={"color": 1})
    payload = params.model_dump(mode="json")
    assert payload["properties"] == {"color": 1}
    again = server.EditEntityPropertiesInput.model_validate(payload)
    assert again.properties.model_fields_set == {"color"}
    assert again.model_dump(mode="json") == payload


def test_acad_nonfinite_existing_property_prevents_transaction(acad_mock):
    acad_mock.entity.Height = float("nan")
    with pytest.raises(ValueError):
        acad_mock.edit_entity_properties("1", {"text": "new"}, dry_run=False)
    assert acad_mock.events == []


@pytest.mark.parametrize("kind,patch", [
    ("LINE", {"start": [2, 3, 4], "end": [20, 30, 40]}),
    ("CIRCLE", {"center": [2, 3, 4], "radius": 12.5}),
    ("TEXT", {"text": "  中文文字  ", "height": 5.0, "rotation": -30.0}),
    ("MTEXT", {"text": "首行\\P次行", "height": 5.0, "rotation": 390.0}),
])
def test_dxf_preview_apply_and_save_roundtrip(drawing, kind, patch, tmp_path):
    import ezdxf
    msp = drawing.msp
    entity = {"LINE": lambda: msp.add_line((0, 0), (10, 0)),
              "CIRCLE": lambda: msp.add_circle((0, 0), 3),
              "TEXT": lambda: msp.add_text("原文字"),
              "MTEXT": lambda: msp.add_mtext("原多行")}[kind]()
    handle = str(int(entity.dxf.handle, 16))
    patch = {"layer": "EDIT", "color": 1, "linetype": "Continuous",
             "lineweight": 50, "linetype_scale": 2.0, **patch}
    baseline = dump(drawing)
    preview = drawing.edit_entity_properties(handle, patch)
    assert preview["dry_run"] and not preview["applied"]
    assert preview["would_write"]
    assert dump(drawing) == baseline
    applied = drawing.edit_entity_properties(handle, patch, dry_run=False)
    assert applied["verified"] and applied["applied"]
    assert applied["before"] == preview["before"]
    assert applied["changes"] == preview["changes"]
    for name, value in patch.items():
        actual = applied["after"]["properties"][name]
        if name == "rotation":
            assert actual == pytest.approx(value % 360)
        elif isinstance(value, (int, float)):
            assert actual == pytest.approx(value)
        elif name == "linetype":
            assert actual.upper() == value.upper()
        else:
            assert actual == value
    assert not drawing.edit_entity_properties(handle, patch, dry_run=False)["would_write"]
    path = tmp_path / "saved.dxf"
    drawing.doc.saveas(path)
    drawing.doc = ezdxf.readfile(path)
    drawing.msp = drawing.doc.modelspace()
    assert drawing.get_entity_properties(handle) == applied["after"]
    assert not drawing.doc.audit().has_errors


def test_dxf_truecolor_is_cleared_even_for_same_aci(drawing):
    entity = drawing.msp.add_circle((0, 0), 3, dxfattribs={"color": 1, "true_color": 0x123456})
    handle = str(int(entity.dxf.handle, 16))
    result = drawing.edit_entity_properties(handle, {"color": 1}, dry_run=False)
    assert result["applied"]
    assert result["before"]["true_color"] == 0x123456
    assert result["after"]["true_color"] is None


@pytest.mark.parametrize("patch", [
    {"radius": 3}, {"layer": "MISSING"}, {"linetype": "MISSING"},
    {"start": [10, 0, 0]},
])
def test_preflight_rejects_without_any_mutation(drawing, patch):
    handle = drawing.draw_line(0, 0, 10, 0)["handle"]
    before = dump(drawing)
    with pytest.raises((ValueError, UnsupportedOperationError)):
        drawing.edit_entity_properties(handle, {"color": 1, **patch}, dry_run=False)
    assert dump(drawing) == before


@pytest.mark.parametrize("target_locked", [False, True])
def test_locked_source_or_target_is_rejected(drawing, target_locked):
    handle = drawing.draw_circle(0, 0, 3, "WALL")["handle"]
    drawing.doc.layers.get("EDIT" if target_locked else "WALL").lock()
    before = dump(drawing)
    with pytest.raises(ValueError, match="锁定"):
        drawing.edit_entity_properties(handle, {"layer": "EDIT", "radius": 4}, dry_run=False)
    assert dump(drawing) == before


def test_rejects_missing_nonmodelspace_and_invalid_handles(drawing):
    entity = drawing.doc.blocks.new("BLOCK").add_circle((0, 0), 3)
    with pytest.raises(ValueError, match="模型空间"):
        drawing.get_entity_properties(str(int(entity.dxf.handle, 16)))
    with pytest.raises(CadMcpError) as error:
        drawing.get_entity_properties("9999999")
    assert error.value.code == "NOT_FOUND"
    for handle in ("A0", "-1", "0", "1.5"):
        with pytest.raises(ValueError):
            drawing.get_entity_properties(handle)


@pytest.mark.parametrize("value", ["%<field>%", "line\nline"])
def test_text_field_and_multiline_protection(drawing, value):
    handle = drawing.draw_text("原文字", 0, 0, 3)["handle"]
    before = dump(drawing)
    with pytest.raises((ValueError, UnsupportedOperationError)):
        drawing.edit_entity_properties(handle, {"text": value}, dry_run=False)
    assert dump(drawing) == before


@pytest.mark.parametrize("failure", ["setter", "readback"])
def test_dxf_restores_original_on_partial_write_or_readback_mismatch(drawing, monkeypatch, failure):
    handle = drawing.draw_circle(0, 0, 3)["handle"]
    old = drawing.get_entity_properties(handle)
    original_write = EntityAccess.write

    def broken(self, name, value):
        if name == "radius":
            if failure == "setter":
                raise RuntimeError("injected write failure")
            return
        original_write(self, name, value)

    monkeypatch.setattr(EntityAccess, "write", broken)
    with pytest.raises(RuntimeError) as error:
        drawing.edit_entity_properties(handle, {"color": 1, "radius": 5}, dry_run=False)
    assert error.value.rollback_status == "succeeded"
    assert drawing.get_entity_properties(handle) == old
    assert not drawing.doc.audit().has_errors


def test_tool_contract_and_gateway_registration(drawing, monkeypatch):
    from cad_mcp.core.isolation import _policy
    monkeypatch.setattr(server, "_get_backend", lambda: drawing)
    handle = drawing.draw_text("old", 0, 0, 3)["handle"]
    result = json.loads(server.cad_edit_entity_properties(server.EditEntityPropertiesInput(
        handle=handle, properties={"text": "  keep spaces  "})))
    assert result["ok"] and result["dry_run"]
    result = json.loads(server.cad_get_entity_properties(server.GetEntityPropertiesInput(handle=handle)))
    assert result["properties"]["text"] == "old"
    error = json.loads(server.cad_get_entity_properties(server.GetEntityPropertiesInput(handle="999999")))
    assert error["ok"] is False and error["error_detail"]["code"] == "NOT_FOUND"
    assert _policy("cad_get_entity_properties", {"params": {"handle": handle}}, server._FULL_TOOL_REGISTRY)[1] is False
    assert _policy("cad_edit_entity_properties", {"params": {"handle": handle, "properties": {"height": 4}}},
                   server._FULL_TOOL_REGISTRY)[1] is True


class MockEntity:
    ObjectName = "AcDbText"
    Layer = "WALL"
    Color = 256
    Linetype = "ByLayer"
    Lineweight = -1
    LinetypeScale = 1.0
    TextString = "原文字"
    Height = 3.0
    Rotation = 0.0
    TrueColor = SimpleNamespace(ColorMethod=192)
    updated = False

    def Update(self):
        self.updated = True


@pytest.fixture
def acad_mock(tmp_path):
    raw = AcadBackend(tmp_path)
    raw.entity = MockEntity()
    raw._check_doc = lambda: None
    raw._find = lambda handle: raw.entity
    raw.doc = SimpleNamespace(
        Layers=SimpleNamespace(Item=lambda name: SimpleNamespace(Name=name, Lock=False)),
        Linetypes=SimpleNamespace(Item=lambda name: SimpleNamespace(Name=name)))
    raw.events = []

    def begin():
        raw.backup = copy.deepcopy(raw.entity.__dict__)
        raw.events.append("begin")
        return True

    def rollback(started):
        raw.entity.__dict__.clear()
        raw.entity.__dict__.update(raw.backup)
        raw.events.append("rollback")
        return True

    raw._begin_undo = begin
    raw._commit_undo = lambda started: raw.events.append("commit")
    raw._rollback_undo = rollback
    return raw


def test_acad_preview_has_no_setters_or_undo_and_apply_converts_angles(acad_mock):
    cad = acad_mock
    patch = {"height": 5.0, "rotation": 90.0, "text": "  新文字  "}
    before = copy.deepcopy(cad.entity.__dict__)
    cad.edit_entity_properties("1", patch)
    assert cad.events == [] and cad.entity.__dict__ == before
    result = cad.edit_entity_properties("1", patch, dry_run=False)
    assert result["verified"] and result["applied"]
    assert cad.entity.Rotation == pytest.approx(math.pi / 2)
    assert cad.entity.TextString == "  新文字  "
    assert cad.entity.Height == 5.0 and cad.entity.updated
    assert cad.events == ["begin", "commit"]


def test_acad_readback_failure_rolls_back(acad_mock, monkeypatch):
    original = EntityAccess.write

    def ignored(self, name, value):
        if name != "height":
            original(self, name, value)

    monkeypatch.setattr(EntityAccess, "write", ignored)
    with pytest.raises(RuntimeError, match="回读") as error:
        acad_mock.edit_entity_properties("1", {"text": "new", "height": 6.0}, dry_run=False)
    assert error.value.rollback_status == "succeeded"
    assert acad_mock.entity.TextString == "原文字"
    assert acad_mock.events == ["begin", "rollback"]


def test_acad_undo_failure_prevents_setters(acad_mock):
    def fail():
        raise RuntimeError("undo failed")

    acad_mock._begin_undo = fail
    with pytest.raises(RuntimeError, match="undo failed"):
        acad_mock.edit_entity_properties("1", {"text": "new"}, dry_run=False)
    assert acad_mock.entity.TextString == "原文字"


def test_acad_proxy_preflight_and_noop_do_not_open_or_undo_empty_groups(acad_mock):
    cad = acad_mock
    cad._acquire_autocad_lock = lambda: None
    cad._release_autocad_lock = lambda: None
    cad._wait_operation_ready = lambda: None
    proxy = server._AcadLockProxy(cad)
    # The proxy's commit(False) is a no-op in the real backend.
    cad._commit_undo = lambda started: cad.events.append("commit") if started else None
    cad._rollback_undo = lambda started: cad.events.append("rollback") if started else None
    proxy.edit_entity_properties("1", {"text": "原文字"}, dry_run=False)
    assert cad.events == []
    with pytest.raises(UnsupportedOperationError):
        proxy.edit_entity_properties("1", {"radius": 4.0}, dry_run=False)
    assert cad.events == []


def test_acad_unverified_rollback_is_not_reported_successful(acad_mock, monkeypatch):
    original = EntityAccess.write

    def fail(self, name, value):
        if name == "height":
            raise RuntimeError("write failed")
        original(self, name, value)

    monkeypatch.setattr(EntityAccess, "write", fail)
    acad_mock._rollback_undo = lambda started: True  # claims success without restoring
    with pytest.raises(RuntimeError) as error:
        acad_mock.edit_entity_properties("1", {"text": "changed", "height": 4.0}, dry_run=False)
    assert error.value.rollback_status == "failed_or_unverified"


def test_dxf_tilted_circle_uses_world_coordinates(drawing):
    from ezdxf.math import Vec3
    entity = drawing.msp.add_circle((0, 0, 0), 3, dxfattribs={"extrusion": (0, 1, 0)})
    handle = str(int(entity.dxf.handle, 16))
    result = drawing.edit_entity_properties(handle, {"center": [2, 3, 4]}, dry_run=False)
    assert result["after"]["properties"]["center"] == [2.0, 3.0, 4.0]
    assert entity.ocs().to_wcs(entity.dxf.center).isclose(Vec3(2, 3, 4))
