"""Exercise the production MCP entry point, schemas and persistent worker state."""

import asyncio
import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@pytest.mark.parametrize("toolset", ["all", "webcodex"])
def test_production_stdio_executes_tools_in_persistent_worker(tmp_path, toolset):
    environment = os.environ.copy()
    environment.update({"CAD_MODE": "dxf", "CAD_DRAWINGS_DIR": str(tmp_path / "drawings"),
                        "CAD_LOG_DIR": str(tmp_path / "logs"), "CAD_MCP_TOOLSET": toolset,
                        "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
    parameters = StdioServerParameters(command=sys.executable, args=["-B", "-m", "cad_mcp"], env=environment)

    async def verify():
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert len(tools.tools) == (114 if toolset == "all" else 3)

                async def call(name, params):
                    if toolset == "webcodex":
                        name, params = "cad_gateway_call", {"tool_name": name, "arguments": {"params": params}}
                    result = await session.call_tool(name, {"params": params})
                    assert result.isError is False
                    return json.loads(result.content[0].text)

                created = await call("cad_new_drawing", {"filename": "stdio-state.dxf"})
                assert created["ok"] is True
                drawn = await call("cad_draw_line", {"x1": 0, "y1": 0, "x2": 10, "y2": 0})
                assert drawn["ok"] is True
                queried = await call("cad_query_entities", {"limit": 10})
                assert queried["ok"] is True
                assert len(queried["entities"]) == 1
                handle = drawn["handle"]
                original = await call("cad_get_entity_properties", {"handle": handle})
                assert original["ok"] and original["properties"]["color"] == 256
                preview = await call("cad_edit_entity_properties", {
                    "handle": handle, "properties": {"color": 1, "end": [20, 5, 0]}})
                assert preview["ok"] and preview["dry_run"] and not preview["applied"]
                assert await call("cad_get_entity_properties", {"handle": handle}) == original
                edited = await call("cad_edit_entity_properties", {
                    "handle": handle, "properties": {"color": 1, "end": [20, 5, 0]}, "dry_run": False})
                assert edited["ok"] and edited["verified"] and edited["applied"]
                readback = await call("cad_get_entity_properties", {"handle": handle})
                assert readback["properties"]["end"] == [20.0, 5.0, 0.0]
                assert readback["properties"]["color"] == 1
                arc = await call("cad_draw_arc", {"cx": 40, "cy": 0, "radius": 3,
                                                   "start_angle": 0, "end_angle": 90})
                assert arc["ok"]
                changed_arc = await call("cad_edit_entity_properties", {
                    "handle": arc["handle"], "properties": {"center": [42, 2], "end_angle": 180}, "dry_run": False})
                assert changed_arc["ok"] and changed_arc["verified"], changed_arc
                assert changed_arc["after"]["properties"]["center"] == [42, 2]
                poly = await call("cad_draw_polyline", {"points": [[0, 10], [10, 10], [10, 20]]})
                assert poly["ok"]
                changed_poly = await call("cad_edit_entity_properties", {
                    "handle": poly["handle"], "properties": {"closed": True, "vertices": [
                        {"x": 1, "y": 10}, {"x": 15, "y": 10}, {"x": 15, "y": 20}]}, "dry_run": False})
                assert changed_poly["ok"] and changed_poly["verified"], changed_poly
                dimension = await call("cad_draw_diameter_dimension", {
                    "cx": 0, "cy": 0, "edge_x": 30, "edge_y": 40,
                    "text_x": 100, "text_y": 100})
                assert dimension["ok"] is True
                assert dimension["kind"] == "diameter"
                assert dimension["diameter"] == 100
                queried = await call("cad_query_entities", {"limit": 10})
                assert len(queried["entities"]) == 4
                health = await call("cad_health", {})
                assert health["document_open"] is True
                assert health["worker"]["generation"] == 1
                assert health["worker"]["write_result_unknown"] is False
                if toolset == "webcodex":
                    for invalid_arguments in ({}, {"params": None}, {"params": {"unexpected": 1}}):
                        result = await session.call_tool("cad_gateway_call", {"params": {
                            "tool_name": "cad_health", "arguments": invalid_arguments}})
                        assert result.isError is False
                        invalid = json.loads(result.content[0].text)
                        assert invalid["ok"] is False
                        assert invalid["error_detail"]["code"] == "INVALID_ARGUMENT"

    asyncio.run(asyncio.wait_for(verify(), timeout=30))
