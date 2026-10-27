"""Isolated fault doubles: this program must never access a real CAD instance."""

from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from cad_mcp import server
from cad_mcp.drawing import autocad
from cad_mcp.drawing.autocad import AcadBackend, _ComRetryProxy


RPC_UNAVAILABLE = -2147023174
COM_BUSY = -2147417846
ATTEMPTS = Counter()


def emit(payload):
    print(json.dumps(payload, ensure_ascii=True), flush=True)


def forbid_real_cad(name):
    def forbidden(*_args, **_kwargs):
        ATTEMPTS[name] += 1
        raise AssertionError(f"real CAD access is forbidden in fault simulation: {name}")
    return forbidden


autocad.win32com.client.Dispatch = forbid_real_cad("start")
autocad.win32com.client.GetActiveObject = forbid_real_cad("attach")


class Meter:
    def __init__(self, kind):
        self.kind = kind
        self.clock = 0.0
        self.reads = Counter()
        self.yields = 0
        self.faulted = False
        self.after_fault_reads = 0

    def touch(self, label):
        if self.faulted:
            self.after_fault_reads += 1
        self.reads[label] += 1
        if self.kind.startswith("slow-") or self.kind == "busy":
            self.clock += 0.01
        if label == "entity.Layer":
            if self.kind == "broken" and self.reads[label] == 40:
                self.faulted = True
                raise autocad.pythoncom.com_error(RPC_UNAVAILABLE, "injected mid-scan disconnect", None, None)
            if self.kind == "busy":
                raise autocad.pythoncom.com_error(COM_BUSY, "injected busy server", None, None)

    def yield_messages(self):
        self.yields += 1


class Collection:
    _oleobj_ = object()

    def __init__(self, items, meter, label):
        self.items = items
        self.meter = meter
        self.label = label

    @property
    def Count(self):
        self.meter.touch(self.label + ".Count")
        return len(self.items)

    def Item(self, index):
        self.meter.touch(self.label + ".Item")
        return self.items[index]


class Entity:
    _oleobj_ = object()

    def __init__(self, index, layer, meter, reference=None):
        self.index = index
        self.layer_name = layer
        self.meter = meter
        self.Name = reference
        self.Handle = format(index + 1, "X")

    @property
    def Layer(self):
        self.meter.touch("entity.Layer")
        return self.layer_name

    @property
    def EntityName(self):
        self.meter.touch("entity.EntityName")
        return "AcDbBlockReference" if self.Name else "AcDbLine"


def install_backend(document, modelspace, application, meter, directory):
    raw = AcadBackend(directory)
    raw.acad = _ComRetryProxy(application)
    raw.doc = _ComRetryProxy(document)
    raw.msp = _ComRetryProxy(modelspace)
    # Fault tests use no real CAD object or shared CAD mutex.
    raw._acquire_autocad_lock = lambda: None
    raw._release_autocad_lock = lambda: None
    raw._yield_com = meter.yield_messages
    raw._attach_existing_acad = lambda: None
    server._backend = server._AcadLockProxy(raw)
    return raw


