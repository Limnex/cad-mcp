"""Verified AutoCAD FILLET operations. DXF geometry is a separate future task."""

from __future__ import annotations

import math
import re
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cad_mcp.core.geometry import MAX_DRAWING_SCAN_ENTITIES


Finite = Annotated[float, Field(allow_inf_nan=False, strict=True)]
Point = tuple[Finite, Finite, Finite]
KINDS = {"AcDbLine": "LINE", "AcDbArc": "ARC", "AcDbCircle": "CIRCLE",
         "AcDbPolyline": "LWPOLYLINE"}


class FilletInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    mode: Literal["pair", "polyline"] = Field(default="pair", description="pair=两次拾取；polyline=整条二维多段线")
    handles: list[str] = Field(min_length=1, max_length=2, description="模型空间图元的十进制句柄；pair 两项，polyline 一项")
    pick_points: list[Point] | None = Field(default=None, min_length=2, max_length=2,
        description="pair 必填，与 handles 对应的 WCS [x,y,z] 拾取位置（mm）；决定保留侧或多段线线段")
    radius: Finite = Field(ge=0, description="圆角半径，mm；0 为尖角连接或去除多段线圆角")
    trim: bool = Field(default=True, strict=True, description="是否修剪/延伸原图元到圆角端点")
    dry_run: bool = Field(default=True, strict=True, description="默认仅预检；false 才执行原生 FILLET，不自动保存")

    @field_validator("handles")
    @classmethod
    def decimal_handles(cls, values):
        if any(not re.fullmatch(r"[0-9]{1,40}", value) or int(value) <= 0 for value in values):
            raise ValueError("handles 必须是正整数的十进制字符串")
        return [str(int(value)) for value in values]

    @model_validator(mode="after")
    def selection(self):
        if self.mode == "pair":
            if len(self.handles) != 2 or self.pick_points is None:
                raise ValueError("pair 模式需要两个句柄和两个拾取位置")
            if self.handles[0] == self.handles[1] and self.pick_points[0] == self.pick_points[1]:
                raise ValueError("同一多段线必须拾取不同线段")
        elif len(self.handles) != 1 or self.pick_points is not None:
            raise ValueError("polyline 模式需要一个句柄，不传 pick_points")
        return self


def _model_entities(backend):
    count = int(backend.msp.Count)
    if count > MAX_DRAWING_SCAN_ENTITIES:
        raise ValueError("模型空间图元过多，无法在本次操作中完成圆角回读核验")
    entities = {}
    for index in range(count):
        backend._scan_checkpoint(index)
        entity = backend.msp.Item(index)
        handle = str(entity.Handle).upper()
        if not re.fullmatch(r"[0-9A-F]+", handle):
            raise ValueError("AutoCAD 返回无效图元句柄")
        entities[str(int(handle, 16))] = entity
    return entities


