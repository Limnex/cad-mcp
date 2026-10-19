import asyncio
import json

from cad_mcp import server


def test_gateway_describe_and_call_use_original_registry():
    described = json.loads(asyncio.run(server._gateway_describe(
        server.GatewayDescribeParams(tool_name="cad_health")
    )))
    assert described["ok"] is True
    assert described["tool"]["name"] == "cad_health"
    assert "inputSchema" in described["tool"]

    called = json.loads(asyncio.run(server._gateway_call(
        server.GatewayCallParams(
            tool_name="cad_health",
            arguments={"params": {"reconnect": False}},
        )
    )))
    assert called["ok"] is True
    assert called["mode"] in {"dxf", "acad"}


def test_invalid_toolset_fails_closed(monkeypatch):
    monkeypatch.setenv("CAD_MCP_TOOLSET", "unknown")
    try:
        server._configure_tool_exposure()
    except ValueError as exc:
        assert "CAD_MCP_TOOLSET" in str(exc)
    else:
        raise AssertionError("invalid toolset must fail closed")
