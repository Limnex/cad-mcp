"""Layout deadlines through the real supervisor; fake workers never use CAD."""

import json
import sys

import pytest

from cad_mcp import server
from cad_mcp.core import isolation
from cad_mcp.core.isolation import ToolProcessSupervisor, _policy
from cad_mcp.core.runtime import CadMcpError


def worker(tmp_path, delay):
    program = tmp_path / "sheet_worker.py"
    actions = tmp_path / "actions.txt"
    program.write_text(
        "import json, sys, time, threading\n"
        "from pathlib import Path\n"
        "state = {'mode':'acad','document_open':False,'file':None}\n"
        "print(json.dumps({'kind':'ready','state':state}), flush=True)\n"
        "for line in sys.stdin:\n"
        " request = json.loads(line)\n"
        " with Path(sys.argv[2]).open('a', encoding='utf-8') as stream:\n"
        "  stream.write(request['tool_name']+'\\n')\n"
        " delay = float(sys.argv[1])\n"
        " if delay < 0: threading.Event().wait()\n"
        " time.sleep(delay)\n"
        " print(json.dumps({'kind':'result','request_id':request['request_id'],"
        "'result':json.dumps({'ok':True}),'state':state}), flush=True)\n",
        encoding="utf-8")
    return ToolProcessSupervisor(
        command=[sys.executable, "-B", str(program), str(delay), str(actions)],
        read_timeout=0.03, write_timeout=0.03), actions


@pytest.mark.parametrize("target,write", [
    ("cad_preview_sheet_layout", False), ("cad_apply_sheet_layout", True),
    ("cad_validate_sheet_layout", False)])
@pytest.mark.parametrize("gateway", [False, True])
def test_layout_calls_outlive_legacy_deadline_in_both_toolsets(tmp_path, target, write, gateway):
    supervisor, actions = worker(tmp_path, 0.10)
    name = "cad_gateway_call" if gateway else target
    arguments = {"params": {"tool_name": target, "arguments": {"params": {}}}} if gateway else {"params": {}}
    actual_target, actual_write = _policy(name, arguments, server._FULL_TOOL_REGISTRY)
    assert (actual_target, actual_write) == (target, write)
    try:
        result = json.loads(supervisor.call(name, arguments, target_name=actual_target, write=actual_write))
        assert result["ok"]
        assert supervisor._generation == 1
        assert actions.read_text(encoding="utf-8").splitlines() == [name]
    finally:
        supervisor.close()


@pytest.mark.parametrize("name,write,code", [
    ("cad_get_drawing_info", False, "WORKER_UNAVAILABLE"),
    ("cad_draw_line", True, "EXECUTION_UNKNOWN")])
def test_existing_tools_keep_original_deadlines(tmp_path, name, write, code):
    supervisor, _ = worker(tmp_path, -1)
    try:
        with pytest.raises(CadMcpError) as failure:
            supervisor.call(name, {"params": {}}, write=write)
        assert failure.value.code == code
        assert supervisor._process is None
    finally:
        supervisor.close()


@pytest.mark.parametrize("target,write,code", [
    ("cad_preview_sheet_layout", False, "WORKER_UNAVAILABLE"),
    ("cad_apply_sheet_layout", True, "EXECUTION_UNKNOWN"),
    ("cad_validate_sheet_layout", False, "WORKER_UNAVAILABLE")])
def test_layout_deadline_remains_bounded_and_never_replays_unknown_write(
        tmp_path, monkeypatch, target, write, code):
    monkeypatch.setitem(isolation._SHEET_LAYOUT_EXECUTION_TIMEOUTS, target, 0.10)
    supervisor, actions = worker(tmp_path, -1)
    try:
        with pytest.raises(CadMcpError) as failure:
            supervisor.call(target, {"params": {}}, write=write)
        assert failure.value.code == code
        assert failure.value.details["worker_cleanup"] == "completed"
        assert supervisor._process is None
        assert actions.read_text(encoding="utf-8").splitlines() == [target]
        if write:
            assert failure.value.details["rollback_status"] == "not_verified"
            for operation in (target, "cad_save_drawing", "cad_close_drawing", "cad_quit"):
                with pytest.raises(CadMcpError, match="执行结果未知"):
                    supervisor.call(operation, {"params": {}}, write=True)
            assert supervisor._generation == 1
            assert actions.read_text(encoding="utf-8").splitlines() == [target]
    finally:
        supervisor.close()
