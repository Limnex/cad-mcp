"""Opt-in preview in an owned temporary drawing in an already running AutoCAD."""

import os
from pathlib import Path

import pytest

from cad_mcp.drawing.autocad import AcadBackend
from cad_mcp.server import _AcadLockProxy


pytestmark = pytest.mark.acad
if os.environ.get("CAD_MCP_RUN_ACAD_INTEGRATION") != "1":
    pytest.skip("requires an authorized, already running AutoCAD", allow_module_level=True)


def documents(application):
    return sorted((str(application.Documents.Item(i).Name),
                   str(application.Documents.Item(i).FullName),
                   bool(application.Documents.Item(i).Saved))
                  for i in range(int(application.Documents.Count)))


def test_preview_live_dwg_without_saving_or_changing_view(tmp_path):
    raw = AcadBackend(tmp_path / "drawings")
    cad = _AcadLockProxy(raw)
    assert cad.health(reconnect=True)["connected"]
    application = raw.acad
    before_documents = documents(application)
    original_active = application.ActiveDocument if before_documents else None

    def drawing_state():
        return (str(raw.doc.FullName), bool(raw.doc.Saved), int(raw.msp.Count),
                tuple(raw.doc.GetVariable("VIEWCTR")), float(raw.doc.GetVariable("VIEWSIZE")),
                str(raw.doc.ActiveLayout.Name), int(raw.doc.SelectionSets.Count))

    try:
        cad.new_drawing("preview-validation.dwg")
        cad.draw_line(-10, 5, 20, 5)
        cad.draw_circle(5, 5, 2)
        before = drawing_state()
        metadata = cad.preview_region(log_dir=tmp_path / "logs", x_min=0, y_min=0,
                                      x_max=10, y_max=10, width=300, height=300)
        assert Path(metadata["image_path"]).is_file()
        assert metadata["mode"] == "acad"
        assert metadata["source"] == "autocad_dxf_export"
        assert drawing_state() == before
        assert not (tmp_path / "drawings" / "preview-validation.dwg").exists()
    finally:
        if raw._own_document:
            cad.close_document(save=False)
        if original_active is not None:
            raw._acquire_autocad_lock()
            try:
                original_active.Activate()
            finally:
                raw._release_autocad_lock()
    assert documents(application) == before_documents
