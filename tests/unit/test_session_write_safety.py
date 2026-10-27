"""Regressions for retained sessions and uniform uncertain-write handling."""

import asyncio
import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import ezdxf
import pytest
from pydantic import ValidationError

from cad_mcp import server
from cad_mcp.core.runtime import failure_json, get_logger
from cad_mcp.core.write_safety import protect_writes, uncertain_result
from cad_mcp.drawing.document import CadDocument


def test_sessions_retain_unsaved_geometry_styles_and_revision(tmp_path):
    cad = CadDocument(tmp_path)
    first = cad.new_drawing("first.dxf")["drawing_id"]
    cad.draw_line(0, 0, 10, 0)
    cad.active_textstyle = "first-style"
    cad._environment_revision = "first-revision"
    second = cad.new_drawing("second.dxf")["drawing_id"]
    cad.draw_circle(0, 0, 5)
    assert first != second and len(cad.list_drawings()) == 2
    assert len(ezdxf.readfile(tmp_path / "first.dxf").modelspace()) == 0
    cad.activate_drawing(first)
    assert len(cad.msp) == 1
    assert cad.active_textstyle == "first-style"
    assert cad._environment_revision == "first-revision"
    cad.open_drawing(str(tmp_path / "second.dxf"))
    assert cad.drawing_id == second and len(cad.msp) == 1
    cad.open_drawing("first.dxf")
    assert cad.new_drawing("first.dxf")["drawing_id"] == first
    assert len(cad.msp) == 1 and len(cad.list_drawings()) == 2
    cad.close_drawing(save=True)
    assert cad.drawing_id == second and len(cad.msp) == 1
    assert len(ezdxf.readfile(tmp_path / "first.dxf").modelspace()) == 1
    cad.close_drawing(save=False)
    assert cad.doc is None and cad.list_drawings() == []
    assert len(ezdxf.readfile(tmp_path / "second.dxf").modelspace()) == 0


