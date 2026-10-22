"""Adapters and native HATCH writers for exact composite closed boundaries."""

import math

from ezdxf.tools import pattern as hatch_patterns

from cad_mcp.core.geometry import validate_hatch_parameters
from cad_mcp.core.hatch_boundaries import (
    BoundarySource, Curve, HatchRegion, TOL, arc_curves, bulge_vertices,
    loop_area, polyline_curves, select_region, validate_source,
)


def refresh_valid_hatch(hatch):
    """Rebuild COM member maps after AppendOuterLoop makes a hatch valid.

    pywin32 wraps AddHatch's return before that mandatory first operation;
    metadata obtained for the provisional object must not govern later writes.
    Build an eager map from the now-valid object's actual runtime type info,
    without generating makepy files or assuming version-specific DISPIDs.
    """
    import pythoncom
    from win32com.client import build, dynamic
    from cad_mcp.drawing.autocad import _ComRetryProxy

    native = object.__getattribute__(hatch, "_obj") if isinstance(hatch, _ComRetryProxy) else hatch
    if not hasattr(native, "_oleobj_"):
        return hatch  # Plain test doubles have no early COM metadata to refresh.
    info = native._oleobj_.GetTypeInfo()
    name = info.GetDocumentation(-1)[0]
    if not name.startswith("IAcadHatch"):
        raise RuntimeError(f"新建填充未返回可验证的 HATCH 接口: {name}")
    attributes = info.GetTypeAttr()
    if attributes[5] == pythoncom.TKIND_INTERFACE and attributes[11] & pythoncom.TYPEFLAG_FDUAL:
        info = info.GetRefTypeInfo(info.GetRefTypeOfImplType(-1))
        attributes = info.GetTypeAttr()
    members = build.DispatchItem(info, attributes, None, 0)
    return _ComRetryProxy(dynamic.CDispatch(native._oleobj_, members, "IAcadHatch"))


def _normal(normal):
    if len(normal) != 3 or not all(math.isclose(float(v), expected, rel_tol=0, abs_tol=1e-10) for v, expected in zip(normal, (0, 0, 1))):
        raise ValueError("组合填充仅支持法向为 WCS +Z 的平面边界，不投影或近似倾斜图元")


