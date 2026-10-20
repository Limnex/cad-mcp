#!/usr/bin/env python3
"""
CAD 绘图公共几何计算（纯函数，无第三方依赖）。

多段线路径合并、trim 修剪算法被 DXF 后端（cad_drawing.py）和
AutoCAD COM 后端（cad_acad.py）共用，保证两后端结果一致。
"""

import math
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

Point = Tuple[float, float]


BulgeVertex = Tuple[float, float, float]

MAX_ARRAY_CREATED_ENTITIES = 10_000
MAX_TABLE_COLUMNS = 64
MAX_TABLE_ROWS = 1_000
MAX_TABLE_CELLS = 20_000
MAX_TABLE_TEXT_LENGTH = 200
MAX_DRAWING_SCAN_ENTITIES = 200_000
MAX_DRAWING_ISSUES = 1_000
MAX_DRAWING_COMPARISONS = 1_000_000
MAX_HATCH_BOUNDARY_POINTS = 1_000


def validate_hatch_parameters(pattern: str, scale: float, angle: float) -> Tuple[str, float, float]:
    """Validate hatch settings for both backends; angles are supplied in degrees."""
    if not isinstance(pattern, str):
        raise ValueError("填充图案名必须是字符串")
    pattern = pattern.strip().upper()
    if not pattern or len(pattern) > 255 or any(ord(char) < 32 or ord(char) == 127 for char in pattern):
        raise ValueError("填充图案名不能为空、超过 255 字符或包含控制字符")
    scale, angle = float(scale), float(angle)
    if not math.isfinite(scale) or not 0 < scale <= 1000:
        raise ValueError("填充图案比例必须是大于 0 且不超过 1000 的有限数值")
    if not math.isfinite(angle) or not -360 <= angle <= 360:
        raise ValueError("填充图案角度必须是 -360 到 360 度之间的有限数值")
    angle %= 360.0
    if pattern == "SOLID" and (scale != 1.0 or angle != 0.0):
        raise ValueError("SOLID 实心填充不使用图案比例和角度，必须使用 scale=1、angle=0")
    return pattern, scale, angle


def validate_closed_polygon(points: Sequence[Point]) -> Tuple[List[Point], float]:
    """Validate a finite, non-self-intersecting polygon and return its area.

    A repeated final vertex is normalized away.  The boundary is considered
    closed by contract; callers create the actual closed polyline/HATCH path.
    """

    normalized = [(float(point[0]), float(point[1])) for point in points]
    if any(not math.isfinite(value) for point in normalized for value in point):
        raise ValueError("填充边界坐标必须是有限数值")
    if normalized and normalized[0] == normalized[-1]:
        normalized.pop()
    if not 3 <= len(normalized) <= MAX_HATCH_BOUNDARY_POINTS:
        raise ValueError(
            f"填充边界去除重复终点后必须有 3 到 {MAX_HATCH_BOUNDARY_POINTS} 个顶点")
    if len(set(normalized)) < 3:
        raise ValueError("填充边界至少需要 3 个不同顶点")

    twice_area = sum(
        normalized[index][0] * normalized[(index + 1) % len(normalized)][1]
        - normalized[(index + 1) % len(normalized)][0] * normalized[index][1]
        for index in range(len(normalized))
    )
    area = abs(twice_area) / 2.0

    def orientation(a: Point, b: Point, c: Point) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def on_segment(a: Point, b: Point, c: Point) -> bool:
        return (
            min(a[0], c[0]) - 1e-12 <= b[0] <= max(a[0], c[0]) + 1e-12
            and min(a[1], c[1]) - 1e-12 <= b[1] <= max(a[1], c[1]) + 1e-12
        )

    def intersects(a: Point, b: Point, c: Point, d: Point) -> bool:
        values = orientation(a, b, c), orientation(a, b, d), orientation(c, d, a), orientation(c, d, b)
        if values[0] * values[1] < 0 and values[2] * values[3] < 0:
            return True
        return any((
            abs(values[0]) <= 1e-12 and on_segment(a, c, b),
            abs(values[1]) <= 1e-12 and on_segment(a, d, b),
            abs(values[2]) <= 1e-12 and on_segment(c, a, d),
            abs(values[3]) <= 1e-12 and on_segment(c, b, d),
        ))

    count = len(normalized)
    for first in range(count):
        a, b = normalized[first], normalized[(first + 1) % count]
        for second in range(first + 1, count):
            if second in {first, (first + 1) % count}:
                continue
            if first == 0 and second == count - 1:
                continue
            c, d = normalized[second], normalized[(second + 1) % count]
            if intersects(a, b, c, d):
                raise ValueError(
                    f"填充边界存在自交或重叠边：边 {first} 与边 {second}")
    if area <= 1e-12:
        raise ValueError("填充边界面积为 0，无法创建实心填充")
    return normalized, area


