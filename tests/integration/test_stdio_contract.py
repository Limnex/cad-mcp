import asyncio
import json
import os
from pathlib import Path
import sys




PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = PLUGIN_ROOT / "src"


def test_stdio_initialize_and_tool_list(tmp_path):
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1.0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    env = os.environ.copy()
    env.update(
        {
            "CAD_MODE": "dxf",
            "CAD_MCP_TOOLSET": "all",
            "CAD_DRAWINGS_DIR": str(tmp_path / "drawings"),
            "CAD_LOG_DIR": str(tmp_path / "logs"),
            "PYTHONUTF8": "1",
            "PYTHONPATH": os.pathsep.join(
                part
                for part in (str(SOURCE_DIR), env.get("PYTHONPATH", ""))
                if part
            ),
        }
    )

    async def exchange():
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-B", "-m", "cad_mcp", cwd=PLUGIN_ROOT, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, limit=8*1024*1024,
            stderr=asyncio.subprocess.PIPE)
        stderr = asyncio.create_task(process.stderr.read())
        async def receive(expected_id):
            while True:
                line = await process.stdout.readline()
                assert line, "MCP exited before returning the expected response"
                item = json.loads(line.decode("utf-8"))
                if item.get("id") == expected_id:
                    return item
        try:
            process.stdin.write((json.dumps(requests[0])+"\n").encode("utf-8"))
            await process.stdin.drain()
            initialize = await receive(1)
            for request in requests[1:]:
                process.stdin.write((json.dumps(request)+"\n").encode("utf-8"))
            await process.stdin.drain()
            tool_list = await receive(2)
            # EOF must follow both responses, not cancel tools/list in flight.
            process.stdin.close()
            await process.wait()
            assert process.returncode == 0, (await stderr).decode("utf-8")
            return initialize, tool_list
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            await stderr

    initialize, tool_list = asyncio.run(asyncio.wait_for(exchange(), timeout=30))
    tools = {tool["name"]: tool for tool in tool_list["result"]["tools"]}
    names = set(tools)

    assert initialize["result"]["serverInfo"]["name"] == "cad_mcp"
    assert initialize["result"]["protocolVersion"] == "2024-11-05"
    assert len(names) == 111
    assert {
        "cad_health",
        "cad_extend",
        "cad_fillet",
        "cad_chamfer",
        "cad_preview_region",
        "cad_get_drawing_settings",
        "cad_set_drawing_settings",
        "cad_list_layers_detailed",
        "cad_list_textstyles",
        "cad_list_dimstyles",
        "cad_preview_dwt_profile",
        "cad_apply_dwt_profile",
        "cad_validate_dwt_profile",
        "cad_create_or_update_ctb",
        "cad_apply_environment_drawing",
        "cad_create_viewport",
        "cad_publish_layout_pdf",
        "cad_audit_block_definition",
        "cad_fill_closed_boundary_solid",
        "cad_switch_mode",
        "cad_chamfer",
    }.issubset(names)

    chamfer_schema = tools["cad_chamfer"]["inputSchema"]
    chamfer_properties = chamfer_schema["$defs"]["ChamferInput"]["properties"]
    assert chamfer_properties["method"]["enum"] == ["distance", "angle"]
    assert chamfer_properties["dry_run"]["default"] is True

    open_schema = tools["cad_open_drawing"]["inputSchema"]
    open_properties = open_schema["$defs"]["OpenDrawingInput"]["properties"]
    assert open_properties["filename"]["maxLength"] == 4096
    assert "绝对路径" in open_properties["filename"]["description"]

    make_block_schema = tools["cad_make_block"]["inputSchema"]
    make_block_properties = make_block_schema["$defs"]["MakeBlockInput"]["properties"]
    assert make_block_properties["annotative"]["type"] == "boolean"
    assert make_block_properties["annotative"]["default"] is False

    dimstyle_schema = tools["cad_create_or_update_dimstyle"]["inputSchema"]
    dimstyle_properties = dimstyle_schema["$defs"][
        "CreateOrUpdateDimStyleInput"
    ]["properties"]
    assert dimstyle_properties["oblique_stroke_size"]["default"] == 0.0
    assert dimstyle_properties["oblique_stroke_size"]["minimum"] == 0
    assert dimstyle_properties["linear_scale_factor"]["default"] == 1.0
    assert dimstyle_properties["linear_scale_factor"]["exclusiveMinimum"] == 0
    assert dimstyle_properties["linear_scale_factor"]["maximum"] == 1000000
    assert dimstyle_properties["precision"]["default"] == 2
    assert dimstyle_properties["suppress_trailing_zeros"]["default"] is False

    textstyle_schema = tools["cad_create_or_update_textstyle"]["inputSchema"]
    textstyle_properties = textstyle_schema["$defs"][
        "CreateOrUpdateTextStyleInput"
    ]["properties"]
    assert textstyle_properties["model_height"]["minimum"] == 0
    assert "exclusiveMinimum" not in textstyle_properties["model_height"]

    preview_schema = tools["cad_preview_region"]["inputSchema"]["$defs"]["PreviewRegionInput"]
    assert set(preview_schema["required"]) == {"x_min", "y_min", "x_max", "y_max"}
    assert preview_schema["properties"]["include_image"]["default"] is True
    assert preview_schema["properties"]["width"]["maximum"] == 2048
