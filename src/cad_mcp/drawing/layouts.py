"""AutoCAD sheet composition, with native independent viewport readback.

Preview/check may write a temporary receipt file, but never change the drawing,
activate a layout, set system variables, or create temporary plot configurations.
The native reader uses only entget; no entmod, command-s or USERS variables.
"""

from __future__ import annotations

import math
from pathlib import Path
import re
import tempfile
from contextlib import nullcontext

import pythoncom
import win32com.client

from cad_mcp.dwt.autocad import _STANDARD_SCALE
from cad_mcp.core.geometry import MAX_DRAWING_SCAN_ENTITIES
from cad_mcp.drawing.com_read import bounding_box, out_pair
from cad_mcp.core.registry import pack_metadata, unpack_metadata
from cad_mcp.core.runtime import ConflictError
from cad_mcp.engineering.layouts import (
    LayoutRequest, VIEWPORT_LAYER, close_values, confirmation, contains,
    fingerprint, overlaps, paper_geometry, plan_sheet, verify_confirmation,
)
from cad_mcp.engineering.templates import load_template_manifest, verify_template_source


LAYOUT_APP = "CAD_MCP_LAYOUT_V1"


def point(values):
    return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(values))


def lisp_string(value):
    if any(ord(c) < 32 for c in value):
        raise ValueError("AutoLISP 文件路径不能含控制字符")
    return '"'+value.replace('\\', '/').replace('"', '\\"')+'"'


def viewport_reader(handles, receipt):
    """Local AutoLISP variables, exact handles, numeric receipt, no DB writes."""
    hex_handles = []
    for handle in handles:
        if not re.fullmatch(r"[0-9A-Fa-f]{1,40}", str(handle)):
            raise ValueError("视口句柄无效")
        hex_handles.append('"'+str(handle).upper()+'"')
    return (
        '((lambda (/ f d h values result frozen v p) '
        f'(setq f (open {lisp_string(str(receipt))} "w")) '
        '(if f (progn '
        '(setq result (vl-catch-all-apply (function (lambda () '
        f"(foreach h '({' '.join(hex_handles)}) "
        '(setq d (entget (handent h))) '
        '(if (/= (cdr (assoc 0 d)) "VIEWPORT") (/ 1 0)) '
        '(setq values (append '
        '(list (car (cdr (assoc 10 d))) (cadr (cdr (assoc 10 d))) '
        '(cdr (assoc 40 d)) (cdr (assoc 41 d)) '
        '(car (cdr (assoc 12 d))) (cadr (cdr (assoc 12 d)))) '
        '(cdr (assoc 16 d)) (cdr (assoc 17 d)) '
        '(list (cdr (assoc 45 d)) (cdr (assoc 51 d)) '
        '(cdr (assoc 68 d)) (cdr (assoc 69 d)) (cdr (assoc 90 d))))) '
        '(if (/= (length values) 17) (/ 1 0)) '
        '(write-line h f) '
        '(foreach v values (write-line (rtos v 2 16) f)) '
        '(setq frozen (vl-remove-if-not (function (lambda (p) (= (car p) 331))) d)) '
        '(write-line (itoa (length frozen)) f) '
        '(foreach p frozen (write-line (cdr (assoc 5 (entget (cdr p)))) f)) '
        '))) nil)) '
        '(write-line (if (vl-catch-all-error-p result) "ERROR" "DONE") f) '
        '(close f))) (princ)))\n'
    )


def parse_viewports(text, handles):
    lines = text.strip().splitlines()
    if not lines or lines[-1] != "DONE":
        raise RuntimeError("原生视口回读未完整完成")
    rows, index = {}, 0
    try:
        for handle in handles:
            key = str(handle).upper()
            if lines[index].upper() != key:
                raise ValueError("视口句柄不一致")
            values = [float(v) for v in lines[index+1:index+18]]
            if len(values) != 17 or not all(math.isfinite(v) for v in values):
                raise ValueError("视口数据不完整或非有限")
            count = int(lines[index+18])
            if count < 0 or count > MAX_DRAWING_SCAN_ENTITIES:
                raise ValueError("冻结图层列表异常")
            frozen = lines[index+19:index+19+count]
            if len(frozen) != count or any(not re.fullmatch(r"[0-9A-Fa-f]+", h) for h in frozen):
                raise ValueError("冻结图层句柄不完整")
            rows[key] = dict(paper_center=values[0:2], paper_size=values[2:4],
                view_center_dcs=values[4:6], direction=values[6:9], target=values[9:12],
                view_height=values[12], twist=values[13], status=int(values[14]),
                number=int(values[15]), flags=int(values[16]), frozen_layers=sorted(frozen))
            index += 19+count
        if index != len(lines)-1:
            raise ValueError("视口回执包含额外数据")
    except (IndexError, ValueError) as exc:
        raise RuntimeError("原生视口回读格式不正确，不能报告定位成功") from exc
    return rows


def plot_scale_reader(handle, receipt):
    if not re.fullmatch(r"[0-9A-Fa-f]{1,40}", str(handle)):
        raise ValueError("打印设置句柄无效")
    return (
        '((lambda (/ f d result v) '
        f'(setq f (open {lisp_string(str(receipt))} "w")) '
        '(if f (progn '
        '(setq result (vl-catch-all-apply (function (lambda () '
        f'(setq d (entget (handent "{str(handle).upper()}"))) '
        "(if (not (member (cdr (assoc 0 d)) '(\"LAYOUT\" \"PLOTSETTINGS\"))) (/ 1 0)) "
        f'(write-line "{str(handle).upper()}" f) '
        '(foreach v (list (cdr (assoc 70 d)) (cdr (assoc 75 d)) (cdr (assoc 147 d))) '
        '(if (not (numberp v)) (/ 1 0)) (write-line (rtos v 2 16) f)) '
        ')) nil)) '
        '(write-line (if (vl-catch-all-error-p result) "ERROR" "DONE") f) '
        '(close f))) (princ)))\n'
    )