def validate_bulk_creation(source_count: int, copies_per_source: int,
                           operation: str) -> int:
    if source_count < 1 or copies_per_source < 1:
        raise ValueError(f"{operation}至少需要一个源图元和一个新增副本")
    total = source_count * copies_per_source
    if total > MAX_ARRAY_CREATED_ENTITIES:
        raise ValueError(
            f"{operation}本次将新增 {total} 个图元，超过单次上限 "
            f"{MAX_ARRAY_CREATED_ENTITIES}；请先打包为图块或分批执行")
    return total


def validate_table_data(col_widths: Sequence[float], headers: Sequence[str],
                        rows: Sequence[Sequence[str]]) -> Tuple[int, int]:
    ncol = len(col_widths)
    nrow = len(rows)
    if not 1 <= ncol <= MAX_TABLE_COLUMNS:
        raise ValueError(f"表格列数必须在 1 到 {MAX_TABLE_COLUMNS} 之间")
    if not 0 <= nrow <= MAX_TABLE_ROWS:
        raise ValueError(f"表格数据行数必须在 0 到 {MAX_TABLE_ROWS} 之间")
    if ncol * nrow > MAX_TABLE_CELLS:
        raise ValueError(
            f"表格共有 {ncol * nrow} 个数据单元格，超过单次上限 {MAX_TABLE_CELLS}")
    if len(headers) != ncol:
        raise ValueError(f"表头列数 {len(headers)} 与列宽列数 {ncol} 不一致")
    if any(not math.isfinite(float(width)) or float(width) <= 0
           for width in col_widths):
        raise ValueError("每个表格列宽都必须是大于 0 的有限数值")
    for index, text in enumerate(headers):
        if len(str(text)) > MAX_TABLE_TEXT_LENGTH:
            raise ValueError(f"表头第 {index + 1} 列超过 {MAX_TABLE_TEXT_LENGTH} 个字符")
    for row_index, row in enumerate(rows):
        if len(row) != ncol:
            raise ValueError(f"数据行 {row_index + 1} 的列数 {len(row)} 与列宽列数 {ncol} 不一致")
        for column_index, value in enumerate(row):
            if len(str(value)) > MAX_TABLE_TEXT_LENGTH:
                raise ValueError(
                    f"数据行 {row_index + 1} 第 {column_index + 1} 列超过 "
                    f"{MAX_TABLE_TEXT_LENGTH} 个字符")
    return nrow, ncol


