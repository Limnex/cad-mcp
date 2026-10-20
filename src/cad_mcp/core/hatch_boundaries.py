"""Exact planar line/circular-arc arrangements used by both HATCH backends.

No curve flattening and no gap repair: a seed selects one bounded face and
disconnected enclosed components become holes. Coordinates are WCS XY.
"""

from dataclasses import dataclass
import math
from typing import List, Optional, Sequence, Tuple


Point = Tuple[float, float]
TOL = 1e-7
MAX_CURVES = 512
MAX_SPLIT_CURVES = 2048


def cross(a: Point, b: Point) -> float:
    return a[0] * b[1] - a[1] * b[0]


def sub(a: Point, b: Point) -> Point:
    return a[0] - b[0], a[1] - b[1]


@dataclass(frozen=True)
class Curve:
    start: Point
    end: Point
    center: Optional[Point] = None
    sweep: float = 0.0

    @property
    def radius(self) -> float:
        return math.dist(self.start, self.center) if self.center is not None else 0.0

    def point(self, t: float) -> Point:
        if t <= 0:
            return self.start
        if t >= 1:
            return self.end
        if self.center is None:
            return tuple(a + t * (b - a) for a, b in zip(self.start, self.end))
        angle = math.atan2(self.start[1] - self.center[1], self.start[0] - self.center[0]) + t * self.sweep
        return self.center[0] + self.radius * math.cos(angle), self.center[1] + self.radius * math.sin(angle)

    def parameter(self, point: Point) -> Optional[float]:
        if math.dist(point, self.start) <= TOL:
            return 0.0
        if math.dist(point, self.end) <= TOL:
            return 1.0
        if self.center is None:
            dx, dy = sub(self.end, self.start)
            length = math.hypot(dx, dy)
            t = ((point[0] - self.start[0]) * dx + (point[1] - self.start[1]) * dy) / (length * length)
            if 0 <= t <= 1 and math.dist(point, self.point(t)) <= TOL:
                return t
            return None
        if abs(math.dist(point, self.center) - self.radius) > TOL:
            return None
        start = math.atan2(self.start[1] - self.center[1], self.start[0] - self.center[0])
        angle = math.atan2(point[1] - self.center[1], point[0] - self.center[0])
        delta = (angle - start) % math.tau if self.sweep > 0 else (start - angle) % math.tau
        t = delta / abs(self.sweep)
        return t if 0 <= t <= 1 else None

    def reversed(self) -> "Curve":
        return Curve(self.end, self.start, self.center, -self.sweep)

    def tangent(self) -> float:
        if self.center is None:
            return math.atan2(self.end[1] - self.start[1], self.end[0] - self.start[0]) % math.tau
        radial = math.atan2(self.start[1] - self.center[1], self.start[0] - self.center[0])
        return (radial + math.copysign(math.pi / 2, self.sweep)) % math.tau

    def area(self) -> float:
        value = cross(self.start, self.end) / 2
        if self.center is not None:
            value += self.radius ** 2 * (self.sweep - math.sin(self.sweep)) / 2
        return value


@dataclass(frozen=True)
class BoundarySource:
    handle: str
    curves: Tuple[Curve, ...]
    closed: bool
    elevation: float = 0.0


@dataclass(frozen=True)
class HatchRegion:
    outer: Tuple[Curve, ...]
    holes: Tuple[Tuple[Curve, ...], ...]
    handles: Tuple[str, ...]
    seed: Point
    elevation: float

    @property
    def loops(self):
        return (self.outer, *self.holes)

    @property
    def outer_area(self) -> float:
        return abs(loop_area(self.outer))

    @property
    def excluded_area(self) -> float:
        return math.fsum(abs(loop_area(loop)) for loop in self.holes)

    @property
    def area(self) -> float:
        return self.outer_area - self.excluded_area

    @property
    def bounds(self):
        points = []
        for curve in self.outer:
            points.extend((curve.start, curve.end))
            if curve.center is not None:
                for angle in (0, math.pi / 2, math.pi, 3 * math.pi / 2):
                    p = (curve.center[0] + curve.radius * math.cos(angle), curve.center[1] + curve.radius * math.sin(angle))
                    if curve.parameter(p) is not None:
                        points.append(p)
        return (min(p[0] for p in points), min(p[1] for p in points)), (max(p[0] for p in points), max(p[1] for p in points))


