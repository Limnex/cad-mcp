"""Native, bounded EXTEND with zero-write preflight and verified readback."""

from __future__ import annotations

import math
from pathlib import Path
import re
import tempfile

from cad_mcp.core.runtime import CadMcpError


TOL = 1e-7
KINDS = {"AcDbLine": "LINE", "AcDbArc": "ARC", "AcDbCircle": "CIRCLE",
         "AcDbPolyline": "LWPOLYLINE"}
SETTINGS = {"CMDECHO": 0, "OSMODE": 0, "PICKFIRST": 0,
            "TRIMEXTENDMODE": 0, "PROJMODE": 0, "EDGEMODE": 0}


def set_short_variable(document, name, value):
    """Send the exact VT_I2 required by these AutoCAD integer variables."""
    if name not in SETTINGS:
        raise ValueError("仅允许恢复延伸工具使用的系统变量")
    if type(value) is not int or not -32768 <= value <= 32767:
        raise ValueError("系统变量值必须是 16 位有符号整数")
    import pythoncom
    from win32com.client import VARIANT
    document.SetVariable(name, VARIANT(pythoncom.VT_I2, value))
    if int(document.GetVariable(name)) != value:
        raise RuntimeError(f"系统变量 {name} 写后回读不一致")


def validate_request(subject_handle, boundary_handles, endpoint, extend_edges, dry_run):
    def handle(value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,40}", value) or int(value) <= 0:
            raise ValueError("句柄必须是正整数的十进制字符串")
        return str(int(value))

    subject = handle(subject_handle)
    if not isinstance(boundary_handles, list) or not 1 <= len(boundary_handles) <= 100:
        raise ValueError("boundary_handles 必须包含 1–100 个边界句柄")
    boundaries = [handle(item) for item in boundary_handles]
    if len(set(boundaries)) != len(boundaries) or subject in boundaries:
        raise ValueError("边界不能重复，也不能包含目标实体本身")
    if endpoint not in {"start", "end"}:
        raise ValueError("endpoint 必须是 start 或 end")
    if type(extend_edges) is not bool or type(dry_run) is not bool:
        raise ValueError("extend_edges 和 dry_run 必须是布尔值")
    return subject, boundaries


def _numbers(values):
    result = [float(value) for value in values]
    if not all(math.isfinite(value) for value in result):
        raise ValueError("实体包含非有限数值")
    return result


def _same(left, right):
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_same(left[key], right[key]) for key in left)
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(left, right, rel_tol=0, abs_tol=TOL)
    return left == right


def _resolve(backend, handle):
    # Direct handle lookup avoids scanning the whole model space for every edge.
    try:
        entity = backend.doc.HandleToObject(format(int(handle), "X"))
    except Exception as exc:
        # Transport errors must keep their original classification.
        from cad_mcp.drawing.autocad import _raise_scan_transport_error
        _raise_scan_transport_error(exc)
        raise CadMcpError(f"未找到实体 {handle}", code="NOT_FOUND",
                          hint="重新查询实体句柄") from exc
    if int(entity.OwnerID) != int(backend.msp.ObjectID):
        raise ValueError("仅支持模型空间实体")
    if int(str(entity.Handle), 16) != int(handle):
        raise RuntimeError("AutoCAD 句柄回读不一致")
    return entity


