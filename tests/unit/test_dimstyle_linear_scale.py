import math

import pytest
from pydantic import ValidationError

from cad_mcp.drawing.autocad import AcadBackend
from cad_mcp.drawing.document import CadDocument
from cad_mcp.server import CreateOrUpdateDimStyleInput


def test_dimstyle_input_defaults_to_unscaled_measurements_and_rejects_invalid_factors():
    params = CreateOrUpdateDimStyleInput(name="P-DIM-ANG-100", overall_scale=100)

    assert params.linear_scale_factor == 1.0
    for invalid_factor in (0.0, -0.001, math.nan, math.inf, -math.inf):
        with pytest.raises(ValidationError):
            CreateOrUpdateDimStyleInput(
                name="P-DIM-LIN-M-100",
                overall_scale=100,
                linear_scale_factor=invalid_factor,
            )


def test_dxf_dimstyle_writes_and_reads_linear_scale_factor(tmp_path):
    backend = CadDocument(tmp_path)
    backend.new_drawing("dimstyle-linear-scale.dxf")

    result = backend.create_or_update_dimstyle(
        "P-DIM-LIN-M-100",
        100,
        3.5,
        2.5,
        0.8,
        precision=3,
        linear_scale_factor=0.001,
    )
    style = backend.doc.dimstyles.get("P-DIM-LIN-M-100")
    environment = backend.get_drafting_environment()

    assert style.dxf.dimlfac == pytest.approx(0.001)
    assert result["linear_scale_factor"] == pytest.approx(0.001)
    assert environment["dimstyle"]["linear_scale_factor"] == pytest.approx(0.001)


def test_acad_dimstyle_sets_dimlfac_before_copying_style(monkeypatch, tmp_path):
    events = []

    class FakeDimStyle:
        def CopyFrom(self, document):
            events.append(("copy_from", document))

    class FakeDimStyles:
        def __init__(self):
            self.style = FakeDimStyle()

        def Item(self, name):
            raise KeyError(name)

        def Add(self, name):
            events.append(("add", name))
            return self.style

    class FakeDocument:
        def __init__(self):
            self.ActiveDimStyle = object()
            self.DimStyles = FakeDimStyles()
            self.variables = {}

        def SetVariable(self, name, value):
            events.append(("set", name, value))
            self.variables[name] = value

        def GetVariable(self, name):
            return self.variables[name]

        def Regen(self, mode):
            events.append(("regen", mode))

    class FakeModelSpace:
        Count = 0

    backend = AcadBackend(tmp_path)
    monkeypatch.setattr(backend, "_connect", lambda: None)
    backend.doc = FakeDocument()
    backend.msp = FakeModelSpace()

    result = backend.create_or_update_dimstyle(
        "P-DIM-LIN-M-100",
        100,
        3.5,
        2.5,
        0.8,
        precision=3,
        linear_scale_factor=0.001,
    )

    dimlfac_index = events.index(("set", "DIMLFAC", 0.001))
    copy_index = next(index for index, event in enumerate(events) if event[0] == "copy_from")
    assert dimlfac_index < copy_index
    assert result["linear_scale_factor"] == pytest.approx(0.001)