def loop_area(curves: Sequence[Curve]) -> float:
    # Translate before integrating to avoid cancellation at large world coordinates.
    origin = curves[0].start
    return math.fsum(Curve(sub(c.start, origin), sub(c.end, origin), sub(c.center, origin) if c.center is not None else None, c.sweep).area() for c in curves)


def bulge_vertices(curves: Sequence[Curve]):
    return [(c.start[0], c.start[1], math.tan(c.sweep / 4) if c.center is not None else 0.0) for c in curves]


def polyline_curves(vertices, closed: bool) -> Tuple[Curve, ...]:
    vertices = [(float(x), float(y), float(bulge)) for x, y, bulge in vertices]
    if len(vertices) < 2 or any(not math.isfinite(v) for vertex in vertices for v in vertex):
        raise ValueError("边界顶点或圆弧凸度无效")
    pairs = list(zip(vertices, vertices[1:]))
    if closed:
        pairs.append((vertices[-1], vertices[0]))
    curves = []
    for (x, y, bulge), (ex, ey, _) in pairs:
        a, b = (x, y), (ex, ey)
        if math.dist(a, b) <= TOL:
            raise ValueError("边界包含零长度边或重复顶点")
        if abs(bulge) <= 1e-14:
            curves.append(Curve(a, b))
        else:
            factor = (1 - bulge * bulge) / (4 * bulge)
            center = ((x + ex) / 2 - (ey - y) * factor, (y + ey) / 2 + (ex - x) * factor)
            curves.append(Curve(a, b, center, 4 * math.atan(bulge)))
    return tuple(curves)


def arc_curves(center: Point, radius: float, start: float = 0.0, sweep: float = math.tau) -> Tuple[Curve, ...]:
    if not all(math.isfinite(v) for v in (*center, radius, start, sweep)) or radius <= TOL or not 0 < abs(sweep) <= math.tau + 1e-12:
        raise ValueError("圆形或圆弧边界参数无效")
    count = max(1, math.ceil(abs(sweep) / (math.pi / 2)))
    def point(t):
        angle = start + t * sweep
        return center[0] + radius * math.cos(angle), center[1] + radius * math.sin(angle)
    return tuple(Curve(point(i / count), point((i + 1) / count), center, sweep / count) for i in range(count))


def intersections(a: Curve, b: Curve) -> List[Point]:
    if a.center is None and b.center is None:
        r, s, delta = sub(a.end, a.start), sub(b.end, b.start), sub(b.start, a.start)
        denom = cross(r, s)
        if abs(denom) <= 1e-12 * math.hypot(*r) * math.hypot(*s):
            if abs(cross(delta, r)) / math.hypot(*r) > TOL:
                return []
            rr = r[0] ** 2 + r[1] ** 2
            ts = [((p[0] - a.start[0]) * r[0] + (p[1] - a.start[1]) * r[1]) / rr for p in (b.start, b.end)]
            lo, hi = max(0.0, min(ts)), min(1.0, max(ts))
            if (hi - lo) * math.sqrt(rr) > TOL:
                raise ValueError("边界包含重叠线段，无法唯一确定填充区域")
            return [a.point((lo + hi) / 2)] if hi >= lo - TOL / math.sqrt(rr) else []
        t, u = cross(delta, s) / denom, cross(delta, r) / denom
        return [a.point(t)] if -TOL / math.hypot(*r) <= t <= 1 + TOL / math.hypot(*r) and -TOL / math.hypot(*s) <= u <= 1 + TOL / math.hypot(*s) else []
    if a.center is None or b.center is None:
        line, arc = (a, b) if a.center is None else (b, a)
        d = sub(line.end, line.start)
        length2 = d[0] ** 2 + d[1] ** 2
        t = ((arc.center[0] - line.start[0]) * d[0] + (arc.center[1] - line.start[1]) * d[1]) / length2
        foot = (line.start[0] + t * d[0], line.start[1] + t * d[1])
        height2 = arc.radius ** 2 - math.dist(foot, arc.center) ** 2
        if height2 < -TOL * max(arc.radius, 1.0) * 2:
            return []
        offset = math.sqrt(max(0.0, height2) / length2)
        points = [(line.start[0] + u * d[0], line.start[1] + u * d[1]) for u in (t - offset, t + offset) if -TOL / math.sqrt(length2) <= u <= 1 + TOL / math.sqrt(length2)]
        return [p for p in points if arc.parameter(p) is not None]
    distance = math.dist(a.center, b.center)
    ra, rb = a.radius, b.radius
    if distance <= TOL and abs(ra - rb) <= TOL:
        angles = sorted({math.atan2(p[1] - a.center[1], p[0] - a.center[0]) % math.tau for p in (a.start, a.end, b.start, b.end)})
        for left, right in zip(angles, angles[1:] + [angles[0] + math.tau]):
            angle = (left + right) / 2
            p = (a.center[0] + ra * math.cos(angle), a.center[1] + ra * math.sin(angle))
            if right - left > TOL / ra and a.parameter(p) is not None and b.parameter(p) is not None:
                raise ValueError("边界包含重叠圆弧，无法唯一确定填充区域")
        return [p for p in (a.start, a.end, b.start, b.end) if a.parameter(p) is not None and b.parameter(p) is not None]
    if distance <= TOL or distance > ra + rb + TOL or distance < abs(ra - rb) - TOL:
        return []
    along = (ra * ra - rb * rb + distance * distance) / (2 * distance)
    height2 = ra * ra - along * along
    if height2 < -TOL * max(ra, rb, 1.0) * 2:
        return []
    dx, dy = sub(b.center, a.center)
    foot = (a.center[0] + along * dx / distance, a.center[1] + along * dy / distance)
    height = math.sqrt(max(0.0, height2))
    points = [(foot[0] + sign * height * -dy / distance, foot[1] + sign * height * dx / distance) for sign in (-1, 1)]
    return [p for p in points if a.parameter(p) is not None and b.parameter(p) is not None]


