#!/usr/bin/env python3
"""
CAD 绘图核心库 — 封装 ezdxf，提供 AutoCAD 平面图绘制能力。

本模块与 MCP 解耦，可独立导入测试。MCP 服务器通过 cad_mcp.server 调用本模块。

句柄约定：所有句柄统一显示为**十进制字符串**（底层为十六进制，显示时统一转换），
保证与 COM 后端（cad_acad.py）输出格式一致。
"""

import io
import hashlib
import math
from pathlib import Path
from typing import Dict, Any, Optional, List, Sequence, Tuple

import ezdxf
from ezdxf import bbox
from ezdxf.enums import TextEntityAlignment
from ezdxf.math import offset_vertices_2d
from ezdxf.tools import pattern as hatch_patterns

from cad_mcp.dwt.contracts import build_drawing_settings_payload
from cad_mcp.dwt.dxf import DxfDwtMixin
from cad_mcp.core.geometry import (
    MAX_DRAWING_ISSUES,
    MAX_DRAWING_SCAN_ENTITIES,
    join_polyline_parts,
    scan_line_circle_issues,
    trim_polyline_detailed,
    validate_bulk_creation,
    validate_closed_polygon,
    validate_diameter_dimension,
    validate_hatch_parameters,
    validate_table_data,
)
from cad_mcp.core.registry import (
    XDATA_APP_NAME,
    ManagedEntityRecord,
    canonical_json,
    pack_metadata,
    unpack_metadata,
)
from cad_mcp.core.runtime import health_metadata
from cad_mcp.core.open_paths import resolve_open_drawing_path
from cad_mcp.drawing.hatch_regions import fill_dxf_region

# 单位常量（ezdxf $INSUNITS）：4 = 毫米
UNIT_MM = 4

# 预设图层
LAYER_WALL = "WALL"
LAYER_DIM = "DIM"
LAYER_FURNITURE = "FURNITURE"
LAYER_TEXT = "TEXT"
LAYER_FILL = "FILL"

# 预设图层颜色（AutoCAD 索引色号）
LAYER_COLORS = {
    LAYER_WALL: 7,       # 白/黑
    LAYER_DIM: 2,        # 黄
    LAYER_FURNITURE: 5,  # 蓝
    LAYER_TEXT: 4,       # 青
    LAYER_FILL: 6,       # 品红
}

Point = Tuple[float, float]

# 标准图纸幅面（宽×高，毫米），横放（工程图框主流方向），用于 draw_title_block 的 size 参数。
PAPER_SIZES = {
    "A0": (1189, 841),
    "A1": (841, 594),
    "A2": (594, 420),
    "A3": (420, 297),
    "A4": (297, 210),
}

_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_WINDOWS_INVALID_FILENAME_CHARS = set('<>:"/\\|?*')


