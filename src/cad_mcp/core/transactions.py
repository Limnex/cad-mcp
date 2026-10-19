"""后端无关的 CAD 操作计划执行器。

事务边界由具体后端的 ``apply_operation_plan`` 建立：AutoCAD 使用撤销组，
DXF 使用内存文档快照。执行器本身只负责严格映射和附加语义元数据。
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Sequence

from cad_mcp.core.contracts import CadOperation, DrawingPlan
from cad_mcp.core.registry import metadata_for_operation, reconcile_plan


class UnsupportedPlanOperation(RuntimeError):
    pass


def _handles(result: Any) -> List[str]:
    if not isinstance(result, dict):
        return []
    handles: List[str] = []
    if result.get("handle") is not None:
        handles.append(str(result["handle"]))
    for handle in result.get("handles", []) or []:
        handles.append(str(handle))
    for entity in result.get("entities", []) or []:
        if isinstance(entity, dict) and entity.get("handle") is not None:
            handles.append(str(entity["handle"]))
    return list(dict.fromkeys(handles))


def _flow_arrow_points(params: Dict[str, Any]) -> List[tuple[float, float]]:
    x, y = map(float, params["point"])
    rotation = math.radians(float(params.get("rotation", 0.0)))
    size = float(params["size"])
    tail = (x - math.cos(rotation) * size * 2.5,
            y - math.sin(rotation) * size * 2.5)
    left = (x + math.cos(rotation + math.radians(150)) * size,
            y + math.sin(rotation + math.radians(150)) * size)
    right = (x + math.cos(rotation - math.radians(150)) * size,
             y + math.sin(rotation - math.radians(150)) * size)
    return [tail, (x, y), left, (x, y), right]


def execute_operation(
    backend: Any,
    operation: CadOperation,
    *,
    project_id: str = "",
    revision: str = "",
) -> Dict[str, Any]:
    params = dict(operation.params)
    kind = operation.kind
    if kind == "ensure_layer":
        lineweight = params.get("lineweight")
        result = backend.create_layer(
            params["name"], int(params.get("color", 7)),
            str(params.get("linetype", "CONTINUOUS")),
            25 if lineweight is None else int(lineweight),
            bool(params.get("plot", True)),
        )
        return {"kind": kind, "message": result, "handles": []}
    if kind == "line":
        result = backend.draw_line(params["x1"], params["y1"],
                                   params["x2"], params["y2"], params.get("layer"))
    elif kind == "polyline":
        result = backend.draw_polyline(params["points"], bool(params.get("close", False)),
                                       params.get("layer"))
    elif kind == "circle":
        result = backend.draw_circle(params["cx"], params["cy"], params["radius"],
                                     params.get("layer"))
    elif kind == "text":
        result = backend.draw_text(params["text"], params["x"], params["y"],
                                   params.get("height"), params.get("rotation", 0),
                                   params.get("layer"))
    elif kind == "mtext":
        if not hasattr(backend, "draw_mtext"):
            raise UnsupportedPlanOperation("当前后端不支持原生 MTEXT，不能用普通 TEXT 替代")
        result = backend.draw_mtext(
            params["text"], params["x"], params["y"], params["width"],
            params.get("height"), params.get("rotation", 0), params.get("layer"))
    elif kind == "flow_arrow":
        result = backend.draw_polyline(_flow_arrow_points(params), False, params.get("layer"))
    elif kind == "block":
        previous_layer = getattr(backend, "active_layer", None)
        if params.get("layer"):
            backend.set_active_layer(params["layer"])
        try:
            result = backend.insert_block(
                params["name"], params["x"], params["y"],
                params.get("rotation", 0), params.get("scale", 1.0))
        finally:
            if previous_layer:
                backend.set_active_layer(previous_layer)
    elif kind == "table":
        result = backend.draw_table(
            params["origin_x"], params["origin_y"], params["col_widths"],
            params["row_height"], params["headers"], params["rows"],
            params.get("title", ""), params.get("layer", "ENV-TABLE"))
    elif kind == "template_table":
        if not hasattr(backend, "fill_template_table"):
            raise UnsupportedPlanOperation(
                "当前后端没有模板单元格填写能力，不能用通用表格替代")
        result = backend.fill_template_table(**params)
    else:
        raise UnsupportedPlanOperation(f"不支持的 CAD 操作类型: {kind}")
    handles = _handles(result)
    if handles:
        if not hasattr(backend, "set_entity_metadata"):
            raise UnsupportedPlanOperation("当前后端无法写入 CAD_MCP_ENV XData")
        for entity_index, handle in enumerate(handles):
            handle_metadata = metadata_for_operation(
                operation,
                revision or getattr(backend, "_environment_revision", ""),
                project_id,
                entity_index=entity_index,
                entity_count=len(handles),
            )
            if hasattr(backend, "get_entity_state_hash"):
                handle_metadata["materialized_hash"] = backend.get_entity_state_hash(handle)
            backend.set_entity_metadata(handle, handle_metadata)
    return {"kind": kind, "semantic_id": operation.semantic_id,
            "role": operation.role, "handles": handles, "result": result}


def execute_plan(backend: Any, plan: DrawingPlan) -> Dict[str, Any]:
    backend._environment_revision = plan.revision
    results = [execute_operation(
        backend,
        operation,
        project_id=plan.project_id,
        revision=plan.revision,
    ) for operation in plan.operations]
    handles = [handle for result in results for handle in result["handles"]]
    return {
        "project_id": plan.project_id,
        "revision": plan.revision,
        "operation_count": len(results),
        "entity_count": len(handles),
        "handles": handles,
        "results": results,
    }


def execute_reconciled_plan(
    backend: Any,
    plan: DrawingPlan,
    policy: str = "update_managed",
) -> Dict[str, Any]:
    """在后端事务内部完成差异计算、受管实体删除和变更操作执行。"""

    if not hasattr(backend, "list_managed_entities"):
        raise UnsupportedPlanOperation("当前后端不能读取 CAD_MCP_ENV XData，无法安全同步")
    existing = backend.list_managed_entities()
    difference = reconcile_plan(plan, existing, policy)
    if difference["conflict"] and policy != "replace_scope":
        handles = ", ".join(
            handle
            for item in difference["conflict"][:12]
            for handle in item.get("handles", [])
        )
        raise RuntimeError(
            "检测到 MCP 受管图元已被手工修改，未自动覆盖。"
            f"冲突句柄: {handles}；如确认重建该范围，请显式使用 replace_scope")
    if difference["conflict"]:
        handles = ", ".join(
            handle
            for item in difference["conflict"][:12]
            for handle in item.get("handles", [])
        )
        raise RuntimeError(
            "replace_scope 遇到无法安全归属的旧版或歧义图元，已停止。"
            f"冲突句柄: {handles}")
    replace_handles = [
        handle
        for item in difference["replace"]
        for handle in item.get("handles", [])
    ]
    delete_handles = [item.handle for item in difference["delete"]]
    for handle in dict.fromkeys([*replace_handles, *delete_handles]):
        backend.delete_entity(handle)
    changed_operations = [*difference["create"],
                          *(item["operation"] for item in difference["replace"])]
    changed_plan = DrawingPlan(
        project_id=plan.project_id,
        revision=plan.revision,
        view_ids=plan.view_ids,
        operations=changed_operations,
        warnings=plan.warnings,
        statistics=plan.statistics,
    )
    execution = execute_plan(backend, changed_plan)
    return {
        "project_id": plan.project_id,
        "revision": plan.revision,
        "policy": policy,
        "difference": difference["statistics"],
        "execution": execution,
    }
