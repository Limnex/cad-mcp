"""环境工程标注的确定性几何生成与纸面尺寸换算。"""

from __future__ import annotations

import math
from typing import List, Tuple

from cad_mcp.core.contracts import AnnotationSpec, CadOperation, Point2D, ValidationIssue


def model_size(paper_size: float, scale_denominator: float) -> float:
    if paper_size <= 0 or scale_denominator <= 0:
        raise ValueError("图纸尺寸和比例分母必须大于 0")
    return float(paper_size) * float(scale_denominator)


def arrow_points(anchor: Point2D, rotation: float, size: float) -> Tuple[Point2D, Point2D, Point2D]:
    """返回箭头尖端及两翼端点。"""

    angle = math.radians(rotation)
    wing = math.radians(150)
    left = (anchor[0] + size * math.cos(angle + wing),
            anchor[1] + size * math.sin(angle + wing))
    right = (anchor[0] + size * math.cos(angle - wing),
             anchor[1] + size * math.sin(angle - wing))
    return anchor, left, right


def _text_operation(annotation: AnnotationSpec, scale_denominator: float, text: str) -> CadOperation:
    return CadOperation(
        kind="text",
        semantic_id=annotation.id,
        view_id=annotation.view_id,
        role=f"annotation_{annotation.annotation_type}",
        params={
            "text": text,
            "x": annotation.anchor[0],
            "y": annotation.anchor[1],
            "height": model_size(annotation.paper_height, scale_denominator),
            "rotation": annotation.rotation,
            "layer": annotation.layer,
        },
        metadata={
            "object_type": "annotation",
            "annotation_type": annotation.annotation_type,
            "target_id": annotation.target_id,
        },
    )


def format_annotation_text(annotation: AnnotationSpec) -> str:
    if annotation.text:
        return annotation.text
    value = annotation.value
    options = annotation.options
    kind = annotation.annotation_type
    if kind in {"elevation", "water_level"}:
        if value is None:
            return ""
        decimals = int(options.get("decimals", 3))
        prefix = str(options.get("prefix", ""))
        return f"{prefix}{value:+.{decimals}f}" if options.get("show_sign", True) else f"{prefix}{value:.{decimals}f}"
    if kind == "slope":
        if value is None:
            return ""
        form = options.get("format", "percent")
        if form == "ratio":
            return f"i={value:.6f}".rstrip("0").rstrip(".")
        return f"i={value * 100:.3f}%".replace(".000", "")
    if kind == "coordinate":
        x, y = annotation.anchor
        return f"X={x:.3f}\nY={y:.3f}"
    return "" if value is None else str(value)


