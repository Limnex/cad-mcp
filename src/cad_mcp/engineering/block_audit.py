"""Strict manifest-driven audit support for native DWG block definitions.

The module intentionally separates COM extraction from comparison policy.  It
never invents expected values: every comparison originates in an explicit
``*_manifest.json`` check, and missing or unreadable evidence is reported as
``无法验证``.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal, ROUND_HALF_EVEN
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cad_mcp.core.contracts import reject_non_finite
from cad_mcp.dwt.contracts import read_utf8_json_object


AuditStatus = Literal["通过", "偏差", "无法验证"]
Point3 = Tuple[float, float, float]
MAX_BLOCK_TREE_ENTITIES = 50_000
MAX_BLOCK_TREE_DEPTH = 64
_MISSING = object()


class BlockAuditModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )

    @model_validator(mode="after")
    def reject_nested_non_finite_values(self):
        reject_non_finite(self.model_dump(mode="python"), "块规格卡")
        return self


class BlockCheck(BlockAuditModel):
    id: str = Field(min_length=1, max_length=160)
    path: str = Field(
        min_length=1,
        max_length=500,
        description="指向审查结果的 JSON Pointer，例如 /geometry/bounding_box/size",
    )
    operator: Literal["equals", "within", "unordered_equals", "contains", "range"]
    expected: Any
    tolerance: Optional[float] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_operator_inputs(self) -> "BlockCheck":
        if not self.path.startswith("/"):
            raise ValueError("规格卡 check.path 必须是以 / 开头的 JSON Pointer")
        if self.operator == "within" and self.tolerance is None:
            raise ValueError("within 比较必须显式提供 tolerance")
        if self.operator == "range":
            if not isinstance(self.expected, dict) or set(self.expected) != {"min", "max"}:
                raise ValueError("range 的 expected 必须且只能包含 min、max")
        return self


class SymmetryTest(BlockAuditModel):
    id: str = Field(min_length=1, max_length=160)
    kind: Literal["reflection", "rotation"]
    tolerance: float = Field(gt=0)
    axis_point: Optional[Point3] = None
    axis_direction: Optional[Point3] = None
    center: Optional[Point3] = None
    order: Optional[int] = Field(default=None, ge=2, le=360)

    @model_validator(mode="after")
    def validate_definition(self) -> "SymmetryTest":
        if self.kind == "reflection":
            if self.axis_point is None or self.axis_direction is None:
                raise ValueError("reflection 必须显式提供 axis_point 和 axis_direction")
            if math.hypot(self.axis_direction[0], self.axis_direction[1]) == 0:
                raise ValueError("axis_direction 的 XY 分量不能同时为 0")
        elif self.center is None or self.order is None:
            raise ValueError("rotation 必须显式提供 center 和 order")
        return self


class BlockSpecCard(BlockAuditModel):
    name: str = Field(min_length=1, max_length=255)
    fingerprint_quantization: float = Field(gt=0)
    expected_fingerprint: Optional[str] = Field(
        pattern=r"^[0-9a-fA-F]{64}$",
        description="未知时必须显式写 null；工具仍计算实际指纹但比较为无法验证",
    )
    connection_tolerance: float = Field(gt=0)
    symmetry_tests: List[SymmetryTest] = Field(max_length=128)
    checks: List[BlockCheck] = Field(max_length=2_000)

    @field_validator("expected_fingerprint")
    @classmethod
    def normalize_fingerprint(cls, value: Optional[str]) -> Optional[str]:
        return value.lower() if value is not None else None


class BlockAuditManifest(BlockAuditModel):
    schema_version: Literal["1.0"]
    source_dwg: str = Field(min_length=1, max_length=2000)
    source_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    blocks: List[BlockSpecCard] = Field(min_length=1, max_length=2_000)

    @field_validator("source_sha256")
    @classmethod
    def normalize_sha256(cls, value: str) -> str:
        return value.lower()

    @model_validator(mode="after")
    def validate_unique_blocks(self) -> "BlockAuditManifest":
        folded = [item.name.casefold() for item in self.blocks]
        if len(folded) != len(set(folded)):
            raise ValueError("块规格卡中的块名不得重复（忽略大小写）")
        return self


def load_block_audit_manifest(
    path: str | Path,
) -> Tuple[Path, BlockAuditManifest]:
    manifest_path, _, data = read_utf8_json_object(path, label="块规格卡")
    if not manifest_path.name.casefold().endswith("_manifest.json"):
        raise ValueError("块规格卡文件名必须以 _manifest.json 结尾")
    manifest = BlockAuditManifest.model_validate(data)
    source = Path(manifest.source_dwg).expanduser()
    if not source.is_absolute():
        raise ValueError("块规格卡 source_dwg 必须是完整绝对路径，不自动推断")
    manifest.source_dwg = str(source.resolve())
    return manifest_path, manifest


def find_block_spec(manifest: BlockAuditManifest, block_name: str) -> BlockSpecCard:
    matches = [item for item in manifest.blocks if item.name.casefold() == block_name.casefold()]
    if not matches:
        raise ValueError(f"块规格卡中不存在指定块: {block_name}")
    return matches[0]


def _safe_get(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name)
    except Exception:
        return default


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    try:
        return [_json_value(item) for item in value]
    except TypeError:
        return str(value)


def _point3(value: Any) -> Optional[Point3]:
    try:
        values = list(value)
        if len(values) == 2:
            values.append(0.0)
        if len(values) < 3:
            return None
        return float(values[0]), float(values[1]), float(values[2])
    except (TypeError, ValueError):
        return None


def _flat_points(value: Any, stride: int) -> List[Point3]:
    try:
        values = [float(item) for item in value]
    except (TypeError, ValueError):
        return []
    points: List[Point3] = []
    for index in range(0, len(values) - stride + 1, stride):
        if stride == 2:
            points.append((values[index], values[index + 1], 0.0))
        else:
            points.append((values[index], values[index + 1], values[index + 2]))
    return points


def _bbox(entity: Any) -> Optional[Dict[str, List[float]]]:
    from cad_mcp.drawing.com_read import bounding_box

    try:
        low, high = bounding_box(entity)
        low_point, high_point = _point3(low), _point3(high)
        if low_point is None or high_point is None:
            return None
        return {"min": list(low_point), "max": list(high_point)}
    except Exception:
        return None


def _bbox_union(values: Iterable[Optional[Dict[str, List[float]]]]) -> Optional[Dict[str, List[float]]]:
    boxes = [value for value in values if value is not None]
    if not boxes:
        return None
    low = [min(box["min"][axis] for box in boxes) for axis in range(3)]
    high = [max(box["max"][axis] for box in boxes) for axis in range(3)]
    return {"min": low, "max": high}


def _entity_geometry(entity: Any, entity_type: str) -> Tuple[Dict[str, Any], List[Point3], List[Tuple[Point3, str]]]:
    geometry: Dict[str, Any] = {}
    samples: List[Point3] = []
    connection_candidates: List[Tuple[Point3, str]] = []

    def add_point(key: str, property_name: str, *, connection_kind: Optional[str] = None) -> None:
        point = _point3(_safe_get(entity, property_name, None))
        if point is not None:
            geometry[key] = list(point)
            samples.append(point)
            if connection_kind is not None:
                connection_candidates.append((point, connection_kind))

    if entity_type == "AcDbLine":
        add_point("start", "StartPoint", connection_kind="endpoint")
        add_point("end", "EndPoint", connection_kind="endpoint")
        geometry["length"] = _safe_get(entity, "Length", None)
    elif entity_type in {"AcDbPolyline", "AcDb2dPolyline", "AcDb3dPolyline"}:
        stride = 2 if entity_type == "AcDbPolyline" else 3
        points = _flat_points(_safe_get(entity, "Coordinates", []), stride)
        closed = bool(_safe_get(entity, "Closed", False))
        geometry.update({
            "vertices": [list(point) for point in points],
            "closed": closed,
            "length": _safe_get(entity, "Length", None),
            "area": _safe_get(entity, "Area", None),
            "constant_width": _safe_get(entity, "ConstantWidth", None),
            "elevation": _safe_get(entity, "Elevation", None),
        })
        samples.extend(points)
        if points and not closed:
            connection_candidates.extend([(points[0], "endpoint"), (points[-1], "endpoint")])
    elif entity_type == "AcDbCircle":
        add_point("center", "Center")
        radius = _safe_get(entity, "Radius", None)
        geometry.update({
            "radius": radius,
            "diameter": _safe_get(entity, "Diameter", None),
            "circumference": _safe_get(entity, "Circumference", None),
            "area": _safe_get(entity, "Area", None),
            "normal": _json_value(_safe_get(entity, "Normal", None)),
        })
        center = _point3(_safe_get(entity, "Center", None))
        if center is not None and isinstance(radius, (int, float)):
            samples.extend([
                (center[0] + radius, center[1], center[2]),
                (center[0] - radius, center[1], center[2]),
                (center[0], center[1] + radius, center[2]),
                (center[0], center[1] - radius, center[2]),
            ])
    elif entity_type == "AcDbArc":
        add_point("center", "Center")
        add_point("start", "StartPoint", connection_kind="endpoint")
        add_point("end", "EndPoint", connection_kind="endpoint")
        geometry.update({
            "radius": _safe_get(entity, "Radius", None),
            "start_angle": _safe_get(entity, "StartAngle", None),
            "end_angle": _safe_get(entity, "EndAngle", None),
            "total_angle": _safe_get(entity, "TotalAngle", None),
            "arc_length": _safe_get(entity, "ArcLength", None),
            "normal": _json_value(_safe_get(entity, "Normal", None)),
        })
    elif entity_type == "AcDbEllipse":
        add_point("center", "Center")
        add_point("start", "StartPoint", connection_kind="endpoint")
        add_point("end", "EndPoint", connection_kind="endpoint")
        geometry.update({
            "major_axis": _json_value(_safe_get(entity, "MajorAxis", None)),
            "major_radius": _safe_get(entity, "MajorRadius", None),
            "minor_radius": _safe_get(entity, "MinorRadius", None),
            "radius_ratio": _safe_get(entity, "RadiusRatio", None),
            "start_parameter": _safe_get(entity, "StartParameter", None),
            "end_parameter": _safe_get(entity, "EndParameter", None),
            "area": _safe_get(entity, "Area", None),
        })
    elif entity_type == "AcDbSpline":
        control_points = _flat_points(_safe_get(entity, "ControlPoints", []), 3)
        fit_points = _flat_points(_safe_get(entity, "FitPoints", []), 3)
        geometry.update({
            "degree": _safe_get(entity, "Degree", None),
            "control_points": [list(point) for point in control_points],
            "fit_points": [list(point) for point in fit_points],
            "knots": _json_value(_safe_get(entity, "Knots", None)),
            "weights": _json_value(_safe_get(entity, "Weights", None)),
            "closed": _safe_get(entity, "Closed", None),
            "rational": _safe_get(entity, "IsRational", None),
            "periodic": _safe_get(entity, "IsPeriodic", None),
        })
        add_point("start", "StartPoint", connection_kind="endpoint")
        add_point("end", "EndPoint", connection_kind="endpoint")
        samples.extend(control_points or fit_points)
    elif entity_type == "AcDbPoint":
        add_point("position", "Coordinates", connection_kind="explicit_point")
    elif entity_type in {"AcDbText", "AcDbMText", "AcDbAttributeDefinition", "AcDbAttribute"}:
        add_point("insertion_point", "InsertionPoint")
        add_point("alignment_point", "TextAlignmentPoint")
        geometry.update({
            "text": _safe_get(entity, "TextString", None),
            "height": _safe_get(entity, "Height", None),
            "rotation": _safe_get(entity, "Rotation", None),
            "style_name": _safe_get(entity, "StyleName", None),
            "alignment": _safe_get(entity, "Alignment", None),
            "width": _safe_get(entity, "Width", None),
            "scale_factor": _safe_get(entity, "ScaleFactor", None),
        })
        if entity_type in {"AcDbAttributeDefinition", "AcDbAttribute"}:
            geometry.update({
                "tag": _safe_get(entity, "TagString", None),
                "prompt": _safe_get(entity, "PromptString", None),
                "constant": _safe_get(entity, "Constant", None),
                "invisible": _safe_get(entity, "Invisible", None),
                "preset": _safe_get(entity, "Preset", None),
                "verify": _safe_get(entity, "Verify", None),
                "lock_position": _safe_get(entity, "LockPosition", None),
            })
    elif entity_type == "AcDbHatch":
        add_point("origin", "Origin")
        geometry.update({
            "pattern_name": _safe_get(entity, "PatternName", None),
            "pattern_type": _safe_get(entity, "PatternType", None),
            "pattern_scale": _safe_get(entity, "PatternScale", None),
            "pattern_angle": _safe_get(entity, "PatternAngle", None),
            "associative": _safe_get(entity, "AssociativeHatch", None),
            "number_of_loops": _safe_get(entity, "NumberOfLoops", None),
            "area": _safe_get(entity, "Area", None),
            "hatch_style": _safe_get(entity, "HatchStyle", None),
            "normal": _json_value(_safe_get(entity, "Normal", None)),
            "elevation": _safe_get(entity, "Elevation", None),
        })
    elif entity_type in {"AcDbSolid", "AcDbTrace", "AcDbFace"}:
        for index in range(1, 5):
            add_point(f"coordinate_{index}", f"Coordinate{index}")
    elif "Dimension" in entity_type:
        add_point("text_position", "TextPosition")
        geometry.update({
            "measurement": _safe_get(entity, "Measurement", None),
            "text_override": _safe_get(entity, "TextOverride", None),
            "style_name": _safe_get(entity, "StyleName", None),
        })
    return geometry, samples, connection_candidates


class ComBlockExtractor:
    """Bounded recursive extractor for AutoCAD block definitions."""

    def __init__(self, document: Any):
        self.document = document
        self.entity_count = 0
        self.complete = True
        self.warnings: List[str] = []
        self.samples: List[Point3] = []
        self.connection_candidates: List[Tuple[Point3, str, str]] = []
        self.root_geometry_analysis_complete = True
        self._blocks = self._block_map(document)

    @staticmethod
    def _block_map(document: Any) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        blocks = document.Blocks
        for index in range(int(blocks.Count)):
            block = blocks.Item(index)
            result[str(block.Name).casefold()] = block
        return result

    def extract(self, block: Any, stack: Tuple[str, ...] = (), depth: int = 0) -> Dict[str, Any]:
        name = str(block.Name)
        folded = name.casefold()
        if depth > MAX_BLOCK_TREE_DEPTH:
            self.complete = False
            self.warnings.append(f"嵌套深度超过安全上限 {MAX_BLOCK_TREE_DEPTH}: {name}")
            return {"name": name, "complete": False, "reason": "max_depth_exceeded", "entities": []}
        if folded in stack:
            self.complete = False
            cycle = " -> ".join((*stack, folded))
            self.warnings.append(f"检测到循环块引用: {cycle}")
            return {"name": name, "complete": False, "reason": "cyclic_reference", "entities": []}
        origin = _point3(_safe_get(block, "Origin", None))
        entities: List[Dict[str, Any]] = []
        for index in range(int(block.Count)):
            if self.entity_count >= MAX_BLOCK_TREE_ENTITIES:
                self.complete = False
                self.warnings.append(f"实体数超过安全上限 {MAX_BLOCK_TREE_ENTITIES}")
                break
            entity = block.Item(index)
            path = f"/{name}/entities/{index}"
            entities.append(self._extract_entity(entity, path, (*stack, folded), depth))
            self.entity_count += 1
        return {
            "name": name,
            "base_point": list(origin) if origin is not None else None,
            "entity_count": int(block.Count),
            "complete": self.complete,
            "entities": entities,
        }

    def _extract_entity(
        self,
        entity: Any,
        path: str,
        stack: Tuple[str, ...],
        depth: int,
    ) -> Dict[str, Any]:
        entity_type = str(
            _safe_get(entity, "EntityName", _safe_get(entity, "ObjectName", "Unknown"))
        )
        box = _bbox(entity)
        geometry, samples, candidates = _entity_geometry(entity, entity_type)
        if samples:
            self.samples.extend(samples)
        elif box is not None:
            low, high = box["min"], box["max"]
            self.samples.extend([
                (low[0], low[1], low[2]),
                (low[0], high[1], low[2]),
                (high[0], low[1], low[2]),
                (high[0], high[1], high[2]),
            ])
        for point, kind in candidates:
            self.connection_candidates.append((point, kind, path))
        unverified: List[str] = []
        if not geometry and entity_type not in {"AcDbBlockReference", "AcDbMInsertBlock"}:
            unverified.append("geometry")
            self.complete = False
        if box is None:
            unverified.append("bounding_box")
            self.complete = False
        node: Dict[str, Any] = {
            "path": path,
            "handle": str(_safe_get(entity, "Handle", "")),
            "entity_type": entity_type,
            "layer": _safe_get(entity, "Layer", None),
            "linetype": _safe_get(entity, "Linetype", None),
            "lineweight": _safe_get(entity, "Lineweight", None),
            "color": _safe_get(entity, "Color", None),
            "geometry": _json_value(geometry),
            "bounding_box": box,
            "unverified_fields": unverified,
        }
        if entity_type in {"AcDbBlockReference", "AcDbMInsertBlock"}:
            # The entity tree and fingerprint include the nested transform, but
            # symmetry/topology samples are not promoted through that transform.
            # Report those two derived analyses as unverifiable instead of
            # silently treating child-local coordinates as parent coordinates.
            self.root_geometry_analysis_complete = False
            name = str(_safe_get(entity, "Name", ""))
            effective_name = str(_safe_get(entity, "EffectiveName", name))
            insertion = _point3(_safe_get(entity, "InsertionPoint", None))
            attributes: List[Dict[str, Any]] = []
            if bool(_safe_get(entity, "HasAttributes", False)):
                try:
                    for attribute in entity.GetAttributes():
                        attributes.append({
                            "tag": _safe_get(attribute, "TagString", None),
                            "text": _safe_get(attribute, "TextString", None),
                        })
                except Exception:
                    node["unverified_fields"].append("attributes")
                    self.complete = False
            node["nested_reference"] = {
                "name": name,
                "effective_name": effective_name,
                "insertion_point": list(insertion) if insertion else None,
                "scale": [
                    _safe_get(entity, "XScaleFactor", None),
                    _safe_get(entity, "YScaleFactor", None),
                    _safe_get(entity, "ZScaleFactor", None),
                ],
                "rotation": _safe_get(entity, "Rotation", None),
                "normal": _json_value(_safe_get(entity, "Normal", None)),
                "attributes": attributes,
            }
            referenced = self._blocks.get(name.casefold())
            if referenced is None:
                node["unverified_fields"].append("nested_definition")
                self.complete = False
            else:
                node["children"] = self.extract(referenced, stack, depth + 1)
        return node


def _distance(a: Sequence[float], b: Sequence[float]) -> float:
    return math.sqrt(sum((float(a[index]) - float(b[index])) ** 2 for index in range(3)))


def _reflect(point: Point3, axis_point: Point3, direction: Point3) -> Point3:
    dx, dy = direction[0], direction[1]
    length = math.hypot(dx, dy)
    ux, uy = dx / length, dy / length
    px, py = point[0] - axis_point[0], point[1] - axis_point[1]
    projection = px * ux + py * uy
    rx = 2 * projection * ux - px
    ry = 2 * projection * uy - py
    return axis_point[0] + rx, axis_point[1] + ry, point[2]


def _rotate(point: Point3, center: Point3, angle: float) -> Point3:
    x, y = point[0] - center[0], point[1] - center[1]
    cosine, sine = math.cos(angle), math.sin(angle)
    return (
        center[0] + x * cosine - y * sine,
        center[1] + x * sine + y * cosine,
        point[2],
    )


def evaluate_symmetry(
    samples: Sequence[Point3],
    tests: Sequence[SymmetryTest],
    *,
    evidence_complete: bool,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for test in tests:
        if not samples or not evidence_complete:
            results.append({
                "id": test.id,
                "kind": test.kind,
                "status": "无法验证",
                "max_residual": None,
                "tolerance": test.tolerance,
                "reason_code": "incomplete_geometry_evidence",
            })
            continue
        transformed: List[Point3]
        if test.kind == "reflection":
            transformed = [
                _reflect(point, test.axis_point, test.axis_direction)  # type: ignore[arg-type]
                for point in samples
            ]
        else:
            angle = 2 * math.pi / int(test.order)
            transformed = [
                _rotate(point, test.center, angle)  # type: ignore[arg-type]
                for point in samples
            ]
        residuals = [min(_distance(point, candidate) for candidate in samples) for point in transformed]
        maximum = max(residuals, default=math.inf)
        results.append({
            "id": test.id,
            "kind": test.kind,
            "status": "通过" if maximum <= test.tolerance else "偏差",
            "max_residual": maximum,
            "tolerance": test.tolerance,
            "sample_count": len(samples),
            "reason_code": "within_tolerance" if maximum <= test.tolerance else "residual_exceeds_tolerance",
        })
    return results


def cluster_connection_points(
    candidates: Sequence[Tuple[Point3, str, str]], tolerance: float, *, complete: bool
) -> Dict[str, Any]:
    clusters: List[Dict[str, Any]] = []
    for point, kind, path in candidates:
        match = next(
            (cluster for cluster in clusters if _distance(cluster["point"], point) <= tolerance),
            None,
        )
        if match is None:
            match = {"point": list(point), "sources": [], "kinds": []}
            clusters.append(match)
        match["sources"].append(path)
        match["kinds"].append(kind)
    connection_points = []
    for cluster in clusters:
        degree = len(cluster["sources"])
        explicit = "explicit_point" in cluster["kinds"]
        if explicit or degree == 1:
            connection_points.append({
                "point": cluster["point"],
                "kind": "explicit_point" if explicit else "open_endpoint",
                "degree": degree,
                "sources": sorted(cluster["sources"]),
            })
    connection_points.sort(key=lambda item: tuple(item["point"]))
    return {
        "complete": complete,
        "tolerance": tolerance,
        "points": connection_points,
        "candidate_cluster_count": len(clusters),
    }


def _quantized(value: float, quantum: float) -> str:
    q = Decimal(str(quantum))
    quantized = (Decimal(str(value)) / q).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN) * q
    return format(quantized.normalize(), "f")


def _canonical_geometry(value: Any, quantum: float) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return _quantized(value, quantum)
    if isinstance(value, list):
        items = [_canonical_geometry(item, quantum) for item in value]
        if items and all(isinstance(item, dict) and "entity_type" in item for item in items):
            items.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return items
    if isinstance(value, dict):
        omitted = {
            "path", "handle", "layer", "linetype", "lineweight", "color",
            "bounding_box", "unverified_fields", "complete", "reason",
        }
        return {
            key: _canonical_geometry(item, quantum)
            for key, item in sorted(value.items())
            if key not in omitted
        }
    return str(value)


def geometry_fingerprint(entity_tree: Dict[str, Any], quantum: float) -> Tuple[str, Dict[str, Any]]:
    payload = {
        "algorithm": "cad-mcp:block-geometry:v1",
        "quantization": _quantized(quantum, quantum),
        "base_point": _canonical_geometry(entity_tree.get("base_point"), quantum),
        "entities": _canonical_geometry(entity_tree.get("entities", []), quantum),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), payload


def _json_pointer(document: Any, pointer: str) -> Any:
    current = document
    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list):
            try:
                current = current[int(token)]
            except (ValueError, IndexError):
                return _MISSING
        else:
            return _MISSING
    return current


def _within(actual: Any, expected: Any, tolerance: float) -> bool:
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return abs(float(actual) - float(expected)) <= tolerance
    if isinstance(actual, (list, tuple)) and isinstance(expected, (list, tuple)):
        return len(actual) == len(expected) and all(
            _within(a, e, tolerance) for a, e in zip(actual, expected)
        )
    return False


def compare_check(actual_document: Dict[str, Any], check: BlockCheck) -> Dict[str, Any]:
    actual = _json_pointer(actual_document, check.path)
    if actual is _MISSING or actual is None:
        return {
            "id": check.id,
            "path": check.path,
            "status": "无法验证",
            "expected": check.expected,
            "actual": None,
            "tolerance": check.tolerance,
            "reason_code": "actual_missing_or_unreadable",
        }
    try:
        if check.operator == "equals":
            passed = actual == check.expected
        elif check.operator == "within":
            passed = _within(actual, check.expected, float(check.tolerance))
        elif check.operator == "unordered_equals":
            passed = Counter(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in actual) == Counter(
                json.dumps(item, ensure_ascii=False, sort_keys=True) for item in check.expected
            )
        elif check.operator == "contains":
            if isinstance(actual, list):
                passed = all(item in actual for item in check.expected)
            elif isinstance(actual, str):
                passed = str(check.expected) in actual
            else:
                raise TypeError("contains 只支持列表或字符串")
        else:
            passed = float(check.expected["min"]) <= float(actual) <= float(check.expected["max"])
    except (TypeError, ValueError, KeyError):
        return {
            "id": check.id,
            "path": check.path,
            "status": "无法验证",
            "expected": check.expected,
            "actual": actual,
            "tolerance": check.tolerance,
            "reason_code": "incompatible_comparison_types",
        }
    return {
        "id": check.id,
        "path": check.path,
        "status": "通过" if passed else "偏差",
        "expected": check.expected,
        "actual": actual,
        "tolerance": check.tolerance,
        "reason_code": "matched" if passed else "mismatch",
    }


def summarize_entity_tree(tree: Dict[str, Any]) -> Dict[str, Any]:
    nodes: List[Dict[str, Any]] = []

    def visit(block: Dict[str, Any]) -> None:
        for entity in block.get("entities", []):
            nodes.append(entity)
            child = entity.get("children")
            if isinstance(child, dict):
                visit(child)

    visit(tree)
    texts = []
    attdefs = []
    nested = []
    for node in nodes:
        entity_type = node.get("entity_type")
        geometry = node.get("geometry", {})
        if entity_type in {"AcDbText", "AcDbMText"}:
            texts.append({"path": node["path"], **geometry})
        if entity_type == "AcDbAttributeDefinition":
            attdefs.append({"path": node["path"], **geometry})
        if "nested_reference" in node:
            nested.append({"path": node["path"], **node["nested_reference"]})
    return {
        "entity_count_recursive": len(nodes),
        "entity_types": dict(sorted(Counter(node.get("entity_type") for node in nodes).items())),
        "layers": sorted({node.get("layer") for node in nodes if node.get("layer") is not None}, key=str.casefold),
        "linetypes": sorted({node.get("linetype") for node in nodes if node.get("linetype") is not None}, key=str.casefold),
        "lineweights": sorted({node.get("lineweight") for node in nodes if node.get("lineweight") is not None}),
        "texts": texts,
        "attdefs": attdefs,
        "nested_references": nested,
    }


def build_block_audit(block: Any, document: Any, spec: BlockSpecCard) -> Dict[str, Any]:
    extractor = ComBlockExtractor(document)
    tree = extractor.extract(block)
    top_boxes = [entity.get("bounding_box") for entity in tree.get("entities", [])]
    bounding = _bbox_union(top_boxes)
    if bounding is None:
        center = None
        size = None
        extractor.complete = False
    else:
        center = [
            (bounding["min"][axis] + bounding["max"][axis]) / 2
            for axis in range(3)
        ]
        size = [
            bounding["max"][axis] - bounding["min"][axis]
            for axis in range(3)
        ]
    fingerprint, canonical_payload = geometry_fingerprint(tree, spec.fingerprint_quantization)
    symmetry_results = evaluate_symmetry(
        extractor.samples,
        spec.symmetry_tests,
        evidence_complete=(
            extractor.complete and extractor.root_geometry_analysis_complete
        ),
    )
    audit: Dict[str, Any] = {
        "block": {
            "name": str(block.Name),
            "base_point": tree.get("base_point"),
            "entity_tree_complete": extractor.complete,
        },
        "entity_tree": tree,
        "geometry": {
            "bounding_box": None if bounding is None else {**bounding, "size": size},
            "center": {
                "bbox_center": center,
                "area_centroid": None,
                "area_centroid_status": "无法验证",
                "reason_code": "mixed_or_open_block_geometry",
            },
            "symmetry": symmetry_results,
            "connection_points": cluster_connection_points(
                extractor.connection_candidates,
                spec.connection_tolerance,
                complete=(
                    extractor.complete and extractor.root_geometry_analysis_complete
                ),
            ),
        },
        "properties": summarize_entity_tree(tree),
        "fingerprint": {
            "algorithm": "cad-mcp:block-geometry:v1",
            "quantization": spec.fingerprint_quantization,
            "sha256": fingerprint,
            "complete": extractor.complete,
            "canonical_payload": canonical_payload,
        },
        "warnings": extractor.warnings,
    }
    comparisons = [compare_check(audit, check) for check in spec.checks]
    for index, (declared, actual) in enumerate(zip(spec.symmetry_tests, symmetry_results)):
        comparisons.append({
            "id": f"symmetry:{declared.id}",
            "path": f"/geometry/symmetry/{index}",
            "status": actual["status"],
            "expected": declared.model_dump(mode="json"),
            "actual": {
                "max_residual": actual.get("max_residual"),
                "sample_count": actual.get("sample_count"),
            },
            "tolerance": declared.tolerance,
            "reason_code": actual["reason_code"],
        })
    if spec.expected_fingerprint is None:
        comparisons.append({
            "id": "geometry_fingerprint",
            "path": "/fingerprint/sha256",
            "status": "无法验证",
            "expected": None,
            "actual": fingerprint,
            "tolerance": None,
            "reason_code": "expected_fingerprint_not_declared",
        })
    else:
        matched = extractor.complete and fingerprint == spec.expected_fingerprint
        comparisons.append({
            "id": "geometry_fingerprint",
            "path": "/fingerprint/sha256",
            "status": "通过" if matched else ("偏差" if extractor.complete else "无法验证"),
            "expected": spec.expected_fingerprint,
            "actual": fingerprint,
            "tolerance": None,
            "reason_code": (
                "matched" if matched else
                "mismatch" if extractor.complete else
                "incomplete_geometry_evidence"
            ),
        })
    if not extractor.complete:
        comparisons.append({
            "id": "entity_tree_completeness",
            "path": "/block/entity_tree_complete",
            "status": "无法验证",
            "expected": True,
            "actual": False,
            "tolerance": None,
            "reason_code": "entity_tree_incomplete",
        })
    statuses = {item["status"] for item in comparisons}
    overall: AuditStatus = (
        "偏差" if "偏差" in statuses else
        "无法验证" if "无法验证" in statuses else
        "通过"
    )
    audit["comparison"] = {
        "overall_status": overall,
        "items": comparisons,
        "counts": dict(Counter(item["status"] for item in comparisons)),
    }
    return audit