def parse_plot_scale(text, handle):
    lines = text.strip().splitlines()
    try:
        if len(lines) != 5 or lines[0].upper() != str(handle).upper() or lines[-1] != "DONE":
            raise ValueError("打印倍率回执不完整")
        flags, standard, factor = map(float, lines[1:4])
        if (not all(math.isfinite(v) for v in (flags,standard,factor)) or
                abs(flags-round(flags)) > 1e-7 or abs(standard-round(standard)) > 1e-7 or min(flags,standard) < 0 or factor <= 0):
            raise ValueError("打印倍率回执无效")
    except (TypeError,ValueError) as exc:
        raise RuntimeError(f"无法独立确认布局的实际打印倍率，原始回执: {lines!r}") from exc
    return {"flags":int(round(flags)),"standard_scale_code":int(round(standard)),"standard_scale_factor":factor}


def verify_viewport(actual, desired, locked=True):
    """Use DXF view-center + target, not COM Target or cached ActiveViewport."""
    length = math.sqrt(sum(v*v for v in actual["direction"]))
    direction = [v/length for v in actual["direction"]] if length else []
    if not close_values(direction, [0,0,1], 1e-8) or abs(actual["twist"]) > 1e-8:
        raise RuntimeError("视口不是无旋转的 WCS XY 俯视图")
    if actual["flags"] & (1 | 2 | 4 | 65536 | 131072) or actual["status"] == 0:
        raise RuntimeError("视口处于透视、裁切或关闭状态")
    if actual["frozen_layers"]:
        raise RuntimeError("视口含冻结图层，无法保证目标内容完整显示")
    center = [actual["target"][0]+actual["view_center_dcs"][0],
              actual["target"][1]+actual["view_center_dcs"][1], actual["target"][2]]
    expected = desired["model_center"]
    # Model tolerance is one micron on paper, with a conservative floating floor.
    tolerance = max(1e-7, 0.001/desired["custom_scale"])
    if not close_values(center, expected, tolerance):
        raise RuntimeError(f"视口实际模型中心 {center} 与目标 {expected} 不一致")
    if not close_values(actual["paper_center"], desired["paper_center"]):
        raise RuntimeError("视口纸面中心回读不一致")
    if not close_values(actual["paper_size"], desired["paper_size"]):
        raise RuntimeError("视口纸面尺寸回读不一致")
    if actual["view_height"] <= 0:
        raise RuntimeError("视口实际视图高度无效")
    scale = actual["paper_size"][1]/actual["view_height"]
    if not math.isclose(scale, desired["custom_scale"], abs_tol=1e-12, rel_tol=1e-7):
        raise RuntimeError("视口实际比例回读不一致")
    if bool(actual["flags"] & 16384) != bool(locked):
        raise RuntimeError("视口锁定状态回读不一致")
    return {"model_center": center, "custom_scale": scale,
            "paper_center": actual["paper_center"], "paper_size": actual["paper_size"],
            "locked": bool(actual["flags"] & 16384)}


