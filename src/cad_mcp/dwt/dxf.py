"""DWT-related capabilities that are honest about the DXF backend boundary."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from cad_mcp.dwt.contracts import DraftingProfileV2
from cad_mcp.dwt.engine import attach_confirmation, build_plan, validate_snapshot
from cad_mcp.core.runtime import UnsupportedModeError


_ROTATION_FROM_DXF = {0: 0, 1: 90, 2: 180, 3: 270}
_PLOT_TYPE_FROM_DXF = {
    0: "display", 1: "extents", 2: "limits", 3: "view", 4: "window", 5: "layout",
}
_PAPER_UNITS_FROM_DXF = {0: "inches", 1: "millimeters", 2: "pixels"}


def _read_dxf(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return obj.dxf.get(name, default)
    except Exception:
        return default


class DxfDwtMixin:
    """Mixin used by :class:`CadDocument`; it never pretends DXF is DWT/DWG."""

    def dwt_target_identity(self) -> Dict[str, Any]:
        self._check_doc()
        path = str(self.current_path.resolve()) if self.current_path else None
        return {
            "mode": "dxf",
            "path": path,
            "name": self.current_path.name if self.current_path else "未保存.dxf",
        }

    def set_drawing_settings(
        self,
        *,
        insertion_units: str = "millimeters",
        measurement_system: str = "metric",
        linear_unit_format: str = "decimal",
        linear_precision: int = 2,
        angular_unit_format: str = "decimal_degrees",
        angular_precision: int = 2,
        associative_dimensions: bool = True,
        mirror_text: bool = False,
        display_lineweights: bool = True,
    ) -> Dict[str, Any]:
        self._check_doc()
        if insertion_units != "millimeters" or measurement_system != "metric":
            raise ValueError("DWT 2.0 只允许毫米模型和公制测量环境")
        if linear_unit_format != "decimal" or angular_unit_format != "decimal_degrees":
            raise ValueError("DWT 2.0 只允许十进制长度和十进制度数")
        before = self.get_drawing_settings()
        requested = {
            "insertion_units": insertion_units,
            "measurement_system": measurement_system,
            "linear_unit_format": linear_unit_format,
            "linear_precision": int(linear_precision),
            "angular_unit_format": angular_unit_format,
            "angular_precision": int(angular_precision),
            "associative_dimensions": bool(associative_dimensions),
            "mirror_text": bool(mirror_text),
            "display_lineweights": bool(display_lineweights),
        }
        raw = {
            "$INSUNITS": 4,
            "$MEASUREMENT": 1,
            "$LUNITS": 2,
            "$LUPREC": int(linear_precision),
            "$AUNITS": 0,
            "$AUPREC": int(angular_precision),
            "$DIMASSOC": 2 if associative_dimensions else 1,
            "$MIRRTEXT": 1 if mirror_text else 0,
            "$LWDISPLAY": 1 if display_lineweights else 0,
        }
        for name, value in raw.items():
            self.doc.header[name] = value
        after = self.get_drawing_settings()
        failed = [name for name, value in requested.items() if after.get(name) != value]
        if failed:
            raise RuntimeError("DXF 图纸设置写入后读回不一致: " + ", ".join(failed))
        changed = [name for name, value in requested.items() if before.get(name) != value]
        unchanged = [name for name in requested if name not in changed]
        return {
            "mode": "dxf",
            "before": before,
            "after": after,
            "changed": changed,
            "unchanged": unchanged,
        }

    @staticmethod
    def _dxf_dimstyle_text_style(document: Any, style: Any) -> Optional[str]:
        value = _read_dxf(style, "dimtxsty")
        if not value:
            return None
        value = str(value)
        if value in document.styles:
            return value
        try:
            entity = document.entitydb.get(value)
            return str(entity.dxf.name) if entity is not None else value
        except Exception:
            return value

    def list_dimstyles(self) -> Dict[str, Any]:
        self._check_doc()
        current = str(self.doc.header.get("$DIMSTYLE", self.active_dimstyle))
        result = []
        unverified: List[str] = []
        for style in self.doc.dimstyles:
            name = str(style.dxf.name)
            dimzin = int(_read_dxf(style, "dimzin", 0) or 0)
            separator = _read_dxf(style, "dimdsep", 46)
            try:
                separator = chr(int(separator))
            except (TypeError, ValueError, OverflowError):
                separator = None
                unverified.append(f"dimension_styles.{name}.decimal_separator")
            result.append({
                "name": name,
                "text_style": self._dxf_dimstyle_text_style(self.doc, style),
                "text_height": float(_read_dxf(style, "dimtxt", 0.0)),
                "overall_scale": float(_read_dxf(style, "dimscale", 1.0)),
                "linear_scale_factor": float(_read_dxf(style, "dimlfac", 1.0)),
                "precision": int(_read_dxf(style, "dimdec", 0)),
                "oblique_stroke_size": float(_read_dxf(style, "dimtsz", 0.0)),
                "arrow_size": float(_read_dxf(style, "dimasz", 0.0)),
                "text_gap": float(_read_dxf(style, "dimgap", 0.0)),
                "text_above": bool(int(_read_dxf(style, "dimtad", 0))),
                "suppress_leading_zeros": bool(dimzin & 4),
                "suppress_trailing_zeros": bool(dimzin & 8),
                "decimal_separator": separator,
                "extension_line_offset": float(_read_dxf(style, "dimexo", 0.0)),
                "extension_line_extension": float(_read_dxf(style, "dimexe", 0.0)),
                "baseline_spacing": float(_read_dxf(style, "dimdli", 0.0)),
                "dimension_line_color": int(_read_dxf(style, "dimclrd", 256)),
                "extension_line_color": int(_read_dxf(style, "dimclre", 256)),
                "text_color": int(_read_dxf(style, "dimclrt", 256)),
                "dimension_line_lineweight": int(_read_dxf(style, "dimlwd", -1)),
                "extension_line_lineweight": int(_read_dxf(style, "dimlwe", -1)),
                "is_current": name.casefold() == current.casefold(),
            })
        return {
            "mode": "dxf",
            "dimstyles": sorted(result, key=lambda item: item["name"].casefold()),
            "unverified_fields": sorted(set(unverified)),
        }

    def list_layouts_detailed(self) -> Dict[str, Any]:
        self._check_doc()
        rows: List[Dict[str, Any]] = []
        unverified: List[str] = []
        for layout in self.doc.layouts:
            name = str(layout.name)
            obj = getattr(layout, "dxf_layout", None)
            plot = getattr(layout, "plot_settings", None) or obj
            entities = list(layout)
            viewport_count = sum(1 for entity in entities if entity.dxftype() == "VIEWPORT")
            user_content_count = sum(
                1 for entity in entities if entity.dxftype() != "VIEWPORT"
            ) + max(0, viewport_count - 1)
            prefix = f"layouts.{name}"

            def value(field: str, dxf_name: str, mapper=None):
                raw = _read_dxf(plot, dxf_name, None)
                if raw is None:
                    unverified.append(f"{prefix}.{field}")
                    return None
                return mapper(raw) if mapper else raw

            numerator = value("plot_scale.numerator", "custom_scale_numerator", float)
            denominator = value("plot_scale.denominator", "custom_scale_denominator", float)
            rows.append({
                "layout_name": name,
                "is_model": name.casefold() == "model",
                "device": value("device", "plot_configuration_file", str),
                "media_name": value("media_name", "paper_size", str),
                "rotation": value("rotation", "plot_rotation", lambda raw: _ROTATION_FROM_DXF.get(int(raw))),
                "plot_style_sheet": value("plot_style_sheet", "current_style_sheet", str),
                "plot_type": value("plot_type", "plot_type", lambda raw: _PLOT_TYPE_FROM_DXF.get(int(raw))),
                "paper_units": value("paper_units", "plot_paper_units", lambda raw: _PAPER_UNITS_FROM_DXF.get(int(raw))),
                "plot_scale": {
                    "mode": "custom" if numerator is not None and denominator is not None else None,
                    "standard_scale": None,
                    "numerator": numerator,
                    "denominator": denominator,
                },
                "center_plot": None,
                "plot_with_lineweights": None,
                "plot_with_plot_styles": None,
                "page_setup": value("page_setup", "page_setup_name", str),
                "viewport_count": viewport_count,
                "user_content_count": user_content_count,
            })
            unverified.extend([
                f"{prefix}.center_plot",
                f"{prefix}.plot_with_lineweights",
                f"{prefix}.plot_with_plot_styles",
            ])
        return {
            "mode": "dxf",
            "layouts": sorted(rows, key=lambda item: item["layout_name"].casefold()),
            "unverified_fields": sorted(set(unverified)),
        }

    def get_plot_environment(self) -> Dict[str, Any]:
        self._check_doc()
        settings = self.get_drawing_settings()
        return {
            "mode": "dxf",
            "plot_style_mode": settings.get("plot_style_mode"),
            "plot_style_search_path": None,
            "available_style_sheets": None,
            "available_devices": None,
            "media_by_device": None,
            "default_device": None,
            "unverified_fields": [
                "plot_style_search_path", "available_style_sheets",
                "available_devices", "media_by_device", "default_device",
            ],
            "note": "DXF 文件不提供本机 AutoCAD 打印资源；返回 None，不把未知当成空列表",
        }

    def list_page_setups(self) -> Dict[str, Any]:
        self._check_doc()
        return {
            "mode": "dxf",
            "page_setups": [],
            "unverified_fields": ["page_setups"],
            "note": "命名页面设置需要 AutoCAD PlotConfigurations，DXF 后端无法可靠枚举",
        }

    def convert_plot_style_mode(
        self, target_mode: str, expected_current_mode: Optional[str], dry_run: bool
    ) -> Dict[str, Any]:
        raise UnsupportedModeError("CTB/STB 模式转换仅 acad 模式可检查或执行")

    def create_or_update_page_setup(self, **_: Any) -> Dict[str, Any]:
        raise UnsupportedModeError("命名页面设置仅 acad 模式支持")

    def configure_layout_plot(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        raise UnsupportedModeError("布局打印设备和页面设置需要 acad 模式")

    def rename_layer(self, old_name: str, new_name: str) -> Dict[str, Any]:
        self._check_doc()
        protected = {"0", "defpoints"}
        if old_name.casefold() in protected:
            raise ValueError("禁止重命名保护图层 0 或 Defpoints")
        if old_name not in self.doc.layers:
            raise ValueError(f"图层不存在: {old_name}")
        if new_name in self.doc.layers:
            raise ValueError(f"目标图层已存在: {new_name}")
        self.doc.layers.get(old_name).rename(new_name)
        after = self.doc.layers.get(new_name)
        if str(after.dxf.name) != new_name or old_name in self.doc.layers:
            raise RuntimeError("图层改名后读回验证失败")
        if self.active_layer.casefold() == old_name.casefold():
            self.active_layer = new_name
        return {"before": old_name, "after": new_name, "verified": True}

    def delete_layer(
        self,
        names: Sequence[str],
        *,
        require_empty: bool = True,
        allow_nonempty: bool = False,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        self._check_doc()
        if require_empty and allow_nonempty:
            raise ValueError("require_empty=true 时不能同时 allow_nonempty=true")
        details = {item["name"].casefold(): item for item in self.list_layers_detailed()["layers"]}
        results = []
        for name in names:
            reason = None
            item = details.get(name.casefold())
            if name.casefold() in {"0", "defpoints"}:
                reason = "保护图层"
            elif item is None:
                reason = "图层不存在"
            elif bool(item.get("is_current")):
                reason = "当前图层"
            elif int(item.get("entity_count") or 0) > 0 and not allow_nonempty:
                reason = "图层非空；只有 allow_nonempty=true 才允许处理"
            if reason:
                results.append({"name": name, "status": "skipped", "reason": reason})
                continue
            if dry_run:
                results.append({
                    "name": name,
                    "status": "would_delete",
                    "entity_count": int(item.get("entity_count") or 0),
                    "would_move_to_layer_0": (
                        int(item.get("entity_count") or 0) if allow_nonempty else 0
                    ),
                })
                continue
            moved = 0
            if int(item.get("entity_count") or 0) > 0:
                for entity in self.doc.entitydb.values():
                    if not getattr(entity, "is_alive", False):
                        continue
                    try:
                        if (
                            entity.dxf.hasattr("layer")
                            and str(entity.dxf.layer).casefold() == item["name"].casefold()
                        ):
                            entity.dxf.layer = "0"
                            moved += 1
                    except Exception:
                        continue
            self.doc.layers.remove(item["name"])
            status = "deleted" if item["name"] not in self.doc.layers else "failed"
            results.append({"name": name, "status": status, "moved_to_layer_0": moved})
            if status == "failed":
                raise RuntimeError(f"图层 {name} 删除后仍存在")
        return {"mode": "dxf", "dry_run": dry_run, "results": results}

    def rename_layout(self, old_name: str, new_name: str) -> Dict[str, Any]:
        self._check_doc()
        if old_name.casefold() == "model":
            raise ValueError("禁止重命名 Model 布局")
        names = {str(layout.name).casefold() for layout in self.doc.layouts}
        if old_name.casefold() not in names:
            raise ValueError(f"布局不存在: {old_name}")
        if new_name.casefold() in names:
            raise ValueError(f"目标布局已存在: {new_name}")
        self.doc.layouts.rename(old_name, new_name)
        after = {str(layout.name).casefold() for layout in self.doc.layouts}
        if new_name.casefold() not in after or old_name.casefold() in after:
            raise RuntimeError("布局改名后读回验证失败")
        return {"before": old_name, "after": new_name, "verified": True}

    def delete_layout(
        self, names: Sequence[str], *, require_empty: bool = True, dry_run: bool = True
    ) -> Dict[str, Any]:
        self._check_doc()
        results = []
        for requested in names:
            rows = self.list_layouts_detailed()["layouts"]
            papers = [item for item in rows if not item["is_model"]]
            item = next((row for row in rows if row["layout_name"].casefold() == requested.casefold()), None)
            reason = None
            if requested.casefold() == "model":
                reason = "保护布局 Model"
            elif item is None:
                reason = "布局不存在"
            elif len(papers) <= 1:
                reason = "不能删除最后一个图纸空间布局"
            elif require_empty and int(item.get("user_content_count") or 0) > 0:
                reason = "布局含用户视口或内容"
            if reason:
                results.append({"name": requested, "status": "skipped", "reason": reason})
                continue
            if dry_run:
                results.append({"name": requested, "status": "would_delete"})
            else:
                self.doc.layouts.delete(item["layout_name"])
                results.append({"name": requested, "status": "deleted"})
        return {"mode": "dxf", "dry_run": dry_run, "results": results}

    def create_or_update_attribute_block(
        self,
        block_name: str,
        base_point: Sequence[float],
        entity_handles: Sequence[str],
        attribute_definitions: Sequence[Mapping[str, Any]],
        replace: bool = False,
        annotative: bool = False,
    ) -> Dict[str, Any]:
        if annotative:
            raise UnsupportedModeError("原生注释性属性块仅支持 acad 模式")
        self._check_doc()
        exists = block_name in self.doc.blocks
        if exists and not replace:
            raise FileExistsError(f"块定义已存在，未覆盖: {block_name}")
        if exists:
            self.doc.blocks.delete_block(block_name, safe=True)
        block = self.doc.blocks.new(block_name, base_point=tuple(base_point))
        copied = []
        for handle in entity_handles:
            entity = self._get_entity(handle)
            if entity is None:
                raise ValueError(f"图元句柄不存在: {handle}")
            block.add_entity(entity.copy())
            copied.append(handle)
        tags = set()
        for definition in attribute_definitions:
            tag = str(definition["tag"]).upper()
            if tag in tags:
                raise ValueError(f"属性标签重复: {tag}")
            tags.add(tag)
            flags = (
                (1 if definition.get("invisible") else 0)
                | (2 if definition.get("constant") else 0)
                | (4 if definition.get("verify") else 0)
                | (8 if definition.get("preset") else 0)
            )
            attribs = {
                "prompt": definition.get("prompt", ""),
                "flags": flags,
                "style": definition.get("text_style") or self.active_textstyle,
            }
            block.add_attdef(
                tag,
                insert=tuple(definition.get("position", (0.0, 0.0, 0.0))),
                text=str(definition.get("default_value", "")),
                height=float(definition.get("text_height", 2.5)),
                rotation=float(definition.get("rotation", 0.0)),
                dxfattribs=attribs,
            )
        return {
            "mode": "dxf",
            "block_name": block_name,
            "created": not exists,
            "replaced": exists,
            "entity_handles_copied": copied,
            "attribute_tags": sorted(tags),
            "verified": block_name in self.doc.blocks,
        }

    def set_block_attributes(self, block_handle: str, values: Mapping[str, str]) -> Dict[str, Any]:
        self._check_doc()
        entity = self._get_entity(block_handle)
        if entity is None or entity.dxftype() != "INSERT":
            raise ValueError(f"块引用不存在: {block_handle}")
        by_tag = {str(attrib.dxf.tag).casefold(): attrib for attrib in entity.attribs}
        updated, unchanged, missing = [], [], []
        for tag, value in values.items():
            attrib = by_tag.get(tag.casefold())
            if attrib is None:
                missing.append(tag)
            elif str(attrib.dxf.text) == str(value):
                unchanged.append(tag)
            else:
                attrib.dxf.text = str(value)
                updated.append(tag)
        return {
            "mode": "dxf",
            "block_handle": block_handle,
            "updated": updated,
            "missing_tags": missing,
            "unchanged": unchanged,
        }

    def bind_block_attribute_field(self, **_: Any) -> Dict[str, Any]:
        raise UnsupportedModeError(
            "关联 AutoCAD 字段需要原生 DWG 对象 ID 和 REGEN，DXF 模式不支持"
        )

    def import_block_definitions(self, **_: Any) -> Dict[str, Any]:
        raise UnsupportedModeError("DWG 块库导入需要 acad 模式；ezdxf 不能读取 DWG")

    def export_block_library(self, **_: Any) -> Dict[str, Any]:
        raise UnsupportedModeError("DWG 块库导出需要 acad 模式")

    def inspect_block_library(self, **_: Any) -> Dict[str, Any]:
        raise UnsupportedModeError("DWG 块库检查需要 acad 模式；不会把 DWG 当作 DXF 猜读")

    def audit_block_definition(self, **_: Any) -> Dict[str, Any]:
        raise UnsupportedModeError("指定 DWG 块审查需要 acad 模式；ezdxf 不能读取 DWG")

    @staticmethod
    def _fixed(value: float, precision: int, suppress_trailing_zeros: bool) -> str:
        text = f"{value:.{precision}f}"
        if suppress_trailing_zeros and "." in text:
            text = text.rstrip("0").rstrip(".")
        return text

    def insert_coordinate_label(
        self,
        *,
        target_x: float,
        target_y: float,
        target_handle: Optional[str],
        display_unit: str,
        precision: int,
        suppress_trailing_zeros: bool,
        associative: bool,
        block_name: str,
        layer: str,
        text_style: Optional[str],
    ) -> Dict[str, Any]:
        if associative:
            raise UnsupportedModeError(
                "DXF 模式不能建立移动后由 REGEN 更新的坐标字段；未生成静态文字"
            )
        self._check_doc()
        factor = 0.001 if display_unit == "m" else 1.0
        if block_name not in self.doc.blocks:
            definitions = [
                {"tag": "X", "prompt": "X", "position": (0, 0, 0), "text_height": 2.5,
                 "text_style": text_style or self.active_textstyle},
                {"tag": "Y", "prompt": "Y", "position": (0, -4, 0), "text_height": 2.5,
                 "text_style": text_style or self.active_textstyle},
            ]
            self.create_or_update_attribute_block(block_name, (0, 0, 0), [], definitions, False)
        if layer not in self.doc.layers:
            self.doc.layers.add(layer)
        ref = self.msp.add_blockref(block_name, (target_x, target_y), dxfattribs={"layer": layer})
        values = {
            "X": self._fixed(target_x * factor, precision, suppress_trailing_zeros),
            "Y": self._fixed(target_y * factor, precision, suppress_trailing_zeros),
        }
        ref.add_auto_attribs(values)
        return {
            "mode": "dxf",
            "handle": self._handle_str(ref.dxf.handle),
            "block_name": block_name,
            "values": values,
            "display_unit": display_unit,
            "associative": False,
            "warning": "调用方明确 associative=false；该标签是静态块属性",
            "target_handle": target_handle,
        }

    def insert_elevation_label(
        self,
        *,
        target_point: Sequence[float],
        target_handle: Optional[str],
        elevation_source: str,
        display_unit: str,
        precision: int,
        suppress_trailing_zeros: bool,
        associative: bool,
        block_name: str,
        layer: str,
        text_style: Optional[str],
        positive_sign: bool,
        zero_format: str,
    ) -> Dict[str, Any]:
        if associative:
            raise UnsupportedModeError(
                "DXF 模式不能建立移动后由 REGEN 更新的标高字段；未生成静态文字"
            )
        self._check_doc()
        point = list(target_point)
        while len(point) < 3:
            point.append(0.0)
        elevation = point[2]
        factor = 0.001 if display_unit == "m" else 1.0
        scaled = elevation * factor
        if abs(scaled) <= 0.5 * (10 ** -precision):
            value = zero_format
        else:
            value = self._fixed(scaled, precision, suppress_trailing_zeros)
            if scaled > 0 and positive_sign:
                value = "+" + value
        if block_name not in self.doc.blocks:
            definition = [{
                "tag": "ELEV", "prompt": "标高", "position": (0, 0, 0),
                "text_height": 2.5, "text_style": text_style or self.active_textstyle,
            }]
            self.create_or_update_attribute_block(block_name, (0, 0, 0), [], definition, False)
        if layer not in self.doc.layers:
            self.doc.layers.add(layer)
        ref = self.msp.add_blockref(block_name, tuple(point), dxfattribs={"layer": layer})
        ref.add_auto_attribs({"ELEV": value})
        return {
            "mode": "dxf",
            "handle": self._handle_str(ref.dxf.handle),
            "value": value,
            "display_unit": display_unit,
            "elevation_source": elevation_source,
            "associative": False,
            "warning": "调用方明确 associative=false；该标签是静态块属性",
            "target_handle": target_handle,
        }

    def create_or_update_ctb(self, **_: Any) -> Dict[str, Any]:
        raise UnsupportedModeError("CTB 创建和验证需要 AutoCAD 实际打印样式环境")

    def capture_dwt_snapshot(self) -> Dict[str, Any]:
        self._check_doc()
        environment = self.get_drafting_environment()
        block_names = sorted(
            str(block.name) for block in self.doc.blocks
            if not str(block.name).startswith("*")
        )
        return {
            "target": self.dwt_target_identity(),
            "drawing_settings": self.get_drawing_settings(),
            "layers": self.list_layers_detailed(),
            "textstyles": self.list_textstyles(),
            "dimstyles": self.list_dimstyles(),
            "linetype_settings": environment.get("linetype") or {},
            "layouts": self.list_layouts_detailed(),
            "page_setups": self.list_page_setups(),
            "plot_environment": self.get_plot_environment(),
            "block_names": block_names,
            "font_files": None,
            "unsupported_items": [
                {"item": "named_page_setups", "reason": "DXF 后端不能可靠读取或写入 AutoCAD 命名页面设置"},
                {"item": "dwg_block_libraries", "reason": "DXF 后端不能读取或生成 DWG 块库"},
                {"item": "associative_fields", "reason": "DXF 后端不能建立 AutoCAD 关联字段"},
            ],
        }

    def preview_dwt_profile(
        self,
        profile: DraftingProfileV2,
        *,
        profile_path: str,
        profile_sha256: str,
        target_drawing: Optional[str],
        update_existing: bool,
        preserve_unmanaged: bool,
    ) -> Dict[str, Any]:
        plan = build_plan(
            profile,
            self.capture_dwt_snapshot(),
            profile_path=profile_path,
            profile_sha256=profile_sha256,
            target_drawing=target_drawing,
            update_existing=update_existing,
            preserve_unmanaged=preserve_unmanaged,
        )
        return attach_confirmation(plan)

    def apply_dwt_profile(
        self, _profile: Optional[DraftingProfileV2] = None, **_: Any
    ) -> Dict[str, Any]:
        raise UnsupportedModeError(
            "DWT 是 AutoCAD 原生模板；正式应用 DraftingProfile 2.0 仅 acad 模式支持"
        )

    def validate_dwt_profile(
        self, profile: DraftingProfileV2, *, preserve_unmanaged: bool = True
    ) -> Dict[str, Any]:
        return validate_snapshot(
            profile, self.capture_dwt_snapshot(), preserve_unmanaged=preserve_unmanaged
        )
