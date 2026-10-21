"""Opt-in native EXTEND in an owned temporary drawing; never start AutoCAD."""

import json
import math
import os

import pytest

from cad_mcp import server
from cad_mcp.drawing.autocad import AcadBackend, _pt, _pt2
from cad_mcp.drawing.native_extend import SETTINGS, _snapshot, _view_snapshot, _same_view, set_short_variable


pytestmark = pytest.mark.acad
if os.environ.get("CAD_MCP_RUN_ACAD_INTEGRATION") != "1":
    pytest.skip("requires an authorized, already running AutoCAD", allow_module_level=True)


def _documents(application):
    documents = application.Documents
    return sorted((str(documents.Item(i).Name), str(documents.Item(i).FullName), bool(documents.Item(i).Saved))
                  for i in range(int(documents.Count)))


def test_native_extend_against_existing_autocad(tmp_path, monkeypatch):
    raw = AcadBackend(tmp_path)
    cad = server._AcadLockProxy(raw)
    assert cad.health(reconnect=True)["connected"]
    application = raw.acad
    before_documents = _documents(application)
    original_values = {name: int(application.ActiveDocument.GetVariable(name)) for name in SETTINGS} if before_documents else {}
    print(json.dumps({"baseline_documents": before_documents, "baseline_variables": original_values}, ensure_ascii=False), flush=True)
    original = application.ActiveDocument if before_documents else None
    monkeypatch.setattr(server, "_get_backend", lambda: cad)
    evidence = []
    execution_unknown = False

    def extend(subject, boundary, endpoint="end", dry_run=False, **kwargs):
        nonlocal execution_unknown
        result = json.loads(server.cad_extend(server.ExtendInput(
            subject_handle=str(int(subject.Handle, 16)), boundary_handles=[str(int(boundary.Handle, 16))],
            endpoint=endpoint, dry_run=dry_run, **kwargs)))
        if result.get("error_detail", {}).get("code") == "EXECUTION_UNKNOWN":
            execution_unknown = True
        return result

    try:
        cad.new_drawing("native-extend-validation.dwg")
        docs = application.Documents
        added = [docs.Item(i) for i in range(int(docs.Count)) if str(docs.Item(i).Name) not in {d[0] for d in before_documents}]
        assert len(added) == 1 and not str(added[0].FullName) and int(added[0].ModelSpace.Count) == 0
        raw._use_doc(added[0])
        # A rotated UCS and enabled snaps must not redirect the extension or
        # leak altered settings into the user's subsequent drawing commands.
        raw._acquire_autocad_lock()
        setup_undo = False
        try:
            setup_undo = raw._begin_undo()
            assert setup_undo
            raw._send_command_and_wait('(command-s "_.UCS" "_Z" 31)\n')
            # First reproduce the formerly failing same-value write with VT_I2.
            for name in SETTINGS:
                value = int(raw.doc.GetVariable(name))
                set_short_variable(raw.doc, name, value)
            print("VT_I2 same-value writes and readback passed.", flush=True)
            set_short_variable(raw.doc, "OSMODE", 167)
            set_short_variable(raw.doc, "TRIMEXTENDMODE", 1)
            set_short_variable(raw.doc, "EDGEMODE", 1)
            set_short_variable(raw.doc, "PROJMODE", 1)
            settings = {name: int(raw.doc.GetVariable(name)) for name in SETTINGS}
            cases = []
            # Both ends of lines; a circle and an arc act as exact boundaries.
            line = raw.msp.AddLine(_pt(0, 0), _pt(10, 0))
            edge = raw.msp.AddLine(_pt(20, -5), _pt(20, 5))
            cases.append((line, edge, "end", [20, 0, 0], False))
            line = raw.msp.AddLine(_pt(0, 20), _pt(10, 20))
            circle = raw.msp.AddCircle(_pt(-10, 20), 2)
            cases.append((line, circle, "start", [-8, 20, 0], False))
            line = raw.msp.AddLine(_pt(0, 40), _pt(10, 40))
            arc_edge = raw.msp.AddArc(_pt(20, 40), 2, math.pi/2, 3*math.pi/2)
            cases.append((line, arc_edge, "end", [18, 40, 0], False))
            # An arc extends along its circle, rather than along a tangent.
            arc = raw.msp.AddArc(_pt(40, 0), 10, 0, math.pi/2)
            edge = raw.msp.AddLine(_pt(25, 0), _pt(35, 0))
            cases.append((arc, edge, "end", [30, 0, 0], False))
            arc = raw.msp.AddArc(_pt(40, 30), 10, 0, math.pi/2)
            edge = raw.msp.AddLine(_pt(40, 15), _pt(40, 25))
            cases.append((arc, edge, "start", [40, 20, 0], False))
            # Open multi-segment polyline, plus a closed polyline boundary.
            poly = raw.msp.AddLightWeightPolyline(_pt2([60, 0, 70, 0, 70, 10]))
            poly.Closed = False
            edge = raw.msp.AddLightWeightPolyline(_pt2([65, 20, 75, 20, 75, 25, 65, 25]))
            edge.Closed = True
            cases.append((poly, edge, "end", [70, 20, 0], False))
            poly = raw.msp.AddLightWeightPolyline(_pt2([60, 40, 70, 40, 70, 50]))
            poly.Closed = False
            edge = raw.msp.AddLine(_pt(50, 35), _pt(50, 45))
            cases.append((poly, edge, "start", [50, 40, 0], False))
            # Edge extension must be opt-in and preserve the actual boundary.
            line = raw.msp.AddLine(_pt(90, 0), _pt(100, 0))
            edge = raw.msp.AddLine(_pt(110, 10), _pt(110, 20))
            cases.append((line, edge, "end", [110, 0, 0], True))
        finally:
            raw._commit_undo(setup_undo)
            raw._release_autocad_lock()

        for subject, boundary, endpoint, expected, implied in cases:
            before, edge_before = _snapshot(subject), _snapshot(boundary)
            view_before = _view_snapshot(raw.doc)
            preview = extend(subject, boundary, endpoint, dry_run=True, extend_edges=implied)
            assert preview["ok"] and preview["preflight_passed"], preview
            assert not preview["applied"] and not preview["verified"], preview
            assert _snapshot(subject) == before and _snapshot(boundary) == edge_before
            assert _same_view(view_before, _view_snapshot(raw.doc))
            applied = extend(subject, boundary, endpoint, extend_edges=implied)
            assert applied["ok"] and applied["verified"] and applied["applied"], applied
            assert applied["after"][endpoint] == pytest.approx(expected, abs=1e-7)
            assert _snapshot(boundary) == edge_before
            assert _same_view(view_before, _view_snapshot(raw.doc))
            assert {name: int(raw.doc.GetVariable(name)) for name in SETTINGS} == settings
            # One finite straight edge offers no farther intersection. A circle
            # can offer another intersection, so repeated EXTEND is not generally
            # idempotent and must not be treated as a no-op for every case.
            if subject is cases[0][0]:
                noop = extend(subject, boundary, endpoint, extend_edges=implied)
                assert noop["ok"] is False, noop
                assert noop["error_detail"]["code"] == "NO_GEOMETRY_CHANGE", noop
                assert noop["error_detail"]["details"]["rollback_status"] == "not_needed", noop
                assert _snapshot(subject) == applied["after"]
            evidence.append({"type": applied["type"], "endpoint": endpoint, "after": applied["after"]})
            print(json.dumps({"passed": evidence[-1]}, ensure_ascii=False), flush=True)
        assert not (tmp_path / "native-extend-validation.dwg").exists()
    finally:
        if raw._own_document and not execution_unknown:
            cad.close_document(save=False)
        elif execution_unknown:
            print("Execution unknown: left the owned temporary drawing open for inspection.")
        if original is not None:
            raw._acquire_autocad_lock()
            try:
                original.Activate()
                if not execution_unknown:
                    for name, value in original_values.items():
                        if int(original.GetVariable(name)) != value:
                            set_short_variable(original, name, value)
                    assert {name: int(original.GetVariable(name)) for name in SETTINGS} == original_values
                print(json.dumps({"after_documents": _documents(application), "execution_unknown": execution_unknown}, ensure_ascii=False), flush=True)
            finally:
                raw._release_autocad_lock()
    assert _documents(application) == before_documents
    if original is not None:
        actual_values = {name: int(original.GetVariable(name)) for name in SETTINGS}
        # Some settings are registry-wide; restore only differences with exact types.
        for name, value in original_values.items():
            if actual_values[name] != value:
                set_short_variable(original, name, value)
        assert {name: int(original.GetVariable(name)) for name in SETTINGS} == original_values
    print(json.dumps({"native_extend": evidence, "existing_documents_unchanged": True,
                      "temporary_drawing_closed_without_save": True}, ensure_ascii=False))
