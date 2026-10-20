"""Opt-in property edits in an owned unsaved drawing, attached to existing CAD."""

import json
import os

import pytest

from cad_mcp import server
from cad_mcp.drawing.autocad import AcadBackend
from cad_mcp.drawing.entity_properties import EntityAccess
from cad_mcp.server import _AcadLockProxy


pytestmark = pytest.mark.acad
if os.environ.get("CAD_MCP_RUN_ACAD_INTEGRATION") != "1":
    pytest.skip("requires an authorized, already running AutoCAD", allow_module_level=True)


def snapshot(application):
    docs = application.Documents
    return sorted((str(docs.Item(i).Name), str(docs.Item(i).FullName), bool(docs.Item(i).Saved))
                  for i in range(int(docs.Count)))


def test_entity_property_tools_against_existing_autocad(tmp_path, monkeypatch):
    raw = AcadBackend(tmp_path)
    cad = _AcadLockProxy(raw)
    assert cad.health(reconnect=False)["connected"] is False
    assert cad.health(reconnect=True)["connected"] is True
    application = raw.acad
    before_documents = snapshot(application)
    original_active = application.ActiveDocument if before_documents else None
    evidence = []
    monkeypatch.setattr(server, "_get_backend", lambda: cad)

    def read(handle):
        response = json.loads(server.cad_get_entity_properties(server.GetEntityPropertiesInput(handle=handle)))
        assert response["ok"], response
        return response

    def edit(handle, patch, dry_run=True):
        return json.loads(server.cad_edit_entity_properties(server.EditEntityPropertiesInput(
            handle=handle, properties=patch, dry_run=dry_run)))

    try:
        cad.new_drawing("entity-property-validation.dwg")
        cad.create_layer("CAD-MCP-EDIT", color=2)
        cases = [
            (cad.draw_line(0, 0, 10, 0), {"start": [1, 2, 0], "end": [15, 6, 0]}),
            (cad.draw_circle(30, 0, 3), {"center": [35, 5, 0], "radius": 5.0}),
            (cad.draw_text("原文字", 0, 20, 3), {"text": "修改文字", "height": 5.0, "rotation": 30.0}),
            (cad.draw_mtext("原多行", 30, 20, 30, 3),
             {"text": "首行\\P次行", "height": 5.0, "rotation": 45.0}),
        ]
        for created, patch in cases:
            handle = created["handle"]
            patch = {"layer": "CAD-MCP-EDIT", "color": 1, "linetype": "Continuous",
                     "lineweight": 50, "linetype_scale": 2.0, **patch}
            before = read(handle)
            count = int(raw.msp.Count)
            preview = edit(handle, patch)
            assert preview["ok"] and preview["dry_run"] and not preview["applied"], preview
            assert read(handle) == before
            assert int(raw.msp.Count) == count
            changed = edit(handle, patch, dry_run=False)
            assert changed["ok"] and changed["verified"] and changed["applied"], changed
            assert read(handle)["properties"] == changed["after"]["properties"]
            noop = edit(handle, patch, dry_run=False)
            assert noop["ok"] and not noop["would_write"] and not noop["applied"], noop
            evidence.append({"type": created["type"], "handle": handle,
                             "readback": changed["after"]["properties"]})

        # A real partially completed write must be rolled back by the MCP undo group.
        circle_handle = cases[1][0]["handle"]
        before_circle = read(circle_handle)
        original_write = EntityAccess.write

        def fail_after_color(self, name, value):
            if name == "radius":
                raise RuntimeError("injected property failure for rollback verification")
            original_write(self, name, value)

        with monkeypatch.context() as scoped:
            scoped.setattr(EntityAccess, "write", fail_after_color)
            failed = edit(circle_handle, {"color": 3, "radius": 6.0}, dry_run=False)
        assert failed["ok"] is False, failed
        assert failed["error_detail"]["details"]["rollback_status"] == "succeeded", failed
        assert read(circle_handle) == before_circle

        # Locked-layer and wrong-type requests must stop without modifying anything.
        raw.doc.Layers.Item("CAD-MCP-EDIT").Lock = True
        rejected = edit(circle_handle, {"radius": 7.0}, dry_run=False)
        assert rejected["ok"] is False, rejected
        assert read(circle_handle) == before_circle
        raw.doc.Layers.Item("CAD-MCP-EDIT").Lock = False
        rejected = edit(circle_handle, {"text": "wrong type"}, dry_run=False)
        assert rejected["error_detail"]["code"] == "UNSUPPORTED_OPERATION", rejected
        assert read(circle_handle) == before_circle
        assert "未完成" not in cad.check_drawing()
        assert not (tmp_path / "entity-property-validation.dwg").exists()
    finally:
        if raw._own_document:
            cad.close_document(save=False)
        if original_active is not None:
            raw._acquire_autocad_lock()
            try:
                original_active.Activate()
            finally:
                raw._release_autocad_lock()

    assert snapshot(application) == before_documents
    print(json.dumps({"properties": evidence, "rollback_verified": True,
                      "existing_documents_unchanged": True,
                      "temporary_drawing_closed_without_save": True}, ensure_ascii=False))


