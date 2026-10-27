"""Preview and verify native CHAMFER operations without drawing substitutes."""

from __future__ import annotations

import copy
import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cad_mcp.core.geometry import MAX_DRAWING_SCAN_ENTITIES
from cad_mcp.drawing.autocad import _bounded_com_scan, _refresh_com_dispatch


TOL = 1e-6
Number = Annotated[float, Field(allow_inf_nan=False, strict=True)]
Point = tuple[Number, Number, Number]
Handle = Annotated[str, Field(pattern=r"^[0-9]{1,40}$")]


class ChamferModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ChamferPick(ChamferModel):
    handle: Handle = Field(description="模型空间实体的十进制句柄")
    point: Point = Field(description="WCS 毫米 [x,y,z]；在线上且位于要保留的一侧，用于确定方向或多段线线段")


class ChamferPair(ChamferModel):
    mode: Literal["pair"] = "pair"
    first: ChamferPick
    second: ChamferPick


class ChamferPolyline(ChamferModel):
    mode: Literal["polyline"] = "polyline"
    handle: Handle = Field(description="整体倒角的二维多段线十进制句柄")


Target = Annotated[ChamferPair | ChamferPolyline, Field(discriminator="mode")]


class ChamferRequest(ChamferModel):
    targets: list[Target] = Field(min_length=1, max_length=20, description="依次处理的对象组；整体倒角或两对象/同一多段线相邻直线段")
    method: Literal["distance", "angle"] = "distance"
    distance1: Number = Field(ge=0, description="第一侧倒角距离或角度方式的长度，毫米；距离方式双零表示尖角")
    distance2: Number | None = Field(default=None, ge=0, description="距离方式的第二侧距离；省略等于 distance1")
    angle: Number | None = Field(default=None, ge=0, lt=180, description="角度方式：相对第一侧的倒角角度，度；须小于所选夹角的补角")
    trim: bool = Field(default=True, strict=True, description="true 修剪/延伸原线；false 保留原线")
    dry_run: bool = Field(default=True, strict=True, description="默认零写入预览；false 执行并验证，不保存图纸")

    @model_validator(mode="after")
    def method_parameters(self):
        if self.method == "distance":
            if self.angle is not None:
                raise ValueError("距离方式不能同时指定 angle")
        elif self.distance2 is not None or self.angle is None:
            raise ValueError("角度方式必须指定 angle，且不能指定 distance2")
        elif (self.distance1 == 0) != (self.angle == 0):
            raise ValueError("角度方式长度和角度必须同时大于零，或同时为零以形成尖角")
        for target in self.targets:
            handles = ([target.handle] if target.mode == "polyline" else
                       [target.first.handle, target.second.handle])
            if any(int(h) <= 0 for h in handles):
                raise ValueError("句柄必须为正整数的十进制字符串")
        return self


def fresh_com(obj):
    """Read actual runtime member maps; CHAMFER may turn an XLINE into a RAY.

    pywin32's lazy maps can also classify Documents.Add as a generic object.
    No makepy generation or fixed, version-dependent DISPIDs are used here.
    """
    return _refresh_com_dispatch(obj)


def _vec(a, b):
    return tuple(y - x for x, y in zip(a, b))


def _add(p, u, length):
    return _point(tuple(x + length * y for x, y in zip(p, u)))


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _unit(v):
    length = math.sqrt(_dot(v, v))
    if not math.isfinite(length) or length <= TOL:
        raise ValueError("倒角对象含零长度线段")
    return tuple(x / length for x in v)


def _close(a, b):
    return math.dist(a, b) <= TOL


def _point(value):
    point = tuple(float(v) for v in value)
    if len(point) != 3 or not all(math.isfinite(v) for v in point):
        raise ValueError("无法读取有限的三维图元坐标")
    return point


@_bounded_com_scan
def _handles(backend, msp):
    count = int(msp.Count)
    if count > MAX_DRAWING_SCAN_ENTITIES:
        raise ValueError(f"倒角结果验证最多扫描 {MAX_DRAWING_SCAN_ENTITIES} 个模型空间实体")
    result = set()
    for i in range(count):
        backend._scan_checkpoint(i)
        result.add(str(int(str(msp.Item(i).Handle), 16)))
    return result


