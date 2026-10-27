"""Native chamfer contract, numerical previews, and transactional failures."""

import copy
import json
import math
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cad_mcp import server
from cad_mcp.drawing.autocad import AcadBackend
from cad_mcp.drawing.chamfer import ChamferRequest, _command, _pair_plan, _polyline_plan, _same


def payload(**patch):
    return {"targets": [{"mode": "pair", "first": {"handle": "10", "point": [15, 0, 0]},
                         "second": {"handle": "11", "point": [0, 15, 0]}}],
            "distance1": 2, **patch}


def line(handle, start, end, kind="AcDbLine"):
    return {"handle": handle, "kind": kind, "layer": "PIPE", "points": [tuple(start), tuple(end)]}


def square():
    return {"handle": "10", "kind": "AcDbPolyline", "layer": "PIPE", "closed": True,
            "points": [(0, 0, 0), (20, 0, 0), (20, 20, 0), (0, 20, 0)], "bulges": [0.] * 4}


def pair_plan(data=None, states=None):
    request = ChamferRequest.model_validate(data or payload())
    states = states or {"10": line("10", (0, 0, 0), (20, 0, 0)),
                        "11": line("11", (0, 0, 0), (0, 20, 0))}
    return _pair_plan(request.targets[0], states, request, "CURRENT")


def test_schema_has_native_modes_and_strict_nested_fields():
    params = server.ChamferInput.model_validate(payload())
    assert params.dry_run and params.trim and params.distance2 is None
    tool = next(t for t in server.mcp._tool_manager.list_tools() if t.name == "cad_chamfer")
    assert tool.annotations.destructiveHint and not tool.annotations.readOnlyHint
    for definition in tool.parameters["$defs"].values():
        if definition.get("type") == "object":
            assert definition["additionalProperties"] is False
    bad = payload()
    bad["targets"][0]["first"]["command"] = "ERASE"
    with pytest.raises(ValidationError):
        server.ChamferInput.model_validate(bad)


@pytest.mark.parametrize("patch", [
    {"distance1": -1}, {"distance1": float("nan")}, {"distance2": float("inf")},
    {"angle": 45}, {"method": "angle"}, {"method": "angle", "angle": 0},
    {"method": "angle", "angle": 45, "distance2": 3},
    {"method": "angle", "angle": 180}, {"trim": "false"}, {"dry_run": 0},
    {"targets": []}, {"targets": payload()["targets"] * 21},
])
def test_bad_parameters_fail_before_accessing_cad(patch):
    with pytest.raises(ValidationError):
        ChamferRequest.model_validate(payload(**patch))


@pytest.mark.parametrize("handle", ["0", "-1", "A", '1\") (command "ERASE")', "１"])
def test_handles_cannot_be_code_or_non_decimal(handle):
    data = payload()
    data["targets"][0]["first"]["handle"] = handle
    with pytest.raises(ValidationError):
        ChamferRequest.model_validate(data)


def test_distance_preview_uses_separate_distances_and_keeps_far_endpoints():
    plan = pair_plan(payload(distance2=3))
    assert plan["after"]["10"]["points"] == [(2, 0, 0), (20, 0, 0)]
    assert plan["after"]["11"]["points"] == [(0, 3, 0), (0, 20, 0)]
    assert plan["lines"] == [{"points": [(2, 0, 0), (0, 3, 0)], "layer": "PIPE"}]


def test_angle_preview_obeys_first_side_length_and_angle():
    plan = pair_plan(payload(method="angle", distance1=6, angle=30))
    a, b = plan["lines"][0]["points"]
    assert a == (6, 0, 0)
    assert b == pytest.approx((0, 2 * math.sqrt(3), 0))
    assert math.degrees(math.atan2(b[1], a[0])) == pytest.approx(30)


def test_no_trim_and_zero_distance_corner():
    plan = pair_plan(payload(trim=False, distance2=3))
    assert plan["after"] == plan["before"] and len(plan["lines"]) == 1
    sharp = pair_plan(payload(distance1=0))
    assert not sharp["lines"]
    assert all(_same(sharp["before"][h], s) for h, s in sharp["after"].items())
    angle_sharp = pair_plan(payload(method="angle", distance1=0, angle=0))
    assert not angle_sharp["lines"]


