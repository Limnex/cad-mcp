"""Per-user configuration with environment-variable overrides."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Mapping, Optional


MAX_CONFIG_BYTES = 64 * 1024
CONFIG_FIELDS = {"drawings_dir", "log_dir", "mode", "semantic_policy"}
ENV_FIELDS = {
    "drawings_dir": "CAD_DRAWINGS_DIR",
    "log_dir": "CAD_LOG_DIR",
    "mode": "CAD_MODE",
    "semantic_policy": "CAD_SEMANTIC_POLICY",
}


class ConfigError(ValueError):
    """Raised when the user configuration cannot be safely loaded."""


@dataclass(frozen=True)
class Settings:
    drawings_dir: Path
    log_dir: Path
    mode: str
    config_path: Path
    semantic_policy: str = "strict"

    def as_dict(self) -> dict[str, str]:
        return {
            "drawings_dir": str(self.drawings_dir),
            "log_dir": str(self.log_dir),
            "mode": self.mode,
            "semantic_policy": self.semantic_policy,
            "config_path": str(self.config_path),
        }


def config_path(env: Optional[Mapping[str, str]] = None) -> Path:
    values = os.environ if env is None else env
    configured = values.get("CAD_CONFIG_FILE")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".cad-mcp" / "config.json"


def load_config(
    path: Optional[Path] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    target = Path(path) if path is not None else config_path(env)
    if not target.exists():
        return {}
    if target.stat().st_size > MAX_CONFIG_BYTES:
        raise ConfigError(f"配置文件超过 {MAX_CONFIG_BYTES} 字节上限: {target}")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"无法读取配置文件 {target}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigError("配置文件根节点必须是 JSON 对象")
    unknown = sorted(set(payload) - CONFIG_FIELDS)
    if unknown:
        raise ConfigError("配置文件包含未知字段: " + ", ".join(unknown))
    validated: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"配置字段 {key} 必须是非空字符串")
        validated[key] = value.strip()
    if "mode" in validated and validated["mode"] not in {"dxf", "acad"}:
        raise ConfigError("mode 只能是 dxf 或 acad")
    if validated.get("semantic_policy") not in {None, "strict", "legacy"}:
        raise ConfigError("semantic_policy 只能是 strict 或 legacy")
    return validated


def load_settings(
    *,
    env: Optional[Mapping[str, str]] = None,
    path: Optional[Path] = None,
) -> Settings:
    values = os.environ if env is None else env
    target = Path(path) if path is not None else config_path(values)
    stored = load_config(target, env=values)
    drawings = values.get(ENV_FIELDS["drawings_dir"], stored.get("drawings_dir", str(Path.home() / "CAD")))
    logs = values.get(ENV_FIELDS["log_dir"], stored.get("log_dir", str(Path.home() / ".cad-mcp" / "logs")))
    mode = values.get(ENV_FIELDS["mode"], stored.get("mode", "acad"))
    if mode not in {"dxf", "acad"}:
        raise ConfigError("CAD_MODE 只能是 dxf 或 acad")
    semantic_policy = values.get(ENV_FIELDS["semantic_policy"], stored.get("semantic_policy", "strict"))
    if semantic_policy not in {"strict", "legacy"}:
        raise ConfigError("CAD_SEMANTIC_POLICY 只能是 strict 或 legacy")
    return Settings(
        drawings_dir=Path(drawings).expanduser(),
        log_dir=Path(logs).expanduser(),
        mode=mode,
        config_path=target,
        semantic_policy=semantic_policy,
    )


def save_config(values: Mapping[str, str], path: Optional[Path] = None) -> Path:
    target = Path(path) if path is not None else config_path()
    validated = load_config_payload(values)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(validated, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


def load_config_payload(values: Mapping[str, str]) -> dict[str, str]:
    unknown = sorted(set(values) - CONFIG_FIELDS)
    if unknown:
        raise ConfigError("配置包含未知字段: " + ", ".join(unknown))
    payload = {key: str(value).strip() for key, value in values.items()}
    for key, value in payload.items():
        if not value:
            raise ConfigError(f"配置字段 {key} 必须是非空字符串")
    if payload.get("mode") not in {None, "dxf", "acad"}:
        raise ConfigError("mode 只能是 dxf 或 acad")
    if payload.get("semantic_policy") not in {None, "strict", "legacy"}:
        raise ConfigError("semantic_policy 只能是 strict 或 legacy")
    return payload


def set_config_value(key: str, value: str, path: Optional[Path] = None) -> Path:
    if key not in CONFIG_FIELDS:
        raise ConfigError(f"未知配置字段: {key}")
    target = Path(path) if path is not None else config_path()
    values = load_config(target)
    values[key] = value
    return save_config(values, target)


def unset_config_value(key: str, path: Optional[Path] = None) -> Path:
    if key not in CONFIG_FIELDS:
        raise ConfigError(f"未知配置字段: {key}")
    target = Path(path) if path is not None else config_path()
    values = load_config(target)
    values.pop(key, None)
    return save_config(values, target)
