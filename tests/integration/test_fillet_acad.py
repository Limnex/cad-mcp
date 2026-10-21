"""Opt-in native FILLET round trips in an owned, unsaved AutoCAD drawing."""

import json
import os

import pytest
import win32com.client

from cad_mcp import server
from cad_mcp.drawing.autocad import AcadBackend


pytestmark = pytest.mark.acad
if os.environ.get("CAD_MCP_RUN_ACAD_INTEGRATION") != "1":
    pytest.skip("requires authorization and an already running AutoCAD", allow_module_level=True)


def test_native_fillet_roundtrips(tmp_path, monkeypatch):
    # Probe only: never let a real round-trip test cold-start AutoCAD.
    try:
        application = win32com.client.GetActiveObject("AutoCAD.Application")
    except Exception:
        pytest.fail("请先手动启动 AutoCAD；圆角测试不会冷启动应用")
    original = application.ActiveDocument if application.Documents.Count else None
    original_names = [application.Documents.Item(i).Name for i in range(application.Documents.Count)]
    raw = AcadBackend(tmp_path)
    cad = server._AcadLockProxy(raw)
    monkeypatch.setattr(server, "_backend", cad)
    evidence = []

    def apply(**params):
        response = json.loads(server.cad_fillet(server.FilletInput(**params)))
        assert response["ok"], response
        return response

    try:
        assert cad.health(reconnect=True)["connected"]
        cad.new_drawing("native-fillet-validation.dwg")
        settings = {name: raw.doc.GetVariable(name)
                    for name in ("FILLETRAD", "TRIMMODE", "OSMODE", "CMDECHO", "USERS5")}

        # Trim and NoTrim, including WCS pick points while the UCS is rotated.
        for index, trim in enumerate((True, False)):
            x = index * 100
            first = cad.draw_line(x+40, 0, x, 0)["handle"]
            second = cad.draw_line(x, 0, x, 40)["handle"]
            params = dict(handles=[first, second], pick_points=[[x+30, 0, 0], [x, 30, 0]],
                          radius=5, trim=trim)
            count = raw.msp.Count
            preview = apply(**params)
            assert not preview["applied"] and not preview["verified"]
            assert raw.msp.Count == count
            if index == 1:
                raw._send_command_and_wait('_.UCS\n_Z\n30\n')
            try:
                result = apply(**params, dry_run=False)
            finally:
                if index == 1:
                    raw._send_command_and_wait('_.UCS\n_World\n')
            assert result["verified"] and result["rounded_arc_count"] == 1
            if not trim:
                assert result["before"] == result["after"]
            evidence.append(result)

        poly = cad.draw_polyline([(200, 0), (240, 0), (240, 40), (200, 40)], close=True)["handle"]
        rounded = apply(mode="polyline", handles=[poly], radius=5, dry_run=False)
        assert rounded["rounded_arc_count"] == 4
        sharp = apply(mode="polyline", handles=[poly], radius=0, dry_run=False)
        assert sharp["verified"] and sharp["rounded_arc_count"] == 0

        # Native rejection/no-op must not undo the preceding successful edit.
        a = cad.draw_line(300, 0, 340, 0)["handle"]
        b = cad.draw_line(350, 0, 390, 0)["handle"]
        failed = json.loads(server.cad_fillet(server.FilletInput(
            handles=[a, b], pick_points=[[330, 0, 0], [360, 0, 0]], radius=5, dry_run=False)))
        assert not failed["ok"]
        assert cad.get_entity_properties(b)["properties"]["end"] == [390, 0, 0]
        assert all(raw.doc.GetVariable(name) == value for name, value in settings.items())
        assert not (tmp_path / "native-fillet-validation.dwg").exists()
    finally:
        if raw._own_document:
            cad.close_document(save=False)
        if original is not None:
            raw._acquire_autocad_lock()
            try:
                original.Activate()
            finally:
                raw._release_autocad_lock()
    assert [application.Documents.Item(i).Name for i in range(application.Documents.Count)] == original_names
    print(json.dumps({"native_fillet": evidence, "temporary_drawing_closed_without_save": True}, ensure_ascii=False))
