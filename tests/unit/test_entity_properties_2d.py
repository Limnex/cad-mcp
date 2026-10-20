import json
import math

import ezdxf
import pytest
from pydantic import ValidationError

from cad_mcp.core.runtime import UnsupportedOperationError
from cad_mcp.drawing.document import CadDocument
from cad_mcp.drawing.entity_properties import EntityPropertyPatch
from cad_mcp.drawing import block_contents


@pytest.fixture
def cad(tmp_path):
    result = CadDocument(tmp_path)
    result.new_drawing("planar.dxf")
    return result


def handle(entity):
    return str(int(entity.dxf.handle, 16))


def vertices(*points):
    return [{"x": x, "y": y} for x, y in points]


def database(cad):
    return json.dumps([(key, item.dxftype(), item.dxf.all_existing_dxf_attribs(),
                        str(item.text) if item.dxftype() == "MTEXT" else None)
                       for key, item in cad.doc.entitydb.items() if item.is_alive], default=str, sort_keys=True)


@pytest.mark.parametrize("entity_type,patch", [
    ("ARC", {"center": [10, 20], "radius": 4.0, "start_angle": 20.0, "end_angle": 210.0}),
    ("LWPOLYLINE", {"vertices": vertices((0, 0), (15, 0), (15, 12)), "closed": True}),
    ("ELLIPSE", {"center": [12, 22], "major_axis": [6, 2], "radius_ratio": 0.4,
                 "start_parameter": 0.2, "end_parameter": 5.5}),
    ("POINT", {"position": [4.123456789, 5.987654321]}),
])
def test_planar_edit_preview_apply_save_and_idempotency(cad, entity_type, patch, tmp_path):
    e = {"ARC": lambda: cad.msp.add_arc((0, 0, 7), 3, 0, 90),
         "LWPOLYLINE": lambda: cad.msp.add_lwpolyline([(0, 0), (10, 0), (10, 10)], dxfattribs={"elevation": 7}),
         "ELLIPSE": lambda: cad.msp.add_ellipse((0, 0, 7), (5, 0, 0), 0.5),
         "POINT": lambda: cad.msp.add_point((1, 2, 7))}[entity_type]()
    h = handle(e)
    before = database(cad)
    preview = cad.edit_entity_properties(h, patch)
    assert preview["dry_run"] and preview["would_write"] and not preview["applied"]
    assert database(cad) == before
    applied = cad.edit_entity_properties(h, patch, dry_run=False)
    assert applied["verified"] and applied["applied"]
    assert applied["changes"] == preview["changes"]
    assert not cad.edit_entity_properties(h, patch, dry_run=False)["would_write"]
    if entity_type == "LWPOLYLINE":
        assert e.dxf.elevation == 7
    elif entity_type == "POINT":
        assert e.dxf.location.z == 7
    else:
        assert e.dxf.center.z == 7
    path = tmp_path / "roundtrip.dxf"
    cad.doc.saveas(path)
    cad.doc = ezdxf.readfile(path)
    cad.msp = cad.doc.modelspace()
    assert cad.get_entity_properties(h) == applied["after"]
    assert not cad.doc.audit().has_errors


@pytest.mark.parametrize("patch", [
    {"position": [1, 2, 3]}, {"major_axis": [1, 2, 3]}, {"x_scale": 0}, {"y_scale": -1},
    {"closed": 1}, {"radius_ratio": 1.1}, {"start_parameter": -1},
    {"vertices": [{"x": 1, "y": 2, "z": 3}, {"x": 2, "y": 3}]},
    {"block_attributes": {}}, {"block_attributes": {"A": 12.0}}, {"block_texts": {"0": "\x00"}},
    {"pattern_scale": float("inf")},
])
def test_new_contract_rejects_3d_invalid_and_ambiguous_values(patch):
    with pytest.raises(ValidationError):
        EntityPropertyPatch.model_validate(patch)


