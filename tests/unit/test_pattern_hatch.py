import json
import math
from types import SimpleNamespace

import ezdxf
import pytest
from ezdxf.entities import Hatch
from ezdxf.math import Vec3
from ezdxf.tools import pattern as hatch_patterns
from pydantic import ValidationError

from cad_mcp import server
from cad_mcp.drawing import autocad
from cad_mcp.drawing.autocad import AcadBackend
from cad_mcp.drawing.document import CadDocument


POINTS = [(0, 0), (10, 0), (10, 5), (0, 5)]


@pytest.fixture
def dxf_backend(tmp_path):
    backend = CadDocument(tmp_path)
    backend.new_drawing("pattern-test.dxf")
    return backend


def _rectangle(backend, **attributes):
    return backend.msp.add_lwpolyline(POINTS, close=True, dxfattribs=attributes)


def _handle(backend, entity):
    return backend._handle_str(entity.dxf.handle)


@pytest.mark.parametrize("pattern", ["ANSI31", "ANSI32", "AR-CONC", "SOLID"])
def test_dxf_pattern_and_boundary_survive_file_roundtrip(dxf_backend, tmp_path, pattern):
    backend = dxf_backend
    boundary = _rectangle(backend)
    settings = {} if pattern == "SOLID" else {"scale": 2.5, "angle": 30.0}
    result = backend.draw_hatch(
        boundary_handle=_handle(backend, boundary), pattern=pattern,
        layer="PATTERN", associative=True, dry_run=False, **settings,
    )

    assert result["verified"] is True
    assert result["boundary_area"] == 50.0
    assert result["readback"]["bounding_box"] == {"min": [0.0, 0.0], "max": [10.0, 5.0]}
    assert len(backend.msp) == 2
    path = tmp_path / "roundtrip.dxf"
    backend.doc.saveas(path)
    loaded = ezdxf.readfile(path)
    hatch = loaded.entitydb[format(int(result["hatch_handle"]), "X")]
    assert hatch.dxf.pattern_name == pattern
    assert bool(hatch.dxf.solid_fill) is (pattern == "SOLID")
    assert hatch.dxf.layer == "PATTERN"
    assert "PATTERN" in loaded.layers
    assert hatch.dxf.associative == 1
    assert hatch.paths[0].source_boundary_objects == [boundary.dxf.handle]
    assert hatch.paths[0].vertices == [(x, y, 0.0) for x, y in POINTS]
    assert hatch.paths[0].is_closed
    assert loaded.entitydb[boundary.dxf.handle].get_reactors() == [hatch.dxf.handle]
    if pattern != "SOLID":
        assert hatch.dxf.pattern_scale == 2.5
        assert hatch.dxf.pattern_angle == 30.0
        definitions = hatch_patterns.load(measurement=loaded.header["$MEASUREMENT"])
        expected = hatch_patterns.scale_pattern(definitions[pattern], factor=2.5, angle=30.0)
        assert len(hatch.pattern.lines) == len(expected)
        for actual, definition in zip(hatch.pattern.lines, expected):
            assert actual.angle == pytest.approx(definition[0])
            assert tuple(actual.base_point) == pytest.approx(definition[1])
            assert tuple(actual.offset) == pytest.approx(definition[2])
            assert actual.dash_length_items == pytest.approx(definition[3])
    assert not loaded.audit().has_errors


def test_dxf_default_preview_is_zero_write_and_names_the_pattern(dxf_backend):
    backend = dxf_backend
    boundary = _rectangle(backend)
    handles = set(backend.doc.entitydb)
    layers = [layer.dxf.name for layer in backend.doc.layers]
    result = backend.draw_hatch(
        boundary_handle=_handle(backend, boundary), pattern=" ansi32 ",
        scale=0.5, angle=-30, layer="NEW_LAYER", associative=True,
    )
    assert result["dry_run"] is True
    assert result["would_write"] is False
    assert result["pattern"] == "ANSI32"
    assert result["scale"] == 0.5
    assert result["angle"] == 330.0
    assert result["keep_boundary"] is True
    assert set(backend.doc.entitydb) == handles
    assert [layer.dxf.name for layer in backend.doc.layers] == layers
    assert boundary.get_reactors() == []


