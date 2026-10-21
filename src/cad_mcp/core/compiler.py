"""把环境工程语义模型编译为后端无关的 CAD 操作计划。"""

from __future__ import annotations

from collections import Counter
from typing import Dict, Iterable, List, Optional, Sequence, Set

from cad_mcp.drawing.annotations import compile_annotation
from cad_mcp.core.contracts import (
    CadOperation,
    DrawingPlan,
    EnvironmentModel,
    MAX_PLAN_OPERATIONS,
    ValidationReport,
)
from cad_mcp.engineering.facilities import compile_facility_plan, compile_facility_section
from cad_mcp.core.geometry import validate_table_data
from cad_mcp.engineering.network import compile_network
from cad_mcp.engineering.profile import compile_profile
from cad_mcp.engineering.schedules import compile_schedule
from cad_mcp.engineering.standards import AnyDraftingProfile, apply_layer_styles
from cad_mcp.core.validation import validate_model
from cad_mcp.core.semantics import model_objects, object_metadata


class ModelValidationError(ValueError):
    def __init__(self, report: ValidationReport):
        self.report = report
        messages = [issue.message for issue in report.issues if issue.severity == "error"]
        super().__init__("环境工程模型预检失败：" + "；".join(messages[:8]))


COMMON_LAYERS = (
    {"name": "ENV-ANNO", "color": 7, "linetype": "CONTINUOUS"},
    {"name": "ENV-TABLE", "color": 7, "linetype": "CONTINUOUS"},
    {"name": "ENV-PROFILE", "color": 3, "linetype": "CONTINUOUS"},
    {"name": "ENV-PROFILE-GRID", "color": 8, "linetype": "CONTINUOUS"},
    {"name": "ENV-PROFILE-NETWORK", "color": 1, "linetype": "CONTINUOUS"},
)


def _deduplicate_layer_operations(operations: Sequence[CadOperation]) -> List[CadOperation]:
    seen: Set[str] = set()
    result: List[CadOperation] = []
    for operation in operations:
        if operation.kind != "ensure_layer":
            result.append(operation)
            continue
        name = str(operation.params.get("name", ""))
        if name in seen:
            continue
        seen.add(name)
        result.append(operation)
    return result


def compile_environment_model(
    model: EnvironmentModel,
    view_ids: Optional[Sequence[str]] = None,
    include_schedules: bool = True,
    allow_validation_errors: bool = False,
    drafting_profile: Optional[AnyDraftingProfile] = None,
) -> tuple[DrawingPlan, ValidationReport]:
    """先完整预检，再生成确定性操作；默认有错误时不产生可执行计划。"""

    report = validate_model(model)
    if not report.ok and not allow_validation_errors:
        raise ModelValidationError(report)
    selected = set(view_ids or [view.id for view in model.views])
    unknown = selected - {view.id for view in model.views}
    if unknown:
        raise ValueError(f"请求了不存在的视图: {', '.join(sorted(unknown))}")
    facilities = {item.id: item for item in model.facilities}
    networks = {item.id: item for item in model.network_systems}
    profiles = {item.id: item for item in model.profiles}
    annotations = {item.id: item for item in model.annotations}
    operations: List[CadOperation] = []

    def extend_checked(items: Iterable[CadOperation]) -> None:
        batch = list(items)
        for operation in batch:
            if operation.kind in {"table", "template_table"}:
                validate_table_data(
                    operation.params.get("col_widths", []),
                    operation.params.get("headers", []),
                    operation.params.get("rows", []),
                )
        if len(operations) + len(batch) > MAX_PLAN_OPERATIONS:
            raise ValueError(
                f"环境工程计划操作数超过单次上限 {MAX_PLAN_OPERATIONS}；"
                "请减少视图范围或拆分应用")
        operations.extend(batch)

    warnings = [issue.message for issue in report.issues if issue.severity == "warning"]
    for layer in COMMON_LAYERS:
        extend_checked([CadOperation(
            kind="ensure_layer", semantic_id="__environment_layers__", role="layer",
            params=dict(layer), metadata={"object_type": "layer"},
        )])
    for view in model.views:
        if view.id not in selected:
            continue
        if view.view_type in {"plan", "schematic"}:
            for facility_id in view.facility_ids:
                extend_checked(compile_facility_plan(
                    facilities[facility_id], view.id, view.scale_denominator))
            for system_id in view.network_system_ids:
                extend_checked(compile_network(
                    networks[system_id], view.id, view.scale_denominator))
        elif view.view_type in {"section", "elevation"}:
            if len(view.facility_ids) > 1:
                missing_origins = [facility_id for facility_id in view.facility_ids
                                   if "section_origin" not in facilities[facility_id].attributes]
                if missing_origins:
                    raise ValueError(
                        f"视图 {view.id} 含多个构筑物，必须为每个构筑物提供 section_origin: "
                        f"{', '.join(missing_origins)}")
            for facility_id in view.facility_ids:
                facility = facilities[facility_id]
                origin = tuple(facility.attributes.get("section_origin", view.origin))
                extend_checked(compile_facility_section(
                    facility, view.id, view.scale_denominator, origin))
        elif view.view_type == "profile":
            for profile_id in view.profile_ids:
                extend_checked(compile_profile(
                    profiles[profile_id], view.id, view.scale_denominator))
        for annotation_id in view.annotation_ids:
            extend_checked(compile_annotation(
                annotations[annotation_id], view.scale_denominator))
    if include_schedules:
        for schedule in model.schedules:
            if schedule.view_id is not None and schedule.view_id not in selected:
                continue
            extend_checked(compile_schedule(model, schedule, schedule.view_id))
    operations = _deduplicate_layer_operations(operations)
    if drafting_profile is not None:
        operations = apply_layer_styles(operations, drafting_profile)
    identities = {obj.object_id: obj for obj in model_objects(model)}
    for operation in operations:
        if operation.kind != "ensure_layer":
            identity = identities.get(operation.semantic_id)
            if identity is None:
                raise ValueError(f"绘图操作缺少工程对象定义: {operation.semantic_id}")
            operation.metadata.update(object_metadata(identity))
    statistics = dict(Counter(operation.kind for operation in operations))
    plan = DrawingPlan(
        project_id=model.project_id,
        revision=model.revision,
        view_ids=sorted(selected),
        operations=operations,
        warnings=warnings,
        statistics=statistics,
    )
    return plan, report