def make_graph(kind, directory):
    meter = Meter(kind)
    layers = [SimpleNamespace(
        _oleobj_=object(), Name=f"PIPE-{i}", Color=i + 1,
        Linetype="CONTINUOUS", Lineweight=70, Plottable=True,
        Description="", Lock=False, Freeze=False, EntityTransparency="0",
    ) for i in range(8)]
    definitions = []
    all_entities = []
    for block in range(256):
        entities = [Entity(
            block * 64 + index, layers[index % 8].Name, meter,
            f"BLOCK-{block + 1}" if block < 255 and index % 16 == 0 else None,
        ) for index in range(64)]
        definitions.append(Collection(entities, meter, "block"))
        all_entities.extend(entities)
    model_entities = [Entity(20000 + i, layers[i % 8].Name, meter, "BLOCK-0" if i % 16 == 0 else None)
                      for i in range(512)]
    all_entities.extend(model_entities)
    expected = Counter(entity.layer_name for entity in all_entities)
    meter.block_reference_entities = sum(entity.Name is not None for entity in all_entities)
    modelspace = Collection(model_entities, meter, "model")
    document = SimpleNamespace(
        _oleobj_=object(), Blocks=Collection([modelspace, *definitions], meter, "blocks"),
        Layers=Collection(layers, meter, "layers"), ActiveLayer=layers[0],
    )
    application = SimpleNamespace(
        _oleobj_=object(), Documents=Collection([document], meter, "documents"),
        GetAcadState=lambda: SimpleNamespace(IsQuiescent=True),
        Quit=forbid_real_cad("quit"),
    )
    raw = install_backend(document, modelspace, application, meter, directory)
    return raw, meter, expected


def graph_scenario(kind, directory):
    raw, meter, expected = make_graph(kind, directory)
    if kind.startswith("slow-") or kind == "busy":
        # Deterministic accumulated latency; not a real AutoCAD performance claim.
        autocad._COM_SCAN_TIMEOUT = 1.0
        autocad.time.monotonic = lambda: meter.clock
        autocad.time.sleep = lambda seconds: setattr(meter, "clock", meter.clock + seconds)
    if kind == "slow-info":
        result = json.loads(server.cad_get_drawing_info(server.GetDrawingInfoInput()))
    elif kind == "slow-query":
        # No matches forces a scan rather than ending at the first page limit.
        result = json.loads(server.cad_query_entities(server.QueryEntitiesInput(layer="ABSENT")))
    else:
        result = json.loads(server.cad_list_layers_detailed(server.ListLayersDetailedInput()))
    actual = {item["name"]: item["entity_count"] for item in result.get("layers", [])}
    emit({
        "event": "result", "scenario": kind, "response": result,
        "graph_entities": sum(expected.values()), "block_definitions": 256,
        "block_reference_entities": meter.block_reference_entities,
        "counts_match": actual == dict(expected),
        "reads": dict(meter.reads), "yields": meter.yields,
        "after_fault_reads": meter.after_fault_reads,
        "deadline_context_reset": autocad._COM_SCAN_STATE.get() is None,
        "document_binding_preserved": raw.doc is not None,
        "real_cad_attempts": dict(ATTEMPTS),
    })


def hanging_scenario(directory, *, serve=False):
    raw, meter, _expected = make_graph("hang", directory)
    autocad._COM_SCAN_TIMEOUT = 0.1

    class NeverReturningCollection:
        _oleobj_ = object()

        @property
        def Count(self):
            if serve:
                (directory / "blocking-call-entered").write_text("entered", encoding="utf-8")
            else:
                emit({"event": "entered_blocking_call", "scan_budget_seconds": 0.1})
            # Represents a native call which never hands control back to Python.
            threading.Event().wait()

    raw.doc._obj.Blocks = NeverReturningCollection()
    if serve:
        from cad_mcp.worker import serve as run_worker

        run_worker(server)
        return
    result = server.cad_list_layers_detailed(server.ListLayersDetailedInput())
    emit({"event": "unexpected_return", "response": result})


def isolated_fault_worker(kind, directory):
    raw, _meter, _expected = make_graph("complete", directory)
    raw._begin_undo = lambda: False

    def failed_call(*_args, **_kwargs):
        with (directory / "simulated-actions").open("a", encoding="utf-8") as stream:
            stream.write("applied\n")
        if "crash" in kind:
            os._exit(24)
        threading.Event().wait()

    def bind(filename):
        raw.current_path = Path(filename).resolve()
        return "bound simulated document"

    raw.open_drawing = bind
    if kind == "isolated-read-crash-worker":
        raw.get_info = failed_call
    else:
        raw.draw_line = failed_call
    from cad_mcp.worker import serve

    serve(server)