@pytest.mark.parametrize("boundary_kind", ["circle", "polyline", "lwpolyline"])
def test_dxf_closed_boundaries_preserve_elevation_and_normal(dxf_backend, boundary_kind):
    backend = dxf_backend
    if boundary_kind == "circle":
        boundary = backend.msp.add_circle((3, 4, 7), 2)
    elif boundary_kind == "polyline":
        boundary = backend.msp.add_polyline2d(POINTS, close=True, dxfattribs={"elevation": (0, 0, 7)})
    else:
        boundary = _rectangle(backend, elevation=7)
    boundary.dxf.extrusion = (0, 1, 1)
    result = backend.draw_hatch(
        boundary_handle=_handle(backend, boundary), pattern="ANSI32", dry_run=False,
    )
    hatch = backend._get_entity(result["hatch_handle"])
    assert Vec3(hatch.dxf.extrusion).isclose(boundary.dxf.extrusion)
    assert hatch.dxf.elevation == Vec3(0, 0, 7)
    assert hatch.dxf.associative == 0
    assert boundary.get_reactors() == []
    if boundary_kind == "circle":
        edge = hatch.paths[0].edges[0]
        assert tuple(edge.center) == (3, 4)
        assert edge.radius == 2
        assert (edge.start_angle, edge.end_angle) == (0, 360)
        assert result["boundary_area"] == pytest.approx(4 * math.pi)


@pytest.mark.parametrize("kind", ["open_lwpolyline", "open_polyline", "arc", "line", "ellipse_arc", "crossing", "bulge", "3d"])
@pytest.mark.parametrize("dry_run", [True, False])
def test_dxf_rejects_invalid_or_unsupported_boundaries_without_writing(dxf_backend, kind, dry_run):
    backend = dxf_backend
    if kind == "open_lwpolyline":
        boundary = backend.msp.add_lwpolyline(POINTS)
    elif kind == "open_polyline":
        boundary = backend.msp.add_polyline2d(POINTS)
    elif kind == "arc":
        boundary = backend.msp.add_arc((0, 0), 2, 0, 180)
    elif kind == "line":
        boundary = backend.msp.add_line((0, 0), (2, 2))
    elif kind == "ellipse_arc":
        boundary = backend.msp.add_ellipse((0, 0), (4, 0), 0.5, end_param=math.pi)
    elif kind == "crossing":
        boundary = backend.msp.add_lwpolyline([(0, 0), (10, 5), (0, 5), (10, 0)], close=True)
    elif kind == "bulge":
        boundary = backend.msp.add_lwpolyline([(0, 0, 1), (10, 0, 0), (10, 5, 0)], format="xyb", close=True)
    else:
        boundary = backend.msp.add_polyline3d([(0, 0, 0), (10, 0, 5), (10, 5, 0)], close=True)
    handles = set(backend.doc.entitydb)
    with pytest.raises(ValueError):
        backend.draw_hatch(boundary_handle=_handle(backend, boundary), dry_run=dry_run)
    assert set(backend.doc.entitydb) == handles
    assert boundary.is_alive
    if kind in {"open_lwpolyline", "open_polyline"}:
        assert not boundary.is_closed


@pytest.mark.parametrize("settings", [
    {"pattern": "DOES_NOT_EXIST"}, {"pattern": ""}, {"pattern": "ANSI\x0031"},
    {"scale": 0}, {"scale": -1}, {"scale": 1001}, {"scale": float("nan")},
    {"scale": float("inf")}, {"angle": 361}, {"angle": float("nan")},
    {"angle": float("inf")}, {"pattern": "SOLID", "scale": 2},
])
def test_dxf_rejects_invalid_pattern_parameters_before_writing(dxf_backend, settings):
    backend = dxf_backend
    boundary = _rectangle(backend)
    handles = set(backend.doc.entitydb)
    with pytest.raises(ValueError):
        backend.draw_hatch(boundary_handle=_handle(backend, boundary), dry_run=False, **settings)
    assert set(backend.doc.entitydb) == handles


@pytest.mark.parametrize("settings", [
    {}, {"points": POINTS}, {"boundary_handle": "1", "points": POINTS},
    {"boundary_handle": ""}, {"boundary_handle": "AB"},
    {"boundary_handle": "1", "scale": 0},
    {"boundary_handle": "1", "scale": float("inf")},
    {"boundary_handle": "1", "angle": float("nan")},
    {"boundary_handle": "1", "keep_boundary": False},
])
def test_mcp_input_only_accepts_existing_boundary_handles(settings):
    with pytest.raises(ValidationError):
        server.DrawHatchInput(**settings)