def validate_source(source: BoundarySource) -> None:
    curves = source.curves
    if not curves or len(curves) > MAX_CURVES or not math.isfinite(source.elevation):
        raise ValueError("边界为空、过于复杂或不在有效平面内")
    for c in curves:
        values = (*c.start, *c.end, *(c.center or ()), c.sweep)
        if any(not math.isfinite(v) for v in values) or math.dist(c.start, c.end) <= TOL:
            raise ValueError("边界包含无效坐标或零长度边")
        try:
            finite_area = math.isfinite(c.area())
        except OverflowError:
            finite_area = False
        if not finite_area:
            raise ValueError("边界数值超出有限精度计算范围")
    for i, a in enumerate(curves):
        for j in range(i + 1, len(curves)):
            adjacent = j == i + 1 or (source.closed and i == 0 and j == len(curves) - 1)
            for p in intersections(a, curves[j]):
                common_end = any(math.dist(p, x) <= TOL and math.dist(p, y) <= TOL for x in (a.start, a.end) for y in (curves[j].start, curves[j].end))
                if not adjacent or not common_end:
                    raise ValueError(f"边界 {source.handle} 存在自交或非相邻顶点接触")


def contains(curves: Sequence[Curve], point: Point) -> bool:
    winding = 0
    px, py = point
    for c in curves:
        if c.center is None:
            a, b = c.start, c.end
            side = cross(sub(b, a), sub(point, a))
            if a[1] <= py < b[1] and side > 0:
                winding += 1
            elif b[1] <= py < a[1] and side < 0:
                winding -= 1
        else:
            dy = py - c.center[1]
            if abs(dy) >= c.radius:
                continue
            dx = math.sqrt(max(0.0, c.radius ** 2 - dy ** 2))
            for x in (c.center[0] - dx, c.center[0] + dx):
                if x <= px:
                    continue
                t = c.parameter((x, py))
                if t is None:
                    continue
                direction = (x - c.center[0]) * c.sweep
                if direction > 0 and t < 1 - 1e-10:
                    winding += 1
                elif direction < 0 and t > 1e-10:
                    winding -= 1
    return winding != 0