def scan_line_circle_issues(lines, circles, tolerance: float) -> Dict[str, object]:
    """Find nearby duplicates/gaps with a spatial grid and explicit work limits."""

    tol = float(tolerance)
    if not math.isfinite(tol) or tol <= 0:
        raise ValueError("图面检查容差必须是大于 0 的有限数值")
    # Ignore only numerical noise, never the user's whole search tolerance.
    connection_epsilon = min(1e-9, tol * 1e-6)
    issues: List[str] = []
    comparisons = 0
    complete = True
    stopped_reason = None

    def cell(point: Point) -> Tuple[int, int]:
        return math.floor(float(point[0]) / tol), math.floor(float(point[1]) / tol)

    def neighbors(point: Point):
        x, y = cell(point)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                yield x + dx, y + dy

    def record(message: str) -> bool:
        nonlocal complete, stopped_reason
        if len(issues) >= MAX_DRAWING_ISSUES:
            complete = False
            stopped_reason = f"问题数超过上限 {MAX_DRAWING_ISSUES}"
            return False
        issues.append(message)
        return True

    circle_grid = defaultdict(list)
    for index, (handle, center, radius) in enumerate(circles):
        candidates = set()
        for key in neighbors(center):
            candidates.update(circle_grid.get(key, ()))
        for other_index in candidates:
            comparisons += 1
            if comparisons > MAX_DRAWING_COMPARISONS:
                complete = False
                stopped_reason = f"候选比较次数超过上限 {MAX_DRAWING_COMPARISONS}"
                break
            other_handle, other_center, other_radius = circles[other_index]
            if (abs(center[0] - other_center[0]) <= tol
                    and abs(center[1] - other_center[1]) <= tol
                    and abs(radius - other_radius) <= tol):
                if not record(
                    f"[重叠] 圆 {other_handle} 与圆 {handle} 几乎重合"
                    f"（圆心 {center[0]:.1f},{center[1]:.1f}，半径 {radius:.1f}）"):
                    break
        if not complete:
            break
        circle_grid[cell(center)].append(index)

    endpoint_grid = defaultdict(list)
    if complete:
        for index, (handle, start, end) in enumerate(lines):
            candidates = set()
            for point in (start, end):
                for key in neighbors(point):
                    candidates.update(item[0] for item in endpoint_grid.get(key, ()))
            for other_index in candidates:
                comparisons += 1
                if comparisons > MAX_DRAWING_COMPARISONS:
                    complete = False
                    stopped_reason = f"候选比较次数超过上限 {MAX_DRAWING_COMPARISONS}"
                    break
                other_handle, other_start, other_end = lines[other_index]
                direct = (math.hypot(start[0] - other_start[0], start[1] - other_start[1]) <= tol
                          and math.hypot(end[0] - other_end[0], end[1] - other_end[1]) <= tol)
                reverse = (math.hypot(start[0] - other_end[0], start[1] - other_end[1]) <= tol
                           and math.hypot(end[0] - other_start[0], end[1] - other_start[1]) <= tol)
                if direct or reverse:
                    direction = "方向相反且" if reverse and not direct else ""
                    if not record(f"[重叠] 线 {other_handle} 与线 {handle} {direction}几乎完全重合"):
                        break
                    continue
                distance = min(
                    math.hypot(start[0] - other_start[0], start[1] - other_start[1]),
                    math.hypot(start[0] - other_end[0], start[1] - other_end[1]),
                    math.hypot(end[0] - other_start[0], end[1] - other_start[1]),
                    math.hypot(end[0] - other_end[0], end[1] - other_end[1]),
                )
                if connection_epsilon < distance <= tol:
                    if not record(
                        f"[断线] 线 {other_handle} 与线 {handle} 端点相距 "
                        f"{distance:.6g}mm，可能未接上（容差 {tol:.6g}mm）"):
                        break
            if not complete:
                break
            endpoint_grid[cell(start)].append((index, start))
            endpoint_grid[cell(end)].append((index, end))

    return {
        "issues": issues,
        "complete": complete,
        "stopped_reason": stopped_reason,
        "comparisons": comparisons,
    }


def _near(a: BulgeVertex, b: BulgeVertex, tolerance: float) -> bool:
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= tolerance


def reverse_polyline_part(part: Sequence[BulgeVertex]) -> List[BulgeVertex]:
    """反转一段开放多段线，同时反转各段 bulge 的方向。"""
    vertices = [(float(x), float(y), float(b)) for x, y, b in part]
    if len(vertices) < 2:
        raise ValueError("待合并路径至少需要两个顶点")
    result: List[BulgeVertex] = []
    for index in range(len(vertices) - 1, -1, -1):
        if index > 0:
            bulge = -vertices[index - 1][2]
        else:
            bulge = 0.0
        result.append((vertices[index][0], vertices[index][1], bulge))
    return result


