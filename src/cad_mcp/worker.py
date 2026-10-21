"""Private JSON-lines worker. COM objects never leave this serial process."""

from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
import json
import sys

from cad_mcp.core.runtime import failure_json
from cad_mcp.core.tool_results import encode_tool_result


def _snapshot(server):
    raw = server._raw_backend(server._backend)
    path = getattr(raw, "current_path", None)
    return {
        "mode": server._backend_mode(server._backend),
        "document_open": getattr(raw, "doc", None) is not None,
        "file": str(path) if path is not None else None,
    }


def serve(server) -> None:
    """Run serially; shutdown releases Python resources, never calls CAD quit/save."""
    output = sys.stdout

    def reply(payload):
        output.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")
        output.flush()

    tools = {tool.name: tool for tool in server.mcp._tool_manager.list_tools()}
    reply({"kind": "ready", "state": _snapshot(server)})
    for line in sys.stdin:
        request = json.loads(line)
        if request.get("kind") == "shutdown":
            return
        name = request["tool_name"]
        try:
            tool = tools.get(name)
            if tool is None:
                raise ValueError(f"未知 CAD 工具: {name}")
            with redirect_stdout(sys.stderr):
                result = encode_tool_result(asyncio.run(tool.run(request["arguments"])))
        except Exception as exc:
            # FastMCP wraps validation errors; keep the original classification.
            from mcp.server.fastmcp.exceptions import ToolError
            from pydantic import ValidationError

            if isinstance(exc, ToolError) and isinstance(exc.__cause__, ValidationError):
                exc = exc.__cause__
            result = failure_json(exc, name)
        reply({"kind": "result", "request_id": request["request_id"],
               "result": result, "state": _snapshot(server)})


def main() -> None:
    from cad_mcp import server

    serve(server)


if __name__ == "__main__":
    main()
