"""环境工程语义模型的内置检查与安全声明式规则执行器。"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from cad_mcp.drawing.annotations import validate_annotation
from cad_mcp.core.contracts import (
    EnvironmentModel,
    RuleDefinition,
    ValidationIssue,
    ValidationReport,
)
from cad_mcp.engineering.facilities import validate_facility
from cad_mcp.engineering.network import validate_network
from cad_mcp.engineering.profile import validate_profile
from cad_mcp.engineering.schedules import validate_schedule


def _get_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if hasattr(current, part):
            current = getattr(current, part)
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def iter_objects(model: EnvironmentModel, target_type: str) -> Iterable[Any]:
    if target_type == "project":
        yield model
    elif target_type == "facility":
        yield from model.facilities
    elif target_type == "facility_component":
        for facility in model.facilities:
            yield from facility.components
    elif target_type == "network_system":
        yield from model.network_systems
    elif target_type == "network_node":
        for system in model.network_systems:
            yield from system.nodes
    elif target_type == "network_segment":
        for system in model.network_systems:
            yield from system.segments
    elif target_type == "profile":
        yield from model.profiles
    elif target_type == "annotation":
        yield from model.annotations
    elif target_type == "schedule":
        yield from model.schedules
    elif target_type == "view":
        yield from model.views
    elif target_type in {"sheet", "drawing"}:
        yield from model.sheets


def _semantic_id(item: Any) -> str:
    return str(getattr(item, "id", ""))


def _message(rule: RuleDefinition, default: str) -> str:
    return rule.message or default


def _apply_required(rule: RuleDefinition, items: Sequence[Any]) -> List[ValidationIssue]:
    if not rule.fields:
        return [ValidationIssue(
            severity="error", rule_id="RULE_CONFIGURATION_INVALID",
            message=f"必填规则 {rule.rule_id} 至少需要配置一个字段",
            semantic_ids=[],
        )]
    issues: List[ValidationIssue] = []
    for item in items:
        missing = [field for field in rule.fields
                   if _get_path(item, field) in (None, "", [], {})]
        if missing:
            issues.append(ValidationIssue(
                severity=rule.severity,
                rule_id=rule.rule_id,
                message=_message(rule, f"对象 {_semantic_id(item)} 缺少必填字段: {', '.join(missing)}"),
                semantic_ids=[_semantic_id(item)],
            ))
    return issues


def _apply_unique(rule: RuleDefinition, items: Sequence[Any]) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    fields = rule.fields or ["id"]
    for field in fields:
        values: Dict[str, List[str]] = {}
        for item in items:
            value = _get_path(item, field)
            key = repr(value)
            values.setdefault(key, []).append(_semantic_id(item))
        for key, ids in values.items():
            if len(ids) > 1:
                issues.append(ValidationIssue(
                    severity=rule.severity,
                    rule_id=rule.rule_id,
                    message=_message(rule, f"字段 {field} 的值 {key} 重复"),
                    semantic_ids=ids,
                ))
    return issues


def _apply_range(rule: RuleDefinition, items: Sequence[Any]) -> List[ValidationIssue]:
    if not rule.fields:
        return [ValidationIssue(
            severity="error", rule_id="RULE_CONFIGURATION_INVALID",
            message=f"范围规则 {rule.rule_id} 至少需要配置一个字段",
            semantic_ids=[],
        )]
    issues: List[ValidationIssue] = []
    minimum = rule.parameters.get("min")
    maximum = rule.parameters.get("max")
    try:
        minimum_value = float(minimum) if minimum is not None else None
        maximum_value = float(maximum) if maximum is not None else None
    except (TypeError, ValueError):
        return [ValidationIssue(
            severity="error", rule_id="RULE_CONFIGURATION_INVALID",
            message=f"范围规则 {rule.rule_id} 的 min/max 必须是数值",
            semantic_ids=[],
        )]
    if ((minimum_value is not None and not math.isfinite(minimum_value))
            or (maximum_value is not None and not math.isfinite(maximum_value))):
        return [ValidationIssue(
            severity="error", rule_id="RULE_CONFIGURATION_INVALID",
            message=f"范围规则 {rule.rule_id} 的 min/max 必须是有限数值",
            semantic_ids=[],
        )]
    if (minimum_value is not None and maximum_value is not None
            and minimum_value > maximum_value):
        return [ValidationIssue(
            severity="error", rule_id="RULE_CONFIGURATION_INVALID",
            message=f"范围规则 {rule.rule_id} 的 min 不能大于 max",
            semantic_ids=[],
        )]
    for item in items:
        for field in rule.fields:
            value = _get_path(item, field)
            if value is None:
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                issues.append(ValidationIssue(
                    severity=rule.severity, rule_id=rule.rule_id,
                    message=_message(rule, f"字段 {field} 不是数值"),
                    semantic_ids=[_semantic_id(item)],
                ))
                continue
            if not math.isfinite(numeric):
                issues.append(ValidationIssue(
                    severity=rule.severity, rule_id=rule.rule_id,
                    message=_message(rule, f"字段 {field} 不是有限数值"),
                    semantic_ids=[_semantic_id(item)],
                ))
                continue
            if ((minimum_value is not None and numeric < minimum_value)
                    or (maximum_value is not None and numeric > maximum_value)):
                issues.append(ValidationIssue(
                    severity=rule.severity, rule_id=rule.rule_id,
                    message=_message(rule, f"对象 {_semantic_id(item)} 的 {field}={numeric} 超出允许范围"),
                    semantic_ids=[_semantic_id(item)],
                ))
    return issues


def _apply_relation(rule: RuleDefinition, items: Sequence[Any]) -> List[ValidationIssue]:
    """安全字段关系检查，只支持白名单比较符，不执行表达式。"""

    issues: List[ValidationIssue] = []
    if len(rule.fields) != 2:
        return [ValidationIssue(
            severity="error", rule_id="RULE_CONFIGURATION_INVALID",
            message=f"关系规则 {rule.rule_id} 必须配置两个字段",
            semantic_ids=[],
        )]
    operator = rule.parameters.get("comparison", "lt")
    comparisons = {
        "lt": lambda a, b: a < b,
        "le": lambda a, b: a <= b,
        "gt": lambda a, b: a > b,
        "ge": lambda a, b: a >= b,
        "eq": lambda a, b: a == b,
        "ne": lambda a, b: a != b,
    }
    if operator not in comparisons:
        return [ValidationIssue(
            severity="error", rule_id="RULE_CONFIGURATION_INVALID",
            message=f"关系规则 {rule.rule_id} 使用了不支持的比较符 {operator}",
            semantic_ids=[],
        )]
    left_field, right_field = rule.fields
    for item in items:
        left, right = _get_path(item, left_field), _get_path(item, right_field)
        if left is None or right is None:
            continue
        try:
            matches = comparisons[operator](left, right)
        except (TypeError, ValueError) as exc:
            issues.append(ValidationIssue(
                severity=rule.severity, rule_id=rule.rule_id,
                message=_message(
                    rule,
                    f"对象 {_semantic_id(item)} 的 {left_field} 与 {right_field} "
                    f"无法比较: {exc}"),
                semantic_ids=[_semantic_id(item)],
            ))
            continue
        if not matches:
            issues.append(ValidationIssue(
                severity=rule.severity, rule_id=rule.rule_id,
                message=_message(rule, f"对象 {_semantic_id(item)} 不满足 {left_field} {operator} {right_field}"),
                semantic_ids=[_semantic_id(item)],
            ))
    return issues


def _apply_style_match(rule: RuleDefinition, items: Sequence[Any]) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    expected = rule.parameters.get("expected", {})
    if not isinstance(expected, dict) or not expected:
        return [ValidationIssue(
            severity="error", rule_id="RULE_CONFIGURATION_INVALID",
            message=f"样式规则 {rule.rule_id} 的 expected 必须是非空对象",
            semantic_ids=[],
        )]
    for item in items:
        mismatches = []
        for field, value in expected.items():
            actual = _get_path(item, field)
            if actual != value:
                mismatches.append(f"{field}={actual!r}（应为 {value!r}）")
        if mismatches:
            issues.append(ValidationIssue(
                severity=rule.severity, rule_id=rule.rule_id,
                message=_message(rule, f"对象 {_semantic_id(item)} 样式不匹配: {', '.join(mismatches)}"),
                semantic_ids=[_semantic_id(item)],
            ))
    return issues


def _apply_artifact_present(
    rule: RuleDefinition,
    model: EnvironmentModel,
) -> List[ValidationIssue]:
    collection = rule.parameters.get("collection", "sheets")
    allowed_collections = {
        "facilities", "network_systems", "profiles", "annotations",
        "schedules", "views", "sheets",
    }
    if not isinstance(collection, str) or collection not in allowed_collections:
        return [ValidationIssue(
            severity="error", rule_id="RULE_CONFIGURATION_INVALID",
            message=f"成果规则 {rule.rule_id} 的 collection 不受支持: {collection!r}",
            semantic_ids=[],
        )]
    required_types = rule.parameters.get("required_types", [])
    if not isinstance(required_types, list):
        return [ValidationIssue(
            severity="error", rule_id="RULE_CONFIGURATION_INVALID",
            message=f"成果规则 {rule.rule_id} 的 required_types 必须是数组",
            semantic_ids=[],
        )]
    type_field = rule.parameters.get("type_field", "view_type")
    if not isinstance(type_field, str) or not type_field:
        return [ValidationIssue(
            severity="error", rule_id="RULE_CONFIGURATION_INVALID",
            message=f"成果规则 {rule.rule_id} 的 type_field 必须是非空字符串",
            semantic_ids=[],
        )]
    values = _get_path(model, collection)
    if not values:
        return [ValidationIssue(
            severity=rule.severity, rule_id=rule.rule_id,
            message=_message(rule, f"项目缺少必需内容集合 {collection}"),
            semantic_ids=[model.project_id],
        )]
    if required_types:
        actual = {_get_path(item, type_field) for item in values}
        missing = [item for item in required_types if item not in actual]
        if missing:
            return [ValidationIssue(
                severity=rule.severity, rule_id=rule.rule_id,
                message=_message(rule, f"项目缺少必需类型: {', '.join(map(str, missing))}"),
                semantic_ids=[model.project_id],
            )]
    return []


def apply_rule(rule: RuleDefinition, model: EnvironmentModel) -> List[ValidationIssue]:
    items = list(iter_objects(model, rule.target_type))
    if rule.operator == "required":
        return _apply_required(rule, items)
    if rule.operator == "unique":
        return _apply_unique(rule, items)
    if rule.operator == "range":
        return _apply_range(rule, items)
    if rule.operator == "relation":
        return _apply_relation(rule, items)
    if rule.operator == "style_match":
        return _apply_style_match(rule, items)
    if rule.operator == "artifact_present":
        return _apply_artifact_present(rule, model)
    # connectivity/slope_consistency 已由通用网络检查完整执行。
    if rule.operator in {"connectivity", "slope_consistency"}:
        if rule.target_type not in {"project", "network_system"}:
            return [ValidationIssue(
                severity="error", rule_id="RULE_CONFIGURATION_INVALID",
                message=(f"规则 {rule.rule_id} 的 {rule.operator} 只能用于 "
                         "project 或 network_system"),
                semantic_ids=[],
            )]
        if rule.parameters:
            return [ValidationIssue(
                severity="error", rule_id="RULE_CONFIGURATION_INVALID",
                message=f"规则 {rule.rule_id} 当前不支持自定义参数，已停止以避免静默忽略",
                semantic_ids=[],
            )]
        network_issues = [issue for system in model.network_systems
                          for issue in validate_network(system)]
        prefix = "NETWORK_"
        if rule.operator == "connectivity":
            allowed = {"NETWORK_MISSING_ENDPOINT_NODE", "NETWORK_START_POINT_MISMATCH",
                       "NETWORK_END_POINT_MISMATCH", "NETWORK_ISOLATED_NODE",
                       "NETWORK_DISCONNECTED_COMPONENTS", "NETWORK_UNMODELED_CROSSING"}
        else:
            allowed = {"NETWORK_SLOPE_INCONSISTENT", "NETWORK_REVERSE_SLOPE"}
        selected = [issue for issue in network_issues if issue.rule_id in allowed]
        for issue in selected:
            issue.rule_id = rule.rule_id
            issue.severity = rule.severity
            if rule.message:
                issue.message = rule.message
        return selected
    if rule.operator == "inside":
        return [ValidationIssue(
            severity=rule.severity, rule_id="RULE_NOT_APPLICABLE",
            message=f"规则 {rule.rule_id} 的 inside 检查需要在编译后的图元范围阶段执行",
            semantic_ids=[],
        )]
    return []


def validate_model(
    model: EnvironmentModel,
    rules: Sequence[RuleDefinition] = (),
) -> ValidationReport:
    issues: List[ValidationIssue] = []
    ids = list(model.iter_semantic_ids())
    for semantic_id, count in Counter(ids).items():
        if count > 1:
            issues.append(ValidationIssue(
                severity="error",
                rule_id="MODEL_DUPLICATE_SEMANTIC_ID",
                message=f"语义编号 {semantic_id} 在项目模型中出现 {count} 次",
                semantic_ids=[semantic_id],
            ))
    view_ids = {view.id for view in model.views}
    facility_ids = {facility.id for facility in model.facilities}
    network_ids = {system.id for system in model.network_systems}
    profile_ids = {profile.id for profile in model.profiles}
    annotation_ids = {annotation.id for annotation in model.annotations}
    network_segment_ids = {
        system.id: {segment.id for segment in system.segments}
        for system in model.network_systems
    }
    for view in model.views:
        for item_id in view.facility_ids:
            if item_id not in facility_ids:
                issues.append(ValidationIssue(
                    severity="error", rule_id="VIEW_UNKNOWN_FACILITY",
                    message=f"视图 {view.id} 引用了不存在的构筑物 {item_id}",
                    semantic_ids=[view.id, item_id],
                ))
        for item_id in view.network_system_ids:
            if item_id not in network_ids:
                issues.append(ValidationIssue(
                    severity="error", rule_id="VIEW_UNKNOWN_NETWORK",
                    message=f"视图 {view.id} 引用了不存在的网络 {item_id}",
                    semantic_ids=[view.id, item_id],
                ))
        for item_id in view.profile_ids:
            if item_id not in profile_ids:
                issues.append(ValidationIssue(
                    severity="error", rule_id="VIEW_UNKNOWN_PROFILE",
                    message=f"视图 {view.id} 引用了不存在的纵断面 {item_id}",
                    semantic_ids=[view.id, item_id],
                ))
        for item_id in view.annotation_ids:
            if item_id not in annotation_ids:
                issues.append(ValidationIssue(
                    severity="error", rule_id="VIEW_UNKNOWN_ANNOTATION",
                    message=f"视图 {view.id} 引用了不存在的标注 {item_id}",
                    semantic_ids=[view.id, item_id],
                ))
    for sheet in model.sheets:
        missing = [view_id for view_id in sheet.view_ids if view_id not in view_ids]
        if missing:
            issues.append(ValidationIssue(
                severity="error", rule_id="SHEET_UNKNOWN_VIEW",
                message=f"图纸 {sheet.id} 引用了不存在的视图: {', '.join(missing)}",
                semantic_ids=[sheet.id, *missing],
            ))
    for profile in model.profiles:
        if profile.segment_ids and profile.network_system_id is None:
            issues.append(ValidationIssue(
                severity="error", rule_id="PROFILE_NETWORK_REQUIRED",
                message=f"纵断面 {profile.id} 指定了管段，但没有指定 network_system_id",
                semantic_ids=[profile.id, *profile.segment_ids],
            ))
            continue
        if profile.network_system_id is not None:
            if profile.network_system_id not in network_ids:
                issues.append(ValidationIssue(
                    severity="error", rule_id="PROFILE_UNKNOWN_NETWORK",
                    message=(f"纵断面 {profile.id} 引用了不存在的网络 "
                             f"{profile.network_system_id}"),
                    semantic_ids=[profile.id, profile.network_system_id],
                ))
                continue
            missing_segments = [
                segment_id for segment_id in profile.segment_ids
                if segment_id not in network_segment_ids[profile.network_system_id]
            ]
            if missing_segments:
                issues.append(ValidationIssue(
                    severity="error", rule_id="PROFILE_UNKNOWN_SEGMENT",
                    message=(f"纵断面 {profile.id} 引用了网络 "
                             f"{profile.network_system_id} 中不存在的管段: "
                             + ", ".join(missing_segments)),
                    semantic_ids=[profile.id, *missing_segments],
                ))
    for schedule in model.schedules:
        if schedule.view_id is not None and schedule.view_id not in view_ids:
            issues.append(ValidationIssue(
                severity="error", rule_id="SCHEDULE_UNKNOWN_VIEW",
                message=f"表格 {schedule.id} 引用了不存在的视图 {schedule.view_id}",
                semantic_ids=[schedule.id, schedule.view_id],
            ))
    for view in model.views:
        if view.clipping_polygon:
            issues.append(ValidationIssue(
                severity="error", rule_id="VIEW_CLIPPING_UNSUPPORTED",
                message=(f"视图 {view.id} 配置了 clipping_polygon，"
                         "但当前编译器尚不支持实际裁剪，已停止以避免静默忽略"),
                semantic_ids=[view.id],
            ))
    for facility in model.facilities:
        issues.extend(validate_facility(facility))
    for system in model.network_systems:
        issues.extend(validate_network(system))
    for profile in model.profiles:
        issues.extend(validate_profile(profile))
    for annotation in model.annotations:
        if annotation.view_id not in view_ids:
            issues.append(ValidationIssue(
                severity="error", rule_id="ANNOTATION_UNKNOWN_VIEW",
                message=f"标注 {annotation.id} 引用了不存在的视图 {annotation.view_id}",
                semantic_ids=[annotation.id, annotation.view_id],
            ))
        issues.extend(validate_annotation(annotation))
    for schedule in model.schedules:
        issues.extend(validate_schedule(model, schedule))
    for rule in rules:
        issues.extend(apply_rule(rule, model))
    checked = {
        "facilities": len(model.facilities),
        "network_systems": len(model.network_systems),
        "network_segments": sum(len(system.segments) for system in model.network_systems),
        "profiles": len(model.profiles),
        "annotations": len(model.annotations),
        "schedules": len(model.schedules),
        "views": len(model.views),
        "sheets": len(model.sheets),
        "rules": len(rules),
    }
    return ValidationReport(ok=not any(issue.severity == "error" for issue in issues),
                            issues=issues, checked_counts=checked)