@_bounded_com_scan
def _read(doc, msp, handle):
    entity = fresh_com(doc.HandleToObject(format(int(handle), "X")))
    if int(entity.OwnerID) != int(msp.ObjectID):
        raise ValueError("倒角仅接受模型空间实体，不接受纸空间、块内或非图形对象")
    kind = str(entity.EntityName)
    state = {"handle": str(int(handle)), "kind": kind, "layer": str(entity.Layer)}
    if kind == "AcDbLine":
        state["points"] = [_point(entity.StartPoint), _point(entity.EndPoint)]
    elif kind in {"AcDbRay", "AcDbXline"}:
        base = _point(entity.BasePoint)
        state["points"] = [base, _add(base, _unit(_point(entity.DirectionVector)), 1)]
    elif kind in {"AcDbPolyline", "AcDb2dPolyline"}:
        if not _close(_point(entity.Normal), (0, 0, 1)):
            raise ValueError("二维多段线倒角当前要求法向为 WCS +Z")
        if kind == "AcDb2dPolyline" and int(entity.Type) != 0:
            raise ValueError("倒角不接受拟合或样条二维多段线")
        coords = tuple(float(v) for v in entity.Coordinates)
        stride = 2 if kind == "AcDbPolyline" else 3
        count = len(coords) // stride
        if len(coords) % stride or not 2 <= count <= 1000:
            raise ValueError("二维多段线必须包含 2 到 1000 个顶点")
        elevation = float(entity.Elevation)
        state["points"] = [_point((coords[i * stride], coords[i * stride + 1], elevation))
                           for i in range(count)]
        state["bulges"] = [float(entity.GetBulge(i)) for i in range(count)]
        if not all(math.isfinite(b) for b in state["bulges"]):
            raise ValueError("多段线圆弧参数不是有限数值")
        state["closed"] = bool(entity.Closed)
    else:
        raise ValueError("倒角接受 LINE、RAY、XLINE 和二维多段线的直线段")
    if any(abs(p[2] - state["points"][0][2]) > TOL for p in state["points"]):
        raise ValueError("二维倒角不接受跨标高或倾斜线段")
    return state


def _segments(state):
    points = state["points"]
    if "bulges" not in state:
        low, high = (0, math.dist(*points)) if state["kind"] == "AcDbLine" else (
            (0, math.inf) if state["kind"] == "AcDbRay" else (-math.inf, math.inf))
        return [(0, points[0], _unit(_vec(*points)), low, high)]
    count = len(points) if state["closed"] else len(points) - 1
    return [(i, points[i], _unit(_vec(points[i], points[(i + 1) % len(points)])),
             0, math.dist(points[i], points[(i + 1) % len(points)]))
            for i in range(count) if abs(state["bulges"][i]) <= 1e-12]


def _pick(state, point):
    found = []
    for seg in _segments(state):
        i, base, direction, low, high = seg
        offset = _dot(_vec(base, point), direction)
        if low - TOL <= offset <= high + TOL and _close(point, _add(base, direction, offset)):
            found.append(seg)
    if len(found) != 1:
        raise ValueError("选择点须唯一落在一条直线段上；避开多段线顶点、圆弧和交叠段")
    return found[0]


def _intersection(first, second):
    _, a, u, _, _ = first
    _, b, v, _, _ = second
    det = u[0] * v[1] - u[1] * v[0]
    if abs(a[2] - b[2]) > TOL or abs(det) <= 1e-10:
        raise ValueError("倒角要求共面且不平行的两条直线段")
    delta = _vec(a, b)
    return _add(a, u, (delta[0] * v[1] - delta[1] * v[0]) / det)


def _distances(request, u, v):
    d1 = request.distance1
    if request.method == "distance":
        return d1, d1 if request.distance2 is None else request.distance2
    if d1 == 0:
        return 0., 0.
    phi = math.acos(max(-1, min(1, _dot(u, v))))
    alpha = math.radians(request.angle)
    if phi + alpha >= math.pi - 1e-10:
        raise ValueError("倒角角度须小于所选保留方向夹角的补角")
    return d1, d1 * math.sin(alpha) / math.sin(phi + alpha)


def _side(segment, intersection, pick):
    _, base, direction, low, high = segment
    offset = _dot(_vec(intersection, pick), direction)
    if abs(offset) <= TOL:
        raise ValueError("选择点不能位于交点；请选择需要保留的那一侧")
    sign = 1 if offset > 0 else -1
    bound = high if sign > 0 else low
    end = None if math.isinf(bound) else _add(base, direction, bound)
    return tuple(sign * x for x in direction), end


