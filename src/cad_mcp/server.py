#!/usr/bin/env python3
"""
CAD MCP 服务器 — 根据用户自然语言要求，生成/修改 AutoCAD 平面图。

双模式：
  - dxf 模式：生成 .dxf 文件，无需 AutoCAD，快速轻量。
  - acad 模式（默认）：直接操控本地 AutoCAD，画出原生 .dwg（需本机安装 AutoCAD）。
  可用 cad_switch_mode 工具在会话中随时切换。

用法：
    cad-mcp                             # stdio 模式
    cad-mcp config show                 # 查看当前配置

配置：
    默认读取 ~/.cad-mcp/config.json；环境变量可覆盖对应配置。
"""

import os
import sys
from pathlib import Path
from typing import Optional, Literal, List, Tuple, Dict, Any

from pydantic import BaseModel, Field, ConfigDict, ValidationError, model_validator
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from cad_mcp.config import load_settings
from cad_mcp.drawing.document import (
    CadDocument,
    LAYER_WALL,
    LAYER_DIM,
    LAYER_TEXT,
    LAYER_FURNITURE,
    LAYER_FILL,
)
from cad_mcp.drawing.entity_properties import EntityPropertyPatch
from cad_mcp.drawing.native_extend import validate_request as validate_extend_request
from cad_mcp.core.geometry import (
    MAX_TABLE_COLUMNS,
    MAX_TABLE_ROWS,
    validate_hatch_parameters,
    validate_bulk_creation,
    validate_diameter_dimension,
    validate_table_data,
)
from cad_mcp.drawing.autocad import AcadBackend
from cad_mcp.drawing.hatch_regions import validate_handles
from cad_mcp.core.compiler import ModelValidationError, compile_environment_model
from cad_mcp.core.contracts import (
    CadOperation,
    EnvironmentModel,
    RuleDefinition,
    reject_excessive_nested_content,
)
from cad_mcp.dwt.contracts import DraftingProfileV2, load_dwt_profile_v2
from cad_mcp.core.registry import reconcile_plan
from cad_mcp.core.rules import RuleSet, load_rule_set
from cad_mcp.engineering.standards import (
    AnyDraftingProfile,
    load_drafting_profile,
    validate_against_drafting_profile,
)
from cad_mcp.engineering.templates import (
    TemplateManifest,
    evaluate_sheet_scales,
    load_template_manifest,
    verify_template_source,
)
from cad_mcp.core.validation import validate_model
from cad_mcp.core.runtime import (
    UnsupportedModeError, failure_json, setup_logging, success_json,
)

_RULE_SETS: Dict[str, RuleSet] = {}
_DRAFTING_PROFILES: Dict[str, AnyDraftingProfile] = {}
_MAX_LOADED_CONFIGS = 64
_MAX_RESOLVED_RULES = 10_000


def _bounded_cache_put(cache: Dict[str, Any], key: str, value: Any) -> None:
    if key in cache:
        cache.pop(key)
    elif len(cache) >= _MAX_LOADED_CONFIGS:
        cache.pop(next(iter(cache)))
    cache[key] = value


def _resolved_rules(
    rule_set_ids: List[str],
    inline_rules: List[RuleDefinition],
) -> List[RuleDefinition]:
    missing = [rule_set_id for rule_set_id in rule_set_ids if rule_set_id not in _RULE_SETS]
    if missing:
        raise ValueError(
            "规则集尚未加载: " + ", ".join(missing)
            + "；请先调用 cad_load_environment_rule_set")
    resolved = [
        rule
        for rule_set_id in rule_set_ids
        for rule in _RULE_SETS[rule_set_id].rules
    ]
    resolved.extend(inline_rules)
    if len(resolved) > _MAX_RESOLVED_RULES:
        raise ValueError(
            f"合并后的规则数 {len(resolved)} 超过单次上限 {_MAX_RESOLVED_RULES}")
    return resolved


def _validate_inline_rule_complexity(rules: List[RuleDefinition]) -> None:
    reject_excessive_nested_content(
        [rule.model_dump(mode="python") for rule in rules]
    )


def _resolved_drafting_profile(profile_id: Optional[str]) -> Optional[AnyDraftingProfile]:
    if profile_id is None:
        return None
    if profile_id not in _DRAFTING_PROFILES:
        raise ValueError(
            f"制图标准配置尚未加载: {profile_id}；"
            "请先调用 cad_load_drafting_profile")
    return _DRAFTING_PROFILES[profile_id]


def _validate_environment(
    model: EnvironmentModel,
    rules: List[RuleDefinition],
    drafting_profile: Optional[AnyDraftingProfile],
):
    report = validate_model(model, rules)
    if drafting_profile is not None:
        report.issues.extend(validate_against_drafting_profile(model, drafting_profile))
        report.checked_counts["drafting_profiles"] = 1
        report.ok = not any(issue.severity == "error" for issue in report.issues)
    return report

# 注：AutoCAD 单实例互斥锁现在由 acad 后端在"真正连接 AutoCAD"时才获取
# （见 drawing/autocad.py 的 _acquire_autocad_lock）。dxf 模式完全不碰锁。

# Windows 控制台默认 GBK，强制 UTF-8 保证 stdio JSON-RPC 与中文不乱码
for stream in (sys.stdout, sys.stderr):
    try:
        if stream.encoding and stream.encoding.lower() not in ("utf-8", "utf8"):
            stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

_SETTINGS = load_settings()
_LOGGER = setup_logging(log_dir=_SETTINGS.log_dir)
DRAWINGS_DIR = _SETTINGS.drawings_dir

mcp = FastMCP("cad_mcp")


class _AcadLockProxy:
    """acad 后端锁代理：所有公开方法调用期间持有"操作级锁"，结束自动释放。

    让 Claude Code / Codex 等多个客户端都能操作同一个 AutoCAD，只是**交替进行**：
    谁调用工具谁短暂持锁，做完立刻还，不会把任何一方锁死。
    锁的获取/释放由本代理在方法调用前后自动完成，工具函数无需改动。
    dxf 模式不经此代理（不碰 AutoCAD，无需锁）。
    """

    # edit_entity_properties and extend_entity own their undo groups after preflight: an empty group
    # followed by U could otherwise undo the preceding successful operation.
    _MUTATING_METHODS = {
        "create_layer", "set_layer_props", "set_active_layer",
        "create_or_update_textstyle", "create_or_update_dimstyle",
        "set_linetype_scale", "set_linetype_settings",
        "draw_line", "draw_rectangle", "draw_circle", "draw_arc",
        "draw_polyline", "draw_mline", "draw_text", "draw_mtext", "trim",
        "draw_dimension", "draw_dimension_at", "draw_angular_dimension",
        "draw_radius_dimension", "draw_diameter_dimension", "draw_hatch", "draw_centerline",
        "fill_closed_boundary_solid",
        "make_block", "insert_block", "draw_title_block", "draw_table",
        "delete_entity", "move_entity", "copy_entity", "rotate_entity",
        "mirror_entity", "array_rect", "array_polar", "offset", "copy_linear",
        "explode", "join_polyline",
        "apply_operation_plan", "set_entity_metadata",
        "create_layout", "insert_template_sheet", "fill_template_fields",
        "fill_template_table", "create_viewport", "configure_layout_plot",
        "set_drawing_settings", "convert_plot_style_mode",
        "rename_layer", "delete_layer", "rename_layout", "delete_layout",
        "create_or_update_page_setup", "create_or_update_attribute_block",
        "set_block_attributes", "bind_block_attribute_field",
        "import_block_definitions", "export_block_library",
        "insert_coordinate_label", "insert_elevation_label",
        "create_or_update_ctb", "apply_dwt_profile",
    }

    # health 是连接诊断与恢复入口。若旧 COM 引用已经失效，先执行普通操作的
    # 空闲检查会把 health 本身挡在重连逻辑之外；因此仅 health 跳过该检查。
    # 单实例锁仍照常获取，其他所有公开操作也仍必须先确认 AutoCAD 空闲。
    _READINESS_EXEMPT_METHODS = {"health"}

    def __init__(self, backend):
        object.__setattr__(self, "_b", backend)

    def __getattr__(self, name):
        attr = getattr(self._b, name)
        if callable(attr) and not name.startswith("_"):
            def locked(*args, **kwargs):
                self._b._acquire_autocad_lock()
                undo_started = False
                try:
                    if name not in self._READINESS_EXEMPT_METHODS:
                        self._b._wait_operation_ready()
                    if name in self._MUTATING_METHODS and kwargs.get("dry_run") is not True:
                        undo_started = self._b._begin_undo()
                    result = attr(*args, **kwargs)
                    self._b._commit_undo(undo_started)
                    return result
                except Exception as exc:
                    rolled_back = self._b._rollback_undo(undo_started)
                    if undo_started:
                        try:
                            exc.rollback_status = (
                                "succeeded" if rolled_back is True else "failed_or_unverified"
                            )
                        except Exception:
                            pass
                    raise
                finally:
                    self._b._release_autocad_lock()
            return locked
        return attr


def _create_backend(mode: str):
    """按模式创建绘图后端：acad=默认模式，直接操控本地 AutoCAD；dxf=文件模式，无需 CAD。"""
    if mode == "acad":
        return _AcadLockProxy(AcadBackend(DRAWINGS_DIR))
    return CadDocument(DRAWINGS_DIR)


_backend = _create_backend(_SETTINGS.mode)


def _get_backend():
    """获取当前绘图后端（AI 可随时通过 cad_switch_mode 切换）。"""
    global _backend
    return _backend


def _raw_backend(backend):
    return getattr(backend, "_b", backend)


def _backend_mode(backend) -> str:
    return "acad" if isinstance(_raw_backend(backend), AcadBackend) else "dxf"


def _backend_has_open_drawing(backend) -> bool:
    return getattr(_raw_backend(backend), "doc", None) is not None


def _ok(payload) -> str:
    """统一成功响应（JSON 文本，便于 AI 解析）。"""
    return success_json(payload)


def _fail(e: Exception) -> str:
    """统一错误响应。"""
    operation = sys._getframe(1).f_code.co_name
    return failure_json(e, operation)


class ToolInput(BaseModel):
    """所有 MCP 工具的严格输入基类。"""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


# ================= 工具：图纸管理 =================

class NewDrawingInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    filename: str = Field(
        ...,
        description="图纸文件名，如 '客厅.dwg'；不写扩展名时按当前模式自动补全",
        min_length=1,
        max_length=120,
    )
    template_path: Optional[str] = Field(
        default=None,
        description=(
            "acad 模式可选的 .dwt 完整绝对路径；留空自动读取 AutoCAD 快速新建(QNEW)"
            "配置的默认模板。dxf 模式不支持 DWT 模板"
        ),
        max_length=500,
    )


