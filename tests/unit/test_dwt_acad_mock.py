from pathlib import Path
from types import SimpleNamespace

import pytest

from cad_mcp.dwt import autocad as cad_dwt_acad
from cad_mcp.drawing.autocad import AcadBackend
from cad_mcp.dwt.contracts import DraftingProfileV2
from cad_mcp.core.runtime import ConflictError, UnsupportedOperationError


class Collection:
    def __init__(self, items=None):
        self.items = list(items or [])

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


class TextStyleDocument:
    """模拟 AutoCAD 对 TEXTSIZE=0 的拒绝，并记录系统变量写入。"""

    def __init__(self):
        self.style = SimpleNamespace(
            Name="TXT-CN",
            FontFile="old.shx",
            BigFontFile="",
            Height=2.5,
            Width=1.0,
            ObliqueAngle=0.0,
        )
        self.TextStyles = Collection([self.style])
        self.ActiveTextStyle = SimpleNamespace(Name="STANDARD")
        self.variables = {"TEXTSIZE": 2.5}
        self.set_variable_calls = []
        self.regen_calls = []

    def GetVariable(self, name):
        return self.variables[name]

    def SetVariable(self, name, value):
        self.set_variable_calls.append((name, value))
        if name == "TEXTSIZE" and float(value) <= 0:
            raise AssertionError("AutoCAD rejects TEXTSIZE <= 0")
        self.variables[name] = value

    def Regen(self, mode):
        self.regen_calls.append(mode)


class FakeDimStyle:
    def __init__(self, name="DWT-DIM"):
        self.Name = name
        self.copy_from_calls = 0

    def CopyFrom(self, _document):
        self.copy_from_calls += 1


class DimStyleDocument:
    """模拟 DIMDSEP 的字符串写入，并可注入异常读回值。"""

    def __init__(self, *, readback_override=None, override_enabled=False):
        self.style = FakeDimStyle()
        self.ActiveDimStyle = self.style
        self.DimStyles = Collection([self.style])
        self.TextStyles = Collection()
        self.variables = {"DIMZIN": 0}
        self.set_variable_calls = []
        self.readback_override = readback_override
        self.override_enabled = override_enabled

    def GetVariable(self, name):
        if name == "DIMDSEP" and self.override_enabled:
            return self.readback_override
        return self.variables[name]

    def SetVariable(self, name, value):
        self.set_variable_calls.append((name, value))
        if name == "DIMDSEP" and not isinstance(value, str):
            raise AssertionError("AutoCAD DIMDSEP requires a string")
        self.variables[name] = value

    def Regen(self, _mode):
        return None


class GuardedCurrentLayer:
    """AutoCAD 2026 can reject even Freeze=False on the active layer."""

    def __init__(self):
        self.Name = "0"
        self.color = 7
        self.Linetype = "Continuous"
        self.Lineweight = -3
        self.Plottable = True
        self.Description = "旧说明"
        self.Lock = False
        self._freeze = False
        self.freeze_write_count = 0

    @property
    def Freeze(self):
        return self._freeze

    @Freeze.setter
    def Freeze(self, _value):
        self.freeze_write_count += 1
        raise AssertionError("active-layer Freeze setter must not be called")


def _backend_with_guarded_current_layer(monkeypatch, tmp_path):
    layer = GuardedCurrentLayer()
    backend = AcadBackend(tmp_path)
    backend.doc = SimpleNamespace(
        ActiveLayer=layer,
        Layers=Collection([layer]),
    )
    backend.msp = SimpleNamespace()
    monkeypatch.setattr(backend, "_ensure_connected", lambda: None)
    return backend, layer


def _backend_with_textstyle_document(monkeypatch, tmp_path):
    document = TextStyleDocument()
    backend = AcadBackend(tmp_path)
    backend.doc = document
    backend.msp = SimpleNamespace()
    monkeypatch.setattr(backend, "_ensure_connected", lambda: None)
    return backend, document


def _backend_with_dimstyle_document(
    monkeypatch, tmp_path, *, readback_override=None, override_enabled=False
):
    document = DimStyleDocument(
        readback_override=readback_override,
        override_enabled=override_enabled,
    )
    backend = AcadBackend(tmp_path)
    backend.doc = document
    backend.msp = SimpleNamespace()
    monkeypatch.setattr(backend, "_ensure_connected", lambda: None)
    return backend, document


