"""The real supervisor must survive faults and keep uncertain writes blocked."""

import asyncio
import json
from pathlib import Path
import sys
import threading
import time

import pytest
from mcp.server.fastmcp import FastMCP

from cad_mcp import server
from cad_mcp.core import isolation
from cad_mcp.core.isolation import ToolProcessSupervisor, _policy, install_isolated_execution
from cad_mcp.core.runtime import CadMcpError


WORKER = Path(__file__).with_name("_runtime_fault_worker.py")


def configure_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("CAD_MODE", "dxf")
    monkeypatch.setenv("CAD_DRAWINGS_DIR", str(tmp_path / "drawings"))
    monkeypatch.setenv("CAD_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("CAD_MCP_RUN_ACAD_INTEGRATION", raising=False)


def fake_supervisor(kind, tmp_path, **kwargs):
    return ToolProcessSupervisor(
        command=[sys.executable, "-B", str(WORKER), kind, str(tmp_path)], **kwargs)


def test_default_worker_preserves_dxf_document_state_between_requests(monkeypatch, tmp_path):
    configure_environment(monkeypatch, tmp_path)
    supervisor = ToolProcessSupervisor(mode="dxf")
    try:
        created = json.loads(supervisor.call("cad_new_drawing", {"params": {"filename": "state.dxf"}}, write=True))
        assert created["ok"] is True
        process = supervisor._process
        for x in (0, 20):
            drawn = json.loads(supervisor.call("cad_draw_line", {"params": {
                "x1": x, "y1": 0, "x2": x + 10, "y2": 0}}, write=True))
            assert drawn["ok"] is True
        queried = json.loads(supervisor.call("cad_query_entities", {"params": {"limit": 10}}))
        assert queried["ok"] is True
        assert len(queried["entities"]) == 2
        assert supervisor._process is process
        health = json.loads(supervisor.call("cad_health", {"params": {}}))
        assert health["mode"] == "dxf"
        assert health["document_open"] is True
        assert health["worker"]["busy"] is False
        assert health["worker"]["generation"] == 1
    finally:
        supervisor.close()
    assert process.poll() is not None


def test_default_worker_keeps_loaded_rules_and_rejects_invalid_parameters(monkeypatch, tmp_path):
    configure_environment(monkeypatch, tmp_path)
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"rule_set_id": "worker-rule", "name": "worker rule", "rules": []}), encoding="utf-8")
    supervisor = ToolProcessSupervisor(mode="dxf")
    try:
        loaded = json.loads(supervisor.call("cad_load_environment_rule_set", {"params": {"path": str(rules)}}))
        assert loaded["ok"] is True
        listed = json.loads(supervisor.call("cad_list_environment_rule_sets", {"params": {}}))
        assert any(item["rule_set_id"] == "worker-rule" for item in listed["rule_sets"])
        # Switching modes without an open document must preserve the same worker cache.
        for mode in ("acad", "dxf"):
            switched = json.loads(supervisor.call("cad_switch_mode", {"params": {"mode": mode}}, write=True))
            assert switched["ok"] is True
            listed = json.loads(supervisor.call("cad_list_environment_rule_sets", {"params": {}}))
            assert any(item["rule_set_id"] == "worker-rule" for item in listed["rule_sets"])
        invalid = json.loads(supervisor.call("cad_query_entities", {"params": {"limit": 500}}))
        assert invalid["error_detail"]["code"] == "INVALID_ARGUMENT"
        assert supervisor._needs_bind is False
        assert supervisor._write_unknown is False
    finally:
        supervisor.close()