class AcadLayoutMixin:
    def _read_native_viewports(self, entities):
        if not entities:
            return {}
        handles = [str(e.Handle).upper() for e in entities]
        previous = self.acad.ActiveDocument
        def identity(document):
            return str(document.FullName).casefold(),str(document.Name).casefold()
        changed = identity(previous) != identity(self.doc)
        try:
            if changed:
                self.doc.Activate()
            if identity(self.acad.ActiveDocument) != identity(self.doc):
                raise RuntimeError("无法激活绑定图纸进行原生视口回读")
            with tempfile.TemporaryDirectory(prefix="cad-mcp-viewport-", ignore_cleanup_errors=True) as directory:
                receipt = Path(directory)/"viewports.txt"
                script = Path(directory)/"read.lsp"
                script.write_text(viewport_reader(handles, receipt), encoding="utf-8")
                self._send_command_and_wait(f'(load {lisp_string(str(script))})\n',
                    completion_check=lambda: receipt.is_file() and
                        receipt.read_bytes().rstrip().endswith((b"DONE", b"ERROR")))
                return parse_viewports(receipt.read_text(encoding="ascii"), handles)
        finally:
            if changed:
                previous.Activate()
                if identity(self.acad.ActiveDocument) != identity(previous):
                    raise RuntimeError("原生视口回读后原活动图纸恢复失败")

    def _read_native_plot_scale(self, plot):
        handle = str(plot.Handle).upper()
        previous = self.acad.ActiveDocument
        def identity(document):
            return str(document.FullName).casefold(),str(document.Name).casefold()
        changed = identity(previous) != identity(self.doc)
        try:
            if changed:
                self.doc.Activate()
            if identity(self.acad.ActiveDocument) != identity(self.doc):
                raise RuntimeError("无法激活绑定图纸读取实际打印倍率")
            with tempfile.TemporaryDirectory(prefix="cad-mcp-plot-scale-", ignore_cleanup_errors=True) as directory:
                receipt = Path(directory)/"plot-scale.txt"
                script = Path(directory)/"read.lsp"
                script.write_text(plot_scale_reader(handle,receipt),encoding="utf-8")
                self._send_command_and_wait(f'(load {lisp_string(str(script))})\n',
                    completion_check=lambda:receipt.is_file() and
                        receipt.read_bytes().rstrip().endswith((b"DONE",b"ERROR")))
                return parse_plot_scale(receipt.read_text(encoding="ascii"),handle)
        finally:
            if changed:
                previous.Activate()
                if identity(self.acad.ActiveDocument) != identity(previous):
                    raise RuntimeError("打印倍率回读后原活动图纸恢复失败")

    def _layout_entities(self, layout):
        block = layout.Block
        if int(block.Count) > MAX_DRAWING_SCAN_ENTITIES:
            raise ValueError("布局图元过多，无法完整核验")
        result = []
        for i in range(int(block.Count)):
            self._scan_checkpoint(i)
            result.append(block.Item(i))
        return result

    def _sheet_plot_geometry(self, plot, rotation=None):
        # These COM methods have OUT arguments; generated pywin32 wrappers return
        # both outputs as tuples. Fail closed if a runtime cannot expose them.
        if int(plot.PaperUnits) != 1:
            raise ValueError("完整布局需要页面设置使用毫米纸面单位")
        size = list(out_pair(plot,"GetPaperSize",variant_type=pythoncom.VT_R8))
        lower, upper = out_pair(plot,"GetPaperMargins")
        if len(size) != 2 or len(lower) != 2 or len(upper) != 2:
            raise ValueError("无法完整读取纸张实际尺寸和打印边距")
        actual_rotation = int(plot.PlotRotation)*90 if rotation is None else rotation
        return paper_geometry(size, lower, upper, actual_rotation)

    @staticmethod
    def _same_sheet_paper(actual, expected):
        return actual["rotation"] == expected["rotation"] and all(
            close_values(actual[key],expected[key]) for key in
            ("size_mm","margins_mm","printable_bounds_mm","paper_origin_layout","plot_origin_mm"))

    def _layout_plot_state(self, layout):
        payload, missing = self._plot_payload(layout)
        if missing:
            raise ValueError("目标布局页面设置回读不完整")
        return {"plot":payload,"origin":list(layout.PlotOrigin),"paper":self._sheet_plot_geometry(layout)}

    def _refresh_sheet_plot_resources(self, setup, rotation=None):
        # Refresh only a detached ObjectDBX database. RefreshPlotDeviceInfo can
        # dirty a reopened DWG even when every visible plot setting is unchanged.
        before, missing = self._plot_payload(setup)
        if missing:
            raise ValueError("命名页面设置回读不完整")
        origin = list(setup.PlotOrigin)
        dbmod = int(self.doc.GetVariable("DBMOD"))
        version = int(str(self.acad.Version).split(".")[0])
        database = self.acad.GetInterfaceObject(f"ObjectDBX.AxDbDocument.{version}")
        probe = database.PlotConfigurations.Add("CAD_MCP_RESOURCE_PROBE", bool(setup.ModelType))
        try:
            probe.CopyFrom(setup)
            probe.RefreshPlotDeviceInfo()
            actual, missing = self._plot_payload(probe)
            differences = {key: {"expected": value, "actual": actual.get(key)}
                           for key, value in before.items() if actual.get(key) != value}
            if missing or differences or list(probe.PlotOrigin) != origin:
                raise ConflictError("独立打印资源探针与命名页面设置不一致", details={
                    "differences": differences, "unverified_fields": missing,
                    "origin_before": origin, "origin_probe": list(probe.PlotOrigin)})
            resources = {
                "devices": list(probe.GetPlotDeviceNames()),
                "media": list(probe.GetCanonicalMediaNames()),
                "styles": list(probe.GetPlotStyleTableNames()),
                "paper": self._sheet_plot_geometry(probe, rotation),
            }
        finally:
            del probe
            del database
        after, missing = self._plot_payload(setup)
        differences = {key: {"before": value, "after": after.get(key)}
                       for key, value in before.items() if after.get(key) != value}
        after_origin, after_dbmod = list(setup.PlotOrigin), int(self.doc.GetVariable("DBMOD"))
        if missing or differences or origin != after_origin or dbmod != after_dbmod:
            raise ConflictError("打印资源刷新时页面设置或图纸状态发生变化，不能报告只读预览成功", details={
                "differences": differences, "unverified_fields": missing,
                "origin_before": origin, "origin_after": after_origin,
                "dbmod_before": dbmod, "dbmod_after": after_dbmod})
        return resources

    def _layout_owner(self, entity):
        # Missing XData returns an empty array. COM read errors must propagate;
        # they must never turn an unreadable entity into an unmanaged entity.
        result = out_pair(entity,"GetXData",leading=(LAYOUT_APP,))
        chunks = self._xdata_chunks(result)
        return unpack_metadata(chunks) if chunks else None

    def _tag_layout_entity(self, entity, owner):
        apps = self.doc.RegisteredApplications
        if not any(str(apps.Item(i).Name).casefold() == LAYOUT_APP.casefold() for i in range(int(apps.Count))):
            apps.Add(LAYOUT_APP)
        chunks = pack_metadata(owner)
        entity.SetXData(
            win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_I2, [1001]+[1000]*len(chunks)),
            win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_VARIANT, [LAYOUT_APP,*chunks]))
        if self._layout_owner(entity) != owner:
            raise RuntimeError("布局归属标记回读不一致")

    def _layout_entity_state(self, entity, native):
        kind = str(entity.ObjectName)
        state = {"handle": str(entity.Handle).upper(), "kind": kind,
                 "layer": str(entity.Layer), "visible": bool(entity.Visible)}
        if kind == "AcDbViewport":
            state["viewport"] = dict(native[str(entity.Handle).upper()])
            # DXF 68 may change from 1 to -1 merely by switching layout tabs.
            state["viewport"]["status"] = int(state["viewport"]["status"] != 0)
            # Floating viewport display IDs are renumbered on DWG reopen.
            if state["viewport"]["number"] > 1:
                state["viewport"]["number"] = 2
            # DWG round-trips perturb binary doubles by a few ULPs. Quantize
            # only viewport numeric readback, far below the paper-fit tolerance.
            for key, value in state["viewport"].items():
                if isinstance(value, float):
                    state["viewport"][key] = round(value, 9)
                elif isinstance(value, list):
                    state["viewport"][key] = [round(v, 9) if isinstance(v, float) else v for v in value]
        else:
            low, high = bounding_box(entity)
            state["bounds"] = [*map(float, low),*map(float, high)]
            state["entity_hash"] = self._entity_state_hash(entity)
        if kind == "AcDbBlockReference":
            state["insertion"] = list(entity.InsertionPoint)
            state["scale"] = [float(entity.XScaleFactor),float(entity.YScaleFactor),float(entity.ZScaleFactor)]
            state["rotation"] = float(entity.Rotation)
            state["name"] = str(entity.Name)
            if bool(getattr(entity,"HasAttributes",False)):
                state["attributes"] = [{"tag":str(att.TagString),"text":str(att.TextString),
                    "point":list(att.InsertionPoint),"height":float(att.Height),"rotation":float(att.Rotation)}
                    for att in entity.GetAttributes()]
        return state

    @staticmethod
    def _layout_state_matches(state, native, owner):
        if owner.get("state_version") == 2 or state["kind"] != "AcDbViewport":
            return owner.get("state_hash") == fingerprint(state)
        # Legacy metadata included a transient viewport number. Reconstruct only
        # the original snapshot, preserving every geometry/flag value exactly.
        # A newly created viewport followed a deleted default viewport (ID 2),
        # so its pre-save ID can be one larger than the compacted ID on reopen.
        legacy = {**state, "viewport": dict(native[state["handle"]])}
        legacy["viewport"]["status"] = int(legacy["viewport"]["status"] != 0)
        number = int(legacy["viewport"]["number"])
        for candidate in (number, number + 1):
            legacy["viewport"]["number"] = candidate
            if owner.get("state_hash") == fingerprint(legacy):
                return True
        return False

    def _block_dependency_state(self, block_name):
        states, seen, count = [], set(), 0
        def visit(name, depth):
            nonlocal count
            if name in seen:
                return
            if depth > 16:
                raise ValueError("图框块嵌套超过16层，无法完整核验")
            seen.add(name)
            block = self.doc.Blocks.Item(name)
            states.append({"name": name, "origin": list(block.Origin)})
            for i in range(int(block.Count)):
                count += 1
                if count > MAX_DRAWING_SCAN_ENTITIES:
                    raise ValueError("图框块图元过多，无法完整核验")
                self._scan_checkpoint(count)
                entity = block.Item(i)
                layer = self.doc.Layers.Item(str(entity.Layer))
                states.append([self._entity_state_hash(entity),bool(entity.Visible),
                               bool(layer.LayerOn),bool(layer.Freeze),bool(layer.Plottable)])
                if str(entity.ObjectName) == "AcDbBlockReference":
                    visit(str(entity.Name), depth+1)
        visit(block_name, 0)
        return states

    def _block_printed_bounds(self, block_name):
        """Read actual printable geometry, excluding nonplot paper references.

        Nested inserts are transformed from their block-definition coordinates;
        layer 0 inherits the insertion layer. No template geometry is modified.
        """
        boxes, count = [], 0
        def visit(name,matrix,inherited_layer,depth,path):
            nonlocal count
            if depth > 16 or name in path:
                raise ValueError("图框块嵌套循环或超过16层")
            block = self.doc.Blocks.Item(name)
            a,b,c,d,tx,ty = matrix
            for i in range(int(block.Count)):
                count += 1
                if count > MAX_DRAWING_SCAN_ENTITIES:
                    raise ValueError("图框打印几何超出完整扫描上限")
                self._scan_checkpoint(count)
                entity = block.Item(i)
                layer_name = str(entity.Layer)
                if layer_name == "0":
                    layer_name = inherited_layer
                layer = self.doc.Layers.Item(layer_name)
                if not entity.Visible or not layer.LayerOn or layer.Freeze or not layer.Plottable:
                    continue
                kind = str(entity.ObjectName)
                if kind == "AcDbAttributeDefinition" and entity.Invisible:
                    continue
                if kind == "AcDbBlockReference":
                    normal = list(entity.Normal)
                    if not close_values(normal,[0,0,1],1e-8):
                        raise ValueError("图框嵌套块必须位于 WCS XY 平面")
                    child = self.doc.Blocks.Item(str(entity.Name))
                    origin = child.Origin
                    angle = float(entity.Rotation)
                    ca,sa = math.cos(angle),math.sin(angle)
                    aa,bb = float(entity.XScaleFactor)*ca,float(entity.XScaleFactor)*sa
                    cc,dd = -float(entity.YScaleFactor)*sa,float(entity.YScaleFactor)*ca
                    x,y = entity.InsertionPoint[:2]
                    x,y = x-aa*origin[0]-cc*origin[1],y-bb*origin[0]-dd*origin[1]
                    visit(str(entity.Name),[a*aa+c*bb,b*aa+d*bb,a*cc+c*dd,b*cc+d*dd,
                          a*x+c*y+tx,b*x+d*y+ty],layer_name,depth+1,path|{name})
                else:
                    low,high = bounding_box(entity)
                    points = [(a*x+c*y+tx,b*x+d*y+ty)
                              for x in (low[0],high[0]) for y in (low[1],high[1])]
                    boxes.append([min(p[0] for p in points),min(p[1] for p in points),
                                  max(p[0] for p in points),max(p[1] for p in points)])
        visit(block_name,[1,0,0,1,0,0],"0",0,set())
        if not boxes:
            raise ValueError("图框没有可核验的可打印几何")
        return [min(b[0] for b in boxes),min(b[1] for b in boxes),
                max(b[2] for b in boxes),max(b[3] for b in boxes)]

    def _layout_content(self, request):
        if int(self.msp.Count) > MAX_DRAWING_SCAN_ENTITIES:
            raise ValueError("模型图元过多，无法完整核验布局内容")
        wanted = set(request.content_handles or [])
        found, states, boxes = set(), [], []
        for i in range(int(self.msp.Count)):
            self._scan_checkpoint(i)
            entity = self.msp.Item(i)
            handle = self._handle_str(entity.Handle)
            if wanted and handle not in wanted:
                continue
            low, high = bounding_box(entity)
            bounds = [float(low[0]),float(low[1]),float(high[0]),float(high[1])]
            if not wanted and not overlaps(bounds, request.content_bounds, tolerance=-1e-8):
                continue
            if not close_values([low[2],high[2]], [request.model_elevation]*2, 1e-7):
                raise ValueError("完整布局当前仅支持同一标高的 WCS XY 平面内容")
            layer = self.doc.Layers.Item(str(entity.Layer))
            if not bool(entity.Visible) or not bool(layer.LayerOn) or bool(layer.Freeze) or not bool(layer.Plottable):
                raise ValueError(f"目标内容 {handle} 不可见或所在图层不可打印")
            states.append([handle,self._entity_state_hash(entity),str(entity.Layer),
                           bool(layer.LayerOn),bool(layer.Freeze),bool(layer.Plottable)])
            boxes.append(bounds)
            found.add(handle)
        if wanted-found:
            raise ValueError("指定句柄不存在于模型空间: "+", ".join(sorted(wanted-found)))
        if not boxes:
            raise ValueError("所选模型范围没有可核验内容")
        bounds = request.content_bounds or [min(b[0] for b in boxes),min(b[1] for b in boxes),
                                            max(b[2] for b in boxes),max(b[3] for b in boxes)]
        return list(bounds), states

    def _prepare_sheet_layout(self, request):
        self._check_doc()
        manifest = load_template_manifest(request.manifest_path)
        target_path = self.dwt_target_identity().get("path")
        if target_path and Path(target_path).resolve() == Path(manifest.source_dwg).resolve():
            raise ConflictError("模板源图纸只读，必须先克隆工作副本，不能直接应用布局")
        verification = verify_template_source(manifest)
        if verification.get("ok") is False or verification.get("matches") is False:
            raise ConflictError("模板源文件哈希不匹配")
        sheet = next((s for s in manifest.sheets if s.id == request.sheet_id), None)
        if sheet is None:
            raise ValueError("模板不存在指定图幅")
        # Enumerate names rather than treating any COM lookup error as absence.
        layouts = {str(self.doc.Layouts.Item(i).Name).casefold(): self.doc.Layouts.Item(i)
                   for i in range(int(self.doc.Layouts.Count))}
        layout = layouts.get(request.layout_name.casefold())
        setups = {str(self.doc.PlotConfigurations.Item(i).Name).casefold(): self.doc.PlotConfigurations.Item(i)
                  for i in range(int(self.doc.PlotConfigurations.Count))}
        setup = setups.get(request.page_setup_name.casefold())
        if setup is None:
            raise ValueError("命名页面设置不存在；请先配置实际打印设备、纸张，再预览布局")
        resources = self._refresh_sheet_plot_resources(setup, request.rotation)
        plot, unverified = self._plot_payload(setup)
        if unverified:
            raise ValueError("命名页面设置回读不完整: "+", ".join(unverified))
        paper = resources["paper"]
        if not str(setup.ConfigName).strip() or str(setup.ConfigName).casefold() == "none":
            raise ValueError("命名页面设置未配置实际打印设备")
        if str(setup.ConfigName).casefold() not in {str(n).casefold() for n in resources["devices"]}:
            raise ValueError("命名页面设置所选打印设备在本机不可用")
        if str(setup.CanonicalMediaName) not in resources["media"]:
            raise ValueError("命名页面设置所选纸张在当前设备不可用")
        if str(setup.StyleSheet).strip() and str(setup.StyleSheet).casefold() not in {
                str(n).casefold() for n in resources["styles"]}:
            raise ValueError("命名页面设置所选打印样式在本机不可用")
        block = self.doc.Blocks.Item(sheet.block_name)
        block_origin = list(map(float, block.Origin))
        if len(block_origin) != 3 or not all(math.isfinite(v) for v in block_origin):
            raise ValueError("无法读取有效的图框块基点")
        content_bounds, content_states = self._layout_content(request)
        geometry = plan_sheet(request, sheet, content_bounds, paper, block_origin)
        actual_printed_bounds = self._block_printed_bounds(sheet.block_name)
        if not contains(sheet.printed_bounds,actual_printed_bounds,0.05/sheet.fixed_insert_scale):
            raise ConflictError("实际图框可打印几何超出 printed_bounds，配置不能证明边框不被裁切")
        entities = self._layout_entities(layout) if layout is not None else []
        viewports = [e for e in entities if str(e.ObjectName) == "AcDbViewport"]
        native = self._read_native_viewports(viewports)
        block_state = self._block_dependency_state(sheet.block_name)
        block_hash = fingerprint(block_state)
        rows, owned, conflicts = [], {}, []
        adopt_viewport = requested_viewport = None
        from cad_mcp.core.semantics import entity_metadata
        for entity in entities:
            state = self._layout_entity_state(entity, native)
            owner = self._layout_owner(entity)
            system_viewport = state["kind"] == "AcDbViewport" and state["viewport"]["number"] == 1
            explicit = self._handle_str(entity.Handle) == request.existing_viewport_handle
            if explicit:
                requested_viewport = entity
                if state["kind"] != "AcDbViewport" or state["viewport"]["number"] <= 1:
                    conflicts.append("指定复用对象必须是目标布局中已初始化的浮动视口")
                elif owner and (owner.get("layout_id") != request.layout_id or owner.get("role") != "viewport"):
                    conflicts.append("指定视口已有其他布局归属，不能接管")
                elif not owner:
                    if entity_metadata(self,entity):
                        conflicts.append("指定视口已有工程身份，不能自动改归属")
                    elif self.doc.Layers.Item(str(entity.Layer)).Lock:
                        conflicts.append("指定复用视口所在图层已锁定")
                    else:
                        adopt_viewport = entity
            if owner and owner.get("layout_id") == request.layout_id:
                role = owner.get("role")
                if role not in ("frame", "viewport") or role in owned:
                    conflicts.append("受管布局对象归属重复或角色无效")
                else:
                    owned[role] = entity
                if (role == "frame" and state["kind"] != "AcDbBlockReference") or (role == "viewport" and state["kind"] != "AcDbViewport"):
                    conflicts.append("受管对象角色与真实图元类型不符")
                if role == "frame" and state.get("name") != sheet.block_name:
                    conflicts.append("受管图框与目标模板块不同，不能自动替换")
                if not self._layout_state_matches(state, native, owner):
                    conflicts.append(f"受管对象 {state['handle']} 已被人工修改，请先核对")
                if owner.get("block_hash") != block_hash:
                    conflicts.append("受管图框定义已变化，请先核对")
            elif not system_viewport and not explicit:
                if state["kind"] == "AcDbViewport":
                    c, s = state["viewport"]["paper_center"], state["viewport"]["paper_size"]
                    bounds = [c[0]-s[0]/2,c[1]-s[1]/2,c[0]+s[0]/2,c[1]+s[1]/2]
                    if overlaps(bounds, geometry["viewport"]["paper_bounds"]):
                        conflicts.append(f"未归属视口 {state['handle']} 与目标绘图区重叠；不会自动删除")
                elif state.get("name") == sheet.block_name:
                    conflicts.append(f"已有未归属图框 {state['handle']}，不能重复插入或自动接管")
            # System viewport is screen UI state and can change on tab switching.
            if not system_viewport:
                rows.append({"state":state,"owner":owner,"engineering_identity":entity_metadata(self,entity) if explicit else None})
        if owned and set(owned) != {"frame","viewport"}:
            conflicts.append("受管图框和视口不成对，不能自动修补未知状态")
        if request.existing_viewport_handle is not None:
            if requested_viewport is None:
                conflicts.append("指定复用视口不存在于目标布局")
            if owned and ("viewport" not in owned or self._handle_str(owned["viewport"].Handle) != request.existing_viewport_handle):
                conflicts.append("指定复用视口与既有受管视口不一致")
        layers = {str(self.doc.Layers.Item(i).Name): self.doc.Layers.Item(i)
                  for i in range(int(self.doc.Layers.Count))}
        vp_layer = layers.get(VIEWPORT_LAYER)
        layer_state = None
        if vp_layer is not None:
            layer_state = [bool(vp_layer.Plottable),bool(vp_layer.Lock),bool(vp_layer.Freeze),bool(vp_layer.LayerOn)]
            if layer_state != [False,False,False,True]:
                conflicts.append("既有视口边界图层须不可打印、未锁定、未冻结且开启")
        current_plot = None
        if layout is not None:
            current_plot = self._layout_plot_state(layout)
            for entity in owned.values():
                owner = self._layout_owner(entity)
                if owner.get("plot_hash") != fingerprint(current_plot):
                    conflicts.append("受管布局页面设置已被人工修改，请先核对")
        zero_layer = layers.get("0")
        if zero_layer is None or zero_layer.Lock or zero_layer.Freeze or not zero_layer.LayerOn or not zero_layer.Plottable:
            conflicts.append("图框默认图层0须开启、可打印且未锁定/冻结")
        for entity in owned.values():
            if self.doc.Layers.Item(str(entity.Layer)).Lock:
                conflicts.append("受管图框或视口所在图层已锁定")
        plan = {"request":request.model_dump(mode="json"), "geometry":geometry,
            "source_sha256":manifest.source_sha256, "manifest_hash":fingerprint(manifest.model_dump(mode="json")),
            "target":self.dwt_target_identity(), "document_session":id(self.doc),
            "snapshot_hash":fingerprint({"entities":rows,"content":content_states,"plot":current_plot,
                                         "block":block_state,"viewport_layer":layer_state}),
            "page_setup":plot, "conflicts":conflicts,
            "actions":["匹配纸张并将打印偏移归零，布局1:1打印", "更新受管图框与视口" if owned else ("插入图框并校正指定的原视口，保留句柄" if adopt_viewport is not None else "插入图框并创建视口"),
                       "仅清理本次新建布局自动生成的浮动视口" if layout is None else "保留既有未归属对象"],
            "existing":bool(layout is not None)}
        return plan, {"layout":layout,"setup":setup,"sheet":sheet,"owned":owned,
                      "block_hash":block_hash,"adopt_viewport":adopt_viewport}

    def preview_sheet_layout(self, **arguments):
        plan, _ = self._prepare_sheet_layout(LayoutRequest.model_validate(arguments))
        return {"applied":False,"verified":False,"plan":plan,
                "confirmation_token":confirmation(plan) if not plan["conflicts"] else None,
                "confirmation_expires_in_seconds":1800}

    def _configure_sheet_viewport(self, viewport, desired, lock=True, *, paper_view_bounds=None):
        # Display initializes the PViewport. ZoomCenter accepts WCS coordinates;
        # unlike ViewCenter it is an actual supported AutoCAD ActiveX method.
        self.doc.Activate()
        if (str(self.acad.ActiveDocument.FullName).casefold(),str(self.acad.ActiveDocument.Name).casefold()) != (
                str(self.doc.FullName).casefold(),str(self.doc.Name).casefold()):
            raise RuntimeError("无法激活目标图纸，未设置视口取景")
        viewport.DisplayLocked = False
        viewport.Center = point([*desired["paper_center"],0])
        viewport.Width, viewport.Height = desired["paper_size"]
        viewport.Direction = point([0,0,1])
        viewport.Target = point([0,0,desired["model_center"][2]])
        viewport.TwistAngle = 0.0
        self.doc.ActiveSpace = 0
        self.doc.MSpace = False
        # A preset may retain a paper view trillions of units wide. Make the
        # actual sheet visible before entering its floating viewport.
        bounds = paper_view_bounds or [
            desired["paper_center"][0]-desired["paper_size"][0]/2,
            desired["paper_center"][1]-desired["paper_size"][1]/2,
            desired["paper_center"][0]+desired["paper_size"][0]/2,
            desired["paper_center"][1]+desired["paper_size"][1]/2]
        margin = max(bounds[2]-bounds[0],bounds[3]-bounds[1])*0.05
        self.acad.ZoomWindow(point([bounds[0]-margin,bounds[1]-margin,0]),
                             point([bounds[2]+margin,bounds[3]+margin,0]))
        viewport.Display(True)
        self.doc.Regen(1)
        if not viewport.ViewportOn:
            raise RuntimeError("原视口未成功开启，未进入模型空间")
        try:
            self.doc.MSpace = True
            if not self.doc.MSpace:
                raise RuntimeError("未成功进入布局模型空间")
            self.doc.ActivePViewport = viewport
            if str(self.doc.ActivePViewport.Handle).upper() != str(viewport.Handle).upper():
                raise RuntimeError("实际活动视口与指定视口不符")
            self.acad.ZoomCenter(point(desired["model_center"]), desired["model_view_height"])
            viewport.CustomScale = desired["custom_scale"]
            self.doc.Regen(1)
        finally:
            self.doc.MSpace = False
        # Verify framing before locking, and verify the final lock independently.
        actual = self._read_native_viewports([viewport])[str(viewport.Handle).upper()]
        verify_viewport(actual, desired, locked=False)
        viewport.DisplayLocked = bool(lock)
        self.doc.Regen(1)
        actual = self._read_native_viewports([viewport])[str(viewport.Handle).upper()]
        return verify_viewport(actual, desired, locked=lock)

    def _sheet_page_mismatches(self, plan, layout):
        mismatches = []
        paper = self._sheet_plot_geometry(layout)
        expected = plan["geometry"]
        plot, missing = self._plot_payload(layout)
        if missing:
            mismatches.append("页面设置回读不完整")
        if not self._same_sheet_paper(paper,expected["paper"]) or not close_values(list(layout.PlotOrigin), [0,0], 1e-8):
            mismatches.append("纸张、边距、方向或打印偏移不符")
        scale = plot.get("plot_scale") or {}
        if (plot.get("plot_type") != "layout" or plot.get("paper_units") != "millimeters"
                or scale.get("mode") != "standard" or scale.get("standard_scale") != "1:1"):
            mismatches.append("布局必须按毫米、1:1打印")
        for field in ("device","media_name","plot_style_sheet","plot_with_lineweights","plot_with_plot_styles"):
            if plot.get(field) != plan["page_setup"].get(field):
                mismatches.append(f"页面设置 {field} 不一致")
        actual_scale = self._read_native_plot_scale(layout)
        if (not actual_scale["flags"] & 16 or actual_scale["standard_scale_code"] != 16 or
                not math.isclose(actual_scale["standard_scale_factor"],1.0,rel_tol=1e-10,abs_tol=1e-12)):
            mismatches.append("布局原生打印倍率不是1:1")
        return mismatches,paper,actual_scale

    def _verify_sheet_result(self, request, plan, layout, owned):
        mismatches,paper,print_scale = self._sheet_page_mismatches(plan,layout)
        expected = plan["geometry"]
        if set(owned) != {"frame","viewport"}:
            mismatches.append("缺少唯一的受管图框或视口")
            return {"verified":False,"mismatches":mismatches}
        frame, viewport = owned["frame"],owned["viewport"]
        geometry = expected["frame"]
        low, high = bounding_box(frame)
        bounds = [low[0],low[1],high[0],high[1]]
        if (str(frame.Name) != geometry["block_name"] or
            not close_values(list(frame.InsertionPoint), geometry["insertion_point"]) or
            not close_values([frame.XScaleFactor,frame.YScaleFactor,frame.ZScaleFactor], [geometry["scale"]]*3,1e-9)
            or abs(float(frame.Rotation)) > 1e-8 or
            not close_values(bounds, geometry["expected_bounds_layout"])):
            mismatches.append("图框位置、尺寸、比例或旋转不符")
        frame_layer = self.doc.Layers.Item(str(frame.Layer))
        if not frame.Visible or not frame_layer.LayerOn or frame_layer.Freeze or not frame_layer.Plottable:
            mismatches.append("图框不可见或不可打印")
        readback = None
        try:
            native = self._read_native_viewports([viewport])[str(viewport.Handle).upper()]
            readback = verify_viewport(native, expected["viewport"])
            c, s = readback["model_center"],readback["paper_size"]
            visible = [c[0]-s[0]/readback["custom_scale"]/2,c[1]-s[1]/readback["custom_scale"]/2,
                       c[0]+s[0]/readback["custom_scale"]/2,c[1]+s[1]/readback["custom_scale"]/2]
            if not contains(visible, expected["content_bounds"], 1e-7):
                mismatches.append("目标模型内容未完整落入视口")
            layer = self.doc.Layers.Item(str(viewport.Layer))
            if (str(viewport.Layer) != VIEWPORT_LAYER or layer.Plottable or not layer.LayerOn
                    or layer.Freeze or not viewport.Visible):
                mismatches.append("视口边界图层必须不可打印且视口可见")
        except RuntimeError as exc:
            mismatches.append(str(exc))
        return {"verified":not mismatches,"mismatches":mismatches,
                "paper":paper,"print_scale":print_scale,"frame_bounds_layout":bounds,"viewport":readback}

    def validate_sheet_layout(self, **arguments):
        request = LayoutRequest.model_validate(arguments)
        plan, context = self._prepare_sheet_layout(request)
        if context["layout"] is None:
            return {"verified":False,"mismatches":["目标布局不存在"]}
        result = self._verify_sheet_result(request, plan, context["layout"],context["owned"])
        result["mismatches"].extend(plan["conflicts"])
        result["verified"] = not result["mismatches"]
        return result

    def apply_sheet_layout(self, *, confirmation_token=None, semantic_binding=None, **arguments):
        if getattr(self,"_semantic_write_blocked",False):
            raise ConflictError("此前撤销无法确认，禁止继续写入；请先人工核对图纸状态")
        request = LayoutRequest.model_validate(arguments)
        plan, context = self._prepare_sheet_layout(request)
        if plan["conflicts"]:
            raise ConflictError("布局计划仍有冲突", details={"conflicts":plan["conflicts"]})
        verify_confirmation(confirmation_token, plan)
        if semantic_binding is not None:
            from cad_mcp.core.semantics import entity_metadata
            for entity in context["owned"].values():
                identity = entity_metadata(self,entity) or {}
                if any(identity.get(field) != semantic_binding.get(key) for field,key in
                       (("project_id","project_id"),("semantic_id","object_id"),("role","role"))):
                    raise ConflictError("既有受管布局的工程身份与本次绑定不符，不能自动改归属")
        from cad_mcp.core.semantics import drawing_transaction
        transaction = nullcontext() if self._undo_mark_open else drawing_transaction(self)
        original_context = self._sheet_context_state()
        try:
            with transaction:
                self._layout_rollback_probe = lambda: (
                    self._prepare_sheet_layout(request)[0] == plan and
                    self._sheet_context_state() == original_context)
                return self._materialize_sheet_layout(request,plan,context,semantic_binding)
        except Exception as exc:
            if getattr(exc,"rollback_status",None) == "failed_or_unverified":
                self._semantic_write_blocked = True
            raise

    def _sheet_context_state(self):
        tilemode = int(self.doc.GetVariable("TILEMODE"))
        mspace = bool(self.doc.MSpace) if tilemode == 0 else False
        return {"layout":str(self.doc.ActiveLayout.Name),"tilemode":tilemode,"mspace":mspace,
                "viewport":str(self.doc.ActivePViewport.Handle) if mspace else None,
                "active_document":[str(self.acad.ActiveDocument.FullName),str(self.acad.ActiveDocument.Name)]}

    def _materialize_sheet_layout(self, request, plan, context, semantic_binding=None):
        previous_document = self.acad.ActiveDocument
        previous_layout = self.doc.ActiveLayout
        previous_tilemode = int(self.doc.GetVariable("TILEMODE"))
        previous_mspace = bool(self.doc.MSpace) if previous_tilemode == 0 else False
        previous_viewport = self.doc.ActivePViewport if previous_mspace else None
        # The public semantic boundary owns one undo group for the full workflow.
        # A failure propagates to that boundary; no nested per-step transactions.
        try:
            self.doc.Activate()
            self.doc.SetVariable("TILEMODE",0)
            self.doc.MSpace = False
            layout = context["layout"]
            created = layout is None
            if created:
                layout = self.doc.Layouts.Add(request.layout_name)
            self._activate_layout(layout)
            layout.CopyFrom(context["setup"])
            layout.PaperUnits = 1
            layout.PlotType = 5
            layout.UseStandardScale = True
            layout.StandardScale = _STANDARD_SCALE["1:1"]
            layout.PlotRotation = plan["geometry"]["paper"]["rotation"]//90
            layout.PlotOrigin = point([0,0])
            layout.RefreshPlotDeviceInfo()
            # Gate one: no frame/viewport writes until actual paper matches.
            page_mismatches,_,_ = self._sheet_page_mismatches(plan,layout)
            if page_mismatches:
                raise RuntimeError("页面设置应用后实际纸张/打印设置与预览不符: "+"; ".join(page_mismatches))
            if created:
                defaults = [e for e in self._layout_entities(layout) if str(e.ObjectName) == "AcDbViewport"]
                native = self._read_native_viewports(defaults)
                for entity in defaults:
                    if native[str(entity.Handle).upper()]["number"] != 1:
                        entity.Delete()
            layers = {str(self.doc.Layers.Item(i).Name):self.doc.Layers.Item(i)
                      for i in range(int(self.doc.Layers.Count))}
            if VIEWPORT_LAYER not in layers:
                layer = self.doc.Layers.Add(VIEWPORT_LAYER)
                layer.Plottable = False
            owned = context["owned"]
            frame_spec = plan["geometry"]["frame"]
            frame = owned.get("frame")
            if frame is None:
                frame = layout.Block.InsertBlock(point(frame_spec["insertion_point"]),
                    frame_spec["block_name"],*[frame_spec["scale"]]*3,0.0)
                # Do not inherit an arbitrary current layer, which may be hidden.
                frame.Layer = "0"
            else:
                if str(frame.Name) != frame_spec["block_name"]:
                    raise ConflictError("已有受管图框块不同；不能以更新位置的方式替换块定义")
                frame.InsertionPoint = point(frame_spec["insertion_point"])
                frame.XScaleFactor = frame.YScaleFactor = frame.ZScaleFactor = frame_spec["scale"]
                frame.Rotation = 0.0
            owned["frame"] = frame
            # Gate two: prove paper/frame fit before adding model viewport.
            low, high = bounding_box(frame)
            if not close_values([low[0],low[1],high[0],high[1]],frame_spec["expected_bounds_layout"]):
                raise RuntimeError("图框实际范围与模板不符，未创建模型视口")
            frame_layer = self.doc.Layers.Item(str(frame.Layer))
            if not frame.Visible or not frame_layer.LayerOn or frame_layer.Freeze or not frame_layer.Plottable:
                raise RuntimeError("图框不可见或不可打印，未创建模型视口")
            viewport = owned.get("viewport")
            if viewport is None:
                viewport = context["adopt_viewport"]
            spec = plan["geometry"]["viewport"]
            if viewport is None:
                viewport = self.doc.PaperSpace.AddPViewport(point([*spec["paper_center"],0]),*spec["paper_size"])
            viewport.Layer = VIEWPORT_LAYER
            self._configure_sheet_viewport(viewport,spec,paper_view_bounds=frame_spec["reference_bounds_layout"])
            owned["viewport"] = viewport
            self.doc.Regen(1)
            result = self._verify_sheet_result(request,plan,layout,owned)
            if not result["verified"]:
                raise RuntimeError("布局结果核验失败: "+"; ".join(result["mismatches"]))
            native = self._read_native_viewports([viewport])
            plot_hash = fingerprint(self._layout_plot_state(layout))
            for role, entity in owned.items():
                state = self._layout_entity_state(entity,native)
                self._tag_layout_entity(entity,{"layout_id":request.layout_id,"role":role,
                    "state_hash":fingerprint(state),"state_version":2,
                    "block_hash":context["block_hash"],"plot_hash":plot_hash})
            if context["adopt_viewport"] is not None and semantic_binding is not None:
                # The outer semantic boundary tags only newly created entities.
                # Reused entities need a verified identity in this same undo group.
                from uuid import uuid4
                from cad_mcp.core.contracts import CadOperation
                from cad_mcp.core.registry import metadata_for_operation
                from cad_mcp.core.semantics import read_object, object_metadata, entity_metadata
                obj = read_object(self,semantic_binding["project_id"],semantic_binding["object_id"])
                if obj is None:
                    raise ConflictError("复用视口的工程对象尚未登记")
                operation = CadOperation(kind="cad_apply_sheet_layout",semantic_id=semantic_binding["object_id"],
                    role=semantic_binding["role"],view_id=semantic_binding.get("view_id"),
                    params=request.model_dump(mode="json"),metadata=object_metadata(obj))
                metadata = metadata_for_operation(operation,"",semantic_binding["project_id"])
                metadata.update(entity_uid=str(uuid4()),materialized_hash=self._entity_state_hash(viewport))
                self.set_entity_metadata(self._handle_str(viewport.Handle),metadata)
                if entity_metadata(self,viewport) != metadata:
                    raise RuntimeError("复用视口的工程身份回读不一致")
            return {"applied":True,"layout_name":request.layout_name,"created":created,
                    "frame_handle":self._handle_str(frame.Handle),
                    "viewport_handle":self._handle_str(viewport.Handle),**result}
        finally:
            # Restore the user's active layout and model/paper context even when
            # verification fails; failure here also aborts the transaction.
            self.doc.MSpace = False
            self._activate_layout(previous_layout)
            self.doc.SetVariable("TILEMODE",previous_tilemode)
            if previous_mspace:
                self.doc.MSpace = True
                self.doc.ActivePViewport = previous_viewport
            if int(self.doc.GetVariable("TILEMODE")) != previous_tilemode:
                raise RuntimeError("布局操作后模型/布局上下文恢复回读不一致")
            if previous_tilemode == 0:
                if bool(self.doc.MSpace) != previous_mspace:
                    raise RuntimeError("布局操作后纸空间上下文恢复回读不一致")
                if previous_mspace and str(self.doc.ActivePViewport.Handle) != str(previous_viewport.Handle):
                    raise RuntimeError("布局操作后原活动视口恢复回读不一致")
            previous_document.Activate()
            if (str(self.acad.ActiveDocument.FullName),str(self.acad.ActiveDocument.Name)) != (
                    str(previous_document.FullName),str(previous_document.Name)):
                raise RuntimeError("布局操作后原活动图纸恢复回读不一致")
