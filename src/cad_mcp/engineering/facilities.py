"""参数化环境工程构筑物、厂房部件和设备的通用二维视图生成。"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from cad_mcp.core.contracts import CadOperation, Facility, FacilityComponent, Point2D, ValidationIssue


def transform_point(point: Point2D, origin: Point2D, rotation: float) -> Point2D:
    angle = math.radians(rotation)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    x, y = float(point[0]), float(point[1])
    return (
        float(origin[0]) + x * cos_a - y * sin_a,
        float(origin[1]) + x * sin_a + y * cos_a,
    )


def rectangle_points(length: float, width: float) -> List[Point2D]:
    return [(0.0, 0.0), (float(length), 0.0),
            (float(length), float(width)), (0.0, float(width))]


def facility_plan_bounds(facility: Facility) -> Tuple[float, float, float, float]:
    outline = facility.outline
    if outline.shape == "circle":
        radius = float(outline.radius)
        ox, oy = facility.origin
        return ox - radius, oy - radius, ox + radius, oy + radius
    local = rectangle_points(outline.length, outline.width) if outline.shape == "rectangle" else outline.points
    points = [transform_point(point, facility.origin, facility.rotation) for point in local]
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _component_points(component: FacilityComponent) -> List[Point2D]:
    geometry = component.geometry
    if "points" in geometry:
        return [(float(p[0]), float(p[1])) for p in geometry["points"]]
    if "p1" in geometry and "p2" in geometry:
        return [tuple(geometry["p1"]), tuple(geometry["p2"])]
    if all(key in geometry for key in ("x", "y", "length", "width")):
        x, y = float(geometry["x"]), float(geometry["y"])
        return [(x + px, y + py) for px, py in rectangle_points(
            float(geometry["length"]), float(geometry["width"]))]
    return []


def validate_facility(facility: Facility, tolerance: float = 1.0) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    if (facility.bottom_elevation is not None and facility.top_elevation is not None
            and facility.top_elevation <= facility.bottom_elevation):
        issues.append(ValidationIssue(
            severity="error",
            rule_id="FACILITY_ELEVATION_ORDER",
            message=f"构筑物 {facility.id} 的顶标高不高于底标高",
            semantic_ids=[facility.id],
        ))
    seen = set()
    for component in facility.components:
        if component.id in seen:
            issues.append(ValidationIssue(
                severity="error",
                rule_id="FACILITY_DUPLICATE_COMPONENT_ID",
                message=f"构筑物 {facility.id} 存在重复部件编号 {component.id}",
                semantic_ids=[facility.id, component.id],
            ))
        seen.add(component.id)
        if (component.bottom_elevation is not None and component.top_elevation is not None
                and component.top_elevation <= component.bottom_elevation):
            issues.append(ValidationIssue(
                severity="error",
                rule_id="FACILITY_COMPONENT_ELEVATION_ORDER",
                message=f"部件 {component.id} 的顶标高不高于底标高",
                semantic_ids=[facility.id, component.id],
            ))
        if component.kind in {"partition_wall", "channel", "weir", "platform"}:
            if len(_component_points(component)) < 2:
                issues.append(ValidationIssue(
                    severity="error",
                    rule_id="FACILITY_COMPONENT_GEOMETRY_MISSING",
                    message=f"部件 {component.id} 缺少可绘制的平面几何",
                    semantic_ids=[facility.id, component.id],
                ))
        if component.kind == "equipment" and not (
                component.geometry.get("block_name") or _component_points(component)):
            issues.append(ValidationIssue(
                severity="error",
                rule_id="FACILITY_EQUIPMENT_REPRESENTATION_MISSING",
                message=f"设备部件 {component.id} 必须提供 block_name 或平面轮廓",
                semantic_ids=[facility.id, component.id],
            ))
    return issues


def _outline_operations(facility: Facility, view_id: str) -> List[CadOperation]:
    metadata = {"object_type": "facility", "facility_type": facility.facility_type}
    outline = facility.outline
    if outline.shape == "circle":
        return [CadOperation(
            kind="circle",
            semantic_id=facility.id,
            view_id=view_id,
            role="facility_outline",
            params={
                "cx": facility.origin[0], "cy": facility.origin[1],
                "radius": outline.radius, "layer": facility.layer,
            },
            metadata=metadata,
        )]
    local_points = (rectangle_points(outline.length, outline.width)
                    if outline.shape == "rectangle" else list(outline.points))
    points = [transform_point(point, facility.origin, facility.rotation)
              for point in local_points]
    operations = [CadOperation(
        kind="polyline",
        semantic_id=facility.id,
        view_id=view_id,
        role="facility_outline",
        params={"points": points, "close": True, "layer": facility.layer},
        metadata=metadata,
    )]
    if facility.wall_thickness and outline.shape == "rectangle":
        thickness = float(facility.wall_thickness)
        if thickness * 2 < min(float(outline.length), float(outline.width)):
            inner = rectangle_points(float(outline.length) - 2 * thickness,
                                     float(outline.width) - 2 * thickness)
            inner = [(x + thickness, y + thickness) for x, y in inner]
            inner = [transform_point(point, facility.origin, facility.rotation)
                     for point in inner]
            operations.append(CadOperation(
                kind="polyline",
                semantic_id=facility.id,
                view_id=view_id,
                role="facility_inner_wall",
                params={"points": inner, "close": True, "layer": facility.layer},
                metadata=metadata,
            ))
    return operations


def _component_operations(
    facility: Facility,
    component: FacilityComponent,
    view_id: str,
    scale_denominator: float,
) -> List[CadOperation]:
    layer = component.layer or facility.layer
    metadata = {
        "object_type": "facility_component",
        "facility_id": facility.id,
        "component_kind": component.kind,
    }
    geometry = component.geometry
    result: List[CadOperation] = []
    if component.kind in {"opening", "manhole"} and "radius" in geometry:
        center = geometry.get("center", (geometry.get("x", 0), geometry.get("y", 0)))
        world = transform_point(tuple(center), facility.origin, facility.rotation)
        result.append(CadOperation(
            kind="circle", semantic_id=component.id, view_id=view_id,
            role=component.kind,
            params={"cx": world[0], "cy": world[1], "radius": float(geometry["radius"]),
                    "layer": layer},
            metadata=metadata,
        ))
        return result
    if component.kind == "equipment" and geometry.get("block_name"):
        insert = transform_point(tuple(geometry.get("insert", (0, 0))),
                                 facility.origin, facility.rotation)
        result.append(CadOperation(
            kind="block", semantic_id=component.id, view_id=view_id,
            role="equipment",
            params={
                "name": geometry["block_name"],
                "x": insert[0], "y": insert[1],
                "rotation": facility.rotation + float(geometry.get("rotation", 0)),
                "scale": float(geometry.get("scale", 1.0)),
                "layer": layer,
            },
            metadata=metadata,
        ))
    points = _component_points(component)
    if points:
        world_points = [transform_point(point, facility.origin, facility.rotation)
                        for point in points]
        close = len(points) >= 3 and bool(geometry.get("close", True))
        result.append(CadOperation(
            kind="polyline", semantic_id=component.id, view_id=view_id,
            role=component.kind,
            params={"points": world_points, "close": close, "layer": layer},
            metadata=metadata,
        ))
    if geometry.get("label"):
        anchor = geometry.get("label_anchor")
        if anchor is None and points:
            anchor = (
                sum(p[0] for p in points) / len(points),
                sum(p[1] for p in points) / len(points),
            )
        if anchor is not None:
            world = transform_point(tuple(anchor), facility.origin, facility.rotation)
            result.append(CadOperation(
                kind="text", semantic_id=component.id, view_id=view_id,
                role="component_label",
                params={
                    "text": str(geometry["label"]), "x": world[0], "y": world[1],
                    "height": float(geometry.get("paper_height", 3.5)) * scale_denominator,
                    "rotation": facility.rotation, "layer": "ENV-ANNO",
                },
                metadata=metadata,
            ))
    return result


def compile_facility_plan(
    facility: Facility,
    view_id: str,
    scale_denominator: float,
) -> List[CadOperation]:
    operations = [CadOperation(
        kind="ensure_layer",
        semantic_id=facility.id,
        view_id=view_id,
        role="layer",
        params={"name": facility.layer, "color": 7, "linetype": "CONTINUOUS"},
    )]
    operations.extend(_outline_operations(facility, view_id))
    for component in facility.components:
        operations.extend(_component_operations(facility, component, view_id, scale_denominator))
    label_anchor = facility.attributes.get("label_anchor")
    if label_anchor is None:
        xmin, ymin, xmax, ymax = facility_plan_bounds(facility)
        label_anchor = ((xmin + xmax) / 2, (ymin + ymax) / 2)
    operations.append(CadOperation(
        kind="text", semantic_id=facility.id, view_id=view_id,
        role="facility_label",
        params={
            "text": facility.name,
            "x": float(label_anchor[0]), "y": float(label_anchor[1]),
            "height": float(facility.attributes.get("label_paper_height", 5.0)) * scale_denominator,
            "rotation": facility.rotation,
            "layer": "ENV-ANNO",
        },
        metadata={"object_type": "facility", "facility_type": facility.facility_type},
    ))
    return operations


def compile_facility_section(
    facility: Facility,
    view_id: str,
    scale_denominator: float,
    origin: Point2D,
) -> List[CadOperation]:
    """基于 2.5D 标高数据生成通用示意剖面；复杂剖切可由部件提供 section_points。"""

    if facility.bottom_elevation is None or facility.top_elevation is None:
        return []
    if facility.outline.shape == "circle":
        span = float(facility.outline.radius) * 2
    elif facility.outline.shape == "rectangle":
        span = float(facility.outline.length)
    else:
        xs = [point[0] for point in facility.outline.points]
        span = max(xs) - min(xs)
    height = float(facility.top_elevation) - float(facility.bottom_elevation)
    points = [origin, (origin[0] + span, origin[1]),
              (origin[0] + span, origin[1] + height), (origin[0], origin[1] + height)]
    operations = [CadOperation(
        kind="polyline", semantic_id=facility.id, view_id=view_id,
        role="facility_section_outline",
        params={"points": points, "close": True, "layer": facility.layer},
        metadata={"object_type": "facility", "view_type": "section"},
    )]
    for component in facility.components:
        section_points = component.geometry.get("section_points")
        if not section_points:
            continue
        shifted = [(origin[0] + float(p[0]), origin[1] + float(p[1]))
                   for p in section_points]
        operations.append(CadOperation(
            kind="polyline", semantic_id=component.id, view_id=view_id,
            role=f"section_{component.kind}",
            params={"points": shifted,
                    "close": bool(component.geometry.get("section_close", False)),
                    "layer": component.layer or facility.layer},
            metadata={"object_type": "facility_component", "facility_id": facility.id},
        ))
    return operations