def select_region(sources: Sequence[BoundarySource], seed: Point) -> HatchRegion:
    if not sources or len(sources) > 64 or len({s.handle for s in sources}) != len(sources):
        raise ValueError("必须提供 1 到 64 个不重复的边界图元句柄")
    seed = tuple(float(v) for v in seed)
    if len(seed) != 2 or not all(math.isfinite(v) for v in seed):
        raise ValueError("区域定位点必须包含两个有限坐标")
    for source in sources:
        validate_source(source)
    if any(abs(s.elevation - sources[0].elevation) > TOL for s in sources):
        raise ValueError("所有边界必须共面，不得跨不同标高拼接")
    curves = [c for source in sources for c in source.curves]
    if len(curves) > MAX_CURVES:
        raise ValueError("组合边界超过 512 条直线或圆弧，请缩小选择范围")
    if any(c.parameter(seed) is not None for c in curves):
        raise ValueError("区域定位点不能位于边界上")
    cuts = [{0.0, 1.0} for c in curves]
    for i, a in enumerate(curves):
        for j in range(i + 1, len(curves)):
            for point in intersections(a, curves[j]):
                for index in (i, j):
                    t = curves[index].parameter(point)
                    if t is not None:
                        cuts[index].add(t)
    pieces = []
    for c, parameters in zip(curves, cuts):
        values = sorted(parameters)
        for left, right in zip(values, values[1:]):
            a, b = c.point(left), c.point(right)
            if math.dist(a, b) > TOL:
                pieces.append(Curve(a, b, c.center, c.sweep * (right - left)))
    if len(pieces) > MAX_SPLIT_CURVES:
        raise ValueError("边界交点过多，超过安全计算上限")
    nodes, grid, outgoing = [], {}, []
    def node(point):
        key = (round(point[0] / TOL), round(point[1] / TOL))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for index in grid.get((key[0] + dx, key[1] + dy), []):
                    if math.dist(nodes[index], point) <= TOL:
                        return index
        index = len(nodes)
        nodes.append(point)
        outgoing.append([])
        grid.setdefault(key, []).append(index)
        return index
    endpoints = []
    directed = []
    for i, c in enumerate(pieces):
        a, b = node(c.start), node(c.end)
        endpoints.extend(((a, b), (b, a)))
        directed.extend((c, c.reversed()))
        outgoing[a].append(2 * i)
        outgoing[b].append(2 * i + 1)
    for edges in outgoing:
        if len(edges) < 2:
            raise ValueError("所选边界存在断口或悬挂线，未形成闭合区域")
        edges.sort(key=lambda h: directed[h].tangent())
        if len(edges) > 2:
            angles = [directed[h].tangent() for h in edges]
            if any((right - left) % math.tau < 1e-9 for left, right in zip(angles, angles[1:] + angles[:1])):
                raise ValueError("边界在切点处存在歧义，无法唯一确定区域")
    component, number = {}, 0
    for start in range(len(nodes)):
        if start in component:
            continue
        stack = [start]
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component[current] = number
            stack.extend(endpoints[h][1] for h in outgoing[current])
        number += 1
    visited, faces, hulls = set(), [], []
    for start in range(len(directed)):
        if start in visited:
            continue
        path, current, vertices = [], start, set()
        while current not in visited:
            visited.add(current)
            a, b = endpoints[current]
            if a in vertices:
                raise ValueError("边界存在悬挂连接或自接触，不能形成简单闭合区域")
            vertices.add(a)
            path.append(directed[current])
            options = outgoing[b]
            current = options[(options.index(current ^ 1) - 1) % len(options)]
        if current != start:
            raise ValueError("边界拓扑不一致，无法形成闭合区域")
        area = loop_area(path)
        if abs(area) <= TOL * TOL:
            raise ValueError("边界区域面积为零")
        item = (tuple(path), component[endpoints[start][0]])
        (faces if area > 0 else hulls).append(item)
    candidates = [(loop, comp) for loop, comp in faces if contains(loop, seed)]
    if not candidates:
        raise ValueError("区域定位点不在任何有界闭合区域内")
    outer, outer_component = min(candidates, key=lambda item: loop_area(item[0]))
    enclosed = [(loop, comp) for loop, comp in hulls if comp != outer_component and contains(outer, loop[0].start)]
    holes = tuple(loop for loop, comp in enclosed if not any(comp != other_comp and contains(other, loop[0].start) for other, other_comp in enclosed))
    plan = HatchRegion(outer, holes, tuple(s.handle for s in sources), seed, sources[0].elevation)
    if plan.area <= TOL * TOL:
        raise ValueError("排除内孔后没有可填充面积")
    return plan
