"""Deterministic sheet planning. All coordinates in this module are paper mm.

No AutoCAD calls and no drawing writes: a paper reference rectangle is distinct
from the printed border and from the usable content rectangle.
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import time
from typing import Literal

from pydantic import Field, model_validator

from cad_mcp.core.runtime import ConfirmationRequiredError, ConflictError
from cad_mcp.engineering.templates import Bounds, SheetTemplate, TemplateModel


PAPER_TOLERANCE_MM = 0.05
VIEWPORT_LAYER = "CAD-MCP-VPORT"


class LayoutRequest(TemplateModel):
    manifest_path: str = Field(min_length=1, max_length=1000, description="图框模板清单 JSON 文件路径；相对源文件路径按清单目录解析")
    sheet_id: str = Field(min_length=1, max_length=120, description="模板清单中已声明的图幅 ID")
    layout_name: str = Field(min_length=1, max_length=120, description="目标纸空间布局名称；不能将 Model 当作纸空间布局")
    layout_id: str = Field(min_length=1, max_length=120,
        description="稳定的布局归属 ID；重复执行必须沿用")
    region_id: str = Field(min_length=1, max_length=120, description="模板清单 usable_regions 中的有效绘图区 ID")
    page_setup_name: str = Field(min_length=1, max_length=255,
        description="已有命名页面设置；预览只读查询该设置，不修改当前布局")
    rotation: Literal[0, 90, 180, 270] | None = Field(default=None, description="旋转角度，单位为度；布局打印仅接受其枚举列出的方向")
    content_bounds: Bounds | None = Field(default=None, description="模型内容范围 [xmin,ymin,xmax,ymax]；使用模型坐标，不是纸面毫米")
    content_handles: list[str] | None = Field(default=None, min_length=1, max_length=10000, description="限定展示范围的模型空间十进制句柄列表；与 content_bounds 互斥")
    model_mm_per_unit: float = Field(gt=0, le=1e9,
        description="一个模型坐标单位对应多少毫米；毫米为1，米为1000")
    model_elevation: float = Field(default=0.0, description="目标模型平面的 WCS Z 标高；不将倾斜对象投影到平面")
    scale_denominator: float = Field(gt=0, le=1e9, description="比例分母；视口 100 表示 1:100，自定义打印比例须结合分子")
    padding_mm: float = Field(default=2.0, ge=0, le=1000, description="视口相对有效绘图区四边的纸面留白，毫米")
    existing_viewport_handle: str | None = Field(default=None,
        description="显式复用目标布局的浮动视口，正整数十进制句柄；保留原实体")

    @model_validator(mode="after")
    def validate_selection(self):
        if self.existing_viewport_handle is not None:
            h = self.existing_viewport_handle
            if not h.isascii() or not h.isdecimal() or len(h) > 40 or int(h) <= 0:
                raise ValueError("existing_viewport_handle 必须是正整数十进制句柄")
            self.existing_viewport_handle = str(int(h))
        if self.layout_name.casefold() == "model" or any(c in self.layout_name for c in '<>/\\":;?*|='):
            raise ValueError("layout_name 必须是有效的非 Model 布局名称")
        if (self.content_bounds is None) == (self.content_handles is None):
            raise ValueError("content_bounds 与 content_handles 必须且只能提供一项")
        if self.content_bounds is not None:
            positive_bounds(self.content_bounds, "content_bounds")
        if self.content_handles is not None:
            if any(not h.isascii() or not h.isdecimal() or len(h) > 40 or int(h) <= 0
                   for h in self.content_handles):
                raise ValueError("content_handles 必须是正整数十进制句柄")
            self.content_handles = list(dict.fromkeys(str(int(h)) for h in self.content_handles))
        return self


def positive_bounds(bounds, name="bounds"):
    if len(bounds) != 4 or not all(math.isfinite(float(v)) for v in bounds):
        raise ValueError(f"{name} 必须包含四个有限坐标")
    if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
        raise ValueError(f"{name} 必须形成正面积矩形")
    return tuple(float(v) for v in bounds)


def contains(outer, inner, tolerance=PAPER_TOLERANCE_MM):
    return (inner[0] >= outer[0] - tolerance and inner[1] >= outer[1] - tolerance
            and inner[2] <= outer[2] + tolerance and inner[3] <= outer[3] + tolerance)


def overlaps(a, b, tolerance=1e-8):
    return (min(a[2], b[2]) - max(a[0], b[0]) > tolerance
            and min(a[3], b[3]) - max(a[1], b[1]) > tolerance)


def close_values(a, b, tolerance=PAPER_TOLERANCE_MM):
    return len(a) == len(b) and all(math.isclose(float(x), float(y), abs_tol=tolerance,
                                               rel_tol=1e-10) for x, y in zip(a, b))


def fingerprint(value):
    raw = json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def paper_geometry(raw_size, lower_left, upper_right, rotation):
    """Rotate physical sheet dimensions and edge margins counterclockwise.

    COM returns margins as distances from each physical edge, not as the upper
    right printable coordinate. PlotOrigin is normalized to zero during apply.
    """
    w, h = map(float, raw_size)
    l, b = map(float, lower_left)
    r, t = map(float, upper_right)
    if not all(math.isfinite(v) for v in (w, h, l, b, r, t)) or min(w, h) <= 0 or min(l,b,r,t) < 0:
        raise ValueError("实际纸张尺寸或打印边距无效")
    if rotation not in (0, 90, 180, 270):
        raise ValueError("纸张旋转必须为 0/90/180/270")
    for _ in range(rotation // 90):
        w, h, l, b, r, t = h, w, t, l, b, r
    if l + r >= w or b + t >= h:
        raise ValueError("实际打印边距占满纸张")
    return {"size_mm": [w, h], "margins_mm": [l, b, r, t],
            "printable_bounds_mm": [l, b, w-r, h-t],
            "paper_origin_layout": [-l, -b], "plot_origin_mm": [0.0, 0.0],
            "rotation": rotation}


def plan_sheet(request: LayoutRequest, sheet: SheetTemplate, content_bounds,
               paper, block_origin):
    reference = sheet.paper_reference_bounds
    printed = sheet.printed_bounds
    if reference is None or printed is None:
        raise ValueError("完整布局需要模板显式提供 paper_reference_bounds 和 printed_bounds；不得从包围盒猜测")
    positive_bounds(reference, "paper_reference_bounds")
    positive_bounds(printed, "printed_bounds")
    scale = sheet.fixed_insert_scale
    declared_size = [sheet.paper_width, sheet.paper_height]
    reference_size = [(reference[2]-reference[0])*scale, (reference[3]-reference[1])*scale]
    if not close_values(reference_size, declared_size):
        raise ValueError("图框纸张参考边界经固定换算后与声明纸张尺寸不符")
    if not close_values(paper["size_mm"], declared_size):
        raise ValueError(f"实际纸张尺寸 {paper['size_mm']} 与图框纸张尺寸 {declared_size} 不符")
    if (sheet.orientation == "landscape") != (sheet.paper_width >= sheet.paper_height):
        raise ValueError("图框横竖方向与声明宽高不符")
    region = next((r for r in sheet.usable_regions if r.id == request.region_id), None)
    if region is None:
        raise ValueError(f"模板不存在绘图区 {request.region_id}")
    ox, oy = paper["paper_origin_layout"]
    # Region coordinates are physical paper mm from paper-reference lower left.
    area = [region.x, region.y, region.x+region.width, region.y+region.height]
    physical = [0, 0, sheet.paper_width, sheet.paper_height]
    if not contains(physical, area):
        raise ValueError("有效绘图区超出纸张")
    for reserved in sheet.reserved_regions:
        bounds = [reserved.x, reserved.y, reserved.x+reserved.width, reserved.y+reserved.height]
        if not contains(physical, bounds) or overlaps(bounds, area):
            raise ValueError("有效绘图区与图签等预留区域重叠，或预留区域超出纸张")
    printed_paper = [(printed[0]-reference[0])*scale, (printed[1]-reference[1])*scale,
                     (printed[2]-reference[0])*scale, (printed[3]-reference[1])*scale]
    if not contains(paper["printable_bounds_mm"], printed_paper):
        raise ValueError("需打印边框超出设备可打印范围；应选择兼容纸张/设备，不能缩放图框")
    if not contains(printed_paper, area) or not contains(paper["printable_bounds_mm"], area):
        raise ValueError("有效绘图区超出需打印边框或设备可打印范围")
    width, height = region.width-2*request.padding_mm, region.height-2*request.padding_mm
    if min(width, height) <= 0:
        raise ValueError("扣除绘图区留白后视口宽高必须大于零")
    xmin, ymin, xmax, ymax = positive_bounds(content_bounds, "模型内容范围")
    custom_scale = request.model_mm_per_unit / request.scale_denominator
    minimum = max((xmax-xmin)*request.model_mm_per_unit/width,
                  (ymax-ymin)*request.model_mm_per_unit/height)
    if request.scale_denominator + 1e-9 < minimum:
        raise ValueError(f"指定比例无法完整显示模型与标注；最小所需比例分母为 {minimum:g}，不自动改比例")
    cx, cy = ox+(area[0]+area[2])/2, oy+(area[1]+area[3])/2
    return {"paper": paper, "frame": {
        "block_name": sheet.block_name, "scale": scale,
        "insertion_point": [ox-(reference[0]-block_origin[0])*scale,
                            oy-(reference[1]-block_origin[1])*scale, block_origin[2]*scale],
        "reference_bounds_layout": [ox, oy, ox+sheet.paper_width, oy+sheet.paper_height],
        "expected_bounds_layout": [ox+(sheet.source_bounds[0]-reference[0])*scale,
                                   oy+(sheet.source_bounds[1]-reference[1])*scale,
                                   ox+(sheet.source_bounds[2]-reference[0])*scale,
                                   oy+(sheet.source_bounds[3]-reference[1])*scale],
        "printed_bounds_paper": printed_paper},
        "region": {"id": region.id, "physical_bounds": area,
                   "layout_bounds": [ox+area[0],oy+area[1],ox+area[2],oy+area[3]]},
        "viewport": {"paper_center": [cx,cy], "paper_size": [width,height],
                     "paper_bounds": [cx-width/2,cy-height/2,cx+width/2,cy+height/2],
                     "model_center": [(xmin+xmax)/2,(ymin+ymax)/2,request.model_elevation],
                     "custom_scale": custom_scale, "scale_denominator": request.scale_denominator,
                     "model_view_height": height/custom_scale, "locked": True},
        "content_bounds": [xmin,ymin,xmax,ymax], "minimum_scale_denominator": minimum}


# Confirmation binds both the plan and fresh evidence, not just caller values.
_CONFIRMATIONS: dict[str, tuple[float, str]] = {}


def confirmation(plan):
    now = time.monotonic()
    for key, (created, _) in list(_CONFIRMATIONS.items()):
        if now-created > 1800:
            del _CONFIRMATIONS[key]
    while len(_CONFIRMATIONS) >= 128:
        del _CONFIRMATIONS[next(iter(_CONFIRMATIONS))]
    token = secrets.token_urlsafe(32)
    _CONFIRMATIONS[token] = (now, fingerprint(plan))
    return token


def verify_confirmation(token, plan):
    record = _CONFIRMATIONS.get(token or "")
    if record is None or time.monotonic()-record[0] > 1800:
        raise ConfirmationRequiredError("布局应用需要有效的预览 confirmation_token（30分钟有效）")
    if record[1] != fingerprint(plan):
        raise ConflictError("预览后布局配置、图框、内容或目标图纸状态已变化，请重新预览")