def test_ray_and_xline_have_bounded_and_unbounded_retained_sides():
    states = {"10": line("10", (0, 0, 0), (1, 0, 0), "AcDbXline"),
              "11": line("11", (0, 0, 0), (0, 1, 0), "AcDbRay")}
    plan = pair_plan(states=states)
    assert plan["after"]["10"]["kind"] == "AcDbRay"
    assert plan["after"]["10"]["points"] == [(2, 0, 0), (3, 0, 0)]
    assert plan["after"]["11"]["points"] == [(0, 2, 0), (0, 3, 0)]


@pytest.mark.parametrize("case", ["parallel", "elevation", "intersection", "off_object", "too_large", "wrong_angle"])
def test_ambiguous_or_impossible_geometry_is_rejected(case):
    data = payload()
    states = {"10": line("10", (0, 0, 0), (20, 0, 0)),
              "11": line("11", (0, 0, 0), (0, 20, 0))}
    if case == "parallel":
        states["11"] = line("11", (0, 5, 0), (20, 5, 0))
        data["targets"][0]["second"]["point"] = [15, 5, 0]
    elif case == "elevation":
        states["11"] = line("11", (0, 0, 1), (0, 20, 1))
        data["targets"][0]["second"]["point"] = [0, 15, 1]
    elif case == "intersection":
        data["targets"][0]["first"]["point"] = [0, 0, 0]
    elif case == "off_object":
        data["targets"][0]["first"]["point"] = [15, 1, 0]
    elif case == "too_large":
        data["distance1"] = 20
    else:
        data.update(method="angle", angle=100)
    with pytest.raises(ValueError):
        pair_plan(data, states)


def test_polyline_all_corners_preview_is_complete_and_rejects_short_edges():
    req = ChamferRequest(targets=[{"mode": "polyline", "handle": "10"}], distance1=2, distance2=3)
    plan = _polyline_plan(req.targets[0], {"10": square()}, req)
    assert len(plan["corners"]) == 4
    assert len(plan["after"]["10"]["points"]) == 8
    assert (0, 2, 0) in plan["after"]["10"]["points"]
    assert (3, 0, 0) in plan["after"]["10"]["points"]
    req.distance1 = 19
    with pytest.raises(ValueError, match="边长不足"):
        _polyline_plan(req.targets[0], {"10": square()}, req)


def test_polyline_arc_adjacent_vertices_are_preserved_and_no_trim_makes_lines():
    req = ChamferRequest(targets=[{"mode": "polyline", "handle": "10"}], distance1=2, trim=False)
    state = square()
    state["bulges"][1] = .5
    plan = _polyline_plan(req.targets[0], {"10": state}, req)
    assert len(plan["corners"]) == len(plan["lines"]) == 2
    assert plan["before"] == plan["after"]


def test_adjacent_polyline_picks_select_only_one_corner():
    data = payload()
    data["targets"][0]["second"] = {"handle": "10", "point": [20, 15, 0]}
    plan = pair_plan(data, {"10": square()})
    assert len(plan["after"]["10"]["points"]) == 5
    assert not plan["lines"]
    assert (18, 0, 0) in plan["after"]["10"]["points"]
    assert (20, 2, 0) in plan["after"]["10"]["points"]


def test_command_uses_exact_entities_wcs_to_ucs_and_no_interactive_options():
    command = _command(pair_plan())
    assert '(handent "A")' in command and '(handent "B")' in command
    assert '(trans (list 15 0 0) 0 1)' in command
    assert 'command-s "_.CHAMFER"' in command
    assert "_Distance" not in command and "_Angle" not in command and "PAUSE" not in command


