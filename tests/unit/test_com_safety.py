import pythoncom
import pytest

from cad_mcp.drawing import autocad as cad_acad
from cad_mcp.drawing.autocad import AcadBackend, _ComRetryProxy
from cad_mcp.server import _AcadLockProxy


class _BusyPropertyTarget:
    def __init__(self):
        self.attempts = 0
        self._value = None

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, new_value):
        self.attempts += 1
        if self.attempts < 3:
            raise pythoncom.com_error(-2147417846, "busy", None, None)
        self._value = new_value


def test_com_property_write_uses_retry(monkeypatch):
    target = _BusyPropertyTarget()
    proxy = _ComRetryProxy(target)
    monkeypatch.setattr(cad_acad.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(cad_acad.pythoncom, "PumpWaitingMessages", lambda: None)

    proxy.value = 42

    assert target.value == 42
    assert target.attempts == 3


def test_operation_readiness_error_is_not_silently_accepted(tmp_path):
    backend = AcadBackend(tmp_path)

    class BrokenAcad:
        def GetAcadState(self):
            raise RuntimeError("state unavailable")

    backend.acad = BrokenAcad()

    with pytest.raises(RuntimeError, match="无法确认空闲状态"):
        backend._wait_operation_ready(timeout=0)


def test_health_bypasses_readiness_guard_but_keeps_operation_lock():
    events = []

    class Backend:
        def _acquire_autocad_lock(self):
            events.append("acquire")

        def _wait_operation_ready(self):
            raise AssertionError("health must reach its own connection probe")

        def _commit_undo(self, started):
            events.append(("commit", started))

        def _rollback_undo(self, started):
            events.append(("rollback", started))

        def _release_autocad_lock(self):
            events.append("release")

        def health(self, reconnect=False):
            events.append(("health", reconnect))
            return {"connected": True}

    result = _AcadLockProxy(Backend()).health(reconnect=True)

    assert result == {"connected": True}
    assert events == [
        "acquire",
        ("health", True),
        ("commit", False),
        "release",
    ]


def test_undo_group_failure_stops_write_before_mutation(tmp_path):
    backend = AcadBackend(tmp_path)

    class BrokenDocument:
        def StartUndoMark(self):
            raise RuntimeError("undo unavailable")

    backend.doc = BrokenDocument()

    with pytest.raises(RuntimeError, match="已停止本次写入操作"):
        backend._begin_undo()

    assert backend._undo_mark_open is False


def test_lock_proxy_does_not_call_mutation_when_undo_start_fails():
    events = []

    class Backend:
        def _acquire_autocad_lock(self):
            events.append("acquire")

        def _wait_operation_ready(self):
            events.append("ready")

        def _begin_undo(self):
            events.append("begin")
            raise RuntimeError("undo unavailable")

        def _rollback_undo(self, started):
            events.append(("rollback", started))

        def _release_autocad_lock(self):
            events.append("release")

        def draw_line(self):
            events.append("draw")

    with pytest.raises(RuntimeError, match="undo unavailable"):
        _AcadLockProxy(Backend()).draw_line()

    assert "draw" not in events
    assert events == ["acquire", "ready", "begin", ("rollback", False), "release"]


def test_rollback_failure_is_recorded(monkeypatch, tmp_path):
    backend = AcadBackend(tmp_path)
    recorded = []

    class BrokenDocument:
        def EndUndoMark(self):
            raise RuntimeError("rollback failed")

    backend.doc = BrokenDocument()
    backend._undo_mark_open = True
    monkeypatch.setattr(
        cad_acad,
        "record_error",
        lambda exc, operation: recorded.append((str(exc), operation)),
    )

    backend._rollback_undo(True)

    assert backend._undo_mark_open is False
    assert recorded == [("rollback failed", "acad_rollback_undo")]
