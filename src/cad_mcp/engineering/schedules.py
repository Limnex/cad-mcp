"""从环境工程语义模型生成表格、图例和图纸目录的数据管线。"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence, Tuple

from cad_mcp.core.contracts import CadOperation, EnvironmentModel, ScheduleDefinition, ValidationIssue


class ScheduleFormatError(ValueError):
    pass


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


def _iter_source(model: EnvironmentModel, source_type: str) -> Iterable[Any]:
    if source_type == "facility":
        yield from model.facilities
    elif source_type == "network_system":
        yield from model.network_systems
    elif source_type == "network_segment":
        for system in model.network_systems:
            for segment in system.segments:
                payload = segment.model_dump()
                payload["system_id"] = system.id
                payload["system_name"] = system.name
                payload["medium"] = system.medium
                yield payload
    elif source_type == "annotation":
        yield from model.annotations
    elif source_type == "drawing":
        yield from model.sheets


def _matches_filters(item: Any, filters: Dict[str, Any]) -> bool:
    return all(_get_path(item, key) == expected for key, expected in filters.items())


def _format_value(value: Any, format_spec: str | None) -> str:
    if value is None:
        return ""
    if not format_spec:
        return str(value)
    if format_spec == "length_2":
        return f"{float(value):.2f}"
    if format_spec == "elevation_3":
        return f"{float(value):.3f}"
    if format_spec == "percent_3":
        return f"{float(value) * 100:.3f}%"
    if format_spec.startswith("prefix:"):
        return format_spec.split(":", 1)[1] + str(value)
    if format_spec.startswith("suffix:"):
        return str(value) + format_spec.split(":", 1)[1]
    return format(value, format_spec)


def schedule_rows(model: EnvironmentModel, schedule: ScheduleDefinition) -> List[List[str]]:
    items = [item for item in _iter_source(model, schedule.source_type)
             if _matches_filters(item, schedule.filters)]
    for key in reversed(schedule.sort_by):
        items.sort(key=lambda item: (str(_get_path(item, key) or "")))
    rows: List[List[str]] = []
    for row_index, item in enumerate(items):
        row: List[str] = []
        for column in schedule.columns:
            value = _get_path(item, column.key)
            try:
                row.append(_format_value(value, column.format))
            except (TypeError, ValueError, OverflowError) as exc:
                item_id = _get_path(item, "id") or f"第 {row_index + 1} 行"
                raise ScheduleFormatError(
                    f"表格 {schedule.id} 的对象 {item_id} 字段 {column.key} "
                    f"无法按 {column.format!r} 格式化: {exc}") from exc
        rows.append(row)
    return rows


def compile_schedule(
    model: EnvironmentModel,
    schedule: ScheduleDefinition,
    view_id: str | None = None,
) -> List[CadOperation]:
    headers = [column.title for column in schedule.columns]
    widths = [column.width for column in schedule.columns]
    rows = schedule_rows(model, schedule)
    capacity = schedule.max_rows_per_table or max(len(rows), 1)
    pages = [rows[index:index + capacity] for index in range(0, len(rows), capacity)] or [[]]
    operations: List[CadOperation] = []
    table_height = schedule.row_height * (capacity + 1)
    for page_index, page in enumerate(pages):
        origin = (schedule.origin[0], schedule.origin[1] + page_index * (table_height + schedule.row_height * 2))
        title = schedule.title
        if len(pages) > 1:
            title = f"{title}（续{page_index + 1}）"
        params = {
            "origin_x": origin[0],
            "origin_y": origin[1],
            "col_widths": widths,
            "row_height": schedule.row_height,
            "headers": headers,
            "column_keys": [column.key for column in schedule.columns],
            "rows": page,
            "title": title,
            "layer": schedule.layer,
            "page_index": page_index,
            "page_count": len(pages),
        }
        kind = "template_table" if schedule.template_id else "table"
        if schedule.template_id:
            params.update({
                "template_id": schedule.template_id,
                "sheet_id": schedule.template_sheet_id,
                "table_id": schedule.template_table_id,
                "layout_name": schedule.layout_name,
            })
        operations.append(CadOperation(
            kind=kind,
            semantic_id=schedule.id,
            view_id=view_id,
            role="schedule",
            params=params,
            metadata={"object_type": "schedule", "source_type": schedule.source_type},
        ))
    return operations


def validate_schedule(model: EnvironmentModel, schedule: ScheduleDefinition) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    try:
        rows = schedule_rows(model, schedule)
    except ScheduleFormatError as exc:
        return [ValidationIssue(
            severity="error",
            rule_id="SCHEDULE_FORMAT_INVALID",
            message=str(exc),
            semantic_ids=[schedule.id],
        )]
    if not rows:
        issues.append(ValidationIssue(
            severity="warning",
            rule_id="SCHEDULE_EMPTY",
            message=f"表格 {schedule.id} 没有匹配到任何数据",
            semantic_ids=[schedule.id],
        ))
        return issues
    for row_index, row in enumerate(rows):
        for column_index, cell in enumerate(row):
            if cell != "":
                continue
            column = schedule.columns[column_index]
            issues.append(ValidationIssue(
                severity="info",
                rule_id="SCHEDULE_EMPTY_CELL",
                message=f"表格 {schedule.id} 第 {row_index + 1} 行字段 {column.key} 为空",
                semantic_ids=[schedule.id],
            ))
    return issues
