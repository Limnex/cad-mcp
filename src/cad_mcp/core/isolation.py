"""Hard process deadlines around tool execution, including non-returning COM calls."""

from __future__ import annotations

import asyncio
from collections import deque
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid

from cad_mcp.core.runtime import CadMcpError, failure_json, health_metadata, success_json


class ToolProcessSupervisor:
    """One persistent serial worker, with deadlines enforced outside that worker."""

    def __init__(self, *, mode="acad", tool_count=110, command=None,
                 read_timeout=30.0, write_timeout=120.0, startup_timeout=15.0):
        self._command = command or [sys.executable, "-u", "-B", "-m", "cad_mcp.worker"]
        self._read_timeout = read_timeout
        self._write_timeout = write_timeout
        self._startup_timeout = startup_timeout
        self._tool_count = tool_count
        self._process = None
        self._generation = 0
        self._state = {"mode": mode, "document_open": False, "file": None}
        self._gate = threading.Lock()
        self._busy = threading.Event()
        self._write_unknown = False
        self._needs_bind = False
        self._closed = False
        self._responses = None
        self._readers = []
        self._stderr = deque(maxlen=20)
        self._sender = None
        self._cleanup_unverified = False

    def _worker_info(self):
        process = self._process
        return {
            "running": process is not None and process.poll() is None,
            "pid": process.pid if process is not None else None,
            "busy": self._busy.is_set(), "generation": self._generation,
            "rebind_required": self._needs_bind,
            "write_result_unknown": self._write_unknown,
        }

    def _accept_state(self, state):
        if (not isinstance(state, dict) or state.get("mode") not in {"acad", "dxf"}
                or type(state.get("document_open")) is not bool
                or (state.get("file") is not None and not isinstance(state["file"], str))):
            raise ValueError("工作进程返回了无效状态")
        self._state = dict(state)

    def diagnostics(self):
        """Never probes COM or waits for the execution gate."""
        state = self._state
        unavailable = self._needs_bind or self._write_unknown or self._busy.is_set()
        return success_json({
            "ok": True, "mode": state["mode"],
            "connected": state["mode"] == "dxf" and not unavailable,
            "document_open": False,
            "document_state": "unavailable" if unavailable else "closed",
            "is_quiescent": None, "file": state.get("file"),
            "last_known_document_open": state["document_open"],
            "tool_count": self._tool_count,
            "worker": self._worker_info(), **health_metadata(state["mode"]),
        })

    def _start(self):
        if self._closed:
            raise RuntimeError("MCP 工作进程监控器已关闭")
        environment = os.environ.copy()
        source_root = str(Path(__file__).resolve().parents[2])
        environment["PYTHONPATH"] = os.pathsep.join(
            value for value in (source_root, environment.get("PYTHONPATH")) if value)
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        process = subprocess.Popen(
            self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
            env=environment,
        )
        self._process = process
        self._generation += 1
        responses = queue.Queue()
        self._responses = responses
        self._stderr = deque(maxlen=20)
        stderr_lines = self._stderr

        def read_stdout():
            try:
                for line in process.stdout:
                    responses.put(json.loads(line))
            except (OSError, ValueError) as exc:
                responses.put({"kind": "protocol_error", "error": str(exc)})
            finally:
                responses.put({"kind": "eof"})

        def read_stderr():
            try:
                for line in process.stderr:
                    stderr_lines.append(line.rstrip())
            except (OSError, ValueError):
                pass

        self._readers = [threading.Thread(target=read_stdout, daemon=True),
                         threading.Thread(target=read_stderr, daemon=True)]
        for reader in self._readers:
            reader.start()
        try:
            ready = responses.get(timeout=self._startup_timeout)
        except queue.Empty as exc:
            raise TimeoutError("工作进程启动超时") from exc
        if ready.get("kind") != "ready":
            raise RuntimeError("工作进程启动失败：" + "\n".join(stderr_lines))
        self._accept_state(ready["state"])

    def _stop(self):
        process = self._process
        if process is None:
            return
        # Only this owned Python worker is terminated; never the AutoCAD process.
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        if self._sender is not None:
            self._sender.join(timeout=0.5)
        for reader in self._readers:
            reader.join(timeout=0.5)
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass  # A dead worker may have broken its input pipe while flushing.
        self._process = None
        self._responses = None
        self._readers = []
        self._sender = None
        self._cleanup_unverified = False

    def close(self):
        self._closed = True
        self._stop()

    def _failed_worker(self, *, name, write, dispatched, reason):
        self._needs_bind = True
        if write and dispatched:
            self._write_unknown = True
        details = {"tool_name": name, "worker_generation": self._generation,
                   "worker_pid": self._process.pid if self._process is not None else None,
                   "mode": self._state["mode"], "last_known_file": self._state.get("file"),
                   "write_result_unknown": self._write_unknown,
                   "rollback_status": "not_verified" if self._write_unknown else "not_applicable",
                   "configuration_cache_invalidated": True}
        try:
            self._stop()
            details["worker_cleanup"] = "completed"
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._cleanup_unverified = True
            details["worker_cleanup"] = "unverified"
            details["cleanup_error"] = str(exc)
        return CadMcpError(
            f"{name}：{reason}；工作进程已隔离，未重试原操作",
            code="EXECUTION_UNKNOWN" if self._write_unknown else "WORKER_UNAVAILABLE",
            hint=("写入结果未知，禁止继续写入；请人工核对 CAD 后重启 MCP，不能认定已回滚"
                  if self._write_unknown else
                  "确认 CAD 已打开后重新连接，并按完整路径重新绑定图纸；配置需重新加载和预览"),
            details=details,
        )

    def _check_write_allowed(self, target, write):
        if self._cleanup_unverified:
            raise CadMcpError(
                "上一个工作进程的清理尚未确认，已阻止新的执行请求",
                code="WORKER_UNAVAILABLE", hint="保留当前 CAD 状态，检查残留工作进程并重启 MCP",
            )
        if self._write_unknown and write:
            raise CadMcpError(
                "此前写入执行结果未知，已阻止后续写入、保存、关闭和退出操作",
                code="EXECUTION_UNKNOWN", hint="人工核对 CAD 并重启 MCP 后再恢复写入",
                details={"write_result_unknown": True, "rollback_status": "not_verified"},
            )
        if self._needs_bind and write and target != "cad_new_drawing":
            raise CadMcpError(
                "工作进程已失效，必须先按完整路径重新绑定图纸",
                code="PRECONDITION_FAILED", hint="先连接已有 CAD，再调用 cad_open_drawing；不自动重试写入",
            )

    def call(self, name, arguments, *, target_name=None, write=False):
        target = target_name or name
        if target == "cad_health":
            payload = arguments.get("params", {})
            if name == "cad_gateway_call":
                payload = payload.get("arguments", {}).get("params", {})
            if not payload.get("reconnect", False) and (
                self._busy.is_set() or self._process is None):
                return self.diagnostics()
        self._check_write_allowed(target, write)
        timeout = self._write_timeout if write else self._read_timeout
        if not self._gate.acquire(timeout=timeout):
            raise CadMcpError("另一个 CAD 操作仍在执行，当前请求未发送",
                              code="BACKEND_BUSY", hint="等待当前操作返回后检查状态", retryable=True)
        dispatched = False
        self._busy.set()
        try:
            # A queued caller must recheck after an earlier write became unknown.
            self._check_write_allowed(target, write)
            if self._process is None:
                self._start()
            request_id = uuid.uuid4().hex
            text = json.dumps({"request_id": request_id, "tool_name": name,
                               "arguments": arguments}, ensure_ascii=False, allow_nan=False) + "\n"
            process, responses = self._process, self._responses

            def send():
                try:
                    process.stdin.write(text)
                    process.stdin.flush()
                except (OSError, ValueError) as exc:
                    responses.put({"kind": "send_error", "error": str(exc)})

            sender = threading.Thread(target=send, daemon=True)
            self._sender = sender
            sender.start()
            dispatched = True
            deadline = time.monotonic() + timeout
            try:
                response = responses.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise TimeoutError("工作进程超过外部执行时限") from exc
            if response.get("kind") != "result" or response.get("request_id") != request_id:
                raise RuntimeError("工作进程退出或通信失效")
            result = response["result"]
            if not isinstance(result, str):
                raise ValueError("工作进程返回了无效工具结果")
            self._accept_state(response["state"])
            # Native EXTEND can report an uncertain write while its worker is
            # responsive. Keep this new result handling scoped to that tool.
            if target == "cad_extend":
                outcome = json.loads(result)
                if (isinstance(outcome, dict) and outcome.get("ok") is False
                        and outcome.get("error_detail", {}).get("code") == "EXECUTION_UNKNOWN"):
                    self._write_unknown = True
            if target in {"cad_open_drawing", "cad_new_drawing"} and self._state["document_open"]:
                try:
                    if json.loads(result).get("ok") is True:
                        self._needs_bind = False
                except (ValueError, AttributeError):
                    pass
            if target == "cad_health":
                health = json.loads(result)
                health["worker"] = self._worker_info()
                health["worker"]["busy"] = False
                result = success_json(health)
            return result
        except CadMcpError:
            raise
        except Exception as exc:
            raise self._failed_worker(name=target, write=write, dispatched=dispatched,
                                      reason=str(exc)) from exc
        finally:
            self._busy.clear()
            self._gate.release()


