"""No live CAD: exercise native dispatch, geometry readback and failure safety."""

import asyncio
import copy
import json
import math
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import pytest
import pythoncom
from pydantic import ValidationError

from cad_mcp import server
from cad_mcp.core.isolation import ToolProcessSupervisor, _policy
from cad_mcp.core.runtime import CadMcpError, backend_capabilities
from cad_mcp.drawing.autocad import AcadBackend, _ComRetryProxy
from cad_mcp.drawing.document import CadDocument
from cad_mcp.drawing.native_extend import SETTINGS, _snapshot, set_short_variable


class Curve:
    Layer = "WALL"
    OwnerID = 100
    Normal = (0, 0, 1)
    Closed = False
    Elevation = 0.0
    Radius = 10.0
    Center = (0, 0, 0)
    StartAngle = 0.0
    EndAngle = math.pi / 2

    def __init__(self, handle="A", kind="AcDbLine", start=(0, 0, 0), end=(10, 0, 0)):
        self.Handle, self.ObjectName = handle, kind
        self.start, self.end = start, end
        self.Coordinates = [0, 0, 10, 0, 20, 0]
        self.hits = [20, 0, 0]
        self.intersect_modes = []

    @property
    def StartPoint(self):
        if self.ObjectName == "AcDbArc":
            return (10*math.cos(self.StartAngle), 10*math.sin(self.StartAngle), 0)
        return self.start

    @property
    def EndPoint(self):
        if self.ObjectName == "AcDbArc":
            return (10*math.cos(self.EndAngle), 10*math.sin(self.EndAngle), 0)
        return self.end

    @property
    def Length(self):
        if self.ObjectName == "AcDbPolyline":
            return sum(math.dist(self.Coordinates[i:i+2], self.Coordinates[i+2:i+4])
                       for i in range(0, len(self.Coordinates)-2, 2))
        return math.dist(self.start, self.end)

    @property
    def ArcLength(self):
        return self.Radius * ((self.EndAngle - self.StartAngle) % math.tau)

    def GetBulge(self, index):
        return getattr(self, "bulges", [0, 0, 0])[index]

    def GetWidth(self, index):
        return (0.0, 0.0)

    def IntersectWith(self, boundary, mode):
        self.intersect_modes.append(mode)
        return self.hits


@pytest.fixture
def cad(tmp_path):
    raw = AcadBackend(tmp_path)
    raw.subject, raw.boundary = Curve(), Curve("B", start=(20, -10, 0), end=(20, 10, 0))
    raw.entities = {"A": raw.subject, "B": raw.boundary}
    raw.variables = {name: value + 1 for name, value in SETTINGS.items()}
    raw.variables.update(TILEMODE=1, VIEWCTR=(0, 0, 0), VIEWSIZE=1e8,
                         VIEWDIR=(0, 0, 1), VIEWTWIST=0, UCSORG=(0, 0, 0),
                         UCSXDIR=(1, 0, 0), UCSYDIR=(0, 1, 0))
    raw.events, raw.sent = [], []
    raw.layer = SimpleNamespace(Lock=False)
    raw.variable_writes = []
    def set_variable(name, value):
        assert value.varianttype == pythoncom.VT_I2
        raw.variable_writes.append((name, value.varianttype, value.value))
        raw.variables[name] = value.value
    raw.doc = SimpleNamespace(
        HandleToObject=lambda handle: raw.entities[handle],
        GetVariable=lambda name: raw.variables[name],
        SetVariable=set_variable,
        Layers=SimpleNamespace(Item=lambda name: raw.layer))
    raw.msp = SimpleNamespace(ObjectID=100)
    raw._check_doc = lambda: None
    raw._acquire_autocad_lock = lambda: raw.events.append("lock")
    raw._release_autocad_lock = lambda: raw.events.append("unlock")
    raw._wait_operation_ready = lambda: None

    def begin():
        raw.events.append("begin")
        raw.backup = copy.deepcopy(raw.entities)
        return True

    def commit(started):
        if started:
            raw.events.append("commit")

    def rollback(started):
        raw.events.append("rollback")
        for key, value in raw.backup.items():
            raw.entities[key].__dict__.clear()
            raw.entities[key].__dict__.update(copy.deepcopy(value.__dict__))
        return True

    raw._begin_undo, raw._commit_undo, raw._rollback_undo = begin, commit, rollback
    raw.effect = lambda: setattr(raw.subject, "end", (20, 0, 0))
    raw.receipt_text = "OK\n"

    def send(command, *, completion_check):
        raw.sent.append(command)
        loader = re.fullmatch(r'\(load "((?:\\.|[^"\\])*)"\)\n', command)
        assert loader is not None and len(command) < 512
        script = loader[1].replace('\\"', '"').replace('\\\\', '\\')
        command = Path(script).read_text(encoding="utf-8")
        raw.script = command
        assert not completion_check()
        raw.effect()
        match = re.search(r'\(open "((?:\\.|[^"\\])*)" "w"\)', command)
        assert match is not None
        path = match[1].replace('\\"', '"').replace('\\\\', '\\')
        Path(path).write_text(raw.receipt_text, encoding="ascii")
        assert completion_check()

    raw._send_command_and_wait = send
    return raw


