import json
from pathlib import Path

import pytest

from cad_mcp.drawing.document import CadDocument
from cad_mcp.dwt.contracts import DraftingProfileV2
from cad_mcp.dwt.engine import (
    attach_confirmation,
    build_plan,
    validate_snapshot,
    verify_confirmation,
)
from cad_mcp.core.runtime import ConflictError, UnsupportedModeError
from cad_mcp.server import _AcadLockProxy
from cad_mcp import server as server


def profile_payload(**drawing_settings):
    return {
        "schema_version": "2.0",
        "profile_id": "generic-v2",
        "name": "通用 DWT",
        "drawing_settings": drawing_settings,
    }


def exact_snapshot(tmp_path):
    return {
        "target": {
            "mode": "acad",
            "path": str((tmp_path / "template.dwg").resolve()),
            "name": "template.dwg",
        },
        "drawing_settings": {
            "insertion_units": "millimeters",
            "measurement_system": "metric",
            "linear_unit_format": "decimal",
            "linear_precision": 2,
            "angular_unit_format": "decimal_degrees",
            "angular_precision": 2,
            "associative_dimensions": True,
            "mirror_text": False,
            "display_lineweights": True,
            "plot_style_mode": "ctb",
            "current_layer": "0",
            "current_layout": "Model",
            "unverified_fields": [],
        },
        "layers": {"layers": [], "unverified_fields": []},
        "textstyles": {"textstyles": [], "unverified_fields": []},
        "dimstyles": {"dimstyles": [], "unverified_fields": []},
        "linetype_settings": {
            "ltscale": 1.0,
            "celtscale": 1.0,
            "msltscale": 0,
            "psltscale": 1,
        },
        "layouts": {"layouts": [], "unverified_fields": []},
        "page_setups": {"page_setups": [], "unverified_fields": []},
        "plot_environment": {
            "available_devices": [],
            "available_style_sheets": [],
            "media_by_device": {},
            "unverified_fields": [],
        },
        "block_names": [],
        "font_files": [],
        "unsupported_items": [],
    }


def build(profile, snapshot, tmp_path):
    return build_plan(
        profile,
        snapshot,
        profile_path=str((tmp_path / "profile.json").resolve()),
        profile_sha256="a" * 64,
        target_drawing="template.dwg",
        update_existing=True,
        preserve_unmanaged=True,
    )


def test_exact_profile_plan_is_idempotent_noop(tmp_path):
    profile = DraftingProfileV2.model_validate(profile_payload())
    plan = build(profile, exact_snapshot(tmp_path), tmp_path)

    assert plan["confirmation_required"] is False
    assert plan["drawing_settings_changes"] == []
    assert plan["linetype_settings_changes"] == []
    assert plan["conflicts"] == []
    assert plan["unsupported_items"] == []
    assert attach_confirmation(plan)["confirmation_token"] is None


def test_confirmation_token_is_bound_to_snapshot_and_options(tmp_path):
    profile = DraftingProfileV2.model_validate(
        profile_payload(linear_precision=3)
    )
    snapshot = exact_snapshot(tmp_path)
    plan = attach_confirmation(build(profile, snapshot, tmp_path))

    assert plan["confirmation_required"] is True
    assert plan["confirmation_token"].startswith("dwt-")
    verify_confirmation(plan["confirmation_token"], plan)

    changed = exact_snapshot(tmp_path)
    changed["drawing_settings"]["angular_precision"] = 4
    changed_plan = build(profile, changed, tmp_path)
    with pytest.raises(ConflictError, match="已变化"):
        verify_confirmation(plan["confirmation_token"], changed_plan)


def test_plan_target_guard_stops_wrong_drawing(tmp_path):
    profile = DraftingProfileV2.model_validate(profile_payload())
    snapshot = exact_snapshot(tmp_path)

    with pytest.raises(ConflictError, match="target_drawing"):
        build_plan(
            profile,
            snapshot,
            profile_path=str((tmp_path / "profile.json").resolve()),
            profile_sha256="b" * 64,
            target_drawing="other.dwg",
            update_existing=True,
            preserve_unmanaged=True,
        )


