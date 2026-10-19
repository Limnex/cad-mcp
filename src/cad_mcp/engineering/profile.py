"""环境工程线路桩号、高程序列和纵断面图的通用计算。"""

from __future__ import annotations

import bisect
from typing import Dict, Iterable, List, Sequence, Tuple

from cad_mcp.core.contracts import (
    CadOperation,
    NetworkSegment,
    NetworkSystem,
    Point2D,
    ProfileDefinition,
    ProfilePoint,
    ProfileSeries,
    ValidationIssue,
)
from cad_mcp.engineering.network import polyline_length


def cumulative_stations(path: Sequence[Point2D]) -> List[float]:
    stations = [0.0]
    for start, end in zip(path, path[1:]):
        stations.append(stations[-1] + polyline_length([start, end]))
    return stations


def interpolate_series(points: Sequence[ProfilePoint], station: float) -> float:
    """对按桩号递增的高程序列进行分段线性插值。"""

    if not points:
        raise ValueError("高程序列不能为空")
    stations = [float(point[0]) for point in points]
    if station <= stations[0]:
        return float(points[0][1])
    if station >= stations[-1]:
        return float(points[-1][1])
    index = bisect.bisect_right(stations, station) - 1
    s1, z1 = map(float, points[index])
    s2, z2 = map(float, points[index + 1])
    if s2 == s1:
        return z2
    ratio = (float(station) - s1) / (s2 - s1)
    return z1 + (z2 - z1) * ratio


def profile_to_drawing_point(
    origin: Point2D,
    datum_elevation: float,
    station: float,
    elevation: float,
    horizontal_scale_denominator: float,
    vertical_scale_denominator: float,
    paper_to_model: float = 1.0,
) -> Point2D:
    """把桩号/标高转换成纵断面图坐标。

    paper_to_model 用于布局纸空间与模型空间之间的显式换算，不做自动猜测。
    """

    return (
        float(origin[0]) + float(station) / float(horizontal_scale_denominator) * paper_to_model,
        float(origin[1]) + (float(elevation) - float(datum_elevation))
        / float(vertical_scale_denominator) * paper_to_model,
    )


def validate_profile(profile: ProfileDefinition) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    for series in profile.series:
        previous = None
        for station, _ in series.points:
            if previous is not None and station <= previous:
                issues.append(ValidationIssue(
                    severity="error",
                    rule_id="PROFILE_STATION_NOT_INCREASING",
                    message=f"纵断面 {profile.id} 的序列 {series.id} 桩号未严格递增",
                    semantic_ids=[profile.id, series.id],
                ))
                break
            previous = station
    if profile.horizontal_scale_denominator <= 0 or profile.vertical_scale_denominator <= 0:
        issues.append(ValidationIssue(
            severity="error",
            rule_id="PROFILE_SCALE_INVALID",
            message=f"纵断面 {profile.id} 的水平或垂直比例无效",
            semantic_ids=[profile.id],
        ))
    return issues


def derive_network_profile_series(
    system: NetworkSystem,
    segment_ids: Sequence[str],
    series_id: str = "network_invert",
) -> ProfileSeries:
    """按给定管段顺序生成管底/中心高程序列；顺序不连续时明确失败。"""

    segments: Dict[str, NetworkSegment] = {segment.id: segment for segment in system.segments}
    nodes = {node.id: node for node in system.nodes}
    station = 0.0
    points: List[ProfilePoint] = []
    previous_end = None
    for segment_id in segment_ids:
        if segment_id not in segments:
            raise ValueError(f"网络 {system.id} 不存在管段 {segment_id}")
        segment = segments[segment_id]
        if previous_end is not None and segment.start_node != previous_end:
            raise ValueError(
                f"纵断面管段顺序不连续：{previous_end} 后接到了 {segment.start_node}")
        start_z = segment.start_elevation
        end_z = segment.end_elevation
        if start_z is None and segment.start_node in nodes:
            start_z = nodes[segment.start_node].elevation
        if end_z is None and segment.end_node in nodes:
            end_z = nodes[segment.end_node].elevation
        if start_z is None or end_z is None:
            raise ValueError(f"管段 {segment.id} 缺少起点或终点标高")
        if not points:
            points.append((station, float(start_z)))
        station += polyline_length(segment.path)
        points.append((station, float(end_z)))
        previous_end = segment.end_node
    return ProfileSeries(id=series_id, series_type="network_elevation",
                         points=points, layer="ENV-PROFILE-NETWORK")


