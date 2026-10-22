"""AutoCAD COM implementation for generic DWT profile management.

Every public write method is called through ``_AcadLockProxy`` and therefore
runs in one serialized AutoCAD undo group.  This module never calls Save on the
current drawing.  File-producing block-library operations write only the
explicit target path and verify its SHA-256 before reporting success.
"""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
import uuid

import pythoncom
import win32com.client

from cad_mcp.dwt.contracts import DraftingProfileV2, sha256_file
from cad_mcp.dwt.engine import (
    attach_confirmation,
    build_plan,
    validate_snapshot,
    verify_confirmation,
)
from cad_mcp.core.runtime import (
    ConflictError,
    UnsupportedOperationError,
)
from cad_mcp.engineering.block_audit import (
    build_block_audit,
    find_block_spec,
    load_block_audit_manifest,
)


_PLOT_TYPE = {
    "display": 0, "extents": 1, "limits": 2, "view": 3, "window": 4, "layout": 5,
}
_PLOT_TYPE_R = {value: key for key, value in _PLOT_TYPE.items()}
_PAPER_UNITS = {"inches": 0, "millimeters": 1, "pixels": 2}
_PAPER_UNITS_R = {value: key for key, value in _PAPER_UNITS.items()}
_ROTATION = {0: 0, 90: 1, 180: 2, 270: 3}
_ROTATION_R = {value: key for key, value in _ROTATION.items()}
# ActiveX AcPlotScale, not AcViewportScale or .NET StandardScaleType.
# Values 1..15 are architectural imperial scales; metric ratios start at 16.
_STANDARD_SCALE = {
    "fit": 0,
    "1:1": 16,
    "1:2": 17,
    "1:4": 18,
    "1:5": 19,
    "1:8": 20,
    "1:10": 21,
    "1:16": 22,
    "1:20": 23,
    "1:30": 24,
    "1:40": 25,
    "1:50": 26,
    "1:100": 27,
    "2:1": 28,
    "4:1": 29,
    "8:1": 30,
    "10:1": 31,
    "100:1": 32,
}
_STANDARD_SCALE_R = {value: key for key, value in _STANDARD_SCALE.items()}
_INSUNITS = {4: "millimeters"}


def _pt(x: float, y: float, z: float = 0.0):
    return win32com.client.VARIANT(
        pythoncom.VT_ARRAY | pythoncom.VT_R8, [float(x), float(y), float(z)]
    )


def _dispatch_array(objects: Iterable[Any]):
    # A block Dispatch is also a Python sequence. Passing that wrapper lets
    # pywin32 infer a nested SAFEARRAY; raw IDispatch pointers stay one-dimensional.
    native = [getattr(item, "_obj", item) for item in objects]
    native = [getattr(item, "_oleobj_", item) for item in native]
    return win32com.client.VARIANT(
        pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, native
    )


def _casefold_set(values: Optional[Iterable[Any]]) -> set[str]:
    return {str(item).casefold() for item in (values or [])}


def _safe_get(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name)
    except Exception:
        return default


