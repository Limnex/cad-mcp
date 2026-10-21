"""Native FILLET contract, transaction and readback checks without opening CAD."""

import copy
import json
import math
import re
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cad_mcp import server
from cad_mcp.drawing.autocad import AcadBackend
from cad_mcp.drawing.fillet import FilletInput


PAIR = {"handles": ["10", "11"], "pick_points": [[8, 0, 0], [0, 8, 0]], "radius": 2}


def line(handle, start, end):
    return SimpleNamespace(Handle=handle, ObjectName="AcDbLine", Layer="WALL", Thickness=0,
                           StartPoint=start, EndPoint=end)


def arc(radius=2):
    return SimpleNamespace(Handle="C", ObjectName="AcDbArc", Layer="WALL", Thickness=0,
                           Normal=(0, 0, 1), Center=(2, 2, 0), Radius=radius,
                           StartAngle=math.pi, EndAngle=1.5*math.pi)


class Polyline:
    Handle = "A"
    ObjectName = "AcDbPolyline"
    Layer = "WALL"
    Thickness = 0
    Normal = (0, 0, 1)
    Elevation = 0
    Closed = False

    def __init__(self):
        self.Coordinates = (10, 0, 0, 0, 0, 10)
        self.bulges = [0, 0, 0]

    def GetBulge(self, i):
        return self.bulges[i]

    def GetWidth(self, i):
        return (0, 0)


def backend_fixture(tmp_path, operation=None):
    backend = AcadBackend(tmp_path)
    items = [line("A", (10, 0, 0), (0, 0, 0)), line("B", (0, 0, 0), (0, 10, 0))]
    calls = []
    variables = {"FILLETRAD": 9.0, "TRIMMODE": 0, "OSMODE": 167, "CMDECHO": 1,
                 "USERS5": "original", "TILEMODE": 1,
                 "VIEWCTR": (0, 0, 0), "VIEWSIZE": 80.0, "VIEWDIR": (0, 0, 1),
                 "TARGET": (0, 0, 0), "VIEWTWIST": 0.0}
    initial = dict(variables)

    class Model:
        @property
        def Count(self):
            return len(items)

        def Item(self, i):
            return items[i]

    def set_variable(name, value):
        calls.append(("set", name, value))
        variables[name] = value

    backend.msp = Model()
    class View:
        def __init__(self):
            self.Center, self.Direction, self.Target = (0, 0), (0, 0, 1), (0, 0, 0)
            self.Width, self.Height = 100.0, 80.0

        def __setattr__(self, name, value):
            object.__setattr__(self, name, value.value if hasattr(value, "value") else value)

    view = View()
    view_history = []
    def zoom(low, high):
        calls.append(("zoom",))
        view_history.append({k: variables[k] for k in ("VIEWCTR", "VIEWSIZE", "VIEWDIR", "TARGET", "VIEWTWIST")})
        variables.update(VIEWCTR=(20, 30, 0), VIEWSIZE=20.0)
        view.Center, view.Width, view.Height = (20, 30), 25.0, 20.0
    def zoom_previous():
        calls.append(("zoom_previous",))
        variables.update(view_history.pop())
        view.Center, view.Width, view.Height = (0, 0), 100.0, 80.0
    backend.acad = SimpleNamespace(ZoomWindow=zoom, ZoomPrevious=zoom_previous)
    backend.doc = SimpleNamespace(GetVariable=variables.__getitem__, SetVariable=set_variable,
        Layers=SimpleNamespace(Item=lambda name: SimpleNamespace(Lock=False)), ActiveViewport=view)
    backend._check_doc = lambda: None
    backend._scan_checkpoint = lambda index: None
    backup = []

    def begin():
        calls.append(("begin",))
        backup[:] = copy.deepcopy(items)
        return True

    def rollback(started):
        calls.append(("rollback", started))
        items[:] = copy.deepcopy(backup)
        return True

    def send(command):
        calls.append(("command", command))
        assert variables["FILLETRAD"] == 2 or variables["FILLETRAD"] == 0
        assert variables["OSMODE"] == 0
        if operation is not None:
            operation(items)
        marker = re.search(r'"(cadmcp-fillet-[a-f0-9]+):done"', command).group(1)
        variables["USERS5"] = marker + ":done"

    backend._begin_undo = begin
    backend._commit_undo = lambda started: calls.append(("commit", started))
    backend._rollback_undo = rollback
    backend._send_command_and_wait = send
    backend._acquire_autocad_lock = lambda: calls.append(("lock",))
    backend._release_autocad_lock = lambda: calls.append(("unlock",))
    backend._wait_operation_ready = lambda: None
    return backend, items, calls, variables, initial


def round_lines(items):
    items[0].EndPoint = (2, 0, 0)
    items[1].StartPoint = (0, 2, 0)
    items.append(arc())


