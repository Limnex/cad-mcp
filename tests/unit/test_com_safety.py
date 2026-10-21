from types import SimpleNamespace

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


class _IncompleteMemberOle:
    def __init__(self, *, lookup_hresult=0x80010001, metadata_hresult=None):
        self.lookup_hresult = lookup_hresult
        self.metadata_hresult = metadata_hresult
        self.lookup_attempts = 0
        self.metadata_attempts = 0

    def GetIDsOfNames(self, lcid, name):
        self.lookup_attempts += 1
        raise pythoncom.com_error(self.lookup_hresult - 2**32, "member lookup", None, None)

    def GetTypeInfo(self):
        self.metadata_attempts += 1
        if self.metadata_hresult is not None:
            raise pythoncom.com_error(self.metadata_hresult - 2**32, "type metadata", None, None)
        raise AssertionError("this fixture requires an explicit metadata implementation")


def _incomplete_dispatch(ole):
    from win32com.client import build, dynamic

    return dynamic.CDispatch(ole, build.DispatchItem(), "<unknown>")


def test_hidden_busy_member_lookup_refreshes_before_single_method_invocation(monkeypatch):
    ole = _IncompleteMemberOle()
    original = _incomplete_dispatch(ole)
    events = []
    refreshed = SimpleNamespace(Item=lambda index: events.append(("invoke", index)) or "layer")

    def rebuild(obj):
        assert obj is original
        events.append("refresh")
        return _ComRetryProxy(refreshed)

    monkeypatch.setattr(cad_acad, "_refresh_com_dispatch", rebuild)
    proxy = _ComRetryProxy(original)
    method = proxy.Item

    assert ole.lookup_attempts == 1
    assert events == ["refresh"]
    assert method(0) == "layer"
    assert events == ["refresh", ("invoke", 0)]
    assert proxy._obj is refreshed


def test_genuinely_missing_member_refreshes_once_and_still_fails(monkeypatch):
    ole = _IncompleteMemberOle(lookup_hresult=0x80020006)
    original = _incomplete_dispatch(ole)
    refreshes = []

    def rebuild(obj):
        refreshes.append(obj)
        return _ComRetryProxy(_incomplete_dispatch(ole))

    monkeypatch.setattr(cad_acad, "_refresh_com_dispatch", rebuild)
    proxy = _ComRetryProxy(original)
    with pytest.raises(AttributeError, match="Missing"):
        proxy.Missing

    assert refreshes == [original]
    assert ole.lookup_attempts == 2
    assert proxy._obj is original


def test_python_attribute_error_does_not_attempt_com_recovery(monkeypatch):
    def unexpected(_obj):
        pytest.fail("ordinary Python objects must not rebuild COM metadata")

    monkeypatch.setattr(cad_acad, "_refresh_com_dispatch", unexpected)
    with pytest.raises(AttributeError):
        _ComRetryProxy(SimpleNamespace()).Item


def test_method_attribute_error_is_not_replayed_or_refreshed(monkeypatch):
    calls = []

    def mutate():
        calls.append("write")
        raise AttributeError("failure after a write")

    def unexpected(_obj):
        pytest.fail("method execution must not trigger member lookup recovery")

    monkeypatch.setattr(cad_acad, "_refresh_com_dispatch", unexpected)
    with pytest.raises(AttributeError, match="after a write"):
        _ComRetryProxy(SimpleNamespace(Mutate=mutate)).Mutate()
    assert calls == ["write"]


