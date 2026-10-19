import pytest
from pydantic import ValidationError

from cad_mcp.drawing.autocad import AcadBackend
from cad_mcp.drawing.document import CadDocument
from cad_mcp.server import CreateOrUpdateDimStyleInput


def test_dimstyle_input_defaults_to_arrow_and_rejects_negative_tick_size():
    params = CreateOrUpdateDimStyleInput(name="P-DIM-RAD-100", overall_scale=100)

    assert params.oblique_stroke_size == 0.0
    with pytest.raises(ValidationError, match="greater_than_equal"):
        CreateOrUpdateDimStyleInput(
            name="P-DIM-LIN-100",
            overall_scale=100,
            oblique_stroke_size=-0.1,
        )


def test_dxf_dimstyle_writes_and_reads_oblique_stroke_size(tmp_path):
    backend = CadDocument(tmp_path)
    backend.new_drawing("dimstyle-terminators.dxf")

    result = backend.create_or_update_dimstyle(
        "P-DIM-LIN-100",
        100,
        3.5,
        2.5,
        0.8,
        set_current=True,
        oblique_stroke_size=2.5,
    )
    style = backend.doc.dimstyles.get("P-DIM-LIN-100")
    environment = backend.get_drafting_environment()

    assert style.dxf.dimasz == 2.5
    assert style.dxf.dimtsz == 2.5
    assert result["oblique_stroke_size"] == 2.5
    assert result["uses_oblique_stroke"] is True
    assert environment["dimstyle"]["oblique_stroke_size"] == 2.5
    assert environment["dimstyle"]["uses_oblique_stroke"] is True


def test_dxf_dimstyle_default_keeps_arrow_mode(tmp_path):
    backend = CadDocument(tmp_path)
    backend.new_drawing("dimstyle-arrows.dxf")

    result = backend.create_or_update_dimstyle(
        "P-DIM-ANG-100", 100, 3.5, 2.5, 0.8
    )
    style = backend.doc.dimstyles.get("P-DIM-ANG-100")

    assert style.dxf.dimtsz == 0.0
    assert result["oblique_stroke_size"] == 0.0
    assert result["uses_oblique_stroke"] is False


def test_acad_dimstyle_sets_dimtsz_before_copying_style(monkeypatch, tmp_path):
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
        "P-DIM-LIN-100",
        100,
        3.5,
        2.5,
        0.8,
        set_current=True,
        oblique_stroke_size=2.5,
    )

    dimtsz_index = events.index(("set", "DIMTSZ", 2.5))
    copy_index = next(index for index, event in enumerate(events) if event[0] == "copy_from")
    assert dimtsz_index < copy_index
    assert result["oblique_stroke_size"] == 2.5
    assert result["uses_oblique_stroke"] is True