@pytest.mark.parametrize("patch", [
    {"radius": -1}, {"radius": math.nan}, {"radius": math.inf},
    {"radius": True}, {"radius": "2"},
    {"handles": ["A", "11"]}, {"handles": ["0", "11"]},
    {"handles": ['10\") (alert "bad', "11"]}, {"handles": ["10"]},
    {"pick_points": None}, {"pick_points": [[1, 2, math.inf], [0, 1, 0]]},
    {"mode": "polyline"}, {"trim": 1}, {"dry_run": None}, {"extra": 1},
    {"handles": ["10", "10"], "pick_points": [[1, 2, 0], [1, 2, 0]]},
])
def test_invalid_inputs(patch):
    with pytest.raises(ValidationError):
        FilletInput.model_validate({**PAIR, **patch})


def test_dry_run_has_zero_writes_and_does_not_claim_geometric_success(tmp_path):
    backend, items, calls, variables, initial = backend_fixture(tmp_path)
    original = copy.deepcopy(items)
    result = backend.fillet(**PAIR)
    assert not result["applied"] and not result["verified"] and result["dry_run"]
    assert items == original and variables == initial and calls == []


def test_native_pair_readback_settings_and_operation_lock(tmp_path, monkeypatch):
    backend, items, calls, variables, initial = backend_fixture(tmp_path, round_lines)
    monkeypatch.setattr(server, "_backend", server._AcadLockProxy(backend))
    response = json.loads(server.cad_fillet(FilletInput(**PAIR, dry_run=False)))
    assert response["ok"] and response["applied"] and response["verified"]
    assert response["rounded_arc_count"] == 1
    assert response["created"][0]["handle"] == "12"
    assert response["modified_handles"] == ["10", "11"]
    assert variables == initial
    assert calls[0] == ("lock",) and calls[-1] == ("unlock",)
    assert sum(c[0] == "begin" for c in calls) == 1
    assert not any(c[0] == "rollback" for c in calls)
    commands = [c[1] for c in calls if c[0] == "command"]
    assert len(commands) == 1 and "command-s" in commands[0]
    assert '(handent "A") (trans (list 8 0 0) 0 1)' in commands[0]
    assert '(handent "B") (trans (list 0 8 0) 0 1)' in commands[0]
    assert "_Polyline" not in commands[0]


def test_no_trim_retains_sources(tmp_path):
    backend, _, _, variables, initial = backend_fixture(tmp_path, lambda items: items.append(arc()))
    result = backend.fillet(**PAIR, trim=False, dry_run=False)
    assert result["verified"] and result["modified_handles"] == []
    assert result["before"] == result["after"]
    assert variables == initial


@pytest.mark.parametrize("failure", ["radius", "marker", "exception", "elevation"])
def test_partial_or_unverified_write_rolls_back_and_restores_settings(tmp_path, failure):
    def operation(items):
        round_lines(items)
        if failure == "radius":
            items[-1].Radius = 3
        elif failure == "elevation":
            items[-1].Center = (2, 2, 5)
        elif failure == "exception":
            raise RuntimeError("injected native error")

    backend, items, calls, variables, initial = backend_fixture(tmp_path, operation)
    original = copy.deepcopy(items)
    if failure == "marker":
        send = backend._send_command_and_wait
        def no_marker(command):
            send(command)
            variables["USERS5"] = "stale:done"
        backend._send_command_and_wait = no_marker
    with pytest.raises(RuntimeError) as error:
        backend.fillet(**PAIR, dry_run=False)
    assert error.value.rollback_status == "succeeded"
    assert items == original and variables == initial
    assert ("rollback", True) in calls


def test_noop_does_not_undo_previous_operation(tmp_path):
    backend, _, calls, variables, initial = backend_fixture(tmp_path)
    with pytest.raises(RuntimeError, match="未改变图形") as error:
        backend.fillet(**PAIR, dry_run=False)
    assert error.value.rollback_status == "not_needed"
    assert not any(c[0] == "rollback" for c in calls)
    assert variables == initial


@pytest.mark.parametrize("condition", ["missing", "locked", "tilted", "different_z", "pick_z", "paper", "same_line"])
def test_preflight_stops_before_undo_or_settings(tmp_path, condition):
    backend, items, calls, variables, _ = backend_fixture(tmp_path)
    params = copy.deepcopy(PAIR)
    if condition == "missing":
        params["handles"][1] = "999"
    elif condition == "locked":
        backend.doc.Layers.Item = lambda name: SimpleNamespace(Lock=True)
    elif condition == "tilted":
        items[0].EndPoint = (0, 0, 1)
    elif condition == "different_z":
        items[1].StartPoint, items[1].EndPoint = (0, 0, 2), (0, 10, 2)
    elif condition == "pick_z":
        params["pick_points"][0][2] = 2
    elif condition == "paper":
        variables["TILEMODE"] = 0
    else:
        params["handles"][1] = "10"
    with pytest.raises(ValueError):
        backend.fillet(**params, dry_run=False)
    assert calls == []