def call(cad, *, endpoint="end", **kwargs):
    return cad.extend_entity("10", ["11"], endpoint, **kwargs)


def test_preflight_default_is_read_only_and_does_not_claim_extension(cad):
    before = _snapshot(cad.subject)
    variables = cad.variables.copy()
    result = call(cad)
    assert result["preflight_passed"] and result["dry_run"]
    assert not result["verified"] and not result["applied"]
    assert result["before"] == result["after"] == before
    assert cad.events == cad.sent == [] and cad.variables == variables


@pytest.mark.parametrize("endpoint,point", [("start", (-10, 0, 0)), ("end", (20, 0, 0))])
@pytest.mark.parametrize("extend_edges", [False, True])
def test_native_line_extension_verifies_selected_endpoint(cad, endpoint, point, extend_edges):
    cad.effect = lambda: setattr(cad.subject, endpoint, point)
    cad.subject.hits = point
    result = call(cad, endpoint=endpoint, extend_edges=extend_edges, dry_run=False)
    assert result["verified"] and result["applied"]
    assert result["after"][endpoint] == list(point)
    assert result["after"]["length"] > result["before"]["length"]
    assert cad.events == ["begin", "commit"] and len(cad.sent) == 1
    assert cad.subject.intersect_modes == [2 if extend_edges else 0]
    assert 'command-s "_.EXTEND"' in cad.script
    assert 'p (trans w 0 1)' in cad.script
    assert f'(setvar "EDGEMODE" {int(extend_edges)})' in cad.script


@pytest.mark.parametrize("endpoint", ["start", "end"])
def test_arc_extension_preserves_center_radius_and_other_end(cad, endpoint):
    cad.subject.ObjectName = "AcDbArc"
    cad.effect = lambda: setattr(cad.subject, "StartAngle" if endpoint == "start" else "EndAngle",
                                  -math.pi/2 if endpoint == "start" else math.pi)
    cad.subject.hits = (0, -10, 0) if endpoint == "start" else (-10, 0, 0)
    result = call(cad, endpoint=endpoint, dry_run=False)
    assert result["verified"] and result["after"]["type"] == "ARC"
    assert result["after"]["length"] == pytest.approx(math.pi*10)


@pytest.mark.parametrize("endpoint", ["start", "end"])
def test_open_polyline_extension_preserves_interior_vertices(cad, endpoint):
    cad.subject.ObjectName = "AcDbPolyline"
    def effect():
        cad.subject.Coordinates[0 if endpoint == "start" else -2] += -10 if endpoint == "start" else 10
    cad.effect = effect
    cad.subject.hits = (-10, 0, 0) if endpoint == "start" else (30, 0, 0)
    result = call(cad, endpoint=endpoint, dry_run=False)
    assert result["verified"] and result["after"]["vertices"][1] == result["before"]["vertices"][1]


