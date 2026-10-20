"""Single-entity property contracts, preflight and verified transactional edits.

Coordinates are WCS millimetres; rotations are degrees, including for COM.
Only model-space graphical entities are addressable. No resources are created.
"""

from __future__ import annotations

import io
import json
import math
import re
from typing import Annotated

import ezdxf
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator

from cad_mcp.core.runtime import CadMcpError, UnsupportedOperationError


LINEWEIGHTS = {-3, -2, -1, 0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40,
               50, 53, 60, 70, 80, 90, 100, 106, 120, 140, 158, 200, 211}
Number = Annotated[float, Field(allow_inf_nan=False, strict=True)]
Point3D = tuple[Number, Number, Number]


class EntityPropertyPatch(BaseModel):
    """Explicit whitelist; omitted fields keep their original values."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    layer: str | None = Field(default=None, min_length=1, max_length=255)
    color: int | None = Field(default=None, ge=0, le=256, strict=True,
                              description="ACI 颜色；0=ByBlock，256=ByLayer；设置后清除真彩色")
    linetype: str | None = Field(default=None, min_length=1, max_length=255)
    lineweight: int | None = Field(default=None, strict=True,
                                   description="1/100 mm；-1=ByLayer，-2=ByBlock，-3=Default")
    linetype_scale: Number | None = Field(default=None, gt=0)
    text: str | None = Field(default=None, min_length=1, max_length=10000,
                             description="TEXT/MTEXT 内容；保留空格，MTEXT 支持原生格式码")
    height: Number | None = Field(default=None, gt=0, description="TEXT/MTEXT 字高，mm")
    rotation: Number | None = Field(default=None, description="TEXT/MTEXT 旋转角，度")
    start: Point3D | None = Field(default=None, description="LINE 起点，WCS [x,y,z]")
    end: Point3D | None = Field(default=None, description="LINE 终点，WCS [x,y,z]")
    center: Point3D | None = Field(default=None, description="CIRCLE 圆心，WCS [x,y,z]")
    radius: Number | None = Field(default=None, gt=0, description="CIRCLE 半径，mm")

    @field_validator("layer", "linetype")
    @classmethod
    def resource_name(cls, value):
        if value is not None and (not value.strip() or value != value.strip()
                                  or any(ord(char) < 32 for char in value)):
            raise ValueError("资源名称不能为空或包含首尾空白、控制字符")
        return value

    @field_validator("lineweight")
    @classmethod
    def standard_lineweight(cls, value):
        if value is not None and value not in LINEWEIGHTS:
            raise ValueError("lineweight 必须是标准 CAD 线宽值")
        return value

    @model_validator(mode="after")
    def explicit_values(self):
        if not self.model_fields_set:
            raise ValueError("至少指定一个要修改的属性")
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("属性不能为 null；不修改的属性请省略")
        if self.rotation is not None:
            self.rotation %= 360.0
        return self

    @model_serializer(mode="wrap")
    def provided_properties(self, handler):
        # Parent tool inputs are serialized twice by the gateway/worker. Preserve
        # omitted fields across that boundary instead of turning them into nulls.
        return {name: value for name, value in handler(self).items()
                if name in self.model_fields_set}


COMMON = ("layer", "color", "linetype", "lineweight", "linetype_scale")
SPECIFIC = {"LINE": ("start", "end"), "CIRCLE": ("center", "radius"),
            "TEXT": ("text", "height", "rotation"),
            "MTEXT": ("text", "height", "rotation")}
DXF_NAMES = {"linetype_scale": "ltscale", "height": "height", "text": "text"}
COM_NAMES = {"layer": "Layer", "color": "Color", "linetype": "Linetype",
             "lineweight": "Lineweight", "linetype_scale": "LinetypeScale",
             "text": "TextString", "height": "Height", "rotation": "Rotation",
             "start": "StartPoint", "end": "EndPoint", "center": "Center",
             "radius": "Radius"}


def equal_value(left, right, *, angle=False):
    if isinstance(left, (tuple, list)) and isinstance(right, (tuple, list)):
        return len(left) == len(right) and all(equal_value(a, b) for a, b in zip(left, right))
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if angle:
            return abs(math.remainder(left - right, 360.0)) <= 1e-8
        return math.isclose(left, right, rel_tol=1e-10, abs_tol=1e-8)
    return left == right


class EntityAccess:
    def __init__(self, backend, handle, mode):
        self.backend, self.mode = backend, mode
        backend._check_doc()
        if not isinstance(handle, str) or not re.fullmatch(r"[0-9]{1,40}", handle) or int(handle) <= 0:
            raise ValueError("handle 必须是正整数的十进制字符串")
        self.handle = str(int(handle))
        self.entity = backend._get_entity(handle) if mode == "dxf" else backend._find(handle)
        if self.entity is None:
            raise CadMcpError(f"未找到句柄为 {handle} 的图元", code="NOT_FOUND",
                              hint="重新查询模型空间实体并核对十进制句柄")
        if mode == "dxf":
            if self.entity.dxf.owner != backend.msp.layout_key:
                raise ValueError("仅支持模型空间实体，不支持块定义、纸空间或非图形对象")
            self.kind = self.entity.dxftype()
        else:
            raw_kind = str(self.entity.ObjectName)
            self.kind = {"AcDbLine": "LINE", "AcDbCircle": "CIRCLE",
                         "AcDbText": "TEXT", "AcDbMText": "MTEXT"}.get(raw_kind, raw_kind)

    @property
    def fields(self):
        return (*COMMON, *SPECIFIC.get(self.kind, ()))

    def read(self, name):
        e = self.entity
        if self.mode == "dxf":
            if self.kind == "MTEXT" and name == "text":
                return e.text
            if self.kind == "MTEXT" and name == "height":
                return float(e.dxf.char_height)
            if self.kind == "MTEXT" and name == "rotation":
                return float(e.get_rotation()) % 360.0
            value = getattr(e.dxf, DXF_NAMES.get(name, name))
            if name == "center":
                value = e.ocs().to_wcs(value)
        else:
            value = getattr(e, COM_NAMES[name])
            if name == "rotation":
                return math.degrees(float(value)) % 360.0
        if name in {"start", "end", "center"}:
            return [float(v) for v in value]
        if name in {"height", "radius", "rotation", "linetype_scale"}:
            return float(value)
        if name in {"color", "lineweight"}:
            return int(value)
        return str(value)

    def true_color(self):
        if self.mode == "dxf":
            return self.entity.dxf.get("true_color")
        color = self.entity.TrueColor
        if int(color.ColorMethod) == 194:  # acColorMethodByRGB
            return (int(color.Red) << 16) | (int(color.Green) << 8) | int(color.Blue)
        return None

    def describe(self):
        result = {"handle": self.handle, "type": self.kind,
                  "properties": {name: self.read(name) for name in self.fields},
                  "true_color": self.true_color(), "editable_properties": list(self.fields)}
        # Fail before writing, or inside the transaction on readback, rather than
        # discovering an unserializable corrupt value after committing the edit.
        json.dumps(result, allow_nan=False)
        return result

    def resource(self, name, *, layer):
        if self.mode == "dxf":
            table = self.backend.doc.layers if layer else self.backend.doc.linetypes
            if name not in table:
                raise ValueError(f"{'图层' if layer else '线型'}不存在: {name}")
            item = table.get(name)
            if layer and item.is_locked():
                raise ValueError(f"图层已锁定: {name}")
            return str(item.dxf.name)
        table = self.backend.doc.Layers if layer else self.backend.doc.Linetypes
        # Missing COM resources fail before writes; no implicit loading/creation.
        item = table.Item(name)
        if layer and bool(item.Lock):
            raise ValueError(f"图层已锁定: {name}")
        return str(item.Name)

    def preflight(self, patch, before):
        invalid = set(patch) - set(self.fields)
        if invalid:
            raise UnsupportedOperationError(f"{self.kind} 不支持属性: {', '.join(sorted(invalid))}")
        self.resource(before["layer"], layer=True)
        for name in ("layer", "linetype"):
            if name in patch:
                patch[name] = self.resource(patch[name], layer=name == "layer")
        if self.kind == "LINE":
            start, end = patch.get("start", before["start"]), patch.get("end", before["end"])
            if math.dist(start, end) <= 1e-10:
                raise ValueError("LINE 起点和终点不能重合")
        if "text" in patch:
            if "%<" in before["text"] or "%<" in patch["text"]:
                raise UnsupportedOperationError("不支持通过通用属性编辑修改关联字段文字")
            if self.mode == "dxf" and self.entity.has_extension_dict:
                if "ACAD_FIELD" in self.entity.get_extension_dict():
                    raise UnsupportedOperationError("不支持覆盖包含关联字段的文字")
            if self.kind == "TEXT" and any(c in patch["text"] for c in "\r\n"):
                raise ValueError("TEXT 不支持换行；多行文字请使用 MTEXT")

    def write(self, name, value):
        e = self.entity
        if self.mode == "dxf":
            if name == "color":
                e.dxf.discard("true_color")
                e.dxf.discard("color_name")
            if self.kind == "MTEXT":
                if name == "text":
                    e.text = value
                    return
                if name == "height":
                    e.dxf.char_height = value
                    return
                if name == "rotation":
                    e.set_rotation(value)
                    return
            if name == "center":
                value = e.ocs().from_wcs(value)
            setattr(e.dxf, DXF_NAMES.get(name, name), value)
        else:
            if name in {"start", "end", "center"}:
                from cad_mcp.drawing.autocad import _pt
                value = _pt(*value)
            elif name == "rotation":
                value = math.radians(value)
            setattr(e, COM_NAMES[name], value)


def get_entity_properties(backend, handle, mode):
    return EntityAccess(backend, handle, mode).describe()


def edit_entity_properties(backend, handle, properties, *, dry_run=True, mode):
    if type(dry_run) is not bool:
        raise ValueError("dry_run 必须为布尔值")
    patch = EntityPropertyPatch.model_validate(properties).model_dump(exclude_unset=True)
    access = EntityAccess(backend, handle, mode)
    original = access.describe()
    before = original["properties"]
    access.preflight(patch, before)
    changes = {name: {"before": before[name], "after": value}
               for name, value in patch.items()
               if not equal_value(before[name], value, angle=name == "rotation")
               or (name == "color" and original["true_color"] is not None)}
    result = {"handle": access.handle, "type": access.kind, "dry_run": dry_run,
              "changes": changes, "before": original, "would_write": bool(changes),
              "applied": False, "verified": False}
    if dry_run or not changes:
        return {**result, "after": original, "verified": not dry_run,
                "would_write": bool(changes)}

    snapshot, started = None, False
    filename = None
    if mode == "dxf":
        snapshot = io.StringIO()
        filename = backend.doc.filename
        backend.doc.write(snapshot)
    else:
        started = backend._begin_undo()
    try:
        for name in changes:
            access.write(name, patch[name])
        if mode == "acad":
            access.entity.Update()
        after = access.describe()
        for name in patch:
            if not equal_value(after["properties"][name], patch[name], angle=name == "rotation"):
                raise RuntimeError(f"属性 {name} 写后回读不一致，已停止并尝试回滚")
        if "color" in patch and after["true_color"] is not None:
            raise RuntimeError("ACI 颜色未替换真彩色，已停止并尝试回滚")
        if mode == "acad":
            backend._commit_undo(started)
        return {**result, "after": after, "applied": True, "verified": True}
    except Exception as exc:
        if mode == "dxf":
            snapshot.seek(0)
            backend.doc = ezdxf.read(snapshot)
            backend.doc.filename = filename
            backend.msp = backend.doc.modelspace()
            exc.rollback_status = "succeeded"
        elif started:
            rolled_back = backend._rollback_undo(started)
            restored = False
            if rolled_back is True:
                try:
                    restored = EntityAccess(backend, handle, mode).describe() == original
                except Exception:
                    restored = False
            exc.rollback_status = "succeeded" if restored else "failed_or_unverified"
        raise
