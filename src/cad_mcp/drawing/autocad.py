#!/usr/bin/env python3
"""
AutoCAD COM 后端 — 通过 COM 自动化接口直接操控本地 AutoCAD 绘图。

与 DXF 后端（cad_drawing.py）提供相同的接口，但底层是"操控 AutoCAD 软件本身"：
所有图元由 AutoCAD 引擎绘制，保存为原生 DWG 格式。

句柄约定：统一显示为**十进制字符串**（底层 COM 的 Handle 为十六进制，显示时统一转换），
与 DXF 后端输出格式一致。

依赖：pywin32（win32com）、本机已安装并注册 AutoCAD。
"""

import math
import hashlib
import os
import re
import time
import uuid
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Dict, Any, Optional, List, Sequence, Tuple

import pythoncom
import win32api
import win32com.client
import win32event
import winerror

from cad_mcp.dwt.contracts import build_drawing_settings_payload
from cad_mcp.dwt.autocad import AcadDwtMixin, _dispatch_array, _safe_get
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
from cad_mcp.engineering.templates import (
    TemplateManifest,
    copy_template_source,
    load_template_manifest,
    sha256_file,
)
from cad_mcp.core.runtime import (
    CadMcpError,
    ConflictError,
    UnsupportedOperationError,
    health_metadata,
    record_error,
)
from cad_mcp.core.open_paths import resolve_open_drawing_path
from cad_mcp.drawing.hatch_regions import fill_acad_region
from cad_mcp.drawing.hatch_regions import acad_source, refresh_valid_hatch
from cad_mcp.drawing.layouts import AcadLayoutMixin
from cad_mcp.drawing.com_read import bounding_box, out_pair
from cad_mcp.core.hatch_boundaries import validate_source

# 保存格式常量（AutoCAD 文件格式枚举）：64 = ac2018_dwg
SAVE_FORMAT_DWG = 64

# 预设图层与颜色（与 DXF 后端一致）
LAYER_COLORS = {
    "WALL": 7,
    "DIM": 2,
    "FURNITURE": 5,
    "TEXT": 4,
    "FILL": 6,
}

Point = Tuple[float, float]

# 标准图纸幅面（宽×高，毫米），横放（工程图框主流方向），用于 draw_title_block 的 size 参数
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


def _pt(x: float, y: float, z: float = 0.0):
    """把 (x, y, z) 包装成 COM 需要的 VARIANT 数组。"""
    return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [x, y, z])


def _lisp_string(value: str) -> str:
    """把外部字符串放进 AutoLISP 字面量，避免命令注入或换行截断。"""
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("图块名不能包含控制字符")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _lisp_real(value: float) -> str:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("图块基点必须为有限数值")
    return format(number, ".17g")


def _pt2(flat: Sequence[float]):
    """把扁平 2D 坐标数组包装成 AddLightWeightPolyline 需要的 VARIANT。"""
    flat = [round(v, 3) for v in flat]
    return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, flat)


def _pt3(flat: Sequence[float]):
    """把扁平 3D 坐标数组包装成 AddMLine 需要的 VARIANT。"""
    flat = [round(v, 3) for v in flat]
    return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, flat)


def _pt2_coord(variant) -> Tuple[float, float]:
    """把 COM 三维坐标 VARIANT 转为 (x, y) 元组。"""
    return (float(variant[0]), float(variant[1]))


def _polyline_pts(e) -> Tuple[List[Point], bool]:
    """读轻量多段线的顶点列表与闭合标志。

    AutoCAD 用重复首点或 Closed 属性表示闭合；若首尾重合但 Closed 为 False，
    归一化为闭合（去掉重复尾点），保证与 ezdxf 后端的顶点语义一致。
    """
    coords = list(e.Coordinates)
    pts = [(float(coords[k]), float(coords[k + 1])) for k in range(0, len(coords), 2)]
    closed = bool(e.Closed)
    if not closed and len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
        closed = True
    return pts, closed


