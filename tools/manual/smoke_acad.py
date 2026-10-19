#!/usr/bin/env python3
"""
AutoCAD COM 后端测试：连接本机 AutoCAD 实际画图并验证。

注意：需要本机装有 AutoCAD；测试会打开 AutoCAD 窗口并操作。
测试图纸保存到临时目录，不污染图纸目录。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

if os.environ.get("CAD_RUN_ACAD_TESTS") != "1":
    print("SKIP: 实机 AutoCAD 测试未启用；如需执行，请设置 CAD_RUN_ACAD_TESTS=1")
    raise SystemExit(0)

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = PLUGIN_ROOT / "src"
sys.path.insert(0, str(SOURCE_DIR))

if sys.stdout.encoding and "utf" not in sys.stdout.encoding.lower():
    sys.stdout.reconfigure(encoding="utf-8")

MANUAL_DIR = PLUGIN_ROOT / ".cache" / "manual"
MANUAL_DIR.mkdir(parents=True, exist_ok=True)
TEST_DIR = Path(tempfile.mkdtemp(prefix="acad-", dir=MANUAL_DIR))
os.environ["CAD_LOG_DIR"] = str(TEST_DIR / "logs")

# 让 MCP 层的图纸目录也指向临时目录，避免污染真实图纸库
os.environ["CAD_DRAWINGS_DIR"] = str(TEST_DIR)

from cad_mcp.drawing.autocad import AcadBackend  # noqa: E402
from cad_mcp import server as cad_mcp_server  # noqa: E402
from cad_mcp.server import (  # noqa: E402
    NewDrawingInput, DrawMLineInput, DrawCircleInput, CopyEntityInput,
    SwitchModeInput,
)
PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name} {detail}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


print("=== AutoCAD COM 后端测试 ===")
cad = None
try:
    cad = AcadBackend(TEST_DIR)
    msg = cad.new_drawing("测试-卧室.dwg")
    check("连接并新建文档", msg["mode"] == "acad" and msg["template_path"].lower().endswith(".dwt"), str(msg))
    cad.create_or_update_textstyle("ENG_TEXT", 700)
    cad.create_or_update_dimstyle(
        "ENG_DIM", 200, 3.5, 2.5, 0.8, 0, True, "ENG_TEXT", True)
    cad.set_linetype_settings(200, 1, 0, 1, True)
    env = cad.get_drafting_environment()
    check("DIMSTYLE 全局比例", env["dimstyle"]["overall_scale"] == 200, str(env["dimstyle"]))
    check("当前文字样式字高", env["textstyle"]["model_height"] == 700, str(env["textstyle"]))

    cad.draw_mline([(0, 0), (3600, 0)], scale=240)
    cad.draw_mline([(3600, 0), (3600, 3000)], scale=240)
    cad.draw_mline([(3600, 3000), (0, 3000)], scale=240)
    cad.draw_mline([(0, 3000), (0, 0)], scale=240)
    check("画四条 MLINE", cad.msp.Count >= 4, f"图元数={cad.msp.Count}")

    cad.draw_rectangle(400, 200, 2400, 1400, "FURNITURE")
    cad.draw_circle(3000, 2400, 400, "FURNITURE")
    cad.draw_text("卧室", 1500, 1650, 250)
    r = cad.draw_line(0, 0, 100, 0)
    check("画线", r["type"] == "LINE", str(r))
    r = cad.draw_arc(100, 100, 50, 0, 180)
    check("圆弧", r["type"] == "ARC", str(r))
    r = cad.draw_polyline([(0, 0), (300, 0), (300, 200)], close=True)
    check("多段线", r["type"] == "LWPOLYLINE", str(r))
    r = cad.draw_text("旋转文字", 200, 200, 200, rotation=45)
    check("文字旋转", r["rotation"] == 45, str(r))
    r = cad.draw_dimension(0, 0, 0, 1500, direction="vertical")
    check("垂直标注", r["value"] == 1500.0, str(r))
    try:
        cad.draw_hatch([(0, 0), (100, 0), (100, 100)], scale=5)
        check("hatch降级", False, "COM 后端填充不应成功")
    except NotImplementedError:
        check("hatch降级", True, "COM 后端明确返回不支持")

    # P0-2 线型线宽
    msg = cad.create_layer("WATER", color=8, linetype="DASHED", lineweight=50)
    check("创建图层(线型线宽)", "DASHED" in msg and "50/100mm" in msg, msg)
    lay = cad._get_layer("WATER")
    check("线型生效", lay.Linetype.upper() == "DASHED", lay.Linetype)
    check("线宽生效", lay.Lineweight == 50, str(lay.Lineweight))
    msg = cad.set_layer_props("WALL", linetype="CENTER", lineweight=25)
    check("修改图层属性", "CENTER" in msg, msg)

    # P0-1 中文字体
    try:
        s = cad.doc.TextStyles.Item("Standard")
        check("中文字体", s.fontFile.lower() == "gbenor.shx", s.fontFile)
    except Exception:
        check("中文字体", False, "读取样式失败")

    # P1-4 连接健康检查
    cad._ensure_connected()
    check("连接健康检查", True, "OK")

    r = cad.draw_dimension(0, 0, 3600, 0)
    check("尺寸标注", r["value"] == 3600.0, f"value={r['value']}")

    info = cad.get_info()
    check("get_info", "DWG" in info and "WALL" in info, info.replace("\n", " | "))

    n_before = cad.msp.Count
    rows = cad.list_entities()
    check("列出图元", "AcDb" in rows, rows.split("\n")[0])
    first = rows.split("\n")[0].split("|")[0].strip()
    msg = cad.move_entity(first, 100, 100)
    check("移动图元", "已移动" in msg, msg)
    msg = cad.delete_entity(first)
    check("删除图元", "已删除" in msg, msg)
    check("删除后数量减1", cad.msp.Count == n_before - 1, f"{n_before}->{cad.msp.Count}")

    # P2：编辑 / 标注 / 图块 / 图框
    r = cad.draw_circle(5000, 1000, 300)
    h_circ = r["handle"]
    r = cad.copy_entity(h_circ, 600, 0)
    check("COM 复制图元", "copied_from" in r, str(r))
    msg = cad.rotate_entity(h_circ, 5000, 1000, 45)
    check("COM 旋转图元", "已旋转" in msg, msg)
    msg = cad.mirror_entity(h_circ, 0, 500, 1000, 500)
    check("COM 镜像图元", "已镜像" in msg, msg)
    r = cad.draw_dimension(0, 0, 300, 400, offset=200, direction="aligned")
    check("COM 对齐标注", r["direction"] == "aligned" and abs(r["value"] - 500.0) < 1, f"value={r['value']}")
    r = cad.draw_angular_dimension(2000, 0, 3000, 0, 2000, 1000, 2600, 400)
    check("COM 角度标注", r["kind"] == "angular", str(r))
    r = cad.draw_radius_dimension(8000, 0, 8500, 0)
    check("COM 半径标注", r["kind"] == "radius" and r["radius"] == 500.0, str(r))
    msg = cad.make_block("PUMP", [h_circ], 0, 0)
    check("COM 打包图块", "已创建图块 PUMP" in msg, msg)
    r = cad.insert_block("PUMP", 9000, 9000, rotation=90, scale=2)
    check("COM 插入图块", r["type"] == "INSERT", str(r))
    msg = cad.draw_title_block(420, 297, title="泵房", project="污水厂",
                               design="张三", check="李四", sheet_no="S-01")
    check("COM 图框标题栏", "已画图框 420×297" in msg, msg)

    # P3：批量布置 / 中心线 / 检查 / 表格 / 图框增强 / PDF
    r = cad.draw_circle(20000, 10000, 400)
    h_p = r["handle"]
    msg = cad.array_rect([h_p], rows=2, cols=3, row_spacing=2000, col_spacing=3000)
    check("COM 矩形阵列", "2 行 × 3 列" in msg and "新增 5 个副本" in msg, msg)
    n_c = sum(1 for i in range(cad.msp.Count) if cad.msp.Item(i).EntityName == "AcDbCircle")
    check("COM 阵列后圆数", n_c >= 6, f"圆×{n_c}")
    msg = cad.array_polar([h_p], 20000, 10000, count=4)
    check("COM 环形阵列", "均布 4 个" in msg and "新增 3 个副本" in msg, msg)
    r = cad.draw_line(30000, 0, 40000, 0)
    h_l = r["handle"]
    msg = cad.copy_linear([h_l], dx=0, dy=-500, copies=3)
    check("COM 线性复制", "× 3 份" in msg and "新增 3 个副本" in msg, msg)
    offset_result = cad.offset(h_l, 500)
    check("COM 原生 OFFSET", offset_result["source_retained"] and len(offset_result["entities"]) == 1,
          str(offset_result))
    r = cad.draw_centerline(1000, 1000, 5000, 3000)
    check("COM 中心线", r["type"] == "CENTERLINE" and len(r["handles"]) == 2, str(r))
    center_lay = cad._get_layer("CENTER")
    check("COM 中心线图层", center_lay.color == 1 and center_lay.Linetype.upper() == "CENTER",
          f"颜色={center_lay.color} 线型={center_lay.Linetype}")
    msg = cad.check_drawing(tolerance=10)
    check("COM 图面检查", "图面检查通过" in msg or "发现" in msg, msg[:60])
    r = cad.draw_table(60000, 0, [30, 100, 150, 60], 40,
                       ["序号", "名称", "规格", "数量"],
                       [["1", "离心泵", "IS100-65", "2台"]], title="设备材料表")
    check("COM 表格", r["type"] == "TABLE" and r["cols"] == 4, str(r))
    msg = cad.draw_title_block(0, 0, size="A3", title="沉淀池", ratio="1:50")
    check("COM 图框 A3 幅面", "已画图框 420×297" in msg, msg)
    try:
        msg = cad.export_pdf("测试导出.pdf")
        pdf_ok = "已导出 PDF" in msg
        if pdf_ok:
            pdf_path = Path(msg.split("已导出 PDF: ", 1)[1].split("（")[0].strip())
            pdf_ok = pdf_path.exists() and pdf_path.suffix.lower() == ".pdf"
        check("COM 导出 PDF", pdf_ok, msg)
    except Exception as e:
        check("COM 导出 PDF", False, f"{type(e).__name__}: {e}")

    # 制图环境不随图形范围自动变化
    cad.draw_rectangle(50000, 0, 100000, 30000)   # 画个大图
    r = cad.draw_text("大图字", 50000, 500)
    check("COM 大图仍继承文字样式", r["height"] == 700, f"字高={r['height']}")
    r = cad.draw_text("显式字高", 50000, 1000, height=200)
    check("COM 显式字高仍生效", r["height"] == 200, f"字高={r['height']}")
    # 标注继承当前 DIMSTYLE，实体不再覆盖 TextHeight/TextPosition
    r = cad.draw_dimension_at(50000, 0, 100000, 0, 50000, -3000, "horizontal")
    d = cad._find(r["handle"])
    check("COM 标注继承当前 DIMSTYLE", d.StyleName == "ENG_DIM", f"StyleName={d.StyleName}")
    # 线型比例仅接受显式值
    msg = cad.set_linetype_scale(30)
    check("COM 显式线型比例", "30" in msg and cad.doc.GetVariable("LTSCALE") == 30, msg)

    msg = cad.save()
    check("保存 DWG", "已保存" in msg, msg)
    fname = TEST_DIR / "测试-卧室.dwg"
    check("DWG 文件已生成", fname.exists(), str(fname))

except Exception as e:
    check("整体测试", False, f"{type(e).__name__}: {e}")
finally:
    if cad is not None:
        try:
            print(f"  [清理] {cad.close_document()}")
        except Exception:
            pass

print("=== MCP 层端到端：双模式切换 ===")
try:
    r = cad_mcp_server.cad_switch_mode(SwitchModeInput(mode="acad"))
    check("MCP 切换 acad 模式", '"acad"' in r, r[:60])
    r = cad_mcp_server.cad_new_drawing(NewDrawingInput(filename="测试-mcp-acad.dwg"))
    check("MCP acad 新建", '"template_source": "cad_qnew"' in r, r[:160])
    r = cad_mcp_server.cad_draw_mline(DrawMLineInput(points=[[0, 0], [1500, 0]], scale=240))
    check("MCP acad 画 MLINE", '"MLINE"' in r, r[:100])
    r = cad_mcp_server.cad_draw_circle(DrawCircleInput(cx=500, cy=500, radius=100))
    h_circle = json.loads(r)["handle"]
    r = cad_mcp_server.cad_copy_entity(CopyEntityInput(handle=h_circle, dx=200, dy=0))
    check("MCP acad 复制", '"ok": true' in r and '"copied_from"' in r, r[:80])
    r = cad_mcp_server.cad_save_drawing(cad_mcp_server.SaveDrawingInput())
    check("MCP acad 保存", "已保存" in r, r[:60])
    # 清理：关闭 acad 后端新建的测试文档，再切回 dxf
    try:
        print(f"  [清理] {cad_mcp_server._backend.close_document()}")
    except Exception:
        pass
    r = cad_mcp_server.cad_switch_mode(SwitchModeInput(mode="dxf"))
    check("MCP 切回 dxf 模式", '"dxf"' in r, r[:60])
    r = cad_mcp_server.cad_new_drawing(NewDrawingInput(filename="测试-mcp-dxf.dxf"))
    check("MCP dxf 新建", "已保存" in r, r[:60])
except Exception as e:
    check("MCP 端到端", False, f"{type(e).__name__}: {e}")

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
