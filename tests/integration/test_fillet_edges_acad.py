"""Opt-in edge cases against an existing AutoCAD, in one owned scratch drawing."""

import json
import os
from pathlib import Path
import re

import pytest
from pydantic import ValidationError
import win32com.client

from cad_mcp import server
from cad_mcp.drawing.autocad import AcadBackend
from cad_mcp.drawing.fillet import KINDS, _model_entities, _snapshot


pytestmark = pytest.mark.acad
if os.environ.get("CAD_MCP_RUN_ACAD_INTEGRATION") != "1":
    pytest.skip("requires authorization and an already running AutoCAD", allow_module_level=True)


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    app = win32com.client.GetActiveObject("AutoCAD.Application")
    active = app.ActiveDocument if app.Documents.Count else None
    originals = [app.Documents.Item(i) for i in range(app.Documents.Count)]

    def document_states():
        return [{"name": d.Name, "path": d.FullName, "saved": bool(d.Saved),
                 "entities": int(d.ModelSpace.Count)} for d in originals]

    before_documents = document_states()
    directory = tmp_path_factory.mktemp("fillet-edges")
    raw = AcadBackend(directory)
    cad = server._AcadLockProxy(raw)
    patch = pytest.MonkeyPatch()
    patch.setattr(server, "_backend", cad)
    evidence = {"autocad_version": app.Version, "before_documents": before_documents, "cases": []}
    report = Path(__file__).resolve().parents[2] / ".cache" / "fillet-live-edges.json"

    class Harness:
        next_x = 0

        def pair(self, *, gap=0, z=0, parallel=False):
            x = self.next_x
            self.next_x += 100
            a = cad.draw_line(x+40, 0, x+gap, 0)["handle"]
            b = (cad.draw_line(x, 10, x+40, 10)["handle"] if parallel else
                 cad.draw_line(x, gap, x, 40)["handle"])
            if z:
                for handle in (a, b):
                    p = cad.get_entity_properties(handle)["properties"]
                    cad.edit_entity_properties(handle, {"start": [*p["start"][:2], z],
                                                       "end": [*p["end"][:2], z]}, dry_run=False)
            return {"handles": [a, b], "pick_points": [[x+30, 0, z],
                    [x+30, 10, z] if parallel else [x, 30, z]], "radius": 5}

        def state(self):
            states = {}
            for handle, entity in _model_entities(raw).items():
                kind = str(entity.ObjectName)
                if kind == "AcDbLine":
                    # Test observations must accept tilted lines so the tool,
                    # rather than this baseline reader, can reject them.
                    states[handle] = {"type": "LINE", "layer": str(entity.Layer),
                                      "start": list(entity.StartPoint), "end": list(entity.EndPoint),
                                      "thickness": float(entity.Thickness)}
                else:
                    states[handle] = (_snapshot(entity) if kind in KINDS
                                      else raw._entity_state_hash(entity))
            return states

        def settings(self):
            return {key: raw.doc.GetVariable(key) for key in
                    ("FILLETRAD", "TRIMMODE", "OSMODE", "CMDECHO", "USERS5", "TILEMODE")}

        def view(self):
            return {key: raw.doc.GetVariable(key) for key in
                    ("VIEWCTR", "VIEWSIZE", "VIEWDIR", "TARGET", "VIEWTWIST")}

        def assert_view(self, expected):
            actual = self.view()
            for key, value in expected.items():
                assert actual[key] == pytest.approx(value, rel=1e-10, abs=1e-7), (key, actual, expected)

        def call(self, params):
            return json.loads(server.cad_fillet(server.FilletInput(**params)))

        def record(self, name, **data):
            evidence["cases"].append({"case": name, "passed": True, **data})
            report.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(evidence["cases"][-1], ensure_ascii=False), flush=True)

        def observe(self, name, **data):
            item = {"case": name, **data}
            evidence.setdefault("observations", []).append(item)
            report.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(item, ensure_ascii=False), flush=True)

        def rejected(self, name, params, *, expected_rollback=None):
            state, settings = self.state(), self.settings()
            view = self.view()
            response = self.call({**params, "dry_run": False})
            assert not response["ok"], response
            assert self.state() == state, response
            assert self.settings() == settings, response
            self.assert_view(view)
            rollback = response["error_detail"].get("details", {}).get("rollback_status")
            assert rollback in {None, "not_needed", "succeeded"}, response
            if expected_rollback is not None:
                assert rollback == expected_rollback, response
            self.record(name, error=response["error"], rollback_status=rollback,
                        geometry_unchanged=True, settings_restored=True)
            return response

        def applied(self, name, params):
            settings = self.settings()
            view = self.view()
            response = self.call({**params, "dry_run": False})
            assert response["ok"] and response["applied"] and response["verified"], response
            assert self.settings() == settings, response
            self.assert_view(view)
            self.record(name, rounded_arc_count=response["rounded_arc_count"],
                        settings_restored=True, view_restored=True, after=response["after"], created=response["created"])
            return response

        def variable(self, name, value):
            raw._acquire_autocad_lock()
            started = False
            try:
                raw._wait_operation_ready()
                started = raw._begin_undo()
                assert started
                raw.doc.SetVariable(name, value)
                raw._commit_undo(started)
            finally:
                raw._release_autocad_lock()

        def command(self, text):
            raw._acquire_autocad_lock()
            try:
                raw._wait_operation_ready()
                started = raw._begin_undo()
                assert started
                try:
                    raw._send_command_and_wait(text)
                finally:
                    raw._commit_undo(started)
            finally:
                raw._release_autocad_lock()

    harness = Harness()
    harness.raw, harness.cad = raw, cad
    try:
        assert cad.health(reconnect=True)["connected"]
        cad.new_drawing("native-fillet-edge-validation.dwg")
        evidence["scratch_name"] = raw.doc.Name
        initial_settings = harness.settings()
        yield harness
        assert harness.settings() == initial_settings
        assert not (directory / "native-fillet-edge-validation.dwg").exists()
    finally:
        try:
            if raw._own_document:
                cad.close_document(save=False)
            if active is not None:
                raw._acquire_autocad_lock()
                try:
                    active.Activate()
                finally:
                    raw._release_autocad_lock()
            evidence["after_documents"] = document_states()
            evidence["original_documents_unchanged"] = evidence["after_documents"] == before_documents
            evidence["scratch_closed_without_save"] = not raw._own_document
            assert evidence["original_documents_unchanged"], evidence
        finally:
            patch.undo()
            report.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True])