class CadDocument(DxfDwtMixin):
    """封装一个 DXF 绘图文档的常用操作。"""

    def __init__(self, drawings_dir: Path):
        self.drawings_dir = Path(drawings_dir)
        self.drawings_dir.mkdir(parents=True, exist_ok=True)
        self.doc: Any = None          # ezdxf Document
        self.msp: Any = None          # Modelspace
        self.current_path: Optional[Path] = None
        self.active_layer: str = LAYER_WALL  # 当前活动图层
        self.active_textstyle: str = "GB"
        self.active_dimstyle: str = "Standard"

    # ---------- 路径安全 ----------

    def _safe_path(self, filename: str) -> Path:
        """验证单个 DXF 文件名，并将结果限定在图纸目录内。"""
        if not isinstance(filename, str):
            raise ValueError("文件名必须是字符串")
        name = filename
        if (not name or name in {".", ".."} or Path(name).name != name
                or name[-1] in {" ", "."}
                or any(char in _WINDOWS_INVALID_FILENAME_CHARS or ord(char) < 32
                       for char in name)
                or len(name.encode("utf-16-le")) // 2 > 255):
            raise ValueError(f"文件名非法: {filename}")
        device_name = name.split(".", 1)[0].rstrip(" .").upper()
        if device_name in _WINDOWS_RESERVED_NAMES:
            raise ValueError(f"文件名使用了 Windows 保留名称: {filename}")
        candidate = self.drawings_dir / name
        if not candidate.suffix:
            candidate = candidate.with_suffix(".dxf")
        elif candidate.suffix.lower() != ".dxf":
            raise ValueError(f"DXF 模式只接受 .dxf 文件名: {filename}")
        if len(candidate.name.encode("utf-16-le")) // 2 > 255:
            raise ValueError(f"文件名过长: {filename}")
        root = self.drawings_dir.resolve()
        path = candidate.resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"文件名非法: {filename}")
        return path

    @staticmethod
    def _handle_str(handle_hex: str) -> str:
        """统一句柄显示：十六进制 → 十进制字符串。"""
        return str(int(handle_hex, 16))

    # ---------- 文档管理 ----------

    def new_drawing(self, filename: str,
                    template_path: Optional[str] = None) -> Dict[str, Any]:
        """新建空白 DXF；DWT 模板只属于 acad 模式。"""
        if template_path:
            raise ValueError("template_path 仅适用于 acad 模式；dxf 模式不能加载 .dwt 模板")
        path = self._safe_path(filename)
        if path.suffix.lower() != ".dxf":
            path = path.with_suffix(".dxf")
        if path.exists():
            raise ValueError(f"文件已存在: {path.name}（如需修改请用打开图纸）")
        doc = ezdxf.new("R2010", setup=True)
        doc.encoding = "utf-8"               # 中文文字不乱码
        doc.header["$INSUNITS"] = UNIT_MM    # 毫米
        # 中文字体样式：gbenor.shx 正体 + gbcbig.shx 大字体，AutoCAD 打开时中文不乱码
        if "GB" not in doc.styles:
            doc.styles.new("GB", dxfattribs={"font": "gbenor.shx", "bigfont": "gbcbig.shx"})
        doc.header["$TEXTSTYLE"] = "GB"
        self.doc = doc
        self.msp = doc.modelspace()
        self.current_path = path
        self.active_layer = LAYER_WALL
        self.active_textstyle = "GB"
        self.active_dimstyle = "Standard"
        for name, color in LAYER_COLORS.items():
            if name not in doc.layers:
                doc.layers.add(name, color=color)
        message = self.save()
        return {
            "message": message,
            "path": str(self.current_path),
            "mode": "dxf",
            "template_path": None,
            "template_source": "ezdxf_r2010",
        }

    def open_drawing(self, filename: str) -> str:
        """打开已有 DXF 图纸。"""
        path = resolve_open_drawing_path(
            filename,
            allowed_suffixes=(".dxf",),
            resolve_filename=self._safe_path,
        )
        self.doc = ezdxf.readfile(path)
        self.msp = self.doc.modelspace()
        self.current_path = path
        self.active_layer = self.doc.header.get("$CLAYER", LAYER_WALL)
        self.active_textstyle = self.doc.header.get("$TEXTSTYLE", "GB")
        self.active_dimstyle = self.doc.header.get("$DIMSTYLE", "Standard")
        return f"已打开 {path.name}，共 {len(self.msp)} 个图元"

    def save(self) -> str:
        """保存当前图纸。"""
        self._check_doc()
        self.doc.saveas(self.current_path)
        return f"已保存: {self.current_path}"

    def close_drawing(self, save: bool = False) -> str:
        """显式保存或放弃当前内存文档，然后释放 DXF 后端状态。"""
        if self.doc is None:
            return "当前没有打开的 DXF 图纸"
        path = self.current_path
        if save:
            self.save()
        self.doc = None
        self.msp = None
        self.current_path = None
        self.active_layer = LAYER_WALL
        self.active_textstyle = "GB"
        self.active_dimstyle = "Standard"
        action = "已保存并关闭" if save else "已放弃内存修改并关闭"
        return f"{action}: {path}"

    def save_as(self, filename: str, overwrite: bool = False) -> Dict[str, Any]:
        """把当前 DXF 另存到受控图纸目录，不允许静默覆盖。"""
        self._check_doc()
        path = self._safe_path(filename)
        if path.suffix.lower() != ".dxf":
            path = path.with_suffix(".dxf")
        if path.exists() and not overwrite:
            raise FileExistsError(f"目标文件已存在，未覆盖: {path}")
        self.doc.saveas(path)
        self.current_path = path
        return {"message": f"已另存为: {path}", "path": str(path), "mode": "dxf"}

    def clone_template_drawing(self, source_path: str, output_filename: str,
                               expected_sha256: Optional[str] = None,
                               overwrite: bool = False) -> Dict[str, Any]:
        raise NotImplementedError(
            "DWG 模板克隆仅 acad 模式支持；DXF 模式不能用重画图框替代")

    def get_info(self) -> str:
        """当前图纸信息：文件名、单位、图层、图元统计。"""
        if self.doc is None:
            return "当前没有打开的图纸"
        layers = [lay.dxf.name for lay in self.doc.layers]
        counts: Dict[str, int] = {}
        for e in self.msp:
            t = e.dxftype()
            counts[t] = counts.get(t, 0) + 1
        fname = self.current_path.name if self.current_path else "未保存"
        return (
            f"文件: {fname}\n"
            f"单位: 毫米\n"
            f"图层({len(layers)}): {', '.join(layers)}\n"
            f"图元统计: {', '.join(f'{k}×{v}' for k, v in counts.items())}"
        )

    def get_drawing_settings(self) -> Dict[str, Any]:
        """读取 DXF 头变量；缺失字段明确列入 unverified_fields。"""

        self._check_doc()
        raw: Dict[str, Any] = {}
        for variable in (
            "INSUNITS", "MEASUREMENT", "LUNITS", "LUPREC", "AUNITS",
            "AUPREC", "DIMASSOC", "MIRRTEXT", "LWDISPLAY", "PSTYLEMODE",
        ):
            try:
                raw[variable] = self.doc.header[f"${variable}"]
            except Exception:
                pass
        try:
            current_layer = str(self.doc.header["$CLAYER"])
        except Exception:
            current_layer = None
        try:
            current_layout = str(self.doc.header["$CTAB"])
        except Exception:
            current_layout = None
        return build_drawing_settings_payload(
            "dxf", raw, current_layer, current_layout
        )

    # ---------- 图层 ----------

    # 常用线型 pattern（ezdxf 格式：正数=落笔长度，负数=抬笔，0=点，单位 mm）
    LINETYPE_PATTERNS = {
        "DASHED": [12.7, -6.35],
        "HIDDEN": [6.35, -3.175],
        "CENTER": [31.75, -6.35, 6.35, -6.35],
        "PHANTOM": [38.1, -6.35, 6.35, -6.35, 6.35, -6.35],
        "DOT": [0.0, -6.35],
        "DASHDOT": [12.7, -6.35, 0.0, -6.35],
    }

    def _ensure_linetype(self, name: str) -> None:
        """确保线型存在（非连续线型自动添加到文档线型表）。"""
        if name in self.doc.linetypes:
            return
        pattern = self.LINETYPE_PATTERNS.get(name)
        if pattern is None:
            raise ValueError(f"未知线型: {name}（可用: CONTINUOUS/DASHED/HIDDEN/CENTER/PHANTOM/DOT/DASHDOT）")
        self.doc.linetypes.add(name, pattern=pattern, description=name)

    def create_layer(self, name: str, color: int = 7, linetype: str = "CONTINUOUS",
                     lineweight: int = 25, plot: bool = True,
                     description: str = "", locked: Optional[bool] = None,
                     transparency: Optional[int] = None) -> str:
        """创建图层（颜色、线型、线宽）；图层已存在则更新属性。

        linetype: CONTINUOUS 实线 / DASHED 虚线 / HIDDEN 隐藏线 / CENTER 点划线 / DOT 点线 等。
        lineweight 单位 1/100mm，如 25=0.25mm、50=0.5mm。
        """
        self._check_doc()
        if name not in self.doc.layers:
            lay = self.doc.layers.add(name)
            created = True
        else:
            lay = self.doc.layers.get(name)
            created = False
        lay.dxf.color = color
        lt = linetype.upper()
        if lt != "CONTINUOUS":
            self._ensure_linetype(lt)
        lay.dxf.linetype = lt
        lay.dxf.lineweight = int(lineweight)
        # 图层是否参与出图是制图标准的一部分，不能在标准配置中静默丢失。
        lay.dxf.plot = 1 if plot else 0
        lay.description = description
        if locked is not None:
            lay.lock() if locked else lay.unlock()
        if transparency is not None:
            lay.transparency = float(transparency) / 100.0
        verb = "已创建" if created else "已更新"
        return (f"{verb}图层 {name}（颜色 {color}，线型 {lt}，线宽 {lineweight}/100mm，"
                f"出图={'是' if plot else '否'}）")

    def set_layer_props(self, name: str, color: Optional[int] = None,
                        linetype: Optional[str] = None,
                        lineweight: Optional[int] = None,
                        plot: Optional[bool] = None,
                        description: Optional[str] = None,
                        locked: Optional[bool] = None,
                        frozen: Optional[bool] = None,
                        transparency: Optional[int] = None) -> str:
        """修改已有图层的颜色/线型/线宽。未指定的属性保持不变。"""
        self._check_doc()
        if name not in self.doc.layers:
            raise ValueError(f"图层 {name} 不存在，请先用 create_layer 创建")
        lay = self.doc.layers.get(name)
        changes = []
        if color is not None:
            lay.dxf.color = color
            changes.append(f"颜色={color}")
        if linetype:
            lt = linetype.upper()
            if lt != "CONTINUOUS":
                self._ensure_linetype(lt)
            lay.dxf.linetype = lt
            changes.append(f"线型={lt}")
        if lineweight is not None:
            lay.dxf.lineweight = int(lineweight)
            changes.append(f"线宽={lineweight}/100mm")
        if plot is not None:
            lay.dxf.plot = 1 if plot else 0
            changes.append(f"出图={plot}")
        if description is not None:
            lay.description = description
            changes.append("说明已更新")
        if locked is not None:
            lay.lock() if locked else lay.unlock()
            changes.append(f"锁定={locked}")
        if frozen is not None:
            lay.freeze() if frozen else lay.thaw()
            changes.append(f"冻结={frozen}")
        if transparency is not None:
            lay.transparency = float(transparency) / 100.0
            changes.append(f"透明度={transparency}%")
        return f"已更新图层 {name}：" + "，".join(changes)

    def set_active_layer(self, name: str) -> str:
        """切换当前活动图层（不存在则自动创建）。之后未指定图层的图元放入该层。"""
        self._check_doc()
        if name not in self.doc.layers:
            self.doc.layers.add(name, color=7)
        self.active_layer = name
        self.doc.header["$CLAYER"] = name
        return f"当前图层已切换为 {name}"

    def list_layers_detailed(self) -> Dict[str, Any]:
        """列出图层及所有数据库实体对图层的实际引用数量。"""

        self._check_doc()
        counts: Dict[str, int] = {}
        for entity in self.doc.entitydb.values():
            if not getattr(entity, "is_alive", False):
                continue
            try:
                if not entity.dxf.hasattr("layer"):
                    continue
                key = str(entity.dxf.layer).casefold()
                counts[key] = counts.get(key, 0) + 1
            except Exception:
                continue

        try:
            current = str(self.doc.header["$CLAYER"])
        except Exception:
            current = None
        unverified: List[str] = []
        layers: List[Dict[str, Any]] = []
        for layer in self.doc.layers:
            name = str(layer.dxf.name)

            def read(field: str, fn):
                try:
                    return fn()
                except Exception:
                    unverified.append(f"layers.{name}.{field}")
                    return None

            layers.append({
                "name": name,
                "color": read("color", lambda: abs(int(layer.dxf.color))),
                "linetype": read("linetype", lambda: str(layer.dxf.linetype)),
                "lineweight": read(
                    "lineweight", lambda: int(layer.dxf.get("lineweight", -3))
                ),
                "plot": read("plot", lambda: bool(int(layer.dxf.get("plot", 1)))),
                "description": read("description", lambda: str(layer.description)),
                "locked": read("locked", lambda: bool(layer.is_locked())),
                "frozen": read("frozen", lambda: bool(layer.is_frozen())),
                "transparency": read(
                    "transparency", lambda: int(round(float(layer.transparency) * 100))
                ),
                "is_current": (
                    name.casefold() == current.casefold() if current is not None else None
                ),
                "entity_count": counts.get(name.casefold(), 0),
            })
        return {
            "mode": "dxf",
            "layers": sorted(layers, key=lambda item: item["name"].casefold()),
            "unverified_fields": sorted(set(unverified)),
        }

    def list_textstyles(self) -> Dict[str, Any]:
        """列出 DXF 文字样式；ezdxf 不暴露注释性时不作猜测。"""

        self._check_doc()
        try:
            current = str(self.doc.header["$TEXTSTYLE"])
        except Exception:
            current = None
        unverified: List[str] = []
        styles: List[Dict[str, Any]] = []
        for style in self.doc.styles:
            name = str(style.dxf.name)
            unverified.append(f"text_styles.{name}.annotative")
            styles.append({
                "name": name,
                "font_file": str(style.dxf.get("font", "")),
                "big_font_file": str(style.dxf.get("bigfont", "")),
                "model_height": float(style.dxf.get("height", 0.0)),
                "width_factor": float(style.dxf.get("width", 1.0)),
                "oblique_angle": round(math.degrees(
                    float(style.dxf.get("oblique", 0.0))
                ), 12),
                "annotative": None,
                "is_current": (
                    name.casefold() == current.casefold() if current is not None else None
                ),
            })
        return {
            "mode": "dxf",
            "textstyles": sorted(styles, key=lambda item: item["name"].casefold()),
            "unverified_fields": sorted(set(unverified)),
        }

    # ---------- 制图环境 ----------

    def create_or_update_textstyle(self, name: str, model_height: float,
                                   font_file: str = "gbenor.shx",
                                   big_font_file: str = "gbcbig.shx",
                                   width_factor: float = 1.0,
                                   set_current: bool = True) -> Dict[str, Any]:
        """创建或更新文字样式；高度 0 表示可变字高且保留 $TEXTSIZE。"""
        self._check_doc()
        height = float(model_height)
        if not math.isfinite(height) or height < 0:
            raise ValueError("model_height 必须是大于等于 0 的有限数值")
        if name in self.doc.styles:
            style = self.doc.styles.get(name)
            created = False
        else:
            style = self.doc.styles.new(name)
            created = True
        style.dxf.font = font_file
        style.dxf.bigfont = big_font_file
        style.dxf.height = height
        style.dxf.width = float(width_factor)
        textsize_action = "not_current"
        if set_current:
            self.active_textstyle = name
            self.doc.header["$TEXTSTYLE"] = name
            if height == 0:
                textsize_action = "preserved"
            else:
                current_textsize = float(self.doc.header.get("$TEXTSIZE", 2.5))
                if math.isclose(
                    current_textsize, height, rel_tol=1e-9, abs_tol=1e-9
                ):
                    textsize_action = "unchanged"
                else:
                    self.doc.header["$TEXTSIZE"] = height
                    textsize_action = "updated"
        return {
            "name": name,
            "created": created,
            "model_height": float(style.dxf.height),
            "font_file": font_file,
            "big_font_file": big_font_file,
            "width_factor": float(width_factor),
            "current": bool(set_current),
            "textsize_action": textsize_action,
        }

    def create_or_update_dimstyle(self, name: str, overall_scale: float,
                                  text_height: float, arrow_size: float,
                                  text_gap: float, precision: int = 2,
                                  text_above: bool = True,
                                  text_style: Optional[str] = None,
                                  set_current: bool = True,
                                  oblique_stroke_size: float = 0.0,
                                  linear_scale_factor: float = 1.0,
                                  suppress_leading_zeros: bool = False,
                                  suppress_trailing_zeros: bool = False,
                                  decimal_separator: str = ".",
                                  extension_line_offset: float = 0.625,
                                  extension_line_extension: float = 1.25,
                                  baseline_spacing: float = 3.75,
                                  dimension_line_color: int = 256,
                                  extension_line_color: int = 256,
                                  text_color: int = 256,
                                  dimension_line_lineweight: int = -1,
                                  extension_line_lineweight: int = -1) -> Dict[str, Any]:
        """创建或更新非注释性尺寸样式；分别设置外观比例与测量值换算。"""
        self._check_doc()
        if name in self.doc.dimstyles:
            style = self.doc.dimstyles.get(name)
            created = False
        else:
            style = self.doc.dimstyles.new(name)
            created = True
        style.dxf.dimscale = float(overall_scale)
        style.dxf.dimtxt = float(text_height)
        style.dxf.dimasz = float(arrow_size)
        style.dxf.dimtsz = float(oblique_stroke_size)
        style.dxf.dimlfac = float(linear_scale_factor)
        style.dxf.dimgap = float(text_gap)
        style.dxf.dimdec = int(precision)
        style.dxf.dimtad = 1 if text_above else 0
        dimzin = int(style.dxf.get("dimzin", 0) or 0) & ~12
        style.dxf.dimzin = dimzin | (4 if suppress_leading_zeros else 0) | (8 if suppress_trailing_zeros else 0)
        style.dxf.dimdsep = ord(decimal_separator)
        style.dxf.dimexo = float(extension_line_offset)
        style.dxf.dimexe = float(extension_line_extension)
        style.dxf.dimdli = float(baseline_spacing)
        style.dxf.dimclrd = int(dimension_line_color)
        style.dxf.dimclre = int(extension_line_color)
        style.dxf.dimclrt = int(text_color)
        style.dxf.dimlwd = int(dimension_line_lineweight)
        style.dxf.dimlwe = int(extension_line_lineweight)
        if text_style:
            if text_style not in self.doc.styles:
                raise ValueError(f"文字样式 {text_style} 不存在，请先创建文字样式")
            # ezdxf 的公开属性使用样式名称，导出时再自动解析为 DXF 句柄。
            style.dxf.dimtxsty = text_style
        if set_current:
            self.active_dimstyle = name
            self.doc.header["$DIMSTYLE"] = name
        return {
            "name": name,
            "created": created,
            "overall_scale": float(overall_scale),
            "text_height": float(text_height),
            "arrow_size": float(arrow_size),
            "oblique_stroke_size": float(oblique_stroke_size),
            "uses_oblique_stroke": bool(oblique_stroke_size > 0),
            "linear_scale_factor": float(linear_scale_factor),
            "text_gap": float(text_gap),
            "precision": int(precision),
            "text_above": bool(text_above),
            "text_style": text_style,
            "suppress_leading_zeros": bool(suppress_leading_zeros),
            "suppress_trailing_zeros": bool(suppress_trailing_zeros),
            "decimal_separator": decimal_separator,
            "extension_line_offset": float(extension_line_offset),
            "extension_line_extension": float(extension_line_extension),
            "baseline_spacing": float(baseline_spacing),
            "dimension_line_color": int(dimension_line_color),
            "extension_line_color": int(extension_line_color),
            "text_color": int(text_color),
            "dimension_line_lineweight": int(dimension_line_lineweight),
            "extension_line_lineweight": int(extension_line_lineweight),
            "format_example_12_5": (
                f"{12.5:.{int(precision)}f}" if not suppress_trailing_zeros
                else f"{12.5:.{int(precision)}f}".rstrip("0").rstrip(".")
            ),
            "current": bool(set_current),
        }

    def set_linetype_settings(self, ltscale: float, celtscale: float = 1.0,
                              msltscale: int = 0, psltscale: int = 1,
                              regen: bool = True) -> Dict[str, Any]:
        """按调用方给定值设置线型环境，不根据图幅做自动推断。"""
        self._check_doc()
        self.doc.header["$LTSCALE"] = float(ltscale)
        self.doc.header["$CELTSCALE"] = float(celtscale)
        self.doc.header["$PSLTSCALE"] = int(psltscale)
        return {
            "ltscale": float(ltscale),
            "celtscale": float(celtscale),
            "msltscale": None,
            "psltscale": int(psltscale),
            "regenerated": False,
            "unsupported": {
                "msltscale": (
                    "ezdxf 1.4.4 不支持写入 $MSLTSCALE；"
                    f"请求值 {int(msltscale)} 未写入，需在 acad 模式设置"
                )
            },
            "note": "DXF 文件模式无需重生成；AutoCAD 打开后会按已写入的头变量显示",
        }

    def get_drafting_environment(self) -> Dict[str, Any]:
        """读取当前文字、尺寸与线型环境。"""
        self._check_doc()
        text_style = self.active_textstyle
        text_height = self._current_text_height()
        dim_name = self.active_dimstyle
        dim = self.doc.dimstyles.get(dim_name) if dim_name in self.doc.dimstyles else None
        oblique_stroke_size = float(dim.dxf.dimtsz) if dim is not None else None
        linear_scale_factor = float(dim.dxf.dimlfac) if dim is not None else None
        return {
            "mode": "dxf",
            "textstyle": {"name": text_style, "model_height": text_height},
            "dimstyle": {
                "name": dim_name,
                "overall_scale": float(dim.dxf.dimscale) if dim is not None else None,
                "text_height": float(dim.dxf.dimtxt) if dim is not None else None,
                "arrow_size": float(dim.dxf.dimasz) if dim is not None else None,
                "oblique_stroke_size": oblique_stroke_size,
                "uses_oblique_stroke": (
                    bool(oblique_stroke_size > 0)
                    if oblique_stroke_size is not None else None
                ),
                "linear_scale_factor": linear_scale_factor,
                "text_gap": float(dim.dxf.dimgap) if dim is not None else None,
                "precision": int(dim.dxf.dimdec) if dim is not None else None,
            },
            "linetype": {
                "ltscale": float(self.doc.header.get("$LTSCALE", 1.0)),
                "celtscale": float(self.doc.header.get("$CELTSCALE", 1.0)),
                "msltscale": None,
                "psltscale": int(self.doc.header.get("$PSLTSCALE", 1)),
                "unsupported": {"msltscale": "DXF 后端无法读取或写入 $MSLTSCALE"},
            },
        }

    # ---------- 绘图原语 ----------

    def draw_line(self, x1: float, y1: float, x2: float, y2: float,
                  layer: Optional[str] = None) -> Dict[str, Any]:
        self._check_doc()
        e = self.msp.add_line((x1, y1), (x2, y2),
                              dxfattribs={"layer": self._layer(layer)})
        return {"type": "LINE", "handle": self._handle_str(e.dxf.handle),
                "layer": e.dxf.layer}

    def draw_rectangle(self, x1: float, y1: float, x2: float, y2: float,
                       layer: Optional[str] = None) -> Dict[str, Any]:
        """画矩形（对角点）。"""
        self._check_doc()
        e = self.msp.add_lwpolyline(
            [(x1, y1), (x2, y1), (x2, y2), (x1, y2)], close=True,
            dxfattribs={"layer": self._layer(layer)},
        )
        return {"type": "LWPOLYLINE", "handle": self._handle_str(e.dxf.handle),
                "layer": e.dxf.layer}

    def draw_circle(self, cx: float, cy: float, radius: float,
                    layer: Optional[str] = None) -> Dict[str, Any]:
        self._check_doc()
        e = self.msp.add_circle((cx, cy), radius,
                                dxfattribs={"layer": self._layer(layer)})
        return {"type": "CIRCLE", "handle": self._handle_str(e.dxf.handle),
                "layer": e.dxf.layer, "radius": radius}

    def draw_arc(self, cx: float, cy: float, radius: float,
                 start_angle: float, end_angle: float,
                 layer: Optional[str] = None) -> Dict[str, Any]:
        """画圆弧（圆心 + 半径 + 起止角度，度）。"""
        self._check_doc()
        e = self.msp.add_arc((cx, cy), radius, start_angle=start_angle,
                             end_angle=end_angle,
                             dxfattribs={"layer": self._layer(layer)})
        return {"type": "ARC", "handle": self._handle_str(e.dxf.handle),
                "layer": e.dxf.layer, "radius": radius,
                "start_angle": start_angle, "end_angle": end_angle}

    def draw_polyline(self, points: Sequence[Point], close: bool = False,
                      layer: Optional[str] = None) -> Dict[str, Any]:
        """画多段线（点列表，可闭合）。"""
        self._check_doc()
        e = self.msp.add_lwpolyline(list(points), close=close,
                                    dxfattribs={"layer": self._layer(layer)})
        return {"type": "LWPOLYLINE", "handle": self._handle_str(e.dxf.handle),
                "layer": e.dxf.layer, "close": close, "points_count": len(points)}

    def draw_mline(self, points: Sequence[Point],
                   style_name: Optional[str] = None,
                   scale: Optional[float] = None,
                   justification: Optional[str] = None,
                   layer: Optional[str] = None) -> Dict[str, Any]:
        """绘制开放 MLINE；样式、比例、对正方式均可显式指定。"""
        self._check_doc()
        vertices = [(float(x), float(y)) for x, y in points]
        if len(vertices) < 2:
            raise ValueError("MLINE 至少需要两个顶点")
        if all(math.hypot(x - vertices[0][0], y - vertices[0][1]) <= 1e-9
               for x, y in vertices[1:]):
            raise ValueError("MLINE 不能由重合点构成")

        actual_style = style_name or str(self.doc.header.get("$CMLSTYLE", "Standard"))
        if actual_style not in self.doc.mline_styles:
            raise ValueError(f"多线样式不存在: {actual_style}；请先在模板或 CAD 中建立该样式")
        actual_scale = float(scale if scale is not None
                             else self.doc.header.get("$CMLSCALE", 20.0))
        if actual_scale <= 0:
            raise ValueError("MLINE 比例必须大于 0")
        just_map = {"top": 0, "zero": 1, "bottom": 2}
        if justification is None:
            actual_just = int(self.doc.header.get("$CMLJUST", 1))
        else:
            try:
                actual_just = just_map[justification.lower()]
            except KeyError as exc:
                raise ValueError("MLINE 对正方式仅支持 top / zero / bottom") from exc

        e = self.msp.add_mline(
            vertices,
            close=False,
            dxfattribs={
                "layer": self._layer(layer),
                "style_name": actual_style,
                "scale_factor": actual_scale,
                "justification": actual_just,
            },
        )
        return {
            "type": "MLINE",
            "handle": self._handle_str(e.dxf.handle),
            "layer": e.dxf.layer,
            "style_name": str(e.dxf.style_name),
            "scale": float(e.dxf.scale_factor),
            "justification": int(e.dxf.justification),
            "points_count": len(vertices),
        }

    def _polyline_part(self, entity) -> Tuple[List[Tuple[float, float, float]], bool]:
        """把可连接图元转换为统一的 (x, y, bulge) 顶点序列。"""
        kind = entity.dxftype()
        if kind == "LINE":
            start = entity.dxf.start
            end = entity.dxf.end
            return [(float(start.x), float(start.y), 0.0),
                    (float(end.x), float(end.y), 0.0)], False
        if kind == "ARC":
            start = entity.start_point
            end = entity.end_point
            span = (float(entity.dxf.end_angle) - float(entity.dxf.start_angle)) % 360.0
            if span <= 1e-9:
                raise ValueError("零圆心角 ARC 不能合并")
            bulge = math.tan(math.radians(span) / 4.0)
            return [(float(start.x), float(start.y), bulge),
                    (float(end.x), float(end.y), 0.0)], False
        if kind == "LWPOLYLINE":
            vertices = [(float(x), float(y), float(b))
                        for x, y, b in entity.get_points("xyb")]
            return vertices, bool(entity.closed)
        if kind == "POLYLINE":
            if not entity.is_2d_polyline:
                raise ValueError("仅支持二维 POLYLINE；3D/网格多段线不能合并")
            vertices = [(float(v.dxf.location.x), float(v.dxf.location.y),
                         float(v.dxf.get("bulge", 0.0)))
                        for v in entity.vertices]
            return vertices, bool(entity.is_closed)
        raise ValueError(f"图元类型 {kind} 不支持合并；仅支持 LINE / ARC / LWPOLYLINE / 2D POLYLINE")

    def join_polyline(self, handles: List[str], close: bool = False,
                      tolerance: float = 1e-6,
                      layer: Optional[str] = None) -> Dict[str, Any]:
        """把端点相接的线、圆弧和二维多段线合并为一条 LWPOLYLINE。"""
        self._check_doc()
        entities = []
        parts = []
        source_layer = None
        for handle in handles:
            entity = self._get_entity(handle)
            if entity is None:
                raise ValueError(f"未找到句柄为 {handle} 的图元")
            part, closed = self._polyline_part(entity)
            if closed:
                if len(handles) != 1 or not close:
                    raise ValueError("闭合多段线只能单独输入，并且 close 必须为 true")
                part = list(part) + [(part[0][0], part[0][1], 0.0)]
            entities.append(entity)
            parts.append(part)
            source_layer = source_layer or str(entity.dxf.layer)

        vertices, is_closed = join_polyline_parts(parts, close=close, tolerance=tolerance)
        target_layer = self._layer(layer) if layer else (source_layer or self.active_layer)
        result = self.msp.add_lwpolyline(
            vertices,
            format="xyb",
            close=is_closed,
            dxfattribs={"layer": target_layer},
        )
        try:
            for entity in entities:
                entity.destroy()
        except Exception:
            result.destroy()
            raise
        return {
            "type": "LWPOLYLINE",
            "handle": self._handle_str(result.dxf.handle),
            "layer": str(result.dxf.layer),
            "closed": bool(result.closed),
            "sources_count": len(entities),
            "points_count": len(vertices),
        }

    def _entity_summary(self, entity) -> Dict[str, Any]:
        row = {
            "type": entity.dxftype(),
            "handle": self._handle_str(entity.dxf.handle),
            "layer": str(entity.dxf.layer),
        }
        if entity.dxftype() == "LINE":
            row["start"] = [float(entity.dxf.start.x), float(entity.dxf.start.y)]
            row["end"] = [float(entity.dxf.end.x), float(entity.dxf.end.y)]
        return row

    def explode(self, handle: str) -> Dict[str, Any]:
        """分解支持 explode() 的复合图元，并返回生成图元的句柄。"""
        self._check_doc()
        entity = self._get_entity(handle)
        if entity is None:
            raise ValueError(f"未找到句柄为 {handle} 的图元")
        source_type = entity.dxftype()
        explode_method = getattr(entity, "explode", None)
        if not callable(explode_method):
            raise ValueError(f"图元类型 {source_type} 不支持分解")
        created = list(explode_method())
        return {
            "source_type": source_type,
            "source_handle": handle,
            "deleted_source": not entity.is_alive,
            "entities": [self._entity_summary(item) for item in created],
        }

    def draw_text(self, text: str, x: float, y: float, height: Optional[float] = None,
                  rotation: float = 0, layer: Optional[str] = None) -> Dict[str, Any]:
        """写文字标注；height 留空时继承当前文字样式的模型字高。"""
        self._check_doc()
        if height is None:
            height = self._current_text_height()
        e = self.msp.add_text(text, dxfattribs={"height": height,
                                                "style": self.active_textstyle,
                                                "layer": self._layer(layer)})
        e.dxf.insert = (x, y)
        e.dxf.rotation = rotation
        return {"type": "TEXT", "handle": self._handle_str(e.dxf.handle),
                "layer": e.dxf.layer, "text": text, "height": height,
                "rotation": rotation}

    def draw_mtext(self, text: str, x: float, y: float, width: float,
                   height: Optional[float] = None, rotation: float = 0,
                   layer: Optional[str] = None) -> Dict[str, Any]:
        """创建原生 MTEXT；不会退化为多个 TEXT。"""
        self._check_doc()
        if width <= 0:
            raise ValueError("MTEXT 宽度必须大于 0")
        if height is None:
            height = self._current_text_height()
        e = self.msp.add_mtext(
            text,
            dxfattribs={
                "char_height": float(height),
                "style": self.active_textstyle,
                "layer": self._layer(layer),
                "rotation": float(rotation),
                "width": float(width),
            },
        )
        e.dxf.insert = (float(x), float(y))
        return {"type": "MTEXT", "handle": self._handle_str(e.dxf.handle),
                "layer": e.dxf.layer, "text": text, "height": height,
                "width": width, "rotation": rotation}

    def trim(self, subject_handle: str, cutting_handles: List[str],
             pick_x: float, pick_y: float) -> Dict[str, Any]:
        """在离拾取点最近的切割交点处，把被修剪图元截断，保留拾取点一侧。

        被修剪图元支持 LINE / LWPOLYLINE；切割边界支持 LINE / LWPOLYLINE
        （折线按每条边参与切割）。先 cad_list_entities 拿句柄再调用。
        """
        self._check_doc()
        subj = self._get_entity(subject_handle)
        if subj is None:
            raise ValueError(f"未找到句柄为 {subject_handle} 的图元")
        kind = subj.dxftype()
        if kind not in ("LINE", "LWPOLYLINE"):
            raise ValueError(f"被修剪图元仅支持 LINE / LWPOLYLINE，当前为 {kind}")

        # 1. 把切割图元展开为线段集合
        cuts = []
        for ch in cutting_handles:
            e = self._get_entity(ch)
            if e is None:
                raise ValueError(f"未找到切割边界句柄 {ch}")
            if e.dxftype() == "LINE":
                cuts.append((tuple(e.dxf.start)[:2], tuple(e.dxf.end)[:2]))
            elif e.dxftype() == "LWPOLYLINE":
                vertices = list(e.get_points("xyb"))
                if any(abs(float(vertex[2])) > 1e-12 for vertex in vertices):
                    raise ValueError(
                        f"切割边界 {ch} 含圆弧段；当前修剪仅支持直线段，未修改图形")
                pts = [(float(vertex[0]), float(vertex[1])) for vertex in vertices]
                m = len(pts)
                step = m if e.closed else m - 1
                for k in range(step):
                    cuts.append((pts[k % m], pts[(k + 1) % m]))
            else:
                raise ValueError(f"切割边界仅支持 LINE / LWPOLYLINE，句柄 {ch} 为 {e.dxftype()}")

        pick = (pick_x, pick_y)

        if kind == "LINE":
            points = [tuple(subj.dxf.start)[:2], tuple(subj.dxf.end)[:2]]
            new_pts, _, cut_points = trim_polyline_detailed(points, False, cuts, pick)
            if new_pts is None:
                return {"type": "LINE", "handle": subject_handle,
                        "message": "未找到可修剪的切割交点"}
            subj.dxf.start = new_pts[0]
            subj.dxf.end = new_pts[1]
            return {"type": "LINE", "handle": subject_handle,
                    "cut": cut_points[0], "cut_points": cut_points,
                    "remaining_points": 2}
        else:
            vertices = list(subj.get_points("xyb"))
            if any(abs(float(vertex[2])) > 1e-12 for vertex in vertices):
                raise ValueError(
                    "被修剪多段线含圆弧段；当前修剪不能安全保留 bulge，未修改图形")
            points = [(float(vertex[0]), float(vertex[1])) for vertex in vertices]
            new_pts, new_closed, cut_points = trim_polyline_detailed(
                points, subj.closed, cuts, pick)
            if new_pts is None:
                return {"type": "LWPOLYLINE", "handle": subject_handle,
                        "message": "未找到可修剪的切割交点"}
            subj.set_points(new_pts, format="xy")
            subj.closed = new_closed
            return {"type": "LWPOLYLINE", "handle": subject_handle,
                    "cut": cut_points[0], "cut_points": cut_points,
                    "remaining_points": len(new_pts)}

    def draw_dimension(self, p1x: float, p1y: float, p2x: float, p2y: float,
                       offset: float = 500, direction: str = "horizontal",
                       layer: str = LAYER_DIM) -> Dict[str, Any]:
        """兼容性尺寸标注：根据 offset 推算尺寸线位置。

        direction:
          'horizontal' 水平（标注线在两点下方 offset），测量水平距离；
          'vertical'   垂直（标注线在两点右侧 offset），测量垂直距离；
          'aligned'    对齐（标注线平行于两点连线，向连线右侧法线方向偏 offset），测量直线距离。
        """
        self._check_doc()
        if direction == "aligned":
            dx, dy = p2x - p1x, p2y - p1y
            length = math.hypot(dx, dy)
            if length == 0:
                raise ValueError("被标注两点重合，无法对齐标注")
            angle = math.atan2(dy, dx)                       # 尺寸线角度 = 连线方向
            nx, ny = dy / length, -dx / length               # 连线右侧法线
            mid_x, mid_y = (p1x + p2x) / 2, (p1y + p2y) / 2
            override = self.msp.add_linear_dim(
                base=(mid_x + nx * offset, mid_y + ny * offset),
                p1=(p1x, p1y), p2=(p2x, p2y), angle=angle,
                dimstyle=self.active_dimstyle, dxfattribs={"layer": layer})
            value = length
        elif direction == "vertical":
            base_x = max(p1x, p2x) + offset
            mid_y = (p1y + p2y) / 2
            override = self.msp.add_linear_dim(base=(base_x, mid_y), p1=(p1x, p1y),
                                               p2=(p2x, p2y),
                                               angle=math.radians(90),
                                               dimstyle=self.active_dimstyle,
                                               dxfattribs={"layer": layer})
            value = abs(p2y - p1y)
        else:
            base_y = min(p1y, p2y) - offset
            override = self.msp.add_linear_dim(base=(p1x, base_y), p1=(p1x, p1y),
                                               p2=(p2x, p2y),
                                               dimstyle=self.active_dimstyle,
                                               dxfattribs={"layer": layer})
            value = abs(p2x - p1x)
        override.render()
        dim = override.dimension          # 真正的 DIMENSION 实体
        return {"type": "DIMENSION", "handle": self._handle_str(dim.dxf.handle),
                "layer": dim.dxf.layer, "value": round(value, 1),
                "direction": direction}

    def draw_dimension_at(self, p1x: float, p1y: float, p2x: float, p2y: float,
                          line_x: float, line_y: float,
                          direction: str = "horizontal",
                          layer: str = LAYER_DIM) -> Dict[str, Any]:
        """在明确给定的尺寸线位置画标注，不替调用方决定方向或放置侧。"""
        self._check_doc()
        if p1x == p2x and p1y == p2y:
            raise ValueError("被标注两点重合，无法标注")
        if direction == "aligned":
            angle = math.atan2(p2y - p1y, p2x - p1x)
            value = math.hypot(p2x - p1x, p2y - p1y)
        elif direction == "vertical":
            angle = math.radians(90)
            value = abs(p2y - p1y)
        else:
            angle = 0.0
            value = abs(p2x - p1x)
        override = self.msp.add_linear_dim(
            base=(line_x, line_y), p1=(p1x, p1y), p2=(p2x, p2y), angle=angle,
            dimstyle=self.active_dimstyle, dxfattribs={"layer": layer})
        override.render()
        dim = override.dimension
        return {
            "type": "DIMENSION",
            "handle": self._handle_str(dim.dxf.handle),
            "layer": dim.dxf.layer,
            "value": round(value, 1),
            "direction": direction,
            "dimension_line_point": (line_x, line_y),
            "dimstyle": self.active_dimstyle,
        }

    def draw_angular_dimension(self, vertex_x: float, vertex_y: float,
                               p1x: float, p1y: float, p2x: float, p2y: float,
                               loc_x: float, loc_y: float,
                               layer: str = LAYER_DIM) -> Dict[str, Any]:
        """角度标注：测量两条线（角顶点 vertex 分别到 p1、p2）的夹角。

        loc 为标注文字/弧所在位置（大致放在角内即可）。
        """
        self._check_doc()
        # line1/line2 各是一条"腿"（起点, 终点）：从顶点到 p1、从顶点到 p2。
        # base 是尺寸线（弧）所在位置点，必须离顶点有距离，故用 loc 而非顶点。
        override = self.msp.add_angular_dim_2l(
            base=(loc_x, loc_y),
            line1=((vertex_x, vertex_y), (p1x, p1y)),
            line2=((vertex_x, vertex_y), (p2x, p2y)),
            location=(loc_x, loc_y), dimstyle=self.active_dimstyle,
            dxfattribs={"layer": layer})
        override.render()
        dim = override.dimension
        return {"type": "DIMENSION", "handle": self._handle_str(dim.dxf.handle),
                "layer": dim.dxf.layer, "kind": "angular",
                "vertex": (vertex_x, vertex_y)}

    def draw_radius_dimension(self, cx: float, cy: float,
                              edge_x: float, edge_y: float,
                              layer: str = LAYER_DIM) -> Dict[str, Any]:
        """半径标注：测量圆心 (cx, cy) 到圆上一点 (edge_x, edge_y) 的距离。

        引线沿圆心→边上一点方向，自动标注半径值（如 R500）。
        """
        self._check_doc()
        radius = math.hypot(edge_x - cx, edge_y - cy)
        if radius == 0:
            raise ValueError("圆心与边上一点重合，无法半径标注")
        # 直接传圆上测量点 mpoint（第二个位置参数），ezdxf 据此生成径向尺寸线
        override = self.msp.add_radius_dim((cx, cy), (edge_x, edge_y),
                                           dimstyle=self.active_dimstyle,
                                           dxfattribs={"layer": layer})
        override.render()
        dim = override.dimension
        return {"type": "DIMENSION", "handle": self._handle_str(dim.dxf.handle),
                "layer": dim.dxf.layer, "kind": "radius", "radius": round(radius, 1)}

    def draw_diameter_dimension(self, cx: float, cy: float,
                                edge_x: float, edge_y: float,
                                layer: str = LAYER_DIM,
                                text_x: Optional[float] = None,
                                text_y: Optional[float] = None) -> Dict[str, Any]:
        """由圆心与圆上一点创建原生直径尺寸，可指定文字位置。"""
        _, diameter = validate_diameter_dimension(cx, cy, edge_x, edge_y, text_x, text_y)
        self._check_doc()
        override = self.msp.add_diameter_dim(
            (cx, cy), (edge_x, edge_y), dimstyle=self.active_dimstyle,
            dxfattribs={"layer": layer})
        if text_x is not None:
            override.user_location_override((text_x, text_y))
        try:
            override.render()
        except Exception:
            self.msp.delete_entity(override.dimension)
            raise
        dim = override.dimension
        return {"type": "DIMENSION", "handle": self._handle_str(dim.dxf.handle),
                "layer": dim.dxf.layer, "kind": "diameter", "diameter": diameter,
                "dimstyle": dim.dxf.dimstyle}

    def draw_hatch(
        self,
        *,
        boundary_handle: Optional[str] = None,
        boundary_handles: Optional[Sequence[str]] = None,
        seed_point: Optional[Point] = None,
        pattern: str = "ANSI31",
        scale: float = 1.0,
        angle: float = 0.0,
        layer: str = "FILL",
        associative: bool = False,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        """Fill one closed entity, or an exact closed region bounded by selected entities."""
        if (boundary_handle is None) == (boundary_handles is None):
            raise ValueError("boundary_handle 与 boundary_handles 必须且只能提供一个")
        if boundary_handles is not None:
            return fill_dxf_region(
                self, boundary_handles=boundary_handles, seed_point=seed_point,
                pattern=pattern, scale=scale, angle=angle, layer=layer,
                associative=associative, dry_run=dry_run,
            )
        if seed_point is not None:
            raise ValueError("seed_point 仅用于 boundary_handles 组合边界模式")
        if not isinstance(boundary_handle, str) or not boundary_handle.isdecimal():
            raise ValueError("填充范围必须通过已有闭合边界的十进制句柄指定")
        return self._fill_closed_boundary(
            boundary_handle=boundary_handle, points=None, layer=layer,
            associative=associative, keep_boundary=True, dry_run=dry_run,
            pattern=pattern, scale=scale, angle=angle,
        )

    def fill_closed_boundary_solid(
        self,
        *,
        boundary_handle: Optional[str],
        points: Optional[Sequence[Point]],
        layer: str,
        associative: bool,
        keep_boundary: bool,
        dry_run: bool,
    ) -> Dict[str, Any]:
        """Create a SOLID HATCH and verify its persisted DXF fields immediately."""

        return self._fill_closed_boundary(
            boundary_handle=boundary_handle, points=points, layer=layer,
            associative=associative, keep_boundary=keep_boundary, dry_run=dry_run,
        )

    def _fill_closed_boundary(
        self,
        *,
        boundary_handle: Optional[str],
        points: Optional[Sequence[Point]],
        layer: str,
        associative: bool,
        keep_boundary: bool,
        dry_run: bool,
        pattern: str = "SOLID",
        scale: float = 1.0,
        angle: float = 0.0,
    ) -> Dict[str, Any]:
        """Create a native hatch and verify its stored settings and boundary."""

        self._check_doc()
        pattern, scale, angle = validate_hatch_parameters(pattern, scale, angle)
        solid = pattern == "SOLID"
        if (boundary_handle is None) == (points is None):
            raise ValueError("boundary_handle 与 points 必须且只能提供一个")
        if not solid and pattern not in hatch_patterns.ISO_PATTERN:
            raise ValueError(f"未知或不可用的 DXF 填充图案: {pattern}")
        boundary = None
        path_vertices: List[Tuple[float, ...]]
        boundary_area: float
        boundary_kind: str
        if points is not None:
            normalized, boundary_area = validate_closed_polygon(points)
            path_vertices = [(x, y) for x, y in normalized]
            boundary_kind = "explicit_polygon"
        else:
            boundary = self._get_entity(str(boundary_handle))
            if boundary is None:
                raise ValueError(f"未找到边界句柄: {boundary_handle}")
            entity_type = boundary.dxftype()
            boundary_kind = entity_type
            if entity_type == "LWPOLYLINE":
                if not bool(boundary.closed):
                    raise ValueError("指定 LWPOLYLINE 边界未闭合")
                raw_vertices = list(boundary.get_points("xyb"))
                if any(abs(float(vertex[2])) > 1e-12 for vertex in raw_vertices):
                    raise ValueError("当前填充回读不支持含 bulge 圆弧段的 LWPOLYLINE")
                normalized, boundary_area = validate_closed_polygon(
                    [(float(vertex[0]), float(vertex[1])) for vertex in raw_vertices]
                )
                path_vertices = [(x, y) for x, y in normalized]
            elif entity_type == "POLYLINE":
                if not bool(boundary.is_closed):
                    raise ValueError("指定 POLYLINE 边界未闭合")
                if not boundary.is_2d_polyline or any(abs(float(vertex.dxf.bulge)) > 1e-12 for vertex in boundary.vertices):
                    raise ValueError("当前填充仅支持二维直线 POLYLINE 边界")
                normalized, boundary_area = validate_closed_polygon([
                    (float(vertex.dxf.location.x), float(vertex.dxf.location.y))
                    for vertex in boundary.vertices
                ])
                path_vertices = [(x, y) for x, y in normalized]
            elif entity_type == "CIRCLE":
                center = boundary.dxf.center
                radius = float(boundary.dxf.radius)
                if not math.isfinite(radius) or radius <= 0:
                    raise ValueError("圆形边界半径必须为大于 0 的有限数值")
                if not all(math.isfinite(float(value)) for value in center):
                    raise ValueError("圆形边界坐标必须是有限数值")
                boundary_area = math.pi * radius * radius
                path_vertices = []
            else:
                raise ValueError(
                    "DXF 填充仅支持显式点、闭合 LWPOLYLINE/POLYLINE 或 CIRCLE")
        if boundary_handle is not None and not keep_boundary:
            raise ValueError("使用已有边界时必须 keep_boundary=true")
        if associative and not keep_boundary and boundary is None:
            raise ValueError("显式点创建关联填充时必须 keep_boundary=true")
        if dry_run:
            return {
                "mode": "dxf",
                "dry_run": True,
                "would_write": False,
                "pattern": pattern,
                "scale": scale,
                "angle": angle,
                "pattern_availability": "available",
                "boundary_kind": boundary_kind,
                "boundary_area": boundary_area,
                "layer": layer,
                "associative": associative,
                "keep_boundary": keep_boundary,
            }

        created_boundary = None
        created_layer = False
        hatch = None
        try:
            if layer not in self.doc.layers:
                self.doc.layers.new(layer)
                created_layer = True
            if points is not None and (keep_boundary or associative):
                created_boundary = self.msp.add_lwpolyline(
                    path_vertices, close=True, dxfattribs={"layer": layer}
                )
                boundary = created_boundary
            hatch = self.msp.add_hatch(dxfattribs={"layer": layer})
            if solid:
                hatch.set_solid_fill()
            else:
                hatch.set_pattern_fill(pattern, scale=scale, angle=angle)
            if boundary is not None:
                hatch.dxf.extrusion = boundary.dxf.extrusion
                if boundary_kind == "LWPOLYLINE":
                    elevation = float(boundary.dxf.elevation)
                elif boundary_kind == "POLYLINE":
                    elevation = float(boundary.dxf.elevation.z)
                elif boundary_kind == "CIRCLE":
                    elevation = float(boundary.dxf.center.z)
                else:
                    elevation = 0.0
                hatch.dxf.elevation = (0.0, 0.0, elevation)
            if boundary_kind == "CIRCLE":
                edge_path = hatch.paths.add_edge_path()
                center = boundary.dxf.center
                edge_path.add_arc(
                    (float(center.x), float(center.y)),
                    float(boundary.dxf.radius),
                    start_angle=0.0,
                    end_angle=360.0,
                )
                hatch_path = edge_path
            else:
                hatch_path = hatch.paths.add_polyline_path(path_vertices, is_closed=True)
            if associative:
                if boundary is None:
                    raise RuntimeError("关联填充缺少可关联的边界实体")
                hatch.associate(hatch_path, [boundary])

            read_box = bbox.extents([hatch])
            readback = {
                "entity_type": hatch.dxftype(),
                "pattern_name": str(hatch.dxf.pattern_name),
                "solid_fill": bool(hatch.dxf.solid_fill),
                "associative": bool(hatch.dxf.associative),
                "layer": str(hatch.dxf.layer),
                "loop_count": len(hatch.paths),
                "bounding_box": (
                    {
                        "min": [float(read_box.extmin.x), float(read_box.extmin.y)],
                        "max": [float(read_box.extmax.x), float(read_box.extmax.y)],
                    }
                    if read_box.has_data else None
                ),
            }
            if not solid:
                readback["pattern_scale"] = float(hatch.dxf.pattern_scale)
                readback["pattern_angle"] = float(hatch.dxf.pattern_angle)
            verified = (
                readback["entity_type"] == "HATCH"
                and readback["pattern_name"].upper() == pattern
                and readback["solid_fill"] is solid
                and readback["associative"] is associative
                and readback["layer"] == layer
                and readback["loop_count"] == 1
                and readback["bounding_box"] is not None
            )
            if not solid:
                verified = (
                    verified
                    and math.isclose(readback["pattern_scale"], scale, rel_tol=1e-9, abs_tol=1e-12)
                    and math.isclose((readback["pattern_angle"] - angle + 180.0) % 360.0 - 180.0, 0.0, abs_tol=1e-7)
                )
            if not verified:
                raise RuntimeError(f"填充写入后读回不一致: {readback}")
            if created_boundary is not None and not keep_boundary:
                created_boundary.destroy()
                created_boundary = None
            return {
                "mode": "dxf",
                "dry_run": False,
                "would_write": True,
                "verified": True,
                "hatch_handle": self._handle_str(hatch.dxf.handle),
                "boundary_handle": (
                    self._handle_str(boundary.dxf.handle)
                    if boundary is not None and boundary.is_alive else None
                ),
                "boundary_kind": boundary_kind,
                "boundary_area": boundary_area,
                "readback": readback,
            }
        except Exception:
            if hatch is not None and hatch.is_alive:
                if boundary is not None and boundary.is_alive:
                    boundary.discard_reactor_handle(hatch.dxf.handle)
                self.msp.delete_entity(hatch)
            if created_boundary is not None and created_boundary.is_alive:
                self.msp.delete_entity(created_boundary)
            if created_layer:
                self.doc.layers.remove(layer)
            raise

    def draw_centerline(self, x1: float, y1: float, x2: float, y2: float,
                        layer: str = "CENTER") -> Dict[str, Any]:
        """给矩形范围画对称中心线（水平 + 垂直两条点划线）。

        (x1,y1)-(x2,y2) 为构件包络矩形的两个对角点。中心线两端各向外伸出 10%
        长度（按长边计），自动放入 CENTER 图层（红色、点划线），并同步线型比例
        LTSCALE 由调用方通过制图环境工具统一设置，本工具不会自动改比例。
        """
        self._check_doc()
        if layer not in self.doc.layers:
            lay = self.doc.layers.add(layer)
        else:
            lay = self.doc.layers.get(layer)
        lay.dxf.color = 1                     # 红
        self._ensure_linetype("CENTER")
        lay.dxf.linetype = "CENTER"
        xmin, xmax = min(x1, x2), max(x1, x2)
        ymin, ymax = min(y1, y2), max(y1, y2)
        cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
        extend = max(xmax - xmin, ymax - ymin) * 0.10
        if extend <= 0:
            extend = 100.0
        h = self.msp.add_line((xmin - extend, cy), (xmax + extend, cy),
                              dxfattribs={"layer": layer})
        v = self.msp.add_line((cx, ymin - extend), (cx, ymax + extend),
                              dxfattribs={"layer": layer})
        return {"type": "CENTERLINE", "layer": layer,
                "handles": [self._handle_str(h.dxf.handle),
                            self._handle_str(v.dxf.handle)],
                "center": (cx, cy),
                "extend": round(extend, 1)}

    # ---------- 图块与图框 ----------

    def make_block(self, name: str, handles: List[str], base_x: float = 0,
                   base_y: float = 0, annotative: bool = False) -> str:
        """把指定句柄的图元打包成一个图块。

        原图元从模型空间移除，视觉位置不变（基点即原坐标），之后可整体移动/旋转/插入副本。
        """
        if annotative:
            from cad_mcp.core.runtime import UnsupportedModeError
            raise UnsupportedModeError(
                "原生注释性块仅 acad 模式支持；DXF 模式没有可验证的注释性块创建路径")
        self._check_doc()
        if not name:
            raise ValueError("图块名不能为空")
        if name in self.doc.blocks:
            raise ValueError(f"图块 {name} 已存在")
        blk = self.doc.blocks.new(name=name, base_point=(base_x, base_y))
        moved = 0
        for h in handles:
            e = self._get_entity(h)
            if e is None:
                continue
            try:
                copy = e.copy()
                copy.dxf.layer = e.dxf.layer
                blk.add_entity(copy)     # 放入块定义（自动分配块内句柄）
                e.destroy()              # 移除原图元
                moved += 1
            except Exception:
                pass
        if moved == 0:
            del self.doc.blocks[name]    # 空块直接删掉
            raise ValueError("没有找到可打包的图元")
        return f"已创建图块 {name}（{moved} 个图元，基点 {base_x},{base_y}）"

    def insert_block(self, name: str, x: float, y: float, rotation: float = 0,
                     scale: float = 1.0, source_path: Optional[str] = None,
                     source_sha256: Optional[str] = None,
                     attributes: Optional[Dict[str, str]] = None,
                     layer: Optional[str] = None,
                     x_scale: Optional[float] = None,
                     y_scale: Optional[float] = None,
                     z_scale: Optional[float] = None,
                     field_bindings: Optional[List[Dict[str, Any]]] = None,
                     replace_existing: bool = False) -> Dict[str, Any]:
        """插入图块；DXF 不读取 DWG，也不伪造 AutoCAD 关联字段。"""
        self._check_doc()
        if source_path:
            from cad_mcp.core.runtime import UnsupportedModeError
            raise UnsupportedModeError("source_path DWG 块库导入仅 acad 模式支持")
        if field_bindings:
            from cad_mcp.core.runtime import UnsupportedModeError
            raise UnsupportedModeError("关联字段需要原生 AutoCAD DWG；DXF 模式未插入静态替代物")
        if name not in self.doc.blocks:
            raise ValueError(f"图块 {name} 不存在，请先用 cad_make_block 创建")
        if replace_existing:
            raise ValueError("未提供 source_path 时 replace_existing 没有意义")
        sx = float(x_scale if x_scale is not None else scale)
        sy = float(y_scale if y_scale is not None else scale)
        sz = float(z_scale if z_scale is not None else scale)
        target_layer = layer or self.active_layer
        if target_layer not in self.doc.layers:
            self.doc.layers.add(target_layer)
        e = self.msp.add_blockref(name, insert=(x, y),
                                  dxfattribs={"rotation": rotation,
                                              "xscale": sx, "yscale": sy,
                                              "zscale": sz, "layer": target_layer})
        supplied = dict(attributes or {})
        tags = {
            str(entity.dxf.tag).casefold(): str(entity.dxf.tag)
            for entity in self.doc.blocks.get(name)
            if entity.dxftype() == "ATTDEF" and not bool(int(entity.dxf.get("flags", 0)) & 2)
        }
        matched = {
            tags[tag.casefold()]: str(value)
            for tag, value in supplied.items() if tag.casefold() in tags
        }
        if matched:
            e.add_auto_attribs(matched)
        missing = [tag for tag in supplied if tag.casefold() not in tags]
        return {"type": "INSERT", "handle": self._handle_str(e.dxf.handle),
                "block": name, "insert": (x, y), "rotation": rotation,
                "scales": {"x": sx, "y": sy, "z": sz}, "layer": target_layer,
                "attributes_updated": sorted(matched), "missing_tags": missing,
                "field_bindings": [], "source_path": None}

    def draw_title_block(self, width: float, height: float, title: str = "",
                         project: str = "", design: str = "", check: str = "",
                         sheet_no: str = "", ratio: str = "1:100",
                         size: Optional[str] = None,
                         layer: str = "TITLE") -> str:
        """画工程图框 + 标题栏。

        外框粗线、内框细线（左边 25mm 装订边，其余 10mm），标题栏在右下角 180×56。
        title 图名、project 工程名、design 设计、check 审核、sheet_no 图号、ratio 比例。
        size 指定标准图纸幅面（A0-A4）时，width/height 自动用对应标准尺寸。
        """
        self._check_doc()
        if size:
            key = size.upper()
            if key not in PAPER_SIZES:
                raise ValueError(f"未知图框幅面 {size}（可用: A0/A1/A2/A3/A4）")
            width, height = PAPER_SIZES[key]
        if layer not in self.doc.layers:
            self.doc.layers.add(layer, color=7)
        self._add_lwpolyline([(0, 0), (width, 0), (width, height), (0, height)], layer, lw=50)
        m, b = 10.0, 25.0
        self._add_lwpolyline([(b, m), (width - m, m), (width - m, height - m), (b, height - m)], layer, lw=25)
        tb_w, tb_h = 180.0, 56.0
        x0, y0 = width - tb_w, 0.0
        self._add_lwpolyline([(x0, y0), (width, y0), (width, y0 + tb_h), (x0, y0 + tb_h)], layer, lw=50)
        col = tb_w - 40.0   # 右列 40mm
        self._add_line(x0 + col, y0, x0 + col, y0 + tb_h, layer)
        self._add_line(x0, y0 + tb_h * 0.55, width, y0 + tb_h * 0.55, layer)
        self._add_line(x0, y0 + tb_h * 0.27, width, y0 + tb_h * 0.27, layer)
        self._add_text(title, x0 + 5, y0 + tb_h - 16, 12, layer)
        self._add_text(f"工程:{project}" if project else "", x0 + 5, y0 + tb_h * 0.55 + 4, 7, layer)
        self._add_text(f"设计:{design}  审核:{check}", x0 + 5, y0 + tb_h * 0.27 + 4, 7, layer)
        self._add_text(f"图号 {sheet_no}", x0 + col + 4, y0 + tb_h - 16, 7, layer)
        self._add_text(f"比例 {ratio}", x0 + col + 4, y0 + tb_h * 0.55 + 4, 7, layer)
        self._add_text("日期 2026-08-12", x0 + col + 4, y0 + tb_h * 0.27 + 4, 7, layer)
        return f"已画图框 {width}×{height} + 右下角标题栏 180×56（图名:{title or '未填'}）"

    def draw_table(self, origin_x: float, origin_y: float,
                   col_widths: Sequence[float], row_height: float,
                   headers: Sequence[str], rows: Sequence[Sequence[str]],
                   title: str = "", layer: str = "TABLE") -> Dict[str, Any]:
        """画通用表格：表头行 + 数据行，文字自动居中、自动适配列宽。

        表格左上角在 (origin_x, origin_y)，向上排列。col_widths 为各列宽数组，
        row_height 为行高，headers 为表头，rows 为数据行列表（每行与列数匹配）。
        可画设备材料表、管道表、图例表等；title 为表题（画在表格上方居中）。
        """
        nrow, ncol = validate_table_data(col_widths, headers, rows)
        self._check_doc()
        if layer not in self.doc.layers:
            self.doc.layers.add(layer, color=7)
        total_w = float(sum(col_widths))
        total_h = float(row_height) * (nrow + 1)        # 含表头行
        handles = []
        # 外框（粗线）
        e = self._add_lwpolyline(
            [(origin_x, origin_y), (origin_x + total_w, origin_y),
             (origin_x + total_w, origin_y + total_h),
             (origin_x, origin_y + total_h)], layer, lw=50)
        handles.append(self._handle_str(e.dxf.handle))
        # 竖分隔线
        x = origin_x
        for w in col_widths[:-1]:
            x += w
            e = self.msp.add_line((x, origin_y), (x, origin_y + total_h),
                                  dxfattribs={"layer": layer})
            handles.append(self._handle_str(e.dxf.handle))
        # 横分隔线（表头下边框用粗线）
        for r in range(1, nrow + 1):
            y = origin_y + row_height * r
            if r == 1:
                e = self._add_lwpolyline(
                    [(origin_x, y), (origin_x + total_w, y)], layer, lw=50)
                handles.append(self._handle_str(e.dxf.handle))
            else:
                e = self.msp.add_line((origin_x, y), (origin_x + total_w, y),
                                      dxfattribs={"layer": layer})
                handles.append(self._handle_str(e.dxf.handle))
        # 表头 + 数据文字（单元格水平垂直居中，字高自动适配列宽）
        for r in range(nrow + 1):
            for c in range(ncol):
                cell_text = headers[c] if r == 0 else rows[r - 1][c]
                cell_text = str(cell_text)
                if not cell_text:
                    continue
                cell_x = origin_x + sum(col_widths[:c]) + col_widths[c] / 2
                cell_y = origin_y + total_h - row_height * r - row_height / 2
                base_h = row_height * 0.5 if r == 0 else row_height * 0.42
                fit_h = col_widths[c] / max(len(cell_text) * 1.05, 1.0)   # 中文宽≈字高
                th = max(min(base_h, fit_h, row_height * 0.6), 1.0)
                e = self.msp.add_text(cell_text, dxfattribs={"height": th,
                                                             "style": "GB",
                                                             "layer": layer})
                e.set_placement((cell_x, cell_y),
                                align=TextEntityAlignment.MIDDLE_CENTER)
                handles.append(self._handle_str(e.dxf.handle))
        # 表题（表格上方居中）
        if title:
            th = row_height * 0.6
            e = self.msp.add_text(title, dxfattribs={"height": th, "style": "GB",
                                                     "layer": layer})
            e.set_placement((origin_x + total_w / 2, origin_y + total_h + row_height * 0.65),
                            align=TextEntityAlignment.MIDDLE_CENTER)
            handles.append(self._handle_str(e.dxf.handle))
        return {"type": "TABLE", "layer": layer, "rows": nrow + 1, "cols": ncol,
                "width": round(total_w, 1), "height": round(total_h, 1),
                "handles": handles, "handles_count": len(handles)}

    def _add_line(self, x1: float, y1: float, x2: float, y2: float, layer: str):
        return self.msp.add_line((x1, y1), (x2, y2), dxfattribs={"layer": layer})

    def _add_lwpolyline(self, pts: List[Point], layer: str, lw: int):
        e = self.msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": layer})
        e.dxf.lineweight = lw
        return e

    def _add_text(self, text: str, x: float, y: float, height: float, layer: str):
        if not text:
            return None
        e = self.msp.add_text(text, dxfattribs={"height": height, "style": "GB", "layer": layer})
        e.dxf.insert = (x, y)
        return e

    # ---------- 查询 / 修改 ----------

    def preview_region(self, **kwargs) -> Dict[str, Any]:
        """对当前内存文档生成局部图像，不保存图纸。"""
        from cad_mcp.drawing.preview import render_region
        self._check_doc()
        return render_region(self.doc, mode="dxf", **kwargs)

    def get_entity_properties(self, handle: str) -> Dict[str, Any]:
        """读取模型空间实体属性及可编辑字段。"""
        from cad_mcp.drawing.entity_properties import get_entity_properties
        return get_entity_properties(self, handle, "dxf")

    def edit_entity_properties(self, handle: str, properties: Dict[str, Any],
                               *, dry_run: bool = True) -> Dict[str, Any]:
        """先预检，按白名单修改实体并回读验证；默认零写入预览。"""
        from cad_mcp.drawing.entity_properties import edit_entity_properties
        return edit_entity_properties(self, handle, properties, dry_run=dry_run, mode="dxf")

    def list_entities(self, layer: Optional[str] = None,
                      offset: int = 0, limit: int = 100) -> str:
        """分页列出图元。句柄统一为十进制。"""
        self._check_doc()
        if offset < 0 or not 1 <= limit <= 500:
            raise ValueError("offset 必须大于等于 0，limit 必须在 1 到 500 之间")
        rows = []
        matched = 0
        has_more = False
        for e in self.msp:
            if layer and e.dxf.layer != layer:
                continue
            if matched < offset:
                matched += 1
                continue
            if len(rows) >= limit:
                has_more = True
                break
            rows.append(f"{self._handle_str(e.dxf.handle)} | {e.dxftype()} | 图层:{e.dxf.layer}")
            matched += 1
        if not rows:
            return ("图纸为空" if offset == 0 and layer is None else
                    f"从 offset={offset} 起没有匹配图元")
        note = (f"\n[分页] offset={offset}，返回 {len(rows)} 条"
                + (f"；后面还有数据，请用 offset={offset + len(rows)} 继续" if has_more else "；已到末页"))
        return "\n".join(rows) + note

    def _get_entity(self, handle: str):
        """按句柄（十进制字符串）从文档实体库取图元。

        ezdxf 实体库的键是十六进制字符串，故查找时把十进制句柄转回十六进制。
        """
        if self.doc is None:
            return None
        try:
            hex_key = format(int(handle), "X")   # 十进制 → 十六进制大写
            entity = self.doc.entitydb.get(hex_key)
            if entity is not None and not entity.is_alive:
                return None
            return entity
        except (ValueError, KeyError):
            return None

    def delete_entity(self, handle: str) -> str:
        """按句柄删除图元。"""
        self._check_doc()
        e = self._get_entity(handle)
        if e is None:
            return f"未找到句柄为 {handle} 的图元"
        kind, layer = e.dxftype(), e.dxf.layer
        e.destroy()
        return f"已删除 {kind}（句柄 {handle}，图层 {layer}）"

    def move_entity(self, handle: str, dx: float, dy: float) -> str:
        """按句柄移动图元（位移 dx, dy）。"""
        self._check_doc()
        e = self._get_entity(handle)
        if e is None:
            return f"未找到句柄为 {handle} 的图元"
        e.translate(dx, dy, 0)
        return f"已移动图元 {handle}，位移 ({dx}, {dy})"

    def copy_entity(self, handle: str, dx: float, dy: float) -> Dict[str, Any]:
        """复制图元并偏移 (dx, dy)，返回新副本句柄。原图元保持不变。"""
        self._check_doc()
        e = self._get_entity(handle)
        if e is None:
            raise ValueError(f"未找到句柄为 {handle} 的图元")
        new_e = e.copy()                 # ezdxf 的 copy() 返回独立副本（不挂任何容器）
        new_e.dxf.layer = e.dxf.layer
        self.msp.add_entity(new_e)       # 加入模型空间并分配新句柄
        try:
            new_e.translate(dx, dy, 0)
        except NotImplementedError:
            new_e.destroy()
            raise ValueError(f"图元类型 {e.dxftype()} 暂不支持复制平移")
        return {"type": e.dxftype(), "handle": self._handle_str(new_e.dxf.handle),
                "layer": new_e.dxf.layer, "copied_from": handle}

    def _copy_and_translate(self, e, dx: float, dy: float):
        """复制单个图元并平移，返回新句柄；平移失败（类型不支持）则返回 None。"""
        try:
            new_e = e.copy()
            new_e.dxf.layer = e.dxf.layer
            self.msp.add_entity(new_e)
            new_e.translate(dx, dy, 0)
        except NotImplementedError:
            try:
                new_e.destroy()
            except Exception:
                pass
            return None
        return self._handle_str(new_e.dxf.handle)

    def array_rect(self, handles: List[str], rows: int, cols: int,
                   row_spacing: float, col_spacing: float) -> str:
        """矩形阵列：把指定图元按 rows 行 × cols 列复制，原件保留在第 0 行第 0 列。

        row_spacing 为行间距（Y 方向），col_spacing 为列间距（X 方向）。
        返回新副本句柄数。
        """
        self._check_doc()
        if rows < 1 or cols < 1:
            raise ValueError("行数、列数至少为 1")
        if rows == 1 and cols == 1:
            raise ValueError("1 行 1 列没有阵列意义，请至少 2 行或 2 列")
        validate_bulk_creation(len(handles), rows * cols - 1, "矩形阵列")
        if row_spacing == 0 and rows > 1:
            raise ValueError("多行时行距不能为 0")
        if col_spacing == 0 and cols > 1:
            raise ValueError("多列时列距不能为 0")
        created = []
        for h in handles:
            e = self._get_entity(h)
            if e is None:
                continue
            for r in range(rows):
                for c in range(cols):
                    if r == 0 and c == 0:
                        continue            # 原件位置，不重复复制
                    h2 = self._copy_and_translate(e, c * col_spacing, r * row_spacing)
                    if h2 is not None:
                        created.append(h2)
        if not created:
            raise ValueError("没有找到可阵列的图元")
        return (f"已阵列 {len(handles)} 个图元为 {rows} 行 × {cols} 列"
                f"（行距 {row_spacing}，列距 {col_spacing}），新增 {len(created)} 个副本")

    def array_polar(self, handles: List[str], cx: float, cy: float,
                    count: int, total_angle: float = 360) -> str:
        """环形阵列：把指定图元绕圆心 (cx, cy) 均布复制 count 个，原件保留在第 0 个。

        total_angle=360 时整圈均布（相邻夹角 = 360/count）；
        部分角度（如 90）时按 count-1 等分分布在该角内。
        """
        self._check_doc()
        if count < 2:
            raise ValueError("环形阵列数量至少 2 个")
        validate_bulk_creation(len(handles), count - 1, "环形阵列")
        if count == 2 and total_angle >= 360:
            total_angle = 180                # 2 个整圈会重合，按半圈处理更合理
        full = total_angle >= 359.999
        created = []
        for h in handles:
            e = self._get_entity(h)
            if e is None:
                continue
            for i in range(1, count):
                ang = i * (total_angle / count if full else total_angle / (count - 1))
                rad = math.radians(ang)
                try:
                    new_e = e.copy()
                    new_e.dxf.layer = e.dxf.layer
                    self.msp.add_entity(new_e)
                except Exception:
                    continue
                from ezdxf.math import Matrix44
                m = (Matrix44.translate(cx, cy, 0) *
                     Matrix44.z_rotate(rad) *
                     Matrix44.translate(-cx, -cy, 0))
                try:
                    new_e.transform(m)
                except NotImplementedError:
                    try:
                        new_e.destroy()
                    except Exception:
                        pass
                    continue
                created.append(self._handle_str(new_e.dxf.handle))
        if not created:
            raise ValueError("没有找到可阵列的图元")
        return (f"已环形阵列 {len(handles)} 个图元绕 ({cx}, {cy}) 均布 {count} 个"
                f"（总角 {total_angle}°），新增 {len(created)} 个副本")

    def copy_linear(self, handles: List[str], dx: float, dy: float, copies: int) -> str:
        """线性复制：把指定图元按位移 (dx, dy) 等间距复制 copies 份。

        第 i 份偏移 (dx*i, dy*i)，原件保留。适合多排隔墙、等距布管等。
        """
        self._check_doc()
        if copies < 1:
            raise ValueError("份数至少为 1")
        validate_bulk_creation(len(handles), copies, "线性复制")
        if dx == 0 and dy == 0:
            raise ValueError("位移 (dx, dy) 不能同时为 0")
        created = []
        for h in handles:
            e = self._get_entity(h)
            if e is None:
                continue
            for i in range(1, copies + 1):
                h2 = self._copy_and_translate(e, dx * i, dy * i)
                if h2 is not None:
                    created.append(h2)
        if not created:
            raise ValueError("没有找到可复制的图元")
        return (f"已线性复制 {len(handles)} 个图元 × {copies} 份"
                f"（位移 {dx},{dy}），新增 {len(created)} 个副本")

    def offset(self, handle: str, distance: float) -> Dict[str, Any]:
        """按曲线自身方向做真正的几何偏移；保留源图元。"""
        self._check_doc()
        if abs(distance) <= 1e-12:
            raise ValueError("偏移距离不能为 0")
        source = self._get_entity(handle)
        if source is None:
            raise ValueError(f"未找到句柄为 {handle} 的图元")
        kind = source.dxftype()
        layer = str(source.dxf.layer)
        created = []

        if kind == "LINE":
            start = source.dxf.start
            end = source.dxf.end
            dx = float(end.x - start.x)
            dy = float(end.y - start.y)
            length = math.hypot(dx, dy)
            if length <= 1e-12:
                raise ValueError("零长度 LINE 不能偏移")
            ox, oy = -dy / length * distance, dx / length * distance
            created.append(self.msp.add_line(
                (float(start.x) + ox, float(start.y) + oy),
                (float(end.x) + ox, float(end.y) + oy),
                dxfattribs={"layer": layer},
            ))
        elif kind in ("CIRCLE", "ARC"):
            radius = float(source.dxf.radius) + distance
            if radius <= 1e-12:
                raise ValueError("偏移后半径必须大于 0")
            center = source.dxf.center
            if kind == "CIRCLE":
                created.append(self.msp.add_circle(
                    (float(center.x), float(center.y)), radius,
                    dxfattribs={"layer": layer},
                ))
            else:
                created.append(self.msp.add_arc(
                    (float(center.x), float(center.y)), radius,
                    start_angle=float(source.dxf.start_angle),
                    end_angle=float(source.dxf.end_angle),
                    dxfattribs={"layer": layer},
                ))
        elif kind in ("LWPOLYLINE", "POLYLINE"):
            part, closed = self._polyline_part(source)
            if any(abs(vertex[2]) > 1e-12 for vertex in part):
                raise ValueError("DXF 后端暂不支持含圆弧段的多段线偏移；acad 模式可调用 AutoCAD 原生 OFFSET")
            points = [(vertex[0], vertex[1]) for vertex in part]
            offset_distance = distance
            if closed:
                signed_area = sum(
                    points[i][0] * points[(i + 1) % len(points)][1]
                    - points[(i + 1) % len(points)][0] * points[i][1]
                    for i in range(len(points))
                ) / 2.0
                if abs(signed_area) <= 1e-12:
                    raise ValueError("零面积闭合多段线不能确定内外偏移方向")
                # offset_vertices_2d 的正值始终向路径左侧；公开接口中闭合曲线正值表示向外。
                offset_distance = -distance if signed_area > 0 else distance
            offset_points = [(float(p.x), float(p.y))
                             for p in offset_vertices_2d(points, offset_distance, closed=closed)]
            created.append(self.msp.add_lwpolyline(
                offset_points,
                close=closed,
                dxfattribs={"layer": layer},
            ))
        else:
            raise ValueError(
                f"DXF 后端不支持 {kind} 偏移；支持 LINE / CIRCLE / ARC / "
                "直线段 LWPOLYLINE / 2D POLYLINE，更多类型请切换 acad 模式")

        return {
            "source_handle": handle,
            "source_type": kind,
            "distance": distance,
            "source_retained": True,
            "entities": [self._entity_summary(entity) for entity in created],
        }

    def rotate_entity(self, handle: str, base_x: float, base_y: float, angle: float) -> str:
        """旋转图元：绕基点 (base_x, base_y) 逆时针旋转 angle 度。"""
        self._check_doc()
        e = self._get_entity(handle)
        if e is None:
            raise ValueError(f"未找到句柄为 {handle} 的图元")
        from ezdxf.math import Matrix44
        # 平移到原点 → 绕 Z 旋转 → 平移回：Matrix44 列向量约定，m1*m2 表示先应用 m2
        m = (Matrix44.translate(base_x, base_y, 0) *
             Matrix44.z_rotate(math.radians(angle)) *
             Matrix44.translate(-base_x, -base_y, 0))
        try:
            e.transform(m)
        except NotImplementedError:
            raise ValueError(f"图元类型 {e.dxftype()} 暂不支持旋转")
        return f"已旋转图元 {handle}，绕 ({base_x}, {base_y}) 逆时针 {angle}°"

    def mirror_entity(self, handle: str, x1: float, y1: float, x2: float, y2: float) -> str:
        """镜像图元：关于直线 (x1,y1)-(x2,y2) 镜像。"""
        self._check_doc()
        e = self._get_entity(handle)
        if e is None:
            raise ValueError(f"未找到句柄为 {handle} 的图元")
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        if length == 0:
            raise ValueError("镜像线两端点重合")
        ux, uy = dx / length, dy / length
        from ezdxf.math import Matrix44
        # 关于过原点方向 (ux,uy) 的直线反射矩阵（ezdxf 要求扁平 16 元素，行主序）
        ref = Matrix44([
            2 * ux * ux - 1, 2 * ux * uy, 0, 0,
            2 * ux * uy, 2 * uy * uy - 1, 0, 0,
            0, 0, 1, 0,
            0, 0, 0, 1,
        ])
        m = Matrix44.translate(x1, y1, 0) * ref * Matrix44.translate(-x1, -y1, 0)
        try:
            e.transform(m)
        except NotImplementedError:
            raise ValueError(f"图元类型 {e.dxftype()} 暂不支持镜像")
        return f"已镜像图元 {handle}（镜像线 {x1},{y1} - {x2},{y2}）"

    def check_drawing(self, tolerance: Optional[float] = None) -> str:
        """图面体检：扫描常见问题。

        检查项：
          1. 重叠图元：两个圆/两条线几乎完全重合（同位置同尺寸）；
          2. 疑似断线：两条线的端点相距很近（小于容差）但未严格相接；
          3. 空图层：有图层定义但没有任何图元。

        tolerance 容差（毫米）留空自动按图纸范围算（对角线的 0.1%）。返回问题清单文本。
        """
        self._check_doc()
        entity_count = len(self.msp)
        if entity_count > MAX_DRAWING_SCAN_ENTITIES:
            return (
                f"图面检查未完成：模型空间有 {entity_count} 个图元，超过单次扫描上限 "
                f"{MAX_DRAWING_SCAN_ENTITIES}；未报告检查通过")
        ext = self._drawing_extent()
        if tolerance is None:
            if ext is None:
                tolerance = 1.0
            else:
                xmin, ymin, xmax, ymax = ext
                tolerance = max(round(math.hypot(xmax - xmin, ymax - ymin) * 0.001, 2), 1.0)
        tol = tolerance
        lines = []      # (handle, (sx,sy), (ex,ey))
        circles = []    # (handle, (cx,cy), r)
        used_layers = set()
        for e in self.msp:
            t = e.dxftype()
            try:
                if hasattr(e.dxf, "layer"):
                    used_layers.add(e.dxf.layer)
                if t == "LINE":
                    s, en = e.dxf.start, e.dxf.end
                    lines.append((self._handle_str(e.dxf.handle), (s.x, s.y), (en.x, en.y)))
                elif t == "CIRCLE":
                    c, r = e.dxf.center, e.dxf.radius
                    circles.append((self._handle_str(e.dxf.handle), (c.x, c.y), r))
            except Exception:
                continue
        scan = scan_line_circle_issues(lines, circles, tol)
        issues = list(scan["issues"])
        # 3. 空图层
        for lay in self.doc.layers:
            n = lay.dxf.name
            if n == "Defpoints":
                continue
            if n not in used_layers:
                if len(issues) >= MAX_DRAWING_ISSUES:
                    scan["complete"] = False
                    scan["stopped_reason"] = f"问题数超过上限 {MAX_DRAWING_ISSUES}"
                    break
                issues.append(f"[空图层] 图层 {n} 定义了但没有图元")
        if not scan["complete"]:
            detail = "\n" + "\n".join(issues) if issues else ""
            return (
                f"图面检查未完成：{scan['stopped_reason']}；已发现 {len(issues)} 个问题，"
                "未报告检查通过" + detail)
        if not issues:
            return ("图面检查通过：未发现重叠图元、疑似断线或空图层"
                    f"（容差 {tol:.1f}mm，共检查 {len(lines)} 条线、{len(circles)} 个圆）")
        return f"发现 {len(issues)} 个问题：\n" + "\n".join(issues)

    # ---------- 环境工程语义元数据与批处理 ----------

    def set_entity_metadata(self, handle: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """把环境工程语义 ID 和摘要写入实体 XData。"""
        self._check_doc()
        entity = self._get_entity(handle)
        if entity is None:
            raise ValueError(f"未找到句柄为 {handle} 的图元")
        if XDATA_APP_NAME not in self.doc.appids:
            self.doc.appids.add(XDATA_APP_NAME)
        chunks = pack_metadata(metadata)
        entity.set_xdata(XDATA_APP_NAME, [(1000, chunk) for chunk in chunks])
        return {"handle": handle, "app_name": XDATA_APP_NAME,
                "chunk_count": len(chunks)}

    @staticmethod
    def _state_value(value: Any) -> Any:
        if hasattr(value, "x") and hasattr(value, "y"):
            return [float(value.x), float(value.y), float(getattr(value, "z", 0.0))]
        if isinstance(value, dict):
            return {str(key): CadDocument._state_value(item)
                    for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
        if isinstance(value, (list, tuple)):
            return [CadDocument._state_value(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    def _entity_state_hash(self, entity: Any) -> str:
        kind = entity.dxftype()
        payload: Dict[str, Any] = {
            "type": kind,
            "layer": str(getattr(entity.dxf, "layer", "0")),
        }
        if kind == "LINE":
            payload.update(start=self._state_value(entity.dxf.start),
                           end=self._state_value(entity.dxf.end))
        elif kind == "LWPOLYLINE":
            payload.update(points=self._state_value(list(entity.get_points("xyseb"))),
                           closed=bool(entity.closed))
        elif kind in {"POLYLINE", "SPLINE"}:
            payload["points"] = self._state_value(
                [vertex.dxf.location for vertex in getattr(entity, "vertices", [])])
            payload["closed"] = bool(getattr(entity, "is_closed", False))
        elif kind == "CIRCLE":
            payload.update(center=self._state_value(entity.dxf.center),
                           radius=float(entity.dxf.radius))
        elif kind == "ARC":
            payload.update(center=self._state_value(entity.dxf.center),
                           radius=float(entity.dxf.radius),
                           start_angle=float(entity.dxf.start_angle),
                           end_angle=float(entity.dxf.end_angle))
        elif kind == "TEXT":
            payload.update(text=str(entity.dxf.text),
                           insert=self._state_value(entity.dxf.insert),
                           height=float(entity.dxf.height),
                           rotation=float(getattr(entity.dxf, "rotation", 0.0)),
                           style=str(getattr(entity.dxf, "style", "Standard")))
        elif kind == "MTEXT":
            payload.update(text=str(entity.text),
                           insert=self._state_value(entity.dxf.insert),
                           height=float(entity.dxf.char_height),
                           width=float(getattr(entity.dxf, "width", 0.0)),
                           rotation=float(getattr(entity.dxf, "rotation", 0.0)),
                           style=str(getattr(entity.dxf, "style", "Standard")))
        elif kind == "INSERT":
            payload.update(name=str(entity.dxf.name),
                           insert=self._state_value(entity.dxf.insert),
                           xscale=float(getattr(entity.dxf, "xscale", 1.0)),
                           yscale=float(getattr(entity.dxf, "yscale", 1.0)),
                           rotation=float(getattr(entity.dxf, "rotation", 0.0)))
        else:
            attributes = dict(entity.dxfattribs())
            for volatile in ("handle", "owner"):
                attributes.pop(volatile, None)
            payload["attributes"] = self._state_value(attributes)
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    def get_entity_state_hash(self, handle: str) -> str:
        self._check_doc()
        entity = self._get_entity(handle)
        if entity is None:
            raise ValueError(f"未找到句柄为 {handle} 的图元")
        return self._entity_state_hash(entity)

    def get_entity_metadata(self, handle: str) -> Optional[Dict[str, Any]]:
        self._check_doc()
        entity = self._get_entity(handle)
        if entity is None:
            raise ValueError(f"未找到句柄为 {handle} 的图元")
        try:
            tags = entity.get_xdata(XDATA_APP_NAME)
        except (ezdxf.DXFValueError, ValueError):
            return None
        chunks = [tag.value for tag in tags if tag.code == 1000]
        return unpack_metadata(chunks) if chunks else None

    def list_managed_entities(self) -> List[ManagedEntityRecord]:
        self._check_doc()
        records: List[ManagedEntityRecord] = []
        for entity in self.msp:
            try:
                tags = entity.get_xdata(XDATA_APP_NAME)
            except (ezdxf.DXFValueError, ValueError):
                continue
            chunks = [tag.value for tag in tags if tag.code == 1000]
            if not chunks:
                continue
            try:
                metadata = unpack_metadata(chunks)
            except (ValueError, TypeError):
                continue
            current_state_hash = self._entity_state_hash(entity)
            materialized_hash = metadata.get("materialized_hash")
            records.append(ManagedEntityRecord(
                handle=self._handle_str(entity.dxf.handle),
                semantic_id=str(metadata.get("semantic_id", "")),
                view_id=metadata.get("view_id"),
                role=str(metadata.get("role", "geometry")),
                geometry_hash=str(metadata.get("geometry_hash", "")),
                project_id=(str(metadata["project_id"])
                            if metadata.get("project_id") else None),
                operation_group=(str(metadata["operation_group"])
                                 if metadata.get("operation_group") else None),
                entity_index=(metadata.get("entity_index")
                              if isinstance(metadata.get("entity_index"), int) else None),
                entity_count=(metadata.get("entity_count")
                              if isinstance(metadata.get("entity_count"), int) else None),
                materialized_hash=(str(materialized_hash)
                                   if materialized_hash is not None else None),
                current_state_hash=current_state_hash,
                manually_modified=(materialized_hash is not None
                                   and str(materialized_hash) != current_state_hash),
                metadata=metadata,
            ))
        return records

    def apply_operation_plan(self, plan, policy: str = "update_managed") -> Dict[str, Any]:
        """以 DXF 内存快照为事务边界执行完整操作计划。"""
        self._check_doc()
        snapshot = io.StringIO()
        self.doc.write(snapshot)
        active_layer = self.active_layer
        active_textstyle = self.active_textstyle
        active_dimstyle = self.active_dimstyle
        from cad_mcp.core.transactions import execute_reconciled_plan
        try:
            return execute_reconciled_plan(self, plan, policy)
        except Exception:
            snapshot.seek(0)
            self.doc = ezdxf.read(snapshot)
            self.msp = self.doc.modelspace()
            self.active_layer = active_layer
            self.active_textstyle = active_textstyle
            self.active_dimstyle = active_dimstyle
            raise

    # ---------- 内部辅助 ----------

    def _drawing_extent(self) -> Optional[Tuple[float, float, float, float]]:
        """计算模型空间全部图元的包围盒 (xmin, ymin, xmax, ymax)；空图返回 None。"""
        if self.msp is None:
            return None
        b = bbox.extents(self.msp)
        if not b.has_data:
            return None
        return (b.extmin.x, b.extmin.y, b.extmax.x, b.extmax.y)

    def get_geometry_extents(self) -> Dict[str, Any]:
        """返回当前模型空间总包围盒，供调用方自行判断出图比例。"""
        self._check_doc()
        ext = self._drawing_extent()
        if ext is None:
            return {"empty": True, "entity_count": 0}
        xmin, ymin, xmax, ymax = ext
        width, height = xmax - xmin, ymax - ymin
        return {
            "empty": False,
            "entity_count": len(self.msp),
            "min": [float(xmin), float(ymin)],
            "max": [float(xmax), float(ymax)],
            "width": float(width),
            "height": float(height),
            "center": [float((xmin + xmax) / 2), float((ymin + ymax) / 2)],
            "diagonal": float(math.hypot(width, height)),
        }

    def _current_text_height(self) -> float:
        """读取当前文字样式字高；不再根据图形范围自动计算。"""
        try:
            style = self.doc.styles.get(self.active_textstyle)
            height = float(style.dxf.height)
            if height > 0:
                return height
        except Exception:
            pass
        height = float(self.doc.header.get("$TEXTSIZE", 2.5))
        return height if height > 0 else 2.5

    def set_linetype_scale(self, scale: float) -> str:
        """兼容性接口：显式设置全局线型比例，不做任何自动推断。"""
        self._check_doc()
        self.doc.header["$LTSCALE"] = float(scale)
        return f"已设置全局线型比例 {scale}"

    def query_entities(self, layer: Optional[str] = None,
                       entity_type: Optional[str] = None,
                       offset: int = 0, limit: int = 50,
                       include_bounds: bool = False) -> Dict[str, Any]:
        """从模型空间索引 offset 开始分页返回，达到 limit 后停止扫描。"""
        self._check_doc()
        wanted_type = entity_type.upper() if entity_type else None
        matched = []
        entities = list(self.msp)
        total = len(entities)
        i = min(offset, total)
        while i < total and len(matched) < limit:
            e = entities[i]
            if layer and e.dxf.layer != layer:
                i += 1
                continue
            if wanted_type and e.dxftype().upper() != wanted_type:
                i += 1
                continue
            row = {
                "index": i,
                "handle": self._handle_str(e.dxf.handle),
                "type": e.dxftype(),
                "layer": e.dxf.layer,
            }
            if include_bounds:
                eb = bbox.extents([e])
                if eb.has_data:
                    row["bounds"] = {
                        "min": [float(eb.extmin.x), float(eb.extmin.y)],
                        "max": [float(eb.extmax.x), float(eb.extmax.y)],
                    }
            matched.append(row)
            i += 1
        return {
            "entities": matched,
            "offset": offset,
            "limit": limit,
            "modelspace_count": total,
            "next_offset": i if i < total else None,
            "scanned": max(i - offset, 0),
        }

    def regen(self) -> Dict[str, Any]:
        """DXF 文件模式无需显示重生成；保留统一能力接口。"""
        self._check_doc()
        return {"mode": "dxf", "regenerated": False,
                "message": "DXF 文件模式无需重生成"}

    def health(self, reconnect: bool = False) -> Dict[str, Any]:
        return {
            "mode": "dxf",
            "connected": True,
            "document_open": self.doc is not None,
            "document_state": "open" if self.doc is not None else "closed",
            "file": self.current_path.name if self.current_path else None,
            **health_metadata("dxf"),
        }

    def _check_doc(self) -> None:
        if self.doc is None or self.msp is None:
            raise RuntimeError("当前没有打开的图纸，请先新建或打开图纸")

    def _layer(self, layer: Optional[str]) -> str:
        """返回图元应使用的图层：未指定则用当前活动图层。"""
        return layer or self.active_layer
