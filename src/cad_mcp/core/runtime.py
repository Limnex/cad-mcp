"""Shared runtime diagnostics for the CAD MCP server.

The module deliberately keeps logging metadata-only: operation names, error
classification, and request IDs are recorded, while drawing payloads are not.
"""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
import platform
from pathlib import Path
import sys
import threading
import uuid
from typing import Any, Dict, Mapping, Optional, Tuple

from pydantic import BaseModel, ValidationError

from cad_mcp.config import load_settings


SERVER_NAME = "cad_mcp"
SERVER_VERSION = "1.5.3"
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 2
_RETRYABLE_COM_HRESULTS = {0x80010001, 0x8001010A, 0x8001011F}

_LOGGER = logging.getLogger(SERVER_NAME)
_LOGGER_LOCK = threading.Lock()
_ERROR_LOCK = threading.Lock()
_RECENT_ERROR_ID: Optional[str] = None


class CadMcpError(Exception):
    """Typed domain failure exposed through the common MCP error envelope."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        hint: str,
        retryable: bool = False,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint
        self.retryable = retryable
        self.details = dict(details or {})


class UnsupportedModeError(CadMcpError):
    def __init__(self, message: str, *, details: Optional[Mapping[str, Any]] = None):
        super().__init__(
            message,
            code="UNSUPPORTED_MODE",
            hint="切换到 acad 模式后重试；DXF 模式不会伪造 AutoCAD 专属能力",
            details=details,
        )


class UnsupportedOperationError(CadMcpError):
    def __init__(self, message: str, *, details: Optional[Mapping[str, Any]] = None):
        super().__init__(
            message,
            code="UNSUPPORTED_OPERATION",
            hint="保留当前文件不变，改用 AutoCAD 已验证的手工流程",
            details=details,
        )


class ConfirmationRequiredError(CadMcpError):
    def __init__(self, message: str, *, details: Optional[Mapping[str, Any]] = None):
        super().__init__(
            message,
            code="CONFIRMATION_REQUIRED",
            hint="先执行 dry_run 获取 confirmation_token，核对计划后再原样提交",
            details=details,
        )


class ConflictError(CadMcpError):
    def __init__(self, message: str, *, details: Optional[Mapping[str, Any]] = None):
        super().__init__(
            message,
            code="CONFLICT",
            hint="检查当前图纸是否已变化，以及 replace/update_existing 保护选项",
            details=details,
        )


def setup_logging(
    runtime_dir: Optional[Path] = None,
    *,
    log_dir: Optional[Path] = None,
) -> logging.Logger:
    """Configure stderr and three-file rotating logs once per process."""
    with _LOGGER_LOCK:
        if getattr(_LOGGER, "_cad_configured", False):
            return _LOGGER

        _LOGGER.setLevel(logging.INFO)
        _LOGGER.propagate = False
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"
        )

        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(formatter)
        _LOGGER.addHandler(stderr_handler)

        default_log_dir = Path(log_dir) if log_dir is not None else (
            Path(runtime_dir) / "logs"
            if runtime_dir is not None
            else load_settings().log_dir
        )
        resolved_log_dir = Path(
            os.environ.get("CAD_LOG_DIR", str(default_log_dir))
        ).expanduser()
        try:
            resolved_log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                resolved_log_dir / "cad_mcp.log",
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            _LOGGER.addHandler(file_handler)
        except OSError as exc:
            _LOGGER.warning("file_logging_unavailable exception_type=%s", type(exc).__name__)

        setattr(_LOGGER, "_cad_configured", True)
        return _LOGGER


def get_logger(name: Optional[str] = None) -> logging.Logger:
    if name:
        return _LOGGER.getChild(name)
    return _LOGGER


def _new_error_id() -> str:
    return f"cad-{uuid.uuid4().hex[:12]}"


def get_recent_error_id() -> Optional[str]:
    with _ERROR_LOCK:
        return _RECENT_ERROR_ID


def _classify_error(exc: Exception) -> Tuple[str, bool, str]:
    cause = exc
    seen = set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, CadMcpError) and cause.code == "EXECUTION_UNKNOWN":
            if getattr(exc, "rollback_status", None) == "succeeded":
                return "OPERATION_ROLLED_BACK", False, "本次操作已撤销，未保存"
            return cause.code, False, cause.hint
        cause = cause.__cause__ or cause.__context__
    message = str(exc).lower()
    class_name = type(exc).__name__.lower()

    if isinstance(exc, CadMcpError):
        return exc.code, exc.retryable, exc.hint
    if isinstance(exc, (ValidationError, ValueError, TypeError)):
        return "INVALID_ARGUMENT", False, "检查字段名称、类型、范围和必填项后重试"
    if isinstance(exc, FileNotFoundError):
        return "NOT_FOUND", False, "检查文件名和路径是否存在"
    if isinstance(exc, PermissionError):
        return "PERMISSION_DENIED", False, "检查文件占用状态和目录写入权限"
    if isinstance(exc, NotImplementedError):
        return "UNSUPPORTED_OPERATION", False, "改用当前后端支持的工具或切换绘图模式"
    if isinstance(exc, TimeoutError) or any(
        token in message
        for token in (
            "timeout",
            "超时",
            "正忙",
            "服务器忙",
            "持续拒绝",
            "稍后重试",
            "未进入空闲",
            "实例未就绪",
        )
    ):
        return "BACKEND_BUSY", True, "等待 AutoCAD 空闲后使用相同参数重试"
    if class_name == "com_error" or (
        "com" in class_name and "error" in class_name
    ):
        hresult = getattr(exc, "hresult", None)
        retryable = (
            hresult is not None
            and (hresult & 0xFFFFFFFF) in _RETRYABLE_COM_HRESULTS
        )
        if retryable:
            return "COM_BUSY", True, "确认 AutoCAD 空闲后重试；持续失败时先调用 cad_health"
        return "COM_ERROR", False, "先调用 cad_health，并根据 HRESULT 检查 AutoCAD 或图纸状态"
    if isinstance(exc, ModuleNotFoundError):
        return "DEPENDENCY_MISSING", False, "按锁定依赖重新安装 CAD MCP 运行环境"
    if "当前没有打开" in str(exc):
        return "PRECONDITION_FAILED", False, "先新建或打开图纸，再执行该操作"
    if isinstance(exc, OSError):
        return "IO_ERROR", False, "检查文件路径、占用状态和磁盘可用空间"
    return "INTERNAL_ERROR", False, "记录错误编号并检查 cad_mcp.log；不要盲目重复写入操作"


def record_error(exc: Exception, operation: str) -> Dict[str, Any]:
    """Record a classified error without logging the tool's drawing payload."""
    global _RECENT_ERROR_ID
    error_id = _new_error_id()
    code, retryable, hint = _classify_error(exc)
    with _ERROR_LOCK:
        _RECENT_ERROR_ID = error_id
    server = sys.modules.get("cad_mcp.server")
    known_operations = getattr(server, "_FULL_TOOL_REGISTRY", {})
    log_operation = (operation if operation in known_operations or operation in {
        "acad_rollback_undo", "cad_health_probe", "_gateway_call", "_gateway_describe"
    } else "unknown_operation")
    # Never format exceptions or traceback source lines: both can contain inputs.
    locations = []
    tb = exc.__traceback__
    while tb is not None:
        locations.append({"module": Path(tb.tb_frame.f_code.co_filename).name,
                          "function": tb.tb_frame.f_code.co_name, "line": tb.tb_lineno})
        tb = tb.tb_next
    known_fields = set()
    pending = list(BaseModel.__subclasses__()) if isinstance(exc, ValidationError) else []
    visited = set()
    while pending:
        model = pending.pop()
        if model in visited:
            continue
        visited.add(model)
        known_fields.update(model.model_fields)
        pending.extend(model.__subclasses__())
    fields = ([{"type": item["type"], "loc": [part if isinstance(part, int) or part in known_fields else "<key>"
                                               for part in item["loc"]]}
               for item in exc.errors(include_url=False, include_context=False, include_input=False)]
              if isinstance(exc, ValidationError) else [])
    _LOGGER.error(
        "operation_failed request_id=%s operation=%s code=%s retryable=%s exception_type=%s fields=%s stack=%s",
        error_id,
        log_operation,
        code,
        retryable,
        type(exc).__name__,
        json.dumps(fields),
        json.dumps(locations),
    )
    result = {
        "code": code,
        "operation": operation,
        "retryable": retryable,
        "hint": hint,
        "request_id": error_id,
    }
    if isinstance(exc, CadMcpError) and exc.details:
        result["details"] = exc.details
    if isinstance(exc, ValidationError):
        result.setdefault("details", {})["validation_errors"] = exc.errors(
            include_url=False, include_context=False, include_input=False,
        )
    rollback_status = getattr(exc, "rollback_status", None)
    if rollback_status is not None:
        result.setdefault("details", {})["rollback_status"] = rollback_status
    return result


def success_json(payload: Dict[str, Any]) -> str:
    """Keep the existing successful response shape and formatting."""
    return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)


def failure_json(exc: Exception, operation: str) -> str:
    detail = record_error(exc, operation)
    return json.dumps(
        {"ok": False, "error": str(exc), "error_detail": detail},
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    )


def backend_capabilities(mode: str) -> Dict[str, Any]:
    common = {
        "strict_input_validation": True,
        "structured_errors": True,
    }
    if mode == "acad":
        return {
            **common,
            "engine": "autocad_com",
            "native_extend": True,
            "native_format": "dwg",
            "native_dwg": True,
            "autocad_required": True,
            "transaction": "undo_group",
            "reconnect_supported": True,
            "com_retry": True,
        }
    return {
        **common,
        "engine": "ezdxf",
        "native_format": "dxf",
        "native_dwg": False,
        "autocad_required": False,
        "transaction": "memory_snapshot",
        "reconnect_supported": False,
        "com_retry": False,
    }


def health_metadata(mode: str) -> Dict[str, Any]:
    return {
        "server": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "capabilities": backend_capabilities(mode),
        "recent_error_id": get_recent_error_id(),
    }