def rollback_unknown_worker(directory):
    from cad_mcp.worker import serve
    from cad_mcp.core.runtime import failure_json
    raw = server._backend
    raw.new_drawing("uncertain.dxf")
    def failed(params):
        with (directory / "simulated-actions").open("a", encoding="utf-8") as stream:
            stream.write("applied\n")
        exc = RuntimeError("simulated rollback uncertainty")
        exc.rollback_status = "failed_or_unverified"
        return failure_json(exc, "cad_fillet")
    for name in ("cad_fillet", "cad_chamfer", "cad_draw_line"):
        tool = server._FULL_TOOL_REGISTRY[name]
        tool.fn, tool.is_async = failed, False
    serve(server)


SERVICE_CODE = """
import json, os, sys
print('ready', flush=True)
for line in sys.stdin:
    command = line.strip()
    if command == 'crash':
        os._exit(23)
    print(json.dumps(1 if command == 'count' else True), flush=True)
"""


class SimulatedService:
    def __init__(self):
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-u", "-c", SERVICE_CODE],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
        )
        assert self.process.stdout.readline().strip() == "ready"

    def request(self, command):
        try:
            self.process.stdin.write(command + "\n")
            self.process.stdin.flush()
            response = self.process.stdout.readline()
            if not response:
                raise EOFError("simulated service exited")
            return json.loads(response)
        except (OSError, EOFError) as exc:
            raise autocad.pythoncom.com_error(RPC_UNAVAILABLE, "simulated RPC service unavailable", None, None) from exc

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        self.process.communicate(timeout=5)


def crash_scenario(directory):
    service = SimulatedService()
    meter = Meter("crash")

    class Documents:
        _oleobj_ = object()

        @property
        def Count(self):
            return service.request("count")

    class Application:
        _oleobj_ = object()

        def __init__(self):
            self.Documents = Documents()

        def GetAcadState(self):
            return SimpleNamespace(IsQuiescent=service.request("quiet"))

        Quit = staticmethod(forbid_real_cad("quit"))

    document = SimpleNamespace(_oleobj_=object())
    raw = install_backend(document, Collection([], meter, "model"), Application(), meter, directory)
    begin_undo = raw._begin_undo

    def unexpected_write():
        ATTEMPTS["undo"] += 1
        return begin_undo()

    raw._begin_undo = unexpected_write
    wait_ready = raw._wait_operation_ready
    raw._wait_operation_ready = lambda: wait_ready(timeout=0)
    try:
        before = json.loads(server.cad_health(server.HealthInput(reconnect=False)))
        try:
            service.request("crash")
        except autocad.pythoncom.com_error:
            pass
        exit_code = service.process.wait(timeout=5)
        after = json.loads(server.cad_health(server.HealthInput(reconnect=False)))
        write = json.loads(server.cad_draw_line(server.DrawLineInput(x1=0, y1=0, x2=1, y2=1)))
        reconnect = json.loads(server.cad_health(server.HealthInput(reconnect=True)))
        emit({
            "event": "result", "scenario": "crash", "before": before,
            "after": after, "write": write, "reconnect": reconnect,
            "simulated_service_exit_code": exit_code,
            "real_cad_attempts": dict(ATTEMPTS),
        })
    finally:
        service.close()


if __name__ == "__main__":
    scenario = sys.argv[1]
    directory = Path(sys.argv[2])
    if scenario == "crash":
        crash_scenario(directory)
    elif scenario == "isolated-hang-worker":
        hanging_scenario(directory, serve=True)
    elif scenario in {"isolated-write-hang-worker", "isolated-write-crash-worker", "isolated-read-crash-worker"}:
        isolated_fault_worker(scenario, directory)
    elif scenario == "isolated-rollback-unknown-worker":
        rollback_unknown_worker(directory)
    elif scenario == "hang":
        hanging_scenario(directory)
    else:
        graph_scenario(scenario, directory)