def test_mcp_exposes_the_new_schema_and_forwards_the_selected_parameters(monkeypatch, dxf_backend):
    backend = dxf_backend
    boundary = _rectangle(backend)
    monkeypatch.setattr(server, "_backend", backend)
    params = server.DrawHatchInput(
        boundary_handle=_handle(backend, boundary), pattern="ansi32",
        scale=3, angle=-45, associative=True, dry_run=False,
    )
    result = json.loads(server.cad_draw_hatch(params))
    assert result["ok"] is True
    assert result["readback"]["pattern_name"] == "ANSI32"
    assert result["readback"]["pattern_scale"] == 3
    assert result["readback"]["pattern_angle"] == 315
    tool = next(tool for tool in server.mcp._tool_manager.list_tools() if tool.name == "cad_draw_hatch")
    schema = tool.parameters["$defs"]["DrawHatchInput"]
    assert "points" not in schema["properties"]
    assert schema.get("required", []) == []
    assert "boundary_handles" in schema["properties"]
    assert "seed_point" in schema["properties"]
    assert schema["properties"]["dry_run"]["default"] is True


def test_mcp_reports_missing_boundary_as_a_structured_error(monkeypatch, dxf_backend):
    monkeypatch.setattr(server, "_backend", dxf_backend)
    result = json.loads(server.cad_draw_hatch(server.DrawHatchInput(boundary_handle="999999")))
    assert result["ok"] is False
    assert "未找到边界" in result["error"]
    assert result["error_detail"]["code"] == "INVALID_ARGUMENT"


def test_dxf_failed_readback_removes_only_this_hatch_and_its_association(monkeypatch, dxf_backend):
    backend = dxf_backend
    boundary = _rectangle(backend)
    existing = backend.draw_hatch(
        boundary_handle=_handle(backend, boundary), associative=True, dry_run=False,
    )
    before_count = len(backend.msp)
    before_reactors = list(boundary.get_reactors())
    original = Hatch.set_pattern_fill

    def corrupt_scale(hatch, *args, **kwargs):
        original(hatch, *args, **kwargs)
        hatch.dxf.pattern_scale = 99

    monkeypatch.setattr(Hatch, "set_pattern_fill", corrupt_scale)
    with pytest.raises(RuntimeError, match="读回不一致"):
        backend.draw_hatch(
            boundary_handle=_handle(backend, boundary), scale=2, associative=True,
            layer="FAILED_LAYER", dry_run=False,
        )
    assert len(backend.msp) == before_count
    assert backend._get_entity(existing["hatch_handle"]).is_alive
    assert boundary.is_alive
    assert boundary.get_reactors() == before_reactors
    assert "FAILED_LAYER" not in backend.doc.layers


class _AcadBoundary:
    EntityName = "AcDbPolyline"
    Handle = "A"
    Closed = True
    Area = 50.0
    deleted = False
    Coordinates = [0.0, 0.0, 10.0, 0.0, 10.0, 5.0, 0.0, 5.0]
    Normal = (0.0, 0.0, 1.0)
    Elevation = 0.0

    def GetBulge(self, index):
        return 0.0

    def Delete(self):
        self.deleted = True


class _AcadHatch:
    EntityName = "AcDbHatch"
    Layer = "FILL"
    NumberOfLoops = 1
    Area = 50.0
    Handle = "B"

    def __init__(self, events, pattern, associative):
        self.events = events
        self.PatternName = pattern
        self.AssociativeHatch = associative
        self.deleted = False
        self._scale = 1.0
        self._angle = 0.0

    def AppendOuterLoop(self, items):
        self.events.append(("append", items))

    @property
    def PatternScale(self):
        return self._scale

    @PatternScale.setter
    def PatternScale(self, value):
        self.events.append(("scale", value))
        self._scale = value

    @property
    def PatternAngle(self):
        return self._angle

    @PatternAngle.setter
    def PatternAngle(self, value):
        self.events.append(("angle", value))
        self._angle = value

    def Evaluate(self):
        self.events.append(("evaluate", None))

    def GetBoundingBox(self):
        return (0.0, 0.0, 0.0), (10.0, 5.0, 0.0)

    def Delete(self):
        self.deleted = True
        self.events.append(("delete_hatch", None))


@pytest.fixture
def acad_backend(monkeypatch, tmp_path):
    events = []
    boundary = _AcadBoundary()
    state = SimpleNamespace(events=events, boundary=boundary, hatch=None, corrupt=None, fail=False)

    class ModelSpace:
        def AddHatch(self, pattern_type, pattern_name, associative, object_type):
            events.append(("add_hatch", (pattern_type, pattern_name, associative, object_type)))
            if state.fail:
                raise RuntimeError("AutoCAD 图案不存在")
            state.hatch = _AcadHatch(events, pattern_name, associative)
            if state.corrupt == "pattern":
                state.hatch.PatternName = "SOLID"
            return state.hatch

    class Document:
        def Regen(self, _mode):
            events.append(("regen", None))
            if state.corrupt == "scale":
                state.hatch._scale = 99
            elif state.corrupt == "angle":
                state.hatch._angle = math.pi

    backend = AcadBackend(tmp_path)
    backend.doc = Document()
    backend.msp = ModelSpace()
    state.backend = backend
    monkeypatch.setattr(backend, "_ensure_connected", lambda: None)
    monkeypatch.setattr(backend, "_find", lambda handle: state.boundary if handle == "10" else None)
    monkeypatch.setattr(backend, "_apply_layer", lambda layer: events.append(("layer", layer)))
    monkeypatch.setattr(autocad, "_dispatch_array", lambda items: list(items))
    return state


