"""Backend-neutral planning and validation for DraftingProfile 2.0.

The engine never writes a drawing.  It compares a strict external profile with
an evidence snapshot supplied by a backend, creates a stable plan hash, and
stores a short-lived confirmation record.  The AutoCAD backend re-captures the
snapshot immediately before a formal apply so a token cannot be reused after
the target drawing changes.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import threading
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from cad_mcp.dwt.contracts import DraftingProfileV2
from cad_mcp.core.runtime import ConfirmationRequiredError, ConflictError


_PREVIEW_LOCK = threading.Lock()
_PREVIEW_TTL_SECONDS = 30 * 60
_MAX_PREVIEWS = 128


@dataclass(frozen=True)
class PreviewRecord:
    token: str
    created_at: float
    profile_path: str
    profile_sha256: str
    target_key: str
    snapshot_hash: str
    plan_hash: str
    update_existing: bool
    preserve_unmanaged: bool


_PREVIEWS: Dict[str, PreviewRecord] = {}


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _same(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) <= 1e-9
    if isinstance(left, str) and isinstance(right, str):
        return left.casefold() == right.casefold()
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return set(left) == set(right) and all(_same(left[key], right[key]) for key in left)
    if isinstance(left, Sequence) and isinstance(right, Sequence) and not isinstance(
        left, (str, bytes)
    ) and not isinstance(right, (str, bytes)):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


def _index(items: Iterable[Mapping[str, Any]], key: str) -> Dict[str, Mapping[str, Any]]:
    result: Dict[str, Mapping[str, Any]] = {}
    for item in items:
        name = item.get(key)
        if isinstance(name, str):
            result[name.casefold()] = item
    return result


def _changes(
    actual: Mapping[str, Any],
    desired: Mapping[str, Any],
    fields: Iterable[str],
    *,
    skip_unknown: Iterable[str] = (),
):
    result = []
    skip = set(skip_unknown)
    for field in fields:
        before = actual.get(field)
        after = desired.get(field)
        if field in skip and before is None:
            continue
        if not _same(before, after):
            result.append({"field": field, "before": before, "after": after})
    return result


def target_key(target: Mapping[str, Any]) -> str:
    path = target.get("path")
    if isinstance(path, str) and path:
        return str(Path(path).resolve()).casefold()
    name = str(target.get("name") or "")
    mode = str(target.get("mode") or "")
    return f"{mode}:{name}".casefold()


def assert_target(target: Mapping[str, Any], requested: Optional[str]) -> None:
    if not requested:
        return
    requested_path = Path(requested).expanduser()
    actual_path = target.get("path")
    actual_name = str(target.get("name") or "")
    if requested_path.is_absolute():
        if not actual_path or str(Path(str(actual_path)).resolve()).casefold() != str(
            requested_path.resolve()
        ).casefold():
            raise ConflictError(
                "target_drawing 与当前打开图纸不一致，已停止",
                details={"requested": str(requested_path.resolve()), "actual": actual_path},
            )
        return
    if requested_path.name.casefold() != actual_name.casefold():
        raise ConflictError(
            "target_drawing 与当前打开图纸不一致，已停止",
            details={"requested": requested, "actual": actual_name},
        )


def snapshot_hash(snapshot: Mapping[str, Any]) -> str:
    """Hash only drawing evidence; diagnostics timestamps are deliberately absent."""
    return content_hash(snapshot)


def _resource_preflight(
    profile: DraftingProfileV2,
    snapshot: Mapping[str, Any],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    conflicts: List[Dict[str, Any]] = []
    unsupported: List[Dict[str, Any]] = list(snapshot.get("unsupported_items") or [])
    plot_environment = snapshot.get("plot_environment") or {}
    devices = plot_environment.get("available_devices")
    styles = plot_environment.get("available_style_sheets")
    required_devices = {item.device for item in profile.page_setups.values()}
    if profile.plot_policy.preferred_device:
        required_devices.add(profile.plot_policy.preferred_device)
    required_styles = set(profile.plot_policy.required_style_sheets)
    required_styles.update(item.plot_style_sheet for item in profile.page_setups.values())

    if devices is None:
        for name in sorted(required_devices):
            unsupported.append({
                "item": f"plot_device:{name}",
                "reason": "当前后端无法核验 AutoCAD 实际打印设备",
            })
    else:
        available = {str(item).casefold() for item in devices}
        for name in sorted(required_devices):
            if name.casefold() not in available:
                conflicts.append({
                    "item": f"plot_device:{name}",
                    "reason": "AutoCAD 实际设备列表中不存在",
                })

    if styles is None:
        for name in sorted(required_styles):
            unsupported.append({
                "item": f"plot_style_sheet:{name}",
                "reason": "当前后端无法核验 AutoCAD 实际打印样式表",
            })
    else:
        available = {str(item).casefold() for item in styles}
        for name in sorted(required_styles):
            if name.casefold() not in available:
                conflicts.append({
                    "item": f"plot_style_sheet:{name}",
                    "reason": "AutoCAD 实际打印样式列表中不存在",
                })

    media_by_device = plot_environment.get("media_by_device")
    for setup_name, setup in profile.page_setups.items():
        media = None
        if isinstance(media_by_device, Mapping):
            for device_name, values in media_by_device.items():
                if str(device_name).casefold() == setup.device.casefold():
                    media = values
                    break
        if media is None:
            unsupported.append({
                "item": f"page_setup:{setup_name}.media_name",
                "reason": (
                    f"只读预览无法在不切换图纸设备的前提下核验 {setup.device} 的纸张；"
                    "请先把该设备设为当前布局设备后重新预览"
                ),
            })
        elif setup.media_name.casefold() not in {str(item).casefold() for item in media}:
            conflicts.append({
                "item": f"page_setup:{setup_name}.media_name",
                "reason": f"设备 {setup.device} 的实际纸张列表中不存在 {setup.media_name}",
            })

    font_files = snapshot.get("font_files")
    required_fonts = {
        name
        for style in profile.text_styles.values()
        for name in (style.font_file, style.big_font_file)
        if name
    }
    if font_files is None:
        for name in sorted(required_fonts):
            unsupported.append({
                "item": f"font:{name}",
                "reason": "AutoCAD 字体搜索路径未能完整枚举；应用后仍需读回样式",
            })
    else:
        available = {str(item).casefold() for item in font_files}
        for name in sorted(required_fonts):
            if Path(name).name.casefold() not in available:
                conflicts.append({
                    "item": f"font:{name}",
                    "reason": "AutoCAD 支持路径中未找到该字体文件",
                })
    return conflicts, unsupported


def build_plan(
    profile: DraftingProfileV2,
    snapshot: Mapping[str, Any],
    *,
    profile_path: str,
    profile_sha256: str,
    target_drawing: Optional[str],
    update_existing: bool,
    preserve_unmanaged: bool,
) -> Dict[str, Any]:
    """Compare a profile with a read-only backend snapshot."""

    target = dict(snapshot.get("target") or {})
    assert_target(target, target_drawing)
    settings = snapshot.get("drawing_settings") or {}
    desired_drawing = profile.drawing_settings.model_dump(
        mode="python", exclude_none=True
    )
    drawing_fields = tuple(desired_drawing)
    drawing_changes = _changes(
        settings,
        desired_drawing,
        drawing_fields,
    )

    layers = _index((snapshot.get("layers") or {}).get("layers", []), "name")
    layers_create: List[Dict[str, Any]] = []
    layers_update: List[Dict[str, Any]] = []
    layers_rename: List[Dict[str, Any]] = []
    layers_delete: List[Dict[str, Any]] = []
    conflicts, unsupported = _resource_preflight(profile, snapshot)

    layer_fields = (
        "color", "linetype", "lineweight", "plot", "description",
        "locked", "frozen", "transparency",
    )
    for name, spec in profile.layer_styles.items():
        existing = layers.get(name.casefold())
        source = layers.get(spec.rename_from.casefold()) if spec.rename_from else None
        desired = spec.model_dump(mode="python", exclude_none=True)
        if spec.state == "absent":
            if existing is not None:
                layers_delete.append({
                    "name": name,
                    "entity_count": existing.get("entity_count"),
                    "require_empty": spec.require_empty,
                    "allow_nonempty": spec.allow_nonempty,
                })
                if name.casefold() in {"0", "defpoints"}:
                    conflicts.append({"item": f"layer:{name}", "reason": "保护图层不能删除"})
                elif existing.get("is_current"):
                    conflicts.append({"item": f"layer:{name}", "reason": "当前图层不能删除"})
                elif int(existing.get("entity_count") or 0) > 0 and not spec.allow_nonempty:
                    conflicts.append({
                        "item": f"layer:{name}",
                        "reason": "图层非空且 allow_nonempty=false",
                    })
            continue
        if spec.rename_from:
            if existing is not None and source is not None and existing is not source:
                conflicts.append({
                    "item": f"layer:{name}",
                    "reason": f"新旧图层同时存在，不能安全地把 {spec.rename_from} 改名为 {name}",
                })
            elif existing is None and source is not None:
                layers_rename.append({"old_name": spec.rename_from, "new_name": name})
                existing = source
        if existing is None:
            layers_create.append({"name": name, "desired": desired})
        else:
            managed_layer_fields = tuple(
                field for field in layer_fields if field in desired
            )
            item_changes = _changes(existing, desired, managed_layer_fields)
            if item_changes:
                if update_existing:
                    layers_update.append({"name": name, "changes": item_changes, "desired": desired})
                else:
                    conflicts.append({
                        "item": f"layer:{name}",
                        "reason": "已有图层与配置不同，update_existing=false",
                        "changes": item_changes,
                    })

    textstyles = _index((snapshot.get("textstyles") or {}).get("textstyles", []), "name")
    textstyle_actions: List[Dict[str, Any]] = []
    for name, spec in profile.text_styles.items():
        desired = spec.model_dump(mode="python", exclude_none=True)
        comparable = dict(desired)
        comparable.pop("current", None)
        if spec.current:
            comparable["is_current"] = True
        existing = textstyles.get(name.casefold())
        fields = tuple(comparable)
        item_changes = ([{"field": "missing", "before": None, "after": desired}]
                        if existing is None else _changes(
                            existing, comparable, fields, skip_unknown={"annotative"}
                        ))
        if item_changes:
            if existing is None or update_existing:
                textstyle_actions.append({
                    "name": name,
                    "action": "create" if existing is None else "update",
                    "changes": item_changes,
                    "desired": desired,
                })
            else:
                conflicts.append({
                    "item": f"textstyle:{name}",
                    "reason": "已有文字样式与配置不同，update_existing=false",
                    "changes": item_changes,
                })

    dimstyles = _index((snapshot.get("dimstyles") or {}).get("dimstyles", []), "name")
    dimstyle_actions: List[Dict[str, Any]] = []
    for name, spec in profile.dimension_styles.items():
        desired = spec.model_dump(mode="python")
        comparable = dict(desired)
        comparable.pop("current", None)
        if spec.current:
            comparable["is_current"] = True
        existing = dimstyles.get(name.casefold())
        fields = tuple(comparable)
        item_changes = ([{"field": "missing", "before": None, "after": desired}]
                        if existing is None else _changes(existing, comparable, fields))
        if item_changes:
            if existing is None or update_existing:
                dimstyle_actions.append({
                    "name": name,
                    "action": "create" if existing is None else "update",
                    "changes": item_changes,
                    "desired": desired,
                })
            else:
                conflicts.append({
                    "item": f"dimstyle:{name}",
                    "reason": "已有尺寸样式与配置不同，update_existing=false",
                    "changes": item_changes,
                })

    desired_linetype = profile.linetype_settings.model_dump(mode="python")
    linetype_changes = _changes(
        snapshot.get("linetype_settings") or {}, desired_linetype, desired_linetype
    )
    current_mode = settings.get("plot_style_mode")
    target_mode = profile.plot_policy.target_mode
    plot_mode_changes = []
    expected_mode = profile.plot_policy.expected_current_mode
    if expected_mode is not None and current_mode != expected_mode:
        conflicts.append({
            "item": "plot_style_mode",
            "reason": (
                f"expected_current_mode={expected_mode}，实际为 {current_mode}"
            ),
        })
    if not _same(current_mode, target_mode):
        plot_mode_changes.append({"before": current_mode, "after": target_mode})
        if not profile.plot_policy.allow_mode_conversion:
            conflicts.append({
                "item": "plot_style_mode",
                "reason": "配置禁止自动转换 CTB/STB 模式",
            })
        else:
            unsupported.append({
                "item": "plot_style_mode_conversion",
                "reason": "AutoCAD COM 无可靠、无对话框的 CTB/STB 转换接口",
            })

    layouts = _index((snapshot.get("layouts") or {}).get("layouts", []), "layout_name")
    layouts_create: List[Dict[str, Any]] = []
    layouts_update: List[Dict[str, Any]] = []
    layouts_rename: List[Dict[str, Any]] = []
    layouts_delete: List[Dict[str, Any]] = []
    paper_layout_count = sum(
        1 for item in layouts.values() if not bool(item.get("is_model"))
    )
    for name, spec in profile.layouts.items():
        existing = layouts.get(name.casefold())
        source = layouts.get(spec.rename_from.casefold()) if spec.rename_from else None
        if spec.state == "absent":
            if existing is not None:
                layouts_delete.append({
                    "name": name,
                    "require_empty": spec.require_empty,
                    "viewport_count": existing.get("viewport_count"),
                })
                if name.casefold() == "model":
                    conflicts.append({"item": f"layout:{name}", "reason": "保护布局 Model 不能删除"})
                elif paper_layout_count <= 1:
                    conflicts.append({
                        "item": f"layout:{name}",
                        "reason": "不能删除最后一个图纸空间布局",
                    })
                elif spec.require_empty and int(existing.get("user_content_count") or 0) > 0:
                    conflicts.append({
                        "item": f"layout:{name}",
                        "reason": "布局含用户视口或内容且 require_empty=true",
                    })
            continue
        if spec.rename_from:
            if existing is not None and source is not None and existing is not source:
                conflicts.append({
                    "item": f"layout:{name}",
                    "reason": f"新旧布局同时存在，不能安全地把 {spec.rename_from} 改名为 {name}",
                })
            elif existing is None and source is not None:
                layouts_rename.append({"old_name": spec.rename_from, "new_name": name})
                existing = source
        if existing is None:
            layouts_create.append({"layout_name": name, "page_setup": spec.page_setup})
        elif spec.page_setup and not _same(existing.get("page_setup"), spec.page_setup):
            if update_existing:
                layouts_update.append({
                    "layout_name": name,
                    "field": "page_setup",
                    "before": existing.get("page_setup"),
                    "after": spec.page_setup,
                })
            else:
                conflicts.append({
                    "item": f"layout:{name}",
                    "reason": "已有布局的页面设置不同，update_existing=false",
                })

    page_setups = _index((snapshot.get("page_setups") or {}).get("page_setups", []), "name")
    page_setup_actions: List[Dict[str, Any]] = []
    for name, spec in profile.page_setups.items():
        desired = spec.model_dump(mode="python", exclude={"replace"})
        existing = page_setups.get(name.casefold())
        item_changes = ([{"field": "missing", "before": None, "after": desired}]
                        if existing is None else _changes(existing, desired, desired))
        if item_changes:
            if existing is None or update_existing or spec.replace:
                page_setup_actions.append({
                    "name": name,
                    "action": "create" if existing is None else "update",
                    "replace": bool(spec.replace),
                    "changes": item_changes,
                    "desired": desired,
                })
            else:
                conflicts.append({
                    "item": f"page_setup:{name}",
                    "reason": "已有页面设置与配置不同，update_existing=false",
                    "changes": item_changes,
                })

    existing_blocks = {str(name).casefold() for name in snapshot.get("block_names") or []}
    block_actions: List[Dict[str, Any]] = []
    for source in profile.block_library.sources:
        names = []
        skipped = []
        for name in source.block_names:
            if name.casefold() in existing_blocks and not source.replace_existing:
                skipped.append({"name": name, "reason": "同名块已存在且 replace_existing=false"})
            else:
                names.append(name)
        if source.import_on_apply and names:
            block_actions.append({
                "source_path": source.source_path,
                "expected_sha256": source.expected_sha256,
                "block_names": names,
                "replace_existing": source.replace_existing,
                "skipped": skipped,
            })

    plan: Dict[str, Any] = {
        "profile_path": str(Path(profile_path).resolve()),
        "profile_sha256": profile_sha256,
        "profile_id": profile.profile_id,
        "schema_version": profile.schema_version,
        "target": target,
        "snapshot_hash": snapshot_hash(snapshot),
        "update_existing": update_existing,
        "preserve_unmanaged": preserve_unmanaged,
        "drawing_settings_changes": drawing_changes,
        "layers_to_create": layers_create,
        "layers_to_update": layers_update,
        "layers_to_rename": layers_rename,
        "layers_to_delete": layers_delete,
        "textstyles_to_create_or_update": textstyle_actions,
        "dimstyles_to_create_or_update": dimstyle_actions,
        "linetype_settings_changes": linetype_changes,
        "plot_mode_changes": plot_mode_changes,
        "layouts_to_create": layouts_create,
        "layouts_to_update": layouts_update,
        "layouts_to_rename": layouts_rename,
        "layouts_to_delete": layouts_delete,
        "page_setups_to_create_or_update": page_setup_actions,
        "block_library_actions": block_actions,
        "conflicts": conflicts,
        "unsupported_items": unsupported,
    }
    mutating_keys = (
        "drawing_settings_changes", "layers_to_create", "layers_to_update",
        "layers_to_rename", "layers_to_delete", "textstyles_to_create_or_update",
        "dimstyles_to_create_or_update", "linetype_settings_changes",
        "plot_mode_changes", "layouts_to_create", "layouts_to_update",
        "layouts_to_rename", "layouts_to_delete",
        "page_setups_to_create_or_update", "block_library_actions",
    )
    plan["confirmation_required"] = any(plan[key] for key in mutating_keys)
    hashable = dict(plan)
    plan["plan_hash"] = content_hash(hashable)
    return plan


def _prune_previews(now: float) -> None:
    expired = [
        token for token, record in _PREVIEWS.items()
        if now - record.created_at > _PREVIEW_TTL_SECONDS
    ]
    for token in expired:
        _PREVIEWS.pop(token, None)
    if len(_PREVIEWS) > _MAX_PREVIEWS:
        oldest = sorted(_PREVIEWS.values(), key=lambda item: item.created_at)
        for record in oldest[: len(_PREVIEWS) - _MAX_PREVIEWS]:
            _PREVIEWS.pop(record.token, None)


def register_confirmation(plan: Mapping[str, Any]) -> str:
    now = time.time()
    token_seed = {
        "profile_sha256": plan["profile_sha256"],
        "target": target_key(plan["target"]),
        "snapshot_hash": plan["snapshot_hash"],
        "plan_hash": plan["plan_hash"],
        "update_existing": plan["update_existing"],
        "preserve_unmanaged": plan["preserve_unmanaged"],
    }
    token = "dwt-" + content_hash(token_seed)[:32]
    record = PreviewRecord(
        token=token,
        created_at=now,
        profile_path=str(plan["profile_path"]),
        profile_sha256=str(plan["profile_sha256"]),
        target_key=target_key(plan["target"]),
        snapshot_hash=str(plan["snapshot_hash"]),
        plan_hash=str(plan["plan_hash"]),
        update_existing=bool(plan["update_existing"]),
        preserve_unmanaged=bool(plan["preserve_unmanaged"]),
    )
    with _PREVIEW_LOCK:
        _prune_previews(now)
        _PREVIEWS[token] = record
    return token


def verify_confirmation(
    token: Optional[str],
    plan: Mapping[str, Any],
) -> PreviewRecord:
    if not token:
        raise ConfirmationRequiredError("正式应用 DWT 配置前缺少 confirmation_token")
    now = time.time()
    with _PREVIEW_LOCK:
        _prune_previews(now)
        record = _PREVIEWS.get(token)
    if record is None:
        raise ConfirmationRequiredError("confirmation_token 不存在或已超过 30 分钟有效期")
    mismatches: Dict[str, Any] = {}
    expected = {
        "profile_path": str(plan["profile_path"]),
        "profile_sha256": str(plan["profile_sha256"]),
        "target_key": target_key(plan["target"]),
        "snapshot_hash": str(plan["snapshot_hash"]),
        "plan_hash": str(plan["plan_hash"]),
        "update_existing": bool(plan["update_existing"]),
        "preserve_unmanaged": bool(plan["preserve_unmanaged"]),
    }
    for field, value in expected.items():
        if getattr(record, field) != value:
            mismatches[field] = {"preview": getattr(record, field), "current": value}
    if mismatches:
        raise ConflictError(
            "预览后配置、目标图纸或图纸状态已变化，原 confirmation_token 已失效",
            details={"mismatches": mismatches},
        )
    if plan.get("conflicts") or plan.get("unsupported_items"):
        raise ConflictError(
            "计划仍含冲突或未支持项，不能正式应用",
            details={
                "conflicts": plan.get("conflicts") or [],
                "unsupported_items": plan.get("unsupported_items") or [],
            },
        )
    return record


def attach_confirmation(plan: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(plan)
    if (
        result.get("confirmation_required")
        and not result.get("conflicts")
        and not result.get("unsupported_items")
    ):
        result["confirmation_token"] = register_confirmation(result)
        result["confirmation_expires_in_seconds"] = _PREVIEW_TTL_SECONDS
    else:
        result["confirmation_token"] = None
    return result


def validate_snapshot(
    profile: DraftingProfileV2,
    snapshot: Mapping[str, Any],
    *,
    preserve_unmanaged: bool = True,
) -> Dict[str, Any]:
    """Produce an evidence-bounded validation report, never filling unknowns."""

    matched: List[str] = []
    mismatched: List[Dict[str, Any]] = []
    missing: List[str] = []
    unverified: List[str] = []
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    def compare(prefix: str, actual: Mapping[str, Any], desired: Mapping[str, Any]):
        for field, expected in desired.items():
            key = f"{prefix}.{field}"
            if field not in actual or actual.get(field) is None:
                unverified.append(key)
            elif _same(actual.get(field), expected):
                matched.append(key)
            else:
                item = {"setting": key, "expected": expected, "actual": actual.get(field)}
                mismatched.append(item)
                errors.append({"code": "MISMATCH", **item})

    compare(
        "drawing_settings",
        snapshot.get("drawing_settings") or {},
        profile.drawing_settings.model_dump(mode="python", exclude_none=True),
    )
    compare(
        "linetype_settings",
        snapshot.get("linetype_settings") or {},
        profile.linetype_settings.model_dump(mode="python"),
    )

    layers = _index((snapshot.get("layers") or {}).get("layers", []), "name")
    layer_fields = (
        "color", "linetype", "lineweight", "plot", "description",
        "locked", "frozen", "transparency",
    )
    for name, spec in profile.layer_styles.items():
        actual = layers.get(name.casefold())
        if spec.state == "absent":
            if actual is None:
                matched.append(f"layer_styles.{name}.absent")
            else:
                item = {"setting": f"layer_styles.{name}", "expected": "absent", "actual": "present"}
                mismatched.append(item)
                errors.append({"code": "UNEXPECTED_PRESENT", **item})
            continue
        if actual is None:
            key = f"layer_styles.{name}"
            missing.append(key)
            errors.append({"code": "MISSING", "setting": key})
        else:
            compare(
                f"layer_styles.{name}", actual,
                spec.model_dump(
                    mode="python", include=set(layer_fields), exclude_none=True
                ),
            )

    textstyles = _index((snapshot.get("textstyles") or {}).get("textstyles", []), "name")
    for name, spec in profile.text_styles.items():
        actual = textstyles.get(name.casefold())
        if actual is None:
            key = f"text_styles.{name}"
            missing.append(key)
            errors.append({"code": "MISSING", "setting": key})
        else:
            desired = spec.model_dump(mode="python", exclude_none=True)
            desired.pop("current", None)
            if spec.current:
                desired["is_current"] = True
            compare(f"text_styles.{name}", actual, desired)

    dimstyles = _index((snapshot.get("dimstyles") or {}).get("dimstyles", []), "name")
    for name, spec in profile.dimension_styles.items():
        actual = dimstyles.get(name.casefold())
        if actual is None:
            key = f"dimension_styles.{name}"
            missing.append(key)
            errors.append({"code": "MISSING", "setting": key})
        else:
            desired = spec.model_dump(mode="python")
            desired.pop("current", None)
            if spec.current:
                desired["is_current"] = True
            compare(f"dimension_styles.{name}", actual, desired)

    layouts = _index((snapshot.get("layouts") or {}).get("layouts", []), "layout_name")
    for name, spec in profile.layouts.items():
        actual = layouts.get(name.casefold())
        if spec.state == "absent":
            if actual is None:
                matched.append(f"layouts.{name}.absent")
            else:
                item = {"setting": f"layouts.{name}", "expected": "absent", "actual": "present"}
                mismatched.append(item)
                errors.append({"code": "UNEXPECTED_PRESENT", **item})
        elif actual is None:
            key = f"layouts.{name}"
            missing.append(key)
            errors.append({"code": "MISSING", "setting": key})
        elif spec.page_setup:
            compare(f"layouts.{name}", actual, {"page_setup": spec.page_setup})

    page_setups = _index((snapshot.get("page_setups") or {}).get("page_setups", []), "name")
    for name, spec in profile.page_setups.items():
        actual = page_setups.get(name.casefold())
        if actual is None:
            key = f"page_setups.{name}"
            missing.append(key)
            errors.append({"code": "MISSING", "setting": key})
        else:
            compare(
                f"page_setups.{name}", actual,
                spec.model_dump(mode="python", exclude={"replace"}),
            )

    actual_mode = (snapshot.get("drawing_settings") or {}).get("plot_style_mode")
    if actual_mode is None:
        unverified.append("plot_policy.target_mode")
    elif _same(actual_mode, profile.plot_policy.target_mode):
        matched.append("plot_policy.target_mode")
    else:
        item = {
            "setting": "plot_policy.target_mode",
            "expected": profile.plot_policy.target_mode,
            "actual": actual_mode,
        }
        mismatched.append(item)
        errors.append({"code": "MISMATCH", **item})

    block_names = {str(name).casefold() for name in snapshot.get("block_names") or []}
    for source in profile.block_library.sources:
        for name in source.block_names:
            if name.casefold() in block_names:
                matched.append(f"block_library.{name}")
            else:
                key = f"block_library.{name}"
                missing.append(key)
                errors.append({"code": "MISSING", "setting": key})

    known_layer_names = {name.casefold() for name in profile.layer_styles}
    known_text_names = {name.casefold() for name in profile.text_styles}
    known_dim_names = {name.casefold() for name in profile.dimension_styles}
    known_layout_names = {name.casefold() for name in profile.layouts}
    known_page_names = {name.casefold() for name in profile.page_setups}
    extra = {
        "layers": sorted(item["name"] for item in layers.values() if item["name"].casefold() not in known_layer_names),
        "textstyles": sorted(item["name"] for item in textstyles.values() if item["name"].casefold() not in known_text_names),
        "dimstyles": sorted(item["name"] for item in dimstyles.values() if item["name"].casefold() not in known_dim_names),
        "layouts": sorted(item["layout_name"] for item in layouts.values() if item["layout_name"].casefold() not in known_layout_names),
        "page_setups": sorted(item["name"] for item in page_setups.values() if item["name"].casefold() not in known_page_names),
    }
    if preserve_unmanaged and any(extra.values()):
        warnings.append({
            "code": "EXTRA_UNMANAGED",
            "message": "存在配置外对象；preserve_unmanaged=true，因此仅报告、不删除",
        })
    for section in (
        snapshot.get("drawing_settings") or {},
        snapshot.get("layers") or {},
        snapshot.get("textstyles") or {},
        snapshot.get("dimstyles") or {},
        snapshot.get("layouts") or {},
        snapshot.get("page_setups") or {},
        snapshot.get("plot_environment") or {},
    ):
        unverified.extend(str(item) for item in section.get("unverified_fields", []) or [])
    for item in snapshot.get("unsupported_items") or []:
        warnings.append({"code": "UNSUPPORTED", **dict(item)})
    if unverified:
        warnings.append({
            "code": "UNVERIFIED",
            "message": "部分属性无法从当前后端可靠读取，未猜测其值",
            "count": len(set(unverified)),
        })
    return {
        "pass": not errors,
        "errors": errors,
        "warnings": warnings,
        "matched_settings": sorted(set(matched)),
        "mismatched_settings": mismatched,
        "missing_settings": sorted(set(missing)),
        "extra_unmanaged_settings": extra,
        "unverified_settings": sorted(set(unverified)),
        "target": dict(snapshot.get("target") or {}),
        "snapshot_hash": snapshot_hash(snapshot),
    }
