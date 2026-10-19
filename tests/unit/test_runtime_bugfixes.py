"""Regressions for attach-only connections, bounded reads and precise diagnostics."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from cad_mcp import server
from cad_mcp.core.geometry import scan_line_circle_issues
from cad_mcp.core.runtime import CadMcpError
from cad_mcp.drawing import autocad
from cad_mcp.drawing.autocad import AcadBackend, _ComRetryProxy, _bounded_com_scan


class Collection:
    def __init__(self, items):
        self.items = items
        self.count_reads = 0
        self.item_reads = []

    @property
    def Count(self):
        self.count_reads += 1
        return len(self.items)

    def Item(self, index):
        self.item_reads.append(index)
        return self.items[index]


def fake_backend(monkeypatch, tmp_path, entities):
    backend = AcadBackend(tmp_path)
    layer = SimpleNamespace(
        Name="PIPE", Color=3, Linetype="CONTINUOUS", Lineweight=70,
        Plottable=True, Description="", Lock=False, Freeze=False,
        EntityTransparency="0",
    )
    backend.msp = Collection(entities)
    backend.doc = SimpleNamespace(
        Blocks=Collection([backend.msp]), Layers=Collection([layer]),
        ActiveLayer=layer,
    )
    monkeypatch.setattr(backend, "_check_doc", lambda: None)
    monkeypatch.setattr(backend, "_yield_com", lambda: None)
    return backend


def test_missing_instance_never_dispatches_autocad(monkeypatch, tmp_path):
    backend = AcadBackend(tmp_path)
    monkeypatch.setattr(backend, "_attach_existing_acad", lambda: None)
    monkeypatch.setattr(
        autocad.win32com.client, "Dispatch",
        lambda *_args: pytest.fail("must never start a new AutoCAD instance"),
    )

    with pytest.raises(CadMcpError, match="未启动新实例") as caught:
        backend._connect()

    assert caught.value.code == "PRECONDITION_FAILED"
    assert backend.acad is None
    assert backend.doc is None


def test_connect_reuses_existing_instance_and_recovers_stale_reference(monkeypatch, tmp_path):
    backend = AcadBackend(tmp_path)

    class StaleApplication:
        @property
        def Documents(self):
            raise RuntimeError("stale reference")

    backend.acad = StaleApplication()
    backend.doc = object()
    backend.msp = object()
    application = SimpleNamespace(Documents=Collection([]), Visible=False)
    attached = []
    monkeypatch.setattr(
        backend, "_attach_existing_acad",
        lambda: attached.append(True) or application,
    )
    monkeypatch.setattr(backend, "_wait_acad_ready", lambda app: None)

    connected = backend._connect()

    assert isinstance(connected, _ComRetryProxy)
    assert application.Visible is True
    assert attached == [True]
    assert backend.doc is None
    assert backend.msp is None
    assert backend._connect() is connected
    assert attached == [True]


def test_failed_attach_does_not_close_existing_instance(monkeypatch, tmp_path):
    backend = AcadBackend(tmp_path)

    class Application:
        def Quit(self):
            pytest.fail("an existing user instance must never be closed")

    monkeypatch.setattr(backend, "_attach_existing_acad", lambda: Application())

    def not_ready(_app):
        raise RuntimeError("not ready")

    monkeypatch.setattr(backend, "_wait_acad_ready", not_ready)
    with pytest.raises(RuntimeError, match="无法连接 AutoCAD"):
        backend._connect()
    assert backend.acad is None


@pytest.mark.parametrize("probe", ["_connect", "_ensure_connected"])
def test_expired_scan_does_not_discard_valid_connection_or_document(monkeypatch, tmp_path, probe):
    backend = AcadBackend(tmp_path)
    backend.acad = _ComRetryProxy(SimpleNamespace(Documents=Collection([])))
    backend.doc = object()
    backend.msp = _ComRetryProxy(Collection([]))
    original_document = backend.doc
    if probe == "_ensure_connected":
        monkeypatch.setattr(backend, "_connect", lambda: backend.acad)
    token = autocad._COM_SCAN_STATE.set((0.0, "expired_read"))
    try:
        with pytest.raises(TimeoutError, match="扫描未完成"):
            getattr(backend, probe)()
        assert backend.acad is not None
        assert backend.doc is original_document
    finally:
        autocad._COM_SCAN_STATE.reset(token)


def test_layer_counts_keep_database_semantics_and_cache_collection_counts(monkeypatch, tmp_path):
    entities = [SimpleNamespace(Layer="PIPE") for _ in range(28)]
    backend = fake_backend(monkeypatch, tmp_path, entities)
    backend.doc.Blocks.items.append(Collection([SimpleNamespace(Layer="PIPE")]))
    yielded = []
    monkeypatch.setattr(backend, "_yield_com", lambda: yielded.append(True))

    result = backend.list_layers_detailed()

    assert result["layers"][0]["entity_count"] == 29
    assert backend.doc.Blocks.count_reads == 1
    assert backend.doc.Layers.count_reads == 1
    assert backend.msp.count_reads == 1
    assert yielded


def test_layer_count_failure_stops_before_scanning_remaining_entities(monkeypatch, tmp_path):
    class BrokenEntity:
        @property
        def Layer(self):
            raise TimeoutError("AutoCAD busy")

    backend = fake_backend(monkeypatch, tmp_path, [BrokenEntity()] * 100)

    with pytest.raises(TimeoutError, match="AutoCAD busy"):
        backend.list_layers_detailed()

    assert backend.msp.item_reads == [0]
    assert backend.doc.Layers.item_reads == []


@pytest.mark.parametrize("method", ["get_info", "list_layers_detailed", "query_entities", "check_drawing"])
def test_read_scan_stops_on_total_deadline_and_resets_context(monkeypatch, tmp_path, method):
    clock = [0.0]
    monkeypatch.setattr(autocad.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(autocad, "_COM_SCAN_TIMEOUT", 1.0)

    class SlowEntity:
        Handle = "A"

        @property
        def EntityName(self):
            clock[0] = 2.0
            return "AcDbLine"

        @property
        def Layer(self):
            clock[0] = 2.0
            return "PIPE"

    backend = fake_backend(monkeypatch, tmp_path, [SlowEntity()] * 100)
    monkeypatch.setattr(backend, "_drawing_extent", lambda: (0, 0, 100, 100))
    with pytest.raises(TimeoutError, match="扫描未完成"):
        getattr(backend, method)()
    assert backend.msp.item_reads == [0]
    assert autocad._COM_SCAN_STATE.get() is None


def test_total_deadline_also_bounds_com_busy_retries(monkeypatch):
    clock = [0.0]
    calls = []
    monkeypatch.setattr(autocad.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(autocad, "_COM_SCAN_TIMEOUT", 0.1)
    monkeypatch.setattr(autocad.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))

    def busy():
        calls.append(True)
        raise autocad.pythoncom.com_error(-2147417846, "busy", None, None)

    @_bounded_com_scan
    def scan(_self):
        return _ComRetryProxy(object())._retry(busy)

    with pytest.raises(TimeoutError, match="扫描未完成"):
        scan(None)
    assert len(calls) < 8
    assert autocad._COM_SCAN_STATE.get() is None


def test_small_filtered_page_keeps_original_pagination(monkeypatch, tmp_path):
    entities = [SimpleNamespace(Handle=format(i + 1, "X"), Layer="PIPE", EntityName="AcDbLine")
                for i in range(4)]
    backend = fake_backend(monkeypatch, tmp_path, entities)

    first = backend.query_entities(layer="PIPE", limit=2)
    second = backend.query_entities(layer="PIPE", offset=first["next_offset"], limit=2)

    assert [item["handle"] for item in first["entities"]] == ["1", "2"]
    assert [item["handle"] for item in second["entities"]] == ["3", "4"]
    assert first["scanned"] == second["scanned"] == 2
    assert second["next_offset"] is None


@pytest.mark.parametrize("hresult, fatal", [(-2147352573, False), (-2147023174, True)])
def test_optional_bounds_distinguish_unsupported_from_lost_connection(monkeypatch, tmp_path, hresult, fatal):
    class Entity:
        Handle = "A"
        Layer = "PIPE"
        EntityName = "AcDbLine"

        def GetBoundingBox(self):
            raise autocad.pythoncom.com_error(hresult, "cannot read bounds", None, None)

    backend = fake_backend(monkeypatch, tmp_path, [Entity()])
    if fatal:
        with pytest.raises(autocad.pythoncom.com_error):
            backend.query_entities(include_bounds=True)
    else:
        result = backend.query_entities(include_bounds=True)
        assert result["entities"][0]["bounds"] is None


@pytest.mark.parametrize("hresult, fatal", [(-2147352573, False), (-2147023174, True)])
def test_optional_layer_fields_distinguish_unsupported_from_lost_connection(monkeypatch, tmp_path, hresult, fatal):
    backend = fake_backend(monkeypatch, tmp_path, [])

    class Layer:
        Name = "PIPE"

        @property
        def EntityTransparency(self):
            raise autocad.pythoncom.com_error(hresult, "cannot read transparency", None, None)

    backend.doc.Layers.items[0] = Layer()
    if fatal:
        with pytest.raises(autocad.pythoncom.com_error):
            backend.list_layers_detailed()
    else:
        result = backend.list_layers_detailed()
        assert result["layers"][0]["transparency"] is None
        assert "layers.PIPE.transparency" in result["unverified_fields"]


@pytest.mark.parametrize("arguments, location", [
    ({}, ["params"]),
    ({"params": {"limit": 500}}, ["params", "limit"]),
    ({"params": {"limit": "many"}}, ["params", "limit"]),
    ({"params": {"unknown": 1}}, ["params", "unknown"]),
])
def test_gateway_validation_errors_keep_field_and_limit_without_execution(monkeypatch, arguments, location):
    monkeypatch.setattr(
        server, "_get_backend", lambda: pytest.fail("invalid input reached the backend"),
    )

    result = json.loads(asyncio.run(server._gateway_call(server.GatewayCallParams(
        tool_name="cad_query_entities", arguments=arguments,
    ))))

    detail = result["error_detail"]
    assert result["ok"] is False
    assert detail["code"] == "INVALID_ARGUMENT"
    assert detail["retryable"] is False
    errors = detail["details"]["validation_errors"]
    assert errors[0]["loc"] == location
    assert "input" not in errors[0]
    if arguments == {"params": {"limit": 500}}:
        assert "100" in errors[0]["msg"]


def test_existing_describe_exposes_required_defaults_and_limits():
    result = json.loads(asyncio.run(server._gateway_describe(server.GatewayDescribeParams(
        tool_name="cad_query_entities",
    ))))
    schema = result["tool"]["inputSchema"]
    assert "params" in schema["required"]
    query_schema = schema["$defs"]["QueryEntitiesInput"]
    assert query_schema["additionalProperties"] is False
    limit = query_schema["properties"]["limit"]
    assert (limit["minimum"], limit["maximum"], limit["default"]) == (1, 100, 50)


@pytest.mark.parametrize("gap, expected", [(0.0, False), (1e-10, False), (1e-8, True), (0.02, True), (1.0, True), (2.0, False)])
def test_gap_check_separates_roundoff_from_real_gaps(gap, expected):
    result = scan_line_circle_issues([
        ("A", (-10.0, 0.0), (0.0, 0.0)),
        ("B", (gap, 0.0), (10.0, 0.0)),
    ], [], 1.0)
    assert bool(result["issues"]) is expected
    if expected:
        assert "相距 0.0mm" not in result["issues"][0]
    if gap == 0.02:
        assert "0.02mm" in result["issues"][0]


def test_explicit_tiny_tolerance_is_not_swallowed_by_roundoff_threshold():
    result = scan_line_circle_issues([
        ("A", (-10.0, 0.0), (0.0, 0.0)),
        ("B", (1e-13, 0.0), (10.0, 0.0)),
    ], [], 1e-12)
    assert len(result["issues"]) == 1
    assert "1e-13mm" in result["issues"][0]