def _create_dimstyle(backend, decimal_separator=","):
    return backend.create_or_update_dimstyle(
        "DWT-DIM",
        overall_scale=1.0,
        text_height=3.5,
        arrow_size=2.5,
        text_gap=0.625,
        decimal_separator=decimal_separator,
    )


def test_dwt_variable_height_textstyle_preserves_textsize(monkeypatch, tmp_path):
    backend, document = _backend_with_textstyle_document(monkeypatch, tmp_path)
    profile = DraftingProfileV2.model_validate({
        "schema_version": "2.0",
        "profile_id": "variable-text-height",
        "name": "可变字高回归",
        "text_styles": {
            "TXT-CN": {
                "font_file": "simplex.shx",
                "big_font_file": "gbcbig.shx",
                "model_height": 0,
                "width_factor": 0.7,
                "current": True,
            },
        },
    })

    result = backend._apply_textstyle_spec(
        "TXT-CN", profile.text_styles["TXT-CN"]
    )

    assert document.style.Height == 0
    assert document.style.Width == pytest.approx(0.7)
    assert document.ActiveTextStyle is document.style
    assert document.variables["TEXTSIZE"] == pytest.approx(2.5)
    assert document.set_variable_calls == []
    assert result["textsize_action"] == "preserved"


def test_fixed_height_textstyle_updates_textsize_only_when_changed(
    monkeypatch, tmp_path
):
    backend, document = _backend_with_textstyle_document(monkeypatch, tmp_path)

    updated = backend.create_or_update_textstyle(
        "TXT-CN", 3.5, "simplex.shx", "gbcbig.shx", 0.7, True
    )
    document.set_variable_calls.clear()
    unchanged = backend.create_or_update_textstyle(
        "TXT-CN", 3.5, "simplex.shx", "gbcbig.shx", 0.7, True
    )

    assert updated["textsize_action"] == "updated"
    assert document.variables["TEXTSIZE"] == pytest.approx(3.5)
    assert unchanged["textsize_action"] == "unchanged"
    assert document.set_variable_calls == []


def test_acad_dimdsep_uses_string_and_exact_readback(monkeypatch, tmp_path):
    backend, document = _backend_with_dimstyle_document(monkeypatch, tmp_path)

    result = _create_dimstyle(backend, decimal_separator=",")

    dimdsep_write = next(
        value for name, value in document.set_variable_calls if name == "DIMDSEP"
    )
    assert type(dimdsep_write) is str
    assert dimdsep_write == ","
    assert result["decimal_separator"] == ","
    assert document.style.copy_from_calls == 1


@pytest.mark.parametrize(
    ("readback", "message"),
    [(44, "读回类型不正确"), (".", "读回不一致")],
)
def test_acad_dimdsep_rejects_invalid_readback_before_copy(
    monkeypatch, tmp_path, readback, message
):
    backend, document = _backend_with_dimstyle_document(
        monkeypatch,
        tmp_path,
        readback_override=readback,
        override_enabled=True,
    )

    with pytest.raises(RuntimeError, match=message):
        _create_dimstyle(backend, decimal_separator=",")

    assert document.style.copy_from_calls == 0
    assert "DIMEXO" not in document.variables


def test_acad_dimdsep_rejects_unsupported_character_before_writes(
    monkeypatch, tmp_path
):
    backend, document = _backend_with_dimstyle_document(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="只能是单字符"):
        _create_dimstyle(backend, decimal_separator=";")

    assert document.set_variable_calls == []
    assert document.style.copy_from_calls == 0


def test_set_layer_props_skips_same_freeze_value_on_active_layer(monkeypatch, tmp_path):
    backend, layer = _backend_with_guarded_current_layer(monkeypatch, tmp_path)

    result = backend.set_layer_props("0", description="新说明", frozen=False)

    assert layer.Description == "新说明"
    assert layer.freeze_write_count == 0
    assert "description" in result
    assert "frozen" in result


