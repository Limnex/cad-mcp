"""Extreme view regression using only an owned, unsaved CAD drawing.

Unexpected failures retain the test document for inspection and stop CAD writes.
"""
import json
import os
import runpy
from pathlib import Path

import pytest

from cad_mcp import server
from cad_mcp.core.semantics import _transaction_state
from cad_mcp.drawing.autocad import AcadBackend
from cad_mcp.drawing.fillet import _point_variant, _same_view, _view_state

pytestmark = pytest.mark.acad
if os.environ.get("CAD_MCP_RUN_ACAD_INTEGRATION") != "1":
    pytest.skip("requires authorization and an already running AutoCAD", allow_module_level=True)


def test_line_pair_extreme_view_restores_unsaved_drawing(tmp_path, monkeypatch):
    import pythoncom
    import win32com.client

    # Probe without starting CAD; every write below targets our new drawing.
    application = win32com.client.GetActiveObject("AutoCAD.Application")
    helpers = runpy.run_path(str(Path(__file__).with_name("test_semantic_acad.py")))
    originals = helpers["_documents"](application)
    active = str(application.ActiveDocument.Name) if originals else None
    original_active = next((r for r in originals if r["name"] == active), None)
    assert server._SETTINGS.semantic_policy == "strict"
    raw = AcadBackend(tmp_path)
    cad = server._AcadLockProxy(raw)
    monkeypatch.setattr(server, "_backend", cad)

    def protected():
        assert not helpers["_document_changes"](originals, helpers["_documents"](application))

    def call(name, params):
        protected()
        result = json.loads(getattr(server, name)(params))
        protected()
        assert result["ok"], result
        return result

    assert cad.health(reconnect=True)["connected"]
    call("cad_new_drawing", server.NewDrawingInput(filename="fillet-extreme-view.dwg"))
    owned = str(raw.doc.Name)
    assert raw._own_document and owned not in {r["name"] for r in originals}
    raw.doc.SetVariable("TILEMODE", win32com.client.VARIANT(pythoncom.VT_I2, 1))
    call("cad_register_semantic_object", server.RegisterSemanticObjectInput(object={
        "project_id": "VIEW-REGRESSION", "object_id": "A", "name": "圆角视图回归",
        "object_type": "test_fixture", "source": "用户批准的未保存图纸测试"}))
    binding = {"project_id": "VIEW-REGRESSION", "object_id": "A", "role": "测试轮廓"}
    first = call("cad_draw_line", server.DrawLineInput(x1=40, y1=0, x2=0, y2=0, semantic=binding))["handle"]
    second = call("cad_draw_line", server.DrawLineInput(x1=0, y1=0, x2=0, y2=40, semantic=binding))["handle"]
    raw._acquire_autocad_lock()
    try:
        raw._wait_operation_ready()
        raw.acad.ZoomCenter(_point_variant([1.4e8, 3e7, 0]), 1.3e8)
        raw._wait_operation_ready()
    finally:
        raw._release_autocad_lock()
    saved = _view_state(raw)
    before = _transaction_state(raw)
    params = dict(handles=[first, second], pick_points=[[30, 0, 0], [0, 30, 0]], radius=5)
    preview = call("cad_fillet", server.FilletInput(**params))
    assert preview["dry_run"] and not preview["applied"]
    assert _transaction_state(raw) == before and _same_view(_view_state(raw), saved)
    result = call("cad_fillet", server.FilletInput(**params, dry_run=False))
    assert result["verified"] and result["rounded_arc_count"] == 1
    assert _same_view(_view_state(raw), saved)
    semantics = call("cad_check_semantics", server.CheckSemanticsInput())
    handles = {first, second, *[e["handle"] for e in result["created"]]}
    rows = [e for e in semantics["entities"] if e["handle"] in handles]
    assert semantics["complete"] and len(rows) == 3
    assert all(e["status"] == "registered" for e in rows)
    assert len({e["metadata"]["entity_uid"] for e in rows}) == 3
    assert str(raw.doc.Name) == owned and str(raw.doc.FullName) == ""
    assert not list(tmp_path.glob("*.dwg"))
    call("cad_close_drawing", server.CloseDrawingInput(save=False))
    helpers["_restore_original_active"](application, original_active)
    assert not helpers["_final_document_changes"](originals, helpers["_documents"](application))
    print(json.dumps({"extreme_view_fillet": result, "view_restored": True,
                      "originals_unchanged": True, "closed_without_save": True}, ensure_ascii=False))
