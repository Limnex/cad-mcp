import json
import os
from pathlib import Path
import subprocess
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
    payload = "".join(json.dumps(item) + "\n" for item in requests)
    env = os.environ.copy()
    env.update(
        {
            "CAD_MODE": "dxf",
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

    completed = subprocess.run(
        [sys.executable, "-m", "cad_mcp"],
        cwd=PLUGIN_ROOT,
        env=env,
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    responses = [json.loads(line) for line in completed.stdout.splitlines() if line]
    initialize = next(item for item in responses if item.get("id") == 1)
    tool_list = next(item for item in responses if item.get("id") == 2)
    tools = {tool["name"]: tool for tool in tool_list["result"]["tools"]}
    names = set(tools)

    assert initialize["result"]["serverInfo"]["name"] == "cad_mcp"
    assert len(names) == 101
    assert {
        "cad_health",
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
    }.issubset(names)

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
