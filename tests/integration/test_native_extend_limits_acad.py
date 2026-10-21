"""Real-CAD boundary cases and expected failures, in one owned scratch drawing."""

from contextlib import contextmanager
import json
import math
import os

import pytest
import pythoncom
from win32com.client import VARIANT
from pydantic import ValidationError

from cad_mcp import server
from cad_mcp.drawing.autocad import AcadBackend, _pt, _pt2
from cad_mcp.drawing import native_extend
from cad_mcp.drawing.native_extend import SETTINGS, _snapshot, _view_snapshot, _same_view, set_short_variable


pytestmark = pytest.mark.acad
if os.environ.get("CAD_MCP_RUN_ACAD_INTEGRATION") != "1":
    pytest.skip("requires an authorized, already running AutoCAD", allow_module_level=True)


def documents(app):
    docs = app.Documents
    return sorted((str(docs.Item(i).Name), str(docs.Item(i).FullName), bool(docs.Item(i).Saved),
                   int(docs.Item(i).ModelSpace.Count)) for i in range(int(docs.Count)))


def handle(entity):
    return str(int(entity.Handle, 16))


def test_native_extend_limits_and_expected_failures(tmp_path, monkeypatch):
    raw = AcadBackend(tmp_path)
    cad = server._AcadLockProxy(raw)
    assert cad.health(reconnect=True)["connected"]
    app = raw.acad
    baseline = documents(app)
    original = app.ActiveDocument if baseline else None
    original_values = {name: int(original.GetVariable(name)) for name in SETTINGS} if original is not None else {}
    unknown = False
    passed = []
    monkeypatch.setattr(server, "_get_backend", lambda: cad)
    print(json.dumps({"baseline_documents": baseline}, ensure_ascii=False), flush=True)

    @contextmanager
    def setup():
        raw._acquire_autocad_lock()
        started = False
        try:
            raw._wait_operation_ready()
            started = raw._begin_undo()
            assert started
            yield
        finally:
            raw._commit_undo(started)
            raw._release_autocad_lock()

    def line(start=(0, 0, 0), end=(10, 0, 0), *, paper=False):
        owner = raw.doc.PaperSpace if paper else raw.msp
        return owner.AddLine(_pt(*start), _pt(*end))

    def edge(x=20, *, z=0, lower=-5, upper=5):
        return line((x, lower, z), (x, upper, z))

    def request(subject, boundaries, endpoint="end", **kwargs):
        nonlocal unknown
        view_before = _view_snapshot(raw.doc)
        result = json.loads(server.cad_extend(server.ExtendInput(
            subject_handle=handle(subject), boundary_handles=[handle(e) for e in boundaries],
            endpoint=endpoint, dry_run=False, **kwargs)))
        if result.get("error_detail", {}).get("code") == "EXECUTION_UNKNOWN":
            unknown = True
        if not unknown:
            assert _same_view(view_before, _view_snapshot(raw.doc))
        return result

    def record(name):
        passed.append(name)
        print(json.dumps({"passed": name}, ensure_ascii=False), flush=True)

    def rejected(name, subject, boundaries, expected="INVALID_ARGUMENT", **kwargs):
        before = [_snapshot(e) for e in [subject, *boundaries]]
        count = int(raw.msp.Count)
        settings = {key: int(raw.doc.GetVariable(key)) for key in SETTINGS}
        result = request(subject, boundaries, **kwargs)
        assert result["ok"] is False and result["error_detail"]["code"] == expected, result
        assert [_snapshot(e) for e in [subject, *boundaries]] == before
        assert int(raw.msp.Count) == count
        assert {key: int(raw.doc.GetVariable(key)) for key in SETTINGS} == settings
        if expected == "NO_GEOMETRY_CHANGE":
            assert result["error_detail"]["details"]["rollback_status"] == "not_needed", result
        record(name)
        return result

    def extended(name, subject, boundaries, expected, endpoint="end", **kwargs):
        edges = [_snapshot(e) for e in boundaries]
        settings = {key: int(raw.doc.GetVariable(key)) for key in SETTINGS}
        result = request(subject, boundaries, endpoint=endpoint, **kwargs)
        assert result["ok"] and result["applied"] and result["verified"], result
        assert result["after"][endpoint] == pytest.approx(expected, abs=1e-7)
        assert [_snapshot(e) for e in boundaries] == edges
        assert {key: int(raw.doc.GetVariable(key)) for key in SETTINGS} == settings
        record(name)

    try:
        cad.new_drawing("native-extend-boundary-validation.dwg")
        docs = app.Documents
        added = [docs.Item(i) for i in range(int(docs.Count)) if str(docs.Item(i).Name) not in {d[0] for d in baseline}]
        assert len(added) == 1 and not str(added[0].FullName) and int(added[0].ModelSpace.Count) == 0
        raw._use_doc(added[0])
        with setup():
            subject, boundary = line(), edge()

        # Schema failures must never dispatch a CAD write, including >100 edges.
        before, count = _snapshot(subject), int(raw.msp.Count)
        for name, bad in [
            ("empty boundaries", {"boundary_handles": []}),
            ("duplicate canonical handles", {"boundary_handles": [handle(boundary), "0"+handle(boundary)]}),
            ("subject used as its own boundary", {"boundary_handles": [handle(subject)]}),
            ("101 boundaries", {"boundary_handles": [str(i+1) for i in range(101)]}),
            ("nondecimal handle", {"subject_handle": "invalid"}),
            ("invalid endpoint", {"endpoint": "both"}),
        ]:
            with pytest.raises(ValidationError):
                server.ExtendInput(**{"subject_handle": handle(subject), "boundary_handles": [handle(boundary)],
                                       "endpoint": "end", "dry_run": False, **bad})
            assert _snapshot(subject) == before and int(raw.msp.Count) == count
            record(name)

        result = json.loads(server.cad_extend(server.ExtendInput(subject_handle="99999999999999999999",
                              boundary_handles=[handle(boundary)], endpoint="end", dry_run=False)))
        assert result["ok"] is False and result["error_detail"]["code"] == "NOT_FOUND", result
        assert _snapshot(subject) == before
        record("nonexistent handle")

        with setup():
            locked_layer = raw.doc.Layers.Add("NATIVE-EXTEND-LOCKED")
            subject.Layer = str(locked_layer.Name)
            locked_layer.Lock = True
        rejected("locked target layer", subject, [boundary])
        with setup():
            locked_layer.Lock = False
            subject.Layer = "0"

        with setup():
            parallel = line((0, 5, 0), (30, 5, 0))
            behind = edge(-10)
            short_edge = edge(20, lower=10, upper=20)
            elevated = edge(20, z=5)
            circle = raw.msp.AddCircle(_pt(0, 0), 3)
            closed = raw.msp.AddLightWeightPolyline(_pt2([0, 0, 10, 0, 10, 10, 0, 10]))
            closed.Closed = True
            paper_line = line(paper=True)
            tilted = raw.msp.AddArc(_pt(0, 0), 10, 0, math.pi/2)
            tilted.Normal = _pt(0, 1, 0)
        rejected("parallel boundary without intersection", subject, [parallel], "NO_GEOMETRY_CHANGE")
        rejected("boundary behind the selected end", subject, [behind], "NO_GEOMETRY_CHANGE")
        rejected("finite boundary does not reach the path", subject, [short_edge], "NO_GEOMETRY_CHANGE")
        extended("explicit implied boundary extension", subject, [short_edge], [20, 0, 0], extend_edges=True)
        rejected("different boundary elevation", subject, [elevated])
        rejected("closed circle target", circle, [boundary])
        rejected("closed polyline target", closed, [boundary])
        # Paper-space and tilted objects cannot use the planar snapshot helper.
        for name, item in [("paper-space target", paper_line), ("tilted arc target", tilted)]:
            count = int(raw.msp.Count)
            result = request(item, [boundary])
            assert not result["ok"] and result["error_detail"]["code"] == "INVALID_ARGUMENT", result
            assert int(raw.msp.Count) == count
            record(name)

        with setup():
            target = line()
            tangent = raw.msp.AddCircle(_pt(20, 5), 5)
        extended("tangent circle boundary", target, [tangent], [20, 0, 0])

        with setup():
            target = line()
            boundaries = [edge(20+i) for i in range(100)]
        extended("100 boundaries in reverse order choose nearest", target, list(reversed(boundaries)), [20, 0, 0])

        with setup():
            target = line((1000000, 1000000, 7), (1000010, 1000000, 7))
            boundary = line((1000010.001, 999995, 7), (1000010.001, 1000005, 7))
        extended("large coordinates small gap and nonzero elevation", target, [boundary], [1000010.001, 1000000, 7])

        for endpoint, expected in [("start", [-10, 10, 0]), ("end", [0, 20, 0])]:
            with setup():
                target = raw.msp.AddLightWeightPolyline(_pt2([0, 0, 10, 10]))
                target.Closed = False
                target.SetBulge(0, math.tan(math.pi/8))
                target.ConstantWidth = 0.5
                boundary = edge(-10, lower=5, upper=15) if endpoint == "start" else line((-5, 20, 0), (5, 20, 0))
            extended("bulged polyline " + endpoint, target, [boundary], expected, endpoint=endpoint)

        with setup():
            # Preserve this sub-millimetre segment; _pt2 rounds to 0.001.
            target = raw.msp.AddLightWeightPolyline(VARIANT(
                pythoncom.VT_ARRAY | pythoncom.VT_R8, [0, 0, 0.0001, 0, 1000, 1000]))
            assert list(target.Coordinates) == [0, 0, 0.0001, 0, 1000, 1000]
            target.Closed = False
            boundary = edge(-10)
        extended("very short first segment", target, [boundary], [-10, 0, 0], endpoint="start")

        with setup():
            target = raw.msp.AddLightWeightPolyline(_pt2([v for i in range(1000) for v in (i, 0)]))
            target.Closed = False
            boundary = edge(1010)
        extended("1000 polyline vertices", target, [boundary], [1010, 0, 0])
        with setup():
            too_many = raw.msp.AddLightWeightPolyline(_pt2([v for i in range(1001) for v in (i, 0)]))
            too_many.Closed = False
        old_coords = list(too_many.Coordinates)
        result = request(too_many, [boundary])
        assert not result["ok"] and result["error_detail"]["code"] == "INVALID_ARGUMENT", result
        assert list(too_many.Coordinates) == old_coords
        record("1001 polyline vertices rejected without change")

        # Force a verifier failure after a genuine native write to test real U.
        with setup():
            target, boundary = line(), edge()
        old = _snapshot(target)
        real_verify = native_extend._verify
        def fail_after_verification(*args, **kwargs):
            real_verify(*args, **kwargs)
            raise RuntimeError("injected failure after verified native extension")
        with monkeypatch.context() as scoped:
            scoped.setattr(native_extend, "_verify", fail_after_verification)
            result = request(target, [boundary])
        assert not result["ok"], result
        assert result["error_detail"]["details"]["rollback_status"] == "succeeded", result
        assert _snapshot(target) == old
        record("real native extension rollback restores original geometry")
    finally:
        if raw._own_document and not unknown:
            cad.close_document(save=False)
        if original is not None:
            raw._acquire_autocad_lock()
            try:
                original.Activate()
                if not unknown:
                    for name, value in original_values.items():
                        if int(original.GetVariable(name)) != value:
                            set_short_variable(original, name, value)
            finally:
                raw._release_autocad_lock()
        after = documents(app)
        print(json.dumps({"passed_cases": passed, "after_documents": after, "execution_unknown": unknown}, ensure_ascii=False), flush=True)
        if not unknown:
            assert after == baseline
            if original is not None:
                assert {name: int(original.GetVariable(name)) for name in SETTINGS} == original_values
