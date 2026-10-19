"""外部 DWG 图框/表格资产清单与固定图框比例校核。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import BinaryIO, Dict, List, Literal, Optional, Sequence, Tuple

from pydantic import ConfigDict, BaseModel, Field, field_validator, model_validator

from cad_mcp.core.contracts import reject_non_finite
from cad_mcp.dwt.contracts import read_utf8_json_object


Bounds = Tuple[float, float, float, float]


class TemplateModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )

    @model_validator(mode="after")
    def reject_nested_non_finite_values(self):
        reject_non_finite(self.model_dump(mode="python"), "模板清单")
        return self


class PaperRegion(TemplateModel):
    id: str = Field(min_length=1, max_length=120)
    x: float
    y: float
    width: float = Field(gt=0)
    height: float = Field(gt=0)


class TemplateField(TemplateModel):
    key: str = Field(min_length=1, max_length=120)
    mode: Literal["attribute", "text_anchor"]
    attribute_tag: Optional[str] = Field(default=None, max_length=120)
    anchor: Optional[Tuple[float, float]] = None
    width: Optional[float] = Field(default=None, gt=0)
    height: Optional[float] = Field(default=None, gt=0)
    alignment: str = Field(default="middle_center", max_length=40)
    text_style: Optional[str] = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def validate_mode(self) -> "TemplateField":
        if self.mode == "attribute" and not self.attribute_tag:
            raise ValueError("attribute 字段必须提供 attribute_tag")
        if self.mode == "text_anchor" and self.anchor is None:
            raise ValueError("text_anchor 字段必须提供 anchor")
        return self


class TableTemplate(TemplateModel):
    id: str = Field(min_length=1, max_length=120)
    block_name: Optional[str] = Field(default=None, max_length=160)
    origin: Tuple[float, float]
    row_capacity: int = Field(gt=0, le=10_000)
    row_height: float = Field(gt=0)
    row_direction: Literal["down", "up"] = "down"
    text_height: float = Field(default=3.5, gt=0)
    columns: Dict[str, TemplateField] = Field(max_length=128)
    panel_offsets: List[Tuple[float, float]] = Field(
        default_factory=lambda: [(0.0, 0.0)],
        min_length=1,
        max_length=128,
        description="同一布局中各连续表格面板相对 origin 的固定偏移",
    )


class SheetTemplate(TemplateModel):
    id: str = Field(min_length=1, max_length=120)
    paper_size: str = Field(min_length=1, max_length=40)
    orientation: Literal["landscape", "portrait"] = "landscape"
    paper_width: float = Field(gt=0)
    paper_height: float = Field(gt=0)
    block_name: str = Field(min_length=1, max_length=160)
    source_bounds: Bounds
    usable_regions: List[PaperRegion] = Field(min_length=1, max_length=64)
    fields: Dict[str, TemplateField] = Field(default_factory=dict, max_length=256)
    tables: Dict[str, TableTemplate] = Field(default_factory=dict, max_length=64)
    fixed_insert_scale: float = Field(
        gt=0,
        description="模板原始坐标到布局毫米的固定换算，不得根据图形范围调整",
    )


class TemplateManifest(TemplateModel):
    schema_version: str = "1.0"
    template_id: str = Field(min_length=1, max_length=120)
    source_dwg: str = Field(min_length=1, max_length=1000)
    source_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    source_units: str = Field(default="unitless", max_length=40)
    sheets: List[SheetTemplate] = Field(min_length=1, max_length=128)
    metadata: Dict[str, object] = Field(default_factory=dict, max_length=256)

    @field_validator("source_sha256")
    @classmethod
    def normalize_sha256(cls, value: str) -> str:
        return value.lower()


def open_shared_read(path: str | Path) -> BinaryIO:
    """以共享读方式打开文件，允许 AutoCAD 正在持有同一 DWG。"""

    resolved = Path(path).expanduser().resolve()
    if os.name != "nt":
        return resolved.open("rb")

    # Python 的普通 open() 不能控制 Windows 共享标志；模板被 AutoCAD 打开时，
    # 必须显式允许 FILE_SHARE_READ/WRITE/DELETE，才能安全地只读校验和复制。
    import ctypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(resolved),
        0x80000000,  # GENERIC_READ
        0x00000001 | 0x00000002 | 0x00000004,  # READ | WRITE | DELETE
        None,
        3,  # OPEN_EXISTING
        0x00000080,  # FILE_ATTRIBUTE_NORMAL
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        error = ctypes.get_last_error()
        raise OSError(error, f"无法以共享读方式打开文件: {resolved}")
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except Exception:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        raise
    return os.fdopen(descriptor, "rb")


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open_shared_read(path) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_template_source(
    source_path: str | Path,
    output_path: str | Path,
    overwrite: bool = False,
) -> Path:
    """按字节复制只读模板；源文件即使在 AutoCAD 中打开也可读取。"""

    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if output.exists() and not overwrite:
        raise FileExistsError(f"工作副本已存在，未覆盖: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=output.parent,
            delete=False,
        ) as output_stream:
            temp_path = Path(output_stream.name)
            with open_shared_read(source) as input_stream:
                shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
        shutil.copystat(source, temp_path)
        if output.exists() and not overwrite:
            raise FileExistsError(f"工作副本已存在，未覆盖: {output}")
        os.replace(temp_path, output)
        temp_path = None
    except Exception:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
        raise
    return output


def load_template_manifest(path: str | Path) -> TemplateManifest:
    manifest_path, _, data = read_utf8_json_object(path, label="模板清单")
    manifest = TemplateManifest.model_validate(data)
    source = Path(manifest.source_dwg).expanduser()
    if not source.is_absolute():
        manifest.source_dwg = str((manifest_path.parent / source).resolve())
    evidence = verify_template_source(manifest)
    if not evidence["ok"]:
        raise ValueError(
            f"模板源文件校验失败: {evidence.get('reason') or 'sha256_mismatch'}；"
            f"路径 {evidence['path']}，期望 {manifest.source_sha256}，"
            f"实际 {evidence.get('sha256')}")
    return manifest


def verify_template_source(manifest: TemplateManifest) -> Dict[str, object]:
    source = Path(manifest.source_dwg).expanduser().resolve()
    if not source.is_file():
        return {"ok": False, "path": str(source), "reason": "source_missing"}
    if source.suffix.lower() != ".dwg":
        return {"ok": False, "path": str(source), "reason": "source_not_dwg"}
    actual = sha256_file(source)
    expected = manifest.source_sha256
    return {
        "ok": actual.lower() == expected.lower(),
        "path": str(source),
        "sha256": actual,
        "expected_sha256": expected,
        "hash_matches": actual.lower() == expected.lower(),
    }


def _expanded_bounds(bounds: Bounds, padding: float) -> Bounds:
    xmin, ymin, xmax, ymax = map(float, bounds)
    return xmin - padding, ymin - padding, xmax + padding, ymax + padding


def evaluate_sheet_scales(
    content_bounds: Bounds,
    sheet: SheetTemplate,
    candidate_denominators: Sequence[float],
    annotation_padding_model: float = 0.0,
) -> Dict[str, object]:
    """固定图框不动，逐一校核候选比例能否落入任一可用绘图区。"""

    if not candidate_denominators:
        raise ValueError("candidate_denominators 不能为空")
    xmin, ymin, xmax, ymax = _expanded_bounds(content_bounds, annotation_padding_model)
    if xmax <= xmin or ymax <= ymin:
        raise ValueError("content_bounds 必须形成正面积矩形")
    model_width, model_height = xmax - xmin, ymax - ymin
    evaluations: List[Dict[str, object]] = []
    for denominator in candidate_denominators:
        denominator = float(denominator)
        if denominator <= 0:
            raise ValueError("比例分母必须大于 0")
        paper_width = model_width / denominator
        paper_height = model_height / denominator
        region_results = []
        for region in sheet.usable_regions:
            fits = paper_width <= region.width and paper_height <= region.height
            region_results.append({
                "region_id": region.id,
                "fits": fits,
                "usable_width": region.width,
                "usable_height": region.height,
                "used_width": paper_width,
                "used_height": paper_height,
                "remaining_width": region.width - paper_width,
                "remaining_height": region.height - paper_height,
            })
        evaluations.append({
            "scale_denominator": denominator,
            "fits": any(result["fits"] for result in region_results),
            "regions": region_results,
        })
    minimum_denominator_by_region = {
        region.id: max(model_width / region.width, model_height / region.height)
        for region in sheet.usable_regions
    }
    return {
        "sheet_id": sheet.id,
        "paper_size": sheet.paper_size,
        "frame_fixed": True,
        "frame_insert_scale": sheet.fixed_insert_scale,
        "content_model_size": {"width": model_width, "height": model_height},
        "minimum_denominator_by_region": minimum_denominator_by_region,
        "evaluations": evaluations,
        "selection_policy": "Agent 从 fits=true 的标准比例中选择；本函数不缩放图框也不替 Agent 决策",
    }