@pytest.mark.parametrize("setup", [
    lambda c: setattr(c.subject, "ObjectName", "AcDbCircle"),
    lambda c: setattr(c.subject, "ObjectName", "AcDbSpline"),
    lambda c: setattr(c.layer, "Lock", True),
    lambda c: setattr(c.subject, "OwnerID", 200),
    lambda c: setattr(c.boundary, "OwnerID", 200),
    lambda c: c.entities.pop("B"),
    lambda c: c.variables.__setitem__("TILEMODE", 0),
    lambda c: setattr(c.boundary, "end", (20, 10, 1)),
    lambda c: setattr(c.boundary, "start", (20, -10, 1)) or setattr(c.boundary, "end", (20, 10, 1)),
    lambda c: setattr(c.subject, "end", (float("nan"), 0, 0)),
    lambda c: setattr(c.subject, "ObjectName", "AcDbPolyline") or setattr(c.subject, "Closed", True),
    lambda c: setattr(c.subject, "ObjectName", "AcDbArc") or setattr(c.subject, "Normal", (0, 1, 0)),
    lambda c: setattr(c.subject, "end", c.subject.start),
])
def test_bad_geometry_selection_or_locked_layer_never_starts_undo(cad, setup):
    setup(cad)
    with pytest.raises((ValueError, CadMcpError)):
        call(cad, dry_run=False)
    assert cad.events == cad.sent == []


@pytest.mark.parametrize("bad", [
    {"subject_handle": "0"}, {"subject_handle": '10)(command "ERASE")'},
    {"subject_handle": "A"}, {"subject_handle": "1"*41},
    {"boundary_handles": []}, {"boundary_handles": ["11"]*101},
    {"boundary_handles": ["11", "011"]}, {"boundary_handles": ["010"]},
    {"endpoint": "both"}, {"dry_run": "false"}, {"extend_edges": 1}, {"unknown": 0},
])
def test_input_contract_rejects_ambiguous_or_unsafe_requests(bad):
    values = {"subject_handle": "10", "boundary_handles": ["11"], "endpoint": "end", **bad}
    with pytest.raises(ValidationError):
        server.ExtendInput(**values)


def test_noop_does_not_undo_the_preceding_operation(cad):
    cad.effect = lambda: None
    with pytest.raises(CadMcpError) as caught:
        call(cad, dry_run=False)
    assert caught.value.code == "NO_GEOMETRY_CHANGE"
    assert caught.value.rollback_status == "not_needed"
    assert cad.events == ["begin", "commit"]


@pytest.mark.parametrize("effect,hits", [
    (lambda c: setattr(c.subject, "end", (20, 0, 0)), []),
    (lambda c: setattr(c.subject, "end", (20, 10, 0)), [20, 10, 0]),
    (lambda c: setattr(c.subject, "start", (-10, 0, 0)), [-10, 0, 0]),
    (lambda c: setattr(c.boundary, "end", (20, 30, 0)), [20, 0, 0]),
])
def test_invalid_native_readback_rolls_back_and_verifies_restore(cad, effect, hits):
    before, boundary = _snapshot(cad.subject), _snapshot(cad.boundary)
    cad.effect = lambda: effect(cad)
    cad.subject.hits = hits
    with pytest.raises((RuntimeError, CadMcpError)) as caught:
        call(cad, dry_run=False)
    assert caught.value.rollback_status == "succeeded"
    assert _snapshot(cad.subject) == before and _snapshot(cad.boundary) == boundary
    assert cad.events == ["begin", "rollback"]


def test_native_command_error_closes_empty_group_without_undo(cad):
    cad.effect = lambda: None
    cad.receipt_text = "ERROR\n"
    with pytest.raises(RuntimeError, match="原生 EXTEND 命令失败") as caught:
        call(cad, dry_run=False)
    assert caught.value.rollback_status == "not_needed"
    assert cad.events == ["begin", "commit"]


def test_failed_rollback_is_unknown_instead_of_retryable(cad):
    cad.subject.hits = []
    cad._rollback_undo = lambda started: False
    with pytest.raises(CadMcpError) as caught:
        call(cad, dry_run=False)
    assert caught.value.code == "EXECUTION_UNKNOWN" and not caught.value.retryable
    assert caught.value.details["rollback_status"] == "failed_or_unverified"


def test_native_timeout_never_issues_undo_or_replays_command(cad):
    def hang(command, **kwargs):
        cad.sent.append(command)
        raise TimeoutError("native command completion not acknowledged")
    cad._send_command_and_wait = hang
    with pytest.raises(CadMcpError) as caught:
        call(cad, dry_run=False)
    assert caught.value.code == "EXECUTION_UNKNOWN" and not caught.value.retryable
    assert caught.value.details["rollback_status"] == "not_verified"
    assert cad.events == ["begin"] and len(cad.sent) == 1