def _polyline_has_bulges(e) -> bool:
    """Fail closed if an AcDbPolyline arc segment cannot be ruled out."""

    coords = list(e.Coordinates)
    try:
        return any(abs(float(e.GetBulge(index))) > 1e-12
                   for index in range(len(coords) // 2))
    except Exception as exc:
        raise RuntimeError("无法验证多段线是否包含圆弧段，已停止修剪") from exc


# "AutoCAD 单实例"操作级锁状态。
# AutoCAD 的 COM 接口不支持多进程**并发**发命令，但允许多个客户端**交替**操作。
# 锁做成**操作级**：只在执行单个 acad 绘图/修改工具调用期间持有，调用结束自动释放，
# 因此 Claude Code 与 Codex 都能用，谁操作谁短暂持锁，用完即还，不会把对方锁死。
# 锁由 MCP 层的锁代理（cad_mcp_server._AcadLockProxy）在调用前后获取/释放；
# 进程异常退出时 Windows 自动回收互斥锁，不会残留。
_LOCK_MUTEX = None


# AutoCAD COM 服务器忙时返回的 HRESULT（32 位无符号值）：
#   RPC_E_SERVERCALL_REJECTED    0x80010001 调用被拒绝
#   RPC_E_CALL_REJECTED          0x80010108 服务器正忙，拒绝接收呼叫
#   RPC_E_SERVERCALL_RETRYLATER  0x8001010A 服务器忙，稍后重试
#   RPC_E_TIMEOUT                0x8001011F 调用超时
_RETRY_HRESULTS = {0x80010001, 0x80010108, 0x8001010A, 0x8001011F}

# A cooperative deadline bounds the whole scan, including individual COM retries.
# It cannot interrupt a single native COM call that never returns.
_COM_SCAN_TIMEOUT = 30.0
_COM_SCAN_STATE = ContextVar("cad_com_scan_state", default=None)


def _check_com_scan_deadline() -> None:
    state = _COM_SCAN_STATE.get()
    if state is not None and time.monotonic() >= state[0]:
        raise TimeoutError(
            f"AutoCAD {state[1]} 扫描未完成：已达到整体耗时上限，"
            "已停止；未返回不完整结果")


def _raise_scan_transport_error(exc: Exception) -> None:
    """Optional unsupported properties stay unverified; a lost/busy server must stop."""
    if isinstance(exc, TimeoutError):
        raise exc
    if isinstance(exc, pythoncom.com_error):
        hr = getattr(exc, "hresult", None)
        if hr is not None and (hr & 0xFFFFFFFF) in (
            _RETRY_HRESULTS | {0x800706BA, 0x800401FD}
        ):
            raise exc


def _bounded_com_scan(method):
    @wraps(method)
    def bounded(self, *args, **kwargs):
        state = (time.monotonic() + _COM_SCAN_TIMEOUT, method.__name__)
        parent = _COM_SCAN_STATE.get()
        if parent is not None and parent[0] < state[0]:
            state = parent
        token = _COM_SCAN_STATE.set(state)
        try:
            _check_com_scan_deadline()
            result = method(self, *args, **kwargs)
            _check_com_scan_deadline()
            return result
        finally:
            _COM_SCAN_STATE.reset(token)

    return bounded


def _is_com_object(obj) -> bool:
    """判断返回值是否为 pywin32 COM 对象（有 _oleobj_ 属性）。"""
    try:
        return hasattr(obj, "_oleobj_")
    except Exception:
        return False


def _refresh_com_dispatch(obj):
    """Rebuild runtime member maps with bounded COM metadata retries."""
    from win32com.client import build, dynamic

    raw = _ComRetryProxy._unwrap(obj)
    if not _is_com_object(raw):
        return obj

    def rebuild():
        info = raw._oleobj_.GetTypeInfo()
        attr = info.GetTypeAttr()
        if attr[5] == pythoncom.TKIND_INTERFACE and attr[11] & pythoncom.TYPEFLAG_FDUAL:
            info = info.GetRefTypeInfo(info.GetRefTypeOfImplType(-1))
            attr = info.GetTypeAttr()
        return _ComRetryProxy(dynamic.CDispatch(
            raw._oleobj_, build.DispatchItem(info, attr, None, 0),
            info.GetDocumentation(-1)[0]))

    # Includes GetFuncDesc calls made while building the eager member map.
    return _ComRetryProxy(raw)._retry(rebuild)


class _ComRetryProxy:
    """COM 对象代理：AutoCAD 忙（RPC 拒绝/稍后重试）时自动等待并重试。

    为什么需要它：COM 是同步跨进程调用，客户端无间隔连发大量调用时，
    AutoCAD 的窗口消息泵来不及处理，会返回 RPC_E_CALL_REJECTED 拒绝接收。
    本代理拦截所有方法调用，遇到可重试的 HRESULT 时：
      1. pythoncom.PumpWaitingMessages() —— 让 AutoCAD 处理积压的窗口消息；
      2. 指数退避 sleep（0.05s → 最大 1.5s）；
      3. 最多重试 8 次。
    返回的 COM 对象会递归包装，因此子对象（图元、图层、文档）的方法调用同样受保护。
    属性读取与赋值同样受重试保护（AutoCAD 忙时属性读取也会被拒绝）。
    """

    _retryable_hr = _RETRY_HRESULTS

    def __init__(self, obj):
        object.__setattr__(self, "_obj", obj)

    def _retry(self, fn):
        """执行 COM 操作（方法调用或属性读取），AutoCAD 忙时等待并重试。"""
        for attempt in range(8):
            _check_com_scan_deadline()
            try:
                result = fn()
                _check_com_scan_deadline()
                return result
            except pythoncom.com_error as e:
                hr = getattr(e, "hresult", None)
                if hr is not None and (hr & 0xFFFFFFFF) in self._retryable_hr:
                    try:
                        pythoncom.PumpWaitingMessages()
                    except Exception:
                        pass
                    time.sleep(min(0.05 * (2 ** attempt), 1.5))
                    continue
                raise
        raise TimeoutError("AutoCAD 持续拒绝 COM 调用（服务器忙），请稍后再试")

    def __getattr__(self, name):
        obj = object.__getattribute__(self, "_obj")
        # 属性读取也可能被 AutoCAD 拒绝（忙），同样走重试
        try:
            attr = self._retry(lambda: getattr(obj, name))
        except AttributeError:
            from win32com.client.dynamic import CDispatch

            if not isinstance(obj, CDispatch):
                raise
            # pywin32 can hide a rejected GetIDsOfNames/GetTypeInfo as a
            # missing member. Refresh metadata once, without invoking a method.
            refreshed = self._unwrap(_refresh_com_dispatch(obj))
            attr = self._retry(lambda: getattr(refreshed, name))
            object.__setattr__(self, "_obj", refreshed)
        # 顺序关键：pywin32 的动态 Dispatch 对象（COM 属性/子对象）同时可调用且
        # 有 _oleobj_；真正的 COM 方法（type=method）没有 _oleobj_。必须先判
        # _oleobj_（属性对象 → 包装），再判 callable（方法 → 重试包装）。
        if _is_com_object(attr):
            return _ComRetryProxy(attr)
        if callable(attr):
            def wrapper(*args, **kwargs):
                return self._call(attr, *args, **kwargs)
            return wrapper
        return attr

    def __setattr__(self, name, value):
        # 代理对象不能作为 COM 属性值回传（如 ActiveLayer = 图层），需解包为原生对象
        if isinstance(value, _ComRetryProxy):
            value = object.__getattribute__(value, "_obj")
        obj = object.__getattribute__(self, "_obj")
        self._retry(lambda: setattr(obj, name, value))

    @staticmethod
    def _unwrap(value):
        """把代理解包为原生 COM 对象，其余类型原样返回。"""
        return value._obj if isinstance(value, _ComRetryProxy) else value

    def __repr__(self):
        return f"<ComRetryProxy {object.__getattribute__(self, '_obj')!r}>"

    def _call(self, fn, *args, **kwargs):
        args = tuple(self._unwrap(a) for a in args)
        kwargs = {k: self._unwrap(v) for k, v in kwargs.items()}
        ret = self._retry(lambda: fn(*args, **kwargs))
        if _is_com_object(ret):
            return _ComRetryProxy(ret)
        return ret


class AcadBackend(AcadLayoutMixin, AcadDwtMixin):
    """直接操控本地 AutoCAD 的绘图后端。"""

    def __init__(self, drawings_dir: Path):
        self.drawings_dir = Path(drawings_dir)
        self.drawings_dir.mkdir(parents=True, exist_ok=True)
        self.acad = None
        self.doc = None
        self.msp = None
        self.current_path: Optional[Path] = None
        self.active_layer: str = "WALL"
        self._own_document = False   # 该文档是否由本实例新建（用于测试结束清理）
        self._undo_mark_open = False
        self._template_manifests: Dict[str, TemplateManifest] = {}

    @staticmethod
    def _handle_str(handle_hex: str) -> str:
        """统一句柄显示：十六进制 → 十进制字符串。"""
        try:
            return str(int(handle_hex, 16))
        except (ValueError, TypeError):
            return str(handle_hex)

    @staticmethod
    def _align_text_entity(entity: Any, anchor: Sequence[float], alignment: str) -> None:
        """把清单中的可读对齐名映射为 AutoCAD 单行文字对齐常量。"""

        alignments = {
            "left": 0,
            "center": 1,
            "right": 2,
            "middle": 4,
            "top_left": 6,
            "top_center": 7,
            "top_right": 8,
            "middle_left": 9,
            "middle_center": 10,
            "middle_right": 11,
            "bottom_left": 12,
            "bottom_center": 13,
            "bottom_right": 14,
        }
        if alignment not in alignments:
            raise ValueError(f"不支持的文字对齐方式: {alignment}")
        entity.Alignment = alignments[alignment]
        if alignments[alignment] != 0:
            entity.TextAlignmentPoint = _pt(float(anchor[0]), float(anchor[1]))

    # ---------- 连接与文档 ----------

    def _acquire_autocad_lock(self, timeout: float = 5.0) -> None:
        """获取"AutoCAD 单实例"操作锁（操作级，用完即还）。

        锁被其他进程持有时**等待**（最多 timeout 秒，每 0.5 秒重试）而不是立刻报错，
        这样对方正在画图时，本方可短暂排队，画完自动接上；超时才提示。

        - 同进程内已持有（_LOCK_MUTEX 非空）则直接放行，避免嵌套调用重复抢锁；
        - 进程退出时 Windows 自动回收互斥锁，不会残留占用。
        """
        global _LOCK_MUTEX
        if _LOCK_MUTEX is not None:
            return   # 本进程已持有（同一操作内嵌套），放行
        deadline = time.time() + timeout
        while True:
            mutex = win32event.CreateMutex(None, False, "Local\\cad_mcp_single_instance")
            if win32api.GetLastError() != winerror.ERROR_ALREADY_EXISTS:
                _LOCK_MUTEX = mutex
                return
            win32api.CloseHandle(mutex)      # 只是探测到占用，关掉探测句柄
            if time.time() >= deadline:
                raise RuntimeError(
                    "AutoCAD 正被另一个 AI 客户端（Claude Code / Codex）正在操作，"
                    f"等待 {timeout:.0f} 秒仍未释放。请稍后重试；"
                         "若持续占用，可结束对方残留的 cad-mcp 进程。")
            time.sleep(0.5)

    def _release_autocad_lock(self) -> None:
        """释放 AutoCAD 操作锁（操作结束自动调用），让其他客户端能立即接上。"""
        global _LOCK_MUTEX
        if _LOCK_MUTEX is not None:
            try:
                win32api.CloseHandle(_LOCK_MUTEX)
            except Exception:
                pass
            _LOCK_MUTEX = None

    def _wait_acad_ready(self, acad) -> None:
        """等待 AutoCAD 真正空闲后再继续，避免启动未完成时触发 COM 异常。"""
        # 首次启动 AutoCAD 很慢（加载 .NET 组件、许可、配置），必须等它
        # "完全空闲"再继续，否则 .NET 组件未就绪时新建文档会触发
        # e0434352（.NET 未处理异常）崩溃。用 GetAcadState().IsQuiescent
        # 判断真正空闲，最多轮询 60 秒。
        ready = False
        for _ in range(30):
            _check_com_scan_deadline()
            try:
                state = acad.GetAcadState()
                if state.IsQuiescent:
                    _ = acad.Documents.Count
                    ready = True
                    break
            except TimeoutError:
                raise
            except Exception:
                pass
            _check_com_scan_deadline()
            time.sleep(2)
        _check_com_scan_deadline()
        if not ready:
            raise RuntimeError("实例未就绪")

    def _wait_operation_ready(self, timeout: float = 15.0) -> None:
        """在一次语义操作开始前等待 AutoCAD 空闲，避免连续工具调用撞上消息泵。"""
        if self.acad is None:
            return
        deadline = time.time() + timeout
        last_error: Optional[Exception] = None
        while True:
            try:
                pythoncom.PumpWaitingMessages()
            except Exception:
                pass
            try:
                if self.acad.GetAcadState().IsQuiescent:
                    return
            except Exception as exc:
                last_error = exc
            if time.time() >= deadline:
                break
            time.sleep(0.1)
        if last_error is not None:
            raise RuntimeError(
                f"AutoCAD 在 {timeout:.0f} 秒内无法确认空闲状态，请稍后重试"
            ) from last_error
        raise RuntimeError(f"AutoCAD 在 {timeout:.0f} 秒内未进入空闲状态，请稍后重试")

    def _begin_undo(self) -> bool:
        """为单个 MCP 语义操作建立一个 AutoCAD 撤销组。"""
        if self.doc is None or self._undo_mark_open:
            return False
        try:
            self.doc.StartUndoMark()
            self._undo_mark_open = True
            return True
        except Exception as exc:
            raise RuntimeError(
                "无法建立 AutoCAD 撤销组，已停止本次写入操作"
            ) from exc

    def _commit_undo(self, started: bool) -> None:
        if not started or not self._undo_mark_open or self.doc is None:
            return
        try:
            self.doc.EndUndoMark()
        finally:
            self._undo_mark_open = False

    def _rollback_undo(self, started: bool) -> Optional[bool]:
        """只撤销当前工具建立的撤销组；回滚失败不遮盖原始异常。"""
        if not started or self.doc is None:
            return None
        try:
            if self._undo_mark_open:
                self.doc.EndUndoMark()
            self._undo_mark_open = False
            self.doc.SendCommand("_.U ")
            self._wait_operation_ready(10.0)
            return True
        except Exception as exc:
            self._undo_mark_open = False
            record_error(exc, "acad_rollback_undo")
            return False

    @staticmethod
    def _yield_com() -> None:
        """长操作中主动让 Windows/AutoCAD 处理消息，降低 RPC_E_CALL_REJECTED。"""
        try:
            pythoncom.PumpWaitingMessages()
        except Exception:
            pass
        time.sleep(0.03)

    def _scan_checkpoint(self, index: int) -> None:
        _check_com_scan_deadline()
        if index > 0 and index % 25 == 0:
            self._yield_com()
            _check_com_scan_deadline()

    def _send_command_and_wait(self, command: str, timeout: float = 20.0, *, completion_check=None) -> None:
        """只发送一次 AutoCAD 命令，并等待命令队列确实执行完毕。"""
        # Native completion receipts require a single dispatch: a rejected or
        # disconnected SendCommand must not be replayed by the COM retry proxy.
        document = self.doc
        if completion_check is not None and isinstance(document, _ComRetryProxy):
            document = object.__getattribute__(document, "_obj")
        document.SendCommand(command)
        deadline = time.time() + timeout
        started_at = time.time()
        while time.time() < deadline:
            try:
                pythoncom.PumpWaitingMessages()
            except Exception:
                pass
            try:
                active = int(self.doc.GetVariable("CMDACTIVE"))
                names = str(self.doc.GetVariable("CMDNAMES") or "").strip()
                quiet = bool(self.acad.GetAcadState().IsQuiescent)
                # 给 SendCommand 一个短暂启动窗口，避免在命令尚未入队时误判完成。
                if (time.time() - started_at >= 0.15 and active == 0 and not names and quiet
                        and (completion_check is None or completion_check())):
                    return
            except Exception:
                pass
            time.sleep(0.05)
        raise RuntimeError(f"AutoCAD 命令在 {timeout:.0f} 秒内未执行完成")

    def _attach_existing_acad(self):
        """优先连接已经打开的 AutoCAD 实例；没有则返回 None。"""
        try:
            return win32com.client.GetActiveObject("AutoCAD.Application")
        except Exception:
            return None

    def _connect(self):
        """只复用已经运行的 AutoCAD；连接失败不启动或关闭任何实例。"""
        if self.acad is not None:
            try:
                _ = self.acad.Documents.Count   # 探活
                return self.acad
            except TimeoutError:
                raise
            except Exception:
                self.acad = None
                self.doc = None
                self.msp = None
        acad = self._attach_existing_acad()
        if acad is None:
            raise CadMcpError(
                "未找到可连接的已运行 AutoCAD 实例；未启动新实例",
                code="PRECONDITION_FAILED",
                hint="若 CAD 是否打开未知，先向用户确认；已打开时检查实例和权限，不自动冷启动",
            )
        try:
            self._wait_acad_ready(acad)
            try:
                acad.Visible = True         # 确保有窗口，避免无窗口自动化实例
            except Exception:
                pass
            self.acad = _ComRetryProxy(acad)   # 包装：所有后续 COM 调用自动重试
            return self.acad
        except TimeoutError:
            raise
        except Exception:
            self.acad = None
            self.doc = None
            self.msp = None
        raise RuntimeError("无法连接 AutoCAD（实例状态异常），请重启 AutoCAD 后重试")

    def _ensure_connected(self) -> None:
        """确保 CAD 连接与当前文档对象仍有效；失效则重置，让用户重新新建/打开。"""
        self._connect()
        if self.doc is not None:
            try:
                _ = self.msp.Count
            except TimeoutError:
                raise
            except Exception:
                self.doc = None
                self.msp = None

    @staticmethod
    def _document_path(doc) -> Optional[Path]:
        """尽力读取文档完整路径；文档仍在加载或尚未保存时返回 None。"""
        try:
            value = str(doc.FullName or "").strip()
            return Path(value).resolve() if value else None
        except Exception:
            return None

    def _find_open_document(self, acad, expected_path: Path):
        """从 Documents 集合寻找已打开的目标文件，避免重复 Open。"""
        expected = expected_path.resolve()
        try:
            count = int(acad.Documents.Count)
        except Exception:
            return None
        for index in range(count):
            try:
                candidate = acad.Documents.Item(index)
                if self._document_path(candidate) == expected:
                    return candidate
            except Exception:
                continue
        return None

    def _use_doc(self, doc, expected_path: Optional[Path] = None,
                 timeout: float = 20.0):
        """等待文档及 ModelSpace 就绪，成功后再原子地更新后端绑定。

        AutoCAD 的 Documents.Open 可能先返回文档对象，随后才完成数据库加载。
        此处会重试原返回对象，并在给定路径时从 ActiveDocument/Documents 集合
        找回同一文档；在验证成功前不改变 self.doc/self.msp。
        """
        primary = doc if isinstance(doc, _ComRetryProxy) else _ComRetryProxy(doc)
        expected = expected_path.resolve() if expected_path is not None else None
        deadline = time.time() + timeout
        last_error: Optional[Exception] = None
        while time.time() < deadline:
            candidates = [primary]
            if expected is not None and self.acad is not None:
                recovered = self._find_open_document(self.acad, expected)
                if recovered is not None:
                    candidates.append(recovered)
                try:
                    candidates.append(self.acad.ActiveDocument)
                except Exception:
                    pass
            for candidate in candidates:
                proxy = candidate if isinstance(candidate, _ComRetryProxy) else _ComRetryProxy(candidate)
                if expected is not None:
                    candidate_path = self._document_path(proxy)
                    if candidate_path is not None and candidate_path != expected:
                        continue
                try:
                    modelspace = proxy.ModelSpace
                    _ = modelspace.Count
                    self.doc = proxy
                    self.msp = modelspace
                    return proxy
                except Exception as exc:
                    last_error = exc
            try:
                pythoncom.PumpWaitingMessages()
            except Exception:
                pass
            time.sleep(0.1)
        target = f"（目标: {expected}）" if expected is not None else ""
        raise RuntimeError(
            f"AutoCAD 文档已打开但 ModelSpace 在 {timeout:.0f} 秒内未就绪{target}: "
            f"{last_error}") from last_error

    def _setup_chinese_font(self):
        """把 Standard 文字样式设为中文字体（gbenor.shx 正体 + gbcbig.shx 大字体），避免中文变'?'。"""
        try:
            style = self.doc.TextStyles.Item("Standard")
            style.fontFile = "gbenor.shx"
            style.bigFontFile = "gbcbig.shx"
        except Exception:
            pass

    def _ensure_chinese_textstyle(self) -> None:
        """提供独立 GB 样式，不覆盖模板中已经配置好的 Standard 样式。"""
        try:
            try:
                style = self.doc.TextStyles.Item("GB")
            except Exception:
                style = self.doc.TextStyles.Add("GB")
            style.FontFile = "gbenor.shx"
            style.BigFontFile = "gbcbig.shx"
        except Exception:
            pass

    def _safe_path(
        self,
        filename: str,
        *,
        default_suffix: str = ".dwg",
        allowed_suffixes: Sequence[str] = (".dwg", ".dxf"),
    ) -> Path:
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
        normalized_allowed = tuple(suffix.lower() for suffix in allowed_suffixes)
        if not candidate.suffix:
            candidate = candidate.with_suffix(default_suffix)
        elif candidate.suffix.lower() not in normalized_allowed:
            expected = "、".join(normalized_allowed)
            raise ValueError(f"文件扩展名非法，应为 {expected}: {filename}")
        if len(candidate.name.encode("utf-16-le")) // 2 > 255:
            raise ValueError(f"文件名过长: {filename}")
        root = self.drawings_dir.resolve()
        path = candidate.resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"文件名非法: {filename}")
        return path

    @staticmethod
    def _lock_paths(path: Path) -> Tuple[Path, Path]:
        """Return AutoCAD's sibling lock paths without deleting either file."""

        return path.with_suffix(".dwl"), path.with_suffix(".dwl2")

    def _assert_no_external_lock(self, path: Path) -> None:
        locks = [lock for lock in self._lock_paths(path) if lock.exists()]
        if locks:
            raise PermissionError(
                "检测到 AutoCAD 文档锁，无法证明它是残留文件；为避免损坏并发编辑，"
                "不会自动删除或绕过。请先在占用该文件的 AutoCAD 中关闭图纸，再重试。"
                f" 锁文件: {', '.join(str(lock) for lock in locks)}"
            )

    def _resolve_new_drawing_template(self, acad,
                                      template_path: Optional[str] = None) -> Tuple[Path, str]:
        """解析新建模板：显式路径优先，否则读取 AutoCAD 的 QNEW 默认模板。"""
        if template_path:
            raw = os.path.expandvars(template_path.strip())
            source = "explicit"
            template = Path(raw).expanduser()
            if not template.is_absolute():
                raise ValueError("template_path 必须是 .dwt 文件的完整绝对路径")
        else:
            try:
                raw = str(acad.Preferences.Files.QNewTemplateFile).strip()
            except Exception as exc:
                raise RuntimeError(
                    "无法读取 AutoCAD 的快速新建默认模板设置（QNewTemplateFile）") from exc
            if not raw:
                raise RuntimeError(
                    "AutoCAD 尚未设置快速新建默认模板。请在“选项→文件→样板设置→"
                    "快速新建的默认样板文件名”中配置，或向 cad_new_drawing 传入 template_path。")
            raw = os.path.expandvars(raw)
            source = "cad_qnew"
            template = Path(raw).expanduser()
            if not template.is_absolute():
                try:
                    template_dir = Path(str(acad.Preferences.Files.TemplateDwgPath))
                    template = template_dir / template
                except Exception as exc:
                    raise RuntimeError(
                        f"AutoCAD 返回了相对模板路径，但无法读取样板目录: {raw}") from exc

        template = template.resolve()
        if template.suffix.lower() != ".dwt":
            raise ValueError(f"新建模板必须是 .dwt 文件: {template}")
        if not template.is_file():
            raise FileNotFoundError(f"新建模板不存在或不可访问: {template}")
        return template, source

    def new_drawing(self, filename: str,
                    template_path: Optional[str] = None) -> Dict[str, Any]:
        """连接 AutoCAD，使用显式模板或 CAD 的 QNEW 默认模板新建文档。"""
        # 先校验文件名，避免文件名错误时已经在 AutoCAD 中创建空白文档。
        path = self._safe_path(filename, allowed_suffixes=(".dwg",))
        if path.exists():
            raise FileExistsError(f"目标文件已存在，未新建也未覆盖: {path}")
        self._assert_no_external_lock(path)
        acad = self._connect()
        self._wait_acad_ready(acad)
        template, template_source = self._resolve_new_drawing_template(acad, template_path)
        self._own_document = False
        doc = None
        add_error = None
        try:
            before_count = acad.Documents.Count
        except Exception:
            before_count = None

        # AutoCAD 偶尔会在 Documents.Add 已创建文档后才返回 COM 异常。
        # 只有确认文档数量增加，才允许通过 ActiveDocument 找回这次新建的文档。
        for attempt in range(3):
            try:
                new = acad.Documents.Add(str(template))
                _ = new.ModelSpace          # 验证返回的文档对象确实可用
                doc = new
                break
            except Exception as e:
                add_error = e
                created_after_error = False
                if before_count is not None:
                    for _ in range(5):
                        try:
                            if acad.Documents.Count > before_count:
                                created_after_error = True
                                self._wait_acad_ready(acad)
                                candidate = acad.ActiveDocument
                                _ = candidate.ModelSpace
                                doc = candidate
                                break
                        except Exception:
                            pass
                        time.sleep(1)
                # 已确认文档数量增加但无法重新获取对象时，不再重复 Add，避免多建空白文档。
                if created_after_error and doc is None:
                    break
                if doc is not None or attempt == 2:
                    break
                self._wait_acad_ready(acad)

        if doc is None:
            raise RuntimeError(
                f"AutoCAD 新建文档失败（文件名: {path.name}，模板: {template}）："
                f"{add_error}") from add_error
        self._own_document = True
        # 新建文档尚未 SaveAs 到目标路径，不能用 filename 校验 FullName。
        self._use_doc(doc)
        self._ensure_chinese_textstyle()
        self.current_path = path
        return {
            "message": "已在 AutoCAD 使用模板新建文档（DWG 模式）",
            "path": str(self.current_path),
            "mode": "acad",
            "template_path": str(template),
            "template_source": template_source,
        }

    def open_drawing(self, filename: str) -> str:
        """在 AutoCAD 中打开已有图纸文件（dwg 或 dxf）。"""
        path = resolve_open_drawing_path(
            filename,
            allowed_suffixes=(".dwg", ".dxf"),
            resolve_filename=self._safe_path,
        )
        acad = self._connect()
        doc = self._find_open_document(acad, path)
        opened_here = False
        if doc is None:
            self._assert_no_external_lock(path)
            doc = acad.Documents.Open(str(path))
            opened_here = True
        if bool(_safe_get(doc, "ReadOnly", False)):
            if opened_here:
                try:
                    doc.Close(False)
                except Exception:
                    pass
            raise PermissionError(
                f"AutoCAD 以只读方式打开了 {path.name}；未绑定为可写当前图纸")
        self._use_doc(doc, expected_path=path)
        self.current_path = path
        count = self.msp.Count
        return f"已在 AutoCAD 打开 {path.name}，模型空间共 {count} 个图元"

    def save(self) -> str:
        """保存当前文档为 DWG。

        新建文档（未关联路径）用 SaveAs 指定文件名；从文件打开的文档用 Save 覆盖
        （AutoCAD 对已打开的文档执行 SaveAs 到同一路径会报"保存文档时出错"）。
        """
        self._check_doc()
        original_path = self.current_path
        path = (original_path.with_suffix(".dwg")
                if original_path.suffix.lower() == ".dxf" else original_path)
        converting = path != original_path
        if converting and path.exists():
            raise FileExistsError(f"DWG 目标已存在，未覆盖: {path}")
        if converting:
            self._assert_no_external_lock(path)
        try:
            if self._own_document or converting:
                self.doc.SaveAs(str(path), SAVE_FORMAT_DWG)
            else:
                self.doc.Save()
        except Exception as exc:
            raise RuntimeError(f"保存失败: {path}: {exc}") from exc
        try:
            actual_path = Path(str(self.doc.FullName)).resolve()
        except Exception as exc:
            raise RuntimeError(f"保存后无法核实实际文档路径: {path}: {exc}") from exc
        if str(actual_path).casefold() != str(path.resolve()).casefold():
            raise RuntimeError(
                f"保存后文档路径不一致，预期 {path.resolve()}，实际 {actual_path}")
        self.current_path = path
        return f"已保存: {path}"

    def save_as(self, filename: str, overwrite: bool = False) -> Dict[str, Any]:
        """把当前文档另存到受控图纸目录，不允许静默覆盖。"""
        self._check_doc()
        path = self._safe_path(filename, allowed_suffixes=(".dwg",))
        if self.current_path and path.resolve() == self.current_path.resolve():
            raise ValueError("另存目标与当前图纸相同；请使用 cad_save_drawing")
        if path.exists() and not overwrite:
            raise FileExistsError(f"目标文件已存在，未覆盖: {path}")
        if self.acad is not None and self._find_open_document(self.acad, path) is not None:
            raise PermissionError(f"另存目标已在 AutoCAD 中打开，未覆盖: {path}")
        self._assert_no_external_lock(path)
        self.doc.SaveAs(str(path), SAVE_FORMAT_DWG)
        self.current_path = path
        return {"message": f"已另存为: {path}", "path": str(path), "mode": "acad"}

    def clone_template_drawing(
        self,
        source_path: str,
        output_filename: str,
        expected_sha256: Optional[str] = None,
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        """按字节复制外部 DWG 为工作副本，再打开副本；源文件始终只读。"""
        source = Path(os.path.expandvars(source_path)).expanduser()
        if not source.is_absolute() or source.suffix.lower() != ".dwg":
            raise ValueError("source_path 必须是现有 DWG 的完整绝对路径")
        source = source.resolve()
        if not source.is_file():
            raise FileNotFoundError(f"模板 DWG 不存在: {source}")
        output = self._safe_path(output_filename, allowed_suffixes=(".dwg",))
        if source == output.resolve():
            raise ValueError("模板源文件与工作副本目标不能是同一个文件")
        if output.exists() and not overwrite:
            raise FileExistsError(f"工作副本已存在，未覆盖: {output}")
        if self.acad is not None and self._find_open_document(self.acad, output) is not None:
            raise PermissionError(f"工作副本目标已在 AutoCAD 中打开，未覆盖: {output}")
        self._assert_no_external_lock(output)
        before_hash = sha256_file(source)
        if expected_sha256 is None:
            raise ValueError(
                "复制模板前必须提供 cad_inspect_template_drawing 返回的 source_sha256")
        expected = str(expected_sha256).lower()
        if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
            raise ValueError("expected_sha256 必须是 64 位十六进制 SHA-256")
        if before_hash.lower() != expected:
            raise ValueError(
                f"模板源文件 SHA-256 不匹配，未复制；期望 {expected}，实际 {before_hash}")
        backup_path: Optional[Path] = None
        opened_doc = None
        if output.exists():
            backup_path = output.with_name(
                f".{output.stem}.{uuid.uuid4().hex}.backup.dwg")
            copy_template_source(output, backup_path, overwrite=False)
        try:
            copy_template_source(source, output, overwrite=overwrite)
            after_hash = sha256_file(source)
            if before_hash != after_hash:
                raise RuntimeError("模板源文件在复制过程中发生变化，已停止打开工作副本")
            output_hash = sha256_file(output)
            if output_hash != before_hash:
                raise RuntimeError("模板工作副本与源文件字节不一致，已停止打开工作副本")
            acad = self._connect()
            if self._find_open_document(acad, output) is not None:
                raise PermissionError(f"工作副本目标已在 AutoCAD 中打开，未接管: {output}")
            opened_doc = acad.Documents.Open(str(output))
            self._use_doc(opened_doc, expected_path=output)
            self.current_path = output
            self._own_document = True
        except Exception as operation_error:
            if opened_doc is not None:
                try:
                    opened_doc.Close(False)
                except Exception:
                    pass
            rollback_error = None
            try:
                if backup_path is None:
                    output.unlink(missing_ok=True)
                else:
                    copy_template_source(backup_path, output, overwrite=True)
            except Exception as exc:
                rollback_error = exc
            if rollback_error is not None:
                raise RuntimeError(
                    f"创建工作副本失败，且恢复原目标失败: {rollback_error}") from operation_error
            raise
        finally:
            if backup_path is not None:
                try:
                    backup_path.unlink(missing_ok=True)
                except OSError:
                    pass
        return {
            "message": "已从外部 DWG 创建并打开工作副本",
            "source_path": str(source),
            "source_sha256": before_hash,
            "output_sha256": output_hash,
            "source_unchanged": True,
            "output_path": str(output),
            "mode": "acad",
        }

    def inspect_template_drawing(self, source_path: str) -> Dict[str, Any]:
        """只读打开外部 DWG，列出可复用块、属性、范围、布局和制图资源。"""
        source = Path(os.path.expandvars(source_path)).expanduser()
        if not source.is_absolute() or source.suffix.lower() != ".dwg":
            raise ValueError("source_path 必须是现有 DWG 的完整绝对路径")
        source = source.resolve()
        if not source.is_file():
            raise FileNotFoundError(f"模板 DWG 不存在: {source}")
        before_hash = sha256_file(source)
        acad = self._connect()
        previous = None
        try:
            previous = acad.ActiveDocument
        except Exception:
            pass
        template_doc = None
        opened_here = False
        for index in range(acad.Documents.Count):
            candidate = acad.Documents.Item(index)
            try:
                if Path(str(candidate.FullName)).resolve() == source:
                    template_doc = candidate
                    break
            except Exception:
                continue
        if template_doc is None:
            template_doc = acad.Documents.Open(str(source), True)
            opened_here = True
        try:
            blocks = []
            for block_index in range(template_doc.Blocks.Count):
                block = template_doc.Blocks.Item(block_index)
                name = str(block.Name)
                if name.startswith("*"):
                    continue
                attributes = []
                entity_types: Dict[str, int] = {}
                xmin = ymin = float("inf")
                xmax = ymax = float("-inf")
                has_bounds = False
                for entity_index in range(block.Count):
                    entity = block.Item(entity_index)
                    entity_name = str(getattr(entity, "EntityName", "UNKNOWN"))
                    entity_types[entity_name] = entity_types.get(entity_name, 0) + 1
                    if entity_name == "AcDbAttributeDefinition":
                        attributes.append({
                            "tag": str(getattr(entity, "TagString", "")),
                            "prompt": str(getattr(entity, "PromptString", "")),
                            "default": str(getattr(entity, "TextString", "")),
                        })
                    try:
                        low, high = bounding_box(entity)
                        xmin, ymin = min(xmin, float(low[0])), min(ymin, float(low[1]))
                        xmax, ymax = max(xmax, float(high[0])), max(ymax, float(high[1]))
                        has_bounds = True
                    except Exception:
                        pass
                blocks.append({
                    "name": name,
                    "entity_count": block.Count,
                    "entity_types": entity_types,
                    "attributes": attributes,
                    "bounds": ([xmin, ymin, xmax, ymax] if has_bounds else None),
                })
            layouts = [str(template_doc.Layouts.Item(index).Name)
                       for index in range(template_doc.Layouts.Count)]
            layers = [str(template_doc.Layers.Item(index).Name)
                      for index in range(template_doc.Layers.Count)]
            textstyles = [str(template_doc.TextStyles.Item(index).Name)
                          for index in range(template_doc.TextStyles.Count)]
            dimstyles = [str(template_doc.DimStyles.Item(index).Name)
                         for index in range(template_doc.DimStyles.Count)]
        finally:
            if opened_here:
                template_doc.Close(False)
            if previous is not None:
                try:
                    previous.Activate()
                except Exception:
                    pass
        after_hash = sha256_file(source)
        if before_hash != after_hash:
            raise RuntimeError("只读检查期间模板文件发生变化，结果已作废")
        return {
            "source_path": str(source),
            "source_sha256": before_hash,
            "source_unchanged": True,
            "blocks": blocks,
            "layouts": layouts,
            "layers": layers,
            "textstyles": textstyles,
            "dimstyles": dimstyles,
        }

    def load_template_manifest(self, manifest_path: str) -> Dict[str, Any]:
        """加载项目外部模板清单；清单不写入 MCP 源码。"""
        manifest = load_template_manifest(manifest_path)
        if manifest.template_id in self._template_manifests:
            self._template_manifests.pop(manifest.template_id)
        elif len(self._template_manifests) >= 64:
            self._template_manifests.pop(next(iter(self._template_manifests)))
        self._template_manifests[manifest.template_id] = manifest
        return {"template_id": manifest.template_id,
                "source_dwg": manifest.source_dwg,
                "sheet_ids": [sheet.id for sheet in manifest.sheets]}

    @_bounded_com_scan
    def get_info(self) -> str:
        """当前文档信息。"""
        if self.doc is None:
            return "当前未连接 AutoCAD"
        layers = []
        layer_collection = self.doc.Layers
        for i in range(int(layer_collection.Count)):
            self._scan_checkpoint(i)
            layers.append(layer_collection.Item(i).Name)
        counts: Dict[str, int] = {}
        for i in range(int(self.msp.Count)):
            self._scan_checkpoint(i)
            n = self.msp.Item(i).EntityName
            counts[n] = counts.get(n, 0) + 1
        fname = self.current_path.name if self.current_path else "当前 AutoCAD 文档"
        stat = ", ".join(f"{k}×{v}" for k, v in counts.items()) or "空"
        return (
            f"文件: {fname}\n"
            f"模式: 直接操控 AutoCAD（DWG）\n"
            f"图层({len(layers)}): {', '.join(layers)}\n"
            f"图元统计: {stat}"
        )

    def get_drawing_settings(self) -> Dict[str, Any]:
        """逐项读取关键系统变量，单个字段失败时不猜测。"""

        self._check_doc()
        raw: Dict[str, Any] = {}
        for variable in (
            "INSUNITS", "MEASUREMENT", "LUNITS", "LUPREC", "AUNITS",
            "AUPREC", "DIMASSOC", "MIRRTEXT", "LWDISPLAY", "PSTYLEMODE",
        ):
            try:
                raw[variable] = self.doc.GetVariable(variable)
            except Exception:
                pass
        try:
            current_layer = str(self.doc.ActiveLayer.Name)
        except Exception:
            current_layer = None
        try:
            current_layout = str(self.doc.ActiveLayout.Name)
        except Exception:
            current_layout = None
        return build_drawing_settings_payload(
            "acad", raw, current_layer, current_layout
        )

    @_bounded_com_scan
    def list_layers_detailed(self) -> Dict[str, Any]:
        """只读列出 AutoCAD 图层和数据库级实体引用数量。"""

        self._check_doc()
        counts: Dict[str, int] = {}
        blocks = self.doc.Blocks
        scanned = 0
        for block_index in range(int(blocks.Count)):
            self._scan_checkpoint(block_index)
            block = blocks.Item(block_index)
            for entity_index in range(int(block.Count)):
                self._scan_checkpoint(scanned)
                name = str(block.Item(entity_index).Layer).casefold()
                counts[name] = counts.get(name, 0) + 1
                scanned += 1

        try:
            current = str(self.doc.ActiveLayer.Name)
        except Exception as exc:
            _raise_scan_transport_error(exc)
            current = None
        unverified: List[str] = []
        layers: List[Dict[str, Any]] = []
        layer_collection = self.doc.Layers
        for index in range(int(layer_collection.Count)):
            self._scan_checkpoint(index)
            layer = layer_collection.Item(index)
            name = str(layer.Name)

            def read(field: str, fn):
                try:
                    return fn()
                except Exception as exc:
                    _raise_scan_transport_error(exc)
                    unverified.append(f"layers.{name}.{field}")
                    return None

            entity_count = counts.get(name.casefold(), 0)
            layers.append({
                "name": name,
                "color": read("color", lambda: int(layer.Color)),
                "linetype": read("linetype", lambda: str(layer.Linetype)),
                "lineweight": read("lineweight", lambda: int(layer.Lineweight)),
                "plot": read("plot", lambda: bool(layer.Plottable)),
                "description": read("description", lambda: str(layer.Description)),
                "locked": read("locked", lambda: bool(layer.Lock)),
                "frozen": read("frozen", lambda: bool(layer.Freeze)),
                "transparency": read(
                    "transparency",
                    lambda: int(str(layer.EntityTransparency)),
                ),
                "is_current": (
                    name.casefold() == current.casefold() if current is not None else None
                ),
                "entity_count": entity_count,
            })
        return {
            "mode": "acad",
            "layers": sorted(layers, key=lambda item: item["name"].casefold()),
            "unverified_fields": sorted(set(unverified)),
        }

    def list_textstyles(self) -> Dict[str, Any]:
        """只读列出 AutoCAD 文字样式；未暴露的注释性属性明确标记。"""

        self._check_doc()
        try:
            current = str(self.doc.ActiveTextStyle.Name)
        except Exception:
            current = None
        unverified: List[str] = []
        styles: List[Dict[str, Any]] = []
        for index in range(self.doc.TextStyles.Count):
            style = self.doc.TextStyles.Item(index)
            name = str(style.Name)

            def read(field: str, fn):
                try:
                    return fn()
                except Exception:
                    unverified.append(f"text_styles.{name}.{field}")
                    return None

            unverified.append(f"text_styles.{name}.annotative")
            styles.append({
                "name": name,
                "font_file": read("font_file", lambda: str(style.FontFile)),
                "big_font_file": read(
                    "big_font_file", lambda: str(style.BigFontFile)
                ),
                "model_height": read("model_height", lambda: float(style.Height)),
                "width_factor": read("width_factor", lambda: float(style.Width)),
                "oblique_angle": read(
                    "oblique_angle",
                    lambda: round(math.degrees(float(style.ObliqueAngle)), 12),
                ),
                "annotative": None,
                "is_current": (
                    name.casefold() == current.casefold() if current is not None else None
                ),
            })
        return {
            "mode": "acad",
            "textstyles": sorted(styles, key=lambda item: item["name"].casefold()),
            "unverified_fields": sorted(set(unverified)),
        }

    # ---------- 图层 ----------

    def _layer_exists(self, name: str) -> bool:
        for i in range(self.doc.Layers.Count):
            if self.doc.Layers.Item(i).Name == name:
                return True
        return False

    def _get_layer(self, name: str):
        """按名称取图层对象，不存在则报错。"""
        for i in range(self.doc.Layers.Count):
            lay = self.doc.Layers.Item(i)
            if lay.Name == name:
                return lay
        raise ValueError(f"图层 {name} 不存在")

    def _ensure_linetype(self, name: str) -> None:
        """确保线型已加载到当前文档（从 acad.lin 加载预定义线型）。"""
        try:
            self.doc.Linetypes.Load(name, "acad.lin")
        except Exception:
            pass   # 已加载或该线型不可用

    def create_layer(self, name: str, color: int = 7, linetype: str = "CONTINUOUS",
                     lineweight: int = 25, plot: bool = True,
                     description: str = "", locked: Optional[bool] = None,
                     transparency: Optional[int] = None) -> str:
        """创建图层（颜色、线型、线宽）；图层已存在则更新属性。"""
        self._check_doc()
        if self._layer_exists(name):
            lay = self._get_layer(name)
            created = False
        else:
            lay = self.doc.Layers.Add(name)
            created = True
        lay.color = color
        lt = linetype.upper()
        if lt != "CONTINUOUS":
            self._ensure_linetype(lt)
        lay.Linetype = lt if lt != "CONTINUOUS" else "Continuous"
        lay.Lineweight = int(lineweight)
        lay.Plottable = bool(plot)
        lay.Description = description
        if locked is not None:
            lay.Lock = bool(locked)
        if transparency is not None:
            raise UnsupportedOperationError(
                "AutoCAD ActiveX 的 Layer 对象不公开可读写透明度；未通过命令行猜写"
            )
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
        """只写入真正变化的图层属性；未指定或相同值不触发 COM setter。"""
        self._check_doc()
        lay = self._get_layer(name)
        if transparency is not None:
            raise UnsupportedOperationError(
                "AutoCAD ActiveX 的 Layer 对象不公开可读写透明度；未通过命令行猜写"
            )

        requested: Dict[str, Any] = {}
        current: Dict[str, Any] = {}
        if color is not None:
            requested["color"] = int(color)
            current["color"] = int(lay.color)
        if linetype is not None:
            requested["linetype"] = str(linetype).upper()
            current["linetype"] = str(lay.Linetype).upper()
        if lineweight is not None:
            requested["lineweight"] = int(lineweight)
            current["lineweight"] = int(lay.Lineweight)
        if plot is not None:
            requested["plot"] = bool(plot)
            current["plot"] = bool(lay.Plottable)
        if description is not None:
            requested["description"] = str(description)
            current["description"] = str(lay.Description)
        if locked is not None:
            requested["locked"] = bool(locked)
            current["locked"] = bool(lay.Lock)
        if frozen is not None:
            requested["frozen"] = bool(frozen)
            current["frozen"] = bool(lay.Freeze)

        changed = [field for field in requested if current[field] != requested[field]]
        unchanged = [field for field in requested if field not in changed]
        if "frozen" in changed:
            try:
                active_name = str(self.doc.ActiveLayer.Name)
            except Exception as exc:
                raise RuntimeError("无法核验当前图层，未执行冻结状态写入") from exc
            if active_name.casefold() == name.casefold():
                raise ConflictError(
                    f"当前图层 {name} 不能更改冻结状态；未执行任何图层属性写入",
                    details={
                        "layer": name,
                        "field": "frozen",
                        "before": current["frozen"],
                        "requested": requested["frozen"],
                    },
                )

        if not changed:
            fields = "、".join(unchanged) if unchanged else "无属性"
            return f"图层 {name} 已符合请求，未执行 COM 写入（未变化：{fields}）"

        if "linetype" in changed and requested["linetype"] != "CONTINUOUS":
            self._ensure_linetype(requested["linetype"])

        if "color" in changed:
            lay.color = requested["color"]
        if "linetype" in changed:
            lay.Linetype = (
                "Continuous"
                if requested["linetype"] == "CONTINUOUS"
                else requested["linetype"]
            )
        if "lineweight" in changed:
            lay.Lineweight = requested["lineweight"]
        if "plot" in changed:
            lay.Plottable = requested["plot"]
        if "description" in changed:
            lay.Description = requested["description"]
        if "locked" in changed:
            lay.Lock = requested["locked"]
        if "frozen" in changed:
            lay.Freeze = requested["frozen"]

        readback = {
            "color": lambda: int(lay.color),
            "linetype": lambda: str(lay.Linetype).upper(),
            "lineweight": lambda: int(lay.Lineweight),
            "plot": lambda: bool(lay.Plottable),
            "description": lambda: str(lay.Description),
            "locked": lambda: bool(lay.Lock),
            "frozen": lambda: bool(lay.Freeze),
        }
        mismatched = {}
        for field in changed:
            actual = readback[field]()
            if actual != requested[field]:
                mismatched[field] = {
                    "expected": requested[field],
                    "actual": actual,
                }
        if mismatched:
            raise RuntimeError(f"图层 {name} 属性写入后读回不一致: {mismatched}")

        changed_text = "、".join(changed)
        unchanged_text = "、".join(unchanged)
        suffix = f"；未变化：{unchanged_text}" if unchanged_text else ""
        return f"已更新图层 {name}：{changed_text}{suffix}"

    def set_active_layer(self, name: str) -> str:
        self._check_doc()
        if not self._layer_exists(name):
            lay = self.doc.Layers.Add(name)
            lay.color = 7
        self.doc.ActiveLayer = self.doc.Layers.Item(name)
        self.active_layer = name
        return f"当前图层已切换为 {name}"

    # ---------- 制图环境 ----------

    def create_or_update_textstyle(self, name: str, model_height: float,
                                   font_file: str = "gbenor.shx",
                                   big_font_file: str = "gbcbig.shx",
                                   width_factor: float = 1.0,
                                   set_current: bool = True) -> Dict[str, Any]:
        """创建或更新文字样式；高度 0 表示可变字高且不改 TEXTSIZE。"""
        self._check_doc()
        height = float(model_height)
        if not math.isfinite(height) or height < 0:
            raise ValueError("model_height 必须是大于等于 0 的有限数值")
        try:
            style = self.doc.TextStyles.Item(name)
            created = False
        except Exception:
            style = self.doc.TextStyles.Add(name)
            created = True
        style.FontFile = font_file
        if big_font_file:
            style.BigFontFile = big_font_file
        style.Height = height
        style.Width = float(width_factor)
        textsize_action = "not_current"
        if set_current:
            self.doc.ActiveTextStyle = style
            if height == 0:
                # 0 是文字样式的可变字高标记，不是合法的默认新文字高度。
                # 保留图纸现有 TEXTSIZE，避免把两个独立概念错误绑定。
                textsize_action = "preserved"
            else:
                current_textsize = float(self.doc.GetVariable("TEXTSIZE"))
                if math.isclose(
                    current_textsize, height, rel_tol=1e-9, abs_tol=1e-9
                ):
                    textsize_action = "unchanged"
                else:
                    self.doc.SetVariable("TEXTSIZE", height)
                    actual_textsize = float(self.doc.GetVariable("TEXTSIZE"))
                    if not math.isclose(
                        actual_textsize, height, rel_tol=1e-9, abs_tol=1e-9
                    ):
                        raise RuntimeError(
                            "TEXTSIZE 写入后读回不一致: "
                            f"expected={height}, actual={actual_textsize}"
                        )
                    textsize_action = "updated"
        self.doc.Regen(1)
        actual_height = float(style.Height)
        if not math.isclose(actual_height, height, rel_tol=1e-9, abs_tol=1e-9):
            raise RuntimeError(
                f"文字样式 {name} 高度写入后读回不一致: "
                f"expected={height}, actual={actual_height}"
            )
        if set_current and str(self.doc.ActiveTextStyle.Name).casefold() != name.casefold():
            raise RuntimeError(f"文字样式 {name} 未能设为当前样式")
        return {
            "name": name,
            "created": created,
            "model_height": actual_height,
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
        """创建或更新非注释性 DIMSTYLE，分别配置外观比例和测量值换算。"""
        self._check_doc()
        if decimal_separator not in {".", ","}:
            raise ValueError("小数分隔符只能是单字符 . 或 ,")
        previous = self.doc.ActiveDimStyle
        try:
            style = self.doc.DimStyles.Item(name)
            created = False
        except Exception:
            style = self.doc.DimStyles.Add(name)
            created = True
        self.doc.SetVariable("DIMSCALE", float(overall_scale))
        self.doc.SetVariable("DIMTXT", float(text_height))
        self.doc.SetVariable("DIMASZ", float(arrow_size))
        self.doc.SetVariable("DIMTSZ", float(oblique_stroke_size))
        self.doc.SetVariable("DIMLFAC", float(linear_scale_factor))
        self.doc.SetVariable("DIMGAP", float(text_gap))
        self.doc.SetVariable("DIMDEC", int(precision))
        self.doc.SetVariable("DIMTAD", 1 if text_above else 0)
        try:
            dimzin = int(self.doc.GetVariable("DIMZIN")) & ~12
        except Exception:
            dimzin = 0
        self.doc.SetVariable(
            "DIMZIN",
            dimzin | (4 if suppress_leading_zeros else 0)
            | (8 if suppress_trailing_zeros else 0),
        )
        self.doc.SetVariable("DIMDSEP", decimal_separator)
        actual_decimal_separator = self.doc.GetVariable("DIMDSEP")
        if not isinstance(actual_decimal_separator, str):
            raise RuntimeError(
                "AutoCAD DIMDSEP 写入后读回类型不正确: "
                f"expected=str, actual={type(actual_decimal_separator).__name__}"
            )
        if actual_decimal_separator != decimal_separator:
            raise RuntimeError(
                "AutoCAD DIMDSEP 写入后读回不一致: "
                f"expected={decimal_separator!r}, actual={actual_decimal_separator!r}"
            )
        self.doc.SetVariable("DIMEXO", float(extension_line_offset))
        self.doc.SetVariable("DIMEXE", float(extension_line_extension))
        self.doc.SetVariable("DIMDLI", float(baseline_spacing))
        self.doc.SetVariable("DIMCLRD", int(dimension_line_color))
        self.doc.SetVariable("DIMCLRE", int(extension_line_color))
        self.doc.SetVariable("DIMCLRT", int(text_color))
        self.doc.SetVariable("DIMLWD", int(dimension_line_lineweight))
        self.doc.SetVariable("DIMLWE", int(extension_line_lineweight))
        self.doc.SetVariable("DIMASSOC", 2)
        if text_style:
            try:
                self.doc.TextStyles.Item(text_style)
            except Exception:
                raise ValueError(f"文字样式 {text_style} 不存在，请先创建文字样式")
            self.doc.SetVariable("DIMTXSTY", text_style)
        style.CopyFrom(self.doc)
        self.doc.ActiveDimStyle = style if set_current else previous
        self.doc.Regen(1)
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
            "decimal_separator": actual_decimal_separator,
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
        """按调用方给定值设置全局/当前实体/模型空间/图纸空间线型比例。"""
        self._check_doc()
        self.doc.SetVariable("LTSCALE", float(ltscale))
        self.doc.SetVariable("CELTSCALE", float(celtscale))
        self.doc.SetVariable("MSLTSCALE", int(msltscale))
        self.doc.SetVariable("PSLTSCALE", int(psltscale))
        if regen:
            self.doc.Regen(1)
        return {
            "ltscale": float(ltscale),
            "celtscale": float(celtscale),
            "msltscale": int(msltscale),
            "psltscale": int(psltscale),
            "regenerated": bool(regen),
        }

    def get_drafting_environment(self) -> Dict[str, Any]:
        """读取当前 DIMSTYLE、TextStyle 与线型比例变量。"""
        self._check_doc()
        oblique_stroke_size = float(self.doc.GetVariable("DIMTSZ"))
        linear_scale_factor = float(self.doc.GetVariable("DIMLFAC"))
        return {
            "mode": "acad",
            "textstyle": {
                "name": self.doc.ActiveTextStyle.Name,
                "model_height": float(self.doc.ActiveTextStyle.Height),
                "textsize": float(self.doc.GetVariable("TEXTSIZE")),
            },
            "dimstyle": {
                "name": self.doc.ActiveDimStyle.Name,
                "overall_scale": float(self.doc.GetVariable("DIMSCALE")),
                "text_height": float(self.doc.GetVariable("DIMTXT")),
                "arrow_size": float(self.doc.GetVariable("DIMASZ")),
                "oblique_stroke_size": oblique_stroke_size,
                "uses_oblique_stroke": bool(oblique_stroke_size > 0),
                "linear_scale_factor": linear_scale_factor,
                "text_gap": float(self.doc.GetVariable("DIMGAP")),
                "precision": int(self.doc.GetVariable("DIMDEC")),
                "text_above": bool(self.doc.GetVariable("DIMTAD")),
                "text_style": str(self.doc.GetVariable("DIMTXSTY")),
            },
            "linetype": {
                "ltscale": float(self.doc.GetVariable("LTSCALE")),
                "celtscale": float(self.doc.GetVariable("CELTSCALE")),
                "msltscale": int(self.doc.GetVariable("MSLTSCALE")),
                "psltscale": int(self.doc.GetVariable("PSLTSCALE")),
            },
        }

    # ---------- 绘图 ----------

    def _layer_name(self, layer: Optional[str]) -> str:
        return layer or self.active_layer

    def _apply_layer(self, layer: Optional[str]):
        """确保目标图层存在，并设为当前图层。"""
        name = self._layer_name(layer)
        if not self._layer_exists(name):
            self.doc.Layers.Add(name)
        self.doc.ActiveLayer = self.doc.Layers.Item(name)

    def draw_line(self, x1: float, y1: float, x2: float, y2: float,
                  layer: Optional[str] = None) -> Dict[str, Any]:
        self._check_doc()
        self._apply_layer(layer)
        e = self.msp.AddLine(_pt(x1, y1), _pt(x2, y2))
        return {"type": "LINE", "handle": self._handle_str(e.Handle), "layer": e.Layer}

    def draw_rectangle(self, x1: float, y1: float, x2: float, y2: float,
                       layer: Optional[str] = None) -> Dict[str, Any]:
        self._check_doc()
        self._apply_layer(layer)
        pts = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
        flat = [v for p in pts for v in p]
        e = self.msp.AddLightWeightPolyline(_pt2(flat))
        e.Closed = True
        return {"type": "LWPOLYLINE", "handle": self._handle_str(e.Handle), "layer": e.Layer}

    def draw_circle(self, cx: float, cy: float, radius: float,
                    layer: Optional[str] = None) -> Dict[str, Any]:
        self._check_doc()
        self._apply_layer(layer)
        e = self.msp.AddCircle(_pt(cx, cy), radius)
        return {"type": "CIRCLE", "handle": self._handle_str(e.Handle),
                "layer": e.Layer, "radius": radius}

    def draw_arc(self, cx: float, cy: float, radius: float,
                 start_angle: float, end_angle: float,
                 layer: Optional[str] = None) -> Dict[str, Any]:
        """画圆弧（圆心 + 半径 + 起止角，度；COM 内部转弧度）。"""
        self._check_doc()
        self._apply_layer(layer)
        e = self.msp.AddArc(_pt(cx, cy), radius,
                            math.radians(start_angle), math.radians(end_angle))
        return {"type": "ARC", "handle": self._handle_str(e.Handle),
                "layer": e.Layer, "radius": radius,
                "start_angle": start_angle, "end_angle": end_angle}

    def draw_polyline(self, points: Sequence[Point], close: bool = False,
                      layer: Optional[str] = None) -> Dict[str, Any]:
        """画多段线（点列表，可闭合）。"""
        self._check_doc()
        self._apply_layer(layer)
        pts = list(points)
        flat = [v for p in pts for v in p]
        e = self.msp.AddLightWeightPolyline(_pt2(flat))
        e.Closed = bool(close)
        return {"type": "LWPOLYLINE", "handle": self._handle_str(e.Handle),
                "layer": e.Layer, "close": close, "points_count": len(points)}

    def draw_mline(self, points: Sequence[Point],
                   style_name: Optional[str] = None,
                   scale: Optional[float] = None,
                   justification: Optional[str] = None,
                   layer: Optional[str] = None) -> Dict[str, Any]:
        """调用 AutoCAD 原生 AddMLine 绘制开放 MLINE。"""
        self._check_doc()
        vertices = [(float(x), float(y)) for x, y in points]
        if len(vertices) < 2:
            raise ValueError("MLINE 至少需要两个顶点")
        if all(math.hypot(x - vertices[0][0], y - vertices[0][1]) <= 1e-9
               for x, y in vertices[1:]):
            raise ValueError("MLINE 不能由重合点构成")
        self._apply_layer(layer)

        old_style = str(self.doc.GetVariable("CMLSTYLE"))
        actual_style = style_name or old_style
        if style_name:
            try:
                self.doc.SetVariable("CMLSTYLE", actual_style)
            except Exception as exc:
                raise ValueError(
                    f"多线样式不存在或不能设为当前样式: {actual_style}；"
                    "请先在模板或 CAD 中建立该样式") from exc
        try:
            flat = [coordinate for x, y in vertices for coordinate in (x, y, 0.0)]
            e = self.msp.AddMLine(_pt3(flat))
        finally:
            if style_name and old_style != actual_style:
                self.doc.SetVariable("CMLSTYLE", old_style)

        if scale is not None:
            if scale <= 0:
                e.Delete()
                raise ValueError("MLINE 比例必须大于 0")
            e.MLineScale = float(scale)
        just_map = {"top": 0, "zero": 1, "bottom": 2}
        if justification is not None:
            try:
                e.Justification = just_map[justification.lower()]
            except KeyError as exc:
                e.Delete()
                raise ValueError("MLINE 对正方式仅支持 top / zero / bottom") from exc
        return {
            "type": "MLINE",
            "handle": self._handle_str(e.Handle),
            "layer": str(e.Layer),
            "style_name": str(e.StyleName),
            "scale": float(e.MLineScale),
            "justification": int(e.Justification),
            "points_count": len(vertices),
        }

    def _polyline_part(self, entity) -> Tuple[List[Tuple[float, float, float]], bool]:
        """把 COM 曲线转换为统一的 (x, y, bulge) 顶点序列。"""
        kind = str(entity.EntityName)
        if kind == "AcDbLine":
            start = _pt2_coord(entity.StartPoint)
            end = _pt2_coord(entity.EndPoint)
            return [(start[0], start[1], 0.0), (end[0], end[1], 0.0)], False
        if kind == "AcDbArc":
            start = _pt2_coord(entity.StartPoint)
            end = _pt2_coord(entity.EndPoint)
            span = (float(entity.EndAngle) - float(entity.StartAngle)) % (2.0 * math.pi)
            if span <= 1e-12:
                raise ValueError("零圆心角 ARC 不能合并")
            bulge = math.tan(span / 4.0)
            return [(start[0], start[1], bulge), (end[0], end[1], 0.0)], False
        if kind == "AcDbPolyline":
            coords = list(entity.Coordinates)
            vertices = []
            for index in range(len(coords) // 2):
                vertices.append((float(coords[index * 2]), float(coords[index * 2 + 1]),
                                 float(entity.GetBulge(index))))
            return vertices, bool(entity.Closed)
        if kind == "AcDb2dPolyline":
            coords = list(entity.Coordinates)
            if len(coords) % 3 != 0:
                raise ValueError("无法读取 2D POLYLINE 顶点坐标")
            vertices = []
            for index in range(len(coords) // 3):
                vertices.append((float(coords[index * 3]), float(coords[index * 3 + 1]),
                                 float(entity.GetBulge(index))))
            return vertices, bool(entity.Closed)
        raise ValueError(
            f"图元类型 {kind} 不支持合并；仅支持 LINE / ARC / LWPOLYLINE / 2D POLYLINE")

    def join_polyline(self, handles: List[str], close: bool = False,
                      tolerance: float = 1e-6,
                      layer: Optional[str] = None) -> Dict[str, Any]:
        """把端点相接的线、圆弧和二维多段线合并为一条 LWPOLYLINE。"""
        self._check_doc()
        entities = []
        parts = []
        source_layer = None
        for handle in handles:
            entity = self._find(handle)
            if entity is None:
                raise ValueError(f"未找到句柄为 {handle} 的图元")
            part, closed = self._polyline_part(entity)
            if closed:
                if len(handles) != 1 or not close:
                    raise ValueError("闭合多段线只能单独输入，并且 close 必须为 true")
                part = list(part) + [(part[0][0], part[0][1], 0.0)]
            entities.append(entity)
            parts.append(part)
            source_layer = source_layer or str(entity.Layer)

        vertices, is_closed = join_polyline_parts(parts, close=close, tolerance=tolerance)
        target_layer = layer or source_layer or self.active_layer
        self._apply_layer(target_layer)
        flat = [coordinate for x, y, _ in vertices for coordinate in (x, y)]
        result = self.msp.AddLightWeightPolyline(_pt2(flat))
        for index, (_, _, bulge) in enumerate(vertices):
            if abs(bulge) > 1e-12:
                result.SetBulge(index, float(bulge))
        result.Closed = bool(is_closed)
        try:
            for entity in entities:
                entity.Delete()
        except Exception:
            result.Delete()
            raise
        return {
            "type": "LWPOLYLINE",
            "handle": self._handle_str(result.Handle),
            "layer": str(result.Layer),
            "closed": bool(result.Closed),
            "sources_count": len(entities),
            "points_count": len(vertices),
        }

    def _entity_summary(self, entity) -> Dict[str, Any]:
        row = {
            "type": str(entity.EntityName),
            "handle": self._handle_str(entity.Handle),
            "layer": str(entity.Layer),
        }
        if str(entity.EntityName) == "AcDbLine":
            start = _pt2_coord(entity.StartPoint)
            end = _pt2_coord(entity.EndPoint)
            row["start"] = [start[0], start[1]]
            row["end"] = [end[0], end[1]]
        return row

    def explode(self, handle: str) -> Dict[str, Any]:
        """分解复合图元；MLINE 使用一次 EXPLODE 命令，其余优先走直接 COM。"""
        self._check_doc()
        entity = self._find(handle)
        if entity is None:
            raise ValueError(f"未找到句柄为 {handle} 的图元")
        source_type = str(entity.EntityName)

        if source_type == "AcDbMline":
            before_count = int(self.msp.Count)
            hex_handle = str(entity.Handle)
            self._send_command_and_wait(f'_.EXPLODE\n(handent "{hex_handle}")\n\n')
            if self._find(handle) is not None:
                raise RuntimeError("EXPLODE 命令完成后 MLINE 源图元仍然存在，分解未生效")
            after_count = int(self.msp.Count)
            created_count = after_count - before_count + 1
            if created_count <= 0:
                raise RuntimeError("EXPLODE 命令未生成可核验的新图元")
            start_index = max(0, after_count - created_count)
            created = [self.msp.Item(index) for index in range(start_index, after_count)]
        else:
            explode_method = getattr(entity, "Explode", None)
            if not callable(explode_method):
                raise ValueError(f"图元类型 {source_type} 不支持分解")
            result = explode_method()
            created = list(result) if result is not None else []
            if not created:
                raise RuntimeError(f"{source_type} 分解后没有生成图元")
            entity.Delete()

        return {
            "source_type": source_type,
            "source_handle": handle,
            "deleted_source": True,
            "entities": [self._entity_summary(item) for item in created],
        }

    def draw_text(self, text: str, x: float, y: float, height: Optional[float] = None,
                  rotation: float = 0, layer: Optional[str] = None) -> Dict[str, Any]:
        """写文字标注；height 留空时继承当前文字样式/当前文字高度。"""
        self._check_doc()
        self._apply_layer(layer)
        if height is None:
            height = self._current_text_height()
        e = self.msp.AddText(text, _pt(x, y), height)
        e.Rotation = math.radians(rotation)
        return {"type": "TEXT", "handle": self._handle_str(e.Handle),
                "layer": e.Layer, "text": text, "height": height,
                "rotation": rotation}

    def draw_mtext(self, text: str, x: float, y: float, width: float,
                   height: Optional[float] = None, rotation: float = 0,
                   layer: Optional[str] = None) -> Dict[str, Any]:
        """创建原生 AcDbMText；不会退化为普通 TEXT。"""
        self._check_doc()
        if width <= 0:
            raise ValueError("MTEXT 宽度必须大于 0")
        self._apply_layer(layer)
        if height is None:
            height = self._current_text_height()
        e = self.msp.AddMText(_pt(x, y), float(width), text)
        e.Height = float(height)
        e.Rotation = math.radians(rotation)
        return {"type": "MTEXT", "handle": self._handle_str(e.Handle),
                "layer": e.Layer, "text": text, "height": height,
                "width": width, "rotation": rotation}

    @_bounded_com_scan
    def fillet(self, *, handles: List[str], radius: float, mode: str = "pair",
               pick_points=None, trim: bool = True, dry_run: bool = True) -> Dict[str, Any]:
        """原生 FILLET；预检后自建撤销组，写后核验，恢复用户设置。"""
        from cad_mcp.drawing.fillet import fillet_acad
        return fillet_acad(self, handles=handles, radius=radius, mode=mode,
                           pick_points=pick_points, trim=trim, dry_run=dry_run)

    def trim(self, subject_handle: str, cutting_handles: List[str],
             pick_x: float, pick_y: float) -> Dict[str, Any]:
        """在离拾取点最近的切割交点处，把被修剪图元截断，保留拾取点一侧。

        被修剪图元支持 AcDbLine / AcDbPolyline；切割边界支持同样两种
        （多段线按每条边参与切割）。先 cad_list_entities 拿句柄再调用。
        """
        self._check_doc()
        subj = self._find(subject_handle)
        if subj is None:
            raise ValueError(f"未找到句柄为 {subject_handle} 的图元")
        ename = subj.EntityName
        if ename not in ("AcDbLine", "AcDbPolyline"):
            raise ValueError(f"被修剪图元仅支持 LINE / LWPOLYLINE，当前为 {ename}")

        # 1. 把切割图元展开为线段集合
        cuts = []
        for ch in cutting_handles:
            e = self._find(ch)
            if e is None:
                raise ValueError(f"未找到切割边界句柄 {ch}")
            en = e.EntityName
            if en == "AcDbLine":
                cuts.append((_pt2_coord(e.StartPoint), _pt2_coord(e.EndPoint)))
            elif en == "AcDbPolyline":
                if _polyline_has_bulges(e):
                    raise ValueError(
                        f"切割边界 {ch} 含圆弧段；当前修剪仅支持直线段，未修改图形")
                pts, closed = _polyline_pts(e)
                m = len(pts)
                step = m if closed else m - 1
                for k in range(step):
                    cuts.append((pts[k % m], pts[(k + 1) % m]))
            else:
                raise ValueError(f"切割边界仅支持 LINE / LWPOLYLINE，句柄 {ch} 为 {en}")

        pick = (pick_x, pick_y)

        if ename == "AcDbLine":
            points = [_pt2_coord(subj.StartPoint), _pt2_coord(subj.EndPoint)]
            new_pts, _, cut_points = trim_polyline_detailed(points, False, cuts, pick)
            if new_pts is None:
                return {"type": "LINE", "handle": subject_handle,
                        "message": "未找到可修剪的切割交点"}
            subj.StartPoint = _pt(*new_pts[0])
            subj.EndPoint = _pt(*new_pts[1])
            return {"type": "LINE", "handle": subject_handle,
                    "cut": cut_points[0], "cut_points": cut_points,
                    "remaining_points": 2}
        else:
            if _polyline_has_bulges(subj):
                raise ValueError(
                    "被修剪多段线含圆弧段；当前修剪不能安全保留 bulge，未修改图形")
            pts, closed = _polyline_pts(subj)
            new_pts, new_closed, cut_points = trim_polyline_detailed(
                pts, closed, cuts, pick)
            if new_pts is None:
                return {"type": "LWPOLYLINE", "handle": subject_handle,
                        "message": "未找到可修剪的切割交点"}
            flat = [v for p in new_pts for v in p]
            subj.Coordinates = _pt2(flat)
            subj.Closed = new_closed
            return {"type": "LWPOLYLINE", "handle": subject_handle,
                    "cut": cut_points[0], "cut_points": cut_points,
                    "remaining_points": len(new_pts)}

    def draw_dimension(self, p1x: float, p1y: float, p2x: float, p2y: float,
                       offset: float = 500, direction: str = "horizontal",
                       layer: str = "DIM") -> Dict[str, Any]:
        """兼容性尺寸标注：根据 offset 推算尺寸线位置。"""
        self._check_doc()
        self._apply_layer(layer)
        if direction == "aligned":
            dx, dy = p2x - p1x, p2y - p1y
            length = math.hypot(dx, dy)
            if length == 0:
                raise ValueError("被标注两点重合，无法对齐标注")
            nx, ny = dy / length, -dx / length          # 连线右侧法线
            mid_x, mid_y = (p1x + p2x) / 2, (p1y + p2y) / 2
            dim = self.msp.AddDimAligned(_pt(p1x, p1y), _pt(p2x, p2y),
                                         _pt(mid_x + nx * offset, mid_y + ny * offset))
            value = length
        elif direction == "vertical":
            base_x = max(p1x, p2x) + offset
            mid_y = (p1y + p2y) / 2
            dim = self.msp.AddDimRotated(_pt(p1x, p1y), _pt(p2x, p2y),
                                         _pt(base_x, mid_y), math.radians(90))
            value = abs(p2y - p1y)
        else:
            base_y = min(p1y, p2y) - offset
            dim = self.msp.AddDimRotated(_pt(p1x, p1y), _pt(p2x, p2y),
                                         _pt((p1x + p2x) / 2, base_y), 0)
            value = abs(p2x - p1x)
        return {"type": "DIMENSION", "handle": self._handle_str(dim.Handle),
                "layer": dim.Layer, "value": round(float(dim.Measurement), 1),
                "direction": direction, "dimstyle": dim.StyleName}

    def draw_dimension_at(self, p1x: float, p1y: float, p2x: float, p2y: float,
                          line_x: float, line_y: float,
                          direction: str = "horizontal",
                          layer: str = "DIM") -> Dict[str, Any]:
        """在明确给定的尺寸线位置画标注，不替调用方决定放置侧。"""
        self._check_doc()
        self._apply_layer(layer)
        if p1x == p2x and p1y == p2y:
            raise ValueError("被标注两点重合，无法标注")
        if direction == "aligned":
            dim = self.msp.AddDimAligned(
                _pt(p1x, p1y), _pt(p2x, p2y), _pt(line_x, line_y))
            value = math.hypot(p2x - p1x, p2y - p1y)
        elif direction == "vertical":
            dim = self.msp.AddDimRotated(
                _pt(p1x, p1y), _pt(p2x, p2y), _pt(line_x, line_y), math.radians(90))
            value = abs(p2y - p1y)
        else:
            dim = self.msp.AddDimRotated(
                _pt(p1x, p1y), _pt(p2x, p2y), _pt(line_x, line_y), 0)
            value = abs(p2x - p1x)
        return {
            "type": "DIMENSION",
            "handle": self._handle_str(dim.Handle),
            "layer": dim.Layer,
            "value": round(float(value), 1),
            "direction": direction,
            "dimension_line_point": (line_x, line_y),
            "dimstyle": dim.StyleName,
        }

    def draw_angular_dimension(self, vertex_x: float, vertex_y: float,
                               p1x: float, p1y: float, p2x: float, p2y: float,
                               loc_x: float, loc_y: float,
                               layer: str = "DIM") -> Dict[str, Any]:
        """角度标注：角顶点 vertex 到 p1、p2 两条线的夹角，loc 为标注文字位置。"""
        self._check_doc()
        self._apply_layer(layer)
        dim = self.msp.AddDimAngular(_pt(vertex_x, vertex_y), _pt(p1x, p1y),
                                     _pt(p2x, p2y), _pt(loc_x, loc_y))
        return {"type": "DIMENSION", "handle": self._handle_str(dim.Handle),
                "layer": dim.Layer, "kind": "angular", "dimstyle": dim.StyleName,
                "vertex": (vertex_x, vertex_y)}

    def draw_radius_dimension(self, cx: float, cy: float,
                              edge_x: float, edge_y: float,
                              layer: str = "DIM") -> Dict[str, Any]:
        """半径标注：圆心 (cx, cy) 到圆上一点 (edge_x, edge_y)。"""
        self._check_doc()
        self._apply_layer(layer)
        radius = math.hypot(edge_x - cx, edge_y - cy)
        if radius == 0:
            raise ValueError("圆心与边上一点重合，无法半径标注")
        dim = self.msp.AddDimRadial(_pt(cx, cy), _pt(edge_x, edge_y), radius * 0.1)
        return {"type": "DIMENSION", "handle": self._handle_str(dim.Handle),
                "layer": dim.Layer, "kind": "radius", "radius": round(radius, 1),
                "dimstyle": dim.StyleName}

    def draw_diameter_dimension(self, cx: float, cy: float,
                                edge_x: float, edge_y: float,
                                layer: str = "DIM",
                                text_x: Optional[float] = None,
                                text_y: Optional[float] = None) -> Dict[str, Any]:
        """通过两个对径点创建原生直径尺寸，使用当前标注样式。"""
        opposite, diameter = validate_diameter_dimension(
            cx, cy, edge_x, edge_y, text_x, text_y)
        self._check_doc()
        self._apply_layer(layer)
        dim = self.msp.AddDimDiametric(
            _pt(edge_x, edge_y), _pt(*opposite), diameter * 0.05)
        if text_x is not None:
            dim.TextPosition = _pt(text_x, text_y)
            dim.Update()
        return {"type": "DIMENSION", "handle": self._handle_str(dim.Handle),
                "layer": dim.Layer, "kind": "diameter", "diameter": diameter,
                "dimstyle": dim.StyleName}

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
            return fill_acad_region(
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
        """Create a native SOLID hatch and verify it through ActiveX readback."""

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
        boundary = None
        normalized: Optional[List[Point]] = None
        boundary_area: Optional[float] = None
        boundary_kind: str
        if points is not None:
            normalized, boundary_area = validate_closed_polygon(points)
            boundary_kind = "explicit_polygon"
        else:
            boundary = self._find(str(boundary_handle))
            if boundary is None:
                raise ValueError(f"未找到边界句柄: {boundary_handle}")
            boundary_kind = str(_safe_get(boundary, "EntityName", ""))
            if boundary_kind in {"AcDbPolyline", "AcDb2dPolyline", "AcDb3dPolyline"}:
                if not bool(_safe_get(boundary, "Closed", False)):
                    raise ValueError("指定多段线边界未闭合")
                if boundary_kind in {"AcDbPolyline", "AcDb2dPolyline"}:
                    validate_source(acad_source(boundary, str(boundary_handle), polyline_ocs=True))
            elif boundary_kind == "AcDbEllipse":
                start = float(boundary.StartParameter)
                end = float(boundary.EndParameter)
                if not math.isclose(abs(end - start), math.tau, rel_tol=0.0, abs_tol=1e-9):
                    raise ValueError("指定椭圆边界未闭合")
            elif boundary_kind not in {"AcDbCircle", "AcDbRegion"}:
                raise ValueError(
                    "AutoCAD 填充仅支持显式点、闭合多段线、圆、椭圆或面域")
            area = _safe_get(boundary, "Area", None)
            if area is None and boundary_kind == "AcDbEllipse":
                boundary_area = math.pi * float(boundary.MajorRadius) * float(boundary.MinorRadius)
            else:
                boundary_area = float(area) if area is not None else None
            if boundary_area is None or not math.isfinite(boundary_area) or boundary_area <= 0:
                raise ValueError("无法验证闭合边界具有有效正面积")
        if boundary_handle is not None and not keep_boundary:
            raise ValueError("不会自动删除用户已有边界；使用 boundary_handle 时必须 keep_boundary=true")
        if associative and not keep_boundary:
            raise ValueError("关联填充必须 keep_boundary=true")
        if dry_run:
            return {
                "mode": "acad",
                "dry_run": True,
                "would_write": False,
                "pattern": pattern,
                "scale": scale,
                "angle": angle,
                "pattern_availability": "unverified_until_creation" if not solid else "available",
                "boundary_kind": boundary_kind,
                "boundary_area": boundary_area,
                "layer": layer,
                "associative": associative,
                "keep_boundary": keep_boundary,
            }

        created_boundary = None
        hatch = None
        try:
            self._apply_layer(layer)
            if normalized is not None:
                created_boundary = self.msp.AddLightWeightPolyline(
                    _pt2([coordinate for point in normalized for coordinate in point])
                )
                created_boundary.Closed = True
                boundary = created_boundary

            # ActiveX requires AppendOuterLoop to be the first operation after
            # AddHatch; do not read or write any hatch property between calls.
            hatch = self.msp.AddHatch(0 if solid else 1, pattern, bool(associative), 0)
            hatch.AppendOuterLoop(_dispatch_array([boundary]))
            hatch = refresh_valid_hatch(hatch)
            if not solid:
                hatch.PatternScale = scale
                hatch.PatternAngle = math.radians(angle)
            hatch.Evaluate()
            self.doc.Regen(1)

            low, high = bounding_box(hatch)
            readback = {
                "entity_type": str(hatch.EntityName),
                "pattern_name": str(hatch.PatternName),
                "associative": bool(hatch.AssociativeHatch),
                "layer": str(hatch.Layer),
                "loop_count": int(hatch.NumberOfLoops),
                "area": (
                    float(_safe_get(hatch, "Area", None))
                    if _safe_get(hatch, "Area", None) is not None else None
                ),
                "bounding_box": {
                    "min": [float(value) for value in low],
                    "max": [float(value) for value in high],
                },
            }
            if not solid:
                readback["pattern_scale"] = float(hatch.PatternScale)
                readback["pattern_angle"] = math.degrees(float(hatch.PatternAngle)) % 360.0
            verified = (
                readback["entity_type"] == "AcDbHatch"
                and readback["pattern_name"].upper() == pattern
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
                created_boundary.Delete()
                created_boundary = None
                self.doc.Regen(1)
            return {
                "mode": "acad",
                "dry_run": False,
                "would_write": True,
                "verified": True,
                "hatch_handle": self._handle_str(hatch.Handle),
                "boundary_handle": (
                    self._handle_str(boundary.Handle)
                    if boundary is not None and (created_boundary is not None or boundary_handle is not None)
                    else None
                ),
                "boundary_kind": boundary_kind,
                "boundary_area": boundary_area,
                "readback": readback,
            }
        except Exception:
            if hatch is not None:
                try:
                    hatch.Delete()
                except Exception:
                    pass
            if created_boundary is not None:
                try:
                    created_boundary.Delete()
                except Exception:
                    pass
            raise

    def draw_centerline(self, x1: float, y1: float, x2: float, y2: float,
                        layer: str = "CENTER") -> Dict[str, Any]:
        """给矩形范围画对称中心线（水平 + 垂直两条点划线），自动进 CENTER 层。"""
        self._check_doc()
        if not self._layer_exists(layer):
            lay = self.doc.Layers.Add(layer)
        else:
            lay = self._get_layer(layer)
        lay.color = 1
        try:
            self._ensure_linetype("CENTER")
            lay.Linetype = "CENTER"
        except Exception:
            pass
        xmin, xmax = min(x1, x2), max(x1, x2)
        ymin, ymax = min(y1, y2), max(y1, y2)
        cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
        extend = max(xmax - xmin, ymax - ymin) * 0.10
        if extend <= 0:
            extend = 100.0
        self._apply_layer(layer)
        h = self.msp.AddLine(_pt(xmin - extend, cy), _pt(xmax + extend, cy))
        v = self.msp.AddLine(_pt(cx, ymin - extend), _pt(cx, ymax + extend))
        return {"type": "CENTERLINE", "layer": layer,
                "handles": [self._handle_str(h.Handle), self._handle_str(v.Handle)],
                "center": (cx, cy), "extend": round(extend, 1)}

    # ---------- 图块与图框 ----------

    def _block_exists(self, name: str) -> bool:
        try:
            for i in range(self.doc.Blocks.Count):
                if self.doc.Blocks.Item(i).Name == name:
                    return True
        except Exception:
            pass
        return False

    def make_block(self, name: str, handles: List[str], base_x: float = 0,
                   base_y: float = 0, annotative: bool = False) -> str | Dict[str, Any]:
        """把指定句柄的图元打包成一个图块。

        用 Document.CopyObjects 深拷贝到新块，然后删除模型空间原图元。
        """
        self._check_doc()
        if not name:
            raise ValueError("图块名不能为空")
        if self._block_exists(name):
            raise ValueError(f"图块 {name} 已存在")
        if annotative:
            return self._make_annotative_block(name, handles, base_x, base_y)
        blk = self.doc.Blocks.Add(_pt(base_x, base_y), name)
        objs = []
        for h in handles:
            e = self._find(h)
            if e is not None:
                objs.append(e)
        if not objs:
            raise ValueError("没有找到可打包的图元")
        # VARIANT 数组需要原生 COM 对象（代理不能放进数组）；objs 保留代理以便 Delete 也受重试保护
        native_objs = [_ComRetryProxy._unwrap(e) for e in objs]
        arr = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, native_objs)
        self.doc.CopyObjects(arr, blk)
        delete_failures = []
        for e in objs:
            try:
                e.Delete()
            except Exception as exc:
                delete_failures.append(f"{getattr(e, 'Handle', '?')}: {exc}")
        if delete_failures:
            raise RuntimeError(
                "图块定义已创建，但源图元删除不完整，已触发撤销："
                + "；".join(delete_failures[:8]))
        return f"已创建图块 {name}（{len(objs)} 个图元，基点 {base_x},{base_y}）"

    def _make_annotative_block(
        self, name: str, handles: List[str], base_x: float, base_y: float,
    ) -> Dict[str, Any]:
        """使用 AutoCAD 原生命令建注释性块，并读回定义而非猜测状态。"""
        quoted_name = _lisp_string(name)
        selected = []
        selected_hex = set()
        for handle in handles:
            entity = self._find(handle)
            if entity is None:
                raise ValueError(f"图元句柄不存在于模型空间: {handle}")
            hex_handle = str(entity.Handle).upper()
            if not re.fullmatch(r"[0-9A-F]+", hex_handle):
                raise ValueError(f"AutoCAD 返回无效图元句柄: {hex_handle}")
            if hex_handle in selected_hex:
                raise ValueError(f"图元句柄重复: {handle}")
            selected_hex.add(hex_handle)
            selected.append(hex_handle)
        if not selected:
            raise ValueError("没有找到可打包的图元")

        # COM 给出 WCS 基点；命令行接收 UCS 点。_non 禁用这一次对象捕捉，
        # 否则开启 OSMODE 时指定基点可能被吸附到源图元上。
        ucs_point = self.doc.Utility.TranslateCoordinates(
            _pt(base_x, base_y), 0, 1, False)
        if len(ucs_point) != 3:
            raise RuntimeError("AutoCAD 无法把图块基点转换到当前 UCS")
        point_expr = "(list " + " ".join(_lisp_real(value) for value in ucs_point) + ")"
        handle_expr = " ".join(f'"{handle}"' for handle in selected)
        command = (
            "(progn "
            "(setq cadmcp_anno_ss (ssadd)) "
            f"(foreach cadmcp_anno_h '({handle_expr}) "
            "(ssadd (handent cadmcp_anno_h) cadmcp_anno_ss)) "
            f'(vl-cmdf "_.-BLOCK" {quoted_name} "_Annotative" "_Yes" '
            f'"_No" "_Mode" "_Delete" "_non" {point_expr} cadmcp_anno_ss "") '
            "(setq cadmcp_anno_ss nil cadmcp_anno_h nil) (princ))\n"
        )
        self._send_command_and_wait(command)
        try:
            block = self.doc.Blocks.Item(name)
        except Exception as exc:
            raise RuntimeError(f"原生命令未创建图块 {name}") from exc
        codes, values = block.GetXData("AcadAnnotative")
        expected_codes = (1001, 1000, 1002, 1070, 1070, 1002)
        expected_values = ("AcadAnnotative", "AnnotativeData", "{", 1, 1, "}")
        if tuple(codes or ()) != expected_codes or tuple(values or ()) != expected_values:
            raise RuntimeError(f"图块 {name} 的原生注释性状态无法回读验证")
        entity_count = int(block.Count)
        if entity_count != len(selected):
            raise RuntimeError(
                f"图块 {name} 实体数回读不一致: 期望 {len(selected)}，实际 {entity_count}")
        remaining = {
            str(self.msp.Item(index).Handle).upper()
            for index in range(self.msp.Count)
        }
        not_removed = sorted(selected_hex & remaining)
        if not_removed:
            raise RuntimeError(
                f"图块 {name} 已创建，但源图元未全部移除: {', '.join(not_removed[:8])}")
        return {
            "message": f"已创建注释性图块 {name}（{entity_count} 个图元，基点 {base_x},{base_y}）",
            "annotative": True,
            "verified": True,
            "readback": {
                "block_name": str(block.Name),
                "entity_count": entity_count,
                "annotative_xdata": list(zip(codes, values)),
                "source_handles_removed": True,
            },
        }

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
        """插入本地图块，可先从已验证的外部 DWG 块库导入定义。"""
        self._check_doc()
        import_result = None
        if source_path:
            import_result = self.import_block_definitions(
                source_path=source_path,
                block_names=[name],
                replace_existing=replace_existing,
                expected_sha256=source_sha256,
            )
        if not self._block_exists(name):
            raise ValueError(f"图块 {name} 不存在，请先用 cad_make_block 创建")
        sx = float(x_scale if x_scale is not None else scale)
        sy = float(y_scale if y_scale is not None else scale)
        sz = float(z_scale if z_scale is not None else scale)
        target_layer = layer or self.active_layer
        if not self._layer_exists(target_layer):
            self.doc.Layers.Add(target_layer)
        e = self.msp.InsertBlock(_pt(x, y), name, sx, sy, sz, math.radians(rotation))
        e.Layer = target_layer
        handle = self._handle_str(e.Handle)
        attribute_result = self.set_block_attributes(handle, attributes or {})
        bindings = []
        for item in field_bindings or []:
            payload = dict(item)
            payload["block_handle"] = handle
            bindings.append(self.bind_block_attribute_field(**payload))
        return {"type": "INSERT", "handle": self._handle_str(e.Handle), "block": name,
                "insert": (x, y), "rotation": rotation,
                "scales": {"x": sx, "y": sy, "z": sz}, "layer": target_layer,
                "attributes_updated": attribute_result["updated"],
                "missing_tags": attribute_result["missing_tags"],
                "unchanged_attributes": attribute_result["unchanged"],
                "field_bindings": bindings, "source_import": import_result}

    def draw_title_block(self, width: float, height: float, title: str = "",
                         project: str = "", design: str = "", check: str = "",
                         sheet_no: str = "", ratio: str = "1:100",
                         size: Optional[str] = None,
                         layer: str = "TITLE") -> str:
        """画工程图框 + 标题栏（外框粗线、内框细线、右下角标题栏 180×56）。

        size 指定标准图纸幅面（A0-A4）时，width/height 自动用对应标准尺寸。
        """
        self._check_doc()
        if size:
            key = size.upper()
            if key not in PAPER_SIZES:
                raise ValueError(f"未知图框幅面 {size}（可用: A0/A1/A2/A3/A4）")
            width, height = PAPER_SIZES[key]
        if not self._layer_exists(layer):
            lay = self.doc.Layers.Add(layer)
            lay.color = 7
        self._apply_layer(layer)
        self._add_lwpolyline([(0, 0), (width, 0), (width, height), (0, height)], 50)
        m, b = 10.0, 25.0
        self._add_lwpolyline([(b, m), (width - m, m), (width - m, height - m), (b, height - m)], 25)
        tb_w, tb_h = 180.0, 56.0
        x0, y0 = width - tb_w, 0.0
        self._add_lwpolyline([(x0, y0), (width, y0), (width, y0 + tb_h), (x0, y0 + tb_h)], 50)
        col = tb_w - 40.0
        self._add_line(x0 + col, y0, x0 + col, y0 + tb_h)
        self._add_line(x0, y0 + tb_h * 0.55, width, y0 + tb_h * 0.55)
        self._add_line(x0, y0 + tb_h * 0.27, width, y0 + tb_h * 0.27)
        self._add_text(title, x0 + 5, y0 + tb_h - 16, 12)
        self._add_text(f"工程:{project}" if project else "", x0 + 5, y0 + tb_h * 0.55 + 4, 7)
        self._add_text(f"设计:{design}  审核:{check}", x0 + 5, y0 + tb_h * 0.27 + 4, 7)
        self._add_text(f"图号 {sheet_no}", x0 + col + 4, y0 + tb_h - 16, 7)
        self._add_text(f"比例 {ratio}", x0 + col + 4, y0 + tb_h * 0.55 + 4, 7)
        self._add_text("日期 2026-08-12", x0 + col + 4, y0 + tb_h * 0.27 + 4, 7)
        return f"已画图框 {width}×{height} + 右下角标题栏 180×56（图名:{title or '未填'}）"

    def draw_table(self, origin_x: float, origin_y: float,
                   col_widths: Sequence[float], row_height: float,
                   headers: Sequence[str], rows: Sequence[Sequence[str]],
                   title: str = "", layer: str = "TABLE") -> Dict[str, Any]:
        """画通用表格：表头行 + 数据行，文字自动居中、自动适配列宽。"""
        nrow, ncol = validate_table_data(col_widths, headers, rows)
        self._check_doc()
        if not self._layer_exists(layer):
            self.doc.Layers.Add(layer)
        self._apply_layer(layer)
        total_w = float(sum(col_widths))
        total_h = float(row_height) * (nrow + 1)
        handles = []
        # 外框（粗线）
        e = self._add_lwpolyline(
            [(origin_x, origin_y), (origin_x + total_w, origin_y),
             (origin_x + total_w, origin_y + total_h),
             (origin_x, origin_y + total_h)], 50)
        handles.append(self._handle_str(e.Handle))
        # 竖分隔线
        x = origin_x
        for w in col_widths[:-1]:
            x += w
            e = self._add_line(x, origin_y, x, origin_y + total_h)
            handles.append(self._handle_str(e.Handle))
        # 横分隔线（表头下边框粗线）
        for r in range(1, nrow + 1):
            y = origin_y + row_height * r
            if r == 1:
                e = self._add_lwpolyline(
                    [(origin_x, y), (origin_x + total_w, y)], 50)
            else:
                e = self._add_line(origin_x, y, origin_x + total_w, y)
            handles.append(self._handle_str(e.Handle))
        # 单元格文字（水平垂直居中，字高适配列宽）
        for r in range(nrow + 1):
            for c in range(ncol):
                t = str(headers[c] if r == 0 else rows[r - 1][c])
                if not t:
                    continue
                cell_x = origin_x + sum(col_widths[:c]) + col_widths[c] / 2
                cell_y = origin_y + total_h - row_height * r - row_height / 2
                base_h = row_height * 0.5 if r == 0 else row_height * 0.42
                fit_h = col_widths[c] / max(len(t) * 1.05, 1.0)
                th = max(min(base_h, fit_h, row_height * 0.6), 1.0)
                e = self.msp.AddText(t, _pt(0, 0), th)
                handles.append(self._handle_str(e.Handle))
                try:
                    e.Alignment = 10          # acAlignmentMiddleCenter
                    e.TextAlignmentPoint = _pt(cell_x, cell_y)
                except Exception:
                    e.TextPosition = _pt(cell_x, cell_y)
        # 表题
        if title:
            e = self.msp.AddText(title, _pt(0, 0), row_height * 0.6)
            handles.append(self._handle_str(e.Handle))
            try:
                e.Alignment = 10
                e.TextAlignmentPoint = _pt(origin_x + total_w / 2,
                                           origin_y + total_h + row_height * 0.65)
            except Exception:
                e.TextPosition = _pt(origin_x + total_w / 2,
                                     origin_y + total_h + row_height * 0.65)
        return {"type": "TABLE", "layer": layer, "rows": nrow + 1, "cols": ncol,
                "width": round(total_w, 1), "height": round(total_h, 1),
                "handles": handles, "handles_count": len(handles)}

    # ---------- 布局、固定图框与精确出图 ----------

    def _get_layout(self, name: str):
        self._check_doc()
        try:
            return self.doc.Layouts.Item(name)
        except Exception as exc:
            raise ValueError(f"布局不存在: {name}") from exc

    def create_layout(self, name: str, replace: bool = False) -> Dict[str, Any]:
        """创建纸空间布局；replace 只允许替换非 Model 布局。"""
        self._check_doc()
        if not name or name.lower() == "model":
            raise ValueError("布局名称不能为空，也不能是 Model")
        try:
            existing = self.doc.Layouts.Item(name)
        except Exception:
            existing = None
        if existing is not None:
            if not replace:
                return {"layout_name": name, "created": False, "replaced": False}
            existing.Delete()
        layout = self.doc.Layouts.Add(name)
        return {"layout_name": layout.Name, "created": True,
                "replaced": existing is not None}

    def insert_template_sheet(
        self,
        template_id: str,
        sheet_id: str,
        layout_name: str,
        x: float = 0.0,
        y: float = 0.0,
    ) -> Dict[str, Any]:
        """把清单指定的原始图框块插入布局，比例只使用固定模板换算值。"""
        self._check_doc()
        if template_id not in self._template_manifests:
            raise ValueError(f"模板清单 {template_id} 尚未加载")
        manifest = self._template_manifests[template_id]
        sheet = next((item for item in manifest.sheets if item.id == sheet_id), None)
        if sheet is None:
            raise ValueError(f"模板 {template_id} 不存在图幅 {sheet_id}")
        if not self._block_exists(sheet.block_name):
            raise ValueError(
                f"当前工作副本没有模板块 {sheet.block_name}；必须先克隆模板 DWG，"
                "不能用通用图框替代")
        layout = self._get_layout(layout_name)
        block = layout.Block
        scale = float(sheet.fixed_insert_scale)
        # 该学院模板的块内图元采用原图全局坐标，块基点并不在图框左下角；
        # 用清单中的 source_bounds 抵消固定偏移，x/y 始终表示纸面图框左下角。
        insert_x = float(x) - float(sheet.source_bounds[0]) * scale
        insert_y = float(y) - float(sheet.source_bounds[1]) * scale
        entity = block.InsertBlock(_pt(insert_x, insert_y), sheet.block_name,
                                   scale, scale, scale, 0.0)
        return {"layout_name": layout_name, "template_id": template_id,
                "sheet_id": sheet_id, "block_name": sheet.block_name,
                "fixed_insert_scale": scale,
                "paper_origin": [x, y],
                "resolved_insert": [insert_x, insert_y],
                "handle": self._handle_str(entity.Handle)}

    def fill_template_fields(
        self,
        template_id: str,
        sheet_id: str,
        layout_name: str,
        values: Dict[str, Any],
        layer: str = "ENV-ANNO",
    ) -> Dict[str, Any]:
        """按模板属性标签或固定文字锚点填写字段，不改变图框线。"""
        self._check_doc()
        if template_id not in self._template_manifests:
            raise ValueError(f"模板清单 {template_id} 尚未加载")
        manifest = self._template_manifests[template_id]
        sheet = next((item for item in manifest.sheets if item.id == sheet_id), None)
        if sheet is None:
            raise ValueError(f"模板 {template_id} 不存在图幅 {sheet_id}")
        layout = self._get_layout(layout_name)
        block = layout.Block
        attribute_refs = []
        for index in range(block.Count):
            entity = block.Item(index)
            try:
                name = str(getattr(entity, "EffectiveName", "") or getattr(entity, "Name", ""))
                if name == sheet.block_name and entity.HasAttributes:
                    attribute_refs.extend(list(entity.GetAttributes()))
            except Exception:
                continue
        by_tag = {str(attribute.TagString).upper(): attribute for attribute in attribute_refs}
        written = []
        handles = []
        for key, value in values.items():
            field = sheet.fields.get(key)
            if field is None:
                raise ValueError(f"模板 {sheet_id} 未定义字段 {key}")
            text = "" if value is None else str(value)
            if field.mode == "attribute":
                tag = str(field.attribute_tag).upper()
                if tag not in by_tag:
                    raise ValueError(f"图框块 {sheet.block_name} 中找不到属性标签 {tag}")
                by_tag[tag].TextString = text
                handles.append(self._handle_str(by_tag[tag].Handle))
            else:
                if not self._layer_exists(layer):
                    self.doc.Layers.Add(layer)
                height = float(field.height or 3.5)
                entity = block.AddText(text, _pt(field.anchor[0], field.anchor[1]), height)
                entity.Layer = layer
                self._align_text_entity(entity, field.anchor, field.alignment)
                handles.append(self._handle_str(entity.Handle))
            written.append(key)
        return {"layout_name": layout_name, "sheet_id": sheet_id,
                "written_fields": written, "handles": handles}

    def fill_template_table(
        self,
        template_id: str,
        sheet_id: str,
        table_id: str,
        layout_name: str,
        column_keys: Sequence[str],
        rows: Sequence[Sequence[str]],
        page_index: int = 0,
        layer: str = "ENV-TABLE",
        **_: Any,
    ) -> Dict[str, Any]:
        """在模板既有表格格线中写字，不调用通用表格重画。"""
        self._check_doc()
        if template_id not in self._template_manifests:
            raise ValueError(f"模板清单 {template_id} 尚未加载")
        manifest = self._template_manifests[template_id]
        sheet = next((item for item in manifest.sheets if item.id == sheet_id), None)
        if sheet is None:
            raise ValueError(f"模板 {template_id} 不存在图幅 {sheet_id}")
        table = sheet.tables.get(table_id)
        if table is None:
            raise ValueError(f"图幅 {sheet_id} 未定义表格 {table_id}")
        if len(rows) > table.row_capacity:
            raise ValueError(
                f"表格 {table_id} 本页容量为 {table.row_capacity} 行，收到 {len(rows)} 行")
        missing_columns = [key for key in column_keys if key not in table.columns]
        if missing_columns:
            raise ValueError(f"模板表格缺少列映射: {', '.join(missing_columns)}")
        layout = self._get_layout(layout_name)
        block = layout.Block
        if not self._layer_exists(layer):
            self.doc.Layers.Add(layer)
        if page_index < 0 or page_index >= len(table.panel_offsets):
            raise ValueError(
                f"表格 {table_id} 只有 {len(table.panel_offsets)} 个面板，"
                f"page_index={page_index} 超出范围；不允许把文字写出原表格")
        page_dx, page_dy = table.panel_offsets[page_index]
        direction = -1.0 if table.row_direction == "down" else 1.0
        handles = []
        for row_index, row in enumerate(rows):
            for column_index, key in enumerate(column_keys):
                field = table.columns[key]
                if field.anchor is None:
                    raise ValueError(f"模板表格列 {key} 必须使用 text_anchor")
                text = "" if column_index >= len(row) else str(row[column_index])
                if not text:
                    continue
                x = table.origin[0] + field.anchor[0] + page_dx
                y = (table.origin[1] + field.anchor[1] + page_dy
                     + direction * row_index * table.row_height)
                height = float(field.height or table.text_height)
                entity = block.AddText(text, _pt(x, y), height)
                entity.Layer = layer
                self._align_text_entity(entity, (x, y), field.alignment)
                handles.append(self._handle_str(entity.Handle))
        return {"type": "TEMPLATE_TABLE", "template_id": template_id,
                "sheet_id": sheet_id, "table_id": table_id,
                "layout_name": layout_name, "rows": len(rows), "handles": handles}

    def create_viewport(
        self,
        layout_name: str,
        center_x: float,
        center_y: float,
        width: float,
        height: float,
        model_center_x: float,
        model_center_y: float,
        scale_denominator: float,
        lock: bool = True,
        model_mm_per_unit: float = 1.0,
    ) -> Dict[str, Any]:
        """在固定图框布局中创建并锁定指定比例的视口。"""
        self._check_doc()
        if not all(math.isfinite(float(v)) for v in (center_x, center_y, width, height,
                model_center_x, model_center_y, scale_denominator, model_mm_per_unit)):
            raise ValueError("视口参数必须为有限数值")
        if min(width, height, scale_denominator, model_mm_per_unit) <= 0:
            raise ValueError("视口宽高和比例分母必须大于 0")
        layout = self._get_layout(layout_name)
        self.doc.SetVariable("TILEMODE", 0)
        self.doc.MSpace = False
        self._activate_layout(layout)
        viewport = self.doc.PaperSpace.AddPViewport(_pt(center_x, center_y), width, height)
        custom_scale = float(model_mm_per_unit) / float(scale_denominator)
        readback = self._configure_sheet_viewport(viewport, {
            "paper_center": [center_x, center_y], "paper_size": [width, height],
            "model_center": [model_center_x, model_center_y, 0.0],
            "custom_scale": custom_scale, "model_view_height": height/custom_scale}, lock=lock)
        return {
            "layout_name": layout_name,
            "handle": self._handle_str(viewport.Handle),
            "paper_center": readback["paper_center"],
            "paper_size": readback["paper_size"],
            "model_center": readback["model_center"][:2],
            "scale_denominator": scale_denominator,
            "custom_scale": readback["custom_scale"],
            "locked": readback["locked"],
            "verified": True,
        }

    def configure_layout_plot(
        self,
        layout_name: str,
        media_name: str,
        style_sheet: str = "monochrome.ctb",
        device: str = "DWG To PDF.pc3",
        rotation: int = 0,
        plot_type: str = "layout",
        paper_units: str = "millimeters",
        scale_mode: str = "standard",
        scale_numerator: Optional[float] = None,
        scale_denominator: Optional[float] = None,
        center_plot: bool = False,
        plot_with_lineweights: bool = True,
        plot_with_plot_styles: bool = True,
        apply_named_page_setup: Optional[str] = None,
    ) -> Dict[str, Any]:
        """配置布局出图并完整读回；可先复制一个命名页面设置。"""
        self._check_doc()
        if rotation not in (0, 90, 180, 270):
            raise ValueError("rotation 只支持 0/90/180/270")
        layout = self._get_layout(layout_name)
        named_result = None
        if apply_named_page_setup:
            named_result = self.apply_named_page_setup(
                layout_name, apply_named_page_setup
            )
        plot_scale = {
            "mode": scale_mode,
            "standard_scale": "1:1" if scale_mode == "standard" else None,
            "numerator": scale_numerator,
            "denominator": scale_denominator,
        }
        self._write_plot_object(
            layout,
            device=device,
            media_name=media_name,
            rotation=rotation,
            plot_style_sheet=style_sheet,
            plot_type=plot_type,
            paper_units=paper_units,
            plot_scale=plot_scale,
            center_plot=center_plot,
            plot_with_lineweights=plot_with_lineweights,
            plot_with_plot_styles=plot_with_plot_styles,
        )
        actual, unverified = self._plot_payload(layout)
        if unverified:
            raise RuntimeError("布局出图设置后无法完整读回: " + ", ".join(unverified))
        return {
            "layout_name": layout_name,
            "applied_named_page_setup": apply_named_page_setup,
            "named_page_setup_result": named_result,
            "plot_settings": actual,
            "verified": True,
        }

    def publish_layout_pdf(self, layout_name: str, filename: str) -> Dict[str, Any]:
        """按已配置布局精确发布 PDF，不使用模型空间范围适配。"""
        self._check_doc()
        layout = self._get_layout(layout_name)
        path = self._safe_path(
            filename, default_suffix=".pdf", allowed_suffixes=(".pdf",))
        if path.exists():
            raise FileExistsError(f"PDF 已存在，未覆盖: {path}")
        device = str(layout.ConfigName or "").strip()
        if not device:
            raise RuntimeError(f"布局 {layout_name!r} 尚未配置打印设备")
        device = self._require_plot_device(layout, device)
        self._plot_layout_atomic(layout, path, device)
        return {"layout_name": layout_name, "path": str(path),
                "plot_type": "layout", "scale_to_fit": False}

    def export_pdf(self, filename: str) -> str:
        """把当前图纸输出为 PDF 打印稿（用 DWG To PDF 打印驱动，模型空间图面范围）。

        ⚠️ 两个坑：
          1. AutoCAD COM 的 PlotToFile 对中文文件名支持不稳（会写出乱码文件名），
             因此文件名含非 ASCII 字符时自动改用 ASCII 安全名（如 测试导出 → ______）；
          2. PlotToFile 是**异步写入**，文件要过约 20 秒才落盘，本方法会轮询等待
             （最多 30 秒），文件确实生成后才返回成功。
        """
        self._check_doc()
        path = self._safe_path(
            filename, default_suffix=".pdf", allowed_suffixes=(".pdf",))
        note = ""
        if any(ord(ch) > 127 for ch in path.stem):
            safe_stem = re.sub(r"[^A-Za-z0-9_-]", "_", path.stem) or "export"
            path = path.with_stem(safe_stem)
            note = f"（原文件名含中文，AutoCAD 写文件会乱码，已改用 {path.name}）"
        if path.exists():
            raise FileExistsError(f"PDF 已存在，未覆盖: {path}")
        self._plot_to_pdf(path)
        return f"已导出 PDF: {path}{note}"

    @staticmethod
    def _temporary_pdf_path(path: Path) -> Path:
        return path.with_name(f".{path.stem}.{uuid.uuid4().hex}.tmp.pdf")

    @staticmethod
    def _discard_temporary_pdf(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _wait_for_complete_pdf(path: Path) -> bool:
        """等待异步打印结束；要求 PDF 头有效且文件大小连续两次不再变化。"""
        previous_size = -1
        stable_polls = 0
        for _ in range(30):
            time.sleep(1)
            try:
                size = path.stat().st_size
                with path.open("rb") as stream:
                    is_pdf = stream.read(5) == b"%PDF-"
            except OSError:
                previous_size = -1
                stable_polls = 0
                continue
            if size > 5 and is_pdf:
                stable_polls = stable_polls + 1 if size == previous_size else 0
                if stable_polls >= 1:
                    return True
            else:
                stable_polls = 0
            previous_size = size
        return False

    @staticmethod
    def _require_plot_device(layout: Any, requested: str) -> str:
        """返回 AutoCAD 实际注册的打印设备名；查询失败或未安装时禁止继续。"""
        try:
            names = [str(item) for item in layout.GetPlotDeviceNames()]
        except Exception as exc:
            raise RuntimeError("无法读取 AutoCAD 打印设备列表，已取消出图") from exc
        matches = {name.casefold(): name for name in names}
        actual = matches.get(requested.casefold())
        if actual is None:
            raise RuntimeError(f"未找到打印设备 {requested!r}，已取消出图")
        return actual

    def _plot_layout_atomic(self, layout: Any, path: Path, device: str) -> None:
        """打印到同目录唯一临时文件，确认完整后再无覆盖地发布。"""
        plot = self.doc.Plot
        temp_path = self._temporary_pdf_path(path)
        previous_layout = self.doc.ActiveLayout
        previous_quiet = plot.QuietErrorMode
        error: Optional[BaseException] = None
        try:
            self._activate_layout(layout)
            plot.QuietErrorMode = True
            result = plot.PlotToFile(str(temp_path), device)
            if result is False:
                raise RuntimeError("AutoCAD 拒绝了 PDF 打印作业")
            if not self._wait_for_complete_pdf(temp_path):
                raise RuntimeError("PDF 发布超时或生成的文件不是有效 PDF")
        except BaseException as exc:
            error = exc
        restore_errors = []
        try:
            plot.QuietErrorMode = previous_quiet
        except Exception as exc:
            restore_errors.append(f"QuietErrorMode: {exc}")
        try:
            self._activate_layout(previous_layout)
        except Exception as exc:
            restore_errors.append(f"活动布局: {exc}")
        if error is not None and not isinstance(error, Exception):
            self._discard_temporary_pdf(temp_path)
            if restore_errors and hasattr(error, "add_note"):
                error.add_note("恢复 AutoCAD 状态失败: " + "; ".join(restore_errors))
            raise error
        if error is not None or restore_errors:
            self._discard_temporary_pdf(temp_path)
            details = str(error) if error is not None else "打印已完成"
            if restore_errors:
                details += f"；恢复 AutoCAD 状态失败: {'; '.join(restore_errors)}"
            raise RuntimeError(details) from error
        try:
            temp_path.rename(path)
        except Exception:
            self._discard_temporary_pdf(temp_path)
            raise

    def _plot_to_pdf(self, path: Path) -> None:
        """事务化导出模型空间：配置可恢复、临时文件完整、目标不覆盖。"""
        try:
            layout = self.doc.Layouts.Item("Model")
        except Exception as exc:
            raise RuntimeError("无法取得 Model 布局，已取消 PDF 导出") from exc
        device = self._require_plot_device(layout, "DWG To PDF.pc3")
        properties = ("ConfigName", "PlotType", "CenterPlot",
                      "UseStandardScale", "StandardScale")
        try:
            snapshot = {name: getattr(layout, name) for name in properties}
        except Exception as exc:
            raise RuntimeError("无法读取当前布局打印设置，已取消 PDF 导出") from exc
        error: Optional[BaseException] = None
        published = False
        try:
            layout.ConfigName = device
            layout.PlotType = 1
            layout.CenterPlot = True
            layout.UseStandardScale = True
            layout.StandardScale = 0
            self._plot_layout_atomic(layout, path, device)
            published = True
        except BaseException as exc:
            error = exc
        restore_errors = []
        for name in properties:
            try:
                setattr(layout, name, snapshot[name])
            except Exception as exc:
                restore_errors.append(f"{name}: {exc}")
        if error is not None and not isinstance(error, Exception):
            if published:
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    restore_errors.append(f"无法清理输出文件 {path}: {exc}")
            if restore_errors and hasattr(error, "add_note"):
                error.add_note("恢复布局打印设置失败: " + "; ".join(restore_errors))
            raise error
        if error is not None or restore_errors:
            if published and path.exists():
                try:
                    path.unlink()
                except OSError:
                    restore_errors.append(f"无法清理输出文件 {path}")
            details = str(error) if error is not None else "PDF 已生成"
            if restore_errors:
                details += f"；恢复布局打印设置失败: {'; '.join(restore_errors)}"
            raise RuntimeError(details) from error

    def _add_line(self, x1: float, y1: float, x2: float, y2: float):
        return self.msp.AddLine(_pt(x1, y1), _pt(x2, y2))

    def _add_lwpolyline(self, pts: List[Point], lw: int):
        flat = [v for p in pts + [pts[0]] for v in p]
        e = self.msp.AddLightWeightPolyline(_pt2(flat))
        try:
            e.Lineweight = lw
        except Exception:
            pass
        return e

    def _add_text(self, text: str, x: float, y: float, height: float):
        if not text:
            return None
        return self.msp.AddText(text, _pt(x, y), height)

    # ---------- 查询 / 修改 ----------

    def preview_region(self, **kwargs) -> Dict[str, Any]:
        """临时导出当前文档后渲染局部图像，不另存或调整视图。"""
        from cad_mcp.drawing.preview import preview_acad_region
        self._check_doc()
        return preview_acad_region(self, **kwargs)

    def get_entity_properties(self, handle: str) -> Dict[str, Any]:
        """读取模型空间实体属性及可编辑字段。"""
        from cad_mcp.drawing.entity_properties import get_entity_properties
        return get_entity_properties(self, handle, "acad")

    def edit_entity_properties(self, handle: str, properties: Dict[str, Any],
                               *, dry_run: bool = True) -> Dict[str, Any]:
        """先预检，按白名单修改实体并回读验证；默认零写入预览。"""
        from cad_mcp.drawing.entity_properties import edit_entity_properties
        return edit_entity_properties(self, handle, properties, dry_run=dry_run, mode="acad")

    def chamfer(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """预检后调用原生 CHAMFER；本方法自行管理撤销组，预览不写入。"""
        from cad_mcp.drawing.chamfer import chamfer
        return chamfer(self, payload)

    def extend_entity(self, subject_handle: str, boundary_handles: List[str], endpoint: str,
                      *, extend_edges: bool = False, dry_run: bool = True) -> Dict[str, Any]:
        """通过原生 EXTEND 延伸指定一端，预检后建立独立撤销组并验证结果。"""
        from cad_mcp.drawing.native_extend import extend_native
        return extend_native(self, subject_handle, boundary_handles, endpoint,
                             extend_edges=extend_edges, dry_run=dry_run)

    def _find(self, handle: str):
        """按句柄（十进制字符串）在模型空间查找图元。"""
        try:
            target = int(handle)
        except (ValueError, TypeError):
            return None
        for i in range(self.msp.Count):
            e = self.msp.Item(i)
            if int(e.Handle, 16) == target:
                return e
        return None

    def list_entities(self, layer: Optional[str] = None,
                      offset: int = 0, limit: int = 100) -> str:
        self._check_doc()
        if offset < 0 or not 1 <= limit <= 500:
            raise ValueError("offset 必须大于等于 0，limit 必须在 1 到 500 之间")
        rows = []
        matched = 0
        has_more = False
        for i in range(self.msp.Count):
            e = self.msp.Item(i)
            if layer and e.Layer != layer:
                continue
            if matched < offset:
                matched += 1
                continue
            if len(rows) >= limit:
                has_more = True
                break
            rows.append(f"{self._handle_str(e.Handle)} | {e.EntityName} | 图层:{e.Layer}")
            matched += 1
        if not rows:
            return ("图纸为空" if offset == 0 and layer is None else
                    f"从 offset={offset} 起没有匹配图元")
        note = (f"\n[分页] offset={offset}，返回 {len(rows)} 条"
                + (f"；后面还有数据，请用 offset={offset + len(rows)} 继续" if has_more else "；已到末页"))
        return "\n".join(rows) + note

    def delete_entity(self, handle: str) -> str:
        self._check_doc()
        e = self._find(handle)
        if e is None:
            return f"未找到句柄为 {handle} 的图元"
        kind, layer = e.EntityName, e.Layer
        e.Delete()
        return f"已删除 {kind}（句柄 {handle}，图层 {layer}）"

    def move_entity(self, handle: str, dx: float, dy: float) -> str:
        self._check_doc()
        e = self._find(handle)
        if e is None:
            return f"未找到句柄为 {handle} 的图元"
        e.Move(_pt(0, 0, 0), _pt(dx, dy, 0))
        return f"已移动图元 {handle}，位移 ({dx}, {dy})"

    def copy_entity(self, handle: str, dx: float, dy: float) -> Dict[str, Any]:
        """复制图元并偏移 (dx, dy)，返回新副本句柄。原图元保持不变。"""
        self._check_doc()
        e = self._find(handle)
        if e is None:
            raise ValueError(f"未找到句柄为 {handle} 的图元")
        new_e = e.Copy()
        new_e.Move(_pt(0, 0, 0), _pt(dx, dy, 0))
        return {"type": e.EntityName, "handle": self._handle_str(new_e.Handle),
                "layer": new_e.Layer, "copied_from": handle}

    def _copy_and_move(self, e, dx: float, dy: float):
        """复制单个图元并平移，返回新句柄。"""
        new_e = e.Copy()
        new_e.Move(_pt(0, 0, 0), _pt(dx, dy, 0))
        return self._handle_str(new_e.Handle)

    def array_rect(self, handles: List[str], rows: int, cols: int,
                   row_spacing: float, col_spacing: float) -> str:
        """矩形阵列：每个原件只调用一次 AutoCAD 原生 ArrayRectangular。"""
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
        source_count = 0
        for h in handles:
            e = self._find(h)
            if e is None:
                continue
            e.ArrayRectangular(rows, cols, 1, row_spacing, col_spacing, 1.0)
            source_count += 1
            self._yield_com()
        if source_count == 0:
            raise ValueError("没有找到可阵列的图元")
        created = source_count * (rows * cols - 1)
        return (f"已用 AutoCAD 原生阵列将 {source_count} 个图元排为 {rows} 行 × {cols} 列"
                f"（行距 {row_spacing}，列距 {col_spacing}），新增 {created} 个副本")

    def array_polar(self, handles: List[str], cx: float, cy: float,
                    count: int, total_angle: float = 360) -> str:
        """环形阵列：每个原件只调用一次 AutoCAD 原生 ArrayPolar。"""
        self._check_doc()
        if count < 2:
            raise ValueError("环形阵列数量至少 2 个")
        validate_bulk_creation(len(handles), count - 1, "环形阵列")
        if count == 2 and total_angle >= 360:
            total_angle = 180
        source_count = 0
        for h in handles:
            e = self._find(h)
            if e is None:
                continue
            e.ArrayPolar(count, math.radians(total_angle), _pt(cx, cy))
            source_count += 1
            self._yield_com()
        if source_count == 0:
            raise ValueError("没有找到可阵列的图元")
        created = source_count * (count - 1)
        return (f"已用 AutoCAD 原生阵列将 {source_count} 个图元绕 ({cx}, {cy}) 均布 {count} 个"
                f"（总角 {total_angle}°），新增 {created} 个副本")

    def copy_linear(self, handles: List[str], dx: float, dy: float, copies: int) -> str:
        """线性复制；单轴使用原生阵列，斜向复制分段让出消息泵。"""
        self._check_doc()
        if copies < 1:
            raise ValueError("份数至少为 1")
        validate_bulk_creation(len(handles), copies, "线性复制")
        if dx == 0 and dy == 0:
            raise ValueError("位移 (dx, dy) 不能同时为 0")
        source_count = 0
        for h in handles:
            e = self._find(h)
            if e is None:
                continue
            if dx == 0:
                e.ArrayRectangular(copies + 1, 1, 1, dy, 1.0, 1.0)
            elif dy == 0:
                e.ArrayRectangular(1, copies + 1, 1, 1.0, dx, 1.0)
            else:
                for i in range(1, copies + 1):
                    self._copy_and_move(e, dx * i, dy * i)
                    if i % 8 == 0:
                        self._yield_com()
            source_count += 1
            self._yield_com()
        if source_count == 0:
            raise ValueError("没有找到可复制的图元")
        created = source_count * copies
        mode = "AutoCAD 原生单轴阵列" if dx == 0 or dy == 0 else "受控斜向复制"
        return (f"已用{mode}复制 {source_count} 个图元 × {copies} 份"
                f"（位移 {dx},{dy}），新增 {created} 个副本")

    def offset(self, handle: str, distance: float) -> Dict[str, Any]:
        """调用 AutoCAD 曲线对象原生 Offset；保留源图元并返回新句柄。"""
        self._check_doc()
        if abs(distance) <= 1e-12:
            raise ValueError("偏移距离不能为 0")
        source = self._find(handle)
        if source is None:
            raise ValueError(f"未找到句柄为 {handle} 的图元")
        source_type = str(source.EntityName)
        offset_method = getattr(source, "Offset", None)
        if not callable(offset_method):
            raise ValueError(
                f"图元类型 {source_type} 不支持 AutoCAD 原生 OFFSET；"
                "常见支持类型为 LINE / ARC / CIRCLE / ELLIPSE / POLYLINE / SPLINE / XLINE")
        result = offset_method(float(distance))
        created = list(result) if result is not None else []
        if not created:
            raise RuntimeError(f"{source_type} 偏移后没有生成图元")
        return {
            "source_handle": handle,
            "source_type": source_type,
            "distance": distance,
            "source_retained": True,
            "entities": [self._entity_summary(entity) for entity in created],
        }

    def rotate_entity(self, handle: str, base_x: float, base_y: float, angle: float) -> str:
        """旋转图元：绕基点 (base_x, base_y) 逆时针 angle 度。"""
        self._check_doc()
        e = self._find(handle)
        if e is None:
            raise ValueError(f"未找到句柄为 {handle} 的图元")
        e.Rotate(_pt(base_x, base_y), math.radians(angle))
        return f"已旋转图元 {handle}，绕 ({base_x}, {base_y}) 逆时针 {angle}°"

    def mirror_entity(self, handle: str, x1: float, y1: float, x2: float, y2: float) -> str:
        """镜像图元：关于直线 (x1,y1)-(x2,y2) 镜像。"""
        self._check_doc()
        e = self._find(handle)
        if e is None:
            raise ValueError(f"未找到句柄为 {handle} 的图元")
        e.Mirror(_pt(x1, y1), _pt(x2, y2))
        return f"已镜像图元 {handle}（镜像线 {x1},{y1} - {x2},{y2}）"

    @_bounded_com_scan
    def check_drawing(self, tolerance: Optional[float] = None) -> str:
        """图面体检：扫描重叠图元、疑似断线、空图层。tolerance 容差留空自动按图纸范围算。"""
        self._check_doc()
        entity_count = int(self.msp.Count)
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
        lines, circles = [], []
        used_layers = set()
        for i in range(entity_count):
            self._scan_checkpoint(i)
            e = self.msp.Item(i)
            try:
                used_layers.add(e.Layer)
            except (TimeoutError, pythoncom.com_error):
                raise
            except Exception:
                pass
            try:
                name = e.EntityName
            except (TimeoutError, pythoncom.com_error):
                raise
            except Exception:
                continue
            try:
                if name == "AcDbLine":
                    sp, ep = e.StartPoint, e.EndPoint
                    lines.append((self._handle_str(e.Handle), (sp[0], sp[1]), (ep[0], ep[1])))
                elif name == "AcDbCircle":
                    c, r = e.Center, e.Radius
                    circles.append((self._handle_str(e.Handle), (c[0], c[1]), r))
            except (TimeoutError, pythoncom.com_error):
                raise
            except Exception:
                continue
        scan = scan_line_circle_issues(lines, circles, tol)
        issues = list(scan["issues"])
        layer_collection = self.doc.Layers
        for i in range(int(layer_collection.Count)):
            self._scan_checkpoint(i)
            n = layer_collection.Item(i).Name
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

    def _check_doc(self) -> None:
        self._ensure_connected()
        if self.doc is None or self.msp is None:
            raise RuntimeError("当前未连接 AutoCAD，请先新建或打开图纸")

    def _drawing_extent(self):
        """通过 AutoCAD 原生 EXTMIN/EXTMAX 读取总包围盒，避免逐实体 COM 扫描。"""
        if self.msp is None:
            return None
        if self.msp.Count == 0:
            return None
        self.doc.Regen(1)
        mn = self.doc.GetVariable("EXTMIN")
        mx = self.doc.GetVariable("EXTMAX")
        return (float(mn[0]), float(mn[1]), float(mx[0]), float(mx[1]))

    # ---------- 环境工程语义元数据与批处理 ----------

    def _ensure_xdata_app(self) -> None:
        self._check_doc()
        try:
            self.doc.RegisteredApplications.Item(XDATA_APP_NAME)
        except Exception:
            self.doc.RegisteredApplications.Add(XDATA_APP_NAME)

    def set_entity_metadata(self, handle: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """把环境工程语义 ID 和摘要写入实体 XData。"""
        self._check_doc()
        entity = self.doc.HandleToObject(format(int(handle), "X"))
        self._ensure_xdata_app()
        chunks = pack_metadata(metadata)
        type_codes = [1001] + [1000] * len(chunks)
        values = [XDATA_APP_NAME, *chunks]
        codes_variant = win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_I2, type_codes)
        values_variant = win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_VARIANT, values)
        entity.SetXData(codes_variant, values_variant)
        return {"handle": handle, "app_name": XDATA_APP_NAME,
                "chunk_count": len(chunks)}

    @staticmethod
    def _com_state_value(value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            return [AcadBackend._com_state_value(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        try:
            return list(value)
        except Exception:
            return str(value)

    def _entity_state_hash(self, entity: Any) -> str:
        kind = str(getattr(entity, "EntityName", "UNKNOWN"))
        payload: Dict[str, Any] = {
            "type": kind,
            "layer": str(getattr(entity, "Layer", "0")),
        }
        property_names = {
            "AcDbLine": ("StartPoint", "EndPoint"),
            "AcDbPolyline": ("Coordinates", "Closed", "ConstantWidth"),
            "AcDb2dPolyline": ("Coordinates", "Closed"),
            "AcDb3dPolyline": ("Coordinates", "Closed"),
            "AcDbCircle": ("Center", "Radius"),
            "AcDbArc": ("Center", "Radius", "StartAngle", "EndAngle"),
            "AcDbText": ("TextString", "InsertionPoint", "Height", "Rotation", "StyleName"),
            "AcDbMText": ("TextString", "InsertionPoint", "Width", "Height", "Rotation", "StyleName"),
            "AcDbBlockReference": ("EffectiveName", "Name", "InsertionPoint",
                                    "XScaleFactor", "YScaleFactor", "Rotation"),
        }
        names = property_names.get(kind, ())
        for name in names:
            try:
                payload[name] = self._com_state_value(getattr(entity, name))
            except Exception:
                continue
        if not names:
            try:
                low, high = bounding_box(entity)
                payload["bounds"] = [self._com_state_value(low), self._com_state_value(high)]
            except Exception:
                pass
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    def get_entity_state_hash(self, handle: str) -> str:
        self._check_doc()
        entity = self._find(handle)
        if entity is None:
            raise ValueError(f"未找到句柄为 {handle} 的图元")
        return self._entity_state_hash(entity)

    @staticmethod
    def _xdata_chunks(result: Any) -> List[str]:
        """规范化 AutoCAD GetXData 的多种“无数据”返回形式。

        不同 AutoCAD/pywin32 组合可能返回 None、(None, None)、空数组，
        或正常的 (type_codes, values)。没有本应用 XData 时统一返回空列表。
        """
        if not isinstance(result, (tuple, list)) or len(result) != 2:
            return []
        type_codes, values = result
        if type_codes is None or values is None:
            return []
        try:
            pairs = zip(type_codes, values)
        except TypeError:
            return []
        chunks: List[str] = []
        for code, value in pairs:
            try:
                is_text = int(code) == 1000
            except (TypeError, ValueError):
                continue
            if is_text and value is not None:
                chunks.append(str(value))
        return chunks

    def get_entity_metadata(self, handle: str) -> Optional[Dict[str, Any]]:
        self._check_doc()
        entity = self._find(handle)
        if entity is None:
            raise ValueError(f"未找到句柄为 {handle} 的图元")
        try:
            result = out_pair(entity,"GetXData",leading=(XDATA_APP_NAME,))
        except Exception:
            return None
        chunks = self._xdata_chunks(result)
        return unpack_metadata(chunks) if chunks else None

    def list_managed_entities(self) -> List[ManagedEntityRecord]:
        self._check_doc()
        records: List[ManagedEntityRecord] = []
        for index in range(self.msp.Count):
            entity = self.msp.Item(index)
            try:
                result = out_pair(entity,"GetXData",leading=(XDATA_APP_NAME,))
            except Exception:
                continue
            chunks = self._xdata_chunks(result)
            if not chunks:
                continue
            try:
                metadata = unpack_metadata(chunks)
            except (ValueError, TypeError):
                continue
            current_state_hash = self._entity_state_hash(entity)
            materialized_hash = metadata.get("materialized_hash")
            records.append(ManagedEntityRecord(
                handle=self._handle_str(entity.Handle),
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
        """在一个 AutoCAD 撤销组中执行完整环境工程操作计划。"""
        self._check_doc()
        from cad_mcp.core.transactions import execute_reconciled_plan
        started = self._begin_undo()
        try:
            result = execute_reconciled_plan(self, plan, policy)
            self._commit_undo(started)
            return result
        except Exception:
            self._rollback_undo(started)
            raise

    def get_geometry_extents(self) -> Dict[str, Any]:
        """返回模型空间总包围盒，供调用方自行判断图幅与比例。"""
        self._check_doc()
        ext = self._drawing_extent()
        if ext is None:
            return {"empty": True, "entity_count": 0}
        xmin, ymin, xmax, ymax = ext
        width, height = xmax - xmin, ymax - ymin
        return {
            "empty": False,
            "entity_count": int(self.msp.Count),
            "min": [xmin, ymin],
            "max": [xmax, ymax],
            "width": width,
            "height": height,
            "center": [(xmin + xmax) / 2, (ymin + ymax) / 2],
            "diagonal": math.hypot(width, height),
        }

    def _current_text_height(self) -> float:
        """读取当前文字样式字高；不再根据图形范围自动计算。"""
        try:
            height = float(self.doc.ActiveTextStyle.Height)
            if height > 0:
                return height
        except Exception:
            pass
        height = float(self.doc.GetVariable("TEXTSIZE"))
        return height if height > 0 else 2.5

    def set_linetype_scale(self, scale: float) -> str:
        """兼容性接口：显式设置 LTSCALE，不做自动推断。"""
        self._check_doc()
        self.doc.SetVariable("LTSCALE", float(scale))
        self.doc.Regen(1)
        return f"已设置全局线型比例 {scale}"

    @_bounded_com_scan
    def query_entities(self, layer: Optional[str] = None,
                       entity_type: Optional[str] = None,
                       offset: int = 0, limit: int = 50,
                       include_bounds: bool = False) -> Dict[str, Any]:
        """从模型空间索引 offset 开始分页查询，达到 limit 后立即停止 COM 扫描。"""
        self._check_doc()
        total = int(self.msp.Count)
        wanted = entity_type.lower() if entity_type else None
        rows = []
        i = min(offset, total)
        while i < total and len(rows) < limit:
            self._scan_checkpoint(i - offset)
            e = self.msp.Item(i)
            kind = str(e.EntityName)
            entity_layer = str(e.Layer)
            if ((not layer or entity_layer == layer)
                    and (not wanted or kind.lower() == wanted
                         or kind.lower().removeprefix("acdb") == wanted.removeprefix("acdb"))):
                row = {
                    "index": i,
                    "handle": self._handle_str(e.Handle),
                    "type": kind,
                    "layer": entity_layer,
                }
                if include_bounds:
                    try:
                        mn, mx = bounding_box(e)
                        row["bounds"] = {
                            "min": [float(mn[0]), float(mn[1])],
                            "max": [float(mx[0]), float(mx[1])],
                        }
                    except Exception as exc:
                        _raise_scan_transport_error(exc)
                        row["bounds"] = None
                rows.append(row)
            i += 1
        return {
            "entities": rows,
            "offset": offset,
            "limit": limit,
            "modelspace_count": total,
            "next_offset": i if i < total else None,
            "scanned": max(i - offset, 0),
        }

    def regen(self) -> Dict[str, Any]:
        self._check_doc()
        self.doc.Regen(1)
        return {"mode": "acad", "regenerated": True}

    def health(self, reconnect: bool = False) -> Dict[str, Any]:
        """检查当前 COM 连接；仅在 reconnect=True 时尝试建立或恢复连接。"""
        if reconnect:
            self._ensure_connected()
        if self.acad is None:
            return {
                "mode": "acad",
                "connected": False,
                "document_open": False,
                "document_state": "closed",
                "is_quiescent": None,
                **health_metadata("acad"),
            }
        try:
            count = int(self.acad.Documents.Count)
            quiet = bool(self.acad.GetAcadState().IsQuiescent)
            return {
                "mode": "acad",
                "connected": True,
                "document_open": self.doc is not None,
                "document_state": "open" if self.doc is not None else "closed",
                "document_count": count,
                "is_quiescent": quiet,
                "file": self.current_path.name if self.current_path else None,
                **health_metadata("acad"),
            }
        except Exception as exc:
            detail = record_error(exc, "cad_health_probe")
            return {
                "mode": "acad",
                "connected": False,
                "document_open": False,
                "document_state": "unavailable",
                "is_quiescent": None,
                "error": str(exc),
                "error_id": detail["request_id"],
                **health_metadata("acad"),
            }

    def _maybe_release_acad(self) -> None:
        """关闭文档后保留仍可用的精确应用引用，仅在 COM 已失效时清空。"""
        if self.acad is None:
            return
        try:
            _ = int(self.acad.Documents.Count)
        except Exception:
            self.acad = None

    def close_document(self, save: bool = False) -> str:
        """关闭本实例新建的文档（用于测试清理）。只关自己新建的，绝不关用户文档。"""
        if self._own_document and self.doc is not None:
            try:
                name = self.doc.Name
                if save:
                    self.save()
                self.doc.Close(False)
                self.doc = None
                self.msp = None
                self._own_document = False
                self._maybe_release_acad()
                return f"已关闭测试文档 {name}"
            except Exception as exc:
                raise RuntimeError(f"关闭文档失败: {exc}") from exc
        return "没有需要关闭的本实例文档"

    def close_drawing(self, save: bool = False) -> str:
        """关闭当前 AutoCAD 文档（用户主动关闭）。save=True 保存后关，False 不保存。"""
        self._ensure_connected()
        if self.doc is None:
            return "当前没有打开的 AutoCAD 文档"
        name = self.doc.Name
        try:
            if save:
                self.save()
            self.doc.Close(False)
        except Exception as exc:
            raise RuntimeError(f"关闭文档失败: {exc}") from exc
        self.doc = None
        self.msp = None
        self._own_document = False
        self._maybe_release_acad()
        return f"已关闭文档 {name}" + ("（已保存）" if save else "")

    def quit(self) -> str:
        """只退出当前已连接的精确 AutoCAD 实例，绝不为退出操作新建或接管实例。"""
        if self.acad is None:
            self._release_autocad_lock()
            return "未连接 AutoCAD；未启动或接管任何实例"
        try:
            self.acad.Quit()
        except Exception as exc:
            raise RuntimeError(f"退出失败（可能 CAD 正忙）: {exc}") from exc
        self.acad = None
        self.doc = None
        self.msp = None
        self.current_path = None
        self._own_document = False
        self._undo_mark_open = False
        self._release_autocad_lock()   # 释放操作锁，其他客户端可接上
        return "已退出 AutoCAD，文件锁已释放"