def test_polyline_rounds_with_native_option_and_measured_bulge_radius(tmp_path):
    def operation(items):
        items[0].Coordinates = (10, 0, 2, 0, 0, 2, 0, 10)
        items[0].bulges = [0, -math.tan(math.pi/8), 0, 0]
    backend, items, calls, variables, initial = backend_fixture(tmp_path, operation)
    items[:] = [Polyline()]
    result = backend.fillet(mode="polyline", handles=["10"], radius=2, dry_run=False)
    assert result["verified"] and result["rounded_arc_count"] == 1
    command = next(c[1] for c in calls if c[0] == "command")
    assert '"_Polyline" (handent "A")' in command
    assert variables == initial


def test_polyline_zero_radius_removes_arc(tmp_path):
    def operation(items):
        items[0].Coordinates = (10, 0, 0, 0, 0, 10)
        items[0].bulges = [0, 0, 0]
    backend, items, _, _, _ = backend_fixture(tmp_path, operation)
    polyline = Polyline()
    polyline.Coordinates = (10, 0, 2, 0, 0, 2, 0, 10)
    polyline.bulges = [0, -math.tan(math.pi/8), 0, 0]
    items[:] = [polyline]
    result = backend.fillet(mode="polyline", handles=["10"], radius=0, dry_run=False)
    assert result["verified"] and result["rounded_arc_count"] == 0


def test_zero_radius_connects_lines(tmp_path):
    def operation(items):
        items[0].EndPoint = (0, 0, 0)
        items[1].StartPoint = (0, 0, 0)
    backend, items, _, _, _ = backend_fixture(tmp_path, operation)
    items[0].EndPoint, items[1].StartPoint = (2, 0, 0), (0, 2, 0)
    result = backend.fillet(**{**PAIR, "radius": 0}, dry_run=False)
    assert result["verified"] and result["rounded_arc_count"] == 0


def test_failed_rollback_is_not_reported_as_verified(tmp_path):
    backend, _, _, _, _ = backend_fixture(tmp_path, lambda items: items.append(arc(5)))
    backend._rollback_undo = lambda started: True  # Claimed success, geometry remains changed.
    with pytest.raises(RuntimeError) as error:
        backend.fillet(**PAIR, dry_run=False)
    assert error.value.rollback_status == "failed_or_unverified"


def test_trimming_existing_arc_is_not_evidence_of_a_new_fillet(tmp_path):
    def operation(items):
        items[0].EndAngle = math.pi / 2
    backend, items, _, _, _ = backend_fixture(tmp_path, operation)
    source = arc()
    source.Handle = "A"
    items[0] = source
    with pytest.raises(RuntimeError, match="无法回读验证指定半径"):
        backend.fillet(**PAIR, dry_run=False)


def test_zero_radius_requires_connected_line_endpoints(tmp_path):
    def operation(items):
        items[0].EndPoint = (1, 0, 0)
    backend, items, _, _, _ = backend_fixture(tmp_path, operation)
    items[0].EndPoint = (2, 0, 0)
    items[1].StartPoint = (0, 2, 0)
    with pytest.raises(RuntimeError, match="端点未连接"):
        backend.fillet(**{**PAIR, "radius": 0}, dry_run=False)


def test_pair_can_select_two_segments_of_the_same_polyline(tmp_path):
    backend, items, calls, _, _ = backend_fixture(tmp_path)
    items[:] = [Polyline()]
    result = backend.fillet(**{**PAIR, "handles": ["10", "10"]})
    assert result["handles"] == ["10", "10"] and calls == []


def test_same_polyline_uses_guarded_points_and_restores_view(tmp_path):
    def operation(items):
        items[0].Coordinates = (10, 0, 2, 0, 0, 2, 0, 10)
        items[0].bulges = [0, -math.tan(math.pi/8), 0, 0]
    backend, items, calls, variables, initial = backend_fixture(tmp_path, operation)
    items[:] = [Polyline()]
    result = backend.fillet(**{**PAIR, "handles": ["10", "10"]}, dry_run=False)
    assert result["verified"] and result["rounded_arc_count"] == 1
    command = next(c[1] for c in calls if c[0] == "command")
    assert '(nentselp "" p1)' in command and '(eq (car selected2) e2)' in command
    assert '(list "_.FILLET" p1 p2)' in command
    assert '(list (handent "A")' not in command
    assert ("zoom",) in calls
    assert ("zoom_previous",) in calls
    assert tuple(backend.doc.ActiveViewport.Center) == (0, 0)
    assert backend.doc.ActiveViewport.Width == 100 and backend.doc.ActiveViewport.Height == 80
    assert variables == initial


