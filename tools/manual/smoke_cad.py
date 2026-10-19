#!/usr/bin/env python3
"""
CAD MCP 功能测试：
1. 核心绘图库 cad_drawing 全功能
2. MCP 工具层（FastMCP 工具注册与调用）
3. 生成示例平面图到用户图纸目录
"""
import json
import os
import shutil
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = PLUGIN_ROOT / "src"
TEST_DIR = PLUGIN_ROOT / ".cache" / "manual" / "cad"
os.environ["CAD_LOG_DIR"] = str(TEST_DIR / "logs")
USER_CAD_DIR = Path(os.environ.get("CAD_DEMO_DIR", str(TEST_DIR / "demo")))

# 每次运行前清理测试目录，保证可重复执行
shutil.rmtree(TEST_DIR, ignore_errors=True)

# 用测试目录隔离，避免污染用户图纸目录
os.environ["CAD_DRAWINGS_DIR"] = str(TEST_DIR)
sys.path.insert(0, str(SOURCE_DIR))

if sys.stdout.encoding and "utf" not in sys.stdout.encoding.lower():
    sys.stdout.reconfigure(encoding="utf-8")

from cad_mcp.drawing.document import CadDocument  # noqa: E402
from cad_mcp.core.geometry import join_polyline_parts, reverse_polyline_part  # noqa: E402
from cad_mcp import server as cad_mcp_server  # noqa: E402
from cad_mcp.server import (  # noqa: E402
    NewDrawingInput, DrawMLineInput, TrimInput,
    DrawRectangleInput, DrawCircleInput, DrawLineInput, DrawArcInput,
    DrawPolylineInput, DrawHatchInput, DrawTextInput, DrawDimensionInput,
    DrawAngularDimensionInput, DrawRadiusDimensionInput,
    CopyEntityInput, RotateEntityInput, MirrorEntityInput,
    MakeBlockInput, InsertBlockInput, DrawTitleBlockInput,
    SetLayerPropsInput, QuitInput, CloseDrawingInput,
    ArrayRectInput, ArrayPolarInput, DrawCenterlineInput, OffsetInput,
    CopyLinearInput, ExplodeInput, JoinPolylineInput,
    CheckDrawingInput, ExportPdfInput, DrawTableInput,
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


# ---------- 1. 核心库功能 ----------
print("=== 1. 核心绘图库 cad_drawing ===")
cad = CadDocument(TEST_DIR)

msg = cad.new_drawing("测试-卧室.dxf")
check("新建图纸", "已保存" in msg["message"], str(msg))
info = cad.get_info()
check("自动创建预设图层", "WALL" in info and "FILL" in info, info.replace("\n", " | "))

# 图层线型/线宽
msg = cad.create_layer("WATER", color=8, linetype="DASHED", lineweight=50)
check("创建图层(线型线宽)", "DASHED" in msg and "50/100mm" in msg, msg)
msg = cad.create_layer("AXIS", color=2, linetype="CENTER", lineweight=13)
check("创建点划线图层", "CENTER" in msg, msg)
msg = cad.set_layer_props("WALL", linetype="DASHED", lineweight=25)
check("修改图层属性", "DASHED" in msg and "25/100mm" in msg, msg)
lay_w = cad.doc.layers.get("WATER")
check("图层属性生效", lay_w.dxf.color == 8 and lay_w.dxf.linetype == "DASHED" and lay_w.dxf.lineweight == 50,
      f"颜色={lay_w.dxf.color} 线型={lay_w.dxf.linetype} 线宽={lay_w.dxf.lineweight}")
# 中文字体样式
check("中文字体样式GB", "GB" in [s.dxf.name for s in cad.doc.styles])

cad.draw_mline([(0, 0), (3600, 0)], scale=240)
cad.draw_mline([(3600, 0), (3600, 3000)], scale=240)
cad.draw_mline([(3600, 3000), (0, 3000)], scale=240)
cad.draw_mline([(0, 3000), (0, 0)], scale=240)
check("画 MLINE", "MLINE" in cad.get_info())

cad.draw_rectangle(500, 200, 2300, 1200, "FURNITURE")
cad.draw_circle(3000, 2400, 400, "FURNITURE")
check("画矩形/圆", "FURNITURE" in cad.get_info())

cad.draw_text("卧室", 1500, 1500, 300)
r = cad.draw_dimension(0, 0, 3600, 0)
check("尺寸标注", r["value"] == 3600.0, f"value={r['value']}")

rows = cad.list_entities()
check("列出图元", "WALL" in rows and "CIRCLE" in rows)
n_before = sum("|" in row for row in rows.splitlines())

first = rows.split("\n")[0].split("|")[0].strip()
msg = cad.move_entity(first, 100, 100)
check("移动图元", "已移动" in msg, msg)
msg = cad.delete_entity(first)
check("删除图元", "已删除" in msg, msg)
n_after = sum("|" in row for row in cad.list_entities().splitlines())
check("删除后数量减1", n_after == n_before - 1, f"{n_before}->{n_after}")

msg = cad.save()
check("保存", "已保存" in msg, msg)
msg = cad.open_drawing("测试-卧室.dxf")
check("重新打开", "已打开" in msg, msg)

# ---------- 1b. P2：编辑 / 标注 / 图块 / 图框 ----------
print("=== 1b. 编辑 / 标注 / 图块 / 图框 ===")
cad2 = CadDocument(TEST_DIR)
cad2.new_drawing("测试-p2.dxf")
r = cad2.draw_rectangle(0, 0, 1000, 800)
h_rect = r["handle"]
r = cad2.draw_circle(2000, 1000, 300)
h_circ = r["handle"]
r = cad2.draw_text("设备A", 300, 300, 200)
h_text = r["handle"]

r = cad2.copy_entity(h_rect, 1500, 0)
check("复制图元", "copied_from" in r and r["type"] == "LWPOLYLINE", str(r))
msg = cad2.rotate_entity(h_circ, 2000, 1000, 45)
check("旋转图元", "已旋转" in msg, msg)
msg = cad2.mirror_entity(h_text, 0, 500, 1000, 500)
check("镜像图元", "已镜像" in msg, msg)
n_poly = sum(1 for e in cad2.msp if e.dxftype() == "LWPOLYLINE")
check("复制后多一个多边形", n_poly == 2, f"LWPOLYLINE×{n_poly}")

r = cad2.draw_dimension(0, 0, 1000, 800, offset=200, direction="aligned")
check("对齐标注", r["direction"] == "aligned" and abs(r["value"] - 1280.6) < 1, f"value={r['value']}")
r = cad2.draw_angular_dimension(3000, 0, 4000, 0, 3000, 1000, 3600, 400)
check("角度标注", r["kind"] == "angular", str(r))
r = cad2.draw_radius_dimension(6000, 0, 6500, 0)
check("半径标注", r["kind"] == "radius" and r["radius"] == 500.0, str(r))

msg = cad2.make_block("PUMP", [h_rect, h_circ, h_text], 0, 0)
check("打包图块", "已创建图块 PUMP" in msg, msg)
r = cad2.insert_block("PUMP", 8000, 8000, rotation=90, scale=2)
check("插入图块", r["type"] == "INSERT" and r["block"] == "PUMP", str(r))
msg = cad2.draw_title_block(420, 297, title="泵房", project="污水厂",
                            design="张三", check="李四", sheet_no="S-01")
check("画图框标题栏", "已画图框 420×297" in msg, msg)
cad2.save()

# ---------- 1c. trim 修剪 ----------
print("=== 1c. trim 修剪 ===")
cad5 = CadDocument(TEST_DIR)
cad5.new_drawing("测试-trim.dxf")
r = cad5.draw_line(0, 0, 1000, 0)              # 被修剪长线
h_line = r["handle"]
r = cad5.draw_line(500, -200, 500, 200)        # 切割竖线
h_cut = r["handle"]
r = cad5.trim(h_line, [h_cut], 800, 0)         # pick 右侧 → 保留 500~1000
check("trim 直线(保留右侧)", r["cut"] == (500.0, 0.0), str(r))
e = cad5._get_entity(h_line)
check("trim 直线端点", (e.dxf.start[0], e.dxf.start[1]) == (500.0, 0.0)
      and (e.dxf.end[0], e.dxf.end[1]) == (1000.0, 0.0),
      f"start={e.dxf.start} end={e.dxf.end}")

r = cad5.draw_line(0, 300, 1000, 300)          # 第二条线
h_line2 = r["handle"]
r = cad5.draw_line(500, 200, 500, 400)         # 第二条切割竖线（覆盖 y=300）
h_cut_high = r["handle"]
r = cad5.trim(h_line2, [h_cut_high], 100, 300)  # pick 左侧 → 保留 0~500
e = cad5._get_entity(h_line2)
check("trim 直线(保留左侧)", (e.dxf.start[0], e.dxf.start[1]) == (0.0, 300.0)
      and (e.dxf.end[0], e.dxf.end[1]) == (500.0, 300.0),
      f"start={e.dxf.start} end={e.dxf.end}")

# 折线修剪：L 形折线在第一条边中点处剪断
r = cad5.draw_polyline([(0, 0), (300, 0), (300, 300)], close=False)
h_poly = r["handle"]
r = cad5.draw_line(150, -100, 150, 100)        # 竖切割线交 (150,0)
h_cut2 = r["handle"]
r = cad5.trim(h_poly, [h_cut2], 250, 0)        # 保留右侧
check("trim 折线", r["remaining_points"] == 3 and r["cut"] == (150.0, 0.0), str(r))
e = cad5._get_entity(h_poly)
pts = list(e.get_points("xy"))
check("trim 折线顶点", pts == [(150.0, 0.0), (300.0, 0.0), (300.0, 300.0)], str(pts))

# 无交点：不应改变图元
r = cad5.draw_line(0, 600, 1000, 600)
h_line3 = r["handle"]
r = cad5.draw_line(2000, 500, 2000, 700)       # 切割线在远处，不相交
h_cut3 = r["handle"]
msg = cad5.trim(h_line3, [h_cut3], 500, 600)
check("trim 无交点", "未找到可修剪的切割交点" in msg.get("message", ""), str(msg))
cad5.save()

# 多段线合并：乱序、反向输入仍能唯一排序，close=True 使用真正 Closed 标志。
cad6 = CadDocument(TEST_DIR)
cad6.new_drawing("测试-几何编辑.dxf")
h1 = cad6.draw_line(0, 0, 1000, 0)["handle"]
h2 = cad6.draw_line(1000, 1000, 1000, 0)["handle"]  # 故意反向
h3 = cad6.draw_line(0, 1000, 1000, 1000)["handle"]
h4 = cad6.draw_line(0, 0, 0, 1000)["handle"]
r = cad6.join_polyline([h3, h1, h4, h2], close=True, tolerance=0.01)
joined = cad6._get_entity(r["handle"])
check("合并闭合多段线", r["closed"] and joined.closed and len(list(joined.get_points("xy"))) == 4, str(r))
check("合并成功后删除源图元", all(cad6._get_entity(h) is None for h in (h1, h2, h3, h4)))
r_offset = cad6.offset(r["handle"], 100)
check("闭合多段线几何偏移", r_offset["source_retained"] and len(r_offset["entities"]) == 1, str(r_offset))
mline = cad6.draw_mline([(2000, 0), (3000, 0)], scale=200)
exploded = cad6.explode(mline["handle"])
check("DXF MLINE 分解", exploded["deleted_source"] and len(exploded["entities"]) >= 2, str(exploded))
arc_forward = [(0.0, 0.0, 0.5), (1.0, 1.0, 0.0)]
arc_reverse = reverse_polyline_part(arc_forward)
check("反向圆弧 bulge", arc_reverse[0][2] == -0.5 and arc_reverse[-1][2] == 0.0, str(arc_reverse))
try:
    join_polyline_parts([
        [(0, 0, 0), (1, 0, 0)],
        [(1, 0, 0), (2, 0, 0)],
        [(1, 0, 0), (1, 1, 0)],
    ])
    check("分叉路径拒绝", False, "分叉输入不应成功")
except ValueError as exc:
    check("分叉路径拒绝", "分叉" in str(exc), str(exc))
cad6.save()

# 制图环境：文字与线型比例均由调用方显式设置
cad3 = CadDocument(TEST_DIR)
cad3.new_drawing("制图环境.dxf")
cad3.create_or_update_textstyle("TEST_TEXT", 700)
r = cad3.draw_text("样式字高", 0, 0)
check("文字继承当前样式字高", r["height"] == 700.0, f"字高={r['height']}")
cad3.draw_rectangle(0, 0, 50000, 30000)
r = cad3.draw_text("大图仍继承样式", 100, 100)
check("图形范围不改变字高", r["height"] == 700.0, f"字高={r['height']}")
r = cad3.draw_text("显式字高", 100, 500, height=200)
check("显式字高仍生效", r["height"] == 200, f"字高={r['height']}")
msg = cad3.set_linetype_scale(50)
check("显式线型比例", "50" in msg, msg)
cad3.save()

# ---------- 1c. P3：批量布置 / 中心线 / 检查 / 表格 / 图框增强 ----------
print("=== 1c. 批量布置 / 中心线 / 检查 / 表格 / 图框增强 ===")
cad4 = CadDocument(TEST_DIR)
cad4.new_drawing("测试-p3.dxf")
r = cad4.draw_rectangle(0, 0, 1000, 500)
h_rect = r["handle"]
r = cad4.draw_circle(2000, 1500, 400)
h_circ = r["handle"]

# 矩形阵列：2 行 × 3 列，原件保留 → 新副本 5 个
msg = cad4.array_rect([h_rect], rows=2, cols=3, row_spacing=1500, col_spacing=2000)
check("矩形阵列", "2 行 × 3 列" in msg and "新增 5 个副本" in msg, msg)
n_poly = sum(1 for e in cad4.msp if e.dxftype() == "LWPOLYLINE")
check("阵列后多边形数=6", n_poly == 6, f"LWPOLYLINE×{n_poly}")

# 环形阵列：圆绕 (2000,1500) 均布 6 个 → 新增 5
msg = cad4.array_polar([h_circ], 2000, 1500, count=6, total_angle=360)
check("环形阵列", "均布 6 个" in msg and "新增 5 个副本" in msg, msg)
n_circ = sum(1 for e in cad4.msp if e.dxftype() == "CIRCLE")
check("环形阵列后圆数=6", n_circ == 6, f"CIRCLE×{n_circ}")

# 批量偏移：线偏移 3 份
r = cad4.draw_line(0, 3000, 5000, 3000)
h_line = r["handle"]
msg = cad4.copy_linear([h_line], dx=0, dy=-300, copies=3)
check("线性复制", "× 3 份" in msg and "新增 3 个副本" in msg, msg)
n_line = sum(1 for e in cad4.msp if e.dxftype() == "LINE")
check("偏移后线数=4", n_line == 4, f"LINE×{n_line}")

# 中心线：给矩形范围画横竖两条点划线
r = cad4.draw_centerline(0, 0, 1000, 500)
check("画中心线", r["type"] == "CENTERLINE" and len(r["handles"]) == 2, str(r))
center_lay = cad4.doc.layers.get("CENTER")
check("中心线图层红色点划线", center_lay.dxf.color == 1 and center_lay.dxf.linetype == "CENTER",
      f"颜色={center_lay.dxf.color} 线型={center_lay.dxf.linetype}")
check("中心线不擅自修改线型比例", cad4.doc.header["$LTSCALE"] == 1, f"LTSCALE={cad4.doc.header['$LTSCALE']}")

# 图面检查：先画一个正常图，应通过（空图层会有提示，所以只查重叠/断线关键词）
cad4.draw_rectangle(10000, 10000, 11000, 10500)
msg = cad4.check_drawing(tolerance=10)
check("检查正常图无重叠断线", "[重叠]" not in msg and "[断线]" not in msg, msg[:60])
# 故意制造重叠圆 + 断线
cad4.draw_circle(5000, 0, 100)
cad4.draw_circle(5000, 0, 100)          # 与上一个完全重合
cad4.draw_line(6000, 0, 6500, 0)
cad4.draw_line(6500, 0.5, 7000, 0)      # 端点相距 0.5mm < 容差 → 断线
msg = cad4.check_drawing(tolerance=5)
check("检查到重叠圆", "[重叠]" in msg, msg)
check("检查到疑似断线", "[断线]" in msg, msg)

# 表格：设备材料表
r = cad4.draw_table(0, 8000, [30, 100, 150, 60, 80], 40,
                    ["序号", "名称", "规格", "数量", "备注"],
                    [["1", "离心泵", "IS100-65-200", "2台", ""],
                     ["2", "格栅", "B=1000", "1套", "细格栅"]],
                    title="设备材料表")
check("画表格", r["type"] == "TABLE" and r["rows"] == 3 and r["cols"] == 5, str(r))
n_text = sum(1 for e in cad4.msp if e.dxftype() == "TEXT")
check("表格文字已写入", n_text >= 10, f"TEXT×{n_text}")

# 图框增强：size=A3 自动 420×297
msg = cad4.draw_title_block(0, 0, size="A3", title="沉淀池", ratio="1:50")
check("图框 A3 幅面", "已画图框 420×297" in msg, msg)
msg = cad4.draw_title_block(0, 0, size="A4", title="横放")
check("图框 A4 幅面", "已画图框 297×210" in msg, msg)
cad4.save()

# ---------- 2. MCP 工具层 ----------
print("=== 2. MCP 工具层（FastMCP） ===")


r = cad_mcp_server.cad_new_drawing(NewDrawingInput(filename="测试-mcp.dxf"))
check("MCP 新建图纸", '"ok": true' in r, r[:60])
r = cad_mcp_server.cad_draw_mline(DrawMLineInput(points=[[0, 0], [2000, 0]], scale=240))
check("MCP 画 MLINE", '"MLINE"' in r, r[:80])
r = cad_mcp_server.cad_draw_line(DrawLineInput(x1=0, y1=2500, x2=2000, y2=2500))
h_subj = json.loads(r)["handle"]
r = cad_mcp_server.cad_draw_line(DrawLineInput(x1=1000, y1=2400, x2=1000, y2=2600))
h_cut = json.loads(r)["handle"]
r = cad_mcp_server.cad_trim(TrimInput(subject_handle=h_subj, cutting_handles=[h_cut],
                                      pick_x=1500, pick_y=2500))
check("MCP 修剪", '"ok": true' in r and "1000.0" in r and "2500.0" in r, r[:120])
r = cad_mcp_server.cad_draw_rectangle(DrawRectangleInput(x1=100, y1=100, x2=300, y2=300, layer="FURNITURE"))
check("MCP 画矩形", '"LWPOLYLINE"' in r, r[:60])
r = cad_mcp_server.cad_draw_circle(DrawCircleInput(cx=1000, cy=1000, radius=200, layer="FURNITURE"))
check("MCP 画圆", '"CIRCLE"' in r, r[:60])
r = cad_mcp_server.cad_draw_line(DrawLineInput(x1=0, y1=0, x2=500, y2=500))
check("MCP 画线", '"LINE"' in r, r[:60])
r = cad_mcp_server.cad_draw_arc(DrawArcInput(cx=100, cy=100, radius=50, start_angle=0, end_angle=180))
check("MCP 圆弧", '"ARC"' in r, r[:60])
r = cad_mcp_server.cad_draw_polyline(DrawPolylineInput(points=[[0,0],[300,0],[300,200]], close=True))
check("MCP 多段线", '"LWPOLYLINE"' in r, r[:60])
r = cad_mcp_server.cad_draw_hatch(DrawHatchInput(points=[[0,0],[200,0],[200,200],[0,200]], scale=5))
check("MCP 填充", '"HATCH"' in r, r[:60])
r = cad_mcp_server.cad_draw_text(DrawTextInput(text="测试", x=800, y=800, height=200))
check("MCP 文字", '"TEXT"' in r, r[:60])
r = cad_mcp_server.cad_draw_text(DrawTextInput(text="旋转", x=500, y=500, height=200, rotation=45))
check("MCP 文字旋转", '"rotation": 45' in r, r[:60])
r = cad_mcp_server.cad_draw_dimension(DrawDimensionInput(p1x=0, p1y=0, p2x=0, p2y=1500, direction="vertical"))
check("MCP 垂直标注", '"vertical"' in r and '"value": 1500' in r, r[:60])
r = cad_mcp_server.cad_draw_mline(DrawMLineInput(points=[[0, 0], [1000, 0]], scale=240, layer="FURNITURE"))
check("MCP MLINE 指定图层", '"FURNITURE"' in r, r[:80])
r = cad_mcp_server.cad_draw_dimension(DrawDimensionInput(p1x=0, p1y=0, p2x=2000, p2y=0))
check("MCP 尺寸", '"DIMENSION"' in r, r[:60])
r = cad_mcp_server.cad_save_drawing(cad_mcp_server.SaveDrawingInput())
check("MCP 保存", '"ok": true' in r, r[:60])
r = cad_mcp_server.cad_set_layer_props(SetLayerPropsInput(name="WALL", color=3, linetype="DASHED"))
check("MCP 修改图层", '"ok": true' in r, r[:60])
# P2：编辑 / 标注 / 图块 / 图框
r = cad_mcp_server.cad_draw_circle(DrawCircleInput(cx=500, cy=500, radius=100))
h_circle = json.loads(r)["handle"]
r = cad_mcp_server.cad_copy_entity(CopyEntityInput(handle=h_circle, dx=300, dy=0))
check("MCP 复制", '"ok": true' in r and '"copied_from"' in r, r[:80])
r = cad_mcp_server.cad_rotate_entity(RotateEntityInput(handle=h_circle, base_x=500, base_y=500, angle=90))
check("MCP 旋转", '"ok": true' in r, r[:80])
r = cad_mcp_server.cad_mirror_entity(MirrorEntityInput(handle=h_circle, x1=0, y1=0, x2=100, y2=0))
check("MCP 镜像", '"ok": true' in r, r[:80])
r = cad_mcp_server.cad_draw_dimension(DrawDimensionInput(p1x=0, p1y=0, p2x=300, p2y=400, direction="aligned"))
check("MCP 对齐标注", '"aligned"' in r and '"value": 500' in r, r[:80])
r = cad_mcp_server.cad_draw_angular_dimension(DrawAngularDimensionInput(vertex_x=0, vertex_y=0, p1x=100, p1y=0, p2x=0, p2y=100, loc_x=30, loc_y=30))
check("MCP 角度标注", '"angular"' in r, r[:80])
r = cad_mcp_server.cad_draw_radius_dimension(DrawRadiusDimensionInput(cx=0, cy=0, edge_x=50, edge_y=0))
check("MCP 半径标注", '"radius"' in r and '"radius": 50.0' in r, r[:80])
r = cad_mcp_server.cad_make_block(MakeBlockInput(name="TEST_BLK", handles=[h_circle], base_x=0, base_y=0))
check("MCP 打包图块", '"ok": true' in r and "TEST_BLK" in r, r[:80])
r = cad_mcp_server.cad_insert_block(InsertBlockInput(name="TEST_BLK", x=100, y=100))
check("MCP 插入图块", '"INSERT"' in r, r[:80])
r = cad_mcp_server.cad_draw_title_block(DrawTitleBlockInput(width=420, height=297, title="测试"))
check("MCP 图框", '"ok": true' in r and "已画图框" in r, r[:80])
# P3：批量布置 / 中心线 / 检查 / 表格 / 出图
r = cad_mcp_server.cad_draw_title_block(DrawTitleBlockInput(size="A3", title="沉淀池", ratio="1:50"))
check("MCP 图框 A3 幅面", '"ok": true' in r and "420×297" in r, r[:80])
r = cad_mcp_server.cad_draw_rectangle(DrawRectangleInput(x1=1000, y1=1000, x2=2000, y2=1500))
h_p3 = json.loads(r)["handle"]
r = cad_mcp_server.cad_array_rect(ArrayRectInput(handles=[h_p3], rows=2, cols=2, row_spacing=500, col_spacing=500))
check("MCP 矩形阵列", '"ok": true' in r and "新增 3 个副本" in r, r[:80])
r = cad_mcp_server.cad_draw_circle(DrawCircleInput(cx=5000, cy=5000, radius=300))
h_pc = json.loads(r)["handle"]
r = cad_mcp_server.cad_array_polar(ArrayPolarInput(handles=[h_pc], cx=5000, cy=5000, count=4))
check("MCP 环形阵列", '"ok": true' in r and "新增 3 个副本" in r, r[:80])
r = cad_mcp_server.cad_offset(OffsetInput(handle=h_p3, distance=300))
check("MCP 几何偏移", '"ok": true' in r and '"source_retained": true' in r, r[:120])
r = cad_mcp_server.cad_copy_linear(CopyLinearInput(handles=[h_p3], dx=0, dy=300, copies=2))
check("MCP 线性复制", '"ok": true' in r and "新增 2 个副本" in r, r[:100])
r = cad_mcp_server.cad_draw_centerline(DrawCenterlineInput(x1=1000, y1=1000, x2=2000, y2=1500))
check("MCP 中心线", '"CENTERLINE"' in r and '"CENTER"' in r, r[:80])
r = cad_mcp_server.cad_check_drawing(CheckDrawingInput(tolerance=5))
check("MCP 图面检查", "图面检查通过" in r or "发现" in r, r[:60])
r = cad_mcp_server.cad_draw_table(DrawTableInput(
    origin_x=0, origin_y=6000, col_widths=[30, 100, 150, 60],
    row_height=40, headers=["序号", "名称", "规格", "数量"],
    rows=[["1", "离心泵", "IS100-65", "2台"]], title="设备材料表"))
check("MCP 表格", '"TABLE"' in r and '"rows": 2' in r, r[:80])
r = cad_mcp_server.cad_export_pdf(ExportPdfInput(filename="测试.pdf"))
check("MCP 导出PDF(dxf提示)", '"ok": false' in r and "仅 acad 模式" in r, r[:80])
r = cad_mcp_server.cad_quit(QuitInput())
check("MCP 退出(dxf提示)", "无需退出" in r, r[:60])
r = cad_mcp_server.cad_close_drawing(CloseDrawingInput())
check("MCP 关闭 DXF", '"ok": true' in r and "关闭" in r, r[:80])

# ---------- 3. 生成示例平面图到用户目录 ----------
if os.environ.get("CAD_WRITE_DEMO") == "1":
    print("=== 3. 生成示例平面图（已显式启用） ===")
    USER_CAD_DIR.mkdir(parents=True, exist_ok=True)
    demo = CadDocument(USER_CAD_DIR)
    demo_file = USER_CAD_DIR / "示例-卧室.dxf"
    if demo_file.exists():
        demo_file.unlink()  # 只覆盖本脚本固定生成的文件
    demo.new_drawing("示例-卧室.dxf")
    demo.draw_mline([(0, 0), (3600, 0)], scale=240)
    demo.draw_mline([(3600, 0), (3600, 3000)], scale=240)
    demo.draw_mline([(3600, 3000), (0, 3000)], scale=240)
    demo.draw_mline([(0, 3000), (0, 0)], scale=240)
    demo.draw_rectangle(400, 200, 2400, 1400, "FURNITURE")
    demo.draw_circle(3000, 2400, 400, "FURNITURE")
    demo.draw_text("卧室", 1500, 1650, 250)
    demo.draw_dimension(0, 0, 3600, 0, offset=600)
    demo.save()
    check("示例平面图已生成", demo_file.exists(), str(demo_file))
else:
    print("[SKIP] 示例图写出未启用；如需执行，请设置 CAD_WRITE_DEMO=1")

print(f"\n========== 结果: {PASS} 通过, {FAIL} 失败 ==========")
sys.exit(1 if FAIL else 0)