def test_busy_metadata_refresh_has_bounded_retries(monkeypatch):
    ole = _IncompleteMemberOle(metadata_hresult=0x80010001)
    original = _incomplete_dispatch(ole)
    proxy = _ComRetryProxy(original)
    monkeypatch.setattr(cad_acad.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(cad_acad.pythoncom, "PumpWaitingMessages", lambda: None)

    with pytest.raises(TimeoutError, match="COM"):
        proxy.Item

    assert ole.lookup_attempts == 1
    assert ole.metadata_attempts == 8
    assert proxy._obj is original


def test_disconnected_metadata_refresh_fails_without_retry_or_rebinding():
    ole = _IncompleteMemberOle(metadata_hresult=0x800706BA)
    original = _incomplete_dispatch(ole)
    proxy = _ComRetryProxy(original)

    with pytest.raises(pythoncom.com_error) as error:
        proxy.Item

    assert error.value.hresult & 0xFFFFFFFF == 0x800706BA
    assert ole.metadata_attempts == 1
    assert proxy._obj is original


def test_member_recovery_obeys_existing_scan_deadline(monkeypatch):
    clock = [0.0]
    ole = _IncompleteMemberOle()

    def busy_metadata():
        ole.metadata_attempts += 1
        clock[0] = 2.0
        raise pythoncom.com_error(0x80010001 - 2**32, "busy", None, None)

    ole.GetTypeInfo = busy_metadata
    monkeypatch.setattr(cad_acad.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cad_acad.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(cad_acad.pythoncom, "PumpWaitingMessages", lambda: None)
    monkeypatch.setattr(cad_acad, "_COM_SCAN_TIMEOUT", 1.0)

    @cad_acad._bounded_com_scan
    def scan(_self):
        return _ComRetryProxy(_incomplete_dispatch(ole)).Item

    with pytest.raises(TimeoutError, match="扫描未完成"):
        scan(None)
    assert ole.metadata_attempts == 1
    assert cad_acad._COM_SCAN_STATE.get() is None


def test_refresh_rebuilds_actual_dual_interface_metadata(monkeypatch):
    from win32com.client import build, dynamic

    dispatch_attr = [0] * 12
    dispatch_attr[5] = pythoncom.TKIND_DISPATCH
    dispatch_info = SimpleNamespace(GetTypeAttr=lambda: dispatch_attr,
                                    GetDocumentation=lambda index: ("IAcadLayers",))
    interface_attr = [0] * 12
    interface_attr[5] = pythoncom.TKIND_INTERFACE
    interface_attr[11] = pythoncom.TYPEFLAG_FDUAL
    lookups = []
    interface_info = SimpleNamespace(
        GetTypeAttr=lambda: interface_attr,
        GetRefTypeOfImplType=lambda index: lookups.append(index) or "dispatch-ref",
        GetRefTypeInfo=lambda reference: dispatch_info,
    )
    ole = SimpleNamespace(GetTypeInfo=lambda: interface_info)
    original = SimpleNamespace(_oleobj_=ole)
    representation = object()

    def build_map(info, attr, typecomp, enum):
        assert info is dispatch_info and attr is dispatch_attr
        assert typecomp is None and enum == 0
        return representation

    def construct(raw_ole, mapping, name):
        assert raw_ole is ole and mapping is representation and name == "IAcadLayers"
        return SimpleNamespace(_oleobj_=raw_ole, Count=2)

    monkeypatch.setattr(build, "DispatchItem", build_map)
    monkeypatch.setattr(dynamic, "CDispatch", construct)
    result = cad_acad._refresh_com_dispatch(original)

    assert isinstance(result, _ComRetryProxy)
    assert result.Count == 2
    assert lookups == [-1]


def test_refresh_preserves_plain_non_com_objects():
    value = SimpleNamespace(Count=1)
    assert cad_acad._refresh_com_dispatch(value) is value


def test_chamfer_refresh_delegates_to_shared_com_recovery(monkeypatch):
    from cad_mcp.drawing import chamfer

    source, refreshed = object(), object()
    calls = []

    def rebuild(obj):
        calls.append(obj)
        return refreshed

    monkeypatch.setattr(chamfer, "_refresh_com_dispatch", rebuild)
    assert chamfer.fresh_com(source) is refreshed
    assert calls == [source]