@pytest.fixture
def native_mock(tmp_path):
    backend = AcadBackend(tmp_path)
    objects = {
        "A": SimpleNamespace(Handle="A", OwnerID=99, EntityName="AcDbLine", Layer="PIPE", StartPoint=(0, 0, 0), EndPoint=(20, 0, 0)),
        "B": SimpleNamespace(Handle="B", OwnerID=99, EntityName="AcDbLine", Layer="PIPE", StartPoint=(0, 0, 0), EndPoint=(0, 20, 0)),
    }
    original = copy.deepcopy(objects)
    events = []
    class ModelSpace:
        ObjectID = 99
        @property
        def Count(self):
            return len(objects)
        def Item(self, i):
            return list(objects.values())[i]
    variables = dict(CHAMFERA=7., CHAMFERB=8., CHAMFERC=9., CHAMFERD=.5, CHAMMODE=1,
                     TRIMMODE=0, OSMODE=167, UNDOCTL=1, CMDACTIVE=0, UCSXDIR=(1, 0, 0), UCSYDIR=(0, 1, 0))
    original_variables = variables.copy()
    def setvar(k, v):
        events.append(("set", k, v)); variables[k] = v
    backend.doc = SimpleNamespace(Name="TEST.dwg", FullName="", HandleToObject=lambda h: objects[h], GetVariable=variables.__getitem__, SetVariable=setvar,
                                  ActiveLayer=SimpleNamespace(Name="PIPE"),
                                  Layers=SimpleNamespace(Item=lambda name: SimpleNamespace(Lock=False, Freeze=False)))
    backend.acad = SimpleNamespace(ActiveDocument=backend.doc)
    backend.msp = ModelSpace()
    backend._check_doc = lambda: None
    backend._begin_undo = lambda: events.append("begin") or True
    backend._commit_undo = lambda started: events.append(("commit", started))
    def rollback(started):
        events.append(("rollback", started))
        objects.clear(); objects.update(copy.deepcopy(original))
        variables.update(original_variables)
        return True
    backend._rollback_undo = rollback
    def command(text):
        events.append(("command", text))
        objects["A"].StartPoint = (2, 0, 0)
        objects["B"].StartPoint = (0, 3, 0)
        objects["C"] = SimpleNamespace(Handle="C", OwnerID=99, EntityName="AcDbLine", Layer="PIPE", StartPoint=(2, 0, 0), EndPoint=(0, 3, 0))
    backend._send_command_and_wait = command
    return backend, objects, events, variables, original_variables


def test_preview_is_zero_write_and_server_exposes_plan(native_mock, monkeypatch):
    backend, objects, events, variables, original = native_mock
    monkeypatch.setattr(server, "_backend", backend)
    result = json.loads(server.cad_chamfer(server.ChamferInput.model_validate(payload(distance2=3))))
    assert result["ok"] and result["dry_run"] and result["would_write"]
    assert not result["applied"] and not result["verified"]
    assert len(objects) == 2 and variables == original and events == []


def test_verified_native_write_restores_settings(native_mock):
    backend, objects, events, variables, original = native_mock
    result = backend.chamfer(payload(distance2=3, dry_run=False))
    assert result["applied"] and result["verified"]
    assert result["entities"][0]["handle"] == "12"
    assert events[0] == "begin" and events[-1] == ("commit", True)
    assert variables == original and len(objects) == 3


def test_native_chamfer_joins_outer_transaction_without_nested_undo(native_mock):
    from cad_mcp.core.semantics import TRANSACTION_BACKEND
    backend, objects, events, variables, original = native_mock
    backend._undo_mark_open = True
    variables["UNDOCTL"] |= 8
    original = variables.copy()
    token = TRANSACTION_BACKEND.set(backend)
    try:
        result = backend.chamfer(payload(distance2=3, dry_run=False))
        assert result["verified"] and variables == original and len(objects) == 3
        assert "begin" not in events
        assert not any(isinstance(event, tuple) and event[0] in {"commit", "rollback"} for event in events)
        assert backend._native_rollback_probe()
        variables["OSMODE"] += 1
        assert not backend._native_rollback_probe()
    finally:
        TRANSACTION_BACKEND.reset(token)


def test_command_without_geometry_is_failure_and_verified_rollback(native_mock):
    backend, objects, events, variables, original = native_mock
    backend._send_command_and_wait = lambda cmd: events.append(("no-op", cmd))
    with pytest.raises(RuntimeError, match="不一致") as caught:
        backend.chamfer(payload(distance2=3, dry_run=False))
    assert caught.value.rollback_status == "succeeded"
    assert len(objects) == 2 and variables == original
    assert ("rollback", True) in events
    assert not any(isinstance(e, tuple) and e[0] == "commit" for e in events)


def test_invalid_batch_and_locked_layer_do_not_open_undo_group(native_mock):
    backend, objects, events, *_ = native_mock
    data = payload(dry_run=False)
    data["targets"] += [{"mode": "polyline", "handle": "10"}]
    with pytest.raises(ValueError):
        backend.chamfer(data)
    assert not events
    backend.doc.Layers.Item = lambda name: SimpleNamespace(Lock=True, Freeze=False)
    with pytest.raises(ValueError, match="锁定"):
        backend.chamfer(payload(dry_run=False))
    assert not events and len(objects) == 2


def test_noop_does_not_undo_previous_operation(native_mock):
    backend, objects, events, *_ = native_mock
    result = backend.chamfer(payload(distance1=0, dry_run=False))
    assert result["verified"] and not result["applied"] and not result["would_write"]
    assert not events


