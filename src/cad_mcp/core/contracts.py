"""环境工程语义模型与 CAD 操作计划的数据契约。

本模块只描述“对象是什么”和“准备执行什么”，不连接 AutoCAD，也不进行
工艺选型。项目专用名称、图框和规则应通过外部 JSON 配置传入。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator


Point2D = Tuple[float, float]
ProfilePoint = Tuple[float, float]

MAX_SEMANTIC_OBJECTS = 10_000
MAX_GEOMETRY_POINTS = 100_000
MAX_PLAN_OPERATIONS = 10_000
MAX_NESTED_DEPTH = 16
MAX_NESTED_NODES = 250_000
MAX_NESTED_STRING_CHARACTERS = 2_000_000


def reject_non_finite(value: Any, location: str = "输入") -> None:
    """Recursively reject NaN and infinities hidden inside Any containers."""

    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{location}不能包含 NaN、正无穷或负无穷")
    if isinstance(value, Mapping):
        for key, item in value.items():
            reject_non_finite(item, f"{location}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            reject_non_finite(item, f"{location}[{index}]")


def reject_excessive_nested_content(value: Any) -> None:
    nodes = 0
    string_characters = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes, string_characters
        if depth > MAX_NESTED_DEPTH:
            raise ValueError(f"输入嵌套层级超过 {MAX_NESTED_DEPTH}")
        nodes += 1
        if nodes > MAX_NESTED_NODES:
            raise ValueError(f"输入嵌套节点超过 {MAX_NESTED_NODES}")
        if isinstance(item, str):
            string_characters += len(item)
            if string_characters > MAX_NESTED_STRING_CHARACTERS:
                raise ValueError(
                    f"输入字符串总长度超过 {MAX_NESTED_STRING_CHARACTERS}")
        elif isinstance(item, Mapping):
            for key, child in item.items():
                visit(key, depth + 1)
                visit(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child, depth + 1)

    visit(value, 0)


class StrictModel(BaseModel):
    """所有公开数据契约都拒绝未知字段，避免拼写错误被静默忽略。"""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )

    @model_validator(mode="after")
    def reject_nested_non_finite_values(self):
        reject_non_finite(self.model_dump(mode="python"))
        return self


class OutlineSpec(StrictModel):
    """构筑物平面外轮廓；尺寸均使用项目模型单位。"""

    shape: Literal["rectangle", "circle", "polygon"]
    length: Optional[float] = Field(default=None, gt=0)
    width: Optional[float] = Field(default=None, gt=0)
    radius: Optional[float] = Field(default=None, gt=0)
    points: List[Point2D] = Field(default_factory=list, max_length=10_000)

    @model_validator(mode="after")
    def validate_shape_data(self) -> "OutlineSpec":
        if self.shape == "rectangle" and (self.length is None or self.width is None):
            raise ValueError("rectangle 外轮廓必须提供 length 和 width")
        if self.shape == "circle" and self.radius is None:
            raise ValueError("circle 外轮廓必须提供 radius")
        if self.shape == "polygon" and len(self.points) < 3:
            raise ValueError("polygon 外轮廓至少需要 3 个点")
        return self


class FacilityComponent(StrictModel):
    """可组合的通用构筑物部件。"""

    id: str = Field(min_length=1, max_length=120)
    kind: str = Field(
        description="通用部件类型，如 partition_wall/channel/weir/opening/platform/equipment",
        min_length=1,
        max_length=80,
    )
    geometry: Dict[str, Any] = Field(default_factory=dict)
    bottom_elevation: Optional[float] = None
    top_elevation: Optional[float] = None
    layer: Optional[str] = Field(default=None, max_length=80)
    attributes: Dict[str, Any] = Field(default_factory=dict)


class Facility(StrictModel):
    """环境工程构筑物或厂房的参数化描述。"""

    id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=160)
    facility_type: str = Field(min_length=1, max_length=80)
    origin: Point2D = (0.0, 0.0)
    rotation: float = Field(default=0.0, ge=-360.0, le=360.0)
    outline: OutlineSpec
    bottom_elevation: Optional[float] = None
    top_elevation: Optional[float] = None
    wall_thickness: Optional[float] = Field(default=None, gt=0)
    layer: str = Field(default="ENV-FACILITY", min_length=1, max_length=80)
    components: List[FacilityComponent] = Field(default_factory=list, max_length=2_000)
    attributes: Dict[str, Any] = Field(default_factory=dict)


class NetworkNode(StrictModel):
    """管网、渠道、风管或输送线路中的连接节点。"""

    id: str = Field(min_length=1, max_length=120)
    node_type: str = Field(default="junction", min_length=1, max_length=80)
    point: Point2D
    elevation: Optional[float] = None
    equipment_id: Optional[str] = Field(default=None, max_length=120)
    port_id: Optional[str] = Field(default=None, max_length=120)
    attributes: Dict[str, Any] = Field(default_factory=dict)


class NetworkSegment(StrictModel):
    """两个显式节点之间的网络段；交叉线不会自动变成连接。"""

    id: str = Field(min_length=1, max_length=120)
    start_node: str = Field(min_length=1, max_length=120)
    end_node: str = Field(min_length=1, max_length=120)
    path: List[Point2D] = Field(min_length=2, max_length=10_000)
    segment_type: Literal["pipe", "channel", "duct", "conveyor", "other"] = "pipe"
    section: Dict[str, Any] = Field(default_factory=dict)
    material: Optional[str] = Field(default=None, max_length=120)
    start_elevation: Optional[float] = None
    end_elevation: Optional[float] = None
    design_slope: Optional[float] = None
    flow_direction: Literal["start_to_end", "end_to_start", "unspecified"] = "unspecified"
    layer: Optional[str] = Field(default=None, max_length=80)
    attributes: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_endpoints(self) -> "NetworkSegment":
        if self.start_node == self.end_node:
            raise ValueError("网络段的起点节点和终点节点不能相同")
        if all(a == b for a, b in zip(self.path[0], self.path[-1])):
            raise ValueError("网络段路径的首尾点不能重合")
        return self


class NetworkSystem(StrictModel):
    """一种介质或用途对应的一套网络。"""

    id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=160)
    medium: str = Field(min_length=1, max_length=120)
    default_segment_type: Literal["pipe", "channel", "duct", "conveyor", "other"] = "pipe"
    layer: str = Field(default="ENV-NETWORK", min_length=1, max_length=80)
    linetype: str = Field(default="CONTINUOUS", min_length=1, max_length=80)
    color: int = Field(default=7, ge=1, le=255)
    lineweight: Optional[int] = Field(default=None, ge=-3, le=211)
    nodes: List[NetworkNode] = Field(default_factory=list, max_length=10_000)
    segments: List[NetworkSegment] = Field(default_factory=list, max_length=10_000)
    attributes: Dict[str, Any] = Field(default_factory=dict)


class AnnotationSpec(StrictModel):
    """与某个视图和可选语义对象关联的工程标注。"""

    id: str = Field(min_length=1, max_length=120)
    annotation_type: str = Field(min_length=1, max_length=80)
    view_id: str = Field(min_length=1, max_length=120)
    anchor: Point2D
    text: Optional[str] = Field(default=None, max_length=1000)
    value: Optional[float] = None
    target_id: Optional[str] = Field(default=None, max_length=120)
    leader_point: Optional[Point2D] = None
    rotation: float = Field(default=0.0, ge=-360.0, le=360.0)
    paper_height: float = Field(default=3.5, gt=0, le=50)
    layer: str = Field(default="ENV-ANNO", min_length=1, max_length=80)
    style: Optional[str] = Field(default=None, max_length=80)
    options: Dict[str, Any] = Field(default_factory=dict)


class ProfileSeries(StrictModel):
    """按桩号给出的地面线、管底线、水位线等高程序列。"""

    id: str = Field(min_length=1, max_length=120)
    series_type: str = Field(min_length=1, max_length=80)
    points: List[ProfilePoint] = Field(min_length=2, max_length=10_000)
    layer: str = Field(default="ENV-PROFILE", min_length=1, max_length=80)
    attributes: Dict[str, Any] = Field(default_factory=dict)


class ProfileDefinition(StrictModel):
    """纵断面视图定义，水平与垂直比例可以不同。"""

    id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=160)
    origin: Point2D = (0.0, 0.0)
    datum_elevation: float
    horizontal_scale_denominator: float = Field(gt=0)
    vertical_scale_denominator: float = Field(gt=0)
    series: List[ProfileSeries] = Field(default_factory=list, max_length=1_000)
    network_system_id: Optional[str] = Field(default=None, max_length=120)
    segment_ids: List[str] = Field(default_factory=list, max_length=10_000)
    attributes: Dict[str, Any] = Field(default_factory=dict)


class ScheduleColumn(StrictModel):
    key: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=120)
    width: float = Field(gt=0)
    format: Optional[str] = Field(default=None, max_length=80)


class ScheduleDefinition(StrictModel):
    """从语义对象查询并形成表格的声明。"""

    id: str = Field(min_length=1, max_length=120)
    title: str = Field(default="", max_length=160)
    schedule_type: str = Field(default="generic", min_length=1, max_length=80)
    view_id: Optional[str] = Field(default=None, max_length=120)
    source_type: Literal["facility", "network_system", "network_segment", "annotation", "drawing"]
    columns: List[ScheduleColumn] = Field(min_length=1, max_length=64)
    origin: Point2D
    row_height: float = Field(gt=0)
    layer: str = Field(default="ENV-TABLE", min_length=1, max_length=80)
    sort_by: List[str] = Field(default_factory=list, max_length=64)
    filters: Dict[str, Any] = Field(default_factory=dict)
    template_id: Optional[str] = Field(default=None, max_length=120)
    template_sheet_id: Optional[str] = Field(default=None, max_length=120)
    template_table_id: Optional[str] = Field(default=None, max_length=120)
    layout_name: Optional[str] = Field(default=None, max_length=120)
    max_rows_per_table: Optional[int] = Field(default=None, gt=0, le=1_000)

    @model_validator(mode="after")
    def validate_template_mapping(self) -> "ScheduleDefinition":
        values = (self.template_id, self.template_sheet_id,
                  self.template_table_id, self.layout_name)
        if any(value is not None for value in values) and not all(value is not None for value in values):
            raise ValueError(
                "填写模板表格时必须同时提供 template_id、template_sheet_id、"
                "template_table_id 和 layout_name")
        return self


class ViewDefinition(StrictModel):
    """一张图中的一个视图。"""

    id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=160)
    view_type: Literal["plan", "section", "elevation", "profile", "schematic"]
    scale_denominator: float = Field(gt=0)
    origin: Point2D = (0.0, 0.0)
    facility_ids: List[str] = Field(default_factory=list, max_length=10_000)
    network_system_ids: List[str] = Field(default_factory=list, max_length=10_000)
    profile_ids: List[str] = Field(default_factory=list, max_length=10_000)
    annotation_ids: List[str] = Field(default_factory=list, max_length=10_000)
    clipping_polygon: List[Point2D] = Field(default_factory=list, max_length=10_000)
    attributes: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_clipping_polygon(self) -> "ViewDefinition":
        if self.clipping_polygon and len(self.clipping_polygon) < 3:
            raise ValueError("clipping_polygon 留空或至少提供 3 个点")
        return self


class SheetDefinition(StrictModel):
    """固定图框中的布局及视图集合。"""

    id: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=160)
    layout_name: str = Field(min_length=1, max_length=120)
    paper_size: str = Field(min_length=1, max_length=40)
    template_id: Optional[str] = Field(default=None, max_length=120)
    view_ids: List[str] = Field(min_length=1, max_length=1_000)
    drawing_number: Optional[str] = Field(default=None, max_length=80)
    attributes: Dict[str, Any] = Field(default_factory=dict)


class EnvironmentModel(StrictModel):
    """与具体工艺无关的环境工程项目模型。"""

    schema_version: Literal["1.0"] = "1.0"
    project_id: str = Field(min_length=1, max_length=120)
    project_name: str = Field(min_length=1, max_length=200)
    units: Literal["mm"] = "mm"
    revision: str = Field(default="1", min_length=1, max_length=80)
    facilities: List[Facility] = Field(default_factory=list, max_length=10_000)
    network_systems: List[NetworkSystem] = Field(default_factory=list, max_length=10_000)
    profiles: List[ProfileDefinition] = Field(default_factory=list, max_length=10_000)
    annotations: List[AnnotationSpec] = Field(default_factory=list, max_length=10_000)
    schedules: List[ScheduleDefinition] = Field(default_factory=list, max_length=10_000)
    views: List[ViewDefinition] = Field(default_factory=list, max_length=10_000)
    sheets: List[SheetDefinition] = Field(default_factory=list, max_length=10_000)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_model_complexity(self):
        semantic_objects = (
            len(self.facilities) + len(self.network_systems) + len(self.profiles)
            + len(self.annotations) + len(self.schedules) + len(self.views)
            + len(self.sheets)
            + sum(len(item.components) for item in self.facilities)
            + sum(len(item.nodes) + len(item.segments) for item in self.network_systems)
            + sum(len(item.series) for item in self.profiles)
        )
        if semantic_objects > MAX_SEMANTIC_OBJECTS:
            raise ValueError(
                f"语义对象总数 {semantic_objects} 超过单个模型上限 {MAX_SEMANTIC_OBJECTS}")
        geometry_points = (
            sum(len(item.outline.points) for item in self.facilities)
            + sum(len(segment.path) for system in self.network_systems
                  for segment in system.segments)
            + sum(len(series.points) for profile in self.profiles
                  for series in profile.series)
            + sum(len(view.clipping_polygon) for view in self.views)
        )
        if geometry_points > MAX_GEOMETRY_POINTS:
            raise ValueError(
                f"几何点总数 {geometry_points} 超过单个模型上限 {MAX_GEOMETRY_POINTS}")
        reject_excessive_nested_content(self.model_dump(mode="python"))
        return self

    def iter_semantic_ids(self) -> Iterable[str]:
        yield from (item.id for item in self.facilities)
        for facility in self.facilities:
            yield from (item.id for item in facility.components)
        for system in self.network_systems:
            yield system.id
            yield from (item.id for item in system.nodes)
            yield from (item.id for item in system.segments)
        yield from (item.id for item in self.profiles)
        yield from (item.id for item in self.annotations)
        yield from (item.id for item in self.schedules)
        yield from (item.id for item in self.views)
        yield from (item.id for item in self.sheets)


class CadOperation(StrictModel):
    """后端无关、可预检的单个 CAD 操作。"""

    kind: str = Field(min_length=1, max_length=80)
    semantic_id: str = Field(min_length=1, max_length=120)
    view_id: Optional[str] = Field(default=None, max_length=120)
    role: str = Field(default="geometry", min_length=1, max_length=80)
    params: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class DrawingPlan(StrictModel):
    """预检通过后才能交给后端执行的完整操作计划。"""

    project_id: str
    revision: str
    view_ids: List[str] = Field(default_factory=list)
    operations: List[CadOperation] = Field(default_factory=list, max_length=MAX_PLAN_OPERATIONS)
    warnings: List[str] = Field(default_factory=list)
    statistics: Dict[str, int] = Field(default_factory=dict)


class ValidationIssue(StrictModel):
    severity: Literal["error", "warning", "info"]
    rule_id: str
    message: str
    semantic_ids: List[str] = Field(default_factory=list)
    entity_handles: List[str] = Field(default_factory=list)
    location: Optional[Point2D] = None
    suggested_action: Optional[str] = None


class ValidationReport(StrictModel):
    ok: bool
    issues: List[ValidationIssue] = Field(default_factory=list)
    checked_counts: Dict[str, int] = Field(default_factory=dict)


class RuleDefinition(StrictModel):
    """安全、声明式的项目规则；不允许执行任意 Python 表达式。"""

    rule_id: str = Field(min_length=1, max_length=120)
    target_type: Literal[
        "project", "facility", "facility_component", "network_system",
        "network_node", "network_segment", "profile", "annotation",
        "schedule", "view", "sheet", "drawing",
    ]
    operator: Literal[
        "required", "unique", "range", "relation", "connectivity",
        "slope_consistency", "inside", "style_match", "artifact_present",
    ]
    severity: Literal["error", "warning", "info"] = "error"
    fields: List[str] = Field(default_factory=list, max_length=64)
    parameters: Dict[str, Any] = Field(default_factory=dict, max_length=256)
    message: Optional[str] = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def validate_rule_complexity(self):
        if any(len(field) > 120 for field in self.fields):
            raise ValueError("规则字段路径最多允许 120 个字符")
        reject_excessive_nested_content(self.model_dump(mode="python"))
        return self