def test_invalid_radius_never_writes(live, value):
    params = live.pair()
    state, settings = live.state(), live.settings()
    with pytest.raises(ValidationError):
        live.call({**params, "radius": value, "dry_run": False})
    assert live.state() == state and live.settings() == settings
    live.record("invalid_radius_" + str(value), geometry_unchanged=True)


def test_missing_and_repeated_line_handles(live):
    params = live.pair()
    live.rejected("missing_handle", {**params, "handles": [params["handles"][0], "9999999999999999"]})
    live.rejected("repeated_line_handle", {**params, "handles": [params["handles"][0]]*2})


def test_locked_source_layer(live):
    params = live.pair()
    live.cad.create_layer("FILLET_LOCK", color=2)
    for handle in params["handles"]:
        live.cad.edit_entity_properties(handle, {"layer": "FILLET_LOCK"}, dry_run=False)
    live.cad.set_layer_props("FILLET_LOCK", locked=True)
    try:
        live.rejected("locked_source_layer", params)
    finally:
        live.cad.set_layer_props("FILLET_LOCK", locked=False)


def test_non_coplanar_and_tilted_lines(live):
    params = live.pair()
    b = params["handles"][1]
    props = live.cad.get_entity_properties(b)["properties"]
    live.cad.edit_entity_properties(b, {"start": [*props["start"][:2], 2],
                                      "end": [*props["end"][:2], 2]}, dry_run=False)
    live.rejected("different_elevations", params)
    live.cad.edit_entity_properties(b, {"end": [*props["end"][:2], 3]}, dry_run=False)
    live.rejected("tilted_line", params)


def test_wrong_pick_elevation_and_polyline_mode(live):
    params = live.pair()
    picks = [params["pick_points"][0], [*params["pick_points"][1][:2], 1]]
    live.rejected("wrong_pick_elevation", {**params, "pick_points": picks})
    live.rejected("polyline_mode_with_line", {"mode": "polyline", "handles": params["handles"][:1], "radius": 5})


def test_paper_space_refused(live):
    params = live.pair()
    live.variable("TILEMODE", 0)
    try:
        live.rejected("paper_space", params)
    finally:
        live.variable("TILEMODE", 1)


def test_too_large_radius_for_short_polyline(live):
    h = live.cad.draw_polyline([(0, 100), (5, 100), (5, 105), (0, 105)], close=True)["handle"]
    live.rejected("oversized_radius_short_polyline", {"mode": "polyline", "handles": [h], "radius": 100})


def test_parallel_lines_do_not_silently_override_radius(live):
    params = live.pair(parallel=True)
    # AutoCAD may use half the line spacing for parallel lines; the tool must
    # reject and undo a native result whose actual radius differs from 2 mm.
    live.rejected("parallel_lines_radius_mismatch", {**params, "radius": 2})