def acad_source(entity, handle, *, polyline_ocs=False):
    kind = str(entity.EntityName)
    if kind in {"AcDbPolyline", "AcDb2dPolyline"}:
        if not polyline_ocs:
            _normal(entity.Normal)
        values = [float(v) for v in entity.Coordinates]
        step = 2 if kind == "AcDbPolyline" else 3
        vertices = [(values[i], values[i + 1], float(entity.GetBulge(i // step))) for i in range(0, len(values), step)]
        closed = bool(entity.Closed)
        return BoundarySource(handle, polyline_curves(vertices, closed), closed, float(entity.Elevation))
    if kind == "AcDbLine":
        a, b = tuple(entity.StartPoint), tuple(entity.EndPoint)
        if abs(float(a[2]) - float(b[2])) > TOL:
            raise ValueError("组合填充不支持跨标高的直线")
        return BoundarySource(handle, (Curve(tuple(map(float, a[:2])), tuple(map(float, b[:2]))),), False, float(a[2]))
    if kind in {"AcDbCircle", "AcDbArc"}:
        _normal(entity.Normal)
        center = tuple(float(v) for v in entity.Center)
        start = 0.0 if kind == "AcDbCircle" else float(entity.StartAngle)
        sweep = math.tau if kind == "AcDbCircle" else (float(entity.EndAngle) - start) % math.tau
        return BoundarySource(handle, arc_curves(center[:2], float(entity.Radius), start, sweep), kind == "AcDbCircle", center[2])
    raise ValueError("组合填充只接受 LINE、ARC、CIRCLE 和二维直线/圆弧多段线，不近似其他曲线")


def dxf_source(entity, handle):
    kind = entity.dxftype()
    if kind in {"LWPOLYLINE", "POLYLINE"}:
        _normal(entity.dxf.extrusion)
        if kind == "LWPOLYLINE":
            vertices = list(entity.get_points("xyb"))
            elevation, closed = float(entity.dxf.elevation), bool(entity.closed)
        else:
            if not entity.is_2d_polyline:
                raise ValueError("组合填充不支持三维 POLYLINE")
            vertices = [(float(v.dxf.location.x), float(v.dxf.location.y), float(v.dxf.bulge)) for v in entity.vertices]
            elevation, closed = float(entity.dxf.elevation.z), bool(entity.is_closed)
        return BoundarySource(handle, polyline_curves(vertices, closed), closed, elevation)
    if kind == "LINE":
        a, b = entity.dxf.start, entity.dxf.end
        if abs(float(a.z) - float(b.z)) > TOL:
            raise ValueError("组合填充不支持跨标高的直线")
        return BoundarySource(handle, (Curve((float(a.x), float(a.y)), (float(b.x), float(b.y))),), False, float(a.z))
    if kind in {"CIRCLE", "ARC"}:
        _normal(entity.dxf.extrusion)
        center = entity.dxf.center
        start = 0.0 if kind == "CIRCLE" else math.radians(float(entity.dxf.start_angle))
        sweep = math.tau if kind == "CIRCLE" else math.radians((float(entity.dxf.end_angle) - float(entity.dxf.start_angle)) % 360)
        return BoundarySource(handle, arc_curves((float(center.x), float(center.y)), float(entity.dxf.radius), start, sweep), kind == "CIRCLE", float(center.z))
    raise ValueError("组合填充只接受 LINE、ARC、CIRCLE 和二维直线/圆弧多段线，不近似其他曲线")


def validate_handles(handles, seed_point, associative):
    if not handles or len(handles) > 64 or len(set(handles)) != len(handles):
        raise ValueError("必须提供 1 到 64 个不重复的边界句柄")
    if any(not isinstance(h, str) or not h.isascii() or not h.isdecimal() or len(h) > 40 for h in handles):
        raise ValueError("边界句柄必须为十进制字符串")
    if seed_point is None or len(seed_point) != 2 or not all(math.isfinite(float(v)) for v in seed_point):
        raise ValueError("组合边界必须提供有限的二维区域定位点 seed_point")
    if associative:
        raise ValueError("组合边界含裁剪和临时边界，当前必须显式使用 associative=false；不能冒充关联填充")


def _preview(plan, mode, pattern, scale, angle, layer):
    low, high = plan.bounds
    return {
        "mode": mode, "dry_run": True, "would_write": False,
        "boundary_kind": "composite_closed_region", "boundary_handles": list(plan.handles),
        "seed_point": list(plan.seed), "boundary_area": plan.area,
        "outer_area": plan.outer_area, "excluded_area": plan.excluded_area,
        "hole_count": len(plan.holes), "loop_count": len(plan.loops),
        "boundary_loops": [bulge_vertices(loop) for loop in plan.loops],
        "bounding_box": {"min": [*low, plan.elevation], "max": [*high, plan.elevation]},
        "pattern": pattern, "scale": scale, "angle": angle, "layer": layer,
        "associative": False, "keep_boundary": True,
        "pattern_availability": "available" if mode == "dxf" or pattern == "SOLID" else "unverified_until_creation",
    }


def _matches(readback, plan, pattern, scale, angle, layer):
    low, high = plan.bounds
    return (
        readback["pattern_name"].upper() == pattern
        and readback["associative"] is False
        and readback["layer"] == layer
        and readback["loop_count"] == len(plan.loops)
        and readback["hatch_style"] == 0
        and len(readback["bounding_box"]["min"]) == 3
        and len(readback["bounding_box"]["max"]) == 3
        and math.isclose(readback["area"], plan.area, rel_tol=1e-9, abs_tol=1e-4)
        and all(math.isclose(v, expected, rel_tol=1e-11, abs_tol=1e-6) for v, expected in zip(readback["bounding_box"]["min"], (*low, plan.elevation)))
        and all(math.isclose(v, expected, rel_tol=1e-11, abs_tol=1e-6) for v, expected in zip(readback["bounding_box"]["max"], (*high, plan.elevation)))
        and (pattern == "SOLID" or (
            math.isclose(readback["pattern_scale"], scale, rel_tol=1e-9, abs_tol=1e-12)
            and math.isclose((readback["pattern_angle"] - angle + 180) % 360 - 180, 0, abs_tol=1e-7)
        ))
    )


def _result(plan, mode, hatch_handle, readback):
    return {
        "mode": mode, "dry_run": False, "would_write": True, "verified": True,
        "hatch_handle": hatch_handle, "boundary_kind": "composite_closed_region",
        "boundary_handles": list(plan.handles), "seed_point": list(plan.seed),
        "boundary_area": plan.area, "hole_count": len(plan.holes),
        "source_geometry_unchanged": True, "temporary_boundaries_removed": True,
        "readback": readback,
    }


def fill_dxf_region(backend, *, boundary_handles, seed_point, pattern, scale, angle, layer, associative, dry_run):
    backend._check_doc()
    validate_handles(boundary_handles, seed_point, associative)
    pattern, scale, angle = validate_hatch_parameters(pattern, scale, angle)
    if pattern != "SOLID" and pattern not in hatch_patterns.ISO_PATTERN:
        raise ValueError(f"未知或不可用的 DXF 填充图案: {pattern}")
    entities, sources = [], []
    for h in boundary_handles:
        entity = backend._get_entity(h)
        if entity is None:
            raise ValueError(f"未找到边界句柄: {h}")
        if entity.dxf.owner != backend.msp.layout_key:
            raise ValueError("所有边界必须位于当前模型空间")
        entities.append(entity)
        sources.append(dxf_source(entity, h))
    plan = select_region(sources, seed_point)
    if dry_run:
        return _preview(plan, "dxf", pattern, scale, angle, layer)
    hatch, created_layer = None, False
    try:
        if layer not in backend.doc.layers:
            backend.doc.layers.new(layer)
            created_layer = True
        hatch = backend.msp.add_hatch(dxfattribs={"layer": layer})
        if pattern == "SOLID":
            hatch.set_solid_fill(style=0)
        else:
            hatch.set_pattern_fill(pattern, scale=scale, angle=angle, style=0)
        hatch.dxf.elevation = (0, 0, plan.elevation)
        for i, loop in enumerate(plan.loops):
            hatch.paths.add_polyline_path(bulge_vertices(loop), is_closed=True, flags=1 if i == 0 else 0)
        actual_loops = []
        if len(hatch.paths) != len(plan.loops):
            raise RuntimeError("填充写入后边界循环数不一致")
        for path, expected in zip(hatch.paths, plan.loops):
            vertices = list(path.vertices)
            expected_vertices = bulge_vertices(expected)
            if not path.is_closed or len(vertices) != len(expected_vertices) or any(not math.isclose(float(v), float(e), rel_tol=1e-12, abs_tol=1e-7) for vertex, ev in zip(vertices, expected_vertices) for v, e in zip(vertex, ev)):
                raise RuntimeError("填充写入后真实直线/圆弧边界不一致")
            actual_loops.append(polyline_curves(vertices, True))
        actual = HatchRegion(actual_loops[0], tuple(actual_loops[1:]), plan.handles, plan.seed, float(hatch.dxf.elevation.z))
        low, high = actual.bounds
        readback = {
            "entity_type": hatch.dxftype(), "pattern_name": str(hatch.dxf.pattern_name),
            "solid_fill": bool(hatch.dxf.solid_fill), "associative": bool(hatch.dxf.associative),
            "layer": str(hatch.dxf.layer), "loop_count": len(hatch.paths), "area": actual.area,
            "hatch_style": int(hatch.dxf.hatch_style),
            "bounding_box": {"min": [*low, actual.elevation], "max": [*high, actual.elevation]},
            "pattern_scale": float(hatch.dxf.pattern_scale), "pattern_angle": float(hatch.dxf.pattern_angle),
        }
        if not _matches(readback, plan, pattern, scale, angle, layer) or readback["entity_type"] != "HATCH" or readback["solid_fill"] != (pattern == "SOLID"):
            raise RuntimeError(f"组合填充写入后读回不一致: {readback}")
        if [dxf_source(e, h) for e, h in zip(entities, boundary_handles)] != sources:
            raise RuntimeError("组合填充过程中原边界几何发生变化")
        return _result(plan, "dxf", backend._handle_str(hatch.dxf.handle), readback)
    except Exception:
        if hatch is not None and hatch.is_alive:
            backend.msp.delete_entity(hatch)
        if created_layer:
            backend.doc.layers.remove(layer)
        raise


def fill_acad_region(backend, *, boundary_handles, seed_point, pattern, scale, angle, layer, associative, dry_run):
    import pythoncom
    import win32com.client
    from cad_mcp.dwt.autocad import _dispatch_array
    from cad_mcp.drawing.com_read import bounding_box

    backend._check_doc()
    validate_handles(boundary_handles, seed_point, associative)
    pattern, scale, angle = validate_hatch_parameters(pattern, scale, angle)
    entities, sources = [], []
    for h in boundary_handles:
        entity = backend._find(h)
        if entity is None:
            raise ValueError(f"未找到边界句柄: {h}")
        entities.append(entity)
        sources.append(acad_source(entity, h))
    plan = select_region(sources, seed_point)
    if dry_run:
        return _preview(plan, "acad", pattern, scale, angle, layer)
    helpers, helper_handles, hatch, hatch_handle = [], [], None, None
    try:
        backend._apply_layer(layer)
        for loop in plan.loops:
            vertices = bulge_vertices(loop)
            coordinates = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [v for x, y, _ in vertices for v in (x, y)])
            helper = backend.msp.AddLightWeightPolyline(coordinates)
            helpers.append(helper)
            helper_handles.append(backend._handle_str(helper.Handle))
            helper.Closed = True
            helper.Normal = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [0.0, 0.0, 1.0])
            helper.Elevation = plan.elevation
            for i, (_, _, bulge) in enumerate(vertices):
                if bulge:
                    helper.SetBulge(i, bulge)
        outer_array = _dispatch_array([helpers[0]])
        hatch = backend.msp.AddHatch(1, pattern, False, 0)
        hatch.AppendOuterLoop(outer_array)
        hatch = refresh_valid_hatch(hatch)
        hatch_handle = backend._handle_str(hatch.Handle)
        for helper in helpers[1:]:
            hatch.AppendInnerLoop(_dispatch_array([helper]))
        hatch.HatchStyle = 0
        if pattern != "SOLID":
            hatch.PatternScale = scale
            hatch.PatternAngle = math.radians(angle)
        hatch.Elevation = plan.elevation
        hatch.Evaluate()
        backend.doc.Regen(1)
        def read():
            low, high = bounding_box(hatch)
            return {
                "entity_type": str(hatch.EntityName), "pattern_name": str(hatch.PatternName),
                "associative": bool(hatch.AssociativeHatch), "layer": str(hatch.Layer),
                "loop_count": int(hatch.NumberOfLoops), "area": float(hatch.Area),
                "hatch_style": int(hatch.HatchStyle),
                "bounding_box": {"min": list(map(float, low)), "max": list(map(float, high))},
                "pattern_scale": float(hatch.PatternScale) if pattern != "SOLID" else 1.0,
                "pattern_angle": math.degrees(float(hatch.PatternAngle)) % 360 if pattern != "SOLID" else 0.0,
            }
        readback = read()
        if readback["entity_type"] != "AcDbHatch" or not _matches(readback, plan, pattern, scale, angle, layer):
            raise RuntimeError(f"组合填充写入后读回不一致: {readback}")
        for helper in helpers:
            helper.Delete()
        backend.doc.Regen(1)
        readback = read()
        if not _matches(readback, plan, pattern, scale, angle, layer) or any(backend._find(h) is not None for h in helper_handles):
            raise RuntimeError("删除辅助边界后填充验证失败或辅助对象仍残留")
        if [acad_source(e, h) for e, h in zip(entities, boundary_handles)] != sources:
            raise RuntimeError("组合填充过程中原边界几何发生变化")
        return _result(plan, "acad", hatch_handle, readback)
    except Exception as original_error:
        cleanup_errors = []
        if hatch is not None:
            try:
                # ModelSpace returns a fresh, fully valid dispatch rather than
                # reusing a member map captured during AddHatch initialization.
                current = backend._find(hatch_handle) if hatch_handle is not None else refresh_valid_hatch(hatch)
                if current is not None:
                    current.Delete()
            except Exception as exc:
                cleanup_errors.append(str(exc))
        for h in helper_handles:
            try:
                entity = backend._find(h)
                if entity is not None:
                    entity.Delete()
            except Exception as exc:
                cleanup_errors.append(str(exc))
        if cleanup_errors:
            raise RuntimeError(
                f"组合填充失败: {original_error}；局部清理未完成，需由外层撤销组回滚: "
                + "; ".join(cleanup_errors)
            ) from original_error
        raise
