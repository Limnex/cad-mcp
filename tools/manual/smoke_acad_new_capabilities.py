#!/usr/bin/env python3
"""AutoCAD 原生模式的新制图环境小范围冒烟测试。"""
import os
import shutil
import sys
from pathlib import Path

if os.environ.get("CAD_RUN_ACAD_TESTS") != "1":
    print("SKIP: 实机 AutoCAD 测试未启用；如需执行，请设置 CAD_RUN_ACAD_TESTS=1")
    raise SystemExit(0)

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = PLUGIN_ROOT / "src"
TEST_DIR = PLUGIN_ROOT / ".cache" / "manual" / "acad-new"
os.environ["CAD_LOG_DIR"] = str(TEST_DIR / "logs")
shutil.rmtree(TEST_DIR, ignore_errors=True)
TEST_DIR.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(SOURCE_DIR))

from cad_mcp.drawing.autocad import AcadBackend  # noqa: E402
from cad_mcp.server import _AcadLockProxy  # noqa: E402


def main() -> None:
    cad = _AcadLockProxy(AcadBackend(TEST_DIR))
    opened = False
    try:
        created = cad.new_drawing("drafting-env-smoke.dwg")
        opened = True
        assert created["mode"] == "acad", created
        assert created["template_source"] == "cad_qnew", created
        assert Path(created["template_path"]).is_file(), created
        assert Path(created["template_path"]).suffix.lower() == ".dwt", created
        cad.create_or_update_textstyle("ENG_TEXT", 700)
        cad.create_or_update_dimstyle(
            "ENG_DIM", 200, 3.5, 2.5, 0.8, 0, True, "ENG_TEXT", True)
        cad.set_linetype_settings(200, 1, 0, 1, True)

        env = cad.get_drafting_environment()
        assert env["dimstyle"]["name"] == "ENG_DIM", env
        assert env["dimstyle"]["overall_scale"] == 200, env
        assert env["textstyle"]["name"] == "ENG_TEXT", env
        assert env["textstyle"]["model_height"] == 700, env
        assert env["linetype"]["ltscale"] == 200, env

        rect = cad.draw_rectangle(0, 0, 10000, 5000)
        assert cad._find(rect["handle"]).Closed, rect
        text = cad.draw_text("继承文字样式", 1000, 1000)
        assert text["height"] == 700, text
        dim = cad.draw_dimension_at(
            0, 0, 10000, 0, 0, -2000, "horizontal", "DIM")
        assert dim["dimstyle"] == "ENG_DIM", dim

        cad.array_rect([rect["handle"]], 2, 3, 7000, 12000)
        cad.copy_linear([rect["handle"]], 0, 7000, 2)
        offset_result = cad.offset(rect["handle"], 500)
        assert offset_result["source_retained"] and offset_result["entities"], offset_result

        lines = [
            cad.draw_line(50000, 0, 51000, 0)["handle"],
            cad.draw_line(51000, 1000, 51000, 0)["handle"],
            cad.draw_line(50000, 1000, 51000, 1000)["handle"],
            cad.draw_line(50000, 0, 50000, 1000)["handle"],
        ]
        joined = cad.join_polyline([lines[2], lines[0], lines[3], lines[1]], True, 0.01)
        assert joined["closed"] and cad._find(joined["handle"]).Closed, joined
        mline = cad.draw_mline([(55000, 0), (56000, 0)], scale=200)
        exploded = cad.explode(mline["handle"])
        assert exploded["deleted_source"] and len(exploded["entities"]) >= 2, exploded
        ext = cad.get_geometry_extents()
        assert not ext["empty"] and ext["entity_count"] >= 9, ext
        page = cad.query_entities(offset=0, limit=5)
        assert len(page["entities"]) == 5, page
        cad.regen()
        health = cad.health(False)
        assert health["connected"] and health["is_quiescent"], health
        cad.save()
        print("PASS: AutoCAD 原生制图环境冒烟测试通过")
    finally:
        if opened:
            try:
                cad.close_drawing(True)
            except Exception as exc:
                print(f"WARN: 测试图纸关闭失败: {exc}")


if __name__ == "__main__":
    main()
