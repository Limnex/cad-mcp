import json
import math
from types import SimpleNamespace

from cad_mcp.drawing.autocad import AcadBackend
from cad_mcp.drawing.document import CadDocument
from cad_mcp import server as server


class Collection:
    def __init__(self, items):
        self.items = list(items)

    @property
    def Count(self):
        return len(self.items)

    def Item(self, key):
        if isinstance(key, int):
            return self.items[key]
        for item in self.items:
            if getattr(item, "Name", None) == key:
                return item
        raise KeyError(key)


class FakeDocument:
    def __init__(self):
        self.variables = {
            "INSUNITS": 4,
            "MEASUREMENT": 1,
            "LUNITS": 2,
            "LUPREC": 3,
            "AUNITS": 0,
            "AUPREC": 2,
            "DIMASSOC": 2,
            "MIRRTEXT": 0,
            "LWDISPLAY": 1,
            "PSTYLEMODE": 1,
        }
        layer_zero = SimpleNamespace(
            Name="0", Color=7, Linetype="Continuous", Lineweight=-3,
            Plottable=True, Description="", Lock=False, Freeze=False,
        )
        layer_wall = SimpleNamespace(
            Name="A-WALL", Color=3, Linetype="Continuous", Lineweight=25,
            Plottable=True, Description="墙体", Lock=False, Freeze=False,
        )
        style = SimpleNamespace(
            Name="GENERIC", FontFile="txt.shx", BigFontFile="",
            Height=2.5, Width=0.8, ObliqueAngle=math.radians(15),
        )
        self.Layers = Collection([layer_zero, layer_wall])
        self.TextStyles = Collection([style])
        self.Blocks = Collection([
            Collection([
                SimpleNamespace(Layer="A-WALL"),
                SimpleNamespace(Layer="A-WALL"),
            ])
        ])
        self.ActiveLayer = layer_wall
        self.ActiveLayout = SimpleNamespace(Name="A1")
        self.ActiveTextStyle = style

    def GetVariable(self, name):
        return self.variables[name]

    def SetVariable(self, *_args):
        raise AssertionError("只读接口不应写入系统变量")


def test_dxf_readonly_tools_report_actual_values_without_writes(tmp_path):
    backend = CadDocument(tmp_path)
    backend.new_drawing("readonly.dxf")
    backend.set_active_layer("DETAIL")
    backend.create_or_update_textstyle(
        "NOTE", 2.5, "txt.shx", "", 1.0, True
    )
    backend.draw_line(0, 0, 10, 0, "DETAIL")
    entity_count_before = len(backend.doc.entitydb)

    settings = backend.get_drawing_settings()
    layers = backend.list_layers_detailed()
    textstyles = backend.list_textstyles()

    assert len(backend.doc.entitydb) == entity_count_before
    assert settings["insertion_units"] == "millimeters"
    assert settings["current_layer"] == "DETAIL"
    detail = next(item for item in layers["layers"] if item["name"] == "DETAIL")
    assert detail["entity_count"] == 1
    note = next(item for item in textstyles["textstyles"] if item["name"] == "NOTE")
    assert note["font_file"] == "txt.shx"
    assert note["model_height"] == 2.5
    assert note["annotative"] is None
    assert "text_styles.NOTE.annotative" in textstyles["unverified_fields"]


def test_dxf_variable_height_textstyle_preserves_textsize(tmp_path):
    backend = CadDocument(tmp_path)
    backend.new_drawing("variable-height.dxf")
    backend.doc.header["$TEXTSIZE"] = 3.5

    result = backend.create_or_update_textstyle(
        "TXT-CN", 0, "simplex.shx", "gbcbig.shx", 0.7, True
    )

    assert backend.doc.styles.get("TXT-CN").dxf.height == 0
    assert backend.doc.header["$TEXTSTYLE"] == "TXT-CN"
    assert backend.doc.header["$TEXTSIZE"] == 3.5
    assert result["textsize_action"] == "preserved"


def test_acad_readonly_tools_use_mocked_com_without_setters(monkeypatch, tmp_path):
    backend = AcadBackend(tmp_path)
    backend.doc = FakeDocument()
    backend.msp = SimpleNamespace()
    monkeypatch.setattr(backend, "_ensure_connected", lambda: None)

    settings = backend.get_drawing_settings()
    layers = backend.list_layers_detailed()
    textstyles = backend.list_textstyles()

    assert settings["plot_style_mode"] == "ctb"
    assert settings["associative_dimensions"] is True
    assert settings["current_layout"] == "A1"
    wall = next(item for item in layers["layers"] if item["name"] == "A-WALL")
    assert wall["description"] == "墙体"
    assert wall["entity_count"] == 2
    generic = textstyles["textstyles"][0]
    assert generic["font_file"] == "txt.shx"
    assert generic["oblique_angle"] == 15.0
    assert generic["annotative"] is None
    assert backend._undo_mark_open is False


def test_mcp_readonly_wrappers_and_tool_registration(monkeypatch, tmp_path):
    backend = CadDocument(tmp_path)
    backend.new_drawing("mcp-readonly.dxf")
    monkeypatch.setattr(server, "_backend", backend)

    settings = json.loads(server.cad_get_drawing_settings(
        server.GetDrawingSettingsInput()
    ))
    layers = json.loads(server.cad_list_layers_detailed(
        server.ListLayersDetailedInput()
    ))
    textstyles = json.loads(server.cad_list_textstyles(
        server.ListTextstylesInput()
    ))
    names = {tool.name for tool in server.mcp._tool_manager.list_tools()}

    assert settings["ok"] and layers["ok"] and textstyles["ok"]
    assert {
        "cad_get_drawing_settings",
        "cad_list_layers_detailed",
        "cad_list_textstyles",
    }.issubset(names)
    assert len(names) == 108
