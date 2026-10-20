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