def test_failed_readback_restores_settings_and_geometry(cad):
    before, variables = _snapshot(cad.subject), cad.variables.copy()
    def bad_restore():
        cad.subject.end = (20, 0, 0)
        cad.variables["EDGEMODE"] = 77
    cad.effect = bad_restore
    with pytest.raises(RuntimeError, match="系统变量未恢复") as caught:
        call(cad, dry_run=False)
    assert caught.value.rollback_status == "succeeded"
    assert _snapshot(cad.subject) == before and cad.variables == variables
    assert cad.variable_writes == [("EDGEMODE", pythoncom.VT_I2, variables["EDGEMODE"])]


@pytest.mark.parametrize("value", [0, 1, 167, -32768, 32767])
def test_short_variable_sends_exact_com_type_and_reads_back(cad, value):
    set_short_variable(cad.doc, "OSMODE", value)
    assert cad.variable_writes == [("OSMODE", pythoncom.VT_I2, value)]
    assert cad.variables["OSMODE"] == value


@pytest.mark.parametrize("value", [True, 1.0, "1", -32769, 32768])
def test_short_variable_rejects_bad_type_or_range_before_write(cad, value):
    with pytest.raises(ValueError):
        set_short_variable(cad.doc, "OSMODE", value)
    assert cad.variable_writes == []


def test_short_variable_detects_ignored_com_write(cad):
    cad.doc.SetVariable = lambda name, value: None
    with pytest.raises(RuntimeError, match="回读不一致"):
        set_short_variable(cad.doc, "OSMODE", 100)


def test_lock_proxy_does_not_create_an_extra_or_preflight_undo_group(cad):
    proxy = server._AcadLockProxy(cad)
    call(proxy)
    assert cad.events == ["lock", "unlock"]
    cad.events.clear()
    call(proxy, dry_run=False)
    assert cad.events == ["lock", "begin", "commit", "unlock"]


def test_tool_gateway_schema_and_write_policy(cad, monkeypatch):
    monkeypatch.setattr(server, "_get_backend", lambda: cad)
    args = {"params": {"subject_handle": "010", "boundary_handles": ["011"], "endpoint": "end"}}
    described = json.loads(asyncio.run(server._gateway_describe(server.GatewayDescribeParams(tool_name="cad_extend"))))
    assert described["tool"]["name"] == "cad_extend"
    result = json.loads(asyncio.run(server._gateway_call(server.GatewayCallParams(tool_name="cad_extend", arguments=args))))
    assert result["ok"] and result["dry_run"] and result["handle"] == "10"
    assert _policy("cad_extend", args, server._FULL_TOOL_REGISTRY) == ("cad_extend", True)
    args["params"]["dry_run"] = True
    assert _policy("cad_extend", args, server._FULL_TOOL_REGISTRY) == ("cad_extend", False)
    args["params"]["dry_run"] = False
    result = json.loads(server.cad_extend(server.ExtendInput(**args["params"])))
    assert result["ok"] and result["verified"]
    assert backend_capabilities("acad")["native_extend"] is True


def test_backend_without_native_interface_has_no_side_effects(tmp_path, monkeypatch):
    drawing = CadDocument(tmp_path)
    drawing.new_drawing("untouched.dxf")
    monkeypatch.setattr(server, "_get_backend", lambda: drawing)
    before = len(drawing.msp)
    result = json.loads(server.cad_extend(server.ExtendInput(subject_handle="10", boundary_handles=["11"], endpoint="end")))
    assert not result["ok"] and result["error_detail"]["code"] == "UNSUPPORTED_OPERATION"
    assert len(drawing.msp) == before


def test_completion_receipt_guards_an_idle_but_not_yet_started_command(tmp_path, monkeypatch):
    from cad_mcp.drawing import autocad
    raw = AcadBackend(tmp_path)
    sent = []
    raw.doc = SimpleNamespace(SendCommand=sent.append, GetVariable=lambda name: 0 if name == "CMDACTIVE" else "")
    raw.acad = SimpleNamespace(GetAcadState=lambda: SimpleNamespace(IsQuiescent=True))
    now = [0.0]
    monkeypatch.setattr(autocad.time, "time", lambda: now[0])
    monkeypatch.setattr(autocad.time, "sleep", lambda seconds: now.__setitem__(0, now[0]+seconds))
    with pytest.raises(RuntimeError, match="未执行完成"):
        raw._send_command_and_wait("command", timeout=0.3, completion_check=lambda: False)
    assert sent == ["command"]
    raw._send_command_and_wait("second", timeout=0.3, completion_check=lambda: True)
    assert sent == ["command", "second"]