@pytest.mark.parametrize("entity_type,patch", [
    ("ARC", {"start_angle": 90.0, "end_angle": 90.0}),
    ("ARC", {"center": [1, 2, 3]}),
    ("ELLIPSE", {"major_axis": [0, 0]}),
    ("ELLIPSE", {"start_parameter": 1.0, "end_parameter": 1.0}),
    ("LWPOLYLINE", {"vertices": vertices((0, 0), (0, 0), (10, 10))}),
])
def test_invalid_geometry_stops_before_common_property_write(cad, entity_type, patch):
    e = {"ARC": lambda: cad.msp.add_arc((0, 0), 3, 0, 90),
         "ELLIPSE": lambda: cad.msp.add_ellipse((0, 0), (5, 0), 0.5),
         "LWPOLYLINE": lambda: cad.msp.add_lwpolyline([(0, 0), (10, 0), (10, 10)])}[entity_type]()
    before = database(cad)
    with pytest.raises(ValueError):
        cad.edit_entity_properties(handle(e), {"color": 1, **patch}, dry_run=False)
    assert database(cad) == before


def test_tilted_new_entities_are_rejected_without_projecting(cad):
    e = cad.msp.add_arc((0, 0), 3, 0, 90, dxfattribs={"extrusion": (0, 1, 0)})
    before = database(cad)
    with pytest.raises(UnsupportedOperationError, match="XY"):
        cad.edit_entity_properties(handle(e), {"center": [2, 3]}, dry_run=False)
    assert database(cad) == before


def test_existing_circle_accepts_xy_without_changing_elevation(cad):
    e = cad.msp.add_circle((0, 0, 9), 4)
    assert cad.edit_entity_properties(handle(e), {"center": [4, 5]}, dry_run=False)["verified"]
    assert tuple(e.dxf.center) == (4, 5, 9)


def test_polyline_preserves_bulges_and_widths_when_only_xy_changes(cad):
    e = cad.msp.add_lwpolyline([(0, 0, 0.2, 0.4, 0.5), (10, 0, 0.1, 0.3, 0)], format="xyseb")
    h = handle(e)
    assert cad.edit_entity_properties(h, {"vertices": vertices((1, 2), (12, 3))}, dry_run=False)["verified"]
    assert list(e.get_points("xyseb")) == [(1, 2, 0.2, 0.4, 0.5), (12, 3, 0.1, 0.3, 0)]
    before = database(cad)
    with pytest.raises(ValueError, match="顶点数"):
        cad.edit_entity_properties(h, {"vertices": vertices((1, 2), (12, 3), (12, 8))}, dry_run=False)
    assert database(cad) == before


def make_block(cad, nested=False):
    block = cad.doc.blocks.new("CONTENT")
    block.add_line((0, 0), (10, 0))
    block.add_text("原说明", dxfattribs={"height": 2.5, "insert": (0, 2)})
    block.add_mtext("12.00", dxfattribs={"char_height": 2.5})
    block.add_attdef("FLOW", (0, 5), text="12.00", dxfattribs={"height": 2.5})
    if nested:
        parent = cad.doc.blocks.new("PARENT")
        parent.add_blockref("CONTENT", (5, 5))
        name = "PARENT"
    else:
        name = "CONTENT"
    first = cad.msp.add_blockref(name, (20, 20, 7))
    second = cad.msp.add_blockref(name, (50, 50, 7))
    if not nested:
        first.add_auto_attribs({"FLOW": "12.00"})
        second.add_auto_attribs({"FLOW": "12.00"})
    return first, second


