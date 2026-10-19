"""Fault injection in disposable Python processes; never connects to AutoCAD."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from cad_mcp.core.isolation import ToolProcessSupervisor
from cad_mcp.core.runtime import CadMcpError


ROOT = Path(__file__).resolve().parents[2]
WORKER = Path(__file__).with_name("_runtime_fault_worker.py")


def worker_environment(tmp_path):
    environment = os.environ.copy()
    environment.update({
        "CAD_MODE": "dxf", "CAD_LOG_DIR": str(tmp_path / "logs"),
        "CAD_DRAWINGS_DIR": str(tmp_path / "drawings"),
        "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
    })
    environment.pop("CAD_MCP_RUN_ACAD_INTEGRATION", None)
    return environment


def run_scenario(scenario, tmp_path):
    completed = subprocess.run(
        [sys.executable, "-B", str(WORKER), scenario, str(tmp_path)],
        cwd=ROOT, env=worker_environment(tmp_path),
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    events = [json.loads(line) for line in completed.stdout.splitlines() if line]
    assert len(events) == 1, completed.stdout
    result = events[0]
    assert result["event"] == "result"
    assert result["real_cad_attempts"] == {}
    return result


def test_simulated_cad_service_crash_stops_writes_and_does_not_restart(tmp_path):
    result = run_scenario("crash", tmp_path)
    assert result["before"]["connected"] is True
    assert result["simulated_service_exit_code"] == 23
    assert result["after"]["connected"] is False
    assert result["after"]["document_state"] == "unavailable"
    assert result["after"]["error_id"].startswith("cad-")
    assert result["write"]["ok"] is False
    assert result["reconnect"]["ok"] is False
    assert result["reconnect"]["error_detail"]["code"] == "PRECONDITION_FAILED"


def test_large_block_graph_keeps_complete_counts_without_recursive_overcount(tmp_path):
    result = run_scenario("complete", tmp_path)
    assert result["response"]["ok"] is True
    assert result["graph_entities"] == 16896
    assert result["block_definitions"] == 256
    assert result["block_reference_entities"] == 1052
    assert result["counts_match"] is True
    assert result["yields"] > 0
    assert result["deadline_context_reset"] is True


@pytest.mark.parametrize("scenario", ["slow-layers", "slow-info", "slow-query", "busy"])
def test_large_slow_or_busy_scan_stops_without_partial_success(scenario, tmp_path):
    result = run_scenario(scenario, tmp_path)
    response = result["response"]
    assert response["ok"] is False
    assert response["error_detail"]["code"] == "BACKEND_BUSY"
    assert "扫描未完成" in response["error"]
    assert "layers" not in response
    assert "entities" not in response
    assert result["reads"].get("entity.Layer", 0) < result["graph_entities"]
    assert result["deadline_context_reset"] is True
    assert result["document_binding_preserved"] is True


def test_mid_scan_disconnect_stops_at_first_fault_instead_of_continuing(tmp_path):
    result = run_scenario("broken", tmp_path)
    assert result["response"]["ok"] is False
    assert result["response"]["error_detail"]["code"] == "COM_ERROR"
    assert result["reads"]["entity.Layer"] == 40
    assert result["after_fault_reads"] == 0
    assert result["deadline_context_reset"] is True


def test_single_nonreturning_call_is_stopped_by_production_supervisor(tmp_path, monkeypatch):
    for key, value in worker_environment(tmp_path).items():
        monkeypatch.setenv(key, value)
    supervisor = ToolProcessSupervisor(
        command=[sys.executable, "-B", str(WORKER), "isolated-hang-worker", str(tmp_path)],
        read_timeout=0.3,
    )
    try:
        with pytest.raises(CadMcpError) as caught:
            supervisor.call("cad_list_layers_detailed", {"params": {}})
        assert caught.value.code == "WORKER_UNAVAILABLE"
        assert caught.value.details["worker_cleanup"] == "completed"
        assert (tmp_path / "blocking-call-entered").read_text(encoding="utf-8") == "entered"
        assert supervisor._process is None
        health = json.loads(supervisor.call("cad_health", {"params": {"reconnect": False}}))
        assert health["ok"] is True
        assert health["worker"]["write_result_unknown"] is False
        assert health["worker"]["rebind_required"] is True
    finally:
        supervisor.close()