def test_stale_viewport_properties_cannot_hide_failed_actual_view_restore(tmp_path):
    def operation(items):
        items[0].Coordinates = (10, 0, 2, 0, 0, 2, 0, 10)
        items[0].bulges = [0, -math.tan(math.pi/8), 0, 0]
    backend, items, _, variables, initial = backend_fixture(tmp_path, operation)
    items[:] = [Polyline()]
    zoom = backend.acad.ZoomWindow
    def zoom_with_stale_com(low, high):
        zoom(low, high)
        backend.doc.ActiveViewport.Center = (0, 0)
        backend.doc.ActiveViewport.Width, backend.doc.ActiveViewport.Height = 100.0, 80.0
    backend.acad.ZoomWindow = zoom_with_stale_com
    backend.acad.ZoomPrevious = lambda: None
    with pytest.raises(RuntimeError, match="临时视图恢复") as error:
        backend.fillet(**{**PAIR, "handles": ["10", "10"]}, dry_run=False)
    assert error.value.rollback_status == "failed_or_unverified"
    assert variables["VIEWCTR"] != initial["VIEWCTR"]


def test_same_polyline_mismatched_pick_restores_view_without_undo(tmp_path):
    backend, items, calls, variables, initial = backend_fixture(tmp_path)
    items[:] = [Polyline()]
    def mismatch(command):
        marker = re.search(r'"(cadmcp-fillet-[a-f0-9]+):done"', command).group(1)
        variables["USERS5"] = marker + ":selection_mismatch"
    backend._send_command_and_wait = mismatch
    with pytest.raises(ValueError, match="未命中指定图元") as error:
        backend.fillet(**{**PAIR, "handles": ["10", "10"]}, dry_run=False)
    assert error.value.rollback_status == "not_needed"
    assert not any(c[0] == "rollback" for c in calls)
    assert tuple(backend.doc.ActiveViewport.Center) == (0, 0)
    assert variables == initial


def test_curve_pair_uses_native_closest_points_and_checks_both_handles(tmp_path):
    backend, items, calls, variables, initial = backend_fixture(tmp_path, lambda items: items.append(arc()))
    circle = arc()
    circle.Handle, circle.ObjectName = "A", "AcDbCircle"
    items[0] = circle
    result = backend.fillet(**PAIR, dry_run=False)
    assert result["verified"] and result["rounded_arc_count"] == 1
    command = next(c[1] for c in calls if c[0] == "command")
    assert '(vlax-curve-getClosestPointTo (vlax-ename->vla-object e1)' in command
    assert '(vlax-curve-getClosestPointTo (vlax-ename->vla-object e2)' in command
    assert '(eq (car selected1) e1)' in command and '(eq (car selected2) e2)' in command
    assert '(handent "A") e2 (handent "B")' in command
    assert ("zoom_previous",) in calls and variables == initial


def test_circle_and_arc_selection_preflight(tmp_path):
    backend, items, calls, _, _ = backend_fixture(tmp_path)
    circle = arc()
    circle.Handle, circle.ObjectName = "A", "AcDbCircle"
    source_arc = arc()
    source_arc.Handle = "B"
    items[:] = [circle, source_arc]
    assert not backend.fillet(**PAIR)["applied"]
    circle.Normal = (0, 1, 0)
    with pytest.raises(ValueError, match="法向"):
        backend.fillet(**PAIR)
    assert calls == []


def test_cannot_start_undo_stops_before_native_write(tmp_path):
    backend, _, calls, variables, initial = backend_fixture(tmp_path)
    backend._begin_undo = lambda: False
    with pytest.raises(RuntimeError, match="独立圆角撤销组"):
        backend.fillet(**PAIR, dry_run=False)
    assert calls == [] and variables == initial


def test_failed_setting_restore_is_visible(tmp_path):
    backend, _, _, variables, _ = backend_fixture(tmp_path, round_lines)
    original = backend.doc.SetVariable
    def fail_restore(name, value):
        if name == "OSMODE" and value == 167:
            raise RuntimeError("injected restore failure")
        original(name, value)
    backend.doc.SetVariable = fail_restore
    with pytest.raises(RuntimeError, match="系统变量恢复失败") as error:
        backend.fillet(**PAIR, dry_run=False)
    assert error.value.rollback_status == "failed_or_unverified"


def test_mcp_schema_and_gateway_registration():
    tool = server.mcp._tool_manager.get_tool("cad_fillet")
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.destructiveHint is True
    schema = FilletInput.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["dry_run"]["default"] is True
    assert "fillet" not in server._AcadLockProxy._MUTATING_METHODS