def _policy(name, arguments, full_registry):
    target, payload = name, arguments.get("params", {})
    if name == "cad_gateway_call":
        target = payload.get("tool_name", "")
        payload = payload.get("arguments", {}).get("params", {})
    if target in {"cad_gateway_list", "cad_gateway_describe", "cad_open_drawing"}:
        return target, False
    tool = full_registry.get(target)
    annotation = tool.annotations if tool is not None else None
    readonly = annotation is not None and annotation.readOnlyHint is True
    return target, not readonly and payload.get("dry_run") is not True


def install_isolated_execution(mcp, full_registry, *, mode):
    """Keep registered schemas and results; only replace the execution functions."""
    supervisor = ToolProcessSupervisor(mode=mode, tool_count=len(full_registry))
    for tool in mcp._tool_manager.list_tools():
        def make_dispatch(name):
            async def dispatch(**kwargs):
                target = name
                try:
                    arguments = {key: value.model_dump(mode="json") if hasattr(value, "model_dump") else value
                                 for key, value in kwargs.items()}
                    if name == "cad_gateway_call":
                        gateway = arguments["params"]
                        target = gateway["tool_name"]
                        inner = full_registry.get(target)
                        if inner is None:
                            raise ValueError(f"未知 CAD 工具: {target}")
                        # Validate inner arguments before local health shortcuts or policy decisions.
                        metadata = inner.fn_metadata
                        parsed = metadata.arg_model.model_validate(metadata.pre_parse_json(gateway["arguments"]))
                        gateway["arguments"] = parsed.model_dump(mode="json")
                    target, write = _policy(name, arguments, full_registry)
                    return await asyncio.to_thread(supervisor.call, name, arguments,
                                                   target_name=target, write=write)
                except Exception as exc:
                    return failure_json(exc, target)
            return dispatch

        tool.fn = make_dispatch(tool.name)
        tool.is_async = True
    return supervisor
