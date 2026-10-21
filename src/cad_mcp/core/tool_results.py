"""Typed, JSON-safe transport for strings and native MCP tool results."""

from mcp.types import CallToolResult


def encode_tool_result(result):
    if isinstance(result, str):
        return result
    if isinstance(result, CallToolResult):
        return {"_cad_mcp_result": "CallToolResult",
                "value": result.model_dump(mode="json", by_alias=True, exclude_none=True)}
    raise ValueError("工作进程返回了无效工具结果")


def decode_tool_result(result):
    if isinstance(result, str):
        return result
    if (isinstance(result, dict) and set(result) == {"_cad_mcp_result", "value"}
            and result["_cad_mcp_result"] == "CallToolResult"):
        return CallToolResult.model_validate(result["value"])
    raise ValueError("工作进程返回了无效工具结果")