def test_planar_properties_and_block_contents_in_existing_autocad(tmp_path, monkeypatch):
    from cad_mcp.drawing.autocad import _pt, _pt2
    from cad_mcp.dwt.autocad import _dispatch_array
    from cad_mcp.drawing.hatch_regions import refresh_valid_hatch
    from cad_mcp.drawing import block_contents

    raw = AcadBackend(tmp_path)
    cad = _AcadLockProxy(raw)
    assert cad.health(reconnect=False)["connected"] is False
    assert cad.health(reconnect=True)["connected"] is True
    application = raw.acad
    before_documents = snapshot(application)
    original_active = application.ActiveDocument if before_documents else None
    monkeypatch.setattr(server, "_get_backend", lambda: cad)
    evidence = []

    def read(h):
        value = json.loads(server.cad_get_entity_properties(server.GetEntityPropertiesInput(handle=h)))
        assert value["ok"], value
        return value

    def edit(h, patch, dry_run=True):
        return json.loads(server.cad_edit_entity_properties(server.EditEntityPropertiesInput(
            handle=h, properties=patch, dry_run=dry_run)))

    try:
        cad.new_drawing("planar-edit-validation.dwg")
        cad.create_layer("CAD-MCP-PLANAR", color=3)
        # Set up native fixtures in one owned undo group, only in this unsaved document.
        raw._acquire_autocad_lock()
        started = False
        try:
            raw._wait_operation_ready()
            started = raw._begin_undo()
            arc = raw.msp.AddArc(_pt(0, 0, 7), 3.0, 0.0, 1.5)
            poly = raw.msp.AddLightWeightPolyline(_pt2([0, 10, 10, 10, 10, 20]))
            poly.Elevation = 7.0
            point = raw.msp.AddPoint(_pt(20, 0, 7))
            ellipse = raw.msp.AddEllipse(_pt(30, 0, 7), _pt(5, 0, 0), 0.5)
            definition = raw.doc.Blocks.Add(_pt(0, 0), "CAD_MCP_CONTENT_TEST")
            definition.AddLine(_pt(0, 0), _pt(10, 0))
            definition.AddText("原说明", _pt(0, 2), 2.5)
            definition.AddMText(_pt(0, 4), 20.0, "12.00")
            definition.AddAttribute(2.5, 0, "流量", _pt(0, 6), "FLOW", "12.00")
            label = definition.AddAttribute(2.5, 0, "编号", _pt(5, 6), "LABEL", "01")
            label.Alignment = 1
            label.TextAlignmentPoint = _pt(5, 6)
            first = raw.msp.InsertBlock(_pt(0, 40, 7), "CAD_MCP_CONTENT_TEST", 1.0, 1.0, 1.0, 0.0)
            second = raw.msp.InsertBlock(_pt(30, 40, 7), "CAD_MCP_CONTENT_TEST", 1.0, 1.0, 1.0, 0.0)
            outer = raw.msp.AddLightWeightPolyline(_pt2([50, 0, 70, 0, 70, 20, 50, 20]))
            outer.Closed = True
            hole = raw.msp.AddCircle(_pt(60, 10), 2.0)
            hatch = raw.msp.AddHatch(1, "ANSI31", True, 0)
            hatch.AppendOuterLoop(_dispatch_array([outer]))
            hatch = refresh_valid_hatch(hatch)
            hatch.AppendInnerLoop(_dispatch_array([hole]))
            hatch.Evaluate()
            raw._commit_undo(started)
        except Exception:
            raw._rollback_undo(started)
            raise
        finally:
            raw._release_autocad_lock()

        h1, h2 = raw._handle_str(first.Handle), raw._handle_str(second.Handle)
        other_block = read(h2)
        fixed = read(h1)["properties"]["block_texts"]
        path = next(key for key, text in fixed.items() if text == "原说明")
        cases = [
            (arc, {"center": [1.0, 2.0], "radius": 5.0, "start_angle": 20.0, "end_angle": 210.0}),
            (poly, {"vertices": [{"x": 1.123456789, "y": 10.0}, {"x": 15.0, "y": 10.0},
                                 {"x": 15.0, "y": 22.0}], "closed": True}),
            (point, {"position": [22.123456789, 4.987654321]}),
            (ellipse, {"center": [32.0, 2.0], "major_axis": [6.0, 2.0], "radius_ratio": 0.4,
                       "start_parameter": 0.2, "end_parameter": 5.5}),
            (first, {"position": [5.0, 45.0], "rotation": 30.0, "x_scale": 2.0, "y_scale": 2.0,
                     "block_attributes": {"FLOW": "24.00"}, "block_texts": {path: "修改后的说明"}}),
            (hatch, {"pattern": "ANSI32", "pattern_scale": 2.0, "pattern_angle": 30.0}),
        ]
        for entity, patch in cases:
            h = raw._handle_str(entity.Handle)
            before = read(h)
            count = int(raw.msp.Count)
            blocks = int(raw.doc.Blocks.Count)
            preview = edit(h, patch)
            assert preview["ok"] and preview["dry_run"] and not preview["applied"], preview
            assert read(h) == before
            assert int(raw.msp.Count) == count and int(raw.doc.Blocks.Count) == blocks
            applied = edit(h, patch, dry_run=False)
            assert applied["ok"] and applied["verified"] and applied["applied"], applied
            assert read(h)["properties"] == applied["after"]["properties"]
            assert applied["after"]["elevation"] == before["elevation"]
            noop = edit(h, patch, dry_run=False)
            assert noop["ok"] and not noop["would_write"], noop
            evidence.append({"type": before["type"], "readback": applied["after"]})

        assert read(h2) == other_block
        assert read(h1)["properties"]["block_attributes"]["FLOW"] == "24.00"
        # Verify attribute geometry follows the selected reference transform.
        attribute = list(first.GetAttributes())[0]
        assert float(attribute.InsertionPoint[2]) == 7.0
        expected = (5.0 - 12.0 * 0.5, 45.0 + 12.0 * (3.0 ** 0.5 / 2.0))
        assert tuple(attribute.InsertionPoint)[:2] == pytest.approx(expected)
        # Independent XY factors must work without changing Z or losing values.
        anisotropic = edit(h1, {"position": [7.0, 45.0], "rotation": 45.0,
                                "x_scale": 3.0, "y_scale": 1.5}, dry_run=False)
        assert anisotropic["ok"] and anisotropic["verified"], anisotropic
        assert read(h2) == other_block
        attribute = list(first.GetAttributes())[0]
        expected = (7.0 - 9.0 * (0.5 ** 0.5), 45.0 + 9.0 * (0.5 ** 0.5))
        assert tuple(attribute.InsertionPoint)[:2] == pytest.approx(expected)
        assert float(attribute.InsertionPoint[2]) == 7.0
        assert str(attribute.TextString) == "24.00"
        centered = next(item for item in first.GetAttributes() if str(item.TagString) == "LABEL")
        expected_center = (7.0 + 6.0 * (0.5 ** 0.5), 45.0 + 24.0 * (0.5 ** 0.5))
        assert tuple(centered.TextAlignmentPoint)[:2] == pytest.approx(expected_center)
        assert str(centered.TextString) == "01"

        hp = raw._handle_str(poly.Handle)
        segmented = edit(hp, {"vertices": [
            {"x": 1.123456789, "y": 10.0, "bulge": 0.5, "start_width": 0.2, "end_width": 0.4},
            {"x": 15.0, "y": 10.0}, {"x": 15.0, "y": 22.0}]}, dry_run=False)
        assert segmented["ok"] and segmented["verified"], segmented
        assert read(hp)["properties"]["vertices"][0]["bulge"] == pytest.approx(0.5)
        widened = edit(hp, {"constant_width": 1.5}, dry_run=False)
        assert widened["ok"] and widened["verified"], widened

        # Both transitions preserve the two original loops, including the hole.
        hh = raw._handle_str(hatch.Handle)
        boundary = read(hh)["boundary"]
        for patch in ({"pattern": "SOLID"}, {"pattern": "ANSI31", "pattern_scale": 1.5}):
            applied = edit(hh, patch, dry_run=False)
            assert applied["ok"] and applied["verified"], applied
            assert read(hh)["boundary"] == boundary

        # Inject a failure after text has been written into a new definition.
        original_write = block_contents._write_text
        before = read(h1)
        blocks_before = int(raw.doc.Blocks.Count)

        def fail_after_text(access, item, value):
            original_write(access, item, value)
            raise RuntimeError("injected isolated block text failure")

        with monkeypatch.context() as scoped:
            scoped.setattr(block_contents, "_write_text", fail_after_text)
            failed = edit(h1, {"block_texts": {path: "rollback test"}}, dry_run=False)
        assert failed["ok"] is False, failed
        assert failed["error_detail"]["details"]["rollback_status"] == "succeeded", failed
        assert read(h1) == before and read(h2) == other_block
        assert int(raw.doc.Blocks.Count) == blocks_before
        assert "未完成" not in cad.check_drawing()
        assert not (tmp_path / "planar-edit-validation.dwg").exists()
    finally:
        if raw._own_document:
            cad.close_document(save=False)
        if original_active is not None:
            raw._acquire_autocad_lock()
            try:
                original_active.Activate()
            finally:
                raw._release_autocad_lock()

    assert snapshot(application) == before_documents
    print(json.dumps({"planar_entities": evidence, "same_name_block_unchanged": True,
                      "clone_rollback_verified": True, "existing_documents_unchanged": True,
                      "temporary_drawing_closed_without_save": True}, ensure_ascii=False))
