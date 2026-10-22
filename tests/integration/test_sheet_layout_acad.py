"""Opt-in isolated AutoCAD layout acceptance; never cold-start AutoCAD.

An operator must supply actual A2 landscape plot resources. The test creates
its own drawings and template; it never binds or saves an existing user DWG.
"""

import hashlib
import json
import os
from pathlib import Path

import pytest

from cad_mcp.drawing.autocad import AcadBackend, _pt, _pt2
from cad_mcp.server import _AcadLockProxy


pytestmark = pytest.mark.acad

if os.environ.get("CAD_MCP_RUN_ACAD_INTEGRATION") != "1":
    pytest.skip("explicit opt-in required for real AutoCAD layout acceptance",allow_module_level=True)


def test_isolated_frame_paper_content_roundtrip_and_pdf(tmp_path):
    resources = [os.environ.get("CAD_MCP_LAYOUT_TEST_"+field) for field in ("DEVICE","MEDIA","STYLE")]
    if not all(resources):
        pytest.skip("provide verified A2 landscape DEVICE, MEDIA and STYLE resources")
    device,media,style = resources
    raw = AcadBackend(tmp_path)
    cad = _AcadLockProxy(raw)
    opened = False
    try:
        # Backend new_drawing only attaches to an already running instance.
        cad.new_drawing("layout-acceptance.dwg")
        opened = True
        raw.doc.Layers.Add("TEST-PAPER-REFERENCE").Plottable = False
        block = raw.doc.Blocks.Add(_pt(0,0),"TEST-FRAME")
        outer = block.AddLightWeightPolyline(_pt2([0,0,594,0,594,420,0,420]))
        outer.Closed = True
        outer.Layer = "TEST-PAPER-REFERENCE"
        inner = block.AddLightWeightPolyline(_pt2([10,10,584,10,584,410,10,410]))
        inner.Closed = True
        block.AddLine(_pt(510,10),_pt(510,410))
        raw.doc.SaveAs(str(tmp_path/"template.dwg"),64)
        source = tmp_path/"template.dwg"
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        # SaveAs creates a separate working file; no later writes target source.
        raw.doc.SaveAs(str(tmp_path/"working.dwg"),64)
        raw.current_path = tmp_path/"working.dwg"
        raw.doc.ActiveLayer = raw.doc.Layers.Item("0")
        raw.msp.AddLine(_pt(10000,20000),_pt(11000,21000))
        cad.create_or_update_page_setup(name="TEST-PDF-A2",device=device,media_name=media,
            rotation=0,plot_style_sheet=style,plot_type="layout",paper_units="millimeters",
            plot_scale={"mode":"standard","standard_scale":"1:1"},center_plot=False,
            plot_with_lineweights=True,plot_with_plot_styles=True)
        manifest = tmp_path/"manifest.json"
        manifest.write_text(json.dumps(dict(template_id="test",source_dwg=str(source),
            source_sha256=source_hash,sheets=[dict(id="A2",paper_size="A2",paper_width=594,paper_height=420,
                block_name="TEST-FRAME",source_bounds=[0,0,594,420],fixed_insert_scale=1,
                paper_reference_bounds=[0,0,594,420],printed_bounds=[10,10,584,410],
                usable_regions=[dict(id="main",x=20,y=20,width=480,height=380)],
                reserved_regions=[dict(id="title",x=510,y=10,width=74,height=400)])])),encoding="utf-8")
        params = dict(manifest_path=str(manifest),sheet_id="A2",layout_name="TEST-A2",layout_id="test-sheet",
            region_id="main",page_setup_name="TEST-PDF-A2",content_bounds=[10000,20000,11000,21000],
            model_mm_per_unit=1,scale_denominator=100)
        original_layout = str(raw.doc.ActiveLayout.Name)
        original_tilemode = int(raw.doc.GetVariable("TILEMODE"))
        preview = cad.preview_sheet_layout(**params)
        assert not preview["plan"]["conflicts"]
        result = cad.apply_sheet_layout(**params,confirmation_token=preview["confirmation_token"])
        assert result["verified"],result
        assert str(raw.doc.ActiveLayout.Name) == original_layout
        assert int(raw.doc.GetVariable("TILEMODE")) == original_tilemode
        assert result["viewport"]["paper_center"][0] < 594/2
        second = cad.preview_sheet_layout(**params)
        updated = cad.apply_sheet_layout(**params,confirmation_token=second["confirmation_token"])
        assert updated["viewport_handle"] == result["viewport_handle"]
        assert updated["frame_handle"] == result["frame_handle"]
        cad.save()
        cad.close_drawing(save=False)
        opened = False
        cad.open_drawing(str(tmp_path/"working.dwg"))
        opened = True
        state_before = (int(raw.doc.GetVariable("DBMOD")),bool(raw.doc.Saved),str(raw.doc.ActiveLayout.Name))
        checked = cad.validate_sheet_layout(**params)
        assert checked["verified"],checked
        assert state_before == (int(raw.doc.GetVariable("DBMOD")),bool(raw.doc.Saved),str(raw.doc.ActiveLayout.Name))
        exported = cad.publish_layout_pdf("TEST-A2","layout-proof.pdf")
        pdf = Path(exported["path"])
        assert pdf.read_bytes().startswith(b"%PDF-")
        assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    finally:
        if opened:
            cad.close_drawing(save=False)