def _trimmed(state, cut, direction, end):
    result = copy.deepcopy(state)
    if end is not None and _dot(_vec(cut, end), direction) <= TOL:
        raise ValueError("倒角距离过大，会耗尽所选线段；请减小距离或改选保留方向")
    if "bulges" in state:
        raise ValueError("两段多段线必须属于同一条多段线且相邻")
    if end is None:
        result["kind"] = "AcDbRay"
        result["points"] = [cut, _add(cut, direction, 1)]
    else:
        result["kind"] = "AcDbLine"
        result["points"] = [cut, end]
    return result


def _replace_corners(state, corners):
    result = copy.deepcopy(state)
    points, bulges = [], []
    for i, point in enumerate(state["points"]):
        if i in corners and not _close(*corners[i]):
            points.extend(corners[i])
            bulges.extend((0., state["bulges"][i]))
        else:
            points.append(point)
            bulges.append(state["bulges"][i])
    if len(points) > 1000:
        raise ValueError("倒角后多段线顶点超过 1000 项，请拆分操作")
    result.update(points=points, bulges=bulges)
    return result


def _pair_plan(target, states, request, active_layer):
    first, second = (states[str(int(p.handle))] for p in (target.first, target.second))
    s1, s2 = _pick(first, target.first.point), _pick(second, target.second.point)
    cross = _intersection(s1, s2)
    u, end1 = _side(s1, cross, target.first.point)
    v, end2 = _side(s2, cross, target.second.point)
    d1, d2 = _distances(request, u, v)
    p1, p2 = _add(cross, u, d1), _add(cross, v, d2)
    if 0 < math.dist(p1, p2) <= TOL:
        raise ValueError("倒角尺寸小于结果验证容差 0.000001 mm")
    before = {s["handle"]: copy.deepcopy(s) for s in (first, second)}
    after = copy.deepcopy(before)
    if first["handle"] == second["handle"]:
        if "bulges" not in first:
            raise ValueError("不能对同一个直线、射线或构造线倒角")
        n = len(first["points"])
        if (s1[0] + 1) % n == s2[0]:
            corner, cuts = s2[0], (p1, p2)
        elif (s2[0] + 1) % n == s1[0]:
            corner, cuts = s1[0], (p2, p1)
        else:
            raise ValueError("多段线选段倒角当前要求两条直线段相邻")
        if not _close(cross, first["points"][corner]):
            raise ValueError("多段线选段未形成可验证的公共顶点")
        for cut, direction, end in ((p1, u, end1), (p2, v, end2)):
            if end is None or _dot(_vec(cut, end), direction) <= TOL:
                raise ValueError("倒角距离过大，会耗尽多段线线段")
        if request.trim:
            after[first["handle"]] = _replace_corners(first, {corner: cuts})
    elif "bulges" in first or "bulges" in second:
        raise ValueError("多段线选段倒角须选择同一多段线的相邻直线段")
    elif request.trim:
        for state, cut, direction, end in ((first, p1, u, end1), (second, p2, v, end2)):
            after[state["handle"]] = _trimmed(state, cut, direction, end)
    lines = []
    if not _close(p1, p2) and (not request.trim or first["handle"] != second["handle"]):
        lines.append({"points": [p1, p2], "layer": first["layer"] if first["layer"] == second["layer"] else active_layer})
    return {"target": target.model_dump(), "before": before, "after": after, "lines": lines,
            "corners": [{"intersection": cross, "first": p1, "second": p2}]}


def _polyline_plan(target, states, request):
    state = states[str(int(target.handle))]
    if "bulges" not in state:
        raise ValueError("polyline 方式仅接受二维多段线")
    points, corners, edges, details = state["points"], {}, [], []
    n = len(points)
    for i in range(n):
        if not state["closed"] and i in {0, n - 1}:
            continue
        prev = (i - 1) % n
        if abs(state["bulges"][prev]) > 1e-12 or abs(state["bulges"][i]) > 1e-12:
            continue  # Native Polyline option leaves arc-adjacent vertices alone.
        u, v = _unit(_vec(points[i], points[prev])), _unit(_vec(points[i], points[(i + 1) % n]))
        if abs(u[0] * v[1] - u[1] * v[0]) <= 1e-10:
            continue
        d1, d2 = _distances(request, u, v)
        cuts = (_add(points[i], u, d1), _add(points[i], v, d2))
        if 0 < math.dist(*cuts) <= TOL:
            raise ValueError("倒角尺寸小于结果验证容差 0.000001 mm")
        corners[i] = cuts
        details.append({"intersection": points[i], "first": cuts[0], "second": cuts[1]})
        if not _close(*cuts):
            edges.append({"points": list(cuts), "layer": state["layer"]})
    for index, base, direction, low, high in _segments(state):
        end = (index + 1) % n
        start_cut = corners[index][1] if index in corners else base
        end_cut = corners[end][0] if end in corners else points[end]
        if _dot(_vec(start_cut, end_cut), direction) <= TOL:
            raise ValueError("多段线边长不足以容纳相邻倒角；不允许原生命令只处理部分顶点")
    after = _replace_corners(state, corners) if request.trim else copy.deepcopy(state)
    return {"target": target.model_dump(), "before": {state["handle"]: state},
            "after": {state["handle"]: after}, "lines": [] if request.trim else edges, "corners": details}


