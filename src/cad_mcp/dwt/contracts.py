"""Strict external contracts for generic DWT drafting profiles.

This module is deliberately independent from AutoCAD and ezdxf.  It validates
configuration structure and local DWG block-library evidence only; fonts,
plot-style tables, and plot devices must be verified by the selected backend.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


MAX_JSON_BYTES = 8 * 1024 * 1024

class DwtModel(BaseModel):
    """Strict base used by every DraftingProfile 2.0 object."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        allow_inf_nan=False,
        strict=True,
        validate_default=True,
    )

    @model_validator(mode="after")
    def reject_nested_non_finite_values(self):
        _reject_non_finite(self.model_dump(mode="python"))
        return self


def _reject_non_finite(value: Any, location: str = "配置") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{location}不能包含 NaN、正无穷或负无穷")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_non_finite(item, f"{location}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_non_finite(item, f"{location}[{index}]")


class DrawingSettingsSpec(DwtModel):
    insertion_units: Literal["millimeters"] = "millimeters"
    measurement_system: Literal["metric"] = "metric"
    linear_unit_format: Literal["decimal"] = "decimal"
    linear_precision: int = Field(default=2, ge=0, le=8)
    angular_unit_format: Literal["decimal_degrees"] = "decimal_degrees"
    angular_precision: int = Field(default=2, ge=0, le=8)
    associative_dimensions: bool = True
    mirror_text: bool = False
    display_lineweights: bool = True
    current_layer: Optional[str] = Field(default=None, min_length=1, max_length=255)
    current_layout: Optional[str] = Field(default=None, min_length=1, max_length=255)


class LayerStyleSpec(DwtModel):
    state: Literal["present", "absent"] = "present"
    rename_from: Optional[str] = Field(default=None, min_length=1, max_length=255)
    color: int = Field(default=7, ge=1, le=255)
    linetype: str = Field(default="CONTINUOUS", min_length=1, max_length=255)
    lineweight: Optional[int] = Field(default=None, ge=-3, le=211)
    plot: bool = True
    description: str = Field(default="", max_length=1024)
    locked: bool = False
    frozen: bool = False
    transparency: Optional[int] = Field(default=None, ge=0, le=90)
    require_empty: bool = True
    allow_nonempty: bool = False

    @model_validator(mode="after")
    def validate_delete_guard(self):
        if self.require_empty and self.allow_nonempty:
            raise ValueError("require_empty=true 时不能同时设置 allow_nonempty=true")
        return self


class TextStyleSpec(DwtModel):
    font_file: str = Field(min_length=1, max_length=500)
    big_font_file: Optional[str] = Field(default=None, min_length=1, max_length=500)
    model_height: float = Field(default=0.0, ge=0, le=1_000_000)
    width_factor: float = Field(default=1.0, gt=0, le=100)
    oblique_angle: float = Field(default=0.0, ge=-85, le=85)
    annotative: bool = False
    current: bool = False


class DimensionStyleSpec(DwtModel):
    text_style: str = Field(min_length=1, max_length=255)
    text_height: float = Field(gt=0, le=1_000_000)
    overall_scale: float = Field(default=1.0, gt=0, le=1_000_000)
    linear_scale_factor: float = Field(default=1.0, gt=0, le=1_000_000)
    precision: int = Field(default=2, ge=0, le=8)
    oblique_stroke_size: float = Field(default=0.0, ge=0, le=1_000_000)
    arrow_size: float = Field(gt=0, le=1_000_000)
    text_gap: float = Field(default=0.625, ge=0, le=1_000_000)
    text_above: bool = True
    suppress_leading_zeros: bool = False
    suppress_trailing_zeros: bool = False
    decimal_separator: Literal[".", ","] = "."
    extension_line_offset: float = Field(default=0.625, ge=0, le=1_000_000)
    extension_line_extension: float = Field(default=1.25, ge=0, le=1_000_000)
    baseline_spacing: float = Field(default=3.75, ge=0, le=1_000_000)
    dimension_line_color: int = Field(default=256, ge=0, le=256)
    extension_line_color: int = Field(default=256, ge=0, le=256)
    text_color: int = Field(default=256, ge=0, le=256)
    dimension_line_lineweight: int = Field(default=-1, ge=-3, le=211)
    extension_line_lineweight: int = Field(default=-1, ge=-3, le=211)
    current: bool = False