@pytest.mark.parametrize("kind", ["isolated-write-hang-worker", "isolated-write-crash-worker"])
def test_timeout_or_crash_after_write_never_claims_rollback_or_retries(monkeypatch, tmp_path, kind):
    configure_environment(monkeypatch, tmp_path)
    supervisor = fake_supervisor(kind, tmp_path, write_timeout=0.3)
    arguments = {"params": {"x1": 0, "y1": 0, "x2": 1, "y2": 1}}
    try:
        with pytest.raises(CadMcpError) as caught:
            supervisor.call("cad_draw_line", arguments, write=True)
        assert caught.value.code == "EXECUTION_UNKNOWN"
        assert caught.value.retryable is False
        assert caught.value.details["rollback_status"] == "not_verified"
        assert caught.value.details["worker_cleanup"] == "completed"
        assert (tmp_path / "simulated-actions").read_text(encoding="utf-8") == "applied\n"
        generation = supervisor._generation
        for name in ("cad_draw_line", "cad_save_drawing", "cad_close_drawing", "cad_quit", "cad_switch_mode"):
            with pytest.raises(CadMcpError, match="执行结果未知"):
                supervisor.call(name, arguments if name == "cad_draw_line" else {"params": {}}, write=True)
        assert supervisor._generation == generation
        health = json.loads(supervisor.call("cad_health", {"params": {}}))
        assert health["worker"]["write_result_unknown"] is True
        # Reconnecting and inspecting a document cannot acknowledge an unknown write.
        reconnected = json.loads(supervisor.call("cad_health", {"params": {"reconnect": True}}))
        assert reconnected["connected"] is True
        supervisor.call("cad_open_drawing", {"params": {"filename": str(tmp_path / "inspect.dwg")}})
        with pytest.raises(CadMcpError):
            supervisor.call("cad_save_drawing", {"params": {}}, write=True)
        assert (tmp_path / "simulated-actions").read_text(encoding="utf-8") == "applied\n"
    finally:
        supervisor.close()


def test_read_crash_requires_explicit_rebind_and_does_not_replay_failed_request(monkeypatch, tmp_path):
    configure_environment(monkeypatch, tmp_path)
    supervisor = fake_supervisor("isolated-read-crash-worker", tmp_path)
    try:
        with pytest.raises(CadMcpError) as caught:
            supervisor.call("cad_get_drawing_info", {"params": {}})
        assert caught.value.code == "WORKER_UNAVAILABLE"
        assert supervisor._write_unknown is False
        with pytest.raises(CadMcpError, match="重新绑定"):
            supervisor.call("cad_save_drawing", {"params": {}}, write=True)
        assert supervisor._process is None
        supervisor.call("cad_health", {"params": {"reconnect": True}})
        assert (tmp_path / "simulated-actions").read_text(encoding="utf-8") == "applied\n"
        bound = json.loads(supervisor.call("cad_open_drawing", {"params": {"filename": str(tmp_path / "read.dwg")}}))
        assert bound["ok"] is True
        assert supervisor._needs_bind is False
    finally:
        supervisor.close()


def test_queued_write_rechecks_unknown_state_after_gate_opens(monkeypatch, tmp_path):
    configure_environment(monkeypatch, tmp_path)
    supervisor = fake_supervisor("isolated-write-hang-worker", tmp_path, write_timeout=0.3)
    errors = []

    def write():
        try:
            supervisor.call("cad_draw_line", {"params": {"x1": 0, "y1": 0, "x2": 1, "y2": 1}}, write=True)
        except CadMcpError as exc:
            errors.append(exc.code)

    first = threading.Thread(target=write)
    second = threading.Thread(target=write)
    try:
        first.start()
        deadline = time.monotonic() + 10
        while not (tmp_path / "simulated-actions").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (tmp_path / "simulated-actions").exists()
        supervisor._write_timeout = 3
        second.start()
        first.join(timeout=5)
        second.join(timeout=5)
        assert not first.is_alive() and not second.is_alive()
        assert errors == ["EXECUTION_UNKNOWN", "EXECUTION_UNKNOWN"]
        assert (tmp_path / "simulated-actions").read_text(encoding="utf-8") == "applied\n"
    finally:
        supervisor.close()
        first.join(timeout=5)
        if second.ident is not None:
            second.join(timeout=5)