def _same(expected, actual):
    if any(expected.get(key) != actual.get(key) for key in ("kind", "layer", "closed")):
        return False
    a, b = expected["points"], actual["points"]
    if len(a) != len(b):
        return False
    if "bulges" in expected:
        n = len(a)
        offsets = range(n) if expected["closed"] else (0,)
        return any(all(_close(a[i], b[(i + offset) % n]) and
                       abs(expected["bulges"][i] - actual["bulges"][(i + offset) % n]) <= 1e-10
                       for i in range(n)) for offset in offsets)
    if expected["kind"] == "AcDbLine":
        return (_close(a[0], b[0]) and _close(a[1], b[1])) or (_close(a[0], b[1]) and _close(a[1], b[0]))
    return all(_close(p, q) for p, q in zip(a, b))


def _command(plan):
    # ename + pick point preserves entity identity even where objects overlap.
    # Selection points are WCS; native commands consume UCS coordinates.
    def point(pick):
        xyz = " ".join(format(v, ".17g") for v in pick["point"])
        return f'(trans (list {xyz}) 0 1)'
    def selection(pick):
        return f'(list (handent "{int(pick["handle"]):X}") {point(pick)})'
    target = plan["target"]
    if _uses_point_picks(plan):
        handle = format(int(target["first"]["handle"]), "X")
        # CHAMFER does not honor an ename/pick-point list for two segments of
        # the same polyline. Use native point picks only after checking that
        # the entire pick aperture contains exactly the requested entity.
        return (
            '((lambda (/ p1 p2 eps s1 s2) '
            f'(setq p1 {point(target["first"])} p2 {point(target["second"])} '
            'eps (* 1.5 (getvar "VIEWSIZE") (/ (+ 2.0 (max (getvar "PICKBOX") (getvar "APERTURE"))) '
            '(cadr (getvar "SCREENSIZE"))))) '
            '(setq s1 (ssget "_C" (mapcar \'- p1 (list eps eps 0.0)) '
            '(mapcar \'+ p1 (list eps eps 0.0)) \'((410 . "Model"))) '
            's2 (ssget "_C" (mapcar \'- p2 (list eps eps 0.0)) '
            '(mapcar \'+ p2 (list eps eps 0.0)) \'((410 . "Model")))) '
            '(if (and s1 s2 (= 1 (sslength s1)) (= 1 (sslength s2)) '
            f'(eq (ssname s1 0) (handent "{handle}")) '
            f'(eq (ssname s2 0) (handent "{handle}"))) '
            '(progn (initcommandversion 2) (command-s "_.CHAMFER" "_non" p1 "_non" p2))) (princ)))\n'
        )
    if target["mode"] == "polyline":
        args = f'"_Polyline" (handent "{int(target["handle"]):X}")'
    else:
        args = selection(target["first"]) + " " + selection(target["second"])
    # Set dimensions with SetVariable beforehand. In AutoLISP, CHAMFER's
    # Distance/Angle options may finish the command rather than await selection.
    return f'(progn (command-s "_.CHAMFER" {args}) (princ))\n'


def _uses_point_picks(plan):
    target = plan["target"]
    return (target["mode"] == "pair" and
            int(target["first"]["handle"]) == int(target["second"]["handle"]))


def _restore_variables(doc, variables):
    errors = []
    for name, value in variables.items():
        try:
            doc.SetVariable(name, value)
        except Exception as exc:
            errors.append(exc)
    if errors:
        raise RuntimeError("倒角后部分系统变量未能恢复") from errors[0]
    if any(doc.GetVariable(name) != value for name, value in variables.items()):
        raise RuntimeError("倒角后系统变量恢复结果不一致")


