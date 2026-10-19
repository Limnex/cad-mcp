#!/usr/bin/env python3
"""
MCP stdio 协议级测试：模拟 Claude Code 启动服务器并列出工具。

验证 initialize 与 tools/list 两个核心请求能正确响应。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def _command(use_installed: bool) -> list[str]:
    if not use_installed:
        return [sys.executable, "-m", "cad_mcp"]

    installed = shutil.which("cad-mcp")
    if installed is None:
        raise SystemExit("[FAIL] PATH 中未找到已安装的 cad-mcp 命令")
    return [installed]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--installed",
        action="store_true",
        help="测试 PATH 中安装的 cad-mcp 命令，而不是当前 Python 环境",
    )
    args = parser.parse_args()
    test_dir = PLUGIN_ROOT / ".cache" / "manual" / "stdio"
    env = os.environ.copy()
    env.update(
        CAD_MODE="dxf",
        CAD_DRAWINGS_DIR=str(test_dir / "drawings"),
        CAD_LOG_DIR=str(test_dir / "logs"),
    )
    p = subprocess.Popen(
        _command(args.installed),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, cwd=str(PLUGIN_ROOT), env=env,
    )
    try:
        # 1. initialize
        req = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05",
                       "capabilities": {},
                       "clientInfo": {"name": "test-client", "version": "1.0"}},
        })
        p.stdin.write((req + "\n").encode("utf-8"))
        p.stdin.flush()
        line = p.stdout.readline().decode("utf-8")
        resp = json.loads(line)
        server_name = resp.get("result", {}).get("serverInfo", {}).get("name")
        print(f"[PASS] initialize 响应，服务器名: {server_name}")

        # 2. initialized 通知 + tools/list
        p.stdin.write((json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode("utf-8"))
        p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n").encode("utf-8"))
        p.stdin.flush()
        line = p.stdout.readline().decode("utf-8")
        resp = json.loads(line)
        tools = resp.get("result", {}).get("tools", [])
        names = [t.get("name") for t in tools]
        print(f"[PASS] tools/list 返回 {len(tools)} 个工具")
        for n in names:
            print(f"       - {n}")

        expected = {
            "cad_new_drawing", "cad_open_drawing", "cad_save_drawing",
            "cad_get_drawing_info", "cad_draw_mline", "cad_trim",
            "cad_draw_rectangle", "cad_draw_circle",
            "cad_draw_line", "cad_draw_arc", "cad_draw_polyline",
            "cad_draw_hatch", "cad_draw_text", "cad_draw_dimension",
            "cad_draw_dimension_at",
            "cad_draw_angular_dimension", "cad_draw_radius_dimension",
            "cad_create_layer", "cad_set_layer_props", "cad_set_active_layer",
            "cad_create_or_update_textstyle", "cad_create_or_update_dimstyle",
            "cad_set_linetype_scale", "cad_set_linetype_settings",
            "cad_get_drafting_environment", "cad_get_drawing_settings",
            "cad_list_layers_detailed", "cad_list_textstyles",
            "cad_get_geometry_extents",
            "cad_evaluate_viewport_fit", "cad_query_entities",
            "cad_regen", "cad_health",
            "cad_list_entities", "cad_delete_entity", "cad_move_entity",
            "cad_copy_entity", "cad_rotate_entity", "cad_mirror_entity",
            "cad_make_block", "cad_insert_block", "cad_draw_title_block",
            "cad_array_rect", "cad_array_polar", "cad_draw_centerline",
            "cad_offset", "cad_copy_linear", "cad_explode", "cad_join_polyline",
            "cad_check_drawing", "cad_export_pdf", "cad_draw_table",
            "cad_load_drafting_profile", "cad_list_drafting_profiles",
            "cad_load_environment_rule_set", "cad_list_environment_rule_sets",
            "cad_validate_environment_model", "cad_preview_environment_drawing",
            "cad_apply_environment_drawing", "cad_list_managed_entities",
            "cad_save_as", "cad_clone_template_drawing",
            "cad_inspect_template_drawing", "cad_verify_template_manifest",
            "cad_load_template_manifest", "cad_evaluate_template_sheet_scales",
            "cad_create_layout", "cad_insert_template_sheet",
            "cad_fill_template_fields", "cad_create_viewport",
            "cad_configure_layout_plot", "cad_publish_layout_pdf",
            "cad_audit_block_definition",
            "cad_fill_closed_boundary_solid",
            "cad_draw_mtext",
            "cad_switch_mode", "cad_close_drawing", "cad_quit",
        }
        missing = expected - set(names)
        if missing:
            print(f"[FAIL] 缺少工具: {missing}")
            sys.exit(1)
        if len(names) != len(set(names)):
            print("[FAIL] 工具名存在重复注册")
            sys.exit(1)
        print(f"[PASS] {len(expected)} 个核心工具均存在，实际注册 {len(names)} 个且名称唯一")
    finally:
        p.kill()


if __name__ == "__main__":
    main()
