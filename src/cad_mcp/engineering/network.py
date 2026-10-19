"""环境工程管线、渠道、风管和输送网络的通用拓扑与制图算法。"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from cad_mcp.core.contracts import (
    CadOperation,
    NetworkNode,
    NetworkSegment,
    NetworkSystem,
    Point2D,
    ValidationIssue,
)


def distance(a: Point2D, b: Point2D) -> float:
    return math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))


def polyline_length(points: Sequence[Point2D]) -> float:
    return sum(distance(a, b) for a, b in zip(points, points[1:]))


def segment_slope(segment: NetworkSegment, nodes: Dict[str, NetworkNode]) -> Optional[float]:
    """按流向返回高差/平面长度；缺少标高时返回 None。"""

    start_z = segment.start_elevation
    end_z = segment.end_elevation
    if start_z is None and segment.start_node in nodes:
        start_z = nodes[segment.start_node].elevation
    if end_z is None and segment.end_node in nodes:
        end_z = nodes[segment.end_node].elevation
    length = polyline_length(segment.path)
    if start_z is None or end_z is None or length <= 0:
        return None
    if segment.flow_direction == "end_to_start":
        return (float(end_z) - float(start_z)) / length
    return (float(start_z) - float(end_z)) / length


def _near(a: Point2D, b: Point2D, tolerance: float) -> bool:
    return distance(a, b) <= tolerance


def _orientation(a: Point2D, b: Point2D, c: Point2D) -> float:
    return ((float(b[0]) - float(a[0])) * (float(c[1]) - float(a[1]))
            - (float(b[1]) - float(a[1])) * (float(c[0]) - float(a[0])))


def _on_segment(a: Point2D, b: Point2D, p: Point2D, tolerance: float) -> bool:
    if abs(_orientation(a, b, p)) > tolerance:
        return False
    return (
        min(a[0], b[0]) - tolerance <= p[0] <= max(a[0], b[0]) + tolerance
        and min(a[1], b[1]) - tolerance <= p[1] <= max(a[1], b[1]) + tolerance
    )


def line_intersection(
    a1: Point2D,
    a2: Point2D,
    b1: Point2D,
    b2: Point2D,
    tolerance: float = 1e-6,
) -> Optional[Point2D]:
    """返回两条有限线段的单一交点；共线重叠返回 None，由重叠检查另行处理。"""

    x1, y1 = map(float, a1)
    x2, y2 = map(float, a2)
    x3, y3 = map(float, b1)
    x4, y4 = map(float, b2)
    denominator = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denominator) <= tolerance:
        return None
    px = ((x1 * y2 - y1 * x2) * (x3 - x4)
          - (x1 - x2) * (x3 * y4 - y3 * x4)) / denominator
    py = ((x1 * y2 - y1 * x2) * (y3 - y4)
          - (y1 - y2) * (x3 * y4 - y3 * x4)) / denominator
    p = (px, py)
    if _on_segment(a1, a2, p, tolerance) and _on_segment(b1, b2, p, tolerance):
        return p
    return None


def _segment_pairs(segment: NetworkSegment) -> Iterable[Tuple[Point2D, Point2D]]:
    return zip(segment.path, segment.path[1:])


def build_adjacency(system: NetworkSystem) -> Dict[str, Set[str]]:
    adjacency: Dict[str, Set[str]] = defaultdict(set)
    for node in system.nodes:
        adjacency[node.id]
    for segment in system.segments:
        adjacency[segment.start_node].add(segment.end_node)
        adjacency[segment.end_node].add(segment.start_node)
    return dict(adjacency)


def connected_components(system: NetworkSystem) -> List[Set[str]]:
    adjacency = build_adjacency(system)
    remaining = set(adjacency)
    result: List[Set[str]] = []
    while remaining:
        seed = next(iter(remaining))
        component: Set[str] = set()
        queue = deque([seed])
        while queue:
            current = queue.popleft()
            if current in component:
                continue
            component.add(current)
            queue.extend(adjacency.get(current, ()))
        remaining -= component
        result.append(component)
    return result


def validate_network(
    system: NetworkSystem,
    endpoint_tolerance: float = 1.0,
    slope_tolerance: float = 1e-6,
) -> List[ValidationIssue]:
    """进行确定性的拓扑和数据一致性检查，不替代水力或工艺设计。"""

    issues: List[ValidationIssue] = []
    nodes: Dict[str, NetworkNode] = {}
    for node in system.nodes:
        if node.id in nodes:
            issues.append(ValidationIssue(
                severity="error",
                rule_id="NETWORK_DUPLICATE_NODE_ID",
                message=f"网络 {system.id} 存在重复节点编号 {node.id}",
                semantic_ids=[system.id, node.id],
            ))
        nodes[node.id] = node

    seen_segments: Set[str] = set()
    for segment in system.segments:
        if segment.id in seen_segments:
            issues.append(ValidationIssue(
                severity="error",
                rule_id="NETWORK_DUPLICATE_SEGMENT_ID",
                message=f"网络 {system.id} 存在重复管段编号 {segment.id}",
                semantic_ids=[system.id, segment.id],
            ))
        seen_segments.add(segment.id)
        missing = [node_id for node_id in (segment.start_node, segment.end_node)
                   if node_id not in nodes]
        if missing:
            issues.append(ValidationIssue(
                severity="error",
                rule_id="NETWORK_MISSING_ENDPOINT_NODE",
                message=f"管段 {segment.id} 引用了不存在的节点: {', '.join(missing)}",
                semantic_ids=[segment.id, *missing],
            ))
            continue
        if not _near(segment.path[0], nodes[segment.start_node].point, endpoint_tolerance):
            issues.append(ValidationIssue(
                severity="error",
                rule_id="NETWORK_START_POINT_MISMATCH",
                message=f"管段 {segment.id} 的路径起点未落在节点 {segment.start_node} 上",
                semantic_ids=[segment.id, segment.start_node],
                location=segment.path[0],
            ))
        if not _near(segment.path[-1], nodes[segment.end_node].point, endpoint_tolerance):
            issues.append(ValidationIssue(
                severity="error",
                rule_id="NETWORK_END_POINT_MISMATCH",
                message=f"管段 {segment.id} 的路径终点未落在节点 {segment.end_node} 上",
                semantic_ids=[segment.id, segment.end_node],
                location=segment.path[-1],
            ))
        length = polyline_length(segment.path)
        if length <= endpoint_tolerance:
            issues.append(ValidationIssue(
                severity="error",
                rule_id="NETWORK_ZERO_LENGTH",
                message=f"管段 {segment.id} 的有效长度过小",
                semantic_ids=[segment.id],
            ))
        calculated = segment_slope(segment, nodes)
        if (calculated is not None and segment.design_slope is not None
                and abs(calculated - float(segment.design_slope)) > slope_tolerance):
            issues.append(ValidationIssue(
                severity="error",
                rule_id="NETWORK_SLOPE_INCONSISTENT",
                message=(f"管段 {segment.id} 的标高计算坡度 {calculated:.6f} "
                         f"与设计坡度 {segment.design_slope:.6f} 不一致"),
                semantic_ids=[segment.id],
                suggested_action="核对起终点标高、平面长度和设计坡度",
            ))
        if calculated is not None and calculated < -slope_tolerance:
            issues.append(ValidationIssue(
                severity="warning",
                rule_id="NETWORK_REVERSE_SLOPE",
                message=f"管段 {segment.id} 沿标注流向出现逆坡",
                semantic_ids=[segment.id],
            ))

    adjacency = build_adjacency(system)
    for node_id, neighbors in adjacency.items():
        if not neighbors:
            issues.append(ValidationIssue(
                severity="warning",
                rule_id="NETWORK_ISOLATED_NODE",
                message=f"节点 {node_id} 未连接任何管段",
                semantic_ids=[system.id, node_id],
            ))
    nonempty_components = [c for c in connected_components(system)
                           if any(adjacency.get(node) for node in c)]
    if len(nonempty_components) > 1:
        issues.append(ValidationIssue(
            severity="warning",
            rule_id="NETWORK_DISCONNECTED_COMPONENTS",
            message=f"网络 {system.id} 包含 {len(nonempty_components)} 个互不连通的子网",
            semantic_ids=[system.id],
            suggested_action="核对是否存在漏画管段，或将独立系统拆分为不同 network_system",
        ))

    # 平面交点没有共享显式节点时只提示，不擅自建立连接。
    for index, left in enumerate(system.segments):
        for right in system.segments[index + 1:]:
            shared = {left.start_node, left.end_node} & {right.start_node, right.end_node}
            for la, lb in _segment_pairs(left):
                for ra, rb in _segment_pairs(right):
                    point = line_intersection(la, lb, ra, rb, endpoint_tolerance * 1e-3)
                    if point is None:
                        continue
                    if shared and any(_near(point, nodes[node_id].point, endpoint_tolerance)
                                      for node_id in shared if node_id in nodes):
                        continue
                    issues.append(ValidationIssue(
                        severity="warning",
                        rule_id="NETWORK_UNMODELED_CROSSING",
                        message=(f"管段 {left.id} 与 {right.id} 在平面上相交，"
                                 "但没有共享连接节点；这可能是交叉或漏建节点"),
                        semantic_ids=[left.id, right.id],
                        location=point,
                    ))
                    break
                else:
                    continue
                break
    return issues


def point_and_angle_at_fraction(points: Sequence[Point2D], fraction: float = 0.5) -> Tuple[Point2D, float]:
    """返回多段线指定长度比例处的点和切线角度。"""

    fraction = max(0.0, min(1.0, float(fraction)))
    total = polyline_length(points)
    if total <= 0:
        return points[0], 0.0
    target = total * fraction
    traversed = 0.0
    for start, end in zip(points, points[1:]):
        part = distance(start, end)
        if traversed + part >= target and part > 0:
            ratio = (target - traversed) / part
            point = (
                start[0] + (end[0] - start[0]) * ratio,
                start[1] + (end[1] - start[1]) * ratio,
            )
            angle = math.degrees(math.atan2(end[1] - start[1], end[0] - start[0]))
            return point, angle
        traversed += part
    start, end = points[-2], points[-1]
    return end, math.degrees(math.atan2(end[1] - start[1], end[0] - start[0]))


def compile_network(system: NetworkSystem, view_id: str, scale_denominator: float) -> List[CadOperation]:
    """把网络系统编译成后端无关的图层、线、节点、流向和标签操作。"""

    operations: List[CadOperation] = [CadOperation(
        kind="ensure_layer",
        semantic_id=system.id,
        view_id=view_id,
        role="layer",
        params={
            "name": system.layer,
            "color": system.color,
            "linetype": system.linetype,
            "lineweight": system.lineweight,
        },
    )]
    nodes = {node.id: node for node in system.nodes}
    paper_height = float(system.attributes.get("label_paper_height", 3.5))
    for segment in system.segments:
        layer = segment.layer or system.layer
        metadata = {
            "object_type": "network_segment",
            "network_system_id": system.id,
            "medium": system.medium,
        }
        operations.append(CadOperation(
            kind="polyline",
            semantic_id=segment.id,
            view_id=view_id,
            role="network_path",
            params={"points": segment.path, "close": False, "layer": layer},
            metadata=metadata,
        ))
        midpoint, angle = point_and_angle_at_fraction(segment.path, 0.5)
        if segment.flow_direction != "unspecified":
            arrow_angle = angle if segment.flow_direction == "start_to_end" else angle + 180.0
            operations.append(CadOperation(
                kind="flow_arrow",
                semantic_id=segment.id,
                view_id=view_id,
                role="flow_arrow",
                params={
                    "point": midpoint,
                    "rotation": arrow_angle,
                    "size": 2.5 * scale_denominator,
                    "layer": layer,
                },
                metadata=metadata,
            ))
        section_text = ""
        if segment.section:
            if "nominal_size" in segment.section:
                section_text = f" DN{segment.section['nominal_size']}"
            elif "width" in segment.section and "height" in segment.section:
                section_text = f" {segment.section['width']}×{segment.section['height']}"
        label = f"{segment.id} {system.medium}{section_text}"
        if segment.material:
            label += f" {segment.material}"
        operations.append(CadOperation(
            kind="text",
            semantic_id=segment.id,
            view_id=view_id,
            role="network_label",
            params={
                "text": label,
                "x": midpoint[0],
                "y": midpoint[1] + paper_height * scale_denominator,
                "height": paper_height * scale_denominator,
                "rotation": angle,
                "layer": "ENV-ANNO",
            },
            metadata=metadata,
        ))
    if bool(system.attributes.get("draw_nodes", False)):
        radius = float(system.attributes.get("node_radius_paper", 1.5)) * scale_denominator
        for node in nodes.values():
            operations.append(CadOperation(
                kind="circle",
                semantic_id=node.id,
                view_id=view_id,
                role="network_node",
                params={
                    "cx": node.point[0], "cy": node.point[1],
                    "radius": radius, "layer": system.layer,
                },
                metadata={"object_type": "network_node", "network_system_id": system.id},
            ))
    return operations