def chamfer(backend, payload):
    from cad_mcp.core.semantics import TRANSACTION_BACKEND
    outer_transaction = TRANSACTION_BACKEND.get() is backend
    request = ChamferRequest.model_validate(payload)
    backend._check_doc()
    doc, msp = fresh_com(backend.doc), fresh_com(backend.msp)
    def ensure_active():
        active = fresh_com(backend.acad.ActiveDocument)
        if (str(active.Name), str(active.FullName)) != (str(doc.Name), str(doc.FullName)):
            raise ValueError("AutoCAD 活动图纸与 MCP 绑定图纸不一致；请先激活绑定图纸再倒角")
    ensure_active()
    if int(doc.GetVariable("CMDACTIVE")) != 0:
        raise ValueError("请先结束 AutoCAD 当前命令再倒角")
    ux, uy = (_point(doc.GetVariable(name)) for name in ("UCSXDIR", "UCSYDIR"))
    uz = (ux[1] * uy[2] - ux[2] * uy[1], ux[2] * uy[0] - ux[0] * uy[2], ux[0] * uy[1] - ux[1] * uy[0])
    if not _close(uz, (0, 0, 1)):
        raise ValueError("二维倒角当前要求 UCS 的 Z 方向为 WCS +Z；允许平移或绕 Z 旋转 UCS")
    initial_handles = _handles(backend, msp)
    active_layer = str(fresh_com(doc.ActiveLayer).Name)
    requested = {str(int(h)) for t in request.targets for h in (
        [t.handle] if t.mode == "polyline" else [t.first.handle, t.second.handle])}
    if not requested.issubset(initial_handles):
        raise ValueError("倒角句柄不存在于当前模型空间，请重新查询十进制句柄")
    original = {h: _read(doc, msp, h) for h in requested}
    states, plans = copy.deepcopy(original), []
    for target in request.targets:
        plan = (_polyline_plan(target, states, request) if target.mode == "polyline" else
                _pair_plan(target, states, request, active_layer))
        states.update(copy.deepcopy(plan["after"]))
        plans.append(plan)
    point_picks = any(_uses_point_picks(p) for p in plans)
    view = None
    if point_picks:
        from cad_mcp.drawing.autocad import _pt
        if not _close(_unit(_point(doc.GetVariable("VIEWDIR"))), (0, 0, 1)):
            raise ValueError("多段线选段倒角需要从 WCS +Z 俯视；请先切换俯视图")
        center = _point(doc.GetVariable("VIEWCTR"))
        view = (_point(doc.Utility.TranslateCoordinates(_pt(*center), 1, 0, False)),
                float(doc.GetVariable("VIEWSIZE")))
    def restore_view():
        if view is not None:
            from cad_mcp.drawing.autocad import _pt
            ensure_active()
            fresh_com(backend.acad).ZoomCenter(_pt(*view[0]), view[1])
            restored = doc.Utility.TranslateCoordinates(_pt(*doc.GetVariable("VIEWCTR")), 1, 0, False)
            if not _close(view[0], _point(restored)) or not math.isclose(
                    view[1], float(doc.GetVariable("VIEWSIZE")), rel_tol=1e-10, abs_tol=TOL):
                raise RuntimeError("倒角后视图恢复结果不一致")
    layers = {s["layer"] for s in original.values()} | {line["layer"] for p in plans for line in p["lines"]}
    for name in layers:
        layer = fresh_com(doc.Layers.Item(name))
        if bool(layer.Lock) or bool(layer.Freeze):
            raise ValueError(f"倒角涉及的图层被锁定或冻结: {name}")
    changes = any(p["lines"] or any(not _same(p["before"][h], s) for h, s in p["after"].items()) for p in plans)
    result = {"mode": "acad", "method": request.method, "trim": request.trim,
              "dry_run": request.dry_run, "would_write": changes, "applied": False,
              "verified": False, "plans": plans, "entities": []}
    if request.dry_run or not changes:
        return {**result, "verified": not request.dry_run}
    undo_control = int(doc.GetVariable("UNDOCTL"))
    if not undo_control & 1 or ((undo_control & 8 or backend._undo_mark_open) and not outer_transaction):
        raise ValueError("倒角需要启用撤销且没有嵌套撤销组；未执行写入")
    variables = {name: doc.GetVariable(name) for name in
                 ("CHAMFERA", "CHAMFERB", "CHAMFERC", "CHAMFERD", "CHAMMODE", "TRIMMODE", "OSMODE")}
    if outer_transaction:
        def restored_state():
            if view is not None:
                from cad_mcp.drawing.autocad import _pt
                center = doc.Utility.TranslateCoordinates(_pt(*doc.GetVariable("VIEWCTR")), 1, 0, False)
                if not _close(view[0], _point(center)) or not math.isclose(
                        view[1], float(doc.GetVariable("VIEWSIZE")), rel_tol=1e-10, abs_tol=TOL):
                    return False
            return all(doc.GetVariable(name) == value for name, value in variables.items())
        backend._native_rollback_probe = restored_state
    started = True if outer_transaction else backend._begin_undo()
    if not started:
        raise RuntimeError("未能建立独立倒角撤销组，未执行写入")
    try:
        try:
            settings = {"CHAMMODE": int(request.method == "angle"), "TRIMMODE": int(request.trim), "OSMODE": 0}
            if request.method == "distance":
                settings.update(CHAMFERA=request.distance1, CHAMFERB=(request.distance1 if request.distance2 is None else request.distance2))
            else:
                settings.update(CHAMFERC=request.distance1, CHAMFERD=math.radians(request.angle))
            for name, value in settings.items():
                doc.SetVariable(name, value)
            known_handles = initial_handles.copy()
            created = []
            for plan in plans:
                if not plan["lines"] and all(_same(plan["before"][h], s) for h, s in plan["after"].items()):
                    continue
                ensure_active()
                if _uses_point_picks(plan):
                    from cad_mcp.drawing.autocad import _pt
                    pts = next(iter(plan["before"].values()))["points"]
                    low = tuple(min(p[i] for p in pts) for i in range(3))
                    high = tuple(max(p[i] for p in pts) for i in range(3))
                    center = tuple((a + b) / 2 for a, b in zip(low, high))
                    # A diagonal-based window includes all vertices even with
                    # view twist. Restore the original view on every exit.
                    aspect = float(doc.GetVariable("SCREENSIZE")[0]) / float(doc.GetVariable("SCREENSIZE")[1])
                    size = max(math.dist(low, high) * 1.5 / min(1., aspect), TOL * 100)
                    fresh_com(backend.acad).ZoomCenter(_pt(*center), size)
                backend._send_command_and_wait(_command(plan))
                for h, expected in plan["after"].items():
                    actual = _read(doc, msp, h)
                    if not _same(expected, actual):
                        if _uses_point_picks(plan) and _same(plan["before"][h], actual):
                            raise RuntimeError("多段线选段倒角未生效；请检查选择范围是否存在其他实体，并更换选择点")
                        raise RuntimeError(f"原生 CHAMFER 结果与预览几何不一致，句柄 {h}")
                after_handles = _handles(backend, msp)
                if not known_handles.issubset(after_handles):
                    raise RuntimeError("原生 CHAMFER 意外删除了实体")
                new = [_read(doc, msp, h) for h in sorted(after_handles - known_handles, key=int)]
                pending = list(new)
                for line in plan["lines"]:
                    expected = {"kind": "AcDbLine", **line}
                    match = next((s for s in pending if _same(expected, s)), None)
                    if match is None:
                        raise RuntimeError("原生 CHAMFER 未生成预期倒角线，或尺寸/图层不一致")
                    pending.remove(match)
                if pending:
                    raise RuntimeError("原生 CHAMFER 生成了额外的非预期实体")
                known_handles = after_handles
                created.extend(new)
            result["entities"] = created
        finally:
            # A restoration failure must not be reported as a successful edit.
            try:
                _restore_variables(doc, variables)
            finally:
                restore_view()
        actual = {h: _read(doc, msp, h) for h in original}
        if not outer_transaction:
            backend._commit_undo(started)
        return {**result, "applied": True, "verified": True,
                "after": actual}
    except Exception as exc:
        if outer_transaction:
            raise
        rolled_back = backend._rollback_undo(started)
        verified = False
        if rolled_back is True:
            try:
                _restore_variables(doc, variables)
                restore_view()
                verified = (_handles(backend, msp) == initial_handles and
                            all(_same(s, _read(doc, msp, h)) for h, s in original.items()) and
                            all(doc.GetVariable(k) == v for k, v in variables.items()))
            except Exception:
                verified = False
        exc.rollback_status = "succeeded" if verified else "failed_or_unverified"
        raise
