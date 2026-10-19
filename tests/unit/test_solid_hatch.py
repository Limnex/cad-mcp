import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cad_mcp import server
from cad_mcp.drawing import autocad as cad_autocad
from cad_mcp.drawing.autocad import AcadBackend
from cad_mcp.drawing.document import CadDocument


def test_solid_fill_dxf_dry_run_is_zero_write_and_formal_call_reads_back(tmp_path):
    backend = CadDocument(tmp_path)
    backend.new_drawing("solid-fill.dxf")
    before = len(backend.msp)

    preview = backend.fill_closed_boundary_solid(
        boundary_handle=None,
        points=[(0, 0), (10, 0), (10, 5), (0, 5)],
        layer="FILL",
        associative=True,
        keep_boundary=True,
        dry_run=True,
    )
    result = backend.fill_closed_boundary_solid(
        boundary_handle=None,
        points=[(0, 0), (10, 0), (10, 5), (0, 5)],
        layer="FILL",
        associative=True,
        keep_boundary=True,
        dry_run=False,
    )

    assert preview["would_write"] is False
    assert len(backend.msp) == before + 2
    assert result["verified"] is True
    assert result["boundary_area"] == 50.0
    assert result["readback"] == {
        "entity_type": "HATCH",
        "pattern_name": "SOLID",
        "solid_fill": True,
        "associative": True,
        "layer": "FILL",
        "loop_count": 1,
        "bounding_box": {"min": [0.0, 0.0], "max": [10.0, 5.0]},
    }


def test_solid_fill_rejects_self_intersection_before_writing(tmp_path):
    backend = CadDocument(tmp_path)
    backend.new_drawing("bad-boundary.dxf")
    before = len(backend.msp)

    with pytest.raises(ValueError, match="自交"):
        backend.fill_closed_boundary_solid(
            boundary_handle=None,
            points=[(0, 0), (10, 10), (0, 10), (10, 0)],
            layer="FILL",
            associative=False,
            keep_boundary=False,
            dry_run=False,
        )

    assert len(backend.msp) == before


def test_solid_fill_input_requires_exactly_one_boundary_and_explicit_policy():
    with pytest.raises(ValidationError):
        server.FillClosedBoundarySolidInput(
            boundary_handle=None,
            points=None,
            layer="FILL",
            associative=False,
            keep_boundary=False,
            dry_run=True,
        )
    with pytest.raises(ValidationError):
        server.FillClosedBoundarySolidInput(
            boundary_handle="1",
            points=[(0, 0), (1, 0), (0, 1)],
            layer="FILL",
            associative=False,
            keep_boundary=True,
            dry_run=True,
        )
    with pytest.raises(ValidationError):
        server.FillClosedBoundarySolidInput(
            boundary_handle=None,
            points=[(0, 0), (1, 0), (0, 1)],
            layer="FILL",
            associative=True,
            keep_boundary=False,
            dry_run=True,
        )


class FakeBoundary:
    EntityName = "AcDbPolyline"
    Handle = "A"
    Closed = True
    Area = 50.0

    def Delete(self):
        self.deleted = True


class FakeHatch:
    EntityName = "AcDbHatch"
    PatternName = "SOLID"
    AssociativeHatch = True
    Layer = "FILL"
    NumberOfLoops = 1
    Area = 50.0
    Handle = "B"

    def __init__(self, events):
        self.events = events
        self.deleted = False

    def AppendOuterLoop(self, items):
        self.events.append(("append", items))

    def Evaluate(self):
        self.events.append(("evaluate", None))

    def GetBoundingBox(self):
        return (0.0, 0.0, 0.0), (10.0, 5.0, 0.0)

    def Delete(self):
        self.deleted = True


def test_solid_fill_acad_appends_immediately_and_verifies_readback(monkeypatch, tmp_path):
    events = []
    boundary = FakeBoundary()
    hatch = FakeHatch(events)

    class ModelSpace:
        def AddLightWeightPolyline(self, _coordinates):
            events.append(("boundary", None))
            return boundary

        def AddHatch(self, pattern_type, pattern_name, associative, object_type):
            events.append(("add_hatch", (pattern_type, pattern_name, associative, object_type)))
            return hatch

    class Document:
        def Regen(self, mode):
            events.append(("regen", mode))

    backend = AcadBackend(tmp_path)
    backend.doc = Document()
    backend.msp = ModelSpace()
    monkeypatch.setattr(backend, "_ensure_connected", lambda: None)
    monkeypatch.setattr(backend, "_apply_layer", lambda layer: events.append(("layer", layer)))
    monkeypatch.setattr(cad_autocad, "_dispatch_array", lambda items: list(items))

    result = backend.fill_closed_boundary_solid(
        boundary_handle=None,
        points=[(0, 0), (10, 0), (10, 5), (0, 5)],
        layer="FILL",
        associative=True,
        keep_boundary=True,
        dry_run=False,
    )

    add_index = next(index for index, event in enumerate(events) if event[0] == "add_hatch")
    assert events[add_index + 1][0] == "append"
    assert result["verified"] is True
    assert result["readback"]["pattern_name"] == "SOLID"
    assert hatch.deleted is False


def test_new_tools_are_registered_and_dxf_dwg_audit_fails_closed(monkeypatch, tmp_path):
    names = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    assert "cad_audit_block_definition" in names
    assert "cad_fill_closed_boundary_solid" in names

    backend = CadDocument(tmp_path)
    backend.new_drawing("audit-unsupported.dxf")
    monkeypatch.setattr(server, "_backend", backend)
    response = json.loads(server.cad_audit_block_definition(
        server.AuditBlockDefinitionInput(
            source_path=str(tmp_path / "blocks.dwg"),
            block_name="BOX",
            manifest_path=str(tmp_path / "blocks_manifest.json"),
        )
    ))

    assert response["ok"] is False
    assert response["error_detail"]["code"] == "UNSUPPORTED_MODE"
