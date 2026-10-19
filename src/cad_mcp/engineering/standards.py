"""可切换制图标准配置及模型校核。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from cad_mcp.core.contracts import CadOperation, EnvironmentModel, ValidationIssue
from cad_mcp.dwt.contracts import (
    DraftingProfileV2,
    dwt_profile_evidence,
    read_utf8_json_object,
)


class StandardModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class LayerStyle(StandardModel):
    color: int = Field(ge=1, le=255)
    linetype: str = Field(min_length=1, max_length=80)
    lineweight: Optional[int] = Field(default=None, ge=-3, le=211)
    plot: bool = True


class DraftingProfile(StandardModel):
    schema_version: Literal["1.0"] = "1.0"
    profile_id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=200)
    standard_references: List[str] = Field(default_factory=list)
    drawing_unit: Literal["mm"] = "mm"
    diameter_unit: Literal["mm"] = "mm"
    paper_text_heights: List[float] = Field(min_length=1)
    base_lineweights_mm: List[float] = Field(min_length=1)
    allowed_scales: Dict[str, List[float]] = Field(default_factory=dict)
    layer_styles: Dict[str, LayerStyle] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


AnyDraftingProfile = Union[DraftingProfile, DraftingProfileV2]


def load_drafting_profile(path: str | Path) -> tuple[AnyDraftingProfile, Dict[str, Any]]:
    """加载旧版环境制图配置或严格的通用 DWT 2.0 配置。"""

    profile_path, raw, data = read_utf8_json_object(path)
    schema_version = data.get("schema_version", "1.0")
    if schema_version == "1.0":
        profile = DraftingProfile.model_validate(data)
        return profile, {
            "path": str(profile_path),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "schema_version": profile.schema_version,
            "layer_style_count": len(profile.layer_styles),
            "scale_rule_count": len(profile.allowed_scales),
        }
    if schema_version == "2.0":
        profile = DraftingProfileV2.model_validate(data)
        return profile, dwt_profile_evidence(profile, profile_path, raw)
    raise ValueError(f"不支持的 DraftingProfile schema_version: {schema_version!r}")


def validate_against_drafting_profile(
    model: EnvironmentModel,
    profile: AnyDraftingProfile,
) -> List[ValidationIssue]:
    # 2.0 配置由专用 DWT 预览/应用/校验接口处理；这里仍只校验环境工程模型。
    # 旧环境模型的比例/纸面字高规则只存在于 1.0，不能从 2.0 猜测。
    if isinstance(profile, DraftingProfileV2):
        return []
    issues: List[ValidationIssue] = []
    for view in model.views:
        role = str(view.attributes.get("drawing_role", ""))
        key = role if role in profile.allowed_scales else view.view_type
        allowed = profile.allowed_scales.get(key)
        if allowed and not any(abs(view.scale_denominator - item) < 1e-9 for item in allowed):
            issues.append(ValidationIssue(
                severity="error",
                rule_id="DRAFTING_SCALE_NOT_ALLOWED",
                message=(f"视图 {view.id} 的比例 1:{view.scale_denominator:g} 不在"
                         f"制图配置 {profile.profile_id} 的 {key} 标准比例中"),
                semantic_ids=[view.id],
            ))
    for annotation in model.annotations:
        if not any(abs(annotation.paper_height - item) < 1e-9
                   for item in profile.paper_text_heights):
            issues.append(ValidationIssue(
                severity="warning",
                rule_id="DRAFTING_NONSTANDARD_TEXT_HEIGHT",
                message=(f"标注 {annotation.id} 的纸面字高 {annotation.paper_height:g} mm "
                         f"不在配置建议值中"),
                semantic_ids=[annotation.id],
            ))
    return issues


def apply_layer_styles(
    operations: List[CadOperation],
    profile: AnyDraftingProfile,
) -> List[CadOperation]:
    """只改 ensure_layer 的样式参数，不改语义对象几何或图层归属。"""

    result: List[CadOperation] = []
    for operation in operations:
        if operation.kind == "ensure_layer":
            layer_name = str(operation.params.get("name", ""))
            style = profile.layer_styles.get(layer_name)
            if style is not None:
                params = dict(operation.params)
                if isinstance(profile, DraftingProfileV2):
                    if style.state != "present":
                        result.append(operation)
                        continue
                    params.update(style.model_dump(
                        include={"color", "linetype", "lineweight", "plot"},
                        exclude_none=True,
                    ))
                else:
                    params.update(style.model_dump(exclude_none=True))
                operation = operation.model_copy(update={"params": params})
        result.append(operation)
    return result
