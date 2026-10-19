"""Resolve existing drawings for opening without changing output path policy."""

from pathlib import Path
from typing import Callable, Sequence


def resolve_open_drawing_path(
    value: str,
    *,
    allowed_suffixes: Sequence[str],
    resolve_filename: Callable[[str], Path],
) -> Path:
    """Accept a bare filename or an existing local absolute drawing path.

    Relative names retain each backend's managed-directory validation.  Only
    opening may use an absolute path; callers must not reuse this for output.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("图纸路径必须是非空字符串")
    candidate = Path(value)
    if not candidate.is_absolute():
        path = resolve_filename(value)
    else:
        if candidate.drive.startswith("\\\\") or ".." in candidate.parts:
            raise ValueError("打开图纸仅支持本机盘符绝对路径，不支持 UNC 或路径回退")
        if candidate.suffix.casefold() not in {
            suffix.casefold() for suffix in allowed_suffixes
        }:
            raise ValueError(
                "打开图纸的文件扩展名非法，应为 "
                + "、".join(allowed_suffixes)
                + f": {value}"
            )
        path = candidate.resolve(strict=False)
        if path.drive.startswith("\\\\"):
            raise ValueError("打开图纸仅支持本机盘符绝对路径")
    if not path.is_file():
        raise FileNotFoundError(f"图纸文件不存在或不是普通文件: {path}")
    return path.resolve(strict=True)
