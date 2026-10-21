"""Modelspace XY previews, persisted before constructing MCP image content."""

from __future__ import annotations

import base64
from datetime import datetime
import io
import math
from pathlib import Path
import struct
import tempfile
import uuid

from mcp.types import CallToolResult, ImageContent, TextContent

from cad_mcp.core.runtime import success_json


MAX_INLINE_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PREVIEW_ENTITIES = 100_000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def validate_region(*, x_min, y_min, x_max, y_max, width=1200, height=800,
                    background="white"):
    coordinates = (x_min, y_min, x_max, y_max)
    if not all(math.isfinite(v) and abs(v) <= 1e12 for v in coordinates):
        raise ValueError("预览坐标必须有限，且绝对值不超过 1e12")
    if x_max - x_min < 1e-6 or y_max - y_min < 1e-6:
        raise ValueError("预览范围须从左下到右上排列，宽高均不得小于 1e-6 图纸单位")
    if any(type(v) is not int or not 64 <= v <= 2048 for v in (width, height)):
        raise ValueError("图片宽高必须是 64 至 2048 的整数像素")
    if background not in {"white", "black"}:
        raise ValueError("background 必须是 white 或 black")


def render_region(doc, *, log_dir: Path, mode: str, x_min: float, y_min: float,
                  x_max: float, y_max: float, width: int = 1200,
                  height: int = 800, background: str = "white") -> dict:
    """Render the live document without saving it, with fixed limits and aspect."""
    validate_region(x_min=x_min, y_min=y_min, x_max=x_max, y_max=y_max,
                    width=width, height=height, background=background)
    msp = doc.modelspace()
    if len(msp) > MAX_PREVIEW_ENTITIES:
        raise ValueError(f"模型空间超过预览上限 {MAX_PREVIEW_ENTITIES} 个实体")

    # Lazy imports keep ordinary tools and diagnostics independent of rendering.
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.config import Configuration, ImagePolicy
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    from ezdxf.addons.drawing.properties import LayoutProperties
    from ezdxf.fonts import fonts
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    warnings = [
        "二维 WCS XY 模型空间预览，保持比例；宽高比不同时留边。",
        "ezdxf 渲染可能与 AutoCAD 原生显示不同，尤其是替代字体、代理对象和外部参照。",
        "外部栅格图像仅显示占位边框，不加载引用的图片文件。",
    ]

    class PreviewFrontend(Frontend):
        def log_message(self, message):
            # Surface skipped/unsupported objects without flooding tool responses.
            if len(warnings) < 24 and message not in warnings:
                warnings.append(str(message)[:500])

    # A power-of-two DPI keeps integer canvas dimensions exact in binary floats.
    figure = Figure(figsize=(width / 128, height / 128), dpi=128,
                    facecolor=background)
    canvas = FigureCanvasAgg(figure)
    try:
        axes = figure.add_axes((0, 0, 1, 1))
        backend = MatplotlibBackend(axes, adjust_figure=False)
        properties = LayoutProperties.from_layout(msp)
        properties.set_colors("#ffffff" if background == "white" else "#000000")
        context = RenderContext(doc)
        # ezdxf does not combine SHX bigfonts; substitute only in the render context.
        for style in doc.styles:
            if not style.dxf.get("bigfont", ""):
                continue
            for font_name in ("msyh.ttc", "simsun.ttc", "NotoSansCJK-Regular.ttc"):
                if fonts.font_manager.has_font(font_name):
                    context.fonts[style.dxf.name.lower()] = fonts.resolve_font_face(font_name)
                    warnings.append(f"文字样式 {style.dxf.name} 的 SHX 大字体以 {font_name} 预览替代，原样式未修改。")
                    break
            else:
                warnings.append(f"文字样式 {style.dxf.name} 使用 SHX 大字体，未找到中文替代字体，可能缺字。")
        frontend = PreviewFrontend(
            context, backend, Configuration(image_policy=ImagePolicy.RECT))
        frontend.draw_layout(msp, finalize=False, layout_properties=properties)
        axes.set_xlim(x_min, x_max)
        axes.set_ylim(y_min, y_max)
        axes.set_aspect("equal", adjustable="box")
        axes.set_axis_off()
        buffer = io.BytesIO()
        canvas.print_png(buffer)
        png = buffer.getvalue()
    finally:
        figure.clear()

    if (not png.startswith(PNG_SIGNATURE) or len(png) < 24
            or struct.unpack(">II", png[16:24]) != (width, height)):
        raise RuntimeError("渲染器未返回指定尺寸的有效 PNG 图片，未发布预览")
    now = datetime.now().astimezone()
    directory = Path(log_dir).expanduser().resolve().parent / "images" / now.strftime("%Y-%m-%d")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"preview_{now:%H%M%S_%f}_{uuid.uuid4().hex}.png"
    # Exclusive creation guarantees that an existing artifact is never overwritten.
    created = False
    try:
        with path.open("xb") as stream:
            created = True
            stream.write(png)
    except OSError:
        # Remove only a file created by this attempt; existing artifacts stay intact.
        if created:
            path.unlink(missing_ok=True)
        raise
    return {
        "ok": True, "mode": mode, "image_path": str(path),
        "mime_type": "image/png", "width": width, "height": height,
        "bounds": {"x_min": x_min, "y_min": y_min, "x_max": x_max, "y_max": y_max},
        "coordinate_system": "WCS_XY", "space": "model",
        "background": background, "renderer": "ezdxf_matplotlib",
        "source": "live_dxf" if mode == "dxf" else "autocad_dxf_export",
        "byte_size": len(png), "created_at": now.isoformat(), "warnings": warnings,
    }


