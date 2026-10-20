"""Strict semantics through the production supervisor and persistent worker."""

import asyncio
import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@pytest.mark.parametrize("toolset", ["all", "webcodex"])
def test_strict_semantics_in_production_worker(tmp_path, toolset):
    environment = os.environ.copy()
    environment.update({
        "CAD_MODE": "dxf", "CAD_SEMANTIC_POLICY": "strict",
        "CAD_DRAWINGS_DIR": str(tmp_path / "drawings"),
        "CAD_LOG_DIR": str(tmp_path / "logs"), "CAD_MCP_TOOLSET": toolset,
        "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
    parameters = StdioServerParameters(command=sys.executable, args=["-B", "-m", "cad_mcp"], env=environment)

    async def verify():
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()

                async def call(name, params):
                    if toolset == "webcodex":
                        name, params = "cad_gateway_call", {"tool_name": name, "arguments": {"params": params}}
                    result = await session.call_tool(name, {"params": params})
                    assert result.isError is False
                    return json.loads(result.content[0].text)

                assert (await call("cad_new_drawing", {"filename": "strict.dxf"}))["ok"]
                missing = await call("cad_draw_circle", {"cx": 0, "cy": 0, "radius": 6000})
                assert missing["error_detail"]["code"] == "SEMANTIC_REQUIRED"
                assert (await call("cad_query_entities", {"limit": 10}))["entities"] == []
                obj = {"project_id": "P1", "object_id": "T1", "name": "二沉池 01",
                       "object_type": "secondary_clarifier", "source": "user_design", "status": "confirmed"}
                assert (await call("cad_register_semantic_object", {"object": obj}))["ok"]
                drawn = await call("cad_draw_circle", {"cx": 0, "cy": 0, "radius": 6000,
                    "semantic": {"project_id": "P1", "object_id": "T1", "role": "池体外轮廓"}})
                assert drawn["ok"], drawn
                meaning = await call("cad_get_entity_semantics", {"handle": drawn["handle"]})
                assert meaning["entities"][0]["object"] == {**obj, "parent_id": None}
                assert meaning["entities"][0]["status"] == "registered"
                assert (await call("cad_save_drawing", {}))["ok"]
                assert (await call("cad_close_drawing", {"save": False}))["ok"]
                assert (await call("cad_open_drawing", {"filename": "strict.dxf"}))["ok"]
                report = await call("cad_check_semantics", {})
                assert report["count"] == 1
                assert report["entities"][0]["object"]["object_type"] == "secondary_clarifier"
                assert report["entities"][0]["status"] == "registered"
                health = await call("cad_health", {"reconnect": False})
                assert health["semantic_policy"] == "strict"
                assert health["worker"]["generation"] == 1

    asyncio.run(asyncio.wait_for(verify(), timeout=45))