def test_set_layer_props_rejects_active_layer_freeze_before_any_write(
    monkeypatch, tmp_path
):
    backend, layer = _backend_with_guarded_current_layer(monkeypatch, tmp_path)

    with pytest.raises(ConflictError, match="当前图层"):
        backend.set_layer_props("0", description="不应写入", frozen=True)

    assert layer.Description == "旧说明"
    assert layer.freeze_write_count == 0


def test_dwt_apply_submits_only_planned_layer_differences(monkeypatch, tmp_path):
    target = tmp_path / "template.dwg"
    profile_path = tmp_path / "profile.json"
    profile = DraftingProfileV2.model_validate({
        "schema_version": "2.0",
        "profile_id": "layer-delta",
        "name": "图层差异回归",
        "layer_styles": {"0": {"description": "新说明"}},
    })
    snapshot = {
        "target": {"mode": "acad", "path": str(target), "name": target.name},
        "drawing_settings": {
            "insertion_units": "millimeters",
            "measurement_system": "metric",
            "linear_unit_format": "decimal",
            "linear_precision": 2,
            "angular_unit_format": "decimal_degrees",
            "angular_precision": 2,
            "associative_dimensions": True,
            "mirror_text": False,
            "display_lineweights": True,
            "plot_style_mode": "ctb",
            "current_layer": "0",
            "current_layout": "Model",
            "unverified_fields": [],
        },
        "layers": {
            "layers": [{
                "name": "0",
                "color": 7,
                "linetype": "CONTINUOUS",
                "lineweight": -3,
                "plot": True,
                "description": "旧说明",
                "locked": False,
                "frozen": False,
                "transparency": None,
                "is_current": True,
                "entity_count": 0,
            }],
            "unverified_fields": [],
        },
        "textstyles": {"textstyles": [], "unverified_fields": []},
        "dimstyles": {"dimstyles": [], "unverified_fields": []},
        "linetype_settings": {
            "ltscale": 1.0,
            "celtscale": 1.0,
            "msltscale": 0,
            "psltscale": 1,
        },
        "layouts": {"layouts": [], "unverified_fields": []},
        "page_setups": {"page_setups": [], "unverified_fields": []},
        "plot_environment": {
            "available_devices": [],
            "available_style_sheets": [],
            "media_by_device": {},
            "unverified_fields": [],
        },
        "block_names": [],
        "font_files": [],
        "unsupported_items": [],
    }
    backend = AcadBackend(tmp_path)
    backend.doc = SimpleNamespace()
    backend.msp = SimpleNamespace()
    monkeypatch.setattr(backend, "_ensure_connected", lambda: None)
    monkeypatch.setattr(backend, "capture_dwt_snapshot", lambda: snapshot)
    calls = []

    def set_layer_props(**kwargs):
        calls.append(kwargs)
        snapshot["layers"]["layers"][0]["description"] = kwargs["description"]
        return "说明已更新"

    monkeypatch.setattr(backend, "set_layer_props", set_layer_props)
    preview = backend.preview_dwt_profile(
        profile,
        profile_path=str(profile_path),
        profile_sha256="a" * 64,
        target_drawing=str(target),
        update_existing=True,
        preserve_unmanaged=True,
    )

    assert [item["field"] for item in preview["layers_to_update"][0]["changes"]] == [
        "description"
    ]
    result = backend.apply_dwt_profile(
        profile,
        profile_path=str(profile_path),
        profile_sha256="a" * 64,
        target_drawing=str(target),
        update_existing=True,
        preserve_unmanaged=True,
        confirmation_token=preview["confirmation_token"],
    )

    assert calls == [{"name": "0", "description": "新说明"}]
    operation = next(
        item for item in result["operations"] if item["operation"] == "set_layer_props"
    )
    assert operation["fields"] == ["description"]
    assert result["validation"]["pass"] is True


