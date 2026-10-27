"""Shared fail-closed handling for uncertain writes, including returned failures."""

from functools import wraps
from contextvars import ContextVar
import json

from cad_mcp.core.runtime import CadMcpError, failure_json

WRITE_BACKEND = ContextVar("write_backend", default=None)


def mark_unknown_write():
    backend = WRITE_BACKEND.get()
    if backend is not None:
        backend._semantic_write_blocked = True


def uncertain_result(payload):
    if not isinstance(payload, dict):
        return False
    detail = payload.get("error_detail") or {}
    details = detail.get("details") or {}
    status = payload.get("semantic_rollback_status", details.get("rollback_status",
                         payload.get("rollback_status")))
    # A verified outer transaction supersedes an uncertain inner operation.
    if status in {"succeeded", "not_needed"}:
        return False
    return (status in {"failed_or_unverified", "not_verified", "failed", "unknown"}
            or detail.get("code") in {"EXECUTION_UNKNOWN", "SEMANTIC_ROLLBACK_UNKNOWN"}
            or details.get("write_result_unknown") is True)


def uncertain_tool_result(result):
    """Inspect textual envelopes without treating an MCP image as JSON."""
    from mcp.types import CallToolResult
    texts = ([result] if isinstance(result, str) else
             [item.text for item in result.content if item.type == "text"]
             if isinstance(result, CallToolResult) else [])
    for value in texts:
        try:
            if uncertain_result(json.loads(value)):
                return True
        except (TypeError, ValueError):
            continue
    return False


def block_error(code="EXECUTION_UNKNOWN"):
    return CadMcpError(
        "此前写入或回滚无法确认，已阻止写入、保存、关闭和退出",
        code=code, hint="保留当前图纸，人工核对后重启 MCP",
        details={"write_result_unknown": True, "rollback_status": "not_verified"})


def protect_writes(fn, backend):
    """Apply to every public mutator, independently of semantic policy."""
    @wraps(fn)
    def guarded(params):
        raw = backend()
        raw = getattr(raw, "_b", raw)
        preview = getattr(params, "dry_run", False) is True
        if getattr(raw, "_semantic_write_blocked", False) and not preview:
            return failure_json(block_error("SEMANTIC_ROLLBACK_UNKNOWN"), fn.__name__)
        token = WRITE_BACKEND.set(raw)
        try:
            try:
                result = fn(params)
            except Exception as exc:
                result = failure_json(exc, fn.__name__)
        finally:
            WRITE_BACKEND.reset(token)
        try:
            payload = json.loads(result)
        except (TypeError, ValueError):
            payload = None
        if uncertain_result(payload):
            raw._semantic_write_blocked = True
            payload["error"] = "写入或回滚无法确认，已停止后续写入"
            payload.setdefault("error_detail", {}).update(
                code="EXECUTION_UNKNOWN", retryable=False, hint="保留当前图纸，人工核对后重启 MCP")
            return json.dumps(payload, ensure_ascii=False)
        elif isinstance(payload, dict) and payload.get("semantic_rollback_status",
                (payload.get("error_detail", {}).get("details") or {}).get("rollback_status")) == "succeeded":
            raw._semantic_write_blocked = False
        elif getattr(raw, "_semantic_write_blocked", False) and not preview:
            return failure_json(block_error(), fn.__name__)
        return result
    return guarded