def _snapshot(entity):
    kind = KINDS.get(str(entity.ObjectName))
    if kind is None:
        raise ValueError("圆角首版支持 LINE、ARC、CIRCLE 和 LWPOLYLINE")
    state = {"handle": str(int(str(entity.Handle), 16)), "type": kind,
             "layer": str(entity.Layer), "thickness": float(entity.Thickness)}
    if kind == "LINE":
        state.update(start=list(entity.StartPoint), end=list(entity.EndPoint))
        if len(state["start"]) != 3 or len(state["end"]) != 3:
            raise ValueError("无法读取直线的三维端点")
        z = float(state["start"][2])
        if not math.isclose(z, float(state["end"][2]), abs_tol=1e-8, rel_tol=0):
            raise ValueError("圆角仅处理平行于 XY 平面的图元")
    else:
        state["normal"] = list(entity.Normal)
        if len(state["normal"]) != 3 or any(not math.isclose(float(a), b, abs_tol=1e-8, rel_tol=0)
               for a, b in zip(state["normal"], (0, 0, 1))):
            raise ValueError("圆角仅处理法向 +Z 的 XY 平面图元")
        if kind == "LWPOLYLINE":
            coords = list(entity.Coordinates)
            if len(coords) < 4 or len(coords) % 2 or len(coords) > 2000:
                raise ValueError("多段线必须有 2 到 1000 个可读取顶点")
            state.update(coordinates=coords, closed=bool(entity.Closed),
                         bulges=[float(entity.GetBulge(i)) for i in range(len(coords) // 2)],
                         widths=[list(entity.GetWidth(i)) for i in range(len(coords) // 2)])
            z = float(entity.Elevation)
        else:
            state.update(center=list(entity.Center), radius=float(entity.Radius))
            if len(state["center"]) != 3 or state["radius"] <= 0:
                raise ValueError("无法读取有效的圆心和半径")
            z = float(state["center"][2])
            if kind == "ARC":
                state.update(start_angle=float(entity.StartAngle), end_angle=float(entity.EndAngle))
    state["elevation"] = z
    if state["thickness"] != 0:
        raise ValueError("圆角首版仅处理厚度为零的平面图元")
    # Reject corrupt COM coordinates before opening an undo group.
    def finite(value):
        if isinstance(value, (list, tuple)):
            return all(finite(v) for v in value)
        return not isinstance(value, (int, float)) or math.isfinite(value)
    if not all(finite(v) for v in state.values()):
        raise ValueError("图元几何数据包含非有限数值")
    return state


def _arcs(state):
    """Return segment signatures and measured radii, including polyline bulges."""
    if state["type"] == "ARC":
        return [(repr(state), state["radius"])]
    if state["type"] != "LWPOLYLINE":
        return []
    coords, bulges = state["coordinates"], state["bulges"]
    count = len(bulges)
    result = []
    for i in range(count if state["closed"] else count - 1):
        bulge = bulges[i]
        if bulge == 0:
            continue
        j = (i + 1) % count
        start, end = coords[2*i:2*i+2], coords[2*j:2*j+2]
        chord = math.dist(start, end)
        if chord <= 0:
            raise RuntimeError("回读到退化的多段线圆弧段")
        result.append((repr((start, end, bulge)), chord * (1 + bulge*bulge) / (4*abs(bulge))))
    return result


def _view_state(backend):
    # ActiveViewport can retain cached properties after an application zoom.
    # Query the actual viewport system variables used by AutoCAD instead.
    result = {}
    for name in ("VIEWCTR", "VIEWSIZE", "VIEWDIR", "TARGET", "VIEWTWIST"):
        value = backend.doc.GetVariable(name)
        result[name] = [float(v) for v in value] if isinstance(value, (tuple, list)) else float(value)
    return result


def _same_view(first, second):
    return all(len(first[key]) == len(second[key]) and
               all(math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-7)
                   for a, b in zip(first[key], second[key])) if isinstance(first[key], list)
               else math.isclose(first[key], second[key], rel_tol=1e-10, abs_tol=1e-7)
               for key in first)


def _point_variant(values):
    import pythoncom
    import win32com.client
    return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, tuple(values))


def _restore_view(backend, saved):
    if _same_view(_view_state(backend), saved):
        return
    backend.acad.ZoomPrevious()
    backend._wait_operation_ready()
    if not _same_view(_view_state(backend), saved):
        raise RuntimeError("圆角临时视图恢复后回读不一致")


def fillet_acad(backend, **arguments):
    from cad_mcp.core.semantics import TRANSACTION_BACKEND
    outer_transaction = TRANSACTION_BACKEND.get() is backend
    params = FilletInput.model_validate(arguments)
    backend._check_doc()
    entities = _model_entities(backend)
    unique = list(dict.fromkeys(params.handles))
    if any(handle not in entities for handle in unique):
        raise ValueError("圆角句柄不存在于当前模型空间")
    before = {handle: _snapshot(entities[handle]) for handle in unique}
    z = next(iter(before.values()))["elevation"]
    for state in before.values():
        if bool(backend.doc.Layers.Item(state["layer"]).Lock):
            raise ValueError(f"图层已锁定: {state['layer']}")
        if not math.isclose(state["elevation"], z, rel_tol=0, abs_tol=1e-8):
            raise ValueError("圆角图元必须处于同一标高")
    if params.mode == "polyline" and next(iter(before.values()))["type"] != "LWPOLYLINE":
        raise ValueError("polyline 模式需要 LWPOLYLINE")
    if len(unique) != len(params.handles) and before[unique[0]]["type"] != "LWPOLYLINE":
        raise ValueError("只能对同一多段线的不同线段重复指定句柄")
    if params.radius == 0 and any(s["type"] not in {"LINE", "LWPOLYLINE"} for s in before.values()):
        raise ValueError("半径为零的尖角操作仅用于直线或多段线线段")
    if params.pick_points and any(not math.isclose(p[2], z, rel_tol=0, abs_tol=1e-8) for p in params.pick_points):
        raise ValueError("拾取位置必须处于图元所在标高")
    if int(backend.doc.GetVariable("TILEMODE")) != 1:
        raise ValueError("请先切换到模型选项卡执行圆角")

    result = {"mode": params.mode, "handles": params.handles, "radius": params.radius,
              "trim": params.trim, "dry_run": params.dry_run, "before": before,
              "applied": False, "verified": False,
              "preview": "预检通过；可行性、实际生成的圆角及跳过的短边由 AutoCAD 执行后回读确定"}
    if params.dry_run:
        return result

    # Read every setting before writes. Restore them even after native errors.
    settings = {name: backend.doc.GetVariable(name)
                for name in ("FILLETRAD", "TRIMMODE", "OSMODE", "CMDECHO", "USERS5")}
    pair_mode = params.mode == "pair"
    point_selection = pair_mode and any(s["type"] != "LINE" for s in before.values())
    # Native FILLET also needs a usable view for ename/pick pairs of lines.
    saved_view = _view_state(backend) if pair_mode else None
    if outer_transaction:
        backend._native_rollback_probe = lambda: (
            all(backend.doc.GetVariable(name) == value for name, value in settings.items())
            and (saved_view is None or _same_view(_view_state(backend), saved_view)))
    marker = "cadmcp-fillet-" + uuid.uuid4().hex
    tokens = ['"_.FILLET"']
    if params.mode == "polyline":
        tokens += ['"_Polyline"', f'(handent "{int(unique[0]):X}")']
    else:
        for handle, point in zip(params.handles, params.pick_points):
            xyz = " ".join(format(v, ".17g") for v in point)
            tokens.append(f'(list (handent "{int(handle):X}") (trans (list {xyz}) 0 1))')
    command = (
        '((lambda (/ result) (setq result (vl-catch-all-apply \'command-s (list '
        + " ".join(tokens) + '))) (setvar "USERS5" (if (vl-catch-all-error-p result) '
        + f'"{marker}:error" "{marker}:done")) (princ)))\n'
    )
    if point_selection:
        points = []
        for index, point in enumerate(params.pick_points, 1):
            wcs_point = "(list " + " ".join(format(v, ".17g") for v in point) + ")"
            # Keep exact picks for repeated polyline handles so a pick on a
            # different entity is rejected. For separate curve entities the
            # native closest point gives an on-curve pick and retains its side.
            if len(unique) > 1:
                wcs_point = (f"(vlax-curve-getClosestPointTo (vlax-ename->vla-object e{index}) "
                             + wcs_point + ")")
            points.append(f"(trans {wcs_point} 0 1)")
        # Passing the same ename twice makes FILLET select the whole polyline
        # twice, ignoring its segments. Native point selections retain segment
        # context; nentselp checks their entity identities before any FILLET.
        load_com = "(vl-load-com) " if len(unique) > 1 else ""
        command = (
            '((lambda (/ result p1 p2 e1 e2 selected1 selected2) ' + load_com
            + f'(setq e1 (handent "{int(params.handles[0]):X}") e2 (handent "{int(params.handles[1]):X}") '
            f'p1 {points[0]} p2 {points[1]} '
            'selected1 (nentselp "" p1) selected2 (nentselp "" p2)) '
            '(if (and selected1 selected2 (eq (car selected1) e1) (eq (car selected2) e2)) '
            '(progn (setq result (vl-catch-all-apply \'command-s (list "_.FILLET" p1 p2))) '
            f'(setvar "USERS5" (if (vl-catch-all-error-p result) "{marker}:error" "{marker}:done"))) '
            f'(setvar "USERS5" "{marker}:selection_mismatch")) (princ)))\n'
        )
    started = True if outer_transaction else backend._begin_undo()
    if not started:
        raise RuntimeError("无法建立独立圆角撤销组，已停止写入")
    restored = False

    def restore():
        errors = []
        for name, value in settings.items():
            try:
                backend.doc.SetVariable(name, value)
                if backend.doc.GetVariable(name) != value:
                    raise RuntimeError("回读不一致")
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        if saved_view is not None:
            try:
                _restore_view(backend, saved_view)
            except Exception as exc:
                errors.append(f"视图: {exc}")
        if errors:
            raise RuntimeError("圆角系统变量恢复失败: " + "; ".join(errors))

    def unchanged():
        current = _model_entities(backend)
        return (set(current) == set(entities)
                and all(_snapshot(current[h]) == before[h] for h in unique))

    try:
        for name, value in {"FILLETRAD": params.radius, "TRIMMODE": int(params.trim),
                            "OSMODE": 0, "CMDECHO": 0, "USERS5": marker + ":pending"}.items():
            backend.doc.SetVariable(name, value)
        if pair_mode:
            points = params.pick_points
            span = max(1.0, *(abs(points[0][axis] - points[1][axis]) for axis in (0, 1)))
            low = [min(p[axis] for p in points) - span * 0.25 for axis in (0, 1)] + [z]
            high = [max(p[axis] for p in points) + span * 0.25 for axis in (0, 1)] + [z]
            backend.acad.ZoomWindow(_point_variant(low), _point_variant(high))
        backend._send_command_and_wait(command)
        if backend.doc.GetVariable("USERS5") == marker + ":selection_mismatch":
            raise ValueError("拾取点未命中指定图元，请选择清晰的线段位置")
        if backend.doc.GetVariable("USERS5") != marker + ":done":
            raise RuntimeError("AutoCAD 原生 FILLET 未返回本次调用的完成标记")
        current = _model_entities(backend)
        added = set(current) - set(entities)
        removed = set(entities) - set(current)
        if removed - set(unique):
            raise RuntimeError("圆角删除了选择范围外的图元")
        after = {h: _snapshot(current[h]) for h in unique if h in current}
        created = [_snapshot(current[h]) for h in sorted(added, key=int)]
        changed = [h for h in after if after[h] != before[h]]
        if not (added or removed or changed):
            raise RuntimeError("FILLET 未改变图形；请检查半径、拾取位置和线段长度")
        output = [*after.values(), *created]
        if any(not math.isclose(s["elevation"], z, rel_tol=0, abs_tol=1e-8) for s in output):
            raise RuntimeError("圆角结果改变了原标高")
        prior_arcs = {key for state in before.values() for key, _ in _arcs(state)}
        rounded = [radius for state in output
                   if state["type"] != "ARC" or state["handle"] in added
                   for key, radius in _arcs(state) if key not in prior_arcs]
        matched = sum(math.isclose(r, params.radius, rel_tol=1e-7, abs_tol=1e-7) for r in rounded)
        if params.radius > 0 and not matched:
            raise RuntimeError("无法回读验证指定半径的圆角")
        if params.radius == 0:
            if rounded:
                raise RuntimeError("零半径圆角仍存在新圆弧段")
            if params.mode == "polyline" and any(_arcs(s) for s in output):
                raise RuntimeError("零半径整体圆角仍存在未消除的圆弧段")
            if len(after) == 2 and all(s["type"] == "LINE" for s in after.values()):
                first, second = after.values()
                if min(math.dist(first[a], second[b]) for a in ("start", "end")
                       for b in ("start", "end")) > 1e-7:
                    raise RuntimeError("零半径圆角的两条直线端点未连接")
        restore()
        restored = True
        if not outer_transaction:
            backend._commit_undo(started)
        return {**result, "applied": True, "verified": True, "after": after,
                "created": created, "modified_handles": changed, "removed_handles": sorted(removed, key=int),
                "rounded_arc_count": matched,
                "verification": "已回读实际变化、标高和圆角半径；整体多段线可能跳过容不下半径的短边"}
    except Exception as exc:
        if outer_transaction:
            if not restored:
                restore()
            raise
        # A rejected/no-op FILLET must not U the preceding user's operation.
        try:
            no_change = unchanged()
        except Exception:
            no_change = False
        if no_change:
            backend._commit_undo(started)
            exc.rollback_status = "not_needed"
        else:
            rolled_back = backend._rollback_undo(started)
            try:
                verified = rolled_back is True and unchanged()
            except Exception:
                verified = False
            exc.rollback_status = "succeeded" if verified else "failed_or_unverified"
        if not restored:
            try:
                restore()
            except Exception as restore_error:
                failure = RuntimeError(f"{exc}；{restore_error}")
                failure.rollback_status = "failed_or_unverified"
                raise failure from exc
        raise
