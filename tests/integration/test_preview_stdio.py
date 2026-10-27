"""Real MCP image blocks through the production worker and both toolsets."""

import asyncio
import base64
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ImageContent
import pytest


@pytest.mark.parametrize("toolset", ["all", "webcodex"])
def test_preview_image_and_path_cross_production_stdio(tmp_path, toolset):
    environment = os.environ.copy()
    environment.update({"CAD_MODE": "dxf", "CAD_DRAWINGS_DIR": str(tmp_path / "drawings"),
                        "CAD_LOG_DIR": str(tmp_path / "custom-log"), "CAD_MCP_TOOLSET": toolset,
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
                    return result, json.loads(result.content[0].text)

                _, created = await call("cad_new_drawing", {"filename": "preview.dxf"})
                assert created["ok"]
                _, drawn = await call("cad_draw_line", {"x1": -10, "y1": 5, "x2": 20, "y2": 5})
                assert drawn["ok"]
                region = {"x_min": 0, "y_min": 0, "x_max": 10, "y_max": 10, "width": 256, "height": 256}
                response, payload = await call("cad_preview_region", region)
                assert payload["ok"] and payload["image_included"], payload
                path = Path(payload["image_path"])
                assert path.is_absolute() and path.parent.parent == tmp_path / "images"
                assert isinstance(response.content[1], ImageContent)
                assert response.content[1].mimeType == "image/png"
                assert base64.b64decode(response.content[1].data) == path.read_bytes()
                response2, fallback = await call("cad_preview_region", region | {"include_image": False})
                assert fallback["ok"] and not fallback["image_included"]
                assert len(response2.content) == 1
                assert fallback["image_path"] != str(path) and path.exists()
                assert Path(fallback["image_path"]).exists()
                _, entities = await call("cad_query_entities", {"limit": 10})
                assert len(entities["entities"]) == 1
                _, health = await call("cad_health", {})
                assert health["document_open"]
                assert health["tool_count"] == (116 if toolset == "all" else 3)
                assert health["worker"]["generation"] == 1
                assert not health["worker"]["write_result_unknown"]

    asyncio.run(asyncio.wait_for(verify(), timeout=60))