def preview_acad_region(backend, **kwargs) -> dict:
    """Export a disposable DXF snapshot, without SaveAs, zoom or plot changes."""
    import ezdxf

    validate_region(**{key: value for key, value in kwargs.items() if key != "log_dir"})
    with tempfile.TemporaryDirectory(prefix="cad-mcp-preview-") as directory:
        snapshot = Path(directory) / "snapshot.dxf"
        selection = backend.doc.SelectionSets.Add("CAD_MCP_PREVIEW_" + uuid.uuid4().hex)
        try:
            # ActiveX requires a valid selection set even though DXF ignores it.
            backend.doc.Export(str(snapshot.with_suffix("")), "DXF", selection)
        finally:
            selection.Delete()
        if not snapshot.is_file() or snapshot.stat().st_size == 0:
            raise RuntimeError("AutoCAD 未生成临时 DXF，未生成预览；未保存原图纸")
        doc = ezdxf.readfile(snapshot)
        return render_region(doc, mode="acad", **kwargs)


def preview_result(metadata: dict, *, include_image: bool = True) -> CallToolResult:
    """Always include the saved path as text, even if inlining the image fails."""
    payload = dict(metadata, image_included=False)
    image = None
    if include_image:
        try:
            # Bound reads as well as wire size, including if the file has changed.
            with Path(metadata["image_path"]).open("rb") as stream:
                png = stream.read(MAX_INLINE_IMAGE_BYTES + 1)
            if len(png) > MAX_INLINE_IMAGE_BYTES:
                raise ValueError("图片超过 8 MiB 内联上限，请按 image_path 读取")
            if not png.startswith(PNG_SIGNATURE):
                raise ValueError("已保存的 PNG 无效，请检查 image_path")
            image = ImageContent(type="image", mimeType="image/png",
                                 data=base64.b64encode(png).decode("ascii"))
            payload["image_included"] = True
        except Exception as exc:
            payload["image_error"] = str(exc)
    content = [TextContent(type="text", text=success_json(payload))]
    if image is not None:
        content.append(image)
    return CallToolResult(content=content)
