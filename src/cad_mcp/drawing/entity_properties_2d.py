"""Planar property adapters. New coordinates are XY; existing elevations stay intact."""

import hashlib
import json
import math

from ezdxf.lldxf.tagwriter import TagCollector
from ezdxf.tools import pattern as hatch_patterns

from cad_mcp.core.geometry import validate_hatch_parameters
from cad_mcp.core.runtime import UnsupportedOperationError
from cad_mcp.drawing.block_contents import inspect_block, preflight_block, write_block, block_name


PLANAR_FIELDS = {
    "ARC": ("center", "radius", "start_angle", "end_angle"),
    "LWPOLYLINE": ("vertices", "closed", "constant_width"),
    "INSERT": ("position", "rotation", "x_scale", "y_scale", "block_attributes", "block_texts"),
    "HATCH": ("pattern", "pattern_scale", "pattern_angle"),
    "ELLIPSE": ("center", "major_axis", "radius_ratio", "start_parameter", "end_parameter"),
    "POINT": ("position",),
}
COM_KINDS = {"AcDbArc": "ARC", "AcDbPolyline": "LWPOLYLINE", "AcDbBlockReference": "INSERT",
             "AcDbHatch": "HATCH", "AcDbEllipse": "ELLIPSE", "AcDbPoint": "POINT"}
_COM_PROPS = {"center": "Center", "radius": "Radius", "start_angle": "StartAngle", "end_angle": "EndAngle",
              "major_axis": "MajorAxis", "radius_ratio": "RadiusRatio", "start_parameter": "StartParameter",
              "end_parameter": "EndParameter", "closed": "Closed", "constant_width": "ConstantWidth",
              "pattern": "PatternName", "pattern_scale": "PatternScale", "pattern_angle": "PatternAngle",
              "x_scale": "XScaleFactor", "y_scale": "YScaleFactor", "rotation": "Rotation"}
_DXF_PROPS = {"radius_ratio": "ratio", "start_parameter": "start_param", "end_parameter": "end_param",
              "x_scale": "xscale", "y_scale": "yscale", "constant_width": "const_width",
              "pattern": "pattern_name", "pattern_scale": "pattern_scale", "pattern_angle": "pattern_angle"}
_DEGREE_FIELDS = {"start_angle", "end_angle", "rotation", "pattern_angle"}


def _pt_exact(values):
    import pythoncom
    import win32com.client
    return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, tuple(values))


