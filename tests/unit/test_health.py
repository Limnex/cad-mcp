import json

from cad_mcp.drawing.autocad import AcadBackend
from cad_mcp.drawing.document import CadDocument
from cad_mcp import server as server


def test_dxf_health_reports_runtime_and_capabilities(tmp_path):
    status = CadDocument(tmp_path).health()

    assert status["mode"] == "dxf"
    assert status["connected"] is True
    assert status["document_state"] == "closed"
    assert status["server"]["name"] == "cad_mcp"
    assert status["server"]["version"] == "1.5.3"
    assert status["python"]["version"]
    assert status["capabilities"]["native_format"] == "dxf"
    assert status["capabilities"]["transaction"] == "memory_snapshot"
    assert "recent_error_id" in status


def test_acad_health_without_reconnect_does_not_connect(monkeypatch, tmp_path):
    backend = AcadBackend(tmp_path)

    def unexpected_connect():
        raise AssertionError("health(reconnect=False) attempted to connect")

    monkeypatch.setattr(backend, "_ensure_connected", unexpected_connect)

    status = backend.health(reconnect=False)

    assert status["mode"] == "acad"
    assert status["connected"] is False
    assert status["document_state"] == "closed"
    assert status["capabilities"]["native_format"] == "dwg"
    assert status["capabilities"]["transaction"] == "undo_group"


def test_acad_health_reports_stale_connection_as_unavailable(tmp_path):
    backend = AcadBackend(tmp_path)

    class BrokenDocuments:
        @property
        def Count(self):
            raise RuntimeError("RPC 服务器不可用")

    class StaleAcad:
        Documents = BrokenDocuments()

    backend.acad = StaleAcad()

    status = backend.health(reconnect=False)

    assert status["connected"] is False
    assert status["document_state"] == "unavailable"
    assert status["is_quiescent"] is None
    assert "RPC 服务器不可用" in status["error"]
    assert status["error_id"].startswith("cad-")


def test_cad_health_tool_reports_registered_tool_count(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_backend", CadDocument(tmp_path))

    result = json.loads(server.cad_health(server.HealthInput(reconnect=False)))

    assert result["ok"] is True
    assert result["tool_count"] == 104
    assert result["server"]["version"] == "1.5.3"