class LinetypeSettingsSpec(DwtModel):
    ltscale: float = Field(default=1.0, gt=0, le=1_000_000)
    celtscale: float = Field(default=1.0, gt=0, le=1_000_000)
    msltscale: Literal[0, 1] = 0
    psltscale: Literal[0, 1] = 1


class PlotPolicySpec(DwtModel):
    target_mode: Literal["ctb", "stb"] = "ctb"
    expected_current_mode: Optional[Literal["ctb", "stb"]] = None
    required_style_sheets: List[str] = Field(default_factory=list, max_length=128)
    preferred_device: Optional[str] = Field(default=None, min_length=1, max_length=500)
    allow_mode_conversion: bool = False


class PlotScaleSpec(DwtModel):
    mode: Literal["standard", "custom", "fit"] = "standard"
    standard_scale: Optional[str] = Field(default="1:1", min_length=1, max_length=80)
    numerator: Optional[float] = Field(default=None, gt=0, le=1_000_000)
    denominator: Optional[float] = Field(default=None, gt=0, le=1_000_000)

    @model_validator(mode="after")
    def validate_scale_fields(self):
        if self.mode == "custom" and (self.numerator is None or self.denominator is None):
            raise ValueError("custom 比例必须同时提供 numerator 和 denominator")
        if self.mode == "standard" and not self.standard_scale:
            raise ValueError("standard 比例必须提供 standard_scale")
        return self


class PageSetupSpec(DwtModel):
    device: str = Field(min_length=1, max_length=500)
    media_name: str = Field(min_length=1, max_length=500)
    rotation: Literal[0, 90, 180, 270] = 0
    plot_style_sheet: str = Field(min_length=1, max_length=500)
    plot_type: Literal["display", "extents", "limits", "view", "window", "layout"] = "layout"
    paper_units: Literal["millimeters", "inches", "pixels"] = "millimeters"
    plot_scale: PlotScaleSpec = Field(default_factory=PlotScaleSpec)
    center_plot: bool = False
    plot_with_lineweights: bool = True
    plot_with_plot_styles: bool = True
    replace: bool = False


class LayoutSpec(DwtModel):
    state: Literal["present", "absent"] = "present"
    rename_from: Optional[str] = Field(default=None, min_length=1, max_length=255)
    page_setup: Optional[str] = Field(default=None, min_length=1, max_length=255)
    require_empty: bool = True


class BlockLibrarySourceSpec(DwtModel):
    source_path: str = Field(min_length=1, max_length=2000)
    expected_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    drawing_unit: Literal["millimeters"] = "millimeters"
    block_names: List[str] = Field(min_length=1, max_length=500)
    import_on_apply: bool = True
    replace_existing: bool = False

    @field_validator("source_path")
    @classmethod
    def normalize_absolute_dwg_path(cls, value: str) -> str:
        source = Path(value).expanduser()
        if not source.is_absolute():
            raise ValueError("块库 source_path 必须是 DWG 完整绝对路径")
        if source.suffix.lower() != ".dwg":
            raise ValueError("块库 source_path 必须指向 .dwg 文件")
        return str(source.resolve())

    @field_validator("expected_sha256")
    @classmethod
    def normalize_sha256(cls, value: str) -> str:
        return value.lower()


class BlockLibrarySpec(DwtModel):
    sources: List[BlockLibrarySourceSpec] = Field(default_factory=list, max_length=32)


