"""Opt-in native CHAMFER checks in one owned, unsaved AutoCAD document."""

import json
import math
import os

import pytest

from cad_mcp import server
from cad_mcp.drawing.autocad import AcadBackend, _pt
from cad_mcp.drawing.chamfer import _read, _same, fresh_com


pytestmark = pytest.mark.acad
if os.environ.get("CAD_MCP_RUN_ACAD_INTEGRATION") != "1":
    pytest.skip("requires an authorized, already running AutoCAD", allow_module_level=True)


def snapshot(app):
    docs = fresh_com(app.Documents)
    result = []
    for i in range(int(docs.Count)):
        doc = fresh_com(docs.Item(i))
        result.append((str(doc.Name), str(doc.FullName), bool(doc.Saved), int(fresh_com(doc.ModelSpace).Count)))
    return sorted(result)


def test_native_chamfer_roundtrip(tmp_path, monkeypatch):
    import pythoncom
    import win32com.client

    app = fresh_com(win32com.client.GetActiveObject("AutoCAD.Application"))
    assert fresh_com(app.GetAcadState()).IsQuiescent
    before = snapshot(app)
    previous = fresh_com(app.ActiveDocument) if before else None
    raw = AcadBackend(tmp_path)
    cad = server._AcadLockProxy(raw)
    documents = fresh_com(app.Documents)
    owned = documents.Add()
    evidence = []
    try:
        doc = fresh_com(owned)
        doc.SetVariable("USERS5", "CADMCP-CHAMFER-OWNED-TEST")
        raw.acad, raw.doc, raw.msp = app, doc, fresh_com(doc.ModelSpace)
        raw._own_document = True
        msp = raw.msp
        monkeypatch.setattr(server, "_get_backend", lambda: cad)
        variables = ("CHAMFERA", "CHAMFERB", "CHAMFERC", "CHAMFERD", "CHAMMODE", "TRIMMODE", "OSMODE")

        def call(request):
            result = json.loads(server.cad_chamfer(server.ChamferInput.model_validate(request)))
            assert result["ok"], result
            return result

        def make_pair(x, kind="Line", gap=0):
            first = fresh_com(getattr(msp, "Add" + kind)(_pt(x + gap, 0), _pt(x + 20, 0)))
            second = fresh_com(getattr(msp, "Add" + kind)(_pt(x, gap), _pt(x, 20)))
            handles = [str(int(str(e.Handle), 16)) for e in (first, second)]
            return {"mode": "pair", "first": {"handle": handles[0], "point": [x + 15, 0, 0]},
                    "second": {"handle": handles[1], "point": [x, 15, 0]}}

        def run(targets, **settings):
            request = {"targets": targets, "distance1": 2, **settings}
            values = {name: doc.GetVariable(name) for name in variables}
            view_before = (tuple(doc.GetVariable("VIEWCTR")), float(doc.GetVariable("VIEWSIZE")))
            count = int(msp.Count)
            preview = call(request)
            assert preview["dry_run"] and not preview["applied"] and not preview["verified"]
            assert int(msp.Count) == count
            assert values == {name: doc.GetVariable(name) for name in variables}
            assert tuple(doc.GetVariable("VIEWCTR")) == pytest.approx(view_before[0])
            assert float(doc.GetVariable("VIEWSIZE")) == pytest.approx(view_before[1])
            for plan in preview["plans"]:
                for h, state in plan["before"].items():
                    # For reused handles, later previews contain the simulated
                    # result of the earlier step, not the still-unmodified CAD.
                    if h not in {k for p in preview["plans"][:preview["plans"].index(plan)] for k in p["after"]}:
                        assert _same(state, _read(doc, msp, h))
            applied = call({**request, "dry_run": False})
            assert applied["verified"] and applied["applied"] == preview["would_write"]
            assert values == {name: doc.GetVariable(name) for name in variables}
            assert tuple(doc.GetVariable("VIEWCTR")) == pytest.approx(view_before[0])
            assert float(doc.GetVariable("VIEWSIZE")) == pytest.approx(view_before[1])
            evidence.append({"method": request.get("method", "distance"), "trim": request.get("trim", True),
                             "targets": len(targets), "verified": True})
            # RAY/XLINE fixtures extend indefinitely. Remove each completed
            # case so they cannot overlap a later polyline's native pick box.
            handles = {h for plan in preview["plans"] for h in plan["before"]}
            handles.update(entity["handle"] for entity in applied["entities"])
            for handle in handles:
                fresh_com(doc.HandleToObject(format(int(handle), "X"))).Delete()
            return applied

        run([make_pair(0)], distance2=3)
        angle = run([make_pair(100)], method="angle", distance1=6, angle=30)
        assert angle["entities"][0]["points"][1] == pytest.approx((100, 2 * math.sqrt(3), 0))
        run([make_pair(200)], trim=False, distance2=3)
        run([make_pair(300, gap=5)], distance1=0)
        run([make_pair(400, "Ray")], distance2=3)
        run([make_pair(500, "Xline")], distance2=3)

        for x, heavy, trim in [(600, False, True), (700, True, True), (800, False, False)]:
            flat = ([x, 0, 0, x + 20, 0, 0, x + 20, 20, 0, x, 20, 0] if heavy else
                    [x, 0, x + 20, 0, x + 20, 20, x, 20])
            coords = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, flat)
            entity = fresh_com((msp.AddPolyline if heavy else msp.AddLightWeightPolyline)(coords))
            entity.Closed = True
            h = str(int(str(entity.Handle), 16))
            run([{"mode": "polyline", "handle": h}], trim=trim, distance2=3)

        coords = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8,
                                       [900, 0, 920, 0, 920, 20, 900, 20])
        poly = fresh_com(msp.AddLightWeightPolyline(coords))
        poly.Closed = True
        h = str(int(str(poly.Handle), 16))
        run([{"mode": "pair", "first": {"handle": h, "point": [915, 0, 0]},
              "second": {"handle": h, "point": [920, 15, 0]}}], distance2=3)

        first = make_pair(1000)
        third = fresh_com(msp.AddLine(_pt(1020, 0), _pt(1020, 20)))
        second = {"mode": "pair", "first": first["first"],
                  "second": {"handle": str(int(str(third.Handle), 16)), "point": [1020, 15, 0]}}
        run([first, second])

        # A rotated UCS must not rotate the WCS selection points a second time.
        angle = math.radians(30)
        ucs = fresh_com(doc.UserCoordinateSystems.Add(_pt(0, 0), _pt(math.cos(angle), math.sin(angle)),
                                                       _pt(-math.sin(angle), math.cos(angle)), "CADMCP-CHAMFER-UCS"))
        doc.ActiveUCS = ucs
        run([make_pair(1100)], distance2=3)
        coords = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8,
                                       [1250, 0, 1270, 0, 1270, 20, 1250, 20])
        poly = fresh_com(msp.AddLightWeightPolyline(coords))
        poly.Closed = True
        h = str(int(str(poly.Handle), 16))
        run([{"mode": "pair", "first": {"handle": h, "point": [1265, 0, 0]},
              "second": {"handle": h, "point": [1270, 15, 0]}}], distance2=3)

        # Crossing/overlapping objects must never be selected in place of the
        # requested polyline. The aperture guard fails before CHAMFER runs.
        coords = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8,
                                       [1300, 0, 1320, 0, 1320, 20, 1300, 20])
        poly = fresh_com(msp.AddLightWeightPolyline(coords))
        poly.Closed = True
        h = str(int(str(poly.Handle), 16))
        overlap = fresh_com(msp.AddLine(_pt(1300, 0), _pt(1320, 0)))
        state = _read(doc, msp, h)
        overlap_state = _read(doc, msp, str(int(str(overlap.Handle), 16)))
        count = int(msp.Count)
        view_before = (tuple(doc.GetVariable("VIEWCTR")), float(doc.GetVariable("VIEWSIZE")))
        failed = json.loads(server.cad_chamfer(server.ChamferInput.model_validate({
            "targets": [{"mode": "pair", "first": {"handle": h, "point": [1315, 0, 0]},
                          "second": {"handle": h, "point": [1320, 15, 0]}}],
            "distance1": 2, "dry_run": False})))
        assert not failed["ok"] and failed["error_detail"]["details"]["rollback_status"] == "succeeded", failed
        assert int(msp.Count) == count and _same(state, _read(doc, msp, h))
        assert _same(overlap_state, _read(doc, msp, overlap_state["handle"]))
        assert tuple(doc.GetVariable("VIEWCTR")) == pytest.approx(view_before[0])
        assert float(doc.GetVariable("VIEWSIZE")) == pytest.approx(view_before[1])
        evidence.append({"ambiguous_pick_blocked_without_edit": True, "view_restored": True})

        # Deliberately corrupt a native result after the command has executed.
        # The entire tool operation must roll back, including its new line.
        target = make_pair(1200)
        h = target["first"]["handle"]
        original_state = _read(doc, msp, h)
        count = int(msp.Count)
        send = raw._send_command_and_wait
        def corrupt(command):
            send(command)
            fresh_com(doc.HandleToObject(format(int(h), "X"))).StartPoint = _pt(1209, 0)
        with monkeypatch.context() as scoped:
            scoped.setattr(raw, "_send_command_and_wait", corrupt)
            failed = json.loads(server.cad_chamfer(server.ChamferInput.model_validate({
                "targets": [target], "distance1": 2, "dry_run": False})))
        assert not failed["ok"], failed
        assert failed["error_detail"]["details"]["rollback_status"] == "succeeded", failed
        assert int(msp.Count) == count and _same(original_state, _read(doc, msp, h))
        assert not list(tmp_path.glob("*.dwg"))
        evidence.append({"readback_failure_rollback_verified": True})
    finally:
        fresh_com(owned).Close(False)
        raw._own_document = False
        if previous is not None:
            previous.Activate()
    assert snapshot(app) == before
    print(json.dumps({"cases": evidence, "existing_documents_unchanged": True,
                      "owned_temporary_drawing_closed_without_save": True}, ensure_ascii=False))