def test_session_paths_and_failed_open_save_preserve_other_sessions(tmp_path, monkeypatch):
    cad = CadDocument(tmp_path)
    first = cad.new_drawing("first.dxf")["drawing_id"]
    cad.draw_line(0, 0, 10, 0)
    second = cad.new_drawing("second.dxf")["drawing_id"]
    with pytest.raises(ValueError, match="另一张"):
        cad.save_as("first.dxf", overwrite=True)
    assert cad.drawing_id == second
    bad = tmp_path / "bad.dxf"
    bad.write_text("not a drawing", encoding="utf-8")
    with pytest.raises((ezdxf.DXFError, OSError)):
        cad.open_drawing("bad.dxf")
    assert cad.drawing_id == second and len(cad.list_drawings()) == 2
    cad.activate_drawing(first)
    monkeypatch.setattr(cad.doc, "saveas", lambda path: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        cad.close_drawing(save=True)
    assert cad.drawing_id == first and len(cad.msp) == 1
    assert len(cad.list_drawings()) == 2


def test_session_tools_and_gateway_use_retained_memory(tmp_path, monkeypatch):
    cad = CadDocument(tmp_path)
    monkeypatch.setattr(server, "_backend", cad)
    first = json.loads(server.cad_new_drawing(server.NewDrawingInput(filename="first.dxf")))["drawing_id"]
    cad.draw_line(0, 0, 10, 0)
    server.cad_new_drawing(server.NewDrawingInput(filename="second.dxf"))
    result = json.loads(asyncio.run(server._gateway_call(server.GatewayCallParams(
        tool_name="cad_activate_drawing", arguments={"params": {"drawing_id": first}}))))
    assert result["ok"] and result["drawing_id"] == first and len(cad.msp) == 1
    listing = json.loads(server.cad_list_drawings(server.ListDrawingsInput()))
    assert len(listing["drawings"]) == 2
    switched = json.loads(server.cad_switch_mode(server.SwitchModeInput(mode="acad")))
    assert not switched["ok"]


@pytest.mark.parametrize("status", ["failed_or_unverified", "not_verified", "failed", "unknown"])
def test_returned_unknown_rollback_blocks_all_mutators_and_gateway(tmp_path, monkeypatch, status):
    cad = CadDocument(tmp_path)
    cad.new_drawing("first.dxf")
    monkeypatch.setattr(server, "_backend", cad)

    def cad_simulated(params):
        return json.dumps({"ok": False, "error_detail": {"code": "INTERNAL_ERROR",
                           "details": {"rollback_status": status}}})

    protect_writes(cad_simulated, lambda: cad)(SimpleNamespace())
    for fn, params in [(server.cad_save_drawing, server.SaveDrawingInput()),
                       (server.cad_close_drawing, server.CloseDrawingInput(save=False)),
                       (server.cad_new_drawing, server.NewDrawingInput(filename="blocked.dxf")),
                       (server.cad_switch_mode, server.SwitchModeInput(mode="acad")),
                       (server.cad_quit, server.QuitInput())]:
        result = json.loads(fn(params))
        assert not result["ok"] and result["error_detail"]["retryable"] is False
    result = json.loads(asyncio.run(server._gateway_call(server.GatewayCallParams(
        tool_name="cad_save_drawing", arguments={"params": {}}))))
    assert not result["ok"]
    assert json.loads(server.cad_list_drawings(server.ListDrawingsInput()))["ok"]
    assert cad.doc is not None and not (tmp_path / "blocked.dxf").exists()


def test_verified_outer_rollback_supersedes_unknown_inner_result():
    assert not uncertain_result({"ok": False, "semantic_rollback_status": "succeeded",
                                "error_detail": {"code": "EXECUTION_UNKNOWN"}})


def test_swallowed_transport_uncertainty_still_blocks_writes(tmp_path):
    import pythoncom
    from cad_mcp.drawing.autocad import _ComRetryProxy
    cad = CadDocument(tmp_path)
    cad.new_drawing("state.dxf")
    actions = []
    class Target:
        def Mutate(self):
            actions.append("applied")
            raise pythoncom.com_error(-2147417825, "timeout", None, None)
    def cad_swallow(params):
        try:
            _ComRetryProxy(Target()).Mutate()
        except Exception:
            pass
        return json.dumps({"ok": True})
    result = json.loads(protect_writes(cad_swallow, lambda: cad)(SimpleNamespace()))
    assert not result["ok"] and result["error_detail"]["code"] == "EXECUTION_UNKNOWN"
    assert cad._semantic_write_blocked and actions == ["applied"]


def test_errors_and_tracebacks_do_not_log_input(tmp_path):
    from logging.handlers import RotatingFileHandler
    marker = "PRIVATE_PAYLOAD_794136"
    stream = io.StringIO()
    handlers = [logging.StreamHandler(stream), RotatingFileHandler(
        tmp_path / "privacy.log", maxBytes=500, backupCount=2, encoding="utf-8")]
    logger = get_logger()
    for handler in handlers:
        logger.addHandler(handler)
    try:
        for _ in range(6):
            try:
                raise ValueError(marker)
            except ValueError as cause:
                try:
                    raise RuntimeError(marker) from cause
                except RuntimeError as exc:
                    failure_json(exc, "cad_test_privacy")
        try:
            server.DrawLineInput(x1=marker, y1=0, x2=10, y2=0)
        except ValidationError as exc:
            result = json.loads(failure_json(exc, "cad_draw_line"))
            assert result["error_detail"]["code"] == "INVALID_ARGUMENT"
        # Unknown gateway tool names are also untrusted user input.
        failure_json(ValueError(marker), marker)
        for handler in handlers:
            handler.flush()
        logged = stream.getvalue() + "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("privacy.log*"))
        assert marker not in logged
        assert "INVALID_ARGUMENT" in logged and "x1" in logged and "cad-" in logged
        assert "RuntimeError" in logged and "stack=" in logged
    finally:
        for handler in handlers:
            logger.removeHandler(handler)
            handler.close()


def test_packaged_example_passes_real_validation_and_offline_preview(monkeypatch):
    example = Path(__file__).resolve().parents[2] / "skills/cad-mcp/examples/environment-model.json"
    payload = {"model": json.loads(example.read_text(encoding="utf-8"))}
    monkeypatch.setattr(server, "_get_backend", lambda: (_ for _ in ()).throw(AssertionError("must stay offline")))
    validated = json.loads(server.cad_validate_environment_model(
        server.ValidateEnvironmentModelInput.model_validate(payload)))
    assert validated["ok"], validated
    preview = json.loads(server.cad_preview_environment_drawing(
        server.PreviewEnvironmentDrawingInput.model_validate(payload | {"include_operations": True})))
    assert preview["ok"] and preview["operation_count"] > 0, preview
