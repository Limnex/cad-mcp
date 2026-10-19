import json
from logging.handlers import RotatingFileHandler

import pytest

from cad_mcp.core.runtime import (
    LOG_BACKUP_COUNT,
    LOG_MAX_BYTES,
    failure_json,
    get_logger,
    get_recent_error_id,
    setup_logging,
    success_json,
)


def test_success_response_keeps_existing_payload_shape(tmp_path):
    setup_logging(tmp_path)
    payload = {"ok": True, "mode": "dxf", "value": 3}

    assert json.loads(success_json(payload)) == payload


def test_success_response_rejects_non_finite_output():
    with pytest.raises(ValueError):
        success_json({"ok": True, "value": float("nan")})


def test_failure_response_is_structured_and_traceable(tmp_path):
    setup_logging(tmp_path)

    response = json.loads(failure_json(ValueError("bad radius"), "cad_draw_circle"))

    assert response["ok"] is False
    assert response["error"] == "bad radius"
    assert response["error_detail"]["code"] == "INVALID_ARGUMENT"
    assert response["error_detail"]["operation"] == "cad_draw_circle"
    assert response["error_detail"]["retryable"] is False
    assert response["error_detail"]["request_id"].startswith("cad-")
    assert get_recent_error_id() == response["error_detail"]["request_id"]


def test_timeout_error_is_marked_retryable(tmp_path):
    setup_logging(tmp_path)

    response = json.loads(failure_json(TimeoutError("timed out"), "cad_save_drawing"))

    assert response["error_detail"]["code"] == "BACKEND_BUSY"
    assert response["error_detail"]["retryable"] is True


def test_tool_failure_uses_structured_runtime_response():
    from cad_mcp.server import DrawLineInput, cad_draw_line

    response = json.loads(
        cad_draw_line(DrawLineInput(x1=0, y1=0, x2=10, y2=10))
    )

    assert response["ok"] is False
    assert response["error_detail"]["code"] == "PRECONDITION_FAILED"
    assert response["error_detail"]["operation"] == "cad_draw_line"


def test_rotating_log_limits_are_bounded(tmp_path):
    setup_logging(tmp_path)
    handlers = [
        handler
        for handler in get_logger().handlers
        if isinstance(handler, RotatingFileHandler)
    ]

    assert handlers
    assert handlers[0].maxBytes == LOG_MAX_BYTES
    assert handlers[0].backupCount == LOG_BACKUP_COUNT