def test_native_sendcommand_bypasses_com_write_retries(tmp_path):
    raw = AcadBackend(tmp_path)
    sent = []
    def refused(command):
        sent.append(command)
        raise pythoncom.com_error(-2147418111, "busy", None, None)
    raw.doc = _ComRetryProxy(SimpleNamespace(SendCommand=refused))
    with pytest.raises(pythoncom.com_error):
        raw._send_command_and_wait("command", completion_check=lambda: False)
    assert sent == ["command"]


def test_unknown_result_from_responsive_worker_blocks_following_writes(tmp_path):
    # Use the production supervisor and an actual child, not a mocked call().
    worker = tmp_path / "unknown_worker.py"
    worker.write_text('''import json, sys
state = {"mode": "acad", "document_open": True, "file": "temporary.dwg"}
print(json.dumps({"kind": "ready", "state": state}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    result = {"ok": False, "error_detail": {"code": "EXECUTION_UNKNOWN"}}
    if request["tool_name"] == "cad_health":
        result = {"ok": True}
    if request["tool_name"] == "legacy_plain_text":
        print(json.dumps({"kind": "result", "state": state, "request_id": request["request_id"], "result": "unchanged legacy result"}), flush=True)
        continue
    print(json.dumps({"kind": "result", "state": state, "request_id": request["request_id"], "result": json.dumps(result)}), flush=True)
''', encoding="utf-8")
    supervisor = ToolProcessSupervisor(command=[sys.executable, "-B", str(worker)])
    try:
        assert supervisor.call("legacy_plain_text", {"params": {}}, write=False) == "unchanged legacy result"
        assert not supervisor._write_unknown
        result = json.loads(supervisor.call("cad_extend", {"params": {}}, write=True))
        assert result["error_detail"]["code"] == "EXECUTION_UNKNOWN"
        assert supervisor._process.poll() is None
        assert supervisor._write_unknown
        for name in ("cad_extend", "cad_save_drawing", "cad_close_drawing", "cad_quit", "cad_switch_mode"):
            with pytest.raises(CadMcpError) as caught:
                supervisor.call(name, {"params": {}}, write=True)
            assert caught.value.code == "EXECUTION_UNKNOWN"
        health = json.loads(supervisor.call("cad_health", {"params": {"reconnect": True}}))
        assert health["worker"]["write_result_unknown"]
    finally:
        supervisor.close()


def test_view_restore_failure_blocks_following_writes(cad):
    def bad_view():
        cad.subject.end = (20, 0, 0)
        cad.variables["VIEWSIZE"] = 0.2
    cad.effect = bad_view
    with pytest.raises(CadMcpError) as caught:
        call(cad, dry_run=False)
    assert caught.value.code == "EXECUTION_UNKNOWN"
    assert caught.value.details["rollback_status"] == "failed_or_unverified"
    assert _snapshot(cad.subject)["end"] == [10, 0, 0]


def test_native_script_keeps_100_long_handles_off_the_command_line(tmp_path):
    from cad_mcp.drawing.native_extend import _command
    command = _command("10", [str(2**120+i) for i in range(100)], "start", False,
                       tmp_path / ("a"*160) / "complete.txt")
    assert len(command) > 2048
    assert 'vla-ZoomCenter' in command and 'vla-put-ActiveViewport' in command
    assert '(vl-catch-all-error-p restore_result)' in command


def test_existing_command_sender_preserves_com_retry_behavior(tmp_path, monkeypatch):
    from cad_mcp.drawing import autocad
    raw = AcadBackend(tmp_path)
    sent = []
    def busy_once(command):
        sent.append(command)
        if len(sent) == 1:
            raise pythoncom.com_error(-2147418111, "busy", None, None)
    raw.doc = _ComRetryProxy(SimpleNamespace(
        SendCommand=busy_once, GetVariable=lambda name: 0 if name == "CMDACTIVE" else ""))
    raw.acad = SimpleNamespace(GetAcadState=lambda: SimpleNamespace(IsQuiescent=True))
    now = [0.0]
    monkeypatch.setattr(autocad.time, "time", lambda: now[0])
    monkeypatch.setattr(autocad.time, "sleep", lambda seconds: now.__setitem__(0, now[0]+seconds))
    raw._send_command_and_wait("existing caller", timeout=1)
    assert sent == ["existing caller", "existing caller"]