def test_async_tool_dispatch_keeps_health_responsive_and_schemas_unchanged(monkeypatch, tmp_path):
    configure_environment(monkeypatch, tmp_path)
    supervisor = fake_supervisor("isolated-hang-worker", tmp_path, read_timeout=0.5)
    mcp = FastMCP("isolation-test")
    mcp.add_tool(lambda params: "unused", name="cad_list_layers_detailed",
                 annotations={"readOnlyHint": True})
    mcp.add_tool(lambda params: "unused", name="cad_health", annotations={"readOnlyHint": True})
    registry = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    schemas = {name: tool.parameters for name, tool in registry.items()}
    monkeypatch.setattr(isolation, "ToolProcessSupervisor", lambda **_kwargs: supervisor)
    install_isolated_execution(mcp, registry, mode="acad")

    async def verify():
        pending = asyncio.create_task(registry["cad_list_layers_detailed"].run({"params": {}}))
        deadline = time.monotonic() + 10
        while not (tmp_path / "blocking-call-entered").exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert (tmp_path / "blocking-call-entered").exists()
        health = json.loads(await asyncio.wait_for(registry["cad_health"].run({"params": {}}), timeout=0.2))
        assert health["worker"]["busy"] is True
        failed = json.loads(await pending)
        assert failed["error_detail"]["code"] == "WORKER_UNAVAILABLE"

    try:
        asyncio.run(verify())
        assert {name: tool.parameters for name, tool in registry.items()} == schemas
    finally:
        supervisor.close()


def test_gateway_policy_uses_inner_tool_and_dry_run():
    registry = server._FULL_TOOL_REGISTRY
    assert _policy("cad_gateway_call", {"params": {"tool_name": "cad_draw_line", "arguments": {"params": {}}}}, registry) == ("cad_draw_line", True)
    assert _policy("cad_gateway_call", {"params": {"tool_name": "cad_apply_dwt_profile", "arguments": {"params": {"dry_run": True}}}}, registry) == ("cad_apply_dwt_profile", False)
    assert _policy("cad_open_drawing", {"params": {"filename": "test.dwg"}}, registry) == ("cad_open_drawing", False)


@pytest.mark.parametrize("ready", [False, True])
def test_bootstrap_and_blocked_input_pipe_are_bounded(monkeypatch, tmp_path, ready):
    configure_environment(monkeypatch, tmp_path)
    program = tmp_path / "no_reader.py"
    program.write_text(
        "import json, threading\n"
        + ("print(json.dumps({'kind':'ready','state':{'mode':'acad','document_open':False,'file':None}}), flush=True)\n" if ready else "")
        + "threading.Event().wait()\n", encoding="utf-8")
    supervisor = ToolProcessSupervisor(
        command=[sys.executable, "-B", str(program)], startup_timeout=0.3, read_timeout=0.3)
    try:
        with pytest.raises(CadMcpError) as caught:
            supervisor.call("cad_query_entities", {"params": {"large_payload": "x" * 1_000_000}})
        assert caught.value.code == "WORKER_UNAVAILABLE"
        assert caught.value.details["worker_cleanup"] == "completed"
        assert supervisor._process is None
    finally:
        supervisor.close()


def test_malformed_worker_state_does_not_poison_local_health(monkeypatch, tmp_path):
    configure_environment(monkeypatch, tmp_path)
    program = tmp_path / "invalid_state.py"
    program.write_text(
        "import json, sys\n"
        "print(json.dumps({'kind':'ready','state':{'mode':'acad','document_open':False,'file':None}}), flush=True)\n"
        "request=json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'kind':'result','request_id':request['request_id'],'result':'{}','state':None}), flush=True)\n",
        encoding="utf-8")
    supervisor = ToolProcessSupervisor(command=[sys.executable, "-B", str(program)])
    try:
        with pytest.raises(CadMcpError):
            supervisor.call("cad_get_drawing_info", {"params": {}})
        health = json.loads(supervisor.call("cad_health", {"params": {}}))
        assert health["mode"] == "acad"
        assert health["worker"]["rebind_required"] is True
    finally:
        supervisor.close()