def test_validate_profile_separates_mismatch_missing_extra_and_unverified(tmp_path):
    payload = profile_payload(linear_precision=3)
    payload["layer_styles"] = {
        "REQUIRED": {"color": 2, "linetype": "CONTINUOUS"}
    }
    profile = DraftingProfileV2.model_validate(payload)
    snapshot = exact_snapshot(tmp_path)
    snapshot["layers"] = {
        "layers": [{"name": "UNMANAGED", "color": 7}],
        "unverified_fields": ["layers.UNMANAGED.description"],
    }

    report = validate_snapshot(profile, snapshot, preserve_unmanaged=True)

    assert report["pass"] is False
    assert any(item["setting"] == "drawing_settings.linear_precision" for item in report["mismatched_settings"])
    assert "layer_styles.REQUIRED" in report["missing_settings"]
    assert report["extra_unmanaged_settings"]["layers"] == ["UNMANAGED"]
    assert "layers.UNMANAGED.description" in report["unverified_settings"]


def test_dxf_preview_and_formal_apply_are_zero_write_and_explicit(monkeypatch, tmp_path):
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps(profile_payload(linear_precision=3), ensure_ascii=False),
        encoding="utf-8",
    )
    backend = CadDocument(tmp_path)
    backend.new_drawing("target.dxf")
    monkeypatch.setattr(server, "_backend", backend)
    before = len(backend.doc.entitydb)

    preview = json.loads(server.cad_preview_dwt_profile(
        server.PreviewDwtProfileInput(
            profile_path=str(profile_path), target_drawing="target.dxf"
        )
    ))
    formal = json.loads(server.cad_apply_dwt_profile(
        server.ApplyDwtProfileInput(
            profile_path=str(profile_path), target_drawing="target.dxf",
            dry_run=False, confirmation_token="dwt-not-valid",
        )
    ))

    assert preview["ok"] is True
    assert preview["confirmation_token"] is None
    assert preview["unsupported_items"]
    assert formal["ok"] is False
    assert formal["error_detail"]["code"] == "UNSUPPORTED_MODE"
    assert len(backend.doc.entitydb) == before


def test_profile_apply_failure_is_rolled_back_by_one_proxy_undo_group():
    events = []

    class Backend:
        def _acquire_autocad_lock(self):
            events.append("acquire")

        def _wait_operation_ready(self):
            events.append("ready")

        def _begin_undo(self):
            events.append("begin")
            return True

        def _commit_undo(self, started):
            events.append(("commit", started))

        def _rollback_undo(self, started):
            events.append(("rollback", started))
            return True

        def _release_autocad_lock(self):
            events.append("release")

        def apply_dwt_profile(self):
            events.append("apply")
            raise ConflictError("mid-plan failure")

    with pytest.raises(ConflictError) as caught:
        _AcadLockProxy(Backend()).apply_dwt_profile()

    assert events == ["acquire", "ready", "begin", "apply", ("rollback", True), "release"]
    assert caught.value.rollback_status == "failed_or_unverified"


def test_destructive_dry_run_skips_undo_group():
    events = []

    class Backend:
        def _acquire_autocad_lock(self):
            events.append("acquire")

        def _wait_operation_ready(self):
            events.append("ready")

        def _begin_undo(self):
            events.append("begin")
            return True

        def _commit_undo(self, started):
            events.append(("commit", started))

        def _rollback_undo(self, started):
            events.append(("rollback", started))

        def _release_autocad_lock(self):
            events.append("release")

        def delete_layer(self, names, *, dry_run):
            events.append(("delete_layer", tuple(names), dry_run))
            return {"dry_run": dry_run}

    result = _AcadLockProxy(Backend()).delete_layer(["X"], dry_run=True)

    assert result == {"dry_run": True}
    assert "begin" not in events
    assert events == [
        "acquire", "ready", ("delete_layer", ("X",), True),
        ("commit", False), "release",
    ]