def test_unverified_rollback_is_not_reported_as_restored(native_mock):
    backend, objects, events, *_ = native_mock
    backend._send_command_and_wait = lambda cmd: (_ for _ in ()).throw(TimeoutError("native deadline"))
    backend._rollback_undo = lambda started: False
    with pytest.raises(TimeoutError) as caught:
        backend.chamfer(payload(dry_run=False))
    assert caught.value.rollback_status == "failed_or_unverified"


def test_later_batch_failure_rolls_back_earlier_native_result(native_mock):
    backend, objects, events, variables, original_variables = native_mock
    objects["14"] = SimpleNamespace(Handle="14", OwnerID=99, EntityName="AcDbLine", Layer="PIPE", StartPoint=(100, 0, 0), EndPoint=(120, 0, 0))
    objects["15"] = SimpleNamespace(Handle="15", OwnerID=99, EntityName="AcDbLine", Layer="PIPE", StartPoint=(100, 0, 0), EndPoint=(100, 20, 0))
    initial = copy.deepcopy(objects)
    send = backend._send_command_and_wait
    count = 0
    def command(text):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("second group failed")
        send(text)
    def rollback(started):
        objects.clear(); objects.update(copy.deepcopy(initial))
        events.append(("rollback", started))
        return True
    backend._send_command_and_wait = command
    backend._rollback_undo = rollback
    data = payload(distance2=3, dry_run=False)
    data["targets"].append({"mode": "pair", "first": {"handle": "20", "point": [115, 0, 0]},
                            "second": {"handle": "21", "point": [100, 15, 0]}})
    with pytest.raises(RuntimeError, match="second group") as caught:
        backend.chamfer(data)
    assert count == 2 and objects == initial and variables == original_variables
    assert caught.value.rollback_status == "succeeded"


def test_source_read_deadline_does_not_prevent_rollback(native_mock, monkeypatch):
    from cad_mcp.drawing import autocad
    backend, objects, events, *_ = native_mock
    send = backend._send_command_and_wait
    def command(text):
        send(text)
        monkeypatch.setattr(autocad, "_COM_SCAN_TIMEOUT", -1)
    backend._send_command_and_wait = command
    real_rollback = backend._rollback_undo
    def rollback(started):
        # A timed-out read must have left its private deadline scope before
        # emergency COM restoration begins.
        assert autocad._COM_SCAN_STATE.get() is None
        monkeypatch.setattr(autocad, "_COM_SCAN_TIMEOUT", 30)
        return real_rollback(started)
    backend._rollback_undo = rollback
    with pytest.raises(TimeoutError) as caught:
        backend.chamfer(payload(distance2=3, dry_run=False))
    assert caught.value.rollback_status == "succeeded" and len(objects) == 2


def test_setting_restore_attempts_all_variables_and_then_rolls_back(native_mock):
    backend, objects, events, variables, original = native_mock
    setter = backend.doc.SetVariable
    failed = False
    def setvar(name, value):
        nonlocal failed
        if name == "CHAMFERA" and value == original[name] and not failed:
            failed = True
            raise RuntimeError("one setting restore failed")
        setter(name, value)
    backend.doc.SetVariable = setvar
    with pytest.raises(RuntimeError, match="系统变量") as caught:
        backend.chamfer(payload(distance2=3, dry_run=False))
    assert failed and caught.value.rollback_status == "succeeded"
    assert variables == original and len(objects) == 2


def test_polyline_command_checks_apertures_before_native_point_picks():
    data = payload()
    data["targets"][0]["second"] = {"handle": "10", "point": [20, 15, 0]}
    command = _command(pair_plan(data, {"10": square()}))
    assert '(ssget "_C"' in command
    assert '(= 1 (sslength s1))' in command
    assert '(eq (ssname s1 0) (handent "A"))' in command
    assert '(initcommandversion 2)' in command
    assert '(command-s "_.CHAMFER" "_non" p1 "_non" p2)' in command


def test_other_active_document_and_existing_undo_group_fail_before_write(native_mock):
    backend, objects, events, variables, _ = native_mock
    backend.acad.ActiveDocument = SimpleNamespace(Name="OTHER.dwg", FullName="")
    with pytest.raises(ValueError, match="活动图纸"):
        backend.chamfer(payload(dry_run=False))
    assert not events
    backend.acad.ActiveDocument = backend.doc
    variables["UNDOCTL"] = 9
    with pytest.raises(ValueError, match="撤销"):
        backend.chamfer(payload(dry_run=False))
    assert not events and len(objects) == 2
