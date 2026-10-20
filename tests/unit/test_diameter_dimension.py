import asyncio
import json
import math
from types import SimpleNamespace

import ezdxf
import pytest
from pydantic import ValidationError

from cad_mcp import server
from cad_mcp.drawing import autocad
from cad_mcp.drawing.autocad import AcadBackend
from cad_mcp.drawing.document import CadDocument


@pytest.mark.parametrize("edge", [(50, 0), (0, -50), (30, 40), (-30, -40)])
def test_dxf_creates_native_diameter_and_preserves_it_on_reload(tmp_path, edge):
    backend = CadDocument(tmp_path)
    backend.new_drawing("diameter.dxf")
    result = backend.draw_diameter_dimension(10, 20, 10 + edge[0], 20 + edge[1])
    dimension = backend.doc.entitydb[f"{int(result['handle']):X}"]

    assert dimension.dxftype() == "DIMENSION"
    assert dimension.dimtype == 3
    assert dimension.get_measurement() == pytest.approx(100)
    assert tuple(dimension.dxf.defpoint) == (10 + edge[0], 20 + edge[1], 0)
    assert tuple(dimension.dxf.defpoint4) == (10 - edge[0], 20 - edge[1], 0)
    assert result["diameter"] == 100
    assert result["kind"] == "diameter"
    assert result["layer"] == "DIM"
    assert result["dimstyle"] == backend.active_dimstyle
    assert any("%%c" in entity.dxf.text.lower()
               for entity in dimension.virtual_entities() if entity.dxftype() == "MTEXT")

    path = tmp_path / "roundtrip.dxf"
    backend.doc.saveas(path)
    reopened = ezdxf.readfile(path)
    saved = list(reopened.modelspace().query("DIMENSION"))
    assert len(saved) == 1
    assert saved[0].dimtype == 3
    assert saved[0].get_measurement() == pytest.approx(100)
    assert not reopened.audit().has_errors


def test_dxf_diameter_inherits_style_without_scaling_returned_geometry(tmp_path):
    backend = CadDocument(tmp_path)
    backend.new_drawing("diameter-style.dxf")
    backend.create_or_update_dimstyle(
        "DIAMETER-M", 10, 3.5, 2.5, 0.8, precision=3, linear_scale_factor=0.001)
    backend.create_layer("DIAMETERS", 3)
    result = backend.draw_diameter_dimension(0, 0, 500, 0, layer="DIAMETERS")
    dimension = next(iter(backend.msp.query("DIMENSION")))
    style = backend.doc.dimstyles.get(dimension.dxf.dimstyle)
    rendered_text = [entity.dxf.text for entity in dimension.virtual_entities()
                     if entity.dxftype() == "MTEXT"]

    assert result["diameter"] == 1000
    assert result["layer"] == "DIAMETERS"
    assert result["dimstyle"] == "DIAMETER-M"
    assert style.dxf.dimlfac == 0.001
    assert style.dxf.dimdec == 3
    assert style.dxf.dimscale == 10
    assert style.dxf.dimtxt == 3.5
    assert style.dxf.dimasz == 2.5
    assert any("%%c1" in text.lower() for text in rendered_text)


@pytest.mark.parametrize("position", [(100, 80), (-100, -80), (0, 0)])
def test_dxf_diameter_accepts_explicit_text_position(tmp_path, position):
    backend = CadDocument(tmp_path)
    backend.new_drawing("diameter-position.dxf")
    backend.draw_diameter_dimension(0, 0, 30, 40, text_x=position[0], text_y=position[1])
    dimension = next(iter(backend.msp.query("DIMENSION")))

    assert dimension.dimtype == 3
    assert dimension.get_measurement() == pytest.approx(100)
    assert tuple(dimension.dxf.text_midpoint) == (*position, 0)
    assert dimension.dxf.dimtype & dimension.USER_LOCATION_OVERRIDE


def test_dxf_diameter_does_not_round_small_measurement_to_zero(tmp_path):
    backend = CadDocument(tmp_path)
    backend.new_drawing("small-diameter.dxf")
    result = backend.draw_diameter_dimension(0, 0, 0.001, 0)
    assert result["diameter"] == pytest.approx(0.002)


INVALID_GEOMETRIES = [
    {"cx": 0, "cy": 0, "edge_x": 0, "edge_y": 0},
    {"cx": 0, "cy": 0, "edge_x": 1, "edge_y": 0, "text_x": 5},
    {"cx": 0, "cy": 0, "edge_x": 1, "edge_y": 0, "text_y": 5},
    {"cx": math.nan, "cy": 0, "edge_x": 1, "edge_y": 0},
    {"cx": 0, "cy": math.inf, "edge_x": 1, "edge_y": 0},
    {"cx": 0, "cy": 0, "edge_x": -math.inf, "edge_y": 0},
    {"cx": 0, "cy": 0, "edge_x": 1, "edge_y": 0, "text_x": 5, "text_y": math.nan},
    {"cx": 1e308, "cy": 0, "edge_x": -1e308, "edge_y": 0},
    {"cx": 0, "cy": 0, "edge_x": 1e308, "edge_y": 0},
    {"cx": 1.5e308, "cy": 0, "edge_x": 1e308, "edge_y": 0},
]


@pytest.mark.parametrize("payload", INVALID_GEOMETRIES)
def test_input_rejects_invalid_diameter_geometry(payload):
    with pytest.raises(ValidationError):
        server.DrawDiameterDimensionInput(**payload)