class PlanarPropertiesMixin:
    def read_planar(self, name):
        e = self.entity
        if self.kind == "HATCH":
            gradient = e.gradient is not None if self.mode == "dxf" else int(e.HatchObjectType) == 1
            if gradient:
                raise UnsupportedOperationError("本轮不编辑渐变填充，不转换为普通图案")
        if name == "vertices":
            if self.mode == "dxf":
                if len(e) > 1000:
                    raise ValueError("多段线顶点超过 1000 项")
                width = float(e.dxf.const_width)
                return [{"x": float(x), "y": float(y), "start_width": width or float(sw),
                         "end_width": width or float(ew), "bulge": float(b)}
                        for x, y, sw, ew, b in e.get_points("xyseb")]
            coords = list(e.Coordinates)
            if len(coords) // 2 > 1000:
                raise ValueError("多段线顶点超过 1000 项")
            result = []
            for i in range(len(coords) // 2):
                sw, ew = e.GetWidth(i)
                result.append({"x": float(coords[2*i]), "y": float(coords[2*i+1]), "start_width": float(sw),
                               "end_width": float(ew), "bulge": float(e.GetBulge(i))})
            return result
        if self.kind == "LWPOLYLINE" and name == "constant_width":
            # AutoCAD raises E_INVALIDINPUT for ConstantWidth on a variable-width
            # polyline. Derive the effective value from actual segment readback;
            # None explicitly means there is no single constant width.
            widths = [v[key] for v in self.read_planar("vertices") for key in ("start_width", "end_width")]
            if not widths:
                raise ValueError("多段线没有可读取的线段宽度")
            return widths[0] if all(math.isclose(v, widths[0], rel_tol=1e-10, abs_tol=1e-10) for v in widths) else None
        if name in {"block_attributes", "block_texts"}:
            attrs, texts = inspect_block(self)
            return attrs if name == "block_attributes" else texts
        if name in {"pattern_scale", "pattern_angle"} and self.read_planar("pattern").upper() == "SOLID":
            return 1.0 if name == "pattern_scale" else 0.0
        if name == "position":
            if self.mode == "dxf":
                point = e.dxf.insert if self.kind == "INSERT" else e.dxf.location
            else:
                point = e.InsertionPoint if self.kind == "INSERT" else e.Coordinates
            return [float(v) for v in tuple(point)[:2]]
        if name == "closed" and self.mode == "dxf":
            return bool(e.closed)
        value = (getattr(e.dxf, _DXF_PROPS.get(name, name)) if self.mode == "dxf"
                 else getattr(e, _COM_PROPS[name]))
        if name in {"center", "major_axis"}:
            return [float(v) for v in tuple(value)[:2]]
        if name == "closed":
            return bool(value)
        if name == "pattern":
            return str(value).upper()
        value = float(value)
        if name in _DEGREE_FIELDS:
            if self.mode == "acad":
                value = math.degrees(value)
            value %= 360.0
        return value

    def require_xy_plane(self):
        if self.kind not in PLANAR_FIELDS:
            return
        e = self.entity
        if self.kind == "POINT":
            return
        normal = e.dxf.get("extrusion", (0, 0, 1)) if self.mode == "dxf" else e.Normal
        if len(normal) != 3 or any(not math.isclose(float(v), wanted, rel_tol=0, abs_tol=1e-10)
                                    for v, wanted in zip(normal, (0, 0, 1))):
            raise UnsupportedOperationError("新增编辑仅支持法向为 +Z 的 XY 平面对象，不投影倾斜对象")

    def planar_metadata(self):
        if self.kind not in PLANAR_FIELDS:
            return {}
        e = self.entity
        if self.mode == "dxf":
            if self.kind == "LWPOLYLINE":
                z = e.dxf.elevation
            else:
                attr = {"POINT": "location", "INSERT": "insert", "HATCH": "elevation"}.get(self.kind, "center")
                z = getattr(e.dxf, attr)[2]
        else:
            if self.kind in {"HATCH", "LWPOLYLINE"}:
                z = e.Elevation
            else:
                attr = {"POINT": "Coordinates", "INSERT": "InsertionPoint"}.get(self.kind, "Center")
                z = getattr(e, attr)[2]
        meta = {"coordinate_mode": "XY", "elevation_preserved": True, "elevation": float(z)}
        if self.kind == "INSERT":
            meta["block_name"] = block_name(self, e)
            meta["block_content_scope"] = "selected_instance"
            meta["z_scale"] = float(e.dxf.zscale if self.mode == "dxf" else e.ZScaleFactor)
        if self.kind == "HATCH":
            meta["boundary"] = self.hatch_boundary()
        return meta

    def hatch_boundary(self):
        e = self.entity
        if self.mode == "dxf":
            collector = TagCollector(dxfversion=self.backend.doc.dxfversion)
            e.paths.export_dxf(collector, "HATCH")
            payload = [(tag.code, str(tag.value)) for tag in collector.tags]
            return {"loop_count": len(e.paths), "associative": bool(e.dxf.associative),
                    "signature": hashlib.sha256(json.dumps(payload).encode()).hexdigest()}
        # GetLoopAt returns the output-only COM argument in pywin32.
        loops = []
        count = int(e.NumberOfLoops)
        if count > 100:
            raise ValueError("填充环路超过 100 项")
        for i in range(count):
            objects = e.GetLoopAt(i)
            if not isinstance(objects, (tuple, list)):
                objects = (objects,)
            if len(objects) > 1000:
                raise ValueError("填充边界对象超过 1000 项")
            loops.append([self.backend._entity_state_hash(item) for item in objects])
        return {"loop_count": count, "associative": bool(e.AssociativeHatch),
                "signature": hashlib.sha256(json.dumps(loops).encode()).hexdigest()}

    def preflight_planar(self, patch, before):
        self.require_xy_plane()
        self._planar_before = before
        if self.kind in {"ARC", "ELLIPSE"}:
            if "center" in patch and len(patch["center"]) != 2:
                raise ValueError("新增平面实体的 center 只能提供 [x,y]，标高保持原值")
        if self.kind == "ARC":
            start = patch.get("start_angle", before["start_angle"]) % 360
            end = patch.get("end_angle", before["end_angle"]) % 360
            if abs(math.remainder(end - start, 360)) <= 1e-8:
                raise ValueError("圆弧起止角不能重合；整圆请使用 CIRCLE")
            for name in ("start_angle", "end_angle"):
                if name in patch:
                    patch[name] %= 360
        elif self.kind == "ELLIPSE":
            axis = patch.get("major_axis", before["major_axis"])
            if math.hypot(*axis) <= 1e-10:
                raise ValueError("椭圆长轴向量不能为零")
            a = patch.get("start_parameter", before["start_parameter"])
            b = patch.get("end_parameter", before["end_parameter"])
            if not 0 <= a < math.tau or not 0 <= b <= math.tau or abs(b-a) <= 1e-10:
                raise ValueError("椭圆起止参数必须在 0 到 2π 内且不重合，整椭圆使用 0 和 2π")
        elif self.kind == "LWPOLYLINE":
            if self.mode == "dxf" and self.entity.get_reactors():
                raise UnsupportedOperationError("该 DXF 多段线有关联依赖，不能证明改形后填充可同步更新")
            vertices = patch.get("vertices", before["vertices"])
            if "constant_width" in patch and "vertices" in patch:
                if any(v.get("start_width", 0) or v.get("end_width", 0) for v in vertices):
                    raise ValueError("不能同时设置常量线宽和非零逐段宽度")
            if "vertices" in patch:
                normalized = []
                changing_count = len(vertices) != len(before["vertices"])
                if changing_count and any(v[key] for v in before["vertices"] for key in ("bulge", "start_width", "end_width")):
                    if any(not {"bulge", "start_width", "end_width"}.issubset(v) for v in vertices):
                        raise ValueError("改变含弧段/宽度的多段线顶点数时，须逐顶点显式给出弯曲及宽度")
                for i, v in enumerate(vertices):
                    base = (dict(before["vertices"][i]) if not changing_count
                            else {"bulge": 0.0, "start_width": 0.0, "end_width": 0.0})
                    normalized.append({**base, **v})
                patch["vertices"] = vertices = normalized
            closed = patch.get("closed", before["closed"])
            if len(vertices) < (3 if closed else 2):
                raise ValueError("闭合多段线至少 3 点，开放多段线至少 2 点")
            pairs = list(zip(vertices, vertices[1:]))
            if closed:
                pairs.append((vertices[-1], vertices[0]))
            if any(math.hypot(a["x"]-b["x"], a["y"]-b["y"]) <= 1e-10 for a, b in pairs):
                raise ValueError("多段线相邻顶点不能重合；闭合时不要重复首点")
            # A constant width replaces per-segment widths in both native APIs.
            if "constant_width" in patch and "vertices" in patch:
                for v in vertices:
                    v["start_width"] = v["end_width"] = patch["constant_width"]
        elif self.kind == "INSERT":
            preflight_block(self, patch, before)
        elif self.kind == "HATCH":
            pattern = patch.get("pattern", before["pattern"]).strip().upper()
            scale = patch.get("pattern_scale", 1.0 if pattern == "SOLID" else before["pattern_scale"])
            angle = patch.get("pattern_angle", 0.0 if pattern == "SOLID" else before["pattern_angle"]) % 360
            pattern, scale, angle = validate_hatch_parameters(pattern, scale, angle)
            measurement = (self.backend.doc.header.get("$MEASUREMENT", 1) if self.mode == "dxf"
                           else self.backend.doc.GetVariable("MEASUREMENT"))
            self._patterns = hatch_patterns.load(measurement=int(measurement))
            if pattern != "SOLID" and pattern not in self._patterns:
                raise UnsupportedOperationError(f"未提供可验证的预定义填充图案: {pattern}")
            patch.update(pattern=pattern, pattern_scale=scale, pattern_angle=angle)
            self._hatch_before = self.hatch_boundary()

    def write_planar(self, patch):
        e = self.entity
        before = self._planar_before
        if self.kind == "INSERT":
            write_block(self, patch, before)
            return
        if self.kind == "LWPOLYLINE":
            if "vertices" in patch:
                vertices = patch["vertices"]
                if self.mode == "dxf":
                    e.dxf.const_width = 0.0
                    e.set_points([(v["x"], v["y"], v["start_width"], v["end_width"], v["bulge"])
                                  for v in vertices], format="xyseb")
                else:
                    e.Coordinates = _pt_exact([n for v in vertices for n in (v["x"], v["y"])])
                    for i, v in enumerate(vertices):
                        e.SetBulge(i, v["bulge"])
                        e.SetWidth(i, v["start_width"], v["end_width"])
            if "closed" in patch:
                if self.mode == "dxf":
                    e.closed = patch["closed"]
                else:
                    e.Closed = patch["closed"]
            if "constant_width" in patch:
                if self.mode == "dxf":
                    e.dxf.const_width = patch["constant_width"]
                    # Clearing a constant width must also clear per-segment
                    # widths, matching native ConstantWidth setter semantics.
                    if patch["constant_width"] == 0:
                        e.set_points([(x, y, 0, 0, b) for x, y, _, _, b in e.get_points("xyseb")], format="xyseb")
                else:
                    e.ConstantWidth = patch["constant_width"]
            return
        if self.kind == "HATCH":
            pattern, scale, angle = (patch.get(key, before[key]) for key in ("pattern", "pattern_scale", "pattern_angle"))
            if self.mode == "dxf":
                color, rgb, style = e.dxf.color, e.rgb, e.dxf.hatch_style
                if pattern == "SOLID":
                    e.set_solid_fill(color=color, style=style, rgb=rgb)
                else:
                    e.set_pattern_fill(pattern, color=color, scale=scale, angle=angle,
                                       style=style, definition=self._patterns[pattern])
                    if rgb is not None:
                        e.rgb = rgb
            else:
                e.SetPattern(0 if pattern == "SOLID" else 1, pattern)
                if pattern != "SOLID":
                    e.PatternScale = scale
                    e.PatternAngle = math.radians(angle)
                e.Evaluate()
            if self.hatch_boundary() != self._hatch_before:
                raise RuntimeError("填充边界/孔洞/关联性在写后发生变化，已停止并尝试回滚")
            return
        for name, value in patch.items():
            if name in {"center", "position", "major_axis"}:
                if self.mode == "dxf":
                    attr = "location" if name == "position" else name
                    old = getattr(e.dxf, attr)
                    setattr(e.dxf, attr, (*value, float(old[2])))
                else:
                    attr = "Coordinates" if name == "position" else _COM_PROPS[name]
                    old = getattr(e, attr)
                    setattr(e, attr, _pt_exact((*value, float(old[2]))))
            elif self.mode == "dxf":
                setattr(e.dxf, _DXF_PROPS.get(name, name), value)
            else:
                setattr(e, _COM_PROPS[name], math.radians(value) if name in _DEGREE_FIELDS else value)