@pytest.mark.parametrize("nested", [False, True])
def test_block_fixed_text_uses_copy_on_write_and_preserves_other_instances(cad, nested, tmp_path):
    first, second = make_block(cad, nested=nested)
    h1, h2 = handle(first), handle(second)
    original_definition = first.dxf.name
    other = cad.get_entity_properties(h2)
    before = cad.get_entity_properties(h1)
    key = next(key for key, value in before["properties"]["block_texts"].items() if value == "原说明")
    patch = {"block_texts": {key: "新说明"}}
    if not nested:
        patch["block_attributes"] = {"flow": "24.00"}
        patch.update(position=[22, 25], rotation=30.0, x_scale=2.0, y_scale=2.0)
    original_db = database(cad)
    preview = cad.edit_entity_properties(h1, patch)
    assert database(cad) == original_db
    applied = cad.edit_entity_properties(h1, patch, dry_run=False)
    assert applied["verified"]
    assert applied["changes"] == preview["changes"]
    assert first.dxf.name != original_definition
    assert first.dxf.insert.z == 7 and first.dxf.zscale == 1
    assert cad.get_entity_properties(h2) == other
    assert applied["after"]["properties"]["block_texts"][key] == "新说明"
    if not nested:
        assert first.attribs[0].dxf.text == "24.00"
        assert first.attribs[0].dxf.insert.z == 7
    names = set(cad.doc.blocks.block_names())
    assert not cad.edit_entity_properties(h1, patch, dry_run=False)["would_write"]
    assert set(cad.doc.blocks.block_names()) == names
    path = tmp_path / "blocks.dxf"
    cad.doc.saveas(path)
    cad.doc = ezdxf.readfile(path)
    cad.msp = cad.doc.modelspace()
    assert cad.get_entity_properties(h2) == other
    assert cad.get_entity_properties(h1) == applied["after"]
    assert not cad.doc.audit().has_errors


@pytest.mark.parametrize("patch", [
    {"block_attributes": {"MISSING": "3"}}, {"block_attributes": {"FLOW": "%<field>%"}},
    {"block_texts": {"99": "unknown"}}, {"block_texts": {"1": "line\nline"}},
])
def test_invalid_block_updates_are_zero_write(cad, patch):
    first, _ = make_block(cad)
    before = database(cad)
    with pytest.raises((ValueError, UnsupportedOperationError)):
        cad.edit_entity_properties(handle(first), {"color": 1, **patch}, dry_run=False)
    assert database(cad) == before


def test_block_copy_failure_restores_all_definitions_and_instance(cad, monkeypatch):
    first, second = make_block(cad)
    h = handle(first)
    before = cad.get_entity_properties(h)
    other = cad.get_entity_properties(handle(second))
    blocks = set(cad.doc.blocks.block_names())
    original = block_contents._write_text

    def fail(access, item, value):
        original(access, item, value)
        raise RuntimeError("injected failure after cloned text write")

    monkeypatch.setattr(block_contents, "_write_text", fail)
    with pytest.raises(RuntimeError) as error:
        cad.edit_entity_properties(h, {"block_texts": {"1": "new"}}, dry_run=False)
    assert error.value.rollback_status == "succeeded"
    assert cad.get_entity_properties(h) == before
    assert cad.get_entity_properties(handle(second)) == other
    assert set(cad.doc.blocks.block_names()) == blocks


def test_hatch_changes_patterns_preserving_holes_and_associations(cad):
    outer = cad.msp.add_lwpolyline([(0, 0), (20, 0), (20, 20), (0, 20)], close=True)
    inner = cad.msp.add_circle((10, 10), 2)
    e = cad.msp.add_hatch()
    e.paths.add_polyline_path([(0, 0), (20, 0), (20, 20), (0, 20)], is_closed=True)
    e.paths.add_edge_path(flags=0).add_arc((10, 10), 2, 0, 360)
    e.associate(e.paths[0], [outer])
    e.associate(e.paths[1], [inner])
    e.set_pattern_fill("ANSI31")
    h = handle(e)
    before = cad.get_entity_properties(h)
    for patch in ({"pattern": "ANSI32", "pattern_scale": 2.5, "pattern_angle": 30.0},
                  {"pattern": "SOLID"}, {"pattern": "ANSI31", "pattern_scale": 1.5}):
        original_db = database(cad)
        cad.edit_entity_properties(h, patch)
        assert database(cad) == original_db
        applied = cad.edit_entity_properties(h, patch, dry_run=False)
        assert applied["verified"]
        assert applied["after"]["boundary"] == before["boundary"]
        assert not cad.edit_entity_properties(h, patch, dry_run=False)["would_write"]
    with pytest.raises(UnsupportedOperationError, match="关联"):
        cad.edit_entity_properties(handle(outer), {"closed": False}, dry_run=False)
    with pytest.raises(UnsupportedOperationError, match="图案"):
        cad.edit_entity_properties(h, {"pattern": "FAKE_MISSING"}, dry_run=False)
    assert not cad.doc.audit().has_errors