@pytest.mark.parametrize("payload", INVALID_GEOMETRIES)
@pytest.mark.parametrize("backend_type", [CadDocument, AcadBackend])
def test_backends_reject_invalid_geometry_before_accessing_document(tmp_path, monkeypatch, backend_type, payload):
    backend = backend_type(tmp_path)

    def unexpected_access():
        raise AssertionError("invalid geometry accessed the document")

    monkeypatch.setattr(backend, "_check_doc", unexpected_access)
    with pytest.raises(ValueError):
        backend.draw_diameter_dimension(**payload)


@pytest.mark.parametrize("extra", [{"typo": 1}, {"layer": ""}, {"layer": None}])
def test_input_rejects_unknown_fields_and_invalid_layer(extra):
    with pytest.raises(ValidationError):
        server.DrawDiameterDimensionInput(cx=0, cy=0, edge_x=1, edge_y=0, **extra)


@pytest.mark.parametrize("position", [None, (100, -80)])
def test_acad_diameter_uses_opposite_endpoints_and_optional_text(tmp_path, monkeypatch, position):
    backend = AcadBackend(tmp_path)
    events = []
    dimension = SimpleNamespace(
        Handle="AB", Layer="DIAMETERS", StyleName="CURRENT-STYLE", TextPosition=None,
        Update=lambda: events.append("update"))

    def add_diameter(first, second, leader):
        events.append(("create", first, second, leader))
        return dimension

    monkeypatch.setattr(autocad, "_pt", lambda x, y, z=0: (x, y, z))
    monkeypatch.setattr(backend, "_check_doc", lambda: events.append("check"))
    monkeypatch.setattr(backend, "_apply_layer", lambda layer: events.append(("layer", layer)))
    backend.msp = SimpleNamespace(AddDimDiametric=add_diameter)
    kwargs = {} if position is None else {"text_x": position[0], "text_y": position[1]}
    result = backend.draw_diameter_dimension(10, 20, 40, 60, "DIAMETERS", **kwargs)

    assert events[:3] == ["check", ("layer", "DIAMETERS"),
                          ("create", (40, 60, 0), (-20, -20, 0), 5)]
    assert dimension.TextPosition == (None if position is None else (*position, 0))
    assert events[3:] == ([] if position is None else ["update"])
    assert result == {"type": "DIMENSION", "handle": "171", "layer": "DIAMETERS",
                      "kind": "diameter", "diameter": 100, "dimstyle": "CURRENT-STYLE"}


@pytest.mark.parametrize("fail", [False, True])
def test_diameter_write_participates_in_acad_undo_group(fail):
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
            assert started is True
            events.append("commit")

        def _rollback_undo(self, started):
            assert started is True
            events.append("rollback")
            return True

        def _release_autocad_lock(self):
            events.append("release")

        def draw_diameter_dimension(self, *args, **kwargs):
            events.append("write")
            if fail:
                raise RuntimeError("text position failed")
            return {"diameter": 100}

    proxy = server._AcadLockProxy(Backend())
    if fail:
        with pytest.raises(RuntimeError) as caught:
            proxy.draw_diameter_dimension(0, 0, 30, 40)
        assert caught.value.rollback_status == "succeeded"
    else:
        assert proxy.draw_diameter_dimension(0, 0, 30, 40) == {"diameter": 100}
    assert events == ["acquire", "ready", "begin", "write",
                      "rollback" if fail else "commit", "release"]


def test_dxf_failed_render_does_not_leave_a_dimension(tmp_path, monkeypatch):
    from ezdxf.entities.dimstyleoverride import DimStyleOverride

    backend = CadDocument(tmp_path)
    backend.new_drawing("failed-render.dxf")

    def fail_render(self):
        raise RuntimeError("render failed")

    monkeypatch.setattr(DimStyleOverride, "render", fail_render)
    with pytest.raises(RuntimeError, match="render failed"):
        backend.draw_diameter_dimension(0, 0, 30, 40)
    assert not list(backend.msp.query("DIMENSION"))


def test_mcp_diameter_registration_gateway_and_backend_result(tmp_path, monkeypatch):
    backend = CadDocument(tmp_path)
    backend.new_drawing("mcp-diameter.dxf")
    monkeypatch.setattr(server, "_backend", backend)
    tool = server.mcp._tool_manager.get_tool("cad_draw_diameter_dimension")
    assert tool.annotations.readOnlyHint is False
    schema = tool.parameters["$defs"]["DrawDiameterDimensionInput"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"cx", "cy", "edge_x", "edge_y"}

    response = json.loads(asyncio.run(server._gateway_call(server.GatewayCallParams(
        tool_name="cad_draw_diameter_dimension",
        arguments={"params": {"cx": 0, "cy": 0, "edge_x": 30, "edge_y": 40}}))))
    assert response["ok"] is True
    assert response["diameter"] == 100
    assert response["kind"] == "diameter"
    assert len(list(backend.msp.query("DIMENSION"))) == 1


def test_mcp_diameter_reports_document_precondition_error(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_backend", CadDocument(tmp_path))
    response = json.loads(server.cad_draw_diameter_dimension(
        server.DrawDiameterDimensionInput(cx=0, cy=0, edge_x=30, edge_y=40)))
    assert response["ok"] is False
    assert response["error_detail"]["code"] == "PRECONDITION_FAILED"
    assert response["error_detail"]["operation"] == "cad_draw_diameter_dimension"