def test_dwt_apply_switches_current_layout_through_document_property(
    monkeypatch, tmp_path
):
    profile = DraftingProfileV2.model_validate({
        "schema_version": "2.0",
        "profile_id": "layout-switch",
        "name": "布局切换回归",
        "drawing_settings": {"current_layout": "Paper"},
    })
    plan = {
        "conflicts": [],
        "unsupported_items": [],
        "confirmation_required": True,
        "plan_hash": "layout-plan",
        "drawing_settings_changes": [{"field": "current_layout"}],
        "layers_to_rename": [],
        "layers_to_create": [],
        "layers_to_update": [],
        "textstyles_to_create_or_update": [],
        "dimstyles_to_create_or_update": [],
        "linetype_settings_changes": [],
        "page_setups_to_create_or_update": [],
        "layouts_to_rename": [],
        "layouts_to_create": [],
        "layouts_to_update": [],
        "block_library_actions": [],
        "layouts_to_delete": [],
        "layers_to_delete": [],
    }
    model = SimpleNamespace(Name="Model")
    paper = SimpleNamespace(Name="Paper")
    backend = AcadBackend(tmp_path)
    backend.doc = SimpleNamespace(ActiveLayout=model)
    backend.msp = SimpleNamespace()
    monkeypatch.setattr(backend, "_ensure_connected", lambda: None)
    monkeypatch.setattr(backend, "capture_dwt_snapshot", lambda: {})
    monkeypatch.setattr(
        backend,
        "get_drawing_settings",
        lambda: {"current_layout": backend.doc.ActiveLayout.Name},
    )
    monkeypatch.setattr(backend, "set_drawing_settings", lambda **values: values)
    monkeypatch.setattr(backend, "_get_layout", lambda name: paper)
    monkeypatch.setattr(cad_dwt_acad, "build_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(cad_dwt_acad, "verify_confirmation", lambda token, value: None)
    monkeypatch.setattr(
        cad_dwt_acad,
        "validate_snapshot",
        lambda *args, **kwargs: {"pass": True, "errors": []},
    )

    result = backend.apply_dwt_profile(
        profile,
        profile_path=str(tmp_path / "profile.json"),
        profile_sha256="a" * 64,
        target_drawing=str(tmp_path / "target.dwg"),
        update_existing=True,
        preserve_unmanaged=True,
        confirmation_token="confirmed",
    )

    assert backend.doc.ActiveLayout is paper
    operation = next(
        item for item in result["operations"]
        if item["operation"] == "set_current_layout"
    )
    assert operation["result"] == {"layout_name": "Paper", "verified": True}


def test_acad_drawing_settings_use_explicit_variables_and_readback(monkeypatch, tmp_path):
    variables = {
        "INSUNITS": 4,
        "MEASUREMENT": 1,
        "LUNITS": 2,
        "LUPREC": 4,
        "AUNITS": 0,
        "AUPREC": 2,
        "DIMASSOC": 2,
        "MIRRTEXT": 1,
        "LWDISPLAY": 0,
        "PSTYLEMODE": 1,
    }

    class Document:
        ActiveLayer = SimpleNamespace(Name="0")
        ActiveLayout = SimpleNamespace(Name="Model")

        def GetVariable(self, name):
            return variables[name]

        def SetVariable(self, name, value):
            variables[name] = value

    backend = AcadBackend(tmp_path)
    backend.doc = Document()
    backend.msp = SimpleNamespace()
    monkeypatch.setattr(backend, "_ensure_connected", lambda: None)

    result = backend.set_drawing_settings(
        linear_precision=3,
        angular_precision=4,
        associative_dimensions=True,
        mirror_text=False,
        display_lineweights=True,
    )

    assert variables | {
        "LUPREC": 3, "AUPREC": 4, "DIMASSOC": 2,
        "MIRRTEXT": 0, "LWDISPLAY": 1,
    } == variables
    assert result["after"]["linear_precision"] == 3
    assert result["after"]["mirror_text"] is False
    assert set(result["changed"]) >= {"linear_precision", "angular_precision", "mirror_text", "display_lineweights"}


def test_acad_current_dimstyle_full_readback_and_noncurrent_unverified(monkeypatch, tmp_path):
    variables = {
        "DIMTXSTY": "GB", "DIMTXT": 3.5, "DIMSCALE": 100.0,
        "DIMLFAC": 0.001, "DIMDEC": 2, "DIMTSZ": 2.5,
        "DIMASZ": 2.5, "DIMGAP": 0.8, "DIMTAD": 1, "DIMZIN": 0,
        "DIMDSEP": ".", "DIMEXO": 0.625, "DIMEXE": 1.25,
        "DIMDLI": 3.75, "DIMCLRD": 256, "DIMCLRE": 256,
        "DIMCLRT": 256, "DIMLWD": -1, "DIMLWE": -1,
    }
    current = SimpleNamespace(Name="DWT")
    other = SimpleNamespace(Name="OTHER")

    class Document:
        ActiveDimStyle = current
        DimStyles = Collection([current, other])

        def GetVariable(self, name):
            return variables[name]

    backend = AcadBackend(tmp_path)
    backend.doc = Document()
    backend.msp = SimpleNamespace()
    monkeypatch.setattr(backend, "_ensure_connected", lambda: None)

    result = backend.list_dimstyles()
    dwt = next(item for item in result["dimstyles"] if item["name"] == "DWT")
    other_row = next(item for item in result["dimstyles"] if item["name"] == "OTHER")

    assert dwt["linear_scale_factor"] == pytest.approx(0.001)
    assert dwt["oblique_stroke_size"] == pytest.approx(2.5)
    assert dwt["precision"] == 2
    assert dwt["suppress_trailing_zeros"] is False
    assert type(dwt["decimal_separator"]) is str
    assert dwt["decimal_separator"] == "."
    assert other_row["linear_scale_factor"] is None
    assert "dimension_styles.OTHER.linear_scale_factor" in result["unverified_fields"]


class FakePlot:
    def __init__(self, name="PDF-A3"):
        self.Name = name
        self.ConfigName = ""
        self.CanonicalMediaName = ""
        self.PlotRotation = 0
        self.StyleSheet = ""
        self.PlotType = 5
        self.PaperUnits = 1
        self._center_plot = False
        self.center_plot_write_count = 0
        self.PlotWithLineweights = True
        self.PlotWithPlotStyles = True
        self.UseStandardScale = True
        self.StandardScale = 16
        self.custom = (1.0, 1.0)

    @property
    def CenterPlot(self):
        return self._center_plot

    @CenterPlot.setter
    def CenterPlot(self, value):
        self.center_plot_write_count += 1
        if self.PlotType == 5:
            raise RuntimeError("AutoCAD rejects CenterPlot writes for acLayout")
        self._center_plot = bool(value)

    def RefreshPlotDeviceInfo(self):
        return None

    def GetCanonicalMediaNames(self):
        return ["ISO_full_bleed_A3_(420.00_x_297.00_MM)"]

    def SetCustomScale(self, numerator, denominator):
        self.custom = (numerator, denominator)

    def GetCustomScale(self):
        return self.custom


class PlotConfigurations(Collection):
    def Add(self, name, _model_type):
        item = FakePlot(name)
        self.items.append(item)
        return item


def test_acad_page_setup_create_and_idempotent_full_readback(monkeypatch, tmp_path):
    configs = PlotConfigurations()
    backend = AcadBackend(tmp_path)
    backend.doc = SimpleNamespace(PlotConfigurations=configs)
    backend.msp = SimpleNamespace()
    monkeypatch.setattr(backend, "_ensure_connected", lambda: None)
    monkeypatch.setattr(
        backend,
        "get_plot_environment",
        lambda: {
            "available_devices": ["PDF.pc3"],
            "available_style_sheets": ["ENV.ctb"],
        },
    )
    payload = dict(
        name="PDF-A3",
        device="PDF.pc3",
        media_name="ISO_full_bleed_A3_(420.00_x_297.00_MM)",
        rotation=0,
        plot_style_sheet="ENV.ctb",
        plot_type="layout",
        paper_units="millimeters",
        plot_scale={"mode": "standard", "standard_scale": "1:1"},
        center_plot=False,
        plot_with_lineweights=True,
        plot_with_plot_styles=True,
        replace=False,
    )

    first = backend.create_or_update_page_setup(**payload)
    second = backend.create_or_update_page_setup(**payload)

    assert first["created"] is True
    assert first["verified"] is True
    assert first["page_setup"]["device"] == "PDF.pc3"
    assert first["page_setup"]["plot_scale"]["standard_scale"] == "1:1"
    assert second["created"] is False
    assert second["changed"] is False
    assert configs.items[0].center_plot_write_count == 0
    assert configs.items[0].StandardScale == 16  # ActiveX ac1_1, not imperial code 1


def test_acad_page_setup_rejects_center_plot_true_for_layout_before_create(monkeypatch, tmp_path):
    configs = PlotConfigurations()
    backend = AcadBackend(tmp_path)
    backend.doc = SimpleNamespace(PlotConfigurations=configs)
    backend.msp = SimpleNamespace()
    monkeypatch.setattr(backend, "_ensure_connected", lambda: None)

    with pytest.raises(ValueError, match="acLayout"):
        backend.create_or_update_page_setup(
            name="PDF-A3",
            device="PDF.pc3",
            media_name="ISO_full_bleed_A3_(420.00_x_297.00_MM)",
            rotation=0,
            plot_style_sheet="ENV.ctb",
            plot_type="layout",
            paper_units="millimeters",
            plot_scale={"mode": "standard", "standard_scale": "1:1"},
            center_plot=True,
            plot_with_lineweights=True,
            plot_with_plot_styles=True,
            replace=False,
        )

    assert configs.Count == 0


def test_acad_page_setup_non_layout_still_writes_center_plot(monkeypatch, tmp_path):
    configs = PlotConfigurations()
    backend = AcadBackend(tmp_path)
    backend.doc = SimpleNamespace(PlotConfigurations=configs)
    backend.msp = SimpleNamespace()
    monkeypatch.setattr(backend, "_ensure_connected", lambda: None)
    monkeypatch.setattr(
        backend,
        "get_plot_environment",
        lambda: {
            "available_devices": ["PDF.pc3"],
            "available_style_sheets": ["ENV.ctb"],
        },
    )

    result = backend.create_or_update_page_setup(
        name="PDF-A3",
        device="PDF.pc3",
        media_name="ISO_full_bleed_A3_(420.00_x_297.00_MM)",
        rotation=0,
        plot_style_sheet="ENV.ctb",
        plot_type="extents",
        paper_units="millimeters",
        plot_scale={"mode": "standard", "standard_scale": "1:1"},
        center_plot=True,
        plot_with_lineweights=True,
        plot_with_plot_styles=True,
        replace=False,
    )

    assert result["verified"] is True
    assert configs.items[0].CenterPlot is True
    assert configs.items[0].center_plot_write_count == 1


def test_block_attribute_field_stores_object_field_not_static_text(monkeypatch, tmp_path):
    attribute = SimpleNamespace(TagString="X", TextString="0", Handle="B")
    attribute.Update = lambda: None
    block = SimpleNamespace(
        EntityName="AcDbBlockReference", HasAttributes=True,
        GetAttributes=lambda: [attribute],
    )
    source = SimpleNamespace(
        EntityName="AcDbBlockReference", InsertionPoint=(1000, 2000, 0),
        ObjectID=42, Handle="A",
    )
    variables = {"FIELDEVAL": 0}

    class Document:
        Utility = SimpleNamespace(GetObjectIdString=lambda obj, _hex: str(obj.ObjectID))

        def HandleToObject(self, handle):
            return {"1": block, "2": source}[handle]

        def GetVariable(self, name):
            return variables[name]

        def SetVariable(self, name, value):
            variables[name] = value

        def Regen(self, _mode):
            return None

    backend = AcadBackend(tmp_path)
    backend.doc = Document()
    backend.msp = SimpleNamespace()
    monkeypatch.setattr(backend, "_ensure_connected", lambda: None)

    from cad_mcp.drawing import native_fields
    monkeypatch.setattr(native_fields,"read_attribute_field",lambda *args:{
        "code":attribute.TextString,"references":["A"]})
    result = backend.bind_block_attribute_field(
        block_handle="1",
        attribute_tag="X",
        source_type="object",
        source_handle="2",
        source_property="insertion_x",
        linear_scale_factor=0.001,
        precision=3,
        prefix="X=",
        suffix=" m",
        update_on_regen=True,
    )

    assert "%<\\AcObjProp" in attribute.TextString
    assert "%pr3" in attribute.TextString
    assert "%ct8[0.001]" in attribute.TextString
    assert result["static_fallback"] is False
    assert variables["FIELDEVAL"] & 16


def test_ctb_unsupported_does_not_leave_empty_file(monkeypatch, tmp_path):
    backend = AcadBackend(tmp_path)
    backend.doc = SimpleNamespace()
    backend.msp = SimpleNamespace()
    monkeypatch.setattr(backend, "_ensure_connected", lambda: None)
    monkeypatch.setattr(
        backend,
        "get_plot_environment",
        lambda: {"plot_style_search_path": str(tmp_path)},
    )

    preview = backend.create_or_update_ctb(
        name="ENV.ctb",
        target_directory=str(tmp_path),
        color_policy="all_black",
        use_object_lineweight=True,
        grayscale=False,
        replace=False,
        dry_run=True,
    )
    assert preview["supported"] is False
    assert preview["file_created"] is False
    with pytest.raises(UnsupportedOperationError):
        backend.create_or_update_ctb(
            name="ENV.ctb",
            target_directory=str(tmp_path),
            color_policy="all_black",
            use_object_lineweight=True,
            grayscale=False,
            replace=False,
            dry_run=False,
        )
    assert not (tmp_path / "ENV.ctb").exists()


def test_mocked_wblock_export_returns_verified_hash_without_saving_current(
    monkeypatch, tmp_path
):
    blocks = Collection([SimpleNamespace(Name="TAG")])
    refs = []

    class Reference:
        def Delete(self):
            return None

    class ModelSpace:
        def InsertBlock(self, *_args):
            ref = Reference()
            refs.append(ref)
            return ref

    class Selection:
        def AddItems(self, items):
            self.items = items

        def Delete(self):
            return None

    class SelectionSets:
        def Add(self, _name):
            return Selection()

    class Document:
        Blocks = blocks

        def WBlock(self, filename, _selection):
            Path(filename).write_bytes(b"verified mock dwg")

        def Save(self):
            raise AssertionError("export must not save current drawing")

    backend = AcadBackend(tmp_path)
    backend.doc = Document()
    backend.doc.SelectionSets = SelectionSets()
    backend.msp = ModelSpace()
    monkeypatch.setattr(backend, "_ensure_connected", lambda: None)
    monkeypatch.setattr(cad_dwt_acad, "_dispatch_array", lambda items: list(items))
    target = tmp_path / "library.dwg"

    preview = backend.export_block_library(
        target_path=str(target), block_names=["TAG"], dry_run=True
    )
    result = backend.export_block_library(
        target_path=str(target), block_names=["TAG"], dry_run=False
    )

    assert preview["would_write"] is False
    assert result["verified"] is True
    assert result["output_hash"] == cad_dwt_acad.sha256_file(target)
    assert result["current_drawing_saved"] is False
    assert target.read_bytes() == b"verified mock dwg"


def test_mocked_cross_database_block_import_hash_and_unit_checks(monkeypatch, tmp_path):
    source_path = tmp_path / "source.dwg"
    source_path.write_bytes(b"source library")
    source_block = SimpleNamespace(Name="TAG")
    source_blocks = Collection([source_block])
    target_blocks = Collection([])

    class SourceDocument:
        Blocks = source_blocks

        def GetVariable(self, name):
            assert name == "INSUNITS"
            return 4

        def CopyObjects(self, objects, owner, id_pairs):
            assert id_pairs.varianttype == cad_dwt_acad.pythoncom.VT_BYREF | cad_dwt_acad.pythoncom.VT_VARIANT
            owner.items.extend(objects)

    class TargetDocument:
        Blocks = target_blocks

        def GetVariable(self, name):
            assert name == "INSUNITS"
            return 4

    backend = AcadBackend(tmp_path)
    backend.doc = TargetDocument()
    backend.msp = SimpleNamespace()
    monkeypatch.setattr(backend, "_ensure_connected", lambda: None)
    monkeypatch.setattr(
        backend,
        "_open_source_document_readonly",
        lambda _source: (SourceDocument(), False, None),
    )
    monkeypatch.setattr(backend, "_close_source_document", lambda *_args: None)
    monkeypatch.setattr(backend, "_yield_com", lambda: None)
    monkeypatch.setattr(cad_dwt_acad, "_dispatch_array", lambda items: list(items))

    result = backend.import_block_definitions(
        source_path=str(source_path), block_names=["TAG"], replace_existing=False
    )

    assert result["imported"] == ["TAG"]
    assert result["source_hash"] == cad_dwt_acad.sha256_file(source_path)
    assert result["source_unchanged"] is True
    assert result["verified"] is True