@mcp.tool(
    name="cad_new_drawing",
    annotations={
        "title": "新建图纸",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_new_drawing(params: NewDrawingInput) -> str:
    """新建图纸；acad 模式默认继承 AutoCAD 快速新建(QNEW)模板。

    输出格式由当前模式决定（用 cad_switch_mode 切换）：
      - dxf 模式：生成空白 .dxf 文件，不支持 .dwt 模板
      - acad 模式：template_path 显式指定时优先；留空读取 CAD 的 QNEW 默认模板

    Args:
        params (NewDrawingInput): filename 为图纸文件名；template_path 为可选 DWT 路径。

    Returns:
        str: JSON，含保存路径。图纸保存在 CAD 图纸目录。
    """
    try:
        result = _get_backend().new_drawing(params.filename, params.template_path)
        if isinstance(result, dict):
            return _ok({"ok": True, **result})
        return _ok({"ok": True, "message": result})
    except Exception as e:
        return _fail(e)


class OpenDrawingInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    filename: str = Field(
        ...,
        description="现有图纸的文件名，或本机盘符绝对路径；DXF 模式仅接受 .dxf，acad 模式接受 .dwg/.dxf",
        min_length=1,
        max_length=4096,
    )


@mcp.tool(
    name="cad_open_drawing",
    annotations={
        "title": "打开图纸",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_open_drawing(params: OpenDrawingInput) -> str:
    """按文件名或本机盘符绝对路径打开已有图纸，以便继续修改。

    Args:
        params (OpenDrawingInput): 包含 filename —— 文件名或现有图纸的本机绝对路径。

    Returns:
        str: JSON，含图元数量。
    """
    try:
        return _ok({"ok": True, "message": _get_backend().open_drawing(params.filename)})
    except Exception as e:
        return _fail(e)


class SaveDrawingInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    pass


@mcp.tool(
    name="cad_save_drawing",
    annotations={
        "title": "保存图纸",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def cad_save_drawing(params: SaveDrawingInput) -> str:
    """保存当前图纸到磁盘。

    Args:
        params (SaveDrawingInput): 无参数。

    Returns:
        str: JSON，含保存路径。
    """
    try:
        return _ok({"ok": True, "message": _get_backend().save()})
    except Exception as e:
        return _fail(e)


class GetDrawingInfoInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    pass


@mcp.tool(
    name="cad_get_drawing_info",
    annotations={
        "title": "查看图纸信息",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def cad_get_drawing_info(params: GetDrawingInfoInput) -> str:
    """查看当前图纸的信息：文件名、单位、图层列表、各类型图元数量。绘图前先调用以了解当前状态。

    Args:
        params (GetDrawingInfoInput): 无参数。

    Returns:
        str: 纯文本信息。
    """
    try:
        return _get_backend().get_info()
    except Exception as e:
        return _fail(e)


# ================= 工具：绘图 =================

class DrawMLineInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    points: List[Tuple[float, float]] = Field(
        ..., description="MLINE 顶点列表 [[x1,y1],[x2,y2],...]；只绘制开放多线",
        min_length=2, max_length=500)
    style_name: Optional[str] = Field(
        default=None, description="已有多线样式名；留空继承当前 CMLSTYLE，不会自动创建样式")
    scale: Optional[float] = Field(
        default=None, description="多线比例；留空继承当前 CMLSCALE", gt=0)
    justification: Optional[Literal["top", "zero", "bottom"]] = Field(
        default=None, description="对正方式；留空继承当前 CMLJUST")
    layer: Optional[str] = Field(default=None, description="图层名；留空使用当前图层")


@mcp.tool(
    name="cad_draw_mline",
    annotations={
        "title": "绘制多线 MLINE",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_draw_mline(params: DrawMLineInput) -> str:
    """绘制原生开放 MLINE，可指定已有样式、比例和对正方式。

    该工具只提供多线能力，不推断墙体厚度，也不负责闭合。需要水密闭合轮廓时，
    应使用 LINE/ARC/多段线组成边界，再调用 cad_join_polyline(close=true) 与 cad_offset。

    Args:
        params (DrawMLineInput): 顶点列表及可选多线属性。

    Returns:
        str: JSON，含 MLINE 句柄和实际样式、比例、对正方式。
    """
    try:
        r = _get_backend().draw_mline(
            params.points, params.style_name, params.scale,
            params.justification, params.layer)
        return _ok({"ok": True, **r})
    except Exception as e:
        return _fail(e)


class TrimInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    subject_handle: str = Field(
        ...,
        description="被修剪图元的句柄（LINE 或 LWPOLYLINE），先用 cad_list_entities 获取",
        min_length=1,
    )
    cutting_handles: List[str] = Field(
        ...,
        description="剪切边界图元的句柄列表（LINE 或 LWPOLYLINE），与图元交叉处将被作为剪断点",
        min_length=1,
        max_length=100,
    )
    pick_x: float = Field(..., description="保留侧参考点 X（毫米）：在要保留的一侧任选一点")
    pick_y: float = Field(..., description="保留侧参考点 Y（毫米）：在要保留的一侧任选一点")


@mcp.tool(
    name="cad_trim",
    annotations={
        "title": "修剪图元",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_trim(params: TrimInput) -> str:
    """修剪图元（TRIM）：在离拾取点最近的切割交点处，把被修剪图元截断，保留拾取点一侧。

    被修剪图元与切割边界均支持 LINE / LWPOLYLINE。先 cad_list_entities 拿句柄再调用。
    例：把一条超出边界的管线，在边界线处截短并保留池内部分。

    Args:
        params (TrimInput): 被修剪句柄、切割边界句柄列表、保留侧拾取点。

    Returns:
        str: JSON，含修剪后的图元类型、句柄、剪断点坐标。
    """
    try:
        r = _get_backend().trim(params.subject_handle, params.cutting_handles,
                                params.pick_x, params.pick_y)
        return _ok({"ok": True, **r})
    except Exception as e:
        return _fail(e)


class ExtendInput(ToolInput):
    subject_handle: str = Field(..., description="目标实体的十进制句柄：LINE、ARC 或开放 LWPOLYLINE", min_length=1, max_length=40)
    boundary_handles: List[str] = Field(..., description="延伸边界的十进制句柄：LINE、ARC、CIRCLE 或 LWPOLYLINE", min_length=1, max_length=100)
    endpoint: Literal["start", "end"] = Field(..., description="延伸起点 start 或终点 end")
    extend_edges: bool = Field(default=False, strict=True, description="是否允许延伸到边界的假想延长线")
    dry_run: bool = Field(default=True, strict=True, description="默认仅预检，不执行原生命令，也不预测交点")

    @model_validator(mode="after")
    def valid_selection(self):
        self.subject_handle, self.boundary_handles = validate_extend_request(
            self.subject_handle, self.boundary_handles, self.endpoint, self.extend_edges, self.dry_run)
        return self


@mcp.tool(
    name="cad_extend",
    annotations={
        "title": "原生延伸图元",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_extend(params: ExtendInput) -> str:
    """调用 AutoCAD 原生 EXTEND，将指定一端延伸至指定边界。

    目标与边界须在模型空间同一 XY 平面；不投影。先查询十进制句柄，
    指定 endpoint=start/end。dry_run 默认只预检，不保证存在交点。
    正式执行后回读几何、核对边界和另一端；失败时报告撤销状态，不自动保存。
    """
    try:
        backend = _get_backend()
        operation = getattr(backend, "extend_entity", None)
        if operation is None:
            raise NotImplementedError("当前后端没有原生命令接口")
        return _ok({"ok": True, **operation(
            params.subject_handle, params.boundary_handles, params.endpoint,
            extend_edges=params.extend_edges, dry_run=params.dry_run)})
    except Exception as e:
        return _fail(e)


class DrawRectangleInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    x1: float = Field(..., description="第一个对角点 X（毫米）")
    y1: float = Field(..., description="第一个对角点 Y（毫米）")
    x2: float = Field(..., description="第二个对角点 X（毫米）")
    y2: float = Field(..., description="第二个对角点 Y（毫米）")
    layer: Optional[str] = Field(default=LAYER_WALL, description="图层名，默认 WALL")


@mcp.tool(
    name="cad_draw_rectangle",
    annotations={
        "title": "画矩形",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_draw_rectangle(params: DrawRectangleInput) -> str:
    """画矩形（两个对角点），可用于房间、家具等轮廓。

    Args:
        params (DrawRectangleInput): 两个对角点坐标与图层。

    Returns:
        str: JSON，含图元句柄。
    """
    try:
        r = _get_backend().draw_rectangle(params.x1, params.y1, params.x2, params.y2, params.layer)
        return _ok({"ok": True, **r})
    except Exception as e:
        return _fail(e)


class DrawCircleInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    cx: float = Field(..., description="圆心 X（毫米）")
    cy: float = Field(..., description="圆心 Y（毫米）")
    radius: float = Field(..., description="半径（毫米）", gt=0, le=10000)
    layer: Optional[str] = Field(default=LAYER_WALL, description="图层名，默认 WALL")


@mcp.tool(
    name="cad_draw_circle",
    annotations={
        "title": "画圆",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_draw_circle(params: DrawCircleInput) -> str:
    """画圆（圆心 + 半径），可用于柱、圆桌等。

    Args:
        params (DrawCircleInput): 圆心坐标与半径。

    Returns:
        str: JSON，含图元句柄。
    """
    try:
        r = _get_backend().draw_circle(params.cx, params.cy, params.radius, params.layer)
        return _ok({"ok": True, **r})
    except Exception as e:
        return _fail(e)


class DrawLineInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    x1: float = Field(..., description="直线起点 X（毫米）")
    y1: float = Field(..., description="直线起点 Y（毫米）")
    x2: float = Field(..., description="直线终点 X（毫米）")
    y2: float = Field(..., description="直线终点 Y（毫米）")
    layer: Optional[str] = Field(default=None, description="图层名，留空用当前活动图层")


@mcp.tool(
    name="cad_draw_line",
    annotations={
        "title": "画直线",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_draw_line(params: DrawLineInput) -> str:
    """画一条直线（两点）。用于斜壁、水面线、辅助线等任意线段，比画墙更灵活。

    Args:
        params (DrawLineInput): 起终点坐标与图层。

    Returns:
        str: JSON，含图元句柄。
    """
    try:
        r = _get_backend().draw_line(params.x1, params.y1, params.x2, params.y2, params.layer)
        return _ok({"ok": True, **r})
    except Exception as e:
        return _fail(e)


class DrawArcInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    cx: float = Field(..., description="圆心 X（毫米）")
    cy: float = Field(..., description="圆心 Y（毫米）")
    radius: float = Field(..., description="半径（毫米）", gt=0, le=10000)
    start_angle: float = Field(..., description="起始角（度，X 正轴起逆时针）", ge=-360, le=360)
    end_angle: float = Field(..., description="终止角（度）", ge=-360, le=360)
    layer: Optional[str] = Field(default=None, description="图层名，留空用当前活动图层")


@mcp.tool(
    name="cad_draw_arc",
    annotations={
        "title": "画圆弧",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_draw_arc(params: DrawArcInput) -> str:
    """画一段圆弧（圆心 + 半径 + 起止角，角度单位为度）。

    Args:
        params (DrawArcInput): 圆心、半径、起止角。

    Returns:
        str: JSON，含图元句柄。
    """
    try:
        r = _get_backend().draw_arc(params.cx, params.cy, params.radius,
                                    params.start_angle, params.end_angle, params.layer)
        return _ok({"ok": True, **r})
    except Exception as e:
        return _fail(e)


class DrawPolylineInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    points: List[Tuple[float, float]] = Field(
        ...,
        description="多段线顶点列表，如 [[0,0],[1000,0],[1000,1000]]，至少 2 个点",
        min_length=2,
        max_length=10_000,
    )
    close: bool = Field(default=False, description="是否闭合（首尾相连），默认否")
    layer: Optional[str] = Field(default=None, description="图层名，留空用当前活动图层")


@mcp.tool(
    name="cad_draw_polyline",
    annotations={
        "title": "画多段线",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_draw_polyline(params: DrawPolylineInput) -> str:
    """画多段线：依次连接多个顶点，可选闭合。用于斜壁轮廓、不规则边界等。

    Args:
        params (DrawPolylineInput): 顶点列表、是否闭合、图层。

    Returns:
        str: JSON，含图元句柄。
    """
    try:
        pts = [tuple(p) for p in params.points]
        r = _get_backend().draw_polyline(pts, params.close, params.layer)
        return _ok({"ok": True, **r})
    except Exception as e:
        return _fail(e)


class DrawHatchInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    boundary_handle: Optional[str] = Field(
        default=None,
        description="单个已有闭合边界的十进制句柄；与 boundary_handles 二选一，不接受顶点列表",
        min_length=1,
        max_length=40,
        pattern=r"^[0-9]+$",
    )
    boundary_handles: Optional[List[str]] = Field(
        default=None, min_length=1, max_length=64,
        description="组合区域的已有图元句柄，支持二维直线、圆弧、圆和含圆弧多段线；与 boundary_handle 二选一",
    )
    seed_point: Optional[Tuple[float, float]] = Field(
        default=None, description="组合模式必填的 WCS XY 区域内定位点（毫米）；只选择封闭区域，不作为边界顶点",
    )
    pattern: str = Field(default="ANSI31", description="预定义填充图案名，如 ANSI31、ANSI32、AR-CONC；SOLID 为实心", min_length=1, max_length=255)
    scale: float = Field(default=1.0, description="图案比例，越小越密；SOLID 必须为 1", gt=0, le=1000)
    angle: float = Field(default=0.0, description="图案旋转角度（度）；SOLID 必须为 0", ge=-360, le=360)
    layer: str = Field(default=LAYER_FILL, description="填充图层，默认 FILL", min_length=1, max_length=255)
    associative: bool = Field(default=False, description="是否关联已有边界；边界始终保留")
    dry_run: bool = Field(default=True, description="默认 true 只预览；确认范围和图案后使用 false 实际写入")

    @model_validator(mode="after")
    def validate_pattern_parameters(self) -> "DrawHatchInput":
        self.pattern, self.scale, self.angle = validate_hatch_parameters(self.pattern, self.scale, self.angle)
        if (self.boundary_handle is None) == (self.boundary_handles is None):
            raise ValueError("boundary_handle 与 boundary_handles 必须且只能提供一个")
        if self.boundary_handles is not None:
            validate_handles(self.boundary_handles, self.seed_point, self.associative)
        elif self.seed_point is not None:
            raise ValueError("seed_point 仅用于 boundary_handles 组合边界模式")
        return self


@mcp.tool(
    name="cad_draw_hatch",
    annotations={
        "title": "按已有闭合边界创建图案填充",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_draw_hatch(params: DrawHatchInput) -> str:
    """按已有图元构成的闭合区域创建原生 HATCH，设置图案、比例和角度。

    boundary_handle 接受单个闭合图元；boundary_handles + seed_point 选择
    多条直线、圆弧、圆及二维多段线共同围成的区域，独立内圈自动留空。
    组合模式保留真实圆弧，不补断口，拒绝自交、重叠、悬挂和不共面的边界。
    组合模式当前要求 associative=false，所有用户图元保留，辅助边界自动清理。
    首先 dry_run=true 预览实际边界、面积和内孔，再改 false 创建并回读验证。
    AutoCAD 预览无法确认图案在本机是否可用，此项在正式创建时验证。
    """
    try:
        r = _get_backend().draw_hatch(
            boundary_handle=params.boundary_handle,
            boundary_handles=params.boundary_handles,
            seed_point=params.seed_point,
            pattern=params.pattern,
            scale=params.scale,
            angle=params.angle,
            layer=params.layer,
            associative=params.associative,
            dry_run=params.dry_run,
        )
        return _ok({"ok": True, **r})
    except Exception as e:
        return _fail(e)


class FillClosedBoundarySolidInput(ToolInput):
    boundary_handle: Optional[str] = Field(
        description="已有闭合边界句柄；与 points 必须且只能提供一个",
        min_length=1,
        max_length=40,
    )
    points: Optional[List[Tuple[float, float]]] = Field(
        description="显式闭合多边形顶点；与 boundary_handle 必须且只能提供一个",
        min_length=3,
        max_length=1_000,
    )
    layer: str = Field(min_length=1, max_length=255)
    associative: bool = Field(description="是否关联边界，必须显式指定")
    keep_boundary: bool = Field(description="是否保留边界，必须显式指定")
    dry_run: bool = Field(description="必须先用 true 预览；false 才实际写入")

    @model_validator(mode="after")
    def validate_boundary_source(self) -> "FillClosedBoundarySolidInput":
        if (self.boundary_handle is None) == (self.points is None):
            raise ValueError("boundary_handle 与 points 必须且只能提供一个")
        if self.boundary_handle is not None and not self.keep_boundary:
            raise ValueError("使用已有边界时必须 keep_boundary=true，工具不会删除用户边界")
        if self.associative and not self.keep_boundary:
            raise ValueError("关联填充必须 keep_boundary=true")
        return self


@mcp.tool(
    name="cad_fill_closed_boundary_solid",
    annotations={
        "title": "封闭边界实心填充并回读验证",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_fill_closed_boundary_solid(params: FillClosedBoundarySolidInput) -> str:
    """创建 SOLID HATCH，并回读图案、边界、关联性、面积和包围盒。"""
    try:
        return _ok({"ok": True, **_get_backend().fill_closed_boundary_solid(
            boundary_handle=params.boundary_handle,
            points=params.points,
            layer=params.layer,
            associative=params.associative,
            keep_boundary=params.keep_boundary,
            dry_run=params.dry_run,
        )})
    except Exception as e:
        return _fail(e)


class DrawTextInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    text: str = Field(..., description="文字内容（如房间名）", min_length=1, max_length=200)
    x: float = Field(..., description="文字插入点 X（毫米）")
    y: float = Field(..., description="文字插入点 Y（毫米）")
    height: Optional[float] = Field(default=None, description="字高（毫米）；留空继承当前文字样式/当前文字高度，不按图形范围自动计算", gt=0, le=1000000)
    rotation: float = Field(default=0, description="文字旋转角（度），默认 0", ge=-360, le=360)
    layer: Optional[str] = Field(default=LAYER_TEXT, description="图层名，默认 TEXT")


@mcp.tool(
    name="cad_draw_text",
    annotations={
        "title": "写文字标注",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_draw_text(params: DrawTextInput) -> str:
    """在图纸上写文字（房间名、说明等）。

    Args:
        params (DrawTextInput): 文字内容、插入点、字高。

    Returns:
        str: JSON，含图元句柄。
    """
    try:
        r = _get_backend().draw_text(params.text, params.x, params.y, params.height,
                                     params.rotation, params.layer)
        return _ok({"ok": True, **r})
    except Exception as e:
        return _fail(e)


class DrawDimensionInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    p1x: float = Field(..., description="被标注第一点 X")
    p1y: float = Field(..., description="被标注第一点 Y")
    p2x: float = Field(..., description="被标注第二点 X")
    p2y: float = Field(..., description="被标注第二点 Y")
    offset: float = Field(default=500, description="标注线离被标注点的距离（毫米），默认 500", gt=0, le=100000)
    direction: Literal["horizontal", "vertical", "aligned"] = Field(
        default="horizontal", description="标注方向：'horizontal' 水平（标注线在下方）；'vertical' 垂直（标注线在右侧）；'aligned' 对齐（标注线平行于两点连线，测量直线距离）")
    layer: Optional[str] = Field(default=LAYER_DIM, description="图层名，默认 DIM")


@mcp.tool(
    name="cad_draw_dimension",
    annotations={
        "title": "尺寸标注",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_draw_dimension(params: DrawDimensionInput) -> str:
    """兼容性尺寸标注：由 offset 推算尺寸线位置，旧流程可继续使用。
    direction='horizontal' 测水平距离（标注线在下方 offset）；'vertical' 测垂直距离（标注线在右侧 offset）。

    Args:
        params (DrawDimensionInput): 被标注两点、标注线偏移、方向、图层。

    Returns:
        str: JSON，含标注值。
    """
    try:
        r = _get_backend().draw_dimension(params.p1x, params.p1y, params.p2x, params.p2y,
                                          params.offset, params.direction, params.layer)
        return _ok({"ok": True, **r})
    except Exception as e:
        return _fail(e)


class DrawDimensionAtInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    p1x: float = Field(..., description="被标注第一点 X")
    p1y: float = Field(..., description="被标注第一点 Y")
    p2x: float = Field(..., description="被标注第二点 X")
    p2y: float = Field(..., description="被标注第二点 Y")
    line_x: float = Field(..., description="尺寸线通过点 X；由 Agent 按工程规范明确给出")
    line_y: float = Field(..., description="尺寸线通过点 Y；由 Agent 按工程规范明确给出")
    direction: Literal["horizontal", "vertical", "aligned"] = Field(
        default="horizontal", description="尺寸方向；工具不会自行改变方向")
    layer: Optional[str] = Field(default=LAYER_DIM, description="图层名，默认 DIM")


@mcp.tool(
    name="cad_draw_dimension_at",
    annotations={
        "title": "按指定位置画尺寸",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_draw_dimension_at(params: DrawDimensionAtInput) -> str:
    """按调用方明确给出的尺寸线位置绘制尺寸，MCP 不参与位置与方向决策。"""
    try:
        r = _get_backend().draw_dimension_at(
            params.p1x, params.p1y, params.p2x, params.p2y,
            params.line_x, params.line_y, params.direction, params.layer)
        return _ok({"ok": True, **r})
    except Exception as e:
        return _fail(e)


class DrawAngularDimensionInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    vertex_x: float = Field(..., description="角顶点 X（两条线交点）")
    vertex_y: float = Field(..., description="角顶点 Y（两条线交点）")
    p1x: float = Field(..., description="第一条线上一点 X（非顶点）")
    p1y: float = Field(..., description="第一条线上一点 Y（非顶点）")
    p2x: float = Field(..., description="第二条线上一点 X（非顶点）")
    p2y: float = Field(..., description="第二条线上一点 Y（非顶点）")
    loc_x: float = Field(..., description="标注文字/弧位置 X（大致放在夹角内）")
    loc_y: float = Field(..., description="标注文字/弧位置 Y（大致放在夹角内）")
    layer: Optional[str] = Field(default=LAYER_DIM, description="图层名，默认 DIM")


@mcp.tool(
    name="cad_draw_angular_dimension",
    annotations={
        "title": "角度标注",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_draw_angular_dimension(params: DrawAngularDimensionInput) -> str:
    """画角度标注：测量两条线（角顶点 vertex 分别到 p1、p2）的夹角。用于斜线、管道拐角等。

    Args:
        params (DrawAngularDimensionInput): 角顶点、两线上各一点、标注位置。

    Returns:
        str: JSON，含标注句柄。
    """
    try:
        r = _get_backend().draw_angular_dimension(
            params.vertex_x, params.vertex_y, params.p1x, params.p1y,
            params.p2x, params.p2y, params.loc_x, params.loc_y, params.layer)
        return _ok({"ok": True, **r})
    except Exception as e:
        return _fail(e)


class DrawRadiusDimensionInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    cx: float = Field(..., description="圆心 X")
    cy: float = Field(..., description="圆心 Y")
    edge_x: float = Field(..., description="圆上一点 X（标注引线方向）")
    edge_y: float = Field(..., description="圆上一点 Y（标注引线方向）")
    layer: Optional[str] = Field(default=LAYER_DIM, description="图层名，默认 DIM")


@mcp.tool(
    name="cad_draw_radius_dimension",
    annotations={
        "title": "半径标注",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_draw_radius_dimension(params: DrawRadiusDimensionInput) -> str:
    """画半径标注：测量圆心到圆上一点的距离（如圆形水池、弯管），自动标注 R 值。

    Args:
        params (DrawRadiusDimensionInput): 圆心与圆上一点。

    Returns:
        str: JSON，含标注句柄与半径值。
    """
    try:
        r = _get_backend().draw_radius_dimension(
            params.cx, params.cy, params.edge_x, params.edge_y, params.layer)
        return _ok({"ok": True, **r})
    except Exception as e:
        return _fail(e)


class DrawDiameterDimensionInput(ToolInput):
    cx: float = Field(..., description="圆心 X")
    cy: float = Field(..., description="圆心 Y")
    edge_x: float = Field(..., description="圆上一点 X，确定直径标注方向")
    edge_y: float = Field(..., description="圆上一点 Y，确定直径标注方向")
    layer: str = Field(default=LAYER_DIM, description="图层名，默认 DIM", min_length=1)
    text_x: Optional[float] = Field(default=None, description="可选文字位置 X，须与 text_y 同时提供")
    text_y: Optional[float] = Field(default=None, description="可选文字位置 Y，须与 text_x 同时提供")

    @model_validator(mode="after")
    def validate_geometry(self):
        validate_diameter_dimension(
            self.cx, self.cy, self.edge_x, self.edge_y, self.text_x, self.text_y)
        return self


@mcp.tool(
    name="cad_draw_diameter_dimension",
    annotations={
        "title": "圆形直径标注",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_draw_diameter_dimension(params: DrawDiameterDimensionInput) -> str:
    """创建原生圆形直径尺寸，自动显示直径符号并继承当前标注样式。

    使用圆心和圆上一点确定几何直径，可同时给出 text_x/text_y 指定文字位置。
    支持 DXF 和 AutoCAD；返回的 diameter 为模型几何值，显示单位由标注样式控制。
    本工具按坐标创建标注，不与已有圆实体建立自动更新关联。
    """
    try:
        r = _get_backend().draw_diameter_dimension(
            params.cx, params.cy, params.edge_x, params.edge_y, params.layer,
            text_x=params.text_x, text_y=params.text_y)
        return _ok({"ok": True, **r})
    except Exception as e:
        return _fail(e)


# ================= 工具：图层 =================

class CreateLayerInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(..., description="图层名（建议大写英文，如 WATER）", min_length=1, max_length=40)
    color: int = Field(default=7, description="AutoCAD 索引色 1-255，默认 7（白/黑）", ge=1, le=255)
    linetype: str = Field(default="CONTINUOUS",
                          description="线型：CONTINUOUS 实线 / DASHED 虚线 / HIDDEN 隐藏线 / CENTER 点划线 / DOT 点线 / PHANTOM",
                          max_length=40)
    lineweight: int = Field(default=25, description="线宽（1/100mm）：25=0.25mm 细线，50=0.5mm 粗线", ge=0, le=211)
    plot: bool = Field(default=True, description="是否参与出图")
    description: str = Field(default="", description="图层说明", max_length=1024)
    locked: Optional[bool] = Field(default=None, description="是否锁定；留空保持后端默认")
    transparency: Optional[int] = Field(default=None, description="图层透明度百分比 0-90", ge=0, le=90)


@mcp.tool(
    name="cad_create_layer",
    annotations={
        "title": "创建图层",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def cad_create_layer(params: CreateLayerInput) -> str:
    """创建图层（可指定颜色、线型、线宽）；图层已存在则更新属性。

    工程图分线常用：轴线用 CENTER 点划线、隐藏线用 DASHED/HIDDEN 虚线、墙体轮廓用粗线（lineweight=50）。

    Args:
        params (CreateLayerInput): 图层名、颜色、线型、线宽。

    Returns:
        str: JSON。
    """
    try:
        return _ok({"ok": True, "message": _get_backend().create_layer(
            params.name, params.color, params.linetype, params.lineweight,
            params.plot, params.description, params.locked, params.transparency)})
    except Exception as e:
        return _fail(e)


class SetLayerPropsInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(..., description="要修改的图层名", min_length=1, max_length=40)
    color: Optional[int] = Field(default=None, description="新颜色（AutoCAD 索引色 1-255），留空不改", ge=1, le=255)
    linetype: Optional[str] = Field(default=None, description="新线型（CONTINUOUS/DASHED/HIDDEN/CENTER/DOT/PHANTOM），留空不改",
                                    max_length=40)
    lineweight: Optional[int] = Field(default=None, description="新线宽（1/100mm），留空不改", ge=0, le=211)
    plot: Optional[bool] = Field(default=None, description="是否参与出图；留空不改")
    description: Optional[str] = Field(default=None, description="图层说明；留空不改", max_length=1024)
    locked: Optional[bool] = Field(default=None, description="是否锁定；留空不改")
    frozen: Optional[bool] = Field(default=None, description="是否冻结；留空不改")
    transparency: Optional[int] = Field(default=None, description="透明度百分比 0-90；留空不改", ge=0, le=90)


@mcp.tool(
    name="cad_set_layer_props",
    annotations={
        "title": "修改图层属性",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def cad_set_layer_props(params: SetLayerPropsInput) -> str:
    """修改已有图层的颜色/线型/线宽。未指定的属性保持不变。

    例如把某个图层改成浅色虚线：cad_set_layer_props(name='WATER', color=8, linetype='DASHED')。

    Args:
        params (SetLayerPropsInput): 图层名与要改的属性。

    Returns:
        str: JSON。
    """
    try:
        return _ok({"ok": True, "message": _get_backend().set_layer_props(
            params.name, params.color, params.linetype, params.lineweight,
            params.plot, params.description, params.locked, params.frozen,
            params.transparency)})
    except Exception as e:
        return _fail(e)


class SetActiveLayerInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(..., description="要切换到的图层名（不存在则自动创建）", min_length=1, max_length=40)


@mcp.tool(
    name="cad_set_active_layer",
    annotations={
        "title": "切换当前图层",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def cad_set_active_layer(params: SetActiveLayerInput) -> str:
    """切换当前活动图层，之后绘制的图元都放入该图层。

    Args:
        params (SetActiveLayerInput): 目标图层名。

    Returns:
        str: JSON。
    """
    try:
        return _ok({"ok": True, "message": _get_backend().set_active_layer(params.name)})
    except Exception as e:
        return _fail(e)


class CreateOrUpdateTextStyleInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(..., description="文字样式名称", min_length=1, max_length=40)
    model_height: float = Field(
        ...,
        description="模型空间文字样式高度；0 表示可变字高并保留当前 TEXTSIZE",
        ge=0,
        le=1000000,
    )
    font_file: str = Field(default="gbenor.shx", description="字体文件", min_length=1, max_length=120)
    big_font_file: str = Field(default="gbcbig.shx", description="中文大字体文件", max_length=120)
    width_factor: float = Field(default=1.0, description="字宽系数", gt=0, le=10)
    set_current: bool = Field(default=True, description="是否设为当前文字样式")


@mcp.tool(
    name="cad_create_or_update_textstyle",
    annotations={
        "title": "创建或更新文字样式",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def cad_create_or_update_textstyle(params: CreateOrUpdateTextStyleInput) -> str:
    """创建文字样式；支持固定字高及 model_height=0 的可变字高。"""
    try:
        result = _get_backend().create_or_update_textstyle(
            params.name, params.model_height, params.font_file,
            params.big_font_file, params.width_factor, params.set_current)
        return _ok({"ok": True, **result})
    except Exception as e:
        return _fail(e)


class CreateOrUpdateDimStyleInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(..., description="尺寸样式名称", min_length=1, max_length=40)
    overall_scale: float = Field(..., description="DIMSTYLE 全局比例 DIMSCALE，由 Agent 决定", gt=0, le=1000000)
    text_height: float = Field(default=3.5, description="图纸字高 DIMTXT（毫米）", gt=0, le=1000)
    arrow_size: float = Field(default=2.5, description="图纸箭头大小 DIMASZ（毫米）", gt=0, le=1000)
    oblique_stroke_size: float = Field(
        default=0.0,
        description="斜短线大小 DIMTSZ（图纸毫米）；0 使用箭头，>0 使用斜短线",
        ge=0,
        le=1000,
    )
    linear_scale_factor: float = Field(
        default=1.0,
        description="线性尺寸测量换算系数 DIMLFAC；1 不换算，0.001 将毫米显示为米",
        gt=0,
        le=1000000,
    )
    text_gap: float = Field(default=0.8, description="图纸文字间隙 DIMGAP（毫米）", ge=0, le=1000)
    precision: int = Field(default=2, description="线性尺寸小数位数 DIMDEC", ge=0, le=8)
    text_above: bool = Field(default=True, description="尺寸文字是否位于尺寸线上方")
    text_style: Optional[str] = Field(default=None, description="尺寸使用的文字样式；留空保持当前设置", max_length=40)
    set_current: bool = Field(default=True, description="是否设为当前尺寸样式")
    suppress_leading_zeros: bool = Field(default=False, description="是否抑制前导零")
    suppress_trailing_zeros: bool = Field(default=False, description="是否抑制尾零；False 时 12.5 按 precision=2 显示 12.50")
    decimal_separator: Literal[".", ","] = Field(default=".", description="小数分隔符")
    extension_line_offset: float = Field(default=0.625, description="尺寸界线起点偏移 DIMEXO", ge=0, le=1000000)
    extension_line_extension: float = Field(default=1.25, description="尺寸界线超出量 DIMEXE", ge=0, le=1000000)
    baseline_spacing: float = Field(default=3.75, description="基线尺寸间距 DIMDLI", ge=0, le=1000000)
    dimension_line_color: int = Field(default=256, description="尺寸线颜色 DIMCLRD，256=随层", ge=0, le=256)
    extension_line_color: int = Field(default=256, description="尺寸界线颜色 DIMCLRE，256=随层", ge=0, le=256)
    text_color: int = Field(default=256, description="尺寸文字颜色 DIMCLRT，256=随层", ge=0, le=256)
    dimension_line_lineweight: int = Field(default=-1, description="尺寸线线宽 DIMLWD，-1=随层", ge=-3, le=211)
    extension_line_lineweight: int = Field(default=-1, description="尺寸界线线宽 DIMLWE，-1=随层", ge=-3, le=211)


@mcp.tool(
    name="cad_create_or_update_dimstyle",
    annotations={
        "title": "创建或更新尺寸样式",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def cad_create_or_update_dimstyle(params: CreateOrUpdateDimStyleInput) -> str:
    """创建尺寸样式，并明确配置终止符与线性尺寸测量换算系数。"""
    try:
        result = _get_backend().create_or_update_dimstyle(
            name=params.name,
            overall_scale=params.overall_scale,
            text_height=params.text_height,
            arrow_size=params.arrow_size,
            text_gap=params.text_gap,
            precision=params.precision,
            text_above=params.text_above,
            text_style=params.text_style,
            set_current=params.set_current,
            oblique_stroke_size=params.oblique_stroke_size,
            linear_scale_factor=params.linear_scale_factor,
            suppress_leading_zeros=params.suppress_leading_zeros,
            suppress_trailing_zeros=params.suppress_trailing_zeros,
            decimal_separator=params.decimal_separator,
            extension_line_offset=params.extension_line_offset,
            extension_line_extension=params.extension_line_extension,
            baseline_spacing=params.baseline_spacing,
            dimension_line_color=params.dimension_line_color,
            extension_line_color=params.extension_line_color,
            text_color=params.text_color,
            dimension_line_lineweight=params.dimension_line_lineweight,
            extension_line_lineweight=params.extension_line_lineweight,
        )
        return _ok({"ok": True, **result})
    except Exception as e:
        return _fail(e)


class SetLinetypeSettingsInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    ltscale: float = Field(..., description="全局线型比例 LTSCALE，由 Agent 明确给出", gt=0, le=1000000)
    celtscale: float = Field(default=1.0, description="当前实体线型比例 CELTSCALE", gt=0, le=1000000)
    msltscale: Literal[0, 1] = Field(default=0, description="模型空间线型比例开关 MSLTSCALE")
    psltscale: Literal[0, 1] = Field(default=1, description="图纸空间线型比例开关 PSLTSCALE")
    regen: bool = Field(default=True, description="设置后是否重生成显示")


@mcp.tool(
    name="cad_set_linetype_settings",
    annotations={
        "title": "设置完整线型环境",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def cad_set_linetype_settings(params: SetLinetypeSettingsInput) -> str:
    """设置 LTSCALE/CELTSCALE/MSLTSCALE/PSLTSCALE；不做图幅或比例决策。"""
    try:
        result = _get_backend().set_linetype_settings(
            params.ltscale, params.celtscale, params.msltscale,
            params.psltscale, params.regen)
        return _ok({"ok": True, **result})
    except Exception as e:
        return _fail(e)


class GetDrawingSettingsInput(ToolInput):
    pass


@mcp.tool(
    name="cad_get_drawing_settings",
    annotations={
        "title": "读取图纸级单位和关键设置",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def cad_get_drawing_settings(params: GetDrawingSettingsInput) -> str:
    """读取明确列出的系统设置；无法核实的字段进入 unverified_fields。"""
    del params
    try:
        return _ok({"ok": True, **_get_backend().get_drawing_settings()})
    except Exception as e:
        return _fail(e)


class ListLayersDetailedInput(ToolInput):
    pass


@mcp.tool(
    name="cad_list_layers_detailed",
    annotations={
        "title": "详细列出图层及实体数量",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def cad_list_layers_detailed(params: ListLayersDetailedInput) -> str:
    """只读返回图层颜色、线型、线宽、打印状态和保护状态。"""
    del params
    try:
        return _ok({"ok": True, **_get_backend().list_layers_detailed()})
    except Exception as e:
        return _fail(e)


class ListTextstylesInput(ToolInput):
    pass


@mcp.tool(
    name="cad_list_textstyles",
    annotations={
        "title": "列出文字样式及实际属性",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def cad_list_textstyles(params: ListTextstylesInput) -> str:
    """只读返回文字样式；后端未暴露的属性不作推断。"""
    del params
    try:
        return _ok({"ok": True, **_get_backend().list_textstyles()})
    except Exception as e:
        return _fail(e)


class GetDraftingEnvironmentInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)


@mcp.tool(
    name="cad_get_drafting_environment",
    annotations={
        "title": "读取当前制图环境",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def cad_get_drafting_environment(params: GetDraftingEnvironmentInput) -> str:
    """读取当前文字样式、尺寸样式全局比例和线型变量。"""
    try:
        return _ok({"ok": True, **_get_backend().get_drafting_environment()})
    except Exception as e:
        return _fail(e)


class SetLinetypeScaleInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    scale: float = Field(..., description="线型比例因子；必须由 Agent 明确给出，不再自动推算", gt=0, le=1000000)


@mcp.tool(
    name="cad_set_linetype_scale",
    annotations={
        "title": "设置线型比例",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def cad_set_linetype_scale(params: SetLinetypeScaleInput) -> str:
    """兼容性工具：显式设置 LTSCALE。新流程优先用 cad_set_linetype_settings。

    Args:
        params (SetLinetypeScaleInput): scale —— Agent 已决定的线型比例因子。

    Returns:
        str: JSON。
    """
    try:
        return _ok({"ok": True, "message": _get_backend().set_linetype_scale(params.scale)})
    except Exception as e:
        return _fail(e)


# ================= 工具：查询 / 修改 =================

class GetGeometryExtentsInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)


@mcp.tool(
    name="cad_get_geometry_extents",
    annotations={
        "title": "读取图形总范围",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def cad_get_geometry_extents(params: GetGeometryExtentsInput) -> str:
    """读取完成后图形的总包围盒；只提供数据，不替 Agent 选择比例。"""
    try:
        return _ok({"ok": True, **_get_backend().get_geometry_extents()})
    except Exception as e:
        return _fail(e)


class EvaluateViewportFitInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    paper_width: float = Field(..., description="图纸总宽度（毫米）", gt=0, le=100000)
    paper_height: float = Field(..., description="图纸总高度（毫米）", gt=0, le=100000)
    margin_left: float = Field(default=0, description="左侧不可用宽度（毫米）", ge=0)
    margin_right: float = Field(default=0, description="右侧不可用宽度（毫米）", ge=0)
    margin_top: float = Field(default=0, description="上侧不可用高度（毫米）", ge=0)
    margin_bottom: float = Field(default=0, description="下侧不可用高度（毫米）", ge=0)
    scale_denominator: Optional[float] = Field(
        default=None, description="待校核的比例分母，如 200 表示 1:200；留空只返回最小所需分母", gt=0)


@mcp.tool(
    name="cad_evaluate_viewport_fit",
    annotations={
        "title": "校核图形能否放入图幅",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def cad_evaluate_viewport_fit(params: EvaluateViewportFitInput) -> str:
    """按图形范围和可用图幅做确定性计算；不推荐、不选择标准比例。"""
    try:
        ext = _get_backend().get_geometry_extents()
        if ext.get("empty"):
            return _ok({"ok": True, "empty": True})
        usable_w = params.paper_width - params.margin_left - params.margin_right
        usable_h = params.paper_height - params.margin_top - params.margin_bottom
        if usable_w <= 0 or usable_h <= 0:
            raise ValueError("扣除边距后图幅可用宽度和高度必须大于 0")
        required = max(float(ext["width"]) / usable_w,
                       float(ext["height"]) / usable_h)
        result: Dict[str, Any] = {
            "ok": True,
            "geometry": ext,
            "usable_paper": {"width": usable_w, "height": usable_h},
            "minimum_scale_denominator": required,
        }
        if params.scale_denominator is not None:
            drawn_w = float(ext["width"]) / params.scale_denominator
            drawn_h = float(ext["height"]) / params.scale_denominator
            result["evaluation"] = {
                "scale_denominator": params.scale_denominator,
                "fits": drawn_w <= usable_w and drawn_h <= usable_h,
                "paper_size_used": {"width": drawn_w, "height": drawn_h},
                "remaining": {"width": usable_w - drawn_w, "height": usable_h - drawn_h},
            }
        return _ok(result)
    except Exception as e:
        return _fail(e)


class QueryEntitiesInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    layer: Optional[str] = Field(default=None, description="图层过滤；留空不过滤", max_length=40)
    entity_type: Optional[str] = Field(default=None, description="实体类型过滤，如 LINE/CIRCLE/AcDbLine", max_length=40)
    offset: int = Field(default=0, description="从模型空间索引开始扫描", ge=0)
    limit: int = Field(default=50, description="可选，默认 50，范围 1–100；继续分页必须使用返回的 next_offset", ge=1, le=100)
    include_bounds: bool = Field(default=False, description="是否读取每个实体包围盒；会增加 COM 调用")


@mcp.tool(
    name="cad_query_entities",
    annotations={
        "title": "分页查询结构化实体",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def cad_query_entities(params: QueryEntitiesInput) -> str:
    """分页查询实体，返回 next_offset；避免一次列出整张大图。"""
    try:
        result = _get_backend().query_entities(
            params.layer, params.entity_type, params.offset,
            params.limit, params.include_bounds)
        return _ok({"ok": True, **result})
    except Exception as e:
        return _fail(e)


class RegenInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)


@mcp.tool(
    name="cad_regen",
    annotations={
        "title": "重生成图形显示",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def cad_regen(params: RegenInput) -> str:
    """显式重生成显示，通常在线型或样式设置后使用。"""
    try:
        return _ok({"ok": True, **_get_backend().regen()})
    except Exception as e:
        return _fail(e)


class HealthInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    reconnect: bool = Field(default=False, description="acad 模式下是否主动尝试建立/恢复连接")


@mcp.tool(
    name="cad_health",
    annotations={
        "title": "检查 CAD 连接状态",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def cad_health(params: HealthInput) -> str:
    """检查连接和空闲状态；reconnect=True 时允许恢复连接。"""
    try:
        status = _get_backend().health(params.reconnect)
        return _ok({
            "ok": True,
            "tool_count": len(mcp._tool_manager.list_tools()),
            **status,
        })
    except Exception as e:
        return _fail(e)


class ListEntitiesInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    layer: Optional[str] = Field(default=None, description="仅列出该图层（留空列出全部）", max_length=40)
    offset: int = Field(default=0, ge=0, description="匹配结果起始位置")
    limit: int = Field(default=100, ge=1, le=500, description="单次最多返回数量")


@mcp.tool(
    name="cad_list_entities",
    annotations={
        "title": "列出图纸图元",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def cad_list_entities(params: ListEntitiesInput) -> str:
    """分页列出当前图纸图元，含句柄、类型、图层。句柄用于后续移动/删除。

    Args:
        params (ListEntitiesInput): 可选图层过滤。

    Returns:
        str: 每行一个图元。
    """
    try:
        return _get_backend().list_entities(params.layer, params.offset, params.limit)
    except Exception as e:
        return _fail(e)


class ExplodeInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    handle: str = Field(..., description="要分解的复合图元句柄", min_length=1, max_length=40)


@mcp.tool(
    name="cad_explode",
    annotations={
        "title": "分解复合图元",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_explode(params: ExplodeInput) -> str:
    """分解复合图元并删除源图元，返回所有新图元的类型和句柄。

    acad 模式优先调用图元的直接 COM Explode；由于 AutoCAD 的 MLINE 没有该 COM
    方法，MLINE 会受控发送一次 EXPLODE 命令，并等待、核验命令完成。
    """
    try:
        return _ok({"ok": True, **_get_backend().explode(params.handle)})
    except Exception as e:
        return _fail(e)


class JoinPolylineInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    handles: List[str] = Field(
        ..., description="待合并的 LINE / ARC / LWPOLYLINE / 2D POLYLINE 句柄",
        min_length=1, max_length=100)
    close: bool = Field(default=False, description="是否要求首尾闭合；true 时会设置真正的 Closed 标志")
    tolerance: float = Field(
        default=1e-6, description="端点连接容差（毫米）；超过容差不强行连接", gt=0, le=100000)
    layer: Optional[str] = Field(
        default=None, description="结果图层；留空继承首个源图元图层")


@mcp.tool(
    name="cad_join_polyline",
    annotations={
        "title": "合并为多段线",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_join_polyline(params: JoinPolylineInput) -> str:
    """按端点自动排序并合并为 LWPOLYLINE，成功后删除源图元。

    支持直线、圆弧、轻量/二维多段线；会保留 ARC 和多段线圆弧段的 bulge。
    遇到分叉、断点或无法唯一排序时明确失败，不猜测连接方式。
    """
    try:
        return _ok({"ok": True, **_get_backend().join_polyline(
            params.handles, params.close, params.tolerance, params.layer)})
    except Exception as e:
        return _fail(e)


class GetEntityPropertiesInput(ToolInput):
    handle: str = Field(..., pattern=r"^[0-9]{1,40}$", description="模型空间实体的十进制句柄")


@mcp.tool(
    name="cad_get_entity_properties",
    annotations={"title": "读取已有实体属性", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def cad_get_entity_properties(params: GetEntityPropertiesInput) -> str:
    """按十进制句柄读取实体属性和 editable_properties 白名单；不修改图纸。"""
    try:
        return _ok({"ok": True, **_get_backend().get_entity_properties(params.handle)})
    except Exception as e:
        return _fail(e)


class EditEntityPropertiesInput(GetEntityPropertiesInput):
    properties: EntityPropertyPatch = Field(..., description="仅包含要修改的属性，禁止 null 和未知字段")
    dry_run: bool = Field(default=True, strict=True, description="默认只预览；false 正式修改并回读，不自动保存")


@mcp.tool(
    name="cad_edit_entity_properties",
    annotations={"title": "编辑已有实体属性", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
)
def cad_edit_entity_properties(params: EditEntityPropertiesInput) -> str:
    """编辑模型空间实体。支持外观、文字、直线、圆，以及 ARC/LWPOLYLINE/INSERT/HATCH/ELLIPSE/POINT。

    新增几何属性仅用 XY，保留原标高和 Z 比例。块内容按 block_attributes 标签或
    block_texts 路径修改，固定文字克隆定义后仅重绑定选中实例，保留同名其他块。

    先以 dry_run=true 检查 changes，再用相同 properties 和 dry_run=false 应用。
    图层和线型必须已存在；拒绝锁定图层及不适用字段。失败回滚，不自动保存。
    """
    try:
        return _ok({"ok": True, **_get_backend().edit_entity_properties(
            params.handle, params.properties.model_dump(exclude_unset=True), dry_run=params.dry_run)})
    except Exception as e:
        return _fail(e)


class DeleteEntityInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    handle: str = Field(..., description="要删除图元的句柄（从 cad_list_entities 获取）", min_length=1, max_length=40)


@mcp.tool(
    name="cad_delete_entity",
    annotations={
        "title": "删除图元",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_delete_entity(params: DeleteEntityInput) -> str:
    """删除指定句柄的图元（破坏性操作，删除后不可恢复）。

    Args:
        params (DeleteEntityInput): 图元句柄。

    Returns:
        str: JSON。
    """
    try:
        return _ok({"ok": True, "message": _get_backend().delete_entity(params.handle)})
    except Exception as e:
        return _fail(e)


class MoveEntityInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    handle: str = Field(..., description="要移动图元的句柄", min_length=1, max_length=40)
    dx: float = Field(..., description="X 方向位移（毫米），正右负左")
    dy: float = Field(..., description="Y 方向位移（毫米），正上负下")


@mcp.tool(
    name="cad_move_entity",
    annotations={
        "title": "移动图元",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_move_entity(params: MoveEntityInput) -> str:
    """按句柄移动图元到新位置。

    Args:
        params (MoveEntityInput): 句柄与位移量。

    Returns:
        str: JSON。
    """
    try:
        return _ok({"ok": True, "message": _get_backend().move_entity(params.handle, params.dx, params.dy)})
    except Exception as e:
        return _fail(e)


class CopyEntityInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    handle: str = Field(..., description="要复制的图元句柄（从 cad_list_entities 获取）", min_length=1, max_length=40)
    dx: float = Field(..., description="副本 X 方向偏移（毫米），正右负左")
    dy: float = Field(..., description="副本 Y 方向偏移（毫米），正上负下")


@mcp.tool(
    name="cad_copy_entity",
    annotations={
        "title": "复制图元",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_copy_entity(params: CopyEntityInput) -> str:
    """复制图元并偏移 (dx, dy)，原图元保留。返回新副本句柄（可用于继续移动/旋转等）。

    Args:
        params (CopyEntityInput): 句柄与副本偏移量。

    Returns:
        str: JSON，含新副本句柄。
    """
    try:
        r = _get_backend().copy_entity(params.handle, params.dx, params.dy)
        return _ok({"ok": True, **r})
    except Exception as e:
        return _fail(e)


class RotateEntityInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    handle: str = Field(..., description="要旋转的图元句柄", min_length=1, max_length=40)
    base_x: float = Field(..., description="旋转基点 X（毫米）")
    base_y: float = Field(..., description="旋转基点 Y（毫米）")
    angle: float = Field(..., description="旋转角度（度，逆时针为正），如 90 顺时针用 -90", ge=-360, le=360)


@mcp.tool(
    name="cad_rotate_entity",
    annotations={
        "title": "旋转图元",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_rotate_entity(params: RotateEntityInput) -> str:
    """按句柄旋转图元：绕基点 (base_x, base_y) 逆时针旋转 angle 度。

    常用于把设备、管道方向改到位（如水泵换个朝向）。

    Args:
        params (RotateEntityInput): 句柄、基点与角度。

    Returns:
        str: JSON。
    """
    try:
        return _ok({"ok": True, "message": _get_backend().rotate_entity(
            params.handle, params.base_x, params.base_y, params.angle)})
    except Exception as e:
        return _fail(e)


class MirrorEntityInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    handle: str = Field(..., description="要镜像的图元句柄", min_length=1, max_length=40)
    x1: float = Field(..., description="镜像线第一点 X")
    y1: float = Field(..., description="镜像线第一点 Y")
    x2: float = Field(..., description="镜像线第二点 X")
    y2: float = Field(..., description="镜像线第二点 Y")


@mcp.tool(
    name="cad_mirror_entity",
    annotations={
        "title": "镜像图元",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_mirror_entity(params: MirrorEntityInput) -> str:
    """按句柄镜像图元：关于直线 (x1,y1)-(x2,y2) 对称翻转。

    常用于画对称构筑物（镜像后左右两边完全一致）。

    Args:
        params (MirrorEntityInput): 句柄与镜像线两点。

    Returns:
        str: JSON。
    """
    try:
        return _ok({"ok": True, "message": _get_backend().mirror_entity(
            params.handle, params.x1, params.y1, params.x2, params.y2)})
    except Exception as e:
        return _fail(e)


# ================= 工具：图块与图框 =================

class MakeBlockInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(..., description="图块名（英文或拼音，如 PUMP1、TANK_A）", min_length=1, max_length=40)
    handles: List[str] = Field(..., description="要打包成块的图元句柄列表（从 cad_list_entities 获取）", min_length=1, max_length=10_000)
    base_x: float = Field(default=0, description="基点 X（图块的插入参考点，默认 0）")
    base_y: float = Field(default=0, description="基点 Y（图块的插入参考点，默认 0）")
    annotative: bool = Field(
        default=False,
        description="是否创建原生注释性块；仅 acad 模式支持，DXF 模式传 true 会拒绝且不写入",
    )


@mcp.tool(
    name="cad_make_block",
    annotations={
        "title": "把图元打包成图块",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_make_block(params: MakeBlockInput) -> str:
    """把多个图元打包成一个图块（整体），之后可整体移动/旋转/复制。

    例如把泵 + 底座线打包成块 PUMP1，之后说"放 3 台泵"用 cad_insert_block 三次即可。

    Args:
        params (MakeBlockInput): 块名、句柄列表、基点、注释性开关。

    Returns:
        str: JSON。
    """
    try:
        result = _get_backend().make_block(
            params.name, params.handles, params.base_x, params.base_y,
            annotative=params.annotative)
        if isinstance(result, dict):
            return _ok({"ok": True, **result})
        return _ok({"ok": True, "message": result})
    except Exception as e:
        return _fail(e)


class BlockFieldBindingInput(ToolInput):
    attribute_tag: str = Field(min_length=1, max_length=255)
    source_type: Literal["object"] = "object"
    source_handle: str = Field(min_length=1, max_length=40)
    source_property: str = Field(min_length=1, max_length=80)
    linear_scale_factor: float = Field(default=1.0, gt=0, le=1000000)
    precision: int = Field(default=3, ge=0, le=8)
    prefix: str = Field(default="", max_length=80)
    suffix: str = Field(default="", max_length=80)
    update_on_regen: bool = True


class InsertBlockInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(..., description="图块名（须先用 cad_make_block 创建）", min_length=1, max_length=40)
    x: float = Field(..., description="插入点 X（毫米）")
    y: float = Field(..., description="插入点 Y（毫米）")
    rotation: float = Field(default=0, description="旋转角（度），默认 0", ge=-360, le=360)
    scale: float = Field(default=1.0, description="缩放比例，默认 1", gt=0.01, le=1000)
    source_path: Optional[str] = Field(default=None, description="可选外部 DWG 块库完整绝对路径", max_length=2000)
    source_sha256: Optional[str] = Field(
        default=None, pattern=r"^[0-9a-fA-F]{64}$",
        description="使用 source_path 时必填；绑定已经检查过的块库 SHA-256")
    attributes: Dict[str, str] = Field(default_factory=dict, max_length=256, description="属性标签到值的映射")
    layer: Optional[str] = Field(default=None, description="块引用所在图层；留空使用当前层", max_length=255)
    x_scale: Optional[float] = Field(default=None, gt=0, le=1000)
    y_scale: Optional[float] = Field(default=None, gt=0, le=1000)
    z_scale: Optional[float] = Field(default=None, gt=0, le=1000)
    field_bindings: List[BlockFieldBindingInput] = Field(default_factory=list, max_length=256)
    replace_existing: bool = Field(default=False, description="导入 source_path 时是否替换未被引用的同名定义")

    @model_validator(mode="after")
    def validate_external_source_evidence(self):
        if self.source_path and not self.source_sha256:
            raise ValueError("使用 source_path 导入块库时必须同时提供 source_sha256")
        if self.source_sha256 and not self.source_path:
            raise ValueError("提供 source_sha256 时必须同时提供 source_path")
        return self


@mcp.tool(
    name="cad_insert_block",
    annotations={
        "title": "插入图块",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_insert_block(params: InsertBlockInput) -> str:
    """把已创建的图块插入到指定位置（可旋转/缩放）。重复调用可放置多个副本。

    Args:
        params (InsertBlockInput): 块名、插入点、旋转角、缩放。

    Returns:
        str: JSON，含块引用句柄。
    """
    try:
        r = _get_backend().insert_block(
            params.name, params.x, params.y, params.rotation, params.scale,
            source_path=params.source_path,
            source_sha256=params.source_sha256,
            attributes=params.attributes,
            layer=params.layer,
            x_scale=params.x_scale,
            y_scale=params.y_scale,
            z_scale=params.z_scale,
            field_bindings=[item.model_dump(mode="python") for item in params.field_bindings],
            replace_existing=params.replace_existing,
        )
        return _ok({"ok": True, **r})
    except Exception as e:
        return _fail(e)


class DrawTitleBlockInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    width: float = Field(default=420, description="图框宽度（毫米）。指定 size 时自动按标准幅面，无需填", gt=100, le=20000)
    height: float = Field(default=297, description="图框高度（毫米）。指定 size 时自动按标准幅面，无需填", gt=100, le=20000)
    size: Optional[Literal["A0", "A1", "A2", "A3", "A4"]] = Field(
        default=None,
        description="标准图纸幅面（横放），如 A3。指定后自动用该幅面尺寸（宽×高：A0 1189×841、A1 841×594、A2 594×420、A3 420×297、A4 297×210），不必再填 width/height")
    title: Optional[str] = Field(default="", description="图名", max_length=60)
    project: Optional[str] = Field(default="", description="工程名称", max_length=60)
    design: Optional[str] = Field(default="", description="设计人", max_length=20)
    check: Optional[str] = Field(default="", description="审核人", max_length=20)
    sheet_no: Optional[str] = Field(default="", description="图号", max_length=20)
    ratio: Optional[str] = Field(default="1:100", description="比例（如 1:50），填进标题栏比例栏", max_length=20)
    layer: Optional[str] = Field(default="TITLE", description="图框图层名，默认 TITLE")


@mcp.tool(
    name="cad_draw_title_block",
    annotations={
        "title": "画工程图框+标题栏",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_draw_title_block(params: DrawTitleBlockInput) -> str:
    """画工程图框 + 标题栏：外框粗线、内框细线（左边 25mm 装订边），右下角标题栏填图名/工程/设计/审核/图号/比例。

    出图时先画好内容再调此工具加图框；画在图左下角 (0,0) 起。
    指定 size（A0-A4）用标准图纸幅面；ratio 填比例（如 1:50）。

    Args:
        params (DrawTitleBlockInput): 图框幅面（size 或 width/height）与标题栏字段。

    Returns:
        str: JSON。
    """
    try:
        return _ok({"ok": True, "message": _get_backend().draw_title_block(
            params.width, params.height, params.title, params.project,
            params.design, params.check, params.sheet_no, params.ratio,
            params.size, params.layer)})
    except Exception as e:
        return _fail(e)


# ================= 工具：批量布置、检查、出图 =================

class ArrayRectInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    handles: List[str] = Field(..., description="要阵列的图元句柄列表；acad 模式对每个原件调用一次原生阵列", min_length=1, max_length=100)
    rows: int = Field(..., description="行数（Y 方向），至少 1", ge=1, le=200)
    cols: int = Field(..., description="列数（X 方向），至少 1", ge=1, le=200)
    row_spacing: float = Field(..., description="行间距（毫米），原件保留在第一行，副本向上逐行偏移", gt=0, le=100000)
    col_spacing: float = Field(..., description="列间距（毫米），原件保留在第一列，副本向右逐列偏移", gt=0, le=100000)

    @model_validator(mode="after")
    def validate_total_creation(self):
        if self.rows == 1 and self.cols == 1:
            raise ValueError("1 行 1 列没有阵列意义，请至少 2 行或 2 列")
        validate_bulk_creation(len(self.handles), self.rows * self.cols - 1, "矩形阵列")
        return self


@mcp.tool(
    name="cad_array_rect",
    annotations={
        "title": "矩形阵列",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_array_rect(params: ArrayRectInput) -> str:
    """矩形阵列：把选中的图元按 rows 行 × cols 列等距复制，原件保留在左下角第一个位置。

    例如"在泵房布 2 排 3 列共 6 台泵"：先画好一台泵（或打包成块），再把它的句柄传进来，
    rows=2, cols=3, 填好行列间距即可。

    Args:
        params (ArrayRectInput): 句柄列表、行列数与间距。

    Returns:
        str: JSON，含新增副本数。
    """
    try:
        return _ok({"ok": True, "message": _get_backend().array_rect(
            params.handles, params.rows, params.cols,
            params.row_spacing, params.col_spacing)})
    except Exception as e:
        return _fail(e)


class ArrayPolarInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    handles: List[str] = Field(..., description="要阵列的图元句柄列表；acad 模式使用原生环形阵列", min_length=1, max_length=100)
    cx: float = Field(..., description="阵列圆心 X（毫米）")
    cy: float = Field(..., description="阵列圆心 Y（毫米）")
    count: int = Field(..., description="总数量（含原件），至少 2", ge=2, le=200)
    total_angle: float = Field(default=360, description="分布总角度（度）：360 整圈均布；部分角度如 90 则在该角内均布", gt=0, le=360)

    @model_validator(mode="after")
    def validate_total_creation(self):
        validate_bulk_creation(len(self.handles), self.count - 1, "环形阵列")
        return self


@mcp.tool(
    name="cad_array_polar",
    annotations={
        "title": "环形阵列",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_array_polar(params: ArrayPolarInput) -> str:
    """环形阵列：把选中的图元绕圆心 (cx, cy) 均布复制，原件保留在第 0 个位置。

    例如"4 台泵绕池心均匀布置"：圆心填池心坐标，count=4，total_angle=360。

    Args:
        params (ArrayPolarInput): 句柄列表、圆心、数量与分布角度。

    Returns:
        str: JSON，含新增副本数。
    """
    try:
        return _ok({"ok": True, "message": _get_backend().array_polar(
            params.handles, params.cx, params.cy, params.count, params.total_angle)})
    except Exception as e:
        return _fail(e)


class DrawCenterlineInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    x1: float = Field(..., description="构件包络矩形第一角点 X（毫米）")
    y1: float = Field(..., description="构件包络矩形第一角点 Y（毫米）")
    x2: float = Field(..., description="构件包络矩形第二角点 X（毫米）")
    y2: float = Field(..., description="构件包络矩形第二角点 Y（毫米）")
    layer: Optional[str] = Field(default="CENTER", description="中心线图层，默认 CENTER（自动设为红色点划线）")


@mcp.tool(
    name="cad_draw_centerline",
    annotations={
        "title": "画对称中心线",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_draw_centerline(params: DrawCenterlineInput) -> str:
    """给矩形范围画对称中心线：水平 + 垂直两条点划线（自动进 CENTER 图层、红色），两端各外伸 10%。

    给矩形（或圆的外接矩形）的两个对角点即可。画圆池、对称构筑物时用来标中心线。

    Args:
        params (DrawCenterlineInput): 包络矩形两角点。

    Returns:
        str: JSON，含两条线句柄。
    """
    try:
        r = _get_backend().draw_centerline(params.x1, params.y1, params.x2, params.y2, params.layer)
        return _ok({"ok": True, **r})
    except Exception as e:
        return _fail(e)


class OffsetInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    handle: str = Field(..., description="要偏移的曲线句柄", min_length=1, max_length=40)
    distance: float = Field(
        ..., description="有符号偏移距离（毫米），不能为 0；闭合曲线正值向外、负值向内")


@mcp.tool(
    name="cad_offset",
    annotations={
        "title": "曲线几何偏移",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_offset(params: OffsetInput) -> str:
    """对单个曲线做真正的几何 OFFSET，保留源图元并返回新图元句柄。

    acad 模式直接调用 AutoCAD 原生 Offset，支持 LINE、ARC、CIRCLE、ELLIPSE、
    POLYLINE、SPLINE、XLINE 等原生支持类型。dxf 模式支持常用直线、圆、圆弧和
    不含圆弧段的二维多段线；不支持的类型会明确报错。

    Args:
        params (OffsetInput): 曲线句柄和有符号偏移距离。

    Returns:
        str: JSON，含源图元信息及新图元句柄列表。
    """
    try:
        if abs(params.distance) <= 1e-12:
            raise ValueError("偏移距离不能为 0")
        return _ok({"ok": True, **_get_backend().offset(
            params.handle, params.distance)})
    except Exception as e:
        return _fail(e)


class CopyLinearInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    handles: List[str] = Field(..., description="要线性复制的图元句柄列表", min_length=1, max_length=100)
    dx: float = Field(..., description="每份副本的 X 位移（毫米），第 i 份偏移 dx*i")
    dy: float = Field(..., description="每份副本的 Y 位移（毫米），第 i 份偏移 dy*i")
    copies: int = Field(..., description="复制份数（不含原件）", ge=1, le=200)

    @model_validator(mode="after")
    def validate_total_creation(self):
        validate_bulk_creation(len(self.handles), self.copies, "线性复制")
        if self.dx == 0 and self.dy == 0:
            raise ValueError("位移 (dx, dy) 不能同时为 0")
        if self.dx != 0 and self.dy != 0 and len(self.handles) * self.copies > 200:
            raise ValueError("斜向复制最多允许 200 个副本操作；请先打包为图块或分次执行")
        return self


@mcp.tool(
    name="cad_copy_linear",
    annotations={
        "title": "线性等距复制",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_copy_linear(params: CopyLinearInput) -> str:
    """按位移向量 (dx, dy) 等距复制多份；这是旧 cad_offset 的位移复制能力。"""
    try:
        if params.dx == 0 and params.dy == 0:
            raise ValueError("位移 (dx, dy) 不能同时为 0")
        if params.dx != 0 and params.dy != 0 and len(params.handles) * params.copies > 200:
            raise ValueError("斜向复制最多允许 200 个副本操作；请先打包为图块或分次执行")
        return _ok({"ok": True, "message": _get_backend().copy_linear(
            params.handles, params.dx, params.dy, params.copies)})
    except Exception as e:
        return _fail(e)


class CheckDrawingInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    tolerance: Optional[float] = Field(default=None, description="容差（毫米）：线端点距离小于此值判为断线/重合。留空自动按图纸范围算（对角线 0.1%）", gt=0, le=100000)


@mcp.tool(
    name="cad_check_drawing",
    annotations={
        "title": "图面体检",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def cad_check_drawing(params: CheckDrawingInput) -> str:
    """图面体检：扫描常见问题并列出清单。

    检查三项：①重叠图元（圆/线几乎完全重合）；②疑似断线（两条线的端点相距很近但没接上，
    常见于手工描图遗漏）；③空图层（定义了但没画东西的图层）。出图前跑一遍查漏。

    Args:
        params (CheckDrawingInput): 可选容差。

    Returns:
        str: 问题清单文本；无问题则显示通过。
    """
    try:
        return _get_backend().check_drawing(params.tolerance)
    except Exception as e:
        return _fail(e)


class ExportPdfInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    filename: str = Field(..., description="PDF 文件名，如 '沉淀池平面图.pdf'（不写扩展名自动补 .pdf）", min_length=1, max_length=120)


@mcp.tool(
    name="cad_export_pdf",
    annotations={
        "title": "导出 PDF 打印稿",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_export_pdf(params: ExportPdfInput) -> str:
    """把当前图纸输出为 PDF 打印稿（用 AutoCAD 的 DWG To PDF 驱动，图面范围适配纸张）。

    ⚠️ 仅 acad 模式（直接操控本地 AutoCAD）可用。dxf 模式没有渲染引擎，
    请先 cad_switch_mode 切到 acad 模式打开图纸再导出，或在 AutoCAD 里手动打印。

    Args:
        params (ExportPdfInput): 文件名。

    Returns:
        str: JSON，含 PDF 路径。
    """
    try:
        backend = _get_backend()
        if not hasattr(backend, "export_pdf"):
            return _ok({"ok": False,
                        "message": "导出 PDF 仅 acad 模式可用。请先用 cad_switch_mode 切到 acad 模式并打开图纸，再调用本工具；或直接在 AutoCAD 里打印为 PDF"})
        return _ok({"ok": True, "message": backend.export_pdf(params.filename)})
    except Exception as e:
        return _fail(e)


class DrawTableInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    origin_x: float = Field(..., description="表格左上角 X（毫米）")
    origin_y: float = Field(..., description="表格左上角 Y（毫米），表格向上排列")
    col_widths: List[float] = Field(..., description="各列宽度（毫米），如 [30, 120, 80, 60, 40]，列数 = 元素个数", min_length=1, max_length=MAX_TABLE_COLUMNS)
    row_height: float = Field(..., description="行高（毫米），表头与数据行同高", gt=5, le=500)
    headers: List[str] = Field(..., description="表头（列标题），数量须与 col_widths 一致", min_length=1, max_length=MAX_TABLE_COLUMNS)
    rows: List[List[str]] = Field(..., description="数据行：每行是一个字符串数组，每行元素数量须与 col_widths 一致。如设备材料表 [['1','离心泵','IS100-65','2台','']]", min_length=1, max_length=MAX_TABLE_ROWS)
    title: Optional[str] = Field(default="", description="表题（画在表格上方居中），如 '设备材料表'", max_length=60)
    layer: Optional[str] = Field(default="TABLE", description="图层名，默认 TABLE")

    @model_validator(mode="after")
    def validate_shape_and_content(self):
        validate_table_data(self.col_widths, self.headers, self.rows)
        return self


@mcp.tool(
    name="cad_draw_table",
    annotations={
        "title": "画表格",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_draw_table(params: DrawTableInput) -> str:
    """画通用表格：表头 + 数据行，文字自动居中、自动适配列宽。可画设备材料表、管道表、图例表等。

    例：画设备材料表 → headers=['序号','名称','规格','数量','备注'], rows=[['1','离心泵','IS100-65','2','']]。

    Args:
        params (DrawTableInput): 表格位置、列宽、行高、表头与数据行。

    Returns:
        str: JSON，含表格尺寸。
    """
    try:
        rows = [list(r) for r in params.rows]
        r = _get_backend().draw_table(
            params.origin_x, params.origin_y, list(params.col_widths),
            params.row_height, list(params.headers), rows,
            params.title, params.layer)
        return _ok({"ok": True, **r})
    except Exception as e:
        return _fail(e)


# ================= 工具：环境工程语义模型 =================

class LoadDraftingProfileInput(ToolInput):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    path: str = Field(min_length=1, max_length=1000)


@mcp.tool(
    name="cad_load_drafting_profile",
    annotations={"title": "加载外置制图标准配置", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
def cad_load_drafting_profile(params: LoadDraftingProfileInput) -> str:
    """加载兼容的 1.0 制图配置或严格的通用 DWT 2.0 配置。"""
    try:
        profile, evidence = load_drafting_profile(params.path)
        _bounded_cache_put(_DRAFTING_PROFILES, profile.profile_id, profile)
        return _ok({"ok": True, "profile_id": profile.profile_id,
                    "name": profile.name,
                    "schema_version": profile.schema_version, **evidence})
    except Exception as e:
        return _fail(e)


class ListDraftingProfilesInput(ToolInput):
    model_config = ConfigDict(extra="forbid")


@mcp.tool(
    name="cad_list_drafting_profiles",
    annotations={"title": "列出已加载制图标准配置", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
def cad_list_drafting_profiles(params: ListDraftingProfilesInput) -> str:
    """列出当前进程可用的制图配置。"""
    del params
    return _ok({"ok": True, "profiles": [
        {"profile_id": item.profile_id, "name": item.name,
         "schema_version": item.schema_version,
         "standard_references": item.standard_references}
        for item in _DRAFTING_PROFILES.values()
    ]})

class LoadEnvironmentRuleSetInput(ToolInput):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    path: str = Field(min_length=1, max_length=1000)


@mcp.tool(
    name="cad_load_environment_rule_set",
    annotations={"title": "加载外置环境工程规则集", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
def cad_load_environment_rule_set(params: LoadEnvironmentRuleSetInput) -> str:
    """只读加载 JSON 规则集；课程、地区和业主要求由配置提供，不写死进 MCP。"""
    try:
        rule_set, evidence = load_rule_set(params.path)
        _bounded_cache_put(_RULE_SETS, rule_set.rule_set_id, rule_set)
        return _ok({"ok": True, "rule_set_id": rule_set.rule_set_id,
                    "name": rule_set.name, **evidence})
    except Exception as e:
        return _fail(e)


class ListEnvironmentRuleSetsInput(ToolInput):
    model_config = ConfigDict(extra="forbid")


@mcp.tool(
    name="cad_list_environment_rule_sets",
    annotations={"title": "列出已加载环境工程规则集", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
def cad_list_environment_rule_sets(params: ListEnvironmentRuleSetsInput) -> str:
    """列出当前进程已加载的外置规则集及其规则数量。"""
    del params
    return _ok({"ok": True, "rule_sets": [
        {"rule_set_id": item.rule_set_id, "name": item.name,
         "description": item.description, "rule_count": len(item.rules)}
        for item in _RULE_SETS.values()
    ]})

class ValidateEnvironmentModelInput(ToolInput):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    model: EnvironmentModel
    drafting_profile_id: Optional[str] = None
    rule_set_ids: List[str] = Field(default_factory=list, max_length=64)
    rules: List[RuleDefinition] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def validate_inline_rules(self):
        _validate_inline_rule_complexity(self.rules)
        return self


@mcp.tool(
    name="cad_validate_environment_model",
    annotations={"title": "检查环境工程语义模型", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
def cad_validate_environment_model(params: ValidateEnvironmentModelInput) -> str:
    """检查通用构筑物、网络、标高、视图、表格及外部声明式规则。"""
    try:
        profile = _resolved_drafting_profile(params.drafting_profile_id)
        report = _validate_environment(
            params.model,
            _resolved_rules(params.rule_set_ids, params.rules),
            profile,
        )
        return _ok(report.model_dump())
    except Exception as e:
        return _fail(e)


class PreviewEnvironmentDrawingInput(ToolInput):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    model: EnvironmentModel
    view_ids: Optional[List[str]] = Field(default=None, max_length=1_000)
    include_schedules: bool = True
    include_operations: bool = False
    operation_limit: int = Field(default=100, ge=1, le=2000)
    drafting_profile_id: Optional[str] = None
    rule_set_ids: List[str] = Field(default_factory=list, max_length=64)
    rules: List[RuleDefinition] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def validate_inline_rules(self):
        _validate_inline_rule_complexity(self.rules)
        return self


@mcp.tool(
    name="cad_preview_environment_drawing",
    annotations={"title": "预检环境工程制图计划", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
def cad_preview_environment_drawing(params: PreviewEnvironmentDrawingInput) -> str:
    """只生成和校验操作计划，不写入 CAD。"""
    try:
        profile = _resolved_drafting_profile(params.drafting_profile_id)
        report = _validate_environment(
            params.model,
            _resolved_rules(params.rule_set_ids, params.rules),
            profile,
        )
        if not report.ok:
            return _ok({"ok": False, "stage": "validation", "report": report.model_dump()})
        plan, _ = compile_environment_model(
            params.model, params.view_ids, params.include_schedules,
            drafting_profile=profile)
        payload: Dict[str, Any] = {
            "ok": True,
            "project_id": plan.project_id,
            "revision": plan.revision,
            "view_ids": plan.view_ids,
            "operation_count": len(plan.operations),
            "statistics": plan.statistics,
            "warnings": plan.warnings,
            "report": report.model_dump(),
        }
        if params.include_operations:
            payload["operations"] = [item.model_dump() for item in plan.operations[:params.operation_limit]]
            payload["operations_truncated"] = len(plan.operations) > params.operation_limit
        return _ok(payload)
    except Exception as e:
        return _fail(e)


class ApplyEnvironmentDrawingInput(ToolInput):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    model: EnvironmentModel
    view_ids: Optional[List[str]] = Field(default=None, max_length=1_000)
    include_schedules: bool = True
    policy: Literal["create_only", "update_managed", "replace_scope"] = "update_managed"
    dry_run: bool = Field(
        default=True,
        description="默认只返回差异；显式设为 false 才在一个后端事务中写入",
    )
    drafting_profile_id: Optional[str] = None
    rule_set_ids: List[str] = Field(default_factory=list, max_length=64)
    rules: List[RuleDefinition] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def validate_inline_rules(self):
        _validate_inline_rule_complexity(self.rules)
        return self


def _operation_preview(operation: Optional[CadOperation]) -> Optional[Dict[str, Any]]:
    if operation is None:
        return None
    return {
        "kind": operation.kind,
        "semantic_id": operation.semantic_id,
        "view_id": operation.view_id,
        "role": operation.role,
    }


def _difference_preview(difference: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "create": [_operation_preview(operation)
                   for operation in difference["create"]],
        "replace": [{
            "operation": _operation_preview(item.get("operation")),
            "handles": list(item.get("handles", [])),
            "manual_change_overridden": bool(
                item.get("manual_change_overridden", False)),
        } for item in difference["replace"]],
        "conflict": [{
            "operation": _operation_preview(item.get("operation")),
            "handles": list(item.get("handles", [])),
            "reason": item.get("reason"),
        } for item in difference["conflict"]],
        "delete": [{
            "handle": record.handle,
            "project_id": record.project_id,
            "semantic_id": record.semantic_id,
            "view_id": record.view_id,
            "role": record.role,
        } for record in difference["delete"]],
    }


@mcp.tool(
    name="cad_apply_environment_drawing",
    annotations={"title": "同步环境工程模型到 CAD", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": True,
                 "openWorldHint": False},
)
def cad_apply_environment_drawing(params: ApplyEnvironmentDrawingInput) -> str:
    """根据语义 ID 和 XData 增量同步；默认 dry_run，不覆盖无 XData 的手工对象。"""
    try:
        profile = _resolved_drafting_profile(params.drafting_profile_id)
        report = _validate_environment(
            params.model,
            _resolved_rules(params.rule_set_ids, params.rules),
            profile,
        )
        if not report.ok:
            return _ok({"ok": False, "stage": "validation", "report": report.model_dump()})
        plan, _ = compile_environment_model(
            params.model, params.view_ids, params.include_schedules,
            drafting_profile=profile)
        backend = _get_backend()
        existing = backend.list_managed_entities()
        difference = reconcile_plan(plan, existing, params.policy)
        if params.dry_run:
            return _ok({
                "ok": True, "dry_run": True, "project_id": plan.project_id,
                "revision": plan.revision, "policy": params.policy,
                "difference": difference["statistics"],
                "can_apply": not difference["conflict"],
                "changes": _difference_preview(difference),
                "operation_count": len(plan.operations), "warnings": plan.warnings,
            })
        result = backend.apply_operation_plan(plan, params.policy)
        return _ok({"ok": True, "dry_run": False, **result})
    except Exception as e:
        return _fail(e)


class ListManagedEntitiesInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)


@mcp.tool(
    name="cad_list_managed_entities",
    annotations={"title": "列出 MCP 管理的工程对象", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
def cad_list_managed_entities(params: ListManagedEntitiesInput) -> str:
    try:
        records = _get_backend().list_managed_entities()
        return _ok({"ok": True, "count": len(records),
                    "entities": [record.__dict__ for record in records]})
    except Exception as e:
        return _fail(e)


# ================= 工具：模板、固定图框和布局 =================

class SaveAsInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    filename: str = Field(min_length=1, max_length=180)
    overwrite: bool = False


@mcp.tool(name="cad_save_as", annotations={"title": "另存当前图纸",
           "readOnlyHint": False, "destructiveHint": False,
           "idempotentHint": False, "openWorldHint": False})
def cad_save_as(params: SaveAsInput) -> str:
    try:
        return _ok({"ok": True, **_get_backend().save_as(params.filename, params.overwrite)})
    except Exception as e:
        return _fail(e)


class CloneTemplateDrawingInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    source_path: str = Field(description="只读模板 DWG 的完整绝对路径", min_length=1, max_length=1000)
    output_filename: str = Field(description="工作副本文件名，输出到受控图纸目录", min_length=1, max_length=180)
    expected_sha256: str = Field(
        pattern=r"^[0-9a-fA-F]{64}$",
        description="必须与 cad_inspect_template_drawing 返回的 source_sha256 一致")
    overwrite: bool = False


@mcp.tool(name="cad_clone_template_drawing", annotations={"title": "从 DWG 模板创建工作副本",
           "readOnlyHint": False, "destructiveHint": False,
           "idempotentHint": False, "openWorldHint": False})
def cad_clone_template_drawing(params: CloneTemplateDrawingInput) -> str:
    try:
        return _ok({"ok": True, **_get_backend().clone_template_drawing(
            params.source_path, params.output_filename,
            params.expected_sha256, params.overwrite)})
    except Exception as e:
        return _fail(e)


class InspectTemplateDrawingInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    source_path: str = Field(description="只读模板 DWG 的完整绝对路径",
                             min_length=1, max_length=1000)


@mcp.tool(name="cad_inspect_template_drawing", annotations={
           "title": "只读检查 DWG 模板资产", "readOnlyHint": True,
           "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def cad_inspect_template_drawing(params: InspectTemplateDrawingInput) -> str:
    try:
        backend = _get_backend()
        if not hasattr(backend, "inspect_template_drawing"):
            raise RuntimeError("DWG 模板资产检查仅 acad 模式支持")
        return _ok({"ok": True, **backend.inspect_template_drawing(params.source_path)})
    except Exception as e:
        return _fail(e)


class TemplateManifestPathInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    manifest_path: str = Field(min_length=1, max_length=1000)


@mcp.tool(name="cad_verify_template_manifest", annotations={"title": "核验模板清单和源 DWG",
           "readOnlyHint": True, "destructiveHint": False,
           "idempotentHint": True, "openWorldHint": False})
def cad_verify_template_manifest(params: TemplateManifestPathInput) -> str:
    try:
        manifest = load_template_manifest(params.manifest_path)
        return _ok({"ok": True, "template_id": manifest.template_id,
                    "sheet_ids": [sheet.id for sheet in manifest.sheets],
                    "source": verify_template_source(manifest)})
    except Exception as e:
        return _fail(e)


@mcp.tool(name="cad_load_template_manifest", annotations={"title": "加载模板资产清单",
           "readOnlyHint": True, "destructiveHint": False,
           "idempotentHint": True, "openWorldHint": False})
def cad_load_template_manifest(params: TemplateManifestPathInput) -> str:
    try:
        backend = _get_backend()
        if not hasattr(backend, "load_template_manifest"):
            raise RuntimeError("模板 DWG 清单仅 acad 模式可加载")
        return _ok({"ok": True, **backend.load_template_manifest(params.manifest_path)})
    except Exception as e:
        return _fail(e)


class EvaluateTemplateSheetScalesInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    manifest_path: str = Field(min_length=1, max_length=1000)
    sheet_id: str = Field(min_length=1, max_length=120)
    content_bounds: Tuple[float, float, float, float]
    candidate_denominators: List[float] = Field(min_length=1, max_length=100)
    annotation_padding_model: float = Field(default=0, ge=0)


@mcp.tool(name="cad_evaluate_template_sheet_scales", annotations={
           "title": "按固定模板图框校核比例", "readOnlyHint": True,
           "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def cad_evaluate_template_sheet_scales(params: EvaluateTemplateSheetScalesInput) -> str:
    try:
        manifest = load_template_manifest(params.manifest_path)
        sheet = next((item for item in manifest.sheets if item.id == params.sheet_id), None)
        if sheet is None:
            raise ValueError(f"模板中不存在图幅 {params.sheet_id}")
        return _ok({"ok": True, **evaluate_sheet_scales(
            params.content_bounds, sheet, params.candidate_denominators,
            params.annotation_padding_model)})
    except Exception as e:
        return _fail(e)


class CreateLayoutInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)
    replace: bool = False


@mcp.tool(name="cad_create_layout", annotations={"title": "创建纸空间布局",
           "readOnlyHint": False, "destructiveHint": True,
           "idempotentHint": True, "openWorldHint": False})
def cad_create_layout(params: CreateLayoutInput) -> str:
    try:
        backend = _get_backend()
        if not hasattr(backend, "create_layout"):
            raise RuntimeError("纸空间布局工具仅 acad 模式支持")
        return _ok({"ok": True, **backend.create_layout(params.name, params.replace)})
    except Exception as e:
        return _fail(e)


class InsertTemplateSheetInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    template_id: str
    sheet_id: str
    layout_name: str
    x: float = 0
    y: float = 0


@mcp.tool(name="cad_insert_template_sheet", annotations={"title": "插入原始模板图框",
           "readOnlyHint": False, "destructiveHint": False,
           "idempotentHint": False, "openWorldHint": False})
def cad_insert_template_sheet(params: InsertTemplateSheetInput) -> str:
    try:
        backend = _get_backend()
        if not hasattr(backend, "insert_template_sheet"):
            raise RuntimeError("DWG 模板图框插入仅 acad 模式支持")
        return _ok({"ok": True, **backend.insert_template_sheet(
            params.template_id, params.sheet_id, params.layout_name, params.x, params.y)})
    except Exception as e:
        return _fail(e)


class FillTemplateFieldsInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    template_id: str
    sheet_id: str
    layout_name: str
    values: Dict[str, Any] = Field(max_length=256)
    layer: str = "ENV-ANNO"


@mcp.tool(name="cad_fill_template_fields", annotations={"title": "填写模板标题栏字段",
           "readOnlyHint": False, "destructiveHint": False,
           "idempotentHint": False, "openWorldHint": False})
def cad_fill_template_fields(params: FillTemplateFieldsInput) -> str:
    try:
        backend = _get_backend()
        if not hasattr(backend, "fill_template_fields"):
            raise RuntimeError("模板字段填写仅 acad 模式支持")
        return _ok({"ok": True, **backend.fill_template_fields(
            params.template_id, params.sheet_id, params.layout_name,
            params.values, params.layer)})
    except Exception as e:
        return _fail(e)


class CreateViewportInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    layout_name: str
    center_x: float
    center_y: float
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    model_center_x: float
    model_center_y: float
    scale_denominator: float = Field(gt=0)
    lock: bool = True


@mcp.tool(name="cad_create_viewport", annotations={"title": "创建并锁定布局视口",
           "readOnlyHint": False, "destructiveHint": False,
           "idempotentHint": False, "openWorldHint": False})
def cad_create_viewport(params: CreateViewportInput) -> str:
    try:
        backend = _get_backend()
        if not hasattr(backend, "create_viewport"):
            raise RuntimeError("布局视口仅 acad 模式支持")
        return _ok({"ok": True, **backend.create_viewport(
            params.layout_name, params.center_x, params.center_y,
            params.width, params.height, params.model_center_x,
            params.model_center_y, params.scale_denominator, params.lock)})
    except Exception as e:
        return _fail(e)


class ConfigureLayoutPlotInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    layout_name: str
    media_name: str
    style_sheet: str = "monochrome.ctb"
    device: str = "DWG To PDF.pc3"
    rotation: Literal[0, 90, 180, 270] = 0
    plot_type: Literal["display", "extents", "limits", "view", "window", "layout"] = "layout"
    paper_units: Literal["millimeters", "inches", "pixels"] = "millimeters"
    scale_mode: Literal["standard", "custom", "fit"] = "standard"
    scale_numerator: Optional[float] = Field(default=None, gt=0, le=1000000)
    scale_denominator: Optional[float] = Field(default=None, gt=0, le=1000000)
    center_plot: bool = False
    plot_with_lineweights: bool = True
    plot_with_plot_styles: bool = True
    apply_named_page_setup: Optional[str] = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_custom_scale(self):
        if self.scale_mode == "custom" and (
            self.scale_numerator is None or self.scale_denominator is None
        ):
            raise ValueError("custom 比例必须同时提供 scale_numerator 和 scale_denominator")
        return self


@mcp.tool(name="cad_configure_layout_plot", annotations={"title": "配置布局精确出图",
           "readOnlyHint": False, "destructiveHint": False,
           "idempotentHint": True, "openWorldHint": False})
def cad_configure_layout_plot(params: ConfigureLayoutPlotInput) -> str:
    try:
        backend = _get_backend()
        if not hasattr(backend, "configure_layout_plot"):
            raise RuntimeError("布局出图配置仅 acad 模式支持")
        return _ok({"ok": True, **backend.configure_layout_plot(
            params.layout_name, params.media_name, params.style_sheet,
            params.device, params.rotation,
            params.plot_type, params.paper_units, params.scale_mode,
            params.scale_numerator, params.scale_denominator,
            params.center_plot, params.plot_with_lineweights,
            params.plot_with_plot_styles, params.apply_named_page_setup)})
    except Exception as e:
        return _fail(e)


class PublishLayoutPdfInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    layout_name: str
    filename: str


@mcp.tool(name="cad_publish_layout_pdf", annotations={"title": "按布局发布 PDF",
           "readOnlyHint": False, "destructiveHint": False,
           "idempotentHint": False, "openWorldHint": False})
def cad_publish_layout_pdf(params: PublishLayoutPdfInput) -> str:
    try:
        backend = _get_backend()
        if not hasattr(backend, "publish_layout_pdf"):
            raise RuntimeError("布局 PDF 发布仅 acad 模式支持")
        return _ok({"ok": True, **backend.publish_layout_pdf(
            params.layout_name, params.filename)})
    except Exception as e:
        return _fail(e)


class DrawMTextInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=10000)
    x: float
    y: float
    width: float = Field(gt=0)
    height: Optional[float] = Field(default=None, gt=0)
    rotation: float = Field(default=0, ge=-360, le=360)
    layer: Optional[str] = None


@mcp.tool(name="cad_draw_mtext", annotations={"title": "绘制原生多行文字",
           "readOnlyHint": False, "destructiveHint": False,
           "idempotentHint": False, "openWorldHint": False})
def cad_draw_mtext(params: DrawMTextInput) -> str:
    try:
        result = _get_backend().draw_mtext(
            params.text, params.x, params.y, params.width, params.height,
            params.rotation, params.layer)
        return _ok({"ok": True, **result})
    except Exception as e:
        return _fail(e)


# ================= 工具：通用 DWT 2.0 =================

def _required_v2_profile(path: str) -> tuple[DraftingProfileV2, Dict[str, Any]]:
    profile, evidence = load_dwt_profile_v2(path)
    return profile, evidence


class SetDrawingSettingsInput(ToolInput):
    insertion_units: Literal["millimeters"] = "millimeters"
    measurement_system: Literal["metric"] = "metric"
    linear_unit_format: Literal["decimal"] = "decimal"
    linear_precision: int = Field(default=2, ge=0, le=8)
    angular_unit_format: Literal["decimal_degrees"] = "decimal_degrees"
    angular_precision: int = Field(default=2, ge=0, le=8)
    associative_dimensions: bool = True
    mirror_text: bool = False
    display_lineweights: bool = True


@mcp.tool(name="cad_set_drawing_settings", annotations={"title": "设置 DWT 图纸环境",
           "readOnlyHint": False, "destructiveHint": False,
           "idempotentHint": True, "openWorldHint": False})
def cad_set_drawing_settings(params: SetDrawingSettingsInput) -> str:
    """按明确字段设置毫米/公制 DWT 环境，并逐项读回 before/after。"""
    try:
        result = _get_backend().set_drawing_settings(**params.model_dump(mode="python"))
        return _ok({"ok": True, **result})
    except Exception as e:
        return _fail(e)


class ListDimstylesInput(ToolInput):
    pass


@mcp.tool(name="cad_list_dimstyles", annotations={"title": "列出尺寸样式详情",
           "readOnlyHint": True, "destructiveHint": False,
           "idempotentHint": True, "openWorldHint": False})
def cad_list_dimstyles(params: ListDimstylesInput) -> str:
    """只读列出尺寸样式；不能无副作用读取的字段进入 unverified_fields。"""
    try:
        return _ok({"ok": True, **_get_backend().list_dimstyles()})
    except Exception as e:
        return _fail(e)


class ListLayoutsDetailedInput(ToolInput):
    pass


@mcp.tool(name="cad_list_layouts_detailed", annotations={"title": "列出布局和出图设置",
           "readOnlyHint": True, "destructiveHint": False,
           "idempotentHint": True, "openWorldHint": False})
def cad_list_layouts_detailed(params: ListLayoutsDetailedInput) -> str:
    try:
        return _ok({"ok": True, **_get_backend().list_layouts_detailed()})
    except Exception as e:
        return _fail(e)


class GetPlotEnvironmentInput(ToolInput):
    pass


@mcp.tool(name="cad_get_plot_environment", annotations={"title": "读取实际打印资源",
           "readOnlyHint": True, "destructiveHint": False,
           "idempotentHint": True, "openWorldHint": True})
def cad_get_plot_environment(params: GetPlotEnvironmentInput) -> str:
    """读取 AutoCAD 实际 CTB/STB、PC3 和纸张证据，不填默认假设。"""
    try:
        return _ok({"ok": True, **_get_backend().get_plot_environment()})
    except Exception as e:
        return _fail(e)


class ListPageSetupsInput(ToolInput):
    pass


@mcp.tool(name="cad_list_page_setups", annotations={"title": "列出命名页面设置",
           "readOnlyHint": True, "destructiveHint": False,
           "idempotentHint": True, "openWorldHint": False})
def cad_list_page_setups(params: ListPageSetupsInput) -> str:
    try:
        return _ok({"ok": True, **_get_backend().list_page_setups()})
    except Exception as e:
        return _fail(e)


class ConvertPlotStyleModeInput(ToolInput):
    target_mode: Literal["ctb", "stb"]
    expected_current_mode: Optional[Literal["ctb", "stb"]] = None
    dry_run: bool = True


@mcp.tool(name="cad_convert_plot_style_mode", annotations={"title": "检查或转换 CTB/STB 模式",
           "readOnlyHint": False, "destructiveHint": True,
           "idempotentHint": True, "openWorldHint": False})
def cad_convert_plot_style_mode(params: ConvertPlotStyleModeInput) -> str:
    try:
        result = _get_backend().convert_plot_style_mode(
            params.target_mode, params.expected_current_mode, dry_run=params.dry_run
        )
        return _ok({"ok": True, **result})
    except Exception as e:
        return _fail(e)


class RenameLayoutInput(ToolInput):
    old_name: str = Field(min_length=1, max_length=255)
    new_name: str = Field(min_length=1, max_length=255)


@mcp.tool(name="cad_rename_layout", annotations={"title": "重命名布局",
           "readOnlyHint": False, "destructiveHint": False,
           "idempotentHint": False, "openWorldHint": False})
def cad_rename_layout(params: RenameLayoutInput) -> str:
    try:
        return _ok({"ok": True, **_get_backend().rename_layout(params.old_name, params.new_name)})
    except Exception as e:
        return _fail(e)


class PageSetupScaleInput(ToolInput):
    mode: Literal["standard", "custom", "fit"] = "standard"
    standard_scale: Optional[str] = Field(default="1:1", min_length=1, max_length=80)
    numerator: Optional[float] = Field(default=None, gt=0, le=1000000)
    denominator: Optional[float] = Field(default=None, gt=0, le=1000000)

    @model_validator(mode="after")
    def validate_scale(self):
        if self.mode == "custom" and (self.numerator is None or self.denominator is None):
            raise ValueError("custom 比例必须同时提供 numerator 和 denominator")
        return self


class CreateOrUpdatePageSetupInput(ToolInput):
    name: str = Field(min_length=1, max_length=255)
    device: str = Field(min_length=1, max_length=500)
    media_name: str = Field(min_length=1, max_length=500)
    rotation: Literal[0, 90, 180, 270] = 0
    plot_style_sheet: str = Field(min_length=1, max_length=500)
    plot_type: Literal["display", "extents", "limits", "view", "window", "layout"] = "layout"
    paper_units: Literal["millimeters", "inches", "pixels"] = "millimeters"
    plot_scale: PageSetupScaleInput = Field(default_factory=PageSetupScaleInput)
    center_plot: bool = False
    plot_with_lineweights: bool = True
    plot_with_plot_styles: bool = True
    replace: bool = False


@mcp.tool(name="cad_create_or_update_page_setup", annotations={"title": "创建或更新命名页面设置",
           "readOnlyHint": False, "destructiveHint": False,
           "idempotentHint": True, "openWorldHint": True})
def cad_create_or_update_page_setup(params: CreateOrUpdatePageSetupInput) -> str:
    try:
        payload = params.model_dump(mode="python")
        return _ok({"ok": True, **_get_backend().create_or_update_page_setup(**payload)})
    except Exception as e:
        return _fail(e)


class PreviewDwtProfileInput(ToolInput):
    profile_path: str = Field(min_length=1, max_length=2000)
    target_drawing: str = Field(min_length=1, max_length=2000)
    update_existing: bool = True
    preserve_unmanaged: bool = True


@mcp.tool(name="cad_preview_dwt_profile", annotations={"title": "预览 DWT 配置计划",
           "readOnlyHint": True, "destructiveHint": False,
           "idempotentHint": True, "openWorldHint": True})
def cad_preview_dwt_profile(params: PreviewDwtProfileInput) -> str:
    """只生成计划和确认令牌，绝不写图纸或文件。"""
    try:
        profile, evidence = _required_v2_profile(params.profile_path)
        result = _get_backend().preview_dwt_profile(
            profile,
            profile_path=evidence["path"],
            profile_sha256=evidence["sha256"],
            target_drawing=params.target_drawing,
            update_existing=params.update_existing,
            preserve_unmanaged=params.preserve_unmanaged,
        )
        return _ok({"ok": True, "dry_run": True, **result})
    except Exception as e:
        return _fail(e)


class ApplyDwtProfileInput(ToolInput):
    profile_path: str = Field(min_length=1, max_length=2000)
    target_drawing: Optional[str] = Field(default=None, min_length=1, max_length=2000)
    dry_run: bool = True
    update_existing: bool = True
    preserve_unmanaged: bool = True
    confirmation_token: Optional[str] = Field(default=None, min_length=1, max_length=80)


@mcp.tool(name="cad_apply_dwt_profile", annotations={"title": "预览或应用 DWT 配置",
           "readOnlyHint": False, "destructiveHint": True,
           "idempotentHint": True, "openWorldHint": True})
def cad_apply_dwt_profile(params: ApplyDwtProfileInput) -> str:
    """首次 dry_run；正式执行需同一状态生成的 confirmation_token。"""
    try:
        profile, evidence = _required_v2_profile(params.profile_path)
        backend = _get_backend()
        common = {
            "profile_path": evidence["path"],
            "profile_sha256": evidence["sha256"],
            "target_drawing": params.target_drawing,
            "update_existing": params.update_existing,
            "preserve_unmanaged": params.preserve_unmanaged,
        }
        if params.dry_run:
            result = backend.preview_dwt_profile(profile, **common)
            return _ok({"ok": True, "dry_run": True, **result})
        result = backend.apply_dwt_profile(
            profile, **common, confirmation_token=params.confirmation_token
        )
        return _ok({"ok": True, "dry_run": False, **result})
    except Exception as e:
        return _fail(e)


class ValidateDwtProfileInput(ToolInput):
    profile_path: str = Field(min_length=1, max_length=2000)
    preserve_unmanaged: bool = True


@mcp.tool(name="cad_validate_dwt_profile", annotations={"title": "校验当前图纸的 DWT 配置",
           "readOnlyHint": True, "destructiveHint": False,
           "idempotentHint": True, "openWorldHint": True})
def cad_validate_dwt_profile(params: ValidateDwtProfileInput) -> str:
    try:
        profile, evidence = _required_v2_profile(params.profile_path)
        result = _get_backend().validate_dwt_profile(
            profile, preserve_unmanaged=params.preserve_unmanaged
        )
        return _ok({
            "ok": True,
            "profile_path": evidence["path"],
            "profile_sha256": evidence["sha256"],
            **result,
        })
    except Exception as e:
        return _fail(e)


class RenameLayerInput(ToolInput):
    old_name: str = Field(min_length=1, max_length=255)
    new_name: str = Field(min_length=1, max_length=255)


@mcp.tool(name="cad_rename_layer", annotations={"title": "重命名图层",
           "readOnlyHint": False, "destructiveHint": False,
           "idempotentHint": False, "openWorldHint": False})
def cad_rename_layer(params: RenameLayerInput) -> str:
    try:
        return _ok({"ok": True, **_get_backend().rename_layer(params.old_name, params.new_name)})
    except Exception as e:
        return _fail(e)


class DeleteLayerInput(ToolInput):
    names: List[str] = Field(min_length=1, max_length=100)
    require_empty: bool = True
    allow_nonempty: bool = False
    dry_run: bool = True

    @model_validator(mode="after")
    def validate_guard(self):
        if self.require_empty and self.allow_nonempty:
            raise ValueError("require_empty=true 时不能同时 allow_nonempty=true")
        return self


@mcp.tool(name="cad_delete_layer", annotations={"title": "预览或删除图层",
           "readOnlyHint": False, "destructiveHint": True,
           "idempotentHint": True, "openWorldHint": False})
def cad_delete_layer(params: DeleteLayerInput) -> str:
    try:
        return _ok({"ok": True, **_get_backend().delete_layer(
            params.names,
            require_empty=params.require_empty,
            allow_nonempty=params.allow_nonempty,
            dry_run=params.dry_run,
        )})
    except Exception as e:
        return _fail(e)


class DeleteLayoutInput(ToolInput):
    names: List[str] = Field(min_length=1, max_length=100)
    require_empty: bool = True
    dry_run: bool = True


@mcp.tool(name="cad_delete_layout", annotations={"title": "预览或删除布局",
           "readOnlyHint": False, "destructiveHint": True,
           "idempotentHint": True, "openWorldHint": False})
def cad_delete_layout(params: DeleteLayoutInput) -> str:
    try:
        return _ok({"ok": True, **_get_backend().delete_layout(
            params.names, require_empty=params.require_empty, dry_run=params.dry_run
        )})
    except Exception as e:
        return _fail(e)


class AttributeDefinitionInput(ToolInput):
    tag: str = Field(min_length=1, max_length=255)
    prompt: str = Field(default="", max_length=255)
    default_value: str = Field(default="", max_length=256)
    text_style: Optional[str] = Field(default=None, min_length=1, max_length=255)
    text_height: float = Field(default=2.5, gt=0, le=1000000)
    position: List[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0], min_length=2, max_length=3)
    rotation: float = Field(default=0.0, ge=-360, le=360)
    constant: bool = False
    invisible: bool = False
    verify: bool = False
    preset: bool = False


class CreateOrUpdateAttributeBlockInput(ToolInput):
    block_name: str = Field(min_length=1, max_length=255)
    base_point: List[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0], min_length=2, max_length=3)
    entity_handles: List[str] = Field(default_factory=list, max_length=10000)
    attribute_definitions: List[AttributeDefinitionInput] = Field(default_factory=list, max_length=500)
    replace: bool = False
    dry_run: bool = True
    annotative: bool = False


@mcp.tool(name="cad_create_or_update_attribute_block", annotations={"title": "创建带属性定义的块",
           "readOnlyHint": False, "destructiveHint": True,
           "idempotentHint": True, "openWorldHint": False})
def cad_create_or_update_attribute_block(params: CreateOrUpdateAttributeBlockInput) -> str:
    try:
        backend = _get_backend()
        if params.annotative and _backend_mode(backend) != "acad":
            raise UnsupportedModeError("原生注释性属性块仅支持 acad 模式")
        if params.dry_run:
            return _ok({
                "ok": True, "dry_run": True, "block_name": params.block_name,
                "entity_handle_count": len(params.entity_handles),
                "attribute_tags": [item.tag for item in params.attribute_definitions],
                "replace": params.replace, "annotative": params.annotative,
                "would_write": False,
            })
        result = backend.create_or_update_attribute_block(
            params.block_name,
            params.base_point,
            params.entity_handles,
            [item.model_dump(mode="python") for item in params.attribute_definitions],
            params.replace,
            annotative=params.annotative,
        )
        return _ok({"ok": True, "dry_run": False, **result})
    except Exception as e:
        return _fail(e)


class SetBlockAttributesInput(ToolInput):
    block_handle: str = Field(min_length=1, max_length=40)
    values: Dict[str, str] = Field(max_length=256)


@mcp.tool(name="cad_set_block_attributes", annotations={"title": "填写块属性",
           "readOnlyHint": False, "destructiveHint": False,
           "idempotentHint": True, "openWorldHint": False})
def cad_set_block_attributes(params: SetBlockAttributesInput) -> str:
    try:
        return _ok({"ok": True, **_get_backend().set_block_attributes(
            params.block_handle, params.values
        )})
    except Exception as e:
        return _fail(e)


class BindBlockAttributeFieldInput(ToolInput):
    block_handle: str = Field(min_length=1, max_length=40)
    attribute_tag: str = Field(min_length=1, max_length=255)
    source_type: Literal["object"] = "object"
    source_handle: str = Field(min_length=1, max_length=40)
    source_property: str = Field(min_length=1, max_length=80)
    linear_scale_factor: float = Field(default=1.0, gt=0, le=1000000)
    precision: int = Field(default=3, ge=0, le=8)
    prefix: str = Field(default="", max_length=80)
    suffix: str = Field(default="", max_length=80)
    update_on_regen: bool = True


@mcp.tool(name="cad_bind_block_attribute_field", annotations={"title": "绑定块属性到 AutoCAD 字段",
           "readOnlyHint": False, "destructiveHint": False,
           "idempotentHint": True, "openWorldHint": False})
def cad_bind_block_attribute_field(params: BindBlockAttributeFieldInput) -> str:
    try:
        return _ok({"ok": True, **_get_backend().bind_block_attribute_field(
            **params.model_dump(mode="python")
        )})
    except Exception as e:
        return _fail(e)


class ImportBlockDefinitionsInput(ToolInput):
    source_path: str = Field(min_length=1, max_length=2000)
    block_names: List[str] = Field(min_length=1, max_length=500)
    expected_sha256: Optional[str] = Field(
        default=None, pattern=r"^[0-9a-fA-F]{64}$",
        description="正式导入时必填；通常使用 dry_run 返回的 source_hash")
    replace_existing: bool = False
    dry_run: bool = True


@mcp.tool(name="cad_import_block_definitions", annotations={"title": "从外部 DWG 导入块定义",
           "readOnlyHint": False, "destructiveHint": True,
           "idempotentHint": True, "openWorldHint": True})
def cad_import_block_definitions(params: ImportBlockDefinitionsInput) -> str:
    try:
        backend = _get_backend()
        if params.dry_run:
            inspection = backend.inspect_block_library(source_path=params.source_path)
            if (params.expected_sha256 is not None
                    and inspection["file_hash"].lower() != params.expected_sha256.lower()):
                raise ValueError("块库文件与 expected_sha256 不一致")
            missing = [name for name in params.block_names if name.casefold() not in {
                item.casefold() for item in inspection["block_names"]
            }]
            if missing:
                raise ValueError("源块库缺少块定义: " + ", ".join(missing))
            return _ok({
                "ok": True, "dry_run": True,
                "source_path": inspection["source_path"],
                "source_hash": inspection["file_hash"],
                "expected_sha256": params.expected_sha256,
                "block_names": params.block_names,
                "replace_existing": params.replace_existing,
                "would_write": False,
            })
        if params.expected_sha256 is None:
            raise ValueError("正式导入前必须提供 dry_run 核对过的 expected_sha256")
        result = backend.import_block_definitions(
            source_path=params.source_path,
            block_names=params.block_names,
            replace_existing=params.replace_existing,
            expected_sha256=params.expected_sha256,
        )
        return _ok({"ok": True, "dry_run": False, **result})
    except Exception as e:
        return _fail(e)


class ExportBlockLibraryInput(ToolInput):
    target_path: str = Field(min_length=1, max_length=2000)
    block_names: List[str] = Field(min_length=1, max_length=500)
    replace: bool = False
    dry_run: bool = True


@mcp.tool(name="cad_export_block_library", annotations={"title": "预览或导出 DWG 块库",
           "readOnlyHint": False, "destructiveHint": True,
           "idempotentHint": False, "openWorldHint": True})
def cad_export_block_library(params: ExportBlockLibraryInput) -> str:
    try:
        return _ok({"ok": True, **_get_backend().export_block_library(
            target_path=params.target_path,
            block_names=params.block_names,
            replace=params.replace,
            dry_run=params.dry_run,
        )})
    except Exception as e:
        return _fail(e)


class InspectBlockLibraryInput(ToolInput):
    source_path: str = Field(min_length=1, max_length=2000)


@mcp.tool(name="cad_inspect_block_library", annotations={"title": "检查外部 DWG 块库",
           "readOnlyHint": True, "destructiveHint": False,
           "idempotentHint": True, "openWorldHint": True})
def cad_inspect_block_library(params: InspectBlockLibraryInput) -> str:
    try:
        return _ok({"ok": True, **_get_backend().inspect_block_library(
            source_path=params.source_path
        )})
    except Exception as e:
        return _fail(e)


class AuditBlockDefinitionInput(ToolInput):
    source_path: str = Field(
        min_length=1,
        max_length=2000,
        description="要读取的现有 DWG 完整绝对路径",
    )
    block_name: str = Field(
        min_length=1,
        max_length=255,
        description="要审查的块定义名称",
    )
    manifest_path: str = Field(
        min_length=1,
        max_length=2000,
        description="显式规格卡完整绝对路径；文件名必须以 _manifest.json 结尾",
    )


@mcp.tool(name="cad_audit_block_definition", annotations={"title": "按规格卡审查指定 DWG 块",
           "readOnlyHint": True, "destructiveHint": False,
           "idempotentHint": True, "openWorldHint": True})
def cad_audit_block_definition(params: AuditBlockDefinitionInput) -> str:
    """返回指定块的递归实体树、几何分析、三态比较和独立几何指纹。"""
    try:
        return _ok({"ok": True, **_get_backend().audit_block_definition(
            source_path=params.source_path,
            block_name=params.block_name,
            manifest_path=params.manifest_path,
        )})
    except Exception as e:
        return _fail(e)


class InsertCoordinateLabelInput(ToolInput):
    target_x: float
    target_y: float
    target_handle: Optional[str] = Field(default=None, min_length=1, max_length=40)
    display_unit: Literal["m", "mm"] = "m"
    precision: int = Field(default=3, ge=0, le=8)
    suppress_trailing_zeros: bool = False
    associative: bool = True
    block_name: str = Field(default="CAD_MCP_COORD_LABEL", min_length=1, max_length=255)
    layer: str = Field(default="ANNO-COORD", min_length=1, max_length=255)
    text_style: Optional[str] = Field(default=None, min_length=1, max_length=255)


@mcp.tool(name="cad_insert_coordinate_label", annotations={"title": "插入坐标属性块标注",
           "readOnlyHint": False, "destructiveHint": False,
           "idempotentHint": False, "openWorldHint": False})
def cad_insert_coordinate_label(params: InsertCoordinateLabelInput) -> str:
    try:
        return _ok({"ok": True, **_get_backend().insert_coordinate_label(
            **params.model_dump(mode="python")
        )})
    except Exception as e:
        return _fail(e)


class InsertElevationLabelInput(ToolInput):
    target_point: List[float] = Field(min_length=2, max_length=3)
    target_handle: Optional[str] = Field(default=None, min_length=1, max_length=40)
    elevation_source: str = Field(default="auto", min_length=1, max_length=80)
    display_unit: Literal["m", "mm"] = "m"
    precision: int = Field(default=3, ge=0, le=8)
    suppress_trailing_zeros: bool = False
    associative: bool = True
    block_name: str = Field(default="CAD_MCP_ELEV_LABEL", min_length=1, max_length=255)
    layer: str = Field(default="ANNO-ELEV", min_length=1, max_length=255)
    text_style: Optional[str] = Field(default=None, min_length=1, max_length=255)
    positive_sign: bool = False
    zero_format: str = Field(default="0.000", min_length=1, max_length=40)


@mcp.tool(name="cad_insert_elevation_label", annotations={"title": "插入标高属性块标注",
           "readOnlyHint": False, "destructiveHint": False,
           "idempotentHint": False, "openWorldHint": False})
def cad_insert_elevation_label(params: InsertElevationLabelInput) -> str:
    try:
        return _ok({"ok": True, **_get_backend().insert_elevation_label(
            **params.model_dump(mode="python")
        )})
    except Exception as e:
        return _fail(e)


class CreateOrUpdateCtbInput(ToolInput):
    name: str = Field(min_length=5, max_length=255)
    target_directory: str = Field(min_length=1, max_length=2000)
    color_policy: Literal["all_black"] = "all_black"
    use_object_lineweight: bool = True
    grayscale: bool = False
    replace: bool = False
    dry_run: bool = True


@mcp.tool(name="cad_create_or_update_ctb", annotations={"title": "检查 CTB 创建请求",
           "readOnlyHint": False, "destructiveHint": True,
           "idempotentHint": True, "openWorldHint": True})
def cad_create_or_update_ctb(params: CreateOrUpdateCtbInput) -> str:
    try:
        return _ok({"ok": True, **_get_backend().create_or_update_ctb(
            **params.model_dump(mode="python")
        )})
    except Exception as e:
        return _fail(e)


# ================= 工具：模式切换 =================

class SwitchModeInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    mode: Literal["dxf", "acad"] = Field(
        ...,
        description="目标模式：'dxf'=文件模式（无需 CAD，生成 .dxf 文件）；'acad'=直接操控本地 AutoCAD（生成原生 .dwg）",
    )


@mcp.tool(
    name="cad_switch_mode",
    annotations={
        "title": "切换绘图模式",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def cad_switch_mode(params: SwitchModeInput) -> str:
    """切换绘图模式：acad（默认，直接操控本地 AutoCAD 画原生 .dwg）或 dxf（生成 .dxf 文件，无需 CAD）。

    用户要求"直接在 CAD 里画"或"生成 DWG"时切到 acad 模式；要求快速出图纸文件时用 dxf 模式。
    切换后需重新 cad_new_drawing 或 cad_open_drawing 打开目标图纸。

    Args:
        params (SwitchModeInput): mode —— 'dxf' 或 'acad'。

    Returns:
        str: JSON，含当前模式。
    """
    global _backend
    try:
        old = _backend
        current_mode = _backend_mode(old)
        if current_mode == params.mode:
            return _ok({"ok": True, "mode": params.mode,
                        "message": f"当前已经是 {params.mode} 模式"})
        if _backend_has_open_drawing(old):
            raise RuntimeError(
                "当前仍有打开的图纸，禁止直接切换模式。请先调用 "
                "cad_close_drawing，并明确选择 save=true 保存或 save=false 放弃修改")
        # 切模式前释放旧 acad 后端占用的 AutoCAD 独占锁，避免残留
        if hasattr(old, "_release_autocad_lock"):
            try:
                old._release_autocad_lock()
            except Exception:
                pass
        _backend = _create_backend(params.mode)
        return _ok({"ok": True, "mode": params.mode,
                    "message": "已切换到 acad 模式：接下来会直接操控本地 AutoCAD 画图，可生成原生 DWG" if params.mode == "acad"
                    else "已切换到 dxf 模式：生成 .dxf 文件，无需 AutoCAD"})
    except Exception as e:
        return _fail(e)


# ================= 工具：生命周期 =================

class CloseDrawingInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    save: bool = Field(default=False, description="是否保存后关闭，默认 False（不保存直接关）")


@mcp.tool(
    name="cad_close_drawing",
    annotations={
        "title": "关闭当前图纸",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_close_drawing(params: CloseDrawingInput) -> str:
    """关闭当前图纸；dxf 模式也会显式保存或放弃内存中的未保存修改。

    Args:
        params (CloseDrawingInput): save —— True 保存后关闭，False 不保存。

    Returns:
        str: JSON。
    """
    try:
        backend = _get_backend()
        if hasattr(backend, "close_drawing"):
            return _ok({"ok": True, "message": backend.close_drawing(params.save)})
        raise RuntimeError("当前后端不支持安全关闭图纸")
    except Exception as e:
        return _fail(e)


class QuitInput(ToolInput):
    model_config = ConfigDict(str_strip_whitespace=True)
    pass


@mcp.tool(
    name="cad_quit",
    annotations={
        "title": "退出 AutoCAD",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def cad_quit(params: QuitInput) -> str:
    """退出 AutoCAD（acad 模式），释放进程与文件锁，避免锁住 DWG 打不开。dxf 模式无进程，无需退出。

    Args:
        params (QuitInput): 无参数。

    Returns:
        str: JSON。
    """
    try:
        backend = _get_backend()
        if hasattr(backend, "quit"):
            return _ok({"ok": True, "message": backend.quit()})
        return _ok({"ok": True, "message": "dxf 模式无需退出 AutoCAD（文件模式，无进程占用）"})
    except Exception as e:
        return _fail(e)


class GatewayListParams(ToolInput):
    query: Optional[str] = Field(
        default=None,
        max_length=128,
        description="Optional case-insensitive substring used to filter tool names and titles.",
    )


class GatewayDescribeParams(ToolInput):
    tool_name: str = Field(min_length=1, max_length=128)


class GatewayCallParams(ToolInput):
    tool_name: str = Field(min_length=1, max_length=128)
    arguments: Dict[str, Any] = Field(default_factory=dict)


_TOOLSET_ENV = "CAD_MCP_TOOLSET"
_FULL_TOOL_REGISTRY: Dict[str, Any] = {}


def _tool_descriptor(tool) -> Dict[str, Any]:
    annotations = None
    if tool.annotations is not None:
        annotations = tool.annotations.model_dump(mode="json", exclude_none=True)
    return {
        "name": tool.name,
        "title": tool.title,
        "description": tool.description,
        "inputSchema": tool.parameters,
        "outputSchema": tool.output_schema,
        "annotations": annotations,
    }


async def _gateway_list(params: GatewayListParams) -> str:
    query = params.query.casefold() if params.query else None
    items = []
    for tool in _FULL_TOOL_REGISTRY.values():
        title = tool.title or ""
        if query and query not in tool.name.casefold() and query not in title.casefold():
            continue
        items.append(
            {
                "name": tool.name,
                "title": tool.title,
                "description": tool.description,
            }
        )
    return _ok({"ok": True, "count": len(items), "tools": items})


async def _gateway_describe(params: GatewayDescribeParams) -> str:
    tool = _FULL_TOOL_REGISTRY.get(params.tool_name)
    if tool is None:
        return _fail(ValueError(f"未知 CAD 工具: {params.tool_name}"))
    return _ok({"ok": True, "tool": _tool_descriptor(tool)})


async def _gateway_call(params: GatewayCallParams) -> str:
    tool = _FULL_TOOL_REGISTRY.get(params.tool_name)
    if tool is None:
        return _fail(ValueError(f"未知 CAD 工具: {params.tool_name}"))
    try:
        result = await tool.run(params.arguments)
    except ToolError as exc:
        # FastMCP wraps validation failures; preserve their original type/fields.
        if isinstance(exc.__cause__, ValidationError):
            return _fail(exc.__cause__)
        return _fail(exc)
    except Exception as exc:
        return _fail(exc)
    if isinstance(result, str):
        return result
    return _ok({"ok": True, "result": result})


def _configure_tool_exposure() -> None:
    """Keep the full tool surface by default; expose a compact gateway for WebCodex."""
    global _FULL_TOOL_REGISTRY

    _FULL_TOOL_REGISTRY = {
        tool.name: tool
        for tool in mcp._tool_manager.list_tools()
    }
    requested = os.environ.get(_TOOLSET_ENV, "").strip().casefold()
    if requested in {"", "all"}:
        return
    if requested != "webcodex":
        raise ValueError(
            f"{_TOOLSET_ENV}={requested!r} 无效；可选值: all, webcodex"
        )

    for name in sorted(_FULL_TOOL_REGISTRY):
        mcp._tool_manager.remove_tool(name)

    mcp.add_tool(
        _gateway_list,
        name="cad_gateway_list",
        title="列出 CAD 工具",
        description="列出原始 CAD 工具目录，可按名称或标题过滤；调用前用 cad_gateway_describe 查看必填项、可选项和限制。网关工具本身应直接调用，不通过 cad_gateway_call 调用。",
        structured_output=False,
    )
    mcp.add_tool(
        _gateway_describe,
        name="cad_gateway_describe",
        title="查看 CAD 工具 Schema",
        description="返回原始工具的完整 inputSchema：required 必填字段、default 默认值、类型、minimum/maximum 范围及未知字段限制。先查看规范，再按原结构调用。",
        structured_output=False,
    )
    mcp.add_tool(
        _gateway_call,
        name="cad_gateway_call",
        title="调用 CAD 工具",
        description='按名称调用原始 CAD 工具。先用 cad_gateway_describe 查参数规范；arguments 必须包含原工具的 params，例如 {"tool_name":"cad_health","arguments":{"params":{"reconnect":false}}}，不能把 reconnect 等参数直接放在 arguments 顶层。',
        structured_output=False,
    )


_configure_tool_exposure()


def main() -> None:
    """Run the CAD MCP server over stdio."""
    from cad_mcp.core.isolation import install_isolated_execution

    supervisor = install_isolated_execution(mcp, _FULL_TOOL_REGISTRY, mode=_SETTINGS.mode)
    try:
        mcp.run()
    finally:
        supervisor.close()


if __name__ == "__main__":
    main()
