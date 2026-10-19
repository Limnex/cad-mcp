#!/usr/bin/env python3
"""新制图环境能力的无 AutoCAD 回归测试（DXF 后端 + MCP 工具层）。"""
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = PLUGIN_ROOT / "src"
TEST_DIR = PLUGIN_ROOT / ".cache" / "manual" / "new-capabilities"
os.environ["CAD_LOG_DIR"] = str(TEST_DIR / "logs")
shutil.rmtree(TEST_DIR, ignore_errors=True)
os.environ["CAD_DRAWINGS_DIR"] = str(TEST_DIR)
sys.path.insert(0, str(SOURCE_DIR))

from cad_mcp.drawing.document import CadDocument  # noqa: E402
from cad_mcp.drawing.autocad import AcadBackend  # noqa: E402
from cad_mcp import server as cad_mcp_server  # noqa: E402


def main() -> None:
    # 模板解析无需启动 AutoCAD：显式路径优先，留空时读取 QNewTemplateFile。
    fake_template = TEST_DIR / "test-default.dwt"
    fake_template.parent.mkdir(parents=True, exist_ok=True)
    fake_template.write_bytes(b"test template placeholder")
    fake_acad = SimpleNamespace(Preferences=SimpleNamespace(Files=SimpleNamespace(
        QNewTemplateFile=str(fake_template), TemplateDwgPath=str(TEST_DIR))))
    resolver = AcadBackend(TEST_DIR)
    resolved, source = resolver._resolve_new_drawing_template(fake_acad)
    assert resolved == fake_template.resolve() and source == "cad_qnew"
    resolved, source = resolver._resolve_new_drawing_template(
        fake_acad, str(fake_template))
    assert resolved == fake_template.resolve() and source == "explicit"
    try:
        resolver._resolve_new_drawing_template(fake_acad, "relative-template.dwt")
        raise AssertionError("显式模板路径必须为绝对路径")
    except ValueError as exc:
        assert "完整绝对路径" in str(exc)
    empty_qnew = SimpleNamespace(Preferences=SimpleNamespace(Files=SimpleNamespace(
        QNewTemplateFile="", TemplateDwgPath=str(TEST_DIR))))
    try:
        resolver._resolve_new_drawing_template(empty_qnew)
        raise AssertionError("QNEW 未配置时应明确失败")
    except RuntimeError as exc:
        assert "尚未设置快速新建默认模板" in str(exc)

    cad = CadDocument(TEST_DIR)
    cad.new_drawing("drafting-env.dxf")
    try:
        cad.new_drawing("dwt-not-supported.dxf", str(fake_template))
        raise AssertionError("dxf 模式不应接受 DWT 模板")
    except ValueError as exc:
        assert "仅适用于 acad 模式" in str(exc)
    text = cad.create_or_update_textstyle(
        "ENG_TEXT", 700, "gbenor.shx", "gbcbig.shx", 1.0, True)
    assert text["model_height"] == 700
    dim = cad.create_or_update_dimstyle(
        "ENG_DIM", 200, 3.5, 2.5, 0.8, 0, True, "ENG_TEXT", True)
    assert dim["overall_scale"] == 200
    lt = cad.set_linetype_settings(200, 1, 0, 1, True)
    assert lt["ltscale"] == 200

    cad.draw_rectangle(0, 0, 135000, 84000)
    text_entity = cad.draw_text("继承样式", 1000, 1000)
    assert text_entity["height"] == 700
    dimension = cad.draw_dimension_at(
        0, 0, 135000, 0, 0, -3000, "horizontal", "DIM")
    assert dimension["value"] == 135000
    assert dimension["dimstyle"] == "ENG_DIM"

    env = cad.get_drafting_environment()
    assert env["textstyle"]["name"] == "ENG_TEXT"
    assert env["dimstyle"]["overall_scale"] == 200
    assert env["linetype"]["msltscale"] is None
    assert "msltscale" in env["linetype"]["unsupported"]

    ext = cad.get_geometry_extents()
    assert not ext["empty"] and ext["width"] >= 135000
    page = cad.query_entities(offset=0, limit=2)
    assert len(page["entities"]) == 2 and page["next_offset"] is not None
    cad.save()

    cad_mcp_server._backend = cad
    fit = json.loads(cad_mcp_server.cad_evaluate_viewport_fit(
        cad_mcp_server.EvaluateViewportFitInput(
            paper_width=841, paper_height=594,
            margin_left=25, margin_right=10, margin_top=10, margin_bottom=10,
            scale_denominator=200)))
    assert fit["ok"]
    assert fit["evaluation"]["fits"] == (
        fit["evaluation"]["scale_denominator"] >= fit["minimum_scale_denominator"])

    names = {tool.name for tool in cad_mcp_server.mcp._tool_manager.list_tools()}
    expected = {
        "cad_create_or_update_textstyle", "cad_create_or_update_dimstyle",
        "cad_set_linetype_settings", "cad_get_drafting_environment",
        "cad_get_geometry_extents", "cad_evaluate_viewport_fit",
        "cad_draw_dimension_at", "cad_query_entities", "cad_regen", "cad_health",
        "cad_draw_mline", "cad_explode", "cad_join_polyline",
        "cad_offset", "cad_copy_linear",
    }
    assert expected <= names, expected - names
    print("PASS: 新制图环境能力与 MCP 工具注册测试通过")


if __name__ == "__main__":
    main()