def test_zero_radius_extends_to_sharp_corner(live):
    result = live.applied("zero_radius_extends_lines", {**live.pair(gap=10), "radius": 0})
    assert result["rounded_arc_count"] == 0


def test_small_radius_and_preserved_elevation(live):
    live.applied("small_radius_0.001", {**live.pair(), "radius": 0.001})
    result = live.applied("preserved_elevation_7", live.pair(z=7))
    assert all(s["elevation"] == 7 for s in [*result["after"].values(), *result["created"]])


@pytest.mark.parametrize("x, rotated", [(100, False), (1000000, False), (2000000, True)])
def test_same_polyline_two_segments(live, x, rotated):
    h = live.cad.draw_polyline([(x, 100), (x+40, 100), (x+40, 140)])["handle"]
    if rotated:
        live.command('_.UCS\n_Z\n30\n')
    try:
        live.applied(f"same_polyline_two_segments_{x}_rotated_{rotated}", {"handles": [h, h],
                     "pick_points": [[x+10, 100, 0], [x+40, 130, 0]], "radius": 5})
    finally:
        if rotated:
            live.command('_.UCS\n_World\n')


def test_same_polyline_wrong_entity_under_pick(live):
    h = live.cad.draw_polyline([(700, 100), (740, 100), (740, 140)])["handle"]
    live.cad.draw_line(700, 120, 730, 120)
    response = live.rejected("same_polyline_pick_on_different_entity", {"handles": [h, h],
                            "pick_points": [[710, 100, 0], [720, 120, 0]], "radius": 5})
    assert "未命中指定图元" in response["error"]


def test_partial_fillet_skips_short_segments(live):
    h = live.cad.draw_polyline([(200, 100), (300, 100), (300, 200),
                               (299, 200), (299, 101), (200, 101)], close=True)["handle"]
    result = live.applied("partial_polyline_short_edges", {"mode": "polyline", "handles": [h], "radius": 10})
    assert 0 < result["rounded_arc_count"] < 6


def test_circle_line_and_arc_line(live):
    for name, x, circle in (("circle_line", 500, True), ("arc_line", 600, False)):
        h = (live.cad.draw_circle(x, 20, 10)["handle"] if circle else
             live.cad.draw_arc(x, 20, 10, 180, 360)["handle"])
        line = live.cad.draw_line(x-30, 0, x+40, 0)["handle"]
        live.applied(name, {"handles": [h, line], "pick_points": [[x+9, 16, 0], [x+30, 0, 0]], "radius": 10})


@pytest.mark.parametrize("selection", ["line_pair", "same_polyline", "curve_pair"])
def test_native_write_followed_by_readback_exception_rolls_back(live, monkeypatch, selection):
    if selection == "same_polyline":
        h = live.cad.draw_polyline([(800, 100), (840, 100), (840, 140)])["handle"]
        params = {"handles": [h, h], "pick_points": [[810, 100, 0], [840, 130, 0]], "radius": 5}
    elif selection == "curve_pair":
        circle = live.cad.draw_circle(900, 20, 10)["handle"]
        line = live.cad.draw_line(870, 0, 940, 0)["handle"]
        params = {"handles": [circle, line], "pick_points": [[909, 16, 0], [930, 0, 0]], "radius": 10}
    else:
        params = live.pair()
    before = live.state()
    send = live.raw._send_command_and_wait
    def fail_after_native(command, *args, **kwargs):
        send(command, *args, **kwargs)
        marker = re.search(r'"(cadmcp-fillet-[a-f0-9]+):done"', command).group(1)
        status = live.raw.doc.GetVariable("USERS5")
        after = live.state()
        selected = set(params["handles"])
        live.observe("pre_injection_" + selection, native_status=status,
                     completed=status == marker + ":done", geometry_changed=after != before,
                     selected_before={h: before[h] for h in selected},
                     selected_after={h: after[h] for h in selected if h in after},
                     created_handles=sorted(set(after) - set(before), key=int))
        assert status == marker + ":done", status
        assert after != before, "native FILLET did not write geometry; fault was not injected"
        raise RuntimeError("injected failure after native FILLET for live undo verification")
    with monkeypatch.context() as patch:
        patch.setattr(live.raw, "_send_command_and_wait", fail_after_native)
        response = live.rejected("injected_post_write_failure_" + selection, params,
                                 expected_rollback="succeeded")
    assert response["error_detail"]["details"]["rollback_status"] == "succeeded"