def _concat_parts(left: Sequence[BulgeVertex],
                  right: Sequence[BulgeVertex]) -> List[BulgeVertex]:
    """连接两段已经首尾相接的 bulge 顶点序列。"""
    result = list(left)
    joint = result[-1]
    first = right[0]
    result[-1] = (joint[0], joint[1], first[2])
    result.extend(right[1:])
    return result


def join_polyline_parts(parts: Sequence[Sequence[BulgeVertex]],
                        close: bool = False,
                        tolerance: float = 1e-6) -> Tuple[List[BulgeVertex], bool]:
    """按端点把 LINE/ARC/多段线片段排成唯一连续路径。

    每个顶点为 ``(x, y, bulge)``，bulge 属于从当前顶点到下一顶点的段。
    遇到分叉、断开或无法唯一排序时明确失败，不猜测连接关系。
    """
    if tolerance <= 0:
        raise ValueError("合并容差必须大于 0")
    remaining = [
        [(float(x), float(y), float(b)) for x, y, b in part]
        for part in parts
    ]
    if not remaining:
        raise ValueError("至少需要一个待合并图元")
    if any(len(part) < 2 for part in remaining):
        raise ValueError("每个待合并图元至少需要两个顶点")

    chain = remaining.pop(0)
    while remaining:
        end_matches = []
        start_matches = []
        for index, part in enumerate(remaining):
            if _near(chain[-1], part[0], tolerance):
                end_matches.append((index, part))
            if _near(chain[-1], part[-1], tolerance):
                end_matches.append((index, reverse_polyline_part(part)))
            if _near(part[-1], chain[0], tolerance):
                start_matches.append((index, part))
            if _near(part[0], chain[0], tolerance):
                start_matches.append((index, reverse_polyline_part(part)))

        end_indices = {item[0] for item in end_matches}
        start_indices = {item[0] for item in start_matches}
        if len(end_indices) > 1 or len(start_indices) > 1:
            raise ValueError("待合并图元在端点处存在分叉，无法唯一排序")
        if end_matches:
            index, part = end_matches[0]
            chain = _concat_parts(chain, part)
            remaining.pop(index)
        elif start_matches:
            index, part = start_matches[0]
            chain = _concat_parts(part, chain)
            remaining.pop(index)
        else:
            raise ValueError("待合并图元之间存在断点，无法在给定容差内连接")

    is_closed = _near(chain[0], chain[-1], tolerance)
    if close and not is_closed:
        raise ValueError("首尾端点未在给定容差内重合，不能设置为闭合多段线")
    if close and len(chain) > 2:
        chain.pop()  # Closed 标志负责最后一段；去掉重复首点。
        is_closed = True
    else:
        is_closed = False
    return chain, is_closed


def seg_intersection(p1: Point, p2: Point, p3: Point, p4: Point,
                     eps: float = 1e-9) -> Optional[Point]:
    """线段 p1p2 与 p3p4 求交。

    交点必须落在两线段范围内（含端点）。平行、共线或不相交返回 None。
    trim 语义下共线段视为无剪断点（交给端点剪断逻辑处理）。
    """
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = p3
    x4, y4 = p4
    dx1, dy1 = x2 - x1, y2 - y1
    dx2, dy2 = x4 - x3, y4 - y3
    denom = dx1 * dy2 - dy1 * dx2
    if abs(denom) < eps:
        return None
    t = ((x3 - x1) * dy2 - (y3 - y1) * dx2) / denom
    u = ((x3 - x1) * dy1 - (y3 - y1) * dx1) / denom
    if -eps <= t <= 1 + eps and -eps <= u <= 1 + eps:
        return (x1 + t * dx1, y1 + t * dy1)
    return None


def trim_polyline(points: List[Point], closed: bool,
                  cuts: List[Tuple[Point, Point]], pick: Point,
                  eps: float = 1e-9) -> Tuple[Optional[List[Point]], bool]:
    """兼容入口：修剪折线并返回顶点和闭合状态。"""

    new_points, new_closed, _ = trim_polyline_detailed(
        points, closed, cuts, pick, eps
    )
    return new_points, new_closed