@pytest.mark.parametrize("pattern", ["ANSI31", "ANSI32", "AR-CONC"])
def test_acad_appends_boundary_before_setting_pattern_and_converts_degrees(acad_backend, pattern):
    state = acad_backend
    result = state.backend.draw_hatch(
        boundary_handle="10", pattern=pattern, scale=2.5, angle=-30,
        associative=True, dry_run=False,
    )
    index = next(i for i, event in enumerate(state.events) if event[0] == "add_hatch")
    assert state.events[index] == ("add_hatch", (1, pattern, True, 0))
    assert state.events[index + 1] == ("append", [state.boundary])
    assert state.events[index + 2] == ("scale", 2.5)
    assert state.events[index + 3] == ("angle", math.radians(330))
    assert result["verified"] is True
    assert result["readback"]["pattern_name"] == pattern
    assert result["readback"]["pattern_scale"] == 2.5
    assert result["readback"]["pattern_angle"] == pytest.approx(330)
    assert state.boundary.deleted is False
    assert state.hatch.deleted is False


def test_acad_default_preview_does_not_create_entities_or_claim_pattern_availability(acad_backend):
    state = acad_backend
    result = state.backend.draw_hatch(boundary_handle="10", pattern="ANSI32", scale=2)
    assert result["dry_run"] is True
    assert result["would_write"] is False
    assert result["pattern"] == "ANSI32"
    assert result["pattern_availability"] == "unverified_until_creation"
    assert state.events == []


@pytest.mark.parametrize("kind", ["open_polyline", "open_ellipse", "invalid_area", "missing", "line"])
@pytest.mark.parametrize("dry_run", [True, False])
def test_acad_rejects_open_missing_or_unsupported_boundaries_before_writing(acad_backend, kind, dry_run):
    state = acad_backend
    if kind == "open_polyline":
        state.boundary.Closed = False
    elif kind == "open_ellipse":
        state.boundary = SimpleNamespace(EntityName="AcDbEllipse", StartParameter=0, EndParameter=math.pi)
    elif kind == "invalid_area":
        state.boundary.Area = float("nan")
    elif kind == "missing":
        state.boundary = None
    else:
        state.boundary.EntityName = "AcDbLine"
    with pytest.raises(ValueError):
        state.backend.draw_hatch(boundary_handle="10", dry_run=dry_run)
    assert state.events == []


def test_acad_accepts_full_ellipse_without_an_activex_area_property(acad_backend):
    state = acad_backend
    state.boundary = SimpleNamespace(
        EntityName="AcDbEllipse", StartParameter=0, EndParameter=math.tau,
        MajorRadius=4, MinorRadius=2,
    )
    result = state.backend.draw_hatch(boundary_handle="10")
    assert result["boundary_area"] == pytest.approx(8 * math.pi)
    assert state.events == []


@pytest.mark.parametrize("corrupt", ["pattern", "scale", "angle", "unavailable"])
def test_acad_failed_creation_or_readback_deletes_new_hatch_and_rolls_back(monkeypatch, acad_backend, corrupt):
    state = acad_backend
    state.corrupt = corrupt
    state.fail = corrupt == "unavailable"
    for name, value in (
        ("_acquire_autocad_lock", lambda: state.events.append(("acquire", None))),
        ("_wait_operation_ready", lambda: None),
        ("_begin_undo", lambda: state.events.append(("begin_undo", None)) or True),
        ("_commit_undo", lambda started: state.events.append(("commit", started))),
        ("_rollback_undo", lambda started: state.events.append(("rollback", started)) or True),
        ("_release_autocad_lock", lambda: state.events.append(("release", None))),
    ):
        monkeypatch.setattr(state.backend, name, value)
    backend = server._AcadLockProxy(state.backend)
    with pytest.raises(RuntimeError):
        backend.draw_hatch(boundary_handle="10", pattern="ANSI32", scale=2, angle=30, dry_run=False)
    if state.hatch is not None:
        assert state.hatch.deleted
    assert state.boundary.deleted is False
    assert ("rollback", True) in state.events
    assert not any(event[0] == "commit" for event in state.events)
    assert state.events[-1] == ("release", None)