def compile_annotation(annotation: AnnotationSpec, scale_denominator: float) -> List[CadOperation]:
    """把一种工程标注编译为原生文字、线、圆或多段线操作。"""

    kind = annotation.annotation_type
    text = format_annotation_text(annotation)
    size = model_size(float(annotation.options.get("symbol_paper_size", 3.0)), scale_denominator)
    metadata = {
        "object_type": "annotation",
        "annotation_type": kind,
        "target_id": annotation.target_id,
    }
    operations: List[CadOperation] = []

    if kind in {"text", "mtext", "network_label", "note"}:
        op = _text_operation(annotation, scale_denominator, text)
        if kind in {"mtext", "note"}:
            op.kind = "mtext"
            op.params["width"] = model_size(
                float(annotation.options.get("paper_width", 50.0)), scale_denominator)
        operations.append(op)
        return operations

    if kind in {"elevation", "water_level"}:
        x, y = annotation.anchor
        triangle = [(x, y), (x - size, y - size * 0.55), (x + size, y - size * 0.55)]
        operations.append(CadOperation(
            kind="polyline", semantic_id=annotation.id, view_id=annotation.view_id,
            role="elevation_symbol",
            params={"points": triangle, "close": True, "layer": annotation.layer},
            metadata=metadata,
        ))
        text_anchor = (x + size * 1.3, y)
        text_annotation = annotation.model_copy(update={"anchor": text_anchor})
        operations.append(_text_operation(text_annotation, scale_denominator, text))
        return operations

    if kind == "coordinate":
        x, y = annotation.anchor
        operations.extend([
            CadOperation(kind="line", semantic_id=annotation.id, view_id=annotation.view_id,
                         role="coordinate_cross", params={"x1": x - size, "y1": y,
                         "x2": x + size, "y2": y, "layer": annotation.layer}, metadata=metadata),
            CadOperation(kind="line", semantic_id=annotation.id, view_id=annotation.view_id,
                         role="coordinate_cross", params={"x1": x, "y1": y - size,
                         "x2": x, "y2": y + size, "layer": annotation.layer}, metadata=metadata),
        ])
        text_anchor = annotation.leader_point or (x + size * 1.5, y + size * 1.5)
        operations.append(_text_operation(annotation.model_copy(update={"anchor": text_anchor}),
                                          scale_denominator, text))
        return operations

    if kind in {"flow_arrow", "slope"}:
        tip, left, right = arrow_points(annotation.anchor, annotation.rotation, size)
        tail_angle = math.radians(annotation.rotation + 180.0)
        tail = (tip[0] + size * 2.5 * math.cos(tail_angle),
                tip[1] + size * 2.5 * math.sin(tail_angle))
        operations.append(CadOperation(
            kind="polyline", semantic_id=annotation.id, view_id=annotation.view_id,
            role=f"{kind}_symbol",
            params={"points": [tail, tip, left, tip, right], "close": False,
                    "layer": annotation.layer},
            metadata=metadata,
        ))
        if text:
            text_anchor = annotation.leader_point or (tail[0], tail[1] + size * 0.8)
            operations.append(_text_operation(annotation.model_copy(update={"anchor": text_anchor}),
                                              scale_denominator, text))
        return operations

    if kind in {"detail_index", "section_marker"}:
        x, y = annotation.anchor
        operations.append(CadOperation(
            kind="circle", semantic_id=annotation.id, view_id=annotation.view_id,
            role=f"{kind}_symbol",
            params={"cx": x, "cy": y, "radius": size, "layer": annotation.layer},
            metadata=metadata,
        ))
        operations.append(_text_operation(annotation, scale_denominator, text))
        if annotation.leader_point:
            operations.append(CadOperation(
                kind="line", semantic_id=annotation.id, view_id=annotation.view_id,
                role=f"{kind}_leader",
                params={"x1": x, "y1": y, "x2": annotation.leader_point[0],
                        "y2": annotation.leader_point[1], "layer": annotation.layer},
                metadata=metadata,
            ))
        return operations

    if kind == "north_arrow":
        tip, left, right = arrow_points(annotation.anchor, annotation.rotation + 90.0, size * 2)
        operations.append(CadOperation(
            kind="polyline", semantic_id=annotation.id, view_id=annotation.view_id,
            role="north_arrow",
            params={"points": [tip, left, right], "close": True, "layer": annotation.layer},
            metadata=metadata,
        ))
        n_anchor = (tip[0], tip[1] + size)
        operations.append(_text_operation(annotation.model_copy(update={"anchor": n_anchor}),
                                          scale_denominator, text or "N"))
        return operations

    # 未知扩展标注仍保留其语义，但必须显式给文字；不猜测符号。
    if not text:
        raise ValueError(f"标注类型 {kind} 没有内置符号，也未提供 text")
    operations.append(_text_operation(annotation, scale_denominator, text))
    return operations


def estimate_text_bounds(annotation: AnnotationSpec, scale_denominator: float) -> Tuple[float, float, float, float]:
    """用于预检的保守文字包围盒，不替代 AutoCAD 的精确范围。"""

    text = format_annotation_text(annotation)
    height = model_size(annotation.paper_height, scale_denominator)
    lines = text.splitlines() or [""]
    width = max(len(line) for line in lines) * height
    total_height = len(lines) * height * 1.4
    x, y = annotation.anchor
    return x, y, x + width, y + total_height


def validate_annotation(annotation: AnnotationSpec) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    if not format_annotation_text(annotation) and annotation.annotation_type not in {"flow_arrow"}:
        issues.append(ValidationIssue(
            severity="error",
            rule_id="ANNOTATION_VALUE_MISSING",
            message=f"标注 {annotation.id} 缺少可显示的文字或数值",
            semantic_ids=[annotation.id],
            location=annotation.anchor,
        ))
    return issues
