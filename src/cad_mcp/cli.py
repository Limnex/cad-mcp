"""Command-line entry point for the MCP server and user configuration."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional, Sequence

from cad_mcp.config import (
    CONFIG_FIELDS,
    ConfigError,
    config_path,
    load_settings,
    set_config_value,
    unset_config_value,
)


FIELD_ALIASES = {
    "drawings-dir": "drawings_dir",
    "log-dir": "log_dir",
    "mode": "mode",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cad-mcp")
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("config", help="查看或修改用户配置")
    actions = config.add_subparsers(dest="action", required=True)
    actions.add_parser("show", help="显示当前生效配置")
    actions.add_parser("path", help="显示配置文件位置")
    setter = actions.add_parser("set", help="设置一个配置项")
    setter.add_argument("field", choices=sorted(FIELD_ALIASES))
    setter.add_argument("value")
    unsetter = actions.add_parser("unset", help="删除一个配置项")
    unsetter.add_argument("field", choices=sorted(FIELD_ALIASES))
    return parser


def _run_config(args: argparse.Namespace) -> int:
    if args.action == "show":
        print(json.dumps(load_settings().as_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.action == "path":
        print(config_path())
        return 0
    key = FIELD_ALIASES[args.field]
    if key not in CONFIG_FIELDS:
        raise ConfigError(f"未知配置字段: {key}")
    if args.action == "set":
        target = set_config_value(key, args.value)
        print(f"已更新: {target}")
        return 0
    target = unset_config_value(key)
    print(f"已更新: {target}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        from cad_mcp.server import main as run_server

        run_server()
        return 0
    if arguments[0] == "--toolset":
        if len(arguments) != 2 or arguments[1] not in {"all", "webcodex"}:
            print("配置错误: --toolset 可选值为 all 或 webcodex", file=sys.stderr)
            return 2
        os.environ["CAD_MCP_TOOLSET"] = arguments[1]
        from cad_mcp.server import main as run_server

        run_server()
        return 0
    try:
        args = _parser().parse_args(arguments)
        if args.command == "config":
            return _run_config(args)
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    return 2