class DraftingProfileV2(DwtModel):
    schema_version: Literal["2.0"] = "2.0"
    profile_id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=200)
    standard_references: List[str] = Field(default_factory=list, max_length=256)
    drawing_settings: DrawingSettingsSpec = Field(default_factory=DrawingSettingsSpec)
    layer_styles: Dict[str, LayerStyleSpec] = Field(default_factory=dict, max_length=2_048)
    text_styles: Dict[str, TextStyleSpec] = Field(default_factory=dict, max_length=512)
    dimension_styles: Dict[str, DimensionStyleSpec] = Field(default_factory=dict, max_length=512)
    linetype_settings: LinetypeSettingsSpec = Field(default_factory=LinetypeSettingsSpec)
    plot_policy: PlotPolicySpec = Field(default_factory=PlotPolicySpec)
    page_setups: Dict[str, PageSetupSpec] = Field(default_factory=dict, max_length=512)
    layouts: Dict[str, LayoutSpec] = Field(default_factory=dict, max_length=512)
    block_library: BlockLibrarySpec = Field(default_factory=BlockLibrarySpec)
    metadata: Dict[str, JsonValue] = Field(default_factory=dict, max_length=256)

    @model_validator(mode="after")
    def validate_named_collections(self):
        for field_name in (
            "layer_styles", "text_styles", "dimension_styles", "page_setups", "layouts"
        ):
            names = getattr(self, field_name)
            for name in names:
                if not name or name != name.strip() or len(name) > 255:
                    raise ValueError(f"{field_name} 包含空名称、首尾空格或超长名称")
        return self


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"JSON 不允许非有限数值: {value}")


def read_utf8_json_object(
    path: str | Path,
    *,
    label: str = "制图标准配置",
) -> tuple[Path, bytes, Dict[str, Any]]:
    """Read an existing absolute JSON path without accepting NaN/Infinity."""

    source = Path(path).expanduser()
    if not source.is_absolute() or source.suffix.lower() != ".json":
        raise ValueError(f"{label}必须是现有 JSON 文件的完整绝对路径")
    source = source.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"{label}不存在: {source}")
    size = source.stat().st_size
    if size > MAX_JSON_BYTES:
        raise ValueError(f"{label}大小 {size} 字节超过上限 {MAX_JSON_BYTES} 字节")
    raw = source.read_bytes()
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError(f"{label}读取期间增长并超过上限 {MAX_JSON_BYTES} 字节")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label}必须使用 UTF-8 编码") from exc
    try:
        data = json.loads(text, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label}不是有效 UTF-8 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{label}顶层必须是 JSON 对象")
    return source, raw, data


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_resource_references(profile: DraftingProfileV2) -> Dict[str, Any]:
    fonts = {style.font_file for style in profile.text_styles.values()}
    big_fonts = {
        style.big_font_file for style in profile.text_styles.values() if style.big_font_file
    }
    style_sheets = set(profile.plot_policy.required_style_sheets)
    style_sheets.update(item.plot_style_sheet for item in profile.page_setups.values())
    devices = {item.device for item in profile.page_setups.values()}
    if profile.plot_policy.preferred_device:
        devices.add(profile.plot_policy.preferred_device)
    return {
        "fonts": sorted(fonts),
        "big_fonts": sorted(big_fonts),
        "plot_style_sheets": sorted(style_sheets),
        "plot_devices": sorted(devices),
        "block_library_paths": [item.source_path for item in profile.block_library.sources],
    }


def verify_block_library_sources(profile: DraftingProfileV2) -> List[Dict[str, Any]]:
    evidence: List[Dict[str, Any]] = []
    for item in profile.block_library.sources:
        source = Path(item.source_path)
        if not source.is_file():
            raise FileNotFoundError(f"外部 DWG 块库不存在: {source}")
        actual = sha256_file(source)
        if actual != item.expected_sha256:
            raise ValueError(
                f"外部 DWG 块库 SHA-256 不匹配: {source}；"
                f"期望 {item.expected_sha256}，实际 {actual}"
            )
        evidence.append({
            "source_path": str(source),
            "sha256": actual,
            "verified": True,
            "block_names": list(item.block_names),
        })
    return evidence


def dwt_profile_evidence(
    profile: DraftingProfileV2,
    source: Path,
    raw: bytes,
) -> Dict[str, Any]:
    resources = collect_resource_references(profile)
    deferred = [
        *(f"font:{name}" for name in resources["fonts"]),
        *(f"big_font:{name}" for name in resources["big_fonts"]),
        *(f"plot_style_sheet:{name}" for name in resources["plot_style_sheets"]),
        *(f"plot_device:{name}" for name in resources["plot_devices"]),
    ]
    return {
        "path": str(source),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "schema_version": profile.schema_version,
        "layer_style_count": len(profile.layer_styles),
        "text_style_count": len(profile.text_styles),
        "dimension_style_count": len(profile.dimension_styles),
        "page_setup_count": len(profile.page_setups),
        "layout_count": len(profile.layouts),
        "resource_references": resources,
        "verified_block_libraries": verify_block_library_sources(profile),
        "backend_verification_required": deferred,
    }