def _snapshot(entity):
    kind = KINDS.get(str(entity.ObjectName))
    if kind is None:
        raise ValueError("实体类型须为 LINE、ARC、CIRCLE 或 LWPOLYLINE")
    result = {"handle": str(int(str(entity.Handle), 16)), "type": kind,
              "layer": str(entity.Layer)}
    if kind == "LINE":
        result.update(start=_numbers(entity.StartPoint), end=_numbers(entity.EndPoint),
                      length=float(entity.Length))
        if not _same(result["start"][2], result["end"][2]):
            raise ValueError("延伸仅处理平行于 XY 平面的实体")
        result["elevation"] = result["start"][2]
    else:
        normal = _numbers(entity.Normal)
        if not _same(normal, [0, 0, 1]):
            raise ValueError("延伸仅处理法向为 WCS +Z 的平面实体")
        result["normal"] = normal
        if kind == "LWPOLYLINE":
            coords = _numbers(entity.Coordinates)
            if len(coords) < 4 or len(coords) % 2 or len(coords) > 2000:
                raise ValueError("多段线须有 2–1000 个顶点")
            vertices = []
            for i in range(len(coords) // 2):
                widths = _numbers(entity.GetWidth(i))
                vertices.append([*coords[2*i:2*i+2], float(entity.GetBulge(i)), *widths])
            elevation = float(entity.Elevation)
            result.update(vertices=vertices, closed=bool(entity.Closed), elevation=elevation,
                          start=[*coords[:2], elevation], end=[*coords[-2:], elevation],
                          length=float(entity.Length))
        else:
            center = _numbers(entity.Center)
            radius = float(entity.Radius)
            result.update(center=center, radius=radius, elevation=center[2])
            if kind == "ARC":
                result.update(start=_numbers(entity.StartPoint), end=_numbers(entity.EndPoint),
                              start_angle=float(entity.StartAngle), end_angle=float(entity.EndAngle),
                              length=float(entity.ArcLength))
            else:
                result["length"] = math.tau * radius
    # Includes polyline bulges/widths, elevations, lengths and radii.
    def finite(value):
        if isinstance(value, dict):
            return all(finite(item) for item in value.values())
        if isinstance(value, list):
            return all(finite(item) for item in value)
        return not isinstance(value, (int, float)) or math.isfinite(value)
    if not finite(result) or result["length"] <= TOL:
        raise ValueError("实体几何退化或包含非有限数值")
    return result


VIEW_VARIABLES = ("VIEWCTR", "VIEWSIZE", "VIEWDIR", "VIEWTWIST", "UCSORG", "UCSXDIR", "UCSYDIR")


def _view_snapshot(document):
    return {name: document.GetVariable(name) for name in VIEW_VARIABLES}


def _same_view(before, after):
    def equal(a, b):
        if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
            return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
        return math.isclose(float(a), float(b), rel_tol=1e-12, abs_tol=TOL)
    return before.keys() == after.keys() and all(equal(before[key], after[key]) for key in before)


def _command(subject, boundaries, endpoint, extend_edges, receipt):
    from cad_mcp.drawing.autocad import _lisp_string
    handles = " ".join(f'"{int(h):X}"' for h in boundaries)
    settings = {**SETTINGS, "EDGEMODE": int(extend_edges)}
    names = " ".join(f'"{name}"' for name in settings)
    setters = " ".join(f'(setvar "{name}" {value})' for name, value in settings.items())
    pick_param = "(+ a d)" if endpoint == "start" else "(- z d)"
    # All identifiers are validated decimal handles; only the generated receipt
    # path is quoted. Local variables leave no global AutoLISP symbols behind.
    return (
        "((lambda (/ saved ss e a z d p w r restore_result f h pair app doc vp vc vh vw vt vd) (vl-load-com) "
        "(setq app (vlax-get-acad-object) doc (vla-get-ActiveDocument app) "
        "vp (vla-get-ActiveViewport doc) vc (vla-get-Center vp) "
        "vh (vla-get-Height vp) vw (vla-get-Width vp) "
        "vt (vla-get-Target vp) vd (vla-get-Direction vp)) "
        f"(setq saved (mapcar '(lambda (n) (list n (getvar n))) '({names}))) "
        "(setq r (vl-catch-all-apply '(lambda () "
        f'{setters} (setq ss (ssadd) e (handent "{int(subject):X}")) '
        f"(foreach h '({handles}) (ssadd (handent h) ss)) "
        "(if (or (null e) (/= (sslength ss) " + str(len(boundaries)) + ")) "
        '(error "Invalid EXTEND selection")) '
        "(setq a (vlax-curve-getStartParam e) z (vlax-curve-getEndParam e) "
        "d (* 0.05 (min 1.0 (- z a))) "
        f"w (vlax-curve-getPointAtParam e {pick_param}) p (trans w 0 1)) "
        # Explicit entity selection still uses a screen pick to choose the end.
        # Focus the chosen terminal segment; unrelated extents must not shrink it.
        f"(vla-ZoomCenter app (vlax-3d-point w) (max 1e-6 (* 4.0 (distance w (vlax-curve-getPointAtParam e {'a' if endpoint == 'start' else 'z'}))))) "
        '(command-s "_.EXTEND" ss "" (list e p) "")) nil)) '
        # A receipt is written only after restoring the user's settings. It
        # prevents an idle but not-yet-started SendCommand from looking complete.
        "(setq restore_result (vl-catch-all-apply '(lambda () "
        "(vla-put-Target vp vt) (vla-put-Direction vp vd) "
        "(vla-put-Center vp vc) (vla-put-Height vp vh) (vla-put-Width vp vw) "
        "(vla-put-ActiveViewport doc vp)) nil)) "
        "(foreach pair saved (setvar (car pair) (cadr pair))) "
        f"(setq f (open {_lisp_string(str(receipt))} \"w\")) "
        '(write-line (if (or (vl-catch-all-error-p r) (vl-catch-all-error-p restore_result)) "ERROR" "OK") f) '
        "(close f) (princ)))\n"
    )


def _verify(before, after, endpoint, subject, boundaries, extend_edges):
    other = "end" if endpoint == "start" else "start"
    if (before["type"] != after["type"] or before["handle"] != after["handle"]
            or before["layer"] != after["layer"] or not _same(before[other], after[other])
            or not _same(before["elevation"], after["elevation"])):
        raise RuntimeError("延伸回读改变了类型、句柄、图层、标高或另一端")
    if _same(before[endpoint], after[endpoint]) or after["length"] <= before["length"] + TOL:
        raise CadMcpError("原生 EXTEND 未产生有效延伸", code="NO_GEOMETRY_CHANGE",
                          hint="核对所选端点和边界是否能相交")
    if not _same(before.get("normal"), after.get("normal")):
        raise RuntimeError("延伸回读改变了实体法向")
    if before["type"] == "LINE":
        direction = [a - b for a, b in zip(before[endpoint], before[other])]
        movement = [a - b for a, b in zip(after[endpoint], before[endpoint])]
        dot = sum(a*b for a, b in zip(direction, movement))
        cross = direction[0]*movement[1] - direction[1]*movement[0]
        if dot <= 0 or abs(cross) / before["length"] > TOL:
            raise RuntimeError("延伸回读改变了直线方向")
    if before["type"] == "ARC":
        if not _same(before["center"], after["center"]) or not _same(before["radius"], after["radius"]):
            raise RuntimeError("延伸回读改变了圆弧圆心或半径")
    if before["type"] == "LWPOLYLINE":
        old, new = before["vertices"], after["vertices"]
        if len(old) != len(new) or after["closed"]:
            raise RuntimeError("延伸回读改变了多段线顶点数或闭合状态")
        for i, (a, b) in enumerate(zip(old, new)):
            # Native extension of an arc end segment may update its bulge.
            moving = i == (0 if endpoint == "start" else len(old) - 1)
            arc_segment = i == (0 if endpoint == "start" else len(old) - 2)
            if ((not moving and not _same(a[:2], b[:2]))
                    or (not arc_segment and not _same(a[2], b[2])) or not _same(a[3:], b[3:])):
                raise RuntimeError("延伸回读改变了无关顶点或多段线线宽")
    # Verify the new endpoint actually meets one of the specified edges; existing
    # intersections elsewhere on the subject cannot satisfy this check.
    point = after[endpoint]
    for boundary in boundaries:
        values = _numbers(subject.IntersectWith(boundary, 2 if extend_edges else 0) or ())
        if len(values) % 3:
            raise RuntimeError("AutoCAD 返回了无效的交点坐标")
        if any(math.dist(point, values[i:i+3]) <= TOL for i in range(0, len(values), 3)):
            return
    raise RuntimeError("延伸端点未与指定边界相交，回读验证失败")


def extend_native(backend, subject_handle, boundary_handles, endpoint, *, extend_edges=False, dry_run=True):
    subject_handle, boundary_handles = validate_request(
        subject_handle, boundary_handles, endpoint, extend_edges, dry_run)
    backend._check_doc()
    if int(backend.doc.GetVariable("TILEMODE")) != 1:
        raise ValueError("请先切换到模型选项卡再执行延伸")
    handles = [subject_handle, *boundary_handles]
    entities = [_resolve(backend, h) for h in handles]
    snapshots = [_snapshot(e) for e in entities]
    before = snapshots[0]
    if before["type"] == "CIRCLE" or before.get("closed"):
        raise ValueError("目标须为 LINE、ARC 或开放 LWPOLYLINE")
    if before["type"] == "LWPOLYLINE":
        points = before["vertices"]
        if _same(points[0][:2], points[1][:2]) or _same(points[-2][:2], points[-1][:2]) or _same(before["start"], before["end"]):
            raise ValueError("目标多段线首尾段不能退化或首尾重合")
    if bool(backend.doc.Layers.Item(before["layer"]).Lock):
        raise ValueError("目标所在图层已锁定")
    if any(not _same(s["elevation"], before["elevation"]) for s in snapshots[1:]):
        raise ValueError("目标和边界须在同一 XY 平面，不进行投影延伸")
    saved = {name: int(backend.doc.GetVariable(name)) for name in SETTINGS}
    result = {"handle": subject_handle, "type": before["type"], "boundary_handles": boundary_handles,
              "endpoint": endpoint, "extend_edges": extend_edges, "dry_run": dry_run,
              "before": before, "after": before, "applied": False, "verified": False,
              "preflight_passed": True}
    if dry_run:
        return result  # Preflight does not predict intersections or claim success.

    with tempfile.TemporaryDirectory(prefix="cad-mcp-extend-", ignore_cleanup_errors=True) as directory:
        receipt = Path(directory) / "complete.txt"
        command = _command(subject_handle, boundary_handles, endpoint, extend_edges, receipt)
        saved_view = _view_snapshot(backend.doc)
        # Only this short loader enters the command line. Up to 100 boundaries
        # and long receipt paths cannot overflow AutoCAD's command input buffer.
        script = Path(directory) / "extend.lsp"
        script.write_text(command, encoding="utf-8")
        from cad_mcp.drawing.autocad import _lisp_string
        loader = f'(load {_lisp_string(str(script))})\n'
        started = backend._begin_undo()
        if not started:
            raise RuntimeError("无法建立独立撤销组，已停止延伸")
        completed = False
        try:
            backend._send_command_and_wait(loader, completion_check=receipt.is_file)
            completed = True
            if receipt.read_text(encoding="ascii").strip() != "OK":
                raise RuntimeError("AutoCAD 原生 EXTEND 命令失败")
            if any(int(backend.doc.GetVariable(name)) != value for name, value in saved.items()):
                raise RuntimeError("延伸后系统变量未恢复")
            if not _same_view(saved_view, _view_snapshot(backend.doc)):
                raise RuntimeError("延伸后视图或坐标系未恢复")
            after_entities = [_resolve(backend, h) for h in handles]
            after_snapshots = [_snapshot(e) for e in after_entities]
            if not _same(snapshots[1:], after_snapshots[1:]):
                raise RuntimeError("延伸改变了指定边界，回读验证失败")
            _verify(before, after_snapshots[0], endpoint, after_entities[0], after_entities[1:], extend_edges)
            backend._commit_undo(started)
            return {**result, "after": after_snapshots[0], "applied": True, "verified": True}
        except Exception as exc:
            if not completed:
                # The native command may still be queued/running. Never issue U
                # or restore variables concurrently, and never replay the write.
                raise CadMcpError("原生延伸执行结果未知，已停止后续写入", code="EXECUTION_UNKNOWN",
                                  hint="人工核对 CAD 后重启 MCP；不能认定已回滚",
                                  details={"rollback_status": "not_verified"}) from exc
            try:
                for name, value in saved.items():
                    if int(backend.doc.GetVariable(name)) != value:
                        set_short_variable(backend.doc, name, value)
                current = [_snapshot(_resolve(backend, h)) for h in handles]
                if _same(snapshots, current):
                    # Empty undo groups must not undo the preceding operation.
                    backend._commit_undo(started)
                    exc.rollback_status = "not_needed"
                else:
                    rolled_back = backend._rollback_undo(started)
                    restored = [_snapshot(_resolve(backend, h)) for h in handles]
                    exc.rollback_status = ("succeeded" if rolled_back is True and _same(snapshots, restored)
                                           else "failed_or_unverified")
                if (any(int(backend.doc.GetVariable(name)) != value for name, value in saved.items())
                        or not _same_view(saved_view, _view_snapshot(backend.doc))):
                    exc.rollback_status = "failed_or_unverified"
            except Exception:
                exc.rollback_status = "failed_or_unverified"
            if exc.rollback_status == "failed_or_unverified":
                raise CadMcpError("原生延伸失败且回滚无法核实，已停止后续写入", code="EXECUTION_UNKNOWN",
                                  hint="人工核对 CAD 后重启 MCP",
                                  details={"rollback_status": exc.rollback_status}) from exc
            raise