class AcadDwtMixin:
    """Native-DWG DWT operations mixed into ``AcadBackend``."""

    def _activate_layout(self, layout: Any) -> str:
        """Set Document.ActiveLayout and verify the exact layout by name."""
        try:
            expected = str(layout.Name)
            self.doc.ActiveLayout = layout
            actual = str(self.doc.ActiveLayout.Name)
        except Exception as exc:
            raise RuntimeError("无法通过 Document.ActiveLayout 切换布局") from exc
        if actual.casefold() != expected.casefold():
            raise RuntimeError(
                f"布局切换后读回不一致，预期 {expected!r}，实际 {actual!r}")
        return actual

    def dwt_target_identity(self) -> Dict[str, Any]:
        self._check_doc()
        path: Optional[Path] = self.current_path
        try:
            full_name = str(self.doc.FullName)
            if full_name:
                path = Path(full_name)
        except Exception:
            pass
        resolved = str(path.resolve()) if path else None
        name = path.name if path else str(_safe_get(self.doc, "Name", "未保存.dwg"))
        return {"mode": "acad", "path": resolved, "name": name}

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
        variables = {
            "INSUNITS": 4,
            "MEASUREMENT": 1,
            "LUNITS": 2,
            "LUPREC": int(linear_precision),
            "AUNITS": 0,
            "AUPREC": int(angular_precision),
            "DIMASSOC": 2 if associative_dimensions else 1,
            "MIRRTEXT": 1 if mirror_text else 0,
            "LWDISPLAY": 1 if display_lineweights else 0,
        }
        for name, value in variables.items():
            self.doc.SetVariable(name, value)
        after = self.get_drawing_settings()
        failed = [name for name, value in requested.items() if after.get(name) != value]
        if failed:
            raise RuntimeError("AutoCAD 图纸设置写入后读回不一致: " + ", ".join(failed))
        changed = [name for name, value in requested.items() if before.get(name) != value]
        return {
            "mode": "acad",
            "before": before,
            "after": after,
            "changed": changed,
            "unchanged": [name for name in requested if name not in changed],
        }

    def _active_dimstyle_payload(self) -> Dict[str, Any]:
        dimzin = int(self.doc.GetVariable("DIMZIN"))
        separator = self.doc.GetVariable("DIMDSEP")
        try:
            separator = chr(int(separator))
        except (TypeError, ValueError, OverflowError):
            separator = str(separator)
        return {
            "text_style": str(self.doc.GetVariable("DIMTXSTY")),
            "text_height": float(self.doc.GetVariable("DIMTXT")),
            "overall_scale": float(self.doc.GetVariable("DIMSCALE")),
            "linear_scale_factor": float(self.doc.GetVariable("DIMLFAC")),
            "precision": int(self.doc.GetVariable("DIMDEC")),
            "oblique_stroke_size": float(self.doc.GetVariable("DIMTSZ")),
            "arrow_size": float(self.doc.GetVariable("DIMASZ")),
            "text_gap": float(self.doc.GetVariable("DIMGAP")),
            "text_above": bool(int(self.doc.GetVariable("DIMTAD"))),
            "suppress_leading_zeros": bool(dimzin & 4),
            "suppress_trailing_zeros": bool(dimzin & 8),
            "decimal_separator": separator,
            "extension_line_offset": float(self.doc.GetVariable("DIMEXO")),
            "extension_line_extension": float(self.doc.GetVariable("DIMEXE")),
            "baseline_spacing": float(self.doc.GetVariable("DIMDLI")),
            "dimension_line_color": int(self.doc.GetVariable("DIMCLRD")),
            "extension_line_color": int(self.doc.GetVariable("DIMCLRE")),
            "text_color": int(self.doc.GetVariable("DIMCLRT")),
            "dimension_line_lineweight": int(self.doc.GetVariable("DIMLWD")),
            "extension_line_lineweight": int(self.doc.GetVariable("DIMLWE")),
        }

    def list_dimstyles(self) -> Dict[str, Any]:
        """Read the current style completely and mark inaccessible noncurrent values."""
        self._check_doc()
        current = str(self.doc.ActiveDimStyle.Name)
        current_values = self._active_dimstyle_payload()
        rows: List[Dict[str, Any]] = []
        unverified: List[str] = []
        fields = tuple(current_values)
        for index in range(self.doc.DimStyles.Count):
            style = self.doc.DimStyles.Item(index)
            name = str(style.Name)
            is_current = name.casefold() == current.casefold()
            values = dict(current_values) if is_current else {field: None for field in fields}
            if not is_current:
                unverified.extend(f"dimension_styles.{name}.{field}" for field in fields)
            rows.append({"name": name, **values, "is_current": is_current})
        return {
            "mode": "acad",
            "dimstyles": sorted(rows, key=lambda item: item["name"].casefold()),
            "unverified_fields": sorted(set(unverified)),
            "note": (
                "ActiveX 不能无副作用地激活并读取每个非当前 DIMSTYLE；"
                "当前样式完整读回，其他样式仅返回名称并标记未验证"
            ),
        }

    @staticmethod
    def _plot_scale_payload(plot: Any, unverified: List[str], prefix: str) -> Dict[str, Any]:
        try:
            use_standard = bool(plot.UseStandardScale)
        except Exception:
            unverified.append(f"{prefix}.plot_scale")
            return {"mode": None, "standard_scale": None, "numerator": None, "denominator": None}
        if use_standard:
            try:
                value = int(plot.StandardScale)
                if value == 0:
                    return {"mode": "fit", "standard_scale": None, "numerator": None, "denominator": None}
                name = _STANDARD_SCALE_R.get(value)
                if name is None:
                    unverified.append(f"{prefix}.plot_scale.standard_scale")
                return {"mode": "standard", "standard_scale": name, "numerator": None, "denominator": None}
            except Exception:
                unverified.append(f"{prefix}.plot_scale")
                return {"mode": "standard", "standard_scale": None, "numerator": None, "denominator": None}
        try:
            numerator, denominator = plot.GetCustomScale()
            return {
                "mode": "custom",
                "standard_scale": None,
                "numerator": float(numerator),
                "denominator": float(denominator),
            }
        except Exception:
            unverified.append(f"{prefix}.plot_scale")
            return {"mode": "custom", "standard_scale": None, "numerator": None, "denominator": None}

    def _plot_payload(self, plot: Any, *, name: Optional[str] = None) -> tuple[Dict[str, Any], List[str]]:
        prefix = f"page_setups.{name}" if name else "plot"
        unverified: List[str] = []

        def read(field: str, property_name: str, mapper=None):
            try:
                value = getattr(plot, property_name)
                return mapper(value) if mapper else value
            except Exception:
                unverified.append(f"{prefix}.{field}")
                return None

        payload = {
            "device": read("device", "ConfigName", str),
            "media_name": read("media_name", "CanonicalMediaName", str),
            "rotation": read("rotation", "PlotRotation", lambda value: _ROTATION_R.get(int(value))),
            "plot_style_sheet": read("plot_style_sheet", "StyleSheet", str),
            "plot_type": read("plot_type", "PlotType", lambda value: _PLOT_TYPE_R.get(int(value))),
            "paper_units": read("paper_units", "PaperUnits", lambda value: _PAPER_UNITS_R.get(int(value))),
            "plot_scale": self._plot_scale_payload(plot, unverified, prefix),
            "center_plot": read("center_plot", "CenterPlot", bool),
            "plot_with_lineweights": read("plot_with_lineweights", "PlotWithLineweights", bool),
            "plot_with_plot_styles": read("plot_with_plot_styles", "PlotWithPlotStyles", bool),
        }
        if name is not None:
            payload = {"name": name, **payload}
        return payload, unverified

    def list_page_setups(self) -> Dict[str, Any]:
        self._check_doc()
        rows, unverified = [], []
        configurations = self.doc.PlotConfigurations
        for index in range(configurations.Count):
            item = configurations.Item(index)
            payload, missing = self._plot_payload(item, name=str(item.Name))
            rows.append(payload)
            unverified.extend(missing)
        return {
            "mode": "acad",
            "page_setups": sorted(rows, key=lambda item: item["name"].casefold()),
            "unverified_fields": sorted(set(unverified)),
        }

    def _matching_page_setup_name(self, layout_payload: Mapping[str, Any]) -> Optional[str]:
        comparable = {
            key: layout_payload.get(key)
            for key in (
                "device", "media_name", "rotation", "plot_style_sheet", "plot_type",
                "paper_units", "plot_scale", "center_plot", "plot_with_lineweights",
                "plot_with_plot_styles",
            )
        }
        matches = []
        for item in self.list_page_setups()["page_setups"]:
            if all(item.get(key) == value for key, value in comparable.items()):
                matches.append(item["name"])
        return matches[0] if len(matches) == 1 else None

    def list_layouts_detailed(self) -> Dict[str, Any]:
        self._check_doc()
        rows: List[Dict[str, Any]] = []
        unverified: List[str] = []
        for index in range(self.doc.Layouts.Count):
            layout = self.doc.Layouts.Item(index)
            name = str(layout.Name)
            payload, missing = self._plot_payload(layout)
            unverified.extend(f"layouts.{name}.{item.split('.', 1)[-1]}" for item in missing)
            viewport_count = 0
            entity_count = 0
            try:
                block = layout.Block
                entity_count = int(block.Count)
                for entity_index in range(block.Count):
                    if str(block.Item(entity_index).EntityName) == "AcDbViewport":
                        viewport_count += 1
            except Exception:
                unverified.append(f"layouts.{name}.viewport_count")
                viewport_count = None
            row = {
                "layout_name": name,
                "is_model": bool(_safe_get(layout, "ModelType", name.casefold() == "model")),
                **payload,
                "page_setup": None,
                "viewport_count": viewport_count,
                "user_content_count": (
                    entity_count - viewport_count + max(0, viewport_count - 1)
                    if viewport_count is not None else None
                ),
            }
            try:
                row["page_setup"] = self._matching_page_setup_name(row)
            except Exception:
                unverified.append(f"layouts.{name}.page_setup")
            rows.append(row)
        return {
            "mode": "acad",
            "layouts": sorted(rows, key=lambda item: item["layout_name"].casefold()),
            "unverified_fields": sorted(set(unverified)),
        }

    def _available_font_files(self) -> Optional[List[str]]:
        try:
            files = self.acad.Preferences.Files
            candidates = []
            for property_name in ("SupportPath", "FontsPath"):
                raw = _safe_get(files, property_name, "")
                candidates.extend(item for item in str(raw).split(";") if item)
            windows_dir = os.environ.get("WINDIR")
            if windows_dir:
                candidates.append(str(Path(windows_dir) / "Fonts"))
            result = set()
            for directory in candidates:
                path = Path(os.path.expandvars(directory)).expanduser()
                if not path.is_dir():
                    continue
                for item in path.iterdir():
                    if item.is_file() and item.suffix.casefold() in {".shx", ".ttf", ".otf"}:
                        result.add(item.name)
            return sorted(result, key=str.casefold)
        except Exception:
            return None

    def get_plot_environment(self) -> Dict[str, Any]:
        """Read actual resources without switching the target layout's device."""
        self._check_doc()
        settings = self.get_drawing_settings()
        layout = self.doc.ActiveLayout
        unverified: List[str] = []

        def call(name: str):
            try:
                return sorted({str(item) for item in getattr(layout, name)()}, key=str.casefold)
            except Exception:
                unverified.append(name)
                return None

        devices = call("GetPlotDeviceNames")
        styles = call("GetPlotStyleTableNames")
        default_device = _safe_get(layout, "ConfigName", None)
        media_by_device: Optional[Dict[str, List[str]]] = None
        if default_device:
            media = call("GetCanonicalMediaNames")
            if media is not None:
                media_by_device = {str(default_device): media}
                for device in devices or []:
                    if device.casefold() != str(default_device).casefold():
                        unverified.append(f"media_by_device.{device}")
        else:
            unverified.append("default_device")
            unverified.append("media_by_device")
        try:
            search_path = str(self.acad.Preferences.Files.PrinterStyleSheetPath)
        except Exception:
            search_path = None
            unverified.append("plot_style_search_path")
        return {
            "mode": "acad",
            "plot_style_mode": settings.get("plot_style_mode"),
            "plot_style_search_path": search_path,
            "available_style_sheets": styles,
            "available_devices": devices,
            "media_by_device": media_by_device,
            "default_device": str(default_device) if default_device else None,
            "unverified_fields": sorted(set(unverified)),
            "note": (
                "只枚举当前设备的纸张；为保持只读，不临时切换布局设备。"
                "创建页面设置时会在待写入对象上再次核验对应纸张。"
            ),
        }

    def convert_plot_style_mode(
        self,
        target_mode: str,
        expected_current_mode: Optional[str] = None,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        self._check_doc()
        before = self.get_drawing_settings().get("plot_style_mode")
        if expected_current_mode and before != expected_current_mode:
            raise ConflictError(
                "expected_current_mode 与当前 PSTYLEMODE 不一致，已停止",
                details={"expected": expected_current_mode, "actual": before},
            )
        if before == target_mode:
            return {
                "mode": "acad",
                "dry_run": dry_run,
                "before": before,
                "after": before,
                "changed": False,
                "verified": True,
            }
        details = {
            "mode": "acad",
            "dry_run": dry_run,
            "before": before,
            "target": target_mode,
            "changed": False,
            "supported": False,
            "reason": (
                "CONVERTPSTYLES 可能打开交互界面，且 STB 转 CTB 会丢失命名样式关联；"
                "ActiveX 没有可验证的无对话框转换 API"
            ),
        }
        if dry_run:
            return details
        raise UnsupportedOperationError(
            "当前 AutoCAD API 无法可靠自动转换 CTB/STB；图纸未修改",
            details=details,
        )

    def rename_layout(self, old_name: str, new_name: str) -> Dict[str, Any]:
        self._check_doc()
        if old_name.casefold() == "model":
            raise ValueError("禁止重命名 Model 布局")
        names = {
            str(self.doc.Layouts.Item(index).Name).casefold()
            for index in range(self.doc.Layouts.Count)
        }
        if old_name.casefold() not in names:
            raise ValueError(f"布局不存在: {old_name}")
        if new_name.casefold() in names:
            raise ValueError(f"目标布局已存在: {new_name}")
        layout = self._get_layout(old_name)
        before = str(layout.Name)
        layout.Name = new_name
        after = str(layout.Name)
        if after != new_name:
            raise RuntimeError("布局改名后读回验证失败")
        return {"before": before, "after": after, "verified": True}

    def delete_layout(
        self,
        names: Sequence[str],
        *,
        require_empty: bool = True,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        self._check_doc()
        results = []
        for requested in names:
            rows = self.list_layouts_detailed()["layouts"]
            paper_layouts = [row for row in rows if not row["is_model"]]
            item = next(
                (row for row in rows if row["layout_name"].casefold() == requested.casefold()),
                None,
            )
            reason = None
            if requested.casefold() == "model":
                reason = "保护布局 Model"
            elif item is None:
                reason = "布局不存在"
            elif len(paper_layouts) <= 1:
                reason = "不能删除最后一个图纸空间布局"
            elif require_empty and int(item.get("user_content_count") or 0) > 0:
                reason = "布局含用户视口或内容"
            if reason:
                results.append({"name": requested, "status": "skipped", "reason": reason})
                continue
            if dry_run:
                results.append({"name": requested, "status": "would_delete"})
            else:
                self._get_layout(item["layout_name"]).Delete()
                remaining = {
                    str(self.doc.Layouts.Item(index).Name).casefold()
                    for index in range(self.doc.Layouts.Count)
                }
                if requested.casefold() in remaining:
                    raise RuntimeError(f"布局 {requested} 删除后仍存在")
                results.append({"name": requested, "status": "deleted"})
        return {"mode": "acad", "dry_run": dry_run, "results": results}

    def rename_layer(self, old_name: str, new_name: str) -> Dict[str, Any]:
        self._check_doc()
        if old_name.casefold() in {"0", "defpoints"}:
            raise ValueError("禁止重命名保护图层 0 或 Defpoints")
        if not self._layer_exists(old_name):
            raise ValueError(f"图层不存在: {old_name}")
        if self._layer_exists(new_name):
            raise ValueError(f"目标图层已存在: {new_name}")
        layer = self._get_layer(old_name)
        before = str(layer.Name)
        layer.Name = new_name
        after = str(layer.Name)
        if after != new_name or self._layer_exists(old_name):
            raise RuntimeError("图层改名后读回验证失败")
        if self.active_layer.casefold() == old_name.casefold():
            self.active_layer = new_name
        return {"before": before, "after": after, "verified": True}

    def _entities_on_layer(self, name: str) -> List[Any]:
        entities = []
        for block_index in range(self.doc.Blocks.Count):
            block = self.doc.Blocks.Item(block_index)
            pending = []
            for entity_index in range(block.Count):
                entity = block.Item(entity_index)
                try:
                    if str(entity.Layer).casefold() == name.casefold():
                        pending.append(entity)
                except Exception:
                    continue
            entities.extend(pending)
        return entities

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
        details = {
            item["name"].casefold(): item
            for item in self.list_layers_detailed()["layers"]
        }
        results = []
        for requested in names:
            item = details.get(requested.casefold())
            reason = None
            if requested.casefold() in {"0", "defpoints"}:
                reason = "保护图层"
            elif item is None:
                reason = "图层不存在"
            elif item.get("is_current"):
                reason = "当前图层"
            elif int(item.get("entity_count") or 0) > 0 and not allow_nonempty:
                reason = "图层非空；只有 allow_nonempty=true 才允许迁移实体后删除"
            if reason:
                results.append({"name": requested, "status": "skipped", "reason": reason})
                continue
            count = int(item.get("entity_count") or 0)
            if dry_run:
                results.append({
                    "name": requested,
                    "status": "would_delete",
                    "entity_count": count,
                    "would_move_to_layer_0": count if allow_nonempty else 0,
                })
                continue
            moved = 0
            if count:
                entities = self._entities_on_layer(item["name"])
                for entity in entities:
                    entity.Layer = "0"
                    moved += 1
            self._get_layer(item["name"]).Delete()
            if self._layer_exists(item["name"]):
                raise RuntimeError(f"图层 {requested} 删除后仍存在")
            results.append({
                "name": requested,
                "status": "deleted",
                "moved_to_layer_0": moved,
            })
        return {"mode": "acad", "dry_run": dry_run, "results": results}

    @staticmethod
    def _normalize_plot_scale(plot_scale: Mapping[str, Any]) -> Dict[str, Any]:
        mode = str(plot_scale.get("mode", "standard"))
        if mode == "fit":
            return {"mode": "fit", "standard_scale": None, "numerator": None, "denominator": None}
        if mode == "standard":
            name = str(plot_scale.get("standard_scale") or "1:1")
            if name not in _STANDARD_SCALE:
                raise ValueError(
                    "不支持的标准出图比例；请改用 custom 并给出 numerator/denominator"
                )
            return {"mode": "standard", "standard_scale": name, "numerator": None, "denominator": None}
        numerator = float(plot_scale.get("numerator"))
        denominator = float(plot_scale.get("denominator"))
        if numerator <= 0 or denominator <= 0:
            raise ValueError("自定义出图比例的 numerator/denominator 必须大于 0")
        return {"mode": "custom", "standard_scale": None, "numerator": numerator, "denominator": denominator}

    @staticmethod
    def _validate_center_plot(plot_type: str, center_plot: bool) -> None:
        if plot_type == "layout" and bool(center_plot):
            raise ValueError("acLayout 打印范围不支持 CenterPlot=true；请使用 center_plot=false")

    def _write_plot_object(
        self,
        plot: Any,
        *,
        device: str,
        media_name: str,
        rotation: int,
        plot_style_sheet: str,
        plot_type: str,
        paper_units: str,
        plot_scale: Mapping[str, Any],
        center_plot: bool,
        plot_with_lineweights: bool,
        plot_with_plot_styles: bool,
    ) -> None:
        self._validate_center_plot(plot_type, center_plot)
        environment = self.get_plot_environment()
        devices = _casefold_set(environment.get("available_devices"))
        styles = _casefold_set(environment.get("available_style_sheets"))
        if not devices or device.casefold() not in devices:
            raise ValueError(f"AutoCAD 实际打印设备不存在: {device}")
        if plot_style_sheet and (not styles or plot_style_sheet.casefold() not in styles):
            raise ValueError(f"AutoCAD 实际打印样式表不存在: {plot_style_sheet}")
        plot.ConfigName = device
        plot.RefreshPlotDeviceInfo()
        try:
            media = _casefold_set(plot.GetCanonicalMediaNames())
        except Exception as exc:
            raise RuntimeError(f"无法读取设备 {device} 的实际纸张列表") from exc
        if media_name.casefold() not in media:
            raise ValueError(f"打印设备 {device} 不支持纸张 {media_name}")
        plot.CanonicalMediaName = media_name
        plot.PlotRotation = _ROTATION[int(rotation)]
        plot.StyleSheet = plot_style_sheet
        plot.PlotType = _PLOT_TYPE[plot_type]
        plot.PaperUnits = _PAPER_UNITS[paper_units]
        if plot_type != "layout":
            plot.CenterPlot = bool(center_plot)
        plot.PlotWithLineweights = bool(plot_with_lineweights)
        plot.PlotWithPlotStyles = bool(plot_with_plot_styles)
        scale = self._normalize_plot_scale(plot_scale)
        if scale["mode"] == "fit":
            plot.UseStandardScale = True
            plot.StandardScale = _STANDARD_SCALE["fit"]
        elif scale["mode"] == "standard":
            plot.UseStandardScale = True
            plot.StandardScale = _STANDARD_SCALE[scale["standard_scale"]]
        else:
            plot.UseStandardScale = False
            plot.SetCustomScale(scale["numerator"], scale["denominator"])

    def create_or_update_page_setup(
        self,
        *,
        name: str,
        device: str,
        media_name: str,
        rotation: int,
        plot_style_sheet: str,
        plot_type: str,
        paper_units: str,
        plot_scale: Mapping[str, Any],
        center_plot: bool,
        plot_with_lineweights: bool,
        plot_with_plot_styles: bool,
        replace: bool = False,
    ) -> Dict[str, Any]:
        self._check_doc()
        self._validate_center_plot(plot_type, center_plot)
        configurations = self.doc.PlotConfigurations
        existing = None
        try:
            existing = configurations.Item(name)
        except Exception:
            pass
        desired = {
            "device": device,
            "media_name": media_name,
            "rotation": rotation,
            "plot_style_sheet": plot_style_sheet,
            "plot_type": plot_type,
            "paper_units": paper_units,
            "plot_scale": self._normalize_plot_scale(plot_scale),
            "center_plot": center_plot,
            "plot_with_lineweights": plot_with_lineweights,
            "plot_with_plot_styles": plot_with_plot_styles,
        }
        if existing is not None:
            before, _ = self._plot_payload(existing, name=name)
            if all(before.get(field) == value for field, value in desired.items()):
                return {"mode": "acad", "created": False, "changed": False, "page_setup": before}
            if not replace:
                raise FileExistsError(
                    f"页面设置 {name} 已存在且与请求不同；replace=false，未覆盖"
                )
            plot = existing
            created = False
        else:
            plot = configurations.Add(name, False)
            created = True
        self._write_plot_object(plot, **desired)
        after, unverified = self._plot_payload(plot, name=name)
        mismatched = {
            field: {"expected": value, "actual": after.get(field)}
            for field, value in desired.items()
            if after.get(field) != value
        }
        if mismatched or unverified:
            raise RuntimeError(
                f"页面设置 {name} 写入后完整读回失败: {mismatched or unverified}"
            )
        return {
            "mode": "acad",
            "created": created,
            "changed": True,
            "page_setup": after,
            "verified": True,
        }

    def _object_from_api_handle(self, handle: str):
        try:
            hex_handle = format(int(handle), "X")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"句柄必须是十进制字符串: {handle}") from exc
        try:
            return self.doc.HandleToObject(hex_handle)
        except Exception:
            entity = self._find(handle)
            if entity is None:
                raise ValueError(f"找不到句柄: {handle}")
            return entity

    def _block_reference_count(self, block_name: str) -> int:
        count = 0
        for block_index in range(self.doc.Blocks.Count):
            owner = self.doc.Blocks.Item(block_index)
            for entity_index in range(owner.Count):
                entity = owner.Item(entity_index)
                if str(_safe_get(entity, "EntityName", "")) != "AcDbBlockReference":
                    continue
                name = str(_safe_get(entity, "EffectiveName", "") or _safe_get(entity, "Name", ""))
                if name.casefold() == block_name.casefold():
                    count += 1
        return count

    def _create_annotative_attribute_block(
        self,
        block_name: str,
        base_point: Sequence[float],
        entity_handles: Sequence[str],
        attribute_definitions: Sequence[Mapping[str, Any]],
        replace: bool,
    ) -> Dict[str, Any]:
        # Public calls enter through _AcadLockProxy, which owns one undo group.
        if not attribute_definitions:
            raise ValueError("注释性属性块至少需要一个属性定义")
        exists = self._block_exists(block_name)
        if exists and not replace:
            raise FileExistsError(f"块定义已存在，未覆盖: {block_name}")
        if exists:
            references = self._block_reference_count(block_name)
            if references:
                raise ConflictError(
                    f"块 {block_name} 已有 {references} 个引用，不能安全替换定义",
                    details={"block_name": block_name, "reference_count": references},
                )

        point = list(base_point)
        while len(point) < 3:
            point.append(0.0)
        sources = []
        source_handles = set()
        for handle in entity_handles:
            entity = self._find(handle)
            if entity is None:
                raise ValueError(f"图元句柄不存在于模型空间: {handle}")
            hex_handle = str(entity.Handle).upper()
            if hex_handle in source_handles:
                raise ValueError(f"图元句柄重复: {handle}")
            source_handles.add(hex_handle)
            sources.append(entity)

        definitions = []
        tags = set()
        for definition in attribute_definitions:
            tag = str(definition["tag"]).upper()
            if not tag or any(char.isspace() or char == "!" for char in tag):
                raise ValueError(f"属性标签包含不允许的字符: {tag}")
            if tag.casefold() in tags:
                raise ValueError(f"属性标签重复: {tag}")
            tags.add(tag.casefold())
            style = definition.get("text_style")
            if style:
                try:
                    self.doc.TextStyles.Item(str(style))
                except Exception as exc:
                    raise ValueError(f"文字样式不存在: {style}") from exc
            position = list(definition.get("position", (0.0, 0.0, 0.0)))
            while len(position) < 3:
                position.append(0.0)
            mode = (
                (1 if definition.get("invisible") else 0)
                | (2 if definition.get("constant") else 0)
                | (4 if definition.get("verify") else 0)
                | (8 if definition.get("preset") else 0)
            )
            definitions.append((tag, definition, position, mode, style))

        temporary_handles = []
        for source in sources:
            copied = source.Copy()
            temporary_handles.append(self._handle_str(copied.Handle))
        for tag, definition, position, mode, style in definitions:
            attribute = self.msp.AddAttribute(
                float(definition.get("text_height", 2.5)),
                mode,
                str(definition.get("prompt", "")),
                _pt(position[0], position[1], position[2]),
                tag,
                str(definition.get("default_value", "")),
            )
            attribute.Rotation = math.radians(float(definition.get("rotation", 0.0)))
            if style:
                attribute.StyleName = str(style)
            temporary_handles.append(self._handle_str(attribute.Handle))

        if exists:
            self.doc.Blocks.Item(block_name).Delete()
        native = self._make_annotative_block(
            block_name, temporary_handles, point[0], point[1])
        if native.get("verified") is not True or native.get("annotative") is not True:
            raise RuntimeError("原生注释性块创建结果未通过验证")

        block = self.doc.Blocks.Item(block_name)
        read_attributes = {}
        for index in range(block.Count):
            entity = block.Item(index)
            if str(_safe_get(entity, "EntityName", "")) != "AcDbAttributeDefinition":
                continue
            tag = str(entity.TagString).upper()
            if tag in read_attributes:
                raise RuntimeError(f"块 {block_name} 中属性标签重复: {tag}")
            read_attributes[tag] = {
                "constant": bool(entity.Constant),
                "default_value": str(entity.TextString),
                "text_height": float(entity.Height),
            }
        expected_tags = {tag for tag, *_ in definitions}
        if set(read_attributes) != expected_tags:
            raise RuntimeError(f"块 {block_name} 的属性标签读回不一致")
        for tag, definition, *_ in definitions:
            actual = read_attributes[tag]
            if actual["constant"] != bool(definition.get("constant", False)):
                raise RuntimeError(f"块 {block_name} 的属性 {tag} 可编辑状态读回不一致")
            if actual["default_value"] != str(definition.get("default_value", "")):
                raise RuntimeError(f"块 {block_name} 的属性 {tag} 默认值读回不一致")
            if not math.isclose(
                actual["text_height"], float(definition.get("text_height", 2.5)),
                rel_tol=0.0, abs_tol=1e-6,
            ):
                raise RuntimeError(f"块 {block_name} 的属性 {tag} 字高读回不一致")

        reference = self.msp.InsertBlock(
            _pt(point[0], point[1], point[2]), block_name, 1.0, 1.0, 1.0, 0.0)
        try:
            editable_tags = {
                str(attribute.TagString).upper()
                for attribute in reference.GetAttributes()
            }
            expected_editable = {
                tag for tag, definition, *_ in definitions
                if not definition.get("constant", False)
            }
            if editable_tags != expected_editable:
                raise RuntimeError(f"块 {block_name} 的可编辑属性引用读回不一致")
        finally:
            reference.Delete()
        return {
            "mode": "acad",
            "block_name": block_name,
            "created": not exists,
            "replaced": exists,
            "base_point": point,
            "entity_handles_copied": list(entity_handles),
            "attribute_tags": sorted(read_attributes, key=str.casefold),
            "annotative": True,
            "verified": True,
            "readback": {
                **native["readback"],
                "attributes": read_attributes,
                "editable_tags": sorted(editable_tags, key=str.casefold),
            },
        }

    def create_or_update_attribute_block(
        self,
        block_name: str,
        base_point: Sequence[float],
        entity_handles: Sequence[str],
        attribute_definitions: Sequence[Mapping[str, Any]],
        replace: bool = False,
        annotative: bool = False,
    ) -> Dict[str, Any]:
        self._check_doc()
        if annotative:
            return self._create_annotative_attribute_block(
                block_name, base_point, entity_handles, attribute_definitions, replace)
        exists = self._block_exists(block_name)
        if exists and not replace:
            raise FileExistsError(f"块定义已存在，未覆盖: {block_name}")
        if exists:
            references = self._block_reference_count(block_name)
            if references:
                raise ConflictError(
                    f"块 {block_name} 已有 {references} 个引用，不能安全替换定义",
                    details={"block_name": block_name, "reference_count": references},
                )
            self.doc.Blocks.Item(block_name).Delete()
        point = list(base_point)
        while len(point) < 3:
            point.append(0.0)
        block = self.doc.Blocks.Add(_pt(point[0], point[1], point[2]), block_name)
        objects = []
        for handle in entity_handles:
            objects.append(self._object_from_api_handle(handle))
        if objects:
            self.doc.CopyObjects(_dispatch_array(objects), block)
        tags = set()
        for definition in attribute_definitions:
            tag = str(definition["tag"]).upper()
            if tag in tags:
                raise ValueError(f"属性标签重复: {tag}")
            tags.add(tag)
            mode = (
                (1 if definition.get("invisible") else 0)
                | (2 if definition.get("constant") else 0)
                | (4 if definition.get("verify") else 0)
                | (8 if definition.get("preset") else 0)
            )
            position = list(definition.get("position", (0.0, 0.0, 0.0)))
            while len(position) < 3:
                position.append(0.0)
            attribute = block.AddAttribute(
                float(definition.get("text_height", 2.5)),
                mode,
                str(definition.get("prompt", "")),
                _pt(position[0], position[1], position[2]),
                tag,
                str(definition.get("default_value", "")),
            )
            attribute.Rotation = math.radians(float(definition.get("rotation", 0.0)))
            text_style = definition.get("text_style")
            if text_style:
                try:
                    self.doc.TextStyles.Item(str(text_style))
                except Exception as exc:
                    raise ValueError(f"文字样式不存在: {text_style}") from exc
                attribute.StyleName = str(text_style)
        read_tags = []
        for index in range(block.Count):
            entity = block.Item(index)
            if str(_safe_get(entity, "EntityName", "")) == "AcDbAttributeDefinition":
                read_tags.append(str(entity.TagString))
        if {tag.casefold() for tag in read_tags} != {tag.casefold() for tag in tags}:
            raise RuntimeError("属性块创建后标签读回不一致")
        return {
            "mode": "acad",
            "block_name": block_name,
            "created": not exists,
            "replaced": exists,
            "base_point": point,
            "entity_handles_copied": list(entity_handles),
            "attribute_tags": sorted(read_tags, key=str.casefold),
            "verified": True,
        }

    def set_block_attributes(self, block_handle: str, values: Mapping[str, str]) -> Dict[str, Any]:
        self._check_doc()
        block = self._object_from_api_handle(block_handle)
        if str(_safe_get(block, "EntityName", "")) != "AcDbBlockReference":
            raise ValueError(f"句柄不是块引用: {block_handle}")
        attributes = list(block.GetAttributes()) if bool(_safe_get(block, "HasAttributes", False)) else []
        by_tag = {str(attribute.TagString).casefold(): attribute for attribute in attributes}
        updated, unchanged, missing = [], [], []
        for tag, value in values.items():
            attribute = by_tag.get(tag.casefold())
            if attribute is None:
                missing.append(tag)
            elif str(attribute.TextString) == str(value):
                unchanged.append(tag)
            else:
                attribute.TextString = str(value)
                attribute.Update()
                if str(attribute.TextString) != str(value):
                    raise RuntimeError(f"属性 {tag} 写入后读回不一致")
                updated.append(tag)
        return {
            "mode": "acad",
            "block_handle": block_handle,
            "updated": updated,
            "missing_tags": missing,
            "unchanged": unchanged,
        }

    @staticmethod
    def _field_object_id(document: Any, source: Any) -> str:
        try:
            return str(document.Utility.GetObjectIdString(source, False))
        except Exception:
            return str(source.ObjectID)

    @staticmethod
    def _field_property(source: Any, requested: str) -> tuple[str, Optional[int]]:
        allowed = {
            "x": ("InsertionPoint", 1),
            "y": ("InsertionPoint", 2),
            "z": ("InsertionPoint", 4),
            "insertion_x": ("InsertionPoint", 1),
            "insertion_y": ("InsertionPoint", 2),
            "insertion_z": ("InsertionPoint", 4),
            "position_x": ("Position", 1),
            "position_y": ("Position", 2),
            "position_z": ("Position", 4),
            "center_x": ("Center", 1),
            "center_y": ("Center", 2),
            "center_z": ("Center", 4),
            "start_x": ("StartPoint", 1),
            "start_y": ("StartPoint", 2),
            "start_z": ("StartPoint", 4),
            "end_x": ("EndPoint", 1),
            "end_y": ("EndPoint", 2),
            "end_z": ("EndPoint", 4),
            "length": ("Length", None),
            "area": ("Area", None),
            "radius": ("Radius", None),
        }
        key = requested.casefold()
        if key not in allowed:
            raise ValueError(
                "source_property 不在安全白名单；可用 x/y/z、insertion_*/position_*/"
                "center_*/start_*/end_*、length、area、radius"
            )
        property_name, component = allowed[key]
        if _safe_get(source, property_name, None) is None:
            raise ValueError(
                f"源对象 {str(_safe_get(source, 'EntityName', 'UNKNOWN'))} 不提供属性 {property_name}"
            )
        return property_name, component

    def bind_block_attribute_field(
        self,
        *,
        block_handle: str,
        attribute_tag: str,
        source_type: str,
        source_handle: str,
        source_property: str,
        linear_scale_factor: float = 1.0,
        precision: int = 3,
        prefix: str = "",
        suffix: str = "",
        update_on_regen: bool = True,
    ) -> Dict[str, Any]:
        self._check_doc()
        if source_type != "object":
            raise UnsupportedOperationError(
                f"当前只支持 source_type=object，收到 {source_type!r}"
            )
        block = self._object_from_api_handle(block_handle)
        if str(_safe_get(block, "EntityName", "")) != "AcDbBlockReference":
            raise ValueError(f"句柄不是块引用: {block_handle}")
        attributes = list(block.GetAttributes()) if bool(_safe_get(block, "HasAttributes", False)) else []
        attribute = next(
            (item for item in attributes if str(item.TagString).casefold() == attribute_tag.casefold()),
            None,
        )
        if attribute is None:
            raise ValueError(f"块引用中不存在属性标签: {attribute_tag}")
        source = self._object_from_api_handle(source_handle)
        property_name, component = self._field_property(source, source_property)
        object_id = self._field_object_id(self.doc, source)
        format_parts = ["%lu2", f"%pr{int(precision)}"]
        if component is not None:
            format_parts.append(f"%pt{component}")
        if abs(float(linear_scale_factor) - 1.0) > 1e-12:
            format_parts.append(f"%ct8[{float(linear_scale_factor):.12g}]")
        expression = (
            f"{prefix}%<\\AcObjProp Object(%<\\_ObjId {object_id}>%)."
            f"{property_name} \\f \"{''.join(format_parts)}\">%{suffix}"
        )
        if len(expression) > 256:
            raise ValueError("字段表达式超过 AttributeReference.TextString 的 256 字符限制")
        attribute.TextString = expression
        attribute.Update()
        if update_on_regen:
            try:
                current = int(self.doc.GetVariable("FIELDEVAL"))
                self.doc.SetVariable("FIELDEVAL", current | 16)
            except Exception as exc:
                raise RuntimeError("无法启用 REGEN 字段更新位 FIELDEVAL=16") from exc
        self.doc.Regen(1)
        # TextString can be evaluated text or an unmaterialized literal expression;
        # neither proves the association. Always read the native FIELD and pointers.
        from cad_mcp.drawing.native_fields import read_attribute_field
        native = read_attribute_field(self, attribute)
        verified = ("\\AcObjProp" in native["code"]
                    and f".{property_name} " in native["code"]
                    and all(part in native["code"] for part in format_parts)
                    and (not prefix or prefix in native["code"])
                    and (not suffix or suffix in native["code"])
                    and {str(source.Handle).upper()} == set(native["references"]))
        if not verified:
            raise UnsupportedOperationError(
                "AutoCAD 未能从 AttributeReference 读回关联字段代码；已停止并回滚，未用静态文字替代",
                details={"attribute_tag": attribute_tag, "source_handle": source_handle},
            )
        return {
            "mode": "acad",
            "block_handle": block_handle,
            "attribute_tag": attribute_tag,
            "source_handle": source_handle,
            "source_property": property_name,
            "linear_scale_factor": float(linear_scale_factor),
            "precision": int(precision),
            "update_on_regen": update_on_regen,
            "field_expression_verified": True,
            "static_fallback": False,
        }

    def _open_source_document_readonly(self, source: Path):
        acad = self._connect()
        previous = _safe_get(acad, "ActiveDocument", None)
        opened_here = False
        document = None
        for index in range(acad.Documents.Count):
            candidate = acad.Documents.Item(index)
            try:
                if Path(str(candidate.FullName)).resolve() == source:
                    document = candidate
                    break
            except Exception:
                continue
        if document is None:
            from cad_mcp.drawing.document_guard import assert_safe_document_open
            assert_safe_document_open(acad)
            document = acad.Documents.Open(str(source), True)
            opened_here = True
        return document, opened_here, previous

    @staticmethod
    def _close_source_document(document: Any, opened_here: bool, previous: Any) -> None:
        if opened_here:
            document.Close(False)
        if previous is not None:
            try:
                previous.Activate()
            except Exception:
                pass

    def inspect_block_library(self, source_path: str) -> Dict[str, Any]:
        source = Path(os.path.expandvars(source_path)).expanduser()
        if not source.is_absolute() or source.suffix.casefold() != ".dwg":
            raise ValueError("source_path 必须是现有 DWG 的完整绝对路径")
        source = source.resolve()
        if not source.is_file():
            raise FileNotFoundError(f"DWG 块库不存在: {source}")
        before_hash = sha256_file(source)
        document, opened_here, previous = self._open_source_document_readonly(source)
        warnings: List[str] = []
        rows = []
        try:
            try:
                insertion_units = int(document.GetVariable("INSUNITS"))
                drawing_unit = _INSUNITS.get(insertion_units, f"insunits:{insertion_units}")
            except Exception:
                drawing_unit = None
                warnings.append("无法读取块库 INSUNITS")
            for block_index in range(document.Blocks.Count):
                block = document.Blocks.Item(block_index)
                name = str(block.Name)
                if name.startswith("*"):
                    continue
                tags, layers, textstyles = set(), set(), set()
                for entity_index in range(block.Count):
                    entity = block.Item(entity_index)
                    layer = _safe_get(entity, "Layer", None)
                    if layer:
                        layers.add(str(layer))
                    style = _safe_get(entity, "StyleName", None)
                    if style:
                        textstyles.add(str(style))
                    if str(_safe_get(entity, "EntityName", "")) == "AcDbAttributeDefinition":
                        tags.add(str(entity.TagString))
                origin = _safe_get(block, "Origin", None)
                base_point = [float(value) for value in origin] if origin is not None else None
                if base_point is None:
                    warnings.append(f"块 {name} 无法读取基点")
                rows.append({
                    "name": name,
                    "base_point": base_point,
                    "entity_count": int(block.Count),
                    "attribute_tags": sorted(tags, key=str.casefold),
                    "dynamic_parameters": [],
                    "dependent_layers": sorted(layers, key=str.casefold),
                    "dependent_textstyles": sorted(textstyles, key=str.casefold),
                })
            warnings.append(
                "ActiveX 不能从未插入的块定义可靠枚举动态块参数；dynamic_parameters 返回空并明确标记"
            )
        finally:
            self._close_source_document(document, opened_here, previous)
        after_hash = sha256_file(source)
        if before_hash != after_hash:
            raise RuntimeError("只读检查期间块库文件发生变化，结果已作废")
        return {
            "mode": "acad",
            "source_path": str(source),
            "file_hash": before_hash,
            "drawing_unit": drawing_unit,
            "block_names": sorted((item["name"] for item in rows), key=str.casefold),
            "blocks": sorted(rows, key=lambda item: item["name"].casefold()),
            "base_points": {item["name"]: item["base_point"] for item in rows},
            "entity_counts": {item["name"]: item["entity_count"] for item in rows},
            "attribute_tags": {item["name"]: item["attribute_tags"] for item in rows},
            "dynamic_parameters": {item["name"]: item["dynamic_parameters"] for item in rows},
            "dependent_layers": sorted({name for item in rows for name in item["dependent_layers"]}, key=str.casefold),
            "dependent_textstyles": sorted({name for item in rows for name in item["dependent_textstyles"]}, key=str.casefold),
            "warnings": warnings,
            "source_unchanged": True,
        }

    def audit_block_definition(
        self,
        *,
        source_path: str,
        block_name: str,
        manifest_path: str,
    ) -> Dict[str, Any]:
        """Audit one native DWG block against an explicit ``*_manifest.json`` card.

        The source drawing is opened read-only and hashed before and after the
        inspection.  Missing specification values are never synthesized.
        """

        source = Path(os.path.expandvars(source_path)).expanduser()
        if not source.is_absolute() or source.suffix.casefold() != ".dwg":
            raise ValueError("source_path 必须是现有 DWG 的完整绝对路径")
        source = source.resolve()
        if not source.is_file():
            raise FileNotFoundError(f"DWG 块库不存在: {source}")

        resolved_manifest_path, manifest = load_block_audit_manifest(manifest_path)
        declared_source = Path(manifest.source_dwg).resolve()
        if declared_source != source:
            raise ValueError(
                "块规格卡 source_dwg 与请求 source_path 不一致；"
                f"规格卡={declared_source}，请求={source}"
            )
        before_hash = sha256_file(source)
        if before_hash.casefold() != manifest.source_sha256.casefold():
            raise ValueError(
                "DWG 与块规格卡 source_sha256 不一致；"
                f"期望 {manifest.source_sha256}，实际 {before_hash}"
            )
        spec = find_block_spec(manifest, block_name)
        document, opened_here, previous = self._open_source_document_readonly(source)
        try:
            block = None
            available: List[str] = []
            for index in range(document.Blocks.Count):
                candidate = document.Blocks.Item(index)
                candidate_name = str(candidate.Name)
                if candidate_name.startswith("*"):
                    continue
                available.append(candidate_name)
                if candidate_name.casefold() == block_name.casefold():
                    block = candidate
            if block is None:
                raise ValueError(
                    f"DWG 中不存在指定块 {block_name!r}；"
                    "可用块: " + ", ".join(sorted(available, key=str.casefold))
                )
            audit = build_block_audit(block, document, spec)
        finally:
            self._close_source_document(document, opened_here, previous)
        after_hash = sha256_file(source)
        if before_hash != after_hash:
            raise RuntimeError("只读块审查期间 DWG 文件发生变化，结果已作废")
        return {
            "mode": "acad",
            "source_path": str(source),
            "source_hash": before_hash,
            "manifest_path": str(resolved_manifest_path),
            "manifest_schema_version": manifest.schema_version,
            "source_unchanged": True,
            **audit,
        }

    def import_block_definitions(
        self,
        *,
        source_path: str,
        block_names: Sequence[str],
        replace_existing: bool = False,
        expected_sha256: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._check_doc()
        source = Path(os.path.expandvars(source_path)).expanduser()
        if not source.is_absolute() or source.suffix.casefold() != ".dwg":
            raise ValueError("source_path 必须是现有 DWG 的完整绝对路径")
        source = source.resolve()
        if not source.is_file():
            raise FileNotFoundError(f"DWG 块库不存在: {source}")
        if not block_names:
            raise ValueError("block_names 不能为空")
        file_hash = sha256_file(source)
        if expected_sha256 is not None:
            expected = str(expected_sha256).lower()
            if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
                raise ValueError("expected_sha256 必须是 64 位十六进制 SHA-256")
            if file_hash.lower() != expected:
                raise ConflictError(
                    "块库文件与预期 SHA-256 不一致，未开始导入",
                    details={
                        "source_path": str(source),
                        "expected_sha256": expected,
                        "actual_sha256": file_hash,
                    },
                )
        document, opened_here, previous = self._open_source_document_readonly(source)
        imported, skipped = [], []
        try:
            try:
                source_units = int(document.GetVariable("INSUNITS"))
            except Exception as exc:
                raise RuntimeError("无法验证源 DWG 的 INSUNITS") from exc
            target_units = int(self.doc.GetVariable("INSUNITS"))
            if source_units != 4 or target_units != 4:
                raise ConflictError(
                    "块库导入要求源和目标均为毫米，已停止",
                    details={"source_insunits": source_units, "target_insunits": target_units},
                )
            source_names = {
                str(document.Blocks.Item(index).Name).casefold(): str(document.Blocks.Item(index).Name)
                for index in range(document.Blocks.Count)
                if not str(document.Blocks.Item(index).Name).startswith("*")
            }
            missing = [name for name in block_names if name.casefold() not in source_names]
            if missing:
                raise ValueError("源块库缺少块定义: " + ", ".join(missing))
            for requested in block_names:
                source_name = source_names[requested.casefold()]
                exists = self._block_exists(requested)
                if exists and not replace_existing:
                    skipped.append({"name": requested, "reason": "同名块已存在，replace_existing=false"})
                    continue
                if exists:
                    references = self._block_reference_count(requested)
                    if references:
                        raise ConflictError(
                            f"块 {requested} 已有 {references} 个引用，不能安全替换",
                            details={"block_name": requested, "reference_count": references},
                        )
                    self.doc.Blocks.Item(requested).Delete()
                source_block = document.Blocks.Item(source_name)
                # Supply the optional IDPairs as an empty in/out VARIANT.
                id_pairs = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_VARIANT, None)
                document.CopyObjects(_dispatch_array([source_block]), self.doc.Blocks, id_pairs)
                if not self._block_exists(requested):
                    raise RuntimeError(f"块定义 {requested} 复制后未在目标图纸中出现")
                imported.append(requested)
                self._yield_com()
        finally:
            self._close_source_document(document, opened_here, previous)
        final_hash = sha256_file(source)
        if final_hash != file_hash or (
            expected_sha256 is not None and final_hash.lower() != str(expected_sha256).lower()
        ):
            raise RuntimeError("导入期间源块库文件发生变化或不再匹配预期哈希，已停止并回滚")
        return {
            "mode": "acad",
            "source_path": str(source),
            "source_hash": file_hash,
            "source_drawing_unit": "millimeters",
            "target_drawing_unit": "millimeters",
            "imported": imported,
            "skipped": skipped,
            "replace_existing": replace_existing,
            "source_unchanged": True,
            "verified": all(self._block_exists(name) for name in imported),
        }

    def export_block_library(
        self,
        *,
        target_path: str,
        block_names: Sequence[str],
        replace: bool = False,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        self._check_doc()
        target = Path(os.path.expandvars(target_path)).expanduser()
        if not target.is_absolute() or target.suffix.casefold() != ".dwg":
            raise ValueError("target_path 必须是 DWG 完整绝对路径")
        target = target.resolve()
        if self.current_path and target == self.current_path.resolve():
            raise ValueError("块库目标不能是当前图纸；不会自动保存或覆盖当前 DWG/DWT")
        if not target.parent.is_dir():
            raise FileNotFoundError(f"目标目录不存在: {target.parent}")
        missing = [name for name in block_names if not self._block_exists(name)]
        if missing:
            raise ValueError("当前图纸缺少块定义: " + ", ".join(missing))
        if target.exists() and not replace:
            raise FileExistsError(f"目标块库已存在，replace=false，未覆盖: {target}")
        preview = {
            "mode": "acad",
            "dry_run": dry_run,
            "target_path": str(target),
            "block_names": list(block_names),
            "replace": replace,
            "target_exists": target.exists(),
            "would_write": not dry_run,
        }
        if dry_run:
            return preview
        temporary = target.with_name(f".{target.stem}.{uuid.uuid4().hex}.tmp.dwg")
        selection = None
        references = []
        backup_path = None
        target_existed = target.exists()
        old_hash = sha256_file(target) if target_existed else None
        target_replaced = False
        try:
            for index, name in enumerate(block_names):
                reference = self.msp.InsertBlock(
                    _pt(float(index) * 10.0, 0.0, 0.0), name, 1.0, 1.0, 1.0, 0.0
                )
                references.append(reference)
            selection_name = f"CAD_MCP_WBLOCK_{uuid.uuid4().hex[:12]}"
            selection = self.doc.SelectionSets.Add(selection_name)
            selection.AddItems(_dispatch_array(references))
            self.doc.WBlock(str(temporary), selection)
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise RuntimeError("AutoCAD WBlock 未生成可读的 DWG 块库")
            output_hash = sha256_file(temporary)
            if target.exists():
                backup_path = target.with_name(f"{target.name}.bak-{old_hash[:12]}")
                if not backup_path.exists():
                    shutil.copy2(target, backup_path)
            os.replace(temporary, target)
            target_replaced = True
            verified_hash = sha256_file(target)
            if verified_hash != output_hash:
                raise RuntimeError("块库落盘后 SHA-256 复核失败")
            return {
                **preview,
                "dry_run": False,
                "output_hash": output_hash,
                "output_size": target.stat().st_size,
                "old_hash": old_hash,
                "backup_path": str(backup_path) if backup_path else None,
                "current_drawing_saved": False,
                "verified": True,
            }
        except Exception:
            if temporary.exists():
                temporary.unlink()
            if target_replaced:
                if backup_path is not None and backup_path.is_file():
                    shutil.copy2(backup_path, target)
                elif not target_existed and target.exists():
                    target.unlink()
            raise
        finally:
            if selection is not None:
                try:
                    selection.Delete()
                except Exception:
                    pass
            for reference in references:
                try:
                    reference.Delete()
                except Exception:
                    pass

    @staticmethod
    def _fixed(value: float, precision: int, suppress_trailing_zeros: bool) -> str:
        text = f"{value:.{precision}f}"
        if suppress_trailing_zeros and "." in text:
            text = text.rstrip("0").rstrip(".")
        return text

    def _ensure_label_block(
        self,
        block_name: str,
        tags: Sequence[Tuple[str, Tuple[float, float, float]]],
        text_style: Optional[str],
    ) -> None:
        if self._block_exists(block_name):
            block = self.doc.Blocks.Item(block_name)
            existing = {
                str(block.Item(index).TagString).casefold()
                for index in range(block.Count)
                if str(_safe_get(block.Item(index), "EntityName", "")) == "AcDbAttributeDefinition"
            }
            missing = [tag for tag, _ in tags if tag.casefold() not in existing]
            if missing:
                raise ConflictError(
                    f"现有标注块 {block_name} 缺少属性标签: {', '.join(missing)}"
                )
            return
        definitions = [
            {
                "tag": tag,
                "prompt": tag,
                "default_value": "0",
                "text_style": text_style,
                "text_height": 2.5,
                "position": position,
                "rotation": 0.0,
                "constant": False,
                "invisible": False,
                "verify": False,
                "preset": True,
            }
            for tag, position in tags
        ]
        self.create_or_update_attribute_block(
            block_name, (0.0, 0.0, 0.0), [], definitions, False
        )

    def _insert_attribute_block_ref(
        self,
        block_name: str,
        point: Sequence[float],
        layer: str,
    ):
        if not self._layer_exists(layer):
            self.doc.Layers.Add(layer)
        coords = list(point)
        while len(coords) < 3:
            coords.append(0.0)
        reference = self.msp.InsertBlock(
            _pt(coords[0], coords[1], coords[2]), block_name, 1.0, 1.0, 1.0, 0.0
        )
        reference.Layer = layer
        return reference

    def _default_source_property(self, source: Any, axis: str) -> str:
        candidates = {
            "AcDbBlockReference": "insertion_" + axis,
            "AcDbText": "insertion_" + axis,
            "AcDbMText": "insertion_" + axis,
            "AcDbPoint": "position_" + axis,
            "AcDbCircle": "center_" + axis,
            "AcDbArc": "center_" + axis,
            "AcDbLine": "start_" + axis,
        }
        entity_name = str(_safe_get(source, "EntityName", ""))
        if entity_name not in candidates:
            raise UnsupportedOperationError(
                f"对象类型 {entity_name or 'UNKNOWN'} 没有经过验证的坐标字段属性"
            )
        return candidates[entity_name]

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
        self._check_doc()
        self._ensure_label_block(
            block_name,
            [("X", (0.0, 0.0, 0.0)), ("Y", (0.0, -4.0, 0.0))],
            text_style,
        )
        reference = self._insert_attribute_block_ref(block_name, (target_x, target_y, 0.0), layer)
        handle = self._handle_str(reference.Handle)
        factor = 0.001 if display_unit == "m" else 1.0
        if not associative:
            values = {
                "X": self._fixed(target_x * factor, precision, suppress_trailing_zeros),
                "Y": self._fixed(target_y * factor, precision, suppress_trailing_zeros),
            }
            result = self.set_block_attributes(handle, values)
            return {
                **result,
                "block_name": block_name,
                "display_unit": display_unit,
                "associative": False,
                "warning": "调用方明确 associative=false；该标签是静态块属性",
            }
        source_handle = target_handle or handle
        source = self._object_from_api_handle(source_handle)
        x_property = self._default_source_property(source, "x")
        y_property = self._default_source_property(source, "y")
        x_binding = self.bind_block_attribute_field(
            block_handle=handle,
            attribute_tag="X",
            source_type="object",
            source_handle=source_handle,
            source_property=x_property,
            linear_scale_factor=factor,
            precision=precision,
            prefix="X=",
            suffix=f" {display_unit}",
            update_on_regen=True,
        )
        y_binding = self.bind_block_attribute_field(
            block_handle=handle,
            attribute_tag="Y",
            source_type="object",
            source_handle=source_handle,
            source_property=y_property,
            linear_scale_factor=factor,
            precision=precision,
            prefix="Y=",
            suffix=f" {display_unit}",
            update_on_regen=True,
        )
        return {
            "mode": "acad",
            "handle": handle,
            "block_name": block_name,
            "target_handle": target_handle,
            "display_unit": display_unit,
            "precision": precision,
            "suppress_trailing_zeros": suppress_trailing_zeros,
            "associative": True,
            "field_bindings": [x_binding, y_binding],
            "static_fallback": False,
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
        self._check_doc()
        point = list(target_point)
        while len(point) < 3:
            point.append(0.0)
        self._ensure_label_block(block_name, [("ELEV", (0.0, 0.0, 0.0))], text_style)
        reference = self._insert_attribute_block_ref(block_name, point, layer)
        handle = self._handle_str(reference.Handle)
        factor = 0.001 if display_unit == "m" else 1.0
        if not associative:
            scaled = point[2] * factor
            if abs(scaled) <= 0.5 * 10 ** (-precision):
                value = zero_format
            else:
                value = self._fixed(scaled, precision, suppress_trailing_zeros)
                if positive_sign and scaled > 0:
                    value = "+" + value
            result = self.set_block_attributes(handle, {"ELEV": value})
            return {
                **result,
                "value": value,
                "display_unit": display_unit,
                "associative": False,
                "warning": "调用方明确 associative=false；该标签是静态块属性",
            }
        if positive_sign or zero_format != f"{0:.{precision}f}":
            raise UnsupportedOperationError(
                "ActiveX 对对象字段没有可验证的正值/负值/零条件格式；"
                "未用静态文字冒充关联标高。可将 positive_sign=false 且 zero_format 设为固定零值后重试",
                details={
                    "positive_sign": positive_sign,
                    "zero_format": zero_format,
                    "required_zero_format": f"{0:.{precision}f}",
                },
            )
        source_handle = target_handle or handle
        source = self._object_from_api_handle(source_handle)
        if target_handle:
            source_property = elevation_source
            if elevation_source in {"z", "auto"}:
                source_property = self._default_source_property(source, "z")
        else:
            source_property = "insertion_z"
        binding = self.bind_block_attribute_field(
            block_handle=handle,
            attribute_tag="ELEV",
            source_type="object",
            source_handle=source_handle,
            source_property=source_property,
            linear_scale_factor=factor,
            precision=precision,
            prefix="",
            suffix=f" {display_unit}",
            update_on_regen=True,
        )
        return {
            "mode": "acad",
            "handle": handle,
            "block_name": block_name,
            "target_handle": target_handle,
            "display_unit": display_unit,
            "associative": True,
            "field_binding": binding,
            "static_fallback": False,
        }

    def create_or_update_ctb(
        self,
        *,
        name: str,
        target_directory: str,
        color_policy: str,
        use_object_lineweight: bool,
        grayscale: bool,
        replace: bool = False,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        self._check_doc()
        if Path(name).name != name or Path(name).suffix.casefold() != ".ctb":
            raise ValueError("name 必须是无目录的 .ctb 文件名")
        environment = self.get_plot_environment()
        actual = environment.get("plot_style_search_path")
        if not actual:
            raise RuntimeError("无法读取 AutoCAD 实际打印样式目录")
        requested_dir = Path(os.path.expandvars(target_directory)).expanduser().resolve()
        actual_dirs = {
            str(Path(os.path.expandvars(item)).expanduser().resolve()).casefold()
            for item in str(actual).split(";") if item
        }
        if str(requested_dir).casefold() not in actual_dirs:
            raise ValueError("target_directory 不是 AutoCAD 实际打印样式搜索目录")
        target = requested_dir / name
        if target.exists() and not replace:
            raise FileExistsError(f"CTB 已存在，replace=false，未覆盖: {target}")
        details = {
            "mode": "acad",
            "dry_run": dry_run,
            "path": str(target),
            "target_directory_verified": True,
            "target_exists": target.exists(),
            "old_hash": sha256_file(target) if target.exists() else None,
            "color_policy": color_policy,
            "use_object_lineweight": use_object_lineweight,
            "grayscale": grayscale,
            "replace": replace,
            "supported": False,
            "file_created": False,
            "reason": "AutoCAD ActiveX 不提供可验证的 CTB 创建/编辑 API",
        }
        if dry_run:
            return details
        raise UnsupportedOperationError(
            "当前 AutoCAD API 不能可靠生成 CTB；未创建空文件或伪造成功",
            details=details,
        )

    def apply_named_page_setup(self, layout_name: str, page_setup_name: str) -> Dict[str, Any]:
        self._check_doc()
        layout = self._get_layout(layout_name)
        try:
            setup = self.doc.PlotConfigurations.Item(page_setup_name)
        except Exception as exc:
            raise ValueError(f"命名页面设置不存在: {page_setup_name}") from exc
        layout.CopyFrom(setup)
        actual, unverified = self._plot_payload(layout)
        expected, expected_unverified = self._plot_payload(setup, name=page_setup_name)
        fields = (
            "device", "media_name", "rotation", "plot_style_sheet", "plot_type",
            "paper_units", "plot_scale", "center_plot", "plot_with_lineweights",
            "plot_with_plot_styles",
        )
        mismatched = {
            field: {"expected": expected.get(field), "actual": actual.get(field)}
            for field in fields if expected.get(field) != actual.get(field)
        }
        if mismatched or unverified or expected_unverified:
            raise RuntimeError(
                f"页面设置复制到布局后读回不一致: {mismatched or unverified or expected_unverified}"
            )
        return {
            "layout_name": layout_name,
            "page_setup": page_setup_name,
            "plot_settings": actual,
            "verified": True,
        }

    def capture_dwt_snapshot(self) -> Dict[str, Any]:
        self._check_doc()
        environment = self.get_drafting_environment()
        block_names = sorted(
            str(self.doc.Blocks.Item(index).Name)
            for index in range(self.doc.Blocks.Count)
            if not str(self.doc.Blocks.Item(index).Name).startswith("*")
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
            "font_files": self._available_font_files(),
            "unsupported_items": [],
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

    def _apply_textstyle_spec(self, name: str, spec: Any) -> Dict[str, Any]:
        result = self.create_or_update_textstyle(
            name,
            spec.model_height,
            spec.font_file,
            spec.big_font_file or "",
            spec.width_factor,
            spec.current,
        )
        style = self.doc.TextStyles.Item(name)
        style.ObliqueAngle = math.radians(float(spec.oblique_angle))
        if spec.annotative:
            try:
                style.Annotative = True
            except Exception as exc:
                raise UnsupportedOperationError(
                    f"当前 AutoCAD ActiveX 无法设置文字样式 {name} 的 annotative=true"
                ) from exc
        result["oblique_angle"] = float(spec.oblique_angle)
        result["annotative"] = bool(spec.annotative)
        return result

    def _apply_layout_spec(self, name: str, page_setup: Optional[str]) -> Dict[str, Any]:
        names = {
            str(self.doc.Layouts.Item(index).Name).casefold()
            for index in range(self.doc.Layouts.Count)
        }
        result = self.create_layout(name, replace=False) if name.casefold() not in names else {
            "layout_name": name, "created": False, "replaced": False
        }
        if page_setup:
            result["named_page_setup"] = self.apply_named_page_setup(name, page_setup)
        return result

    def apply_dwt_profile(
        self,
        profile: DraftingProfileV2,
        *,
        profile_path: str,
        profile_sha256: str,
        target_drawing: Optional[str],
        update_existing: bool,
        preserve_unmanaged: bool,
        confirmation_token: Optional[str],
    ) -> Dict[str, Any]:
        """Apply one already-confirmed plan; the proxy owns the single undo group."""
        self._check_doc()
        snapshot = self.capture_dwt_snapshot()
        plan = build_plan(
            profile,
            snapshot,
            profile_path=profile_path,
            profile_sha256=profile_sha256,
            target_drawing=target_drawing,
            update_existing=update_existing,
            preserve_unmanaged=preserve_unmanaged,
        )
        if plan.get("conflicts") or plan.get("unsupported_items"):
            raise ConflictError(
                "DWT 应用计划含冲突或未支持项，未执行任何写入",
                details={
                    "conflicts": plan.get("conflicts") or [],
                    "unsupported_items": plan.get("unsupported_items") or [],
                },
            )
        if not plan.get("confirmation_required"):
            validation = validate_snapshot(
                profile, snapshot, preserve_unmanaged=preserve_unmanaged
            )
            return {
                "mode": "acad",
                "applied": False,
                "idempotent_noop": True,
                "plan_hash": plan["plan_hash"],
                "operations": [],
                "validation": validation,
                "rollback_status": "not_required",
                "current_drawing_saved": False,
            }
        verify_confirmation(confirmation_token, plan)
        operations: List[Dict[str, Any]] = []

        if plan["drawing_settings_changes"]:
            desired = profile.drawing_settings.model_dump(
                mode="python", exclude={"current_layer", "current_layout"}
            )
            operations.append({
                "operation": "set_drawing_settings",
                "result": self.set_drawing_settings(**desired),
            })
        for item in plan["layers_to_rename"]:
            operations.append({"operation": "rename_layer", "result": self.rename_layer(**item)})
        for item in plan["layers_to_create"]:
            desired = item["desired"]
            operations.append({
                "operation": "create_layer",
                "result": self.create_layer(
                    name=item["name"],
                    color=desired["color"],
                    linetype=desired["linetype"],
                    lineweight=desired.get("lineweight", -3),
                    plot=desired["plot"],
                    description=desired["description"],
                    locked=desired["locked"],
                    transparency=desired.get("transparency"),
                ),
            })
            if desired["frozen"]:
                self.set_layer_props(item["name"], frozen=True)
        for item in plan["layers_to_update"]:
            desired = item["desired"]
            writable_fields = {
                "color", "linetype", "lineweight", "plot", "description",
                "locked", "frozen", "transparency",
            }
            changed_fields = {
                str(change.get("field")) for change in item.get("changes", [])
            }
            unknown_fields = changed_fields - writable_fields
            if unknown_fields:
                raise RuntimeError(
                    "DWT 图层更新计划包含不可写字段: "
                    + ", ".join(sorted(unknown_fields))
                )
            update_values = {
                field: desired[field]
                for field in changed_fields
                if field in desired
            }
            if not update_values:
                raise RuntimeError(
                    f"DWT 图层 {item['name']} 被列为更新项，但计划中没有可写差异"
                )
            operations.append({
                "operation": "set_layer_props",
                "fields": sorted(update_values),
                "result": self.set_layer_props(
                    name=item["name"],
                    **update_values,
                ),
            })
        for item in plan["textstyles_to_create_or_update"]:
            operations.append({
                "operation": "create_or_update_textstyle",
                "result": self._apply_textstyle_spec(
                    item["name"], profile.text_styles[item["name"]]
                ),
            })
        for item in plan["dimstyles_to_create_or_update"]:
            spec = profile.dimension_styles[item["name"]]
            operations.append({
                "operation": "create_or_update_dimstyle",
                "result": self.create_or_update_dimstyle(
                    name=item["name"],
                    **spec.model_dump(mode="python", exclude={"current"}),
                    set_current=spec.current,
                ),
            })
        if plan["linetype_settings_changes"]:
            operations.append({
                "operation": "set_linetype_settings",
                "result": self.set_linetype_settings(
                    **profile.linetype_settings.model_dump(mode="python"), regen=True
                ),
            })
        for item in plan["page_setups_to_create_or_update"]:
            spec = profile.page_setups[item["name"]]
            payload = spec.model_dump(mode="python")
            # update_existing=true is the explicit profile-application permission.
            payload["replace"] = bool(payload["replace"] or update_existing)
            operations.append({
                "operation": "create_or_update_page_setup",
                "result": self.create_or_update_page_setup(name=item["name"], **payload),
            })
        for item in plan["layouts_to_rename"]:
            operations.append({"operation": "rename_layout", "result": self.rename_layout(**item)})
        for item in plan["layouts_to_create"]:
            operations.append({
                "operation": "create_layout",
                "result": self._apply_layout_spec(item["layout_name"], item.get("page_setup")),
            })
        for item in plan["layouts_to_update"]:
            operations.append({
                "operation": "apply_named_page_setup",
                "result": self.apply_named_page_setup(item["layout_name"], item["after"]),
            })
        for item in plan["block_library_actions"]:
            operations.append({
                "operation": "import_block_definitions",
                "result": self.import_block_definitions(
                    source_path=item["source_path"],
                    block_names=item["block_names"],
                    replace_existing=item["replace_existing"],
                    expected_sha256=item["expected_sha256"],
                ),
            })
        for item in plan["layouts_to_delete"]:
            operations.append({
                "operation": "delete_layout",
                "result": self.delete_layout(
                    [item["name"]], require_empty=item["require_empty"], dry_run=False
                ),
            })
        for item in plan["layers_to_delete"]:
            operations.append({
                "operation": "delete_layer",
                "result": self.delete_layer(
                    [item["name"]],
                    require_empty=item["require_empty"],
                    allow_nonempty=item["allow_nonempty"],
                    dry_run=False,
                ),
            })
        desired_layer = profile.drawing_settings.current_layer
        if desired_layer and self.get_drawing_settings().get("current_layer") != desired_layer:
            operations.append({
                "operation": "set_active_layer",
                "result": self.set_active_layer(desired_layer),
            })
        desired_layout = profile.drawing_settings.current_layout
        if desired_layout and self.get_drawing_settings().get("current_layout") != desired_layout:
            layout = self._get_layout(desired_layout)
            self._activate_layout(layout)
            operations.append({
                "operation": "set_current_layout",
                "result": {"layout_name": desired_layout, "verified": True},
            })
        after = self.capture_dwt_snapshot()
        validation = validate_snapshot(
            profile, after, preserve_unmanaged=preserve_unmanaged
        )
        if validation["errors"]:
            raise ConflictError(
                "DWT 应用后逐项验证未通过；将由外层撤销组整体回滚",
                details={"validation": validation, "completed_operations": operations},
            )
        return {
            "mode": "acad",
            "applied": True,
            "idempotent_noop": False,
            "plan_hash": plan["plan_hash"],
            "operations": operations,
            "validation": validation,
            "rollback_status": "not_required",
            "current_drawing_saved": False,
        }

    def validate_dwt_profile(
        self,
        profile: DraftingProfileV2,
        *,
        preserve_unmanaged: bool = True,
    ) -> Dict[str, Any]:
        return validate_snapshot(
            profile,
            self.capture_dwt_snapshot(),
            preserve_unmanaged=preserve_unmanaged,
        )