def compile_profile(
    profile: ProfileDefinition,
    view_id: str,
    view_scale_denominator: float,
) -> List[CadOperation]:
    """生成纵断面曲线、基准线、刻度和序列名称。"""

    if not profile.series:
        return []
    operations: List[CadOperation] = []
    paper_to_model = float(view_scale_denominator)
    all_stations = [point[0] for series in profile.series for point in series.points]
    max_station = max(all_stations)
    datum_start = profile.origin
    datum_end = profile_to_drawing_point(
        profile.origin, profile.datum_elevation, max_station, profile.datum_elevation,
        profile.horizontal_scale_denominator, profile.vertical_scale_denominator,
        paper_to_model,
    )
    operations.append(CadOperation(
        kind="line", semantic_id=profile.id, view_id=view_id, role="profile_datum",
        params={"x1": datum_start[0], "y1": datum_start[1],
                "x2": datum_end[0], "y2": datum_end[1], "layer": "ENV-PROFILE-GRID"},
        metadata={"object_type": "profile", "datum_elevation": profile.datum_elevation},
    ))
    text_height = 3.5 * view_scale_denominator
    operations.append(CadOperation(
        kind="text", semantic_id=profile.id, view_id=view_id, role="profile_datum_label",
        params={"text": f"基准标高 {profile.datum_elevation:.3f}",
                "x": datum_start[0], "y": datum_start[1] - text_height * 1.5,
                "height": text_height, "rotation": 0, "layer": "ENV-ANNO"},
        metadata={"object_type": "profile"},
    ))
    for series in profile.series:
        points = [profile_to_drawing_point(
            profile.origin, profile.datum_elevation, station, elevation,
            profile.horizontal_scale_denominator, profile.vertical_scale_denominator,
            paper_to_model,
        ) for station, elevation in series.points]
        operations.append(CadOperation(
            kind="polyline", semantic_id=series.id, view_id=view_id,
            role=f"profile_series_{series.series_type}",
            params={"points": points, "close": False, "layer": series.layer},
            metadata={"object_type": "profile_series", "profile_id": profile.id,
                      "series_type": series.series_type},
        ))
        if points:
            operations.append(CadOperation(
                kind="text", semantic_id=series.id, view_id=view_id,
                role="profile_series_label",
                params={"text": str(series.attributes.get("label", series.id)),
                        "x": points[-1][0] + text_height, "y": points[-1][1],
                        "height": text_height, "rotation": 0, "layer": "ENV-ANNO"},
                metadata={"object_type": "profile_series", "profile_id": profile.id},
            ))
    # 桩号刻度按配置间距生成；不由 MCP 猜测工程需求。
    interval = float(profile.attributes.get("station_interval", 0))
    if interval > 0:
        station = 0.0
        tick = 2.0 * view_scale_denominator
        while station <= max_station + 1e-9:
            point = profile_to_drawing_point(
                profile.origin, profile.datum_elevation, station, profile.datum_elevation,
                profile.horizontal_scale_denominator, profile.vertical_scale_denominator,
                paper_to_model,
            )
            operations.append(CadOperation(
                kind="line", semantic_id=profile.id, view_id=view_id, role="profile_station_tick",
                params={"x1": point[0], "y1": point[1], "x2": point[0],
                        "y2": point[1] - tick, "layer": "ENV-PROFILE-GRID"},
                metadata={"object_type": "profile", "station": station},
            ))
            station += interval
    return operations