def _projection_on_segment(point: Point, start: Point, end: Point,
                           eps: float) -> Optional[Tuple[float, float, Point]]:
    dx, dy = end[0] - start[0], end[1] - start[1]
    length2 = dx * dx + dy * dy
    if length2 <= eps * eps:
        return None
    raw_t = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length2
    t = min(1.0, max(0.0, raw_t))
    projected = (start[0] + t * dx, start[1] + t * dy)
    distance2 = ((point[0] - projected[0]) ** 2
                 + (point[1] - projected[1]) ** 2)
    return distance2, t, projected


def _deduplicate_path_events(events, path_tolerance: float,
                             point_tolerance: float):
    result = []
    for event in sorted(events, key=lambda item: item[0]):
        if result:
            previous = result[-1]
            if (abs(event[0] - previous[0]) <= path_tolerance
                    and math.hypot(event[1][0] - previous[1][0],
                                   event[1][1] - previous[1][1]) <= point_tolerance):
                continue
        result.append(event)
    return result


def _append_distinct(points: List[Point], point: Point, tolerance: float) -> None:
    value = (float(point[0]), float(point[1]))
    if not points or math.hypot(points[-1][0] - value[0],
                                points[-1][1] - value[1]) > tolerance:
        points.append(value)


def trim_polyline_detailed(
    points: List[Point],
    closed: bool,
    cuts: List[Tuple[Point, Point]],
    pick: Point,
    eps: float = 1e-9,
) -> Tuple[Optional[List[Point]], bool, List[Point]]:
    """按整条路径上的拾取位置修剪折线，并返回实际切点。

    Args:
        points:  折线顶点 [(x, y), ...]
        closed:  折线是否闭合（闭合折线的尾边为末点→首点）
        cuts:    切割线段列表 [(a1, a2), ...]
        pick:    保留侧参考点 (x, y)——在要保留的一侧任选一点

    Returns:
        (new_points, new_closed, cut_points)。开放折线在离拾取点最近的
        路径交点处截断；闭合折线保留包含拾取点、且由相邻两个交点围成的区段。
        无有效交点时返回 (None, closed, [])。

    闭合路径只有一个交点时无法定义应保留的区段，因此明确失败。拾取点投影在
    自交路径的多个位置或恰落在切点上时同样失败，不猜测用户意图。
    """
    if eps <= 0:
        raise ValueError("修剪容差必须大于 0")
    normalized = [(float(point[0]), float(point[1])) for point in points]
    n = len(normalized)
    if n < 2:
        return None, closed, []
    step = n if closed else n - 1
    segments = []
    cumulative = [0.0]
    for i in range(step):
        a1 = normalized[i % n]
        a2 = normalized[(i + 1) % n]
        dx, dy = a2[0] - a1[0], a2[1] - a1[1]
        length = math.hypot(dx, dy)
        segments.append((i, a1, a2, length, cumulative[-1]))
        cumulative.append(cumulative[-1] + length)
    total_length = cumulative[-1]
    if total_length <= eps:
        raise ValueError("被修剪折线没有可用长度")
    path_tolerance = max(eps, total_length * 1e-12)
    point_tolerance = max(eps, 1e-9)

    projections = []
    for i, a1, a2, length, start_s in segments:
        if length <= eps:
            continue
        projected = _projection_on_segment(pick, a1, a2, eps)
        if projected is None:
            continue
        distance2, t, projected_point = projected
        path_s = start_s + t * length
        if closed and path_s >= total_length - path_tolerance:
            path_s = 0.0
        projections.append((distance2, path_s, projected_point))
    if not projections:
        raise ValueError("无法把拾取点投影到被修剪折线路径")
    best_distance2 = min(item[0] for item in projections)
    distance_tolerance = max(eps * eps, best_distance2 * 1e-12)
    nearest = [item for item in projections
               if abs(item[0] - best_distance2) <= distance_tolerance]
    nearest_positions = []
    for item in nearest:
        if not any(
            abs(item[1] - existing) <= path_tolerance
            or (closed and abs(abs(item[1] - existing) - total_length) <= path_tolerance)
            for existing in nearest_positions
        ):
            nearest_positions.append(item[1])
    if len(nearest_positions) != 1:
        raise ValueError("拾取点对应路径上的多个位置，无法唯一判断保留区段")
    pick_s = nearest_positions[0]

    events = []  # (path_s, point, segment_index, segment_t, distance_to_pick²)
    for i, a1, a2, length, start_s in segments:
        if length <= eps:
            continue
        for b1, b2 in cuts:
            hit = seg_intersection(a1, a2, b1, b2, eps)
            if hit is None:
                continue
            dx, dy = a2[0] - a1[0], a2[1] - a1[1]
            length2 = length * length
            t = ((hit[0] - a1[0]) * dx + (hit[1] - a1[1]) * dy) / length2
            t = min(1.0, max(0.0, t))
            path_s = start_s + t * length
            if closed and path_s >= total_length - path_tolerance:
                path_s = 0.0
            if not closed and (path_s <= path_tolerance
                               or path_s >= total_length - path_tolerance):
                continue  # 开放路径整体端点不会把路径分成两个可保留区段。
            dist2 = (hit[0] - pick[0]) ** 2 + (hit[1] - pick[1]) ** 2
            events.append((path_s, (float(hit[0]), float(hit[1])), i, t, dist2))
    events = _deduplicate_path_events(events, path_tolerance, point_tolerance)
    if not events:
        return None, closed, []

    if not closed:
        best_distance = min(item[4] for item in events)
        cut_candidates = [item for item in events
                          if abs(item[4] - best_distance)
                          <= max(eps * eps, best_distance * 1e-12)]
        if len(cut_candidates) != 1:
            raise ValueError("多个切点与拾取点等距，无法唯一选择修剪位置")
        cut_s, hit, segment_index, _, _ = cut_candidates[0]
        if abs(pick_s - cut_s) <= path_tolerance:
            raise ValueError("拾取点位于切割交点上，无法判断应保留哪一侧")
        new: List[Point] = []
        if pick_s < cut_s:
            for point in normalized[:segment_index + 1]:
                _append_distinct(new, point, point_tolerance)
            _append_distinct(new, hit, point_tolerance)
        else:
            _append_distinct(new, hit, point_tolerance)
            for point in normalized[segment_index + 1:]:
                _append_distinct(new, point, point_tolerance)
        if len(new) < 2:
            raise ValueError("修剪结果不足两个不同顶点")
        return new, False, [hit]

    if len(events) < 2:
        raise ValueError("闭合多段线至少需要两个不同切割交点才能确定保留区段")
    for event in events:
        circular_distance = abs(pick_s - event[0])
        circular_distance = min(circular_distance, total_length - circular_distance)
        if circular_distance <= path_tolerance:
            raise ValueError("拾取点位于切割交点上，无法判断应保留哪个闭合区段")

    selected = None
    for index, start_event in enumerate(events):
        end_event = events[(index + 1) % len(events)]
        start_s = start_event[0]
        end_s = end_event[0] + (total_length if index == len(events) - 1 else 0.0)
        candidate_pick_s = pick_s if pick_s > start_s else pick_s + total_length
        if start_s + path_tolerance < candidate_pick_s < end_s - path_tolerance:
            selected = (start_event, end_event, start_s, end_s)
            break
    if selected is None:
        raise ValueError("无法确定闭合多段线中包含拾取点的唯一保留区段")

    start_event, end_event, start_s, end_s = selected
    new = []
    _append_distinct(new, start_event[1], point_tolerance)
    vertex_events = [(cumulative[index], normalized[index]) for index in range(1, n)]
    vertex_events.append((total_length, normalized[0]))
    for vertex_s, vertex in vertex_events:
        for unwrapped_s in (vertex_s, vertex_s + total_length):
            if start_s + path_tolerance < unwrapped_s < end_s - path_tolerance:
                _append_distinct(new, vertex, point_tolerance)
    _append_distinct(new, end_event[1], point_tolerance)
    if len(new) < 2:
        raise ValueError("闭合多段线修剪结果不足两个不同顶点")
    return new, False, [start_event[1], end_event[1]]