def test_nested_property_serialization_preserves_unspecified_segment_data():
    model = EntityPropertyPatch(vertices=vertices((0, 0), (10, 0)))
    serialized = model.model_dump(mode="json")
    assert serialized == {"vertices": vertices((0, 0), (10, 0))}
    assert EntityPropertyPatch.model_validate(serialized).model_dump(mode="json") == serialized


def test_uniform_only_block_refuses_xy_only_scaling_before_write(cad):
    first, _ = make_block(cad)
    cad.doc.blocks.get("CONTENT").block_record.dxf.scale = 1
    before = database(cad)
    with pytest.raises(UnsupportedOperationError, match="统一缩放"):
        cad.edit_entity_properties(handle(first), {"x_scale": 2.0, "y_scale": 2.0}, dry_run=False)
    assert database(cad) == before


def test_gradient_hatch_is_not_converted_to_plain_fill(cad):
    e = cad.msp.add_hatch()
    e.paths.add_polyline_path([(0, 0), (10, 0), (10, 10)], is_closed=True)
    e.set_gradient()
    before = database(cad)
    with pytest.raises(UnsupportedOperationError, match="渐变"):
        cad.edit_entity_properties(handle(e), {"pattern": "SOLID"}, dry_run=False)
    assert database(cad) == before


def test_block_recursive_reference_stops_before_any_write(cad):
    block = cad.doc.blocks.new("CYCLE")
    block.add_blockref("CYCLE", (0, 0))
    e = cad.msp.add_blockref("CYCLE", (0, 0))
    before = database(cad)
    with pytest.raises(UnsupportedOperationError, match="循环"):
        cad.edit_entity_properties(handle(e), {"color": 1}, dry_run=False)
    assert database(cad) == before


def test_constant_width_and_segment_width_contract(cad):
    e = cad.msp.add_lwpolyline([(0, 0), (10, 0), (10, 10)])
    assert cad.edit_entity_properties(handle(e), {"constant_width": 1.5}, dry_run=False)["verified"]
    patch = {"vertices": [{"x": 0, "y": 0, "start_width": 2.0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}],
             "constant_width": 1.0}
    with pytest.raises(ValueError, match="常量线宽"):
        cad.edit_entity_properties(handle(e), patch, dry_run=False)


def test_variable_width_is_explicit_and_can_be_reset_to_constant_or_zero(cad):
    e = cad.msp.add_lwpolyline([(0, 0, 0.2, 0.4, 0.5), (10, 0, 0, 0, 0)], format="xyseb")
    h = handle(e)
    assert cad.get_entity_properties(h)["properties"]["constant_width"] is None
    assert cad.edit_entity_properties(h, {"constant_width": 1.5}, dry_run=False)["verified"]
    result = cad.edit_entity_properties(h, {"vertices": vertices((1, 2), (12, 4)), "constant_width": 2.0}, dry_run=False)
    assert result["verified"] and result["after"]["properties"]["constant_width"] == 2.0
    result = cad.edit_entity_properties(h, {"constant_width": 0.0}, dry_run=False)
    assert result["verified"] and result["after"]["properties"]["constant_width"] == 0.0


def test_native_block_with_extension_dictionary_is_rejected_before_clone():
    from types import SimpleNamespace
    definition = SimpleNamespace(IsXRef=False, HasExtensionDictionary=True)
    access = SimpleNamespace(mode="acad", backend=SimpleNamespace(
        doc=SimpleNamespace(Blocks=SimpleNamespace(Item=lambda name: definition))))
    reference = SimpleNamespace(Name="SPECIAL", IsDynamicBlock=False)
    with pytest.raises(UnsupportedOperationError, match="扩展字典"):
        block_contents._static_block(access, reference)