def load_dwt_profile_v2(path: str | Path) -> tuple[DraftingProfileV2, Dict[str, Any]]:
    source, raw, data = read_utf8_json_object(path)
    profile = DraftingProfileV2.model_validate(data)
    return profile, dwt_profile_evidence(profile, source, raw)


_INSUNITS = {
    0: "unitless", 1: "inches", 2: "feet", 3: "miles", 4: "millimeters",
    5: "centimeters", 6: "meters", 7: "kilometers", 8: "microinches",
    9: "mils", 10: "yards", 11: "angstroms", 12: "nanometers",
    13: "microns", 14: "decimeters", 15: "decameters", 16: "hectometers",
    17: "gigameters", 18: "astronomical_units", 19: "light_years", 20: "parsecs",
}
_MEASUREMENT = {0: "imperial", 1: "metric"}
_LUNITS = {1: "scientific", 2: "decimal", 3: "engineering", 4: "architectural", 5: "fractional"}
_AUNITS = {0: "decimal_degrees", 1: "degrees_minutes_seconds", 2: "grads", 3: "radians", 4: "surveyor"}
_PSTYLEMODE = {0: "stb", 1: "ctb"}


def build_drawing_settings_payload(
    mode: Literal["acad", "dxf"],
    raw_variables: Mapping[str, Any],
    current_layer: Optional[str],
    current_layout: Optional[str],
    unverified_fields: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Normalize verified system-variable values without inventing defaults."""

    unverified = list(unverified_fields or [])

    def mapped(variable: str, field: str, values: Mapping[int, str]) -> Optional[str]:
        if variable not in raw_variables:
            unverified.append(field)
            return None
        try:
            result = values.get(int(raw_variables[variable]))
        except (TypeError, ValueError):
            result = None
        if result is None:
            unverified.append(field)
        return result

    def integer(variable: str, field: str) -> Optional[int]:
        if variable not in raw_variables:
            unverified.append(field)
            return None
        try:
            return int(raw_variables[variable])
        except (TypeError, ValueError):
            unverified.append(field)
            return None

    def boolean(variable: str, field: str) -> Optional[bool]:
        value = integer(variable, field)
        if value not in (0, 1):
            if field not in unverified:
                unverified.append(field)
            return None
        return bool(value)

    dimassoc = integer("DIMASSOC", "associative_dimensions")
    if dimassoc in (0, 1):
        associative: Optional[bool] = False
    elif dimassoc == 2:
        associative = True
    else:
        associative = None
        if "associative_dimensions" not in unverified:
            unverified.append("associative_dimensions")
    if current_layer is None:
        unverified.append("current_layer")
    if current_layout is None:
        unverified.append("current_layout")

    return {
        "mode": mode,
        "insertion_units": mapped("INSUNITS", "insertion_units", _INSUNITS),
        "measurement_system": mapped("MEASUREMENT", "measurement_system", _MEASUREMENT),
        "linear_unit_format": mapped("LUNITS", "linear_unit_format", _LUNITS),
        "linear_precision": integer("LUPREC", "linear_precision"),
        "angular_unit_format": mapped("AUNITS", "angular_unit_format", _AUNITS),
        "angular_precision": integer("AUPREC", "angular_precision"),
        "associative_dimensions": associative,
        "mirror_text": boolean("MIRRTEXT", "mirror_text"),
        "display_lineweights": boolean("LWDISPLAY", "display_lineweights"),
        "plot_style_mode": mapped("PSTYLEMODE", "plot_style_mode", _PSTYLEMODE),
        "current_layer": current_layer,
        "current_layout": current_layout,
        "raw_system_variables": dict(sorted(raw_variables.items())),
        "unverified_fields": sorted(set(unverified)),
    }
