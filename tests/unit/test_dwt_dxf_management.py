import json

import pytest

from cad_mcp.drawing.document import CadDocument
from cad_mcp import server as server


def test_dxf_drawing_settings_write_and_full_readback(tmp_path):
    backend = CadDocument(tmp_path)
    backend.new_drawing("settings.dxf")

    result = backend.set_drawing_settings(
        linear_precision=3,
        angular_precision=4,
        associative_dimensions=True,
        mirror_text=False,
        display_lineweights=True,
    )

    assert result["before"]["linear_precision"] != 3
    assert result["after"]["linear_precision"] == 3
    assert result["after"]["angular_precision"] == 4
    assert result["after"]["associative_dimensions"] is True
    assert result["after"]["mirror_text"] is False
    assert result["after"]["display_lineweights"] is True
    assert "linear_precision" in result["changed"]


def test_dxf_layer_create_update_rename_delete_and_guards(tmp_path):
    backend = CadDocument(tmp_path)
    backend.new_drawing("layers.dxf")
    backend.create_layer(
        "TEMP", 3, "DASHED", 35, False, "临时层", True, 40
    )
    layer = next(item for item in backend.list_layers_detailed()["layers"] if item["name"] == "TEMP")
    assert layer | {
        "color": 3,
        "linetype": "DASHED",
        "lineweight": 35,
        "plot": False,
        "description": "临时层",
        "locked": True,
        "transparency": 40,
    } == layer

    backend.set_layer_props(
        "TEMP", color=5, plot=True, description="更新", locked=False,
        frozen=True, transparency=20,
    )
    changed = next(item for item in backend.list_layers_detailed()["layers"] if item["name"] == "TEMP")
    assert changed["color"] == 5
    assert changed["plot"] is True
    assert changed["description"] == "更新"
    assert changed["locked"] is False
    assert changed["frozen"] is True
    assert changed["transparency"] == 20

    backend.rename_layer("TEMP", "RENAMED")
    assert "RENAMED" in backend.doc.layers and "TEMP" not in backend.doc.layers
    with pytest.raises(ValueError, match="保护图层"):
        backend.rename_layer("0", "ZERO")

    backend.draw_line(0, 0, 10, 0, "RENAMED")
    preview = backend.delete_layer(
        ["RENAMED", "0"], require_empty=False, allow_nonempty=True, dry_run=True
    )
    assert preview["results"][0]["status"] == "would_delete"
    assert preview["results"][1]["status"] == "skipped"
    result = backend.delete_layer(
        ["RENAMED"], require_empty=False, allow_nonempty=True, dry_run=False
    )
    assert result["results"][0]["moved_to_layer_0"] == 1
    assert "RENAMED" not in backend.doc.layers
    assert next(iter(backend.msp)).dxf.layer == "0"


def test_dxf_dimstyle_complete_roundtrip_keeps_fixed_trailing_zero_format(tmp_path):
    backend = CadDocument(tmp_path)
    backend.new_drawing("dimstyle.dxf")

    result = backend.create_or_update_dimstyle(
        "DWT-DIM",
        overall_scale=100,
        text_height=3.5,
        arrow_size=2.5,
        text_gap=0.8,
        precision=2,
        text_style="GB",
        oblique_stroke_size=2.5,
        linear_scale_factor=0.001,
        suppress_leading_zeros=False,
        suppress_trailing_zeros=False,
        decimal_separator=".",
        extension_line_offset=0.625,
        extension_line_extension=1.25,
        baseline_spacing=3.75,
        dimension_line_color=256,
        extension_line_color=256,
        text_color=256,
        dimension_line_lineweight=-1,
        extension_line_lineweight=-1,
    )
    readback = next(
        item for item in backend.list_dimstyles()["dimstyles"]
        if item["name"] == "DWT-DIM"
    )

    assert result["format_example_12_5"] == "12.50"
    assert readback["precision"] == 2
    assert readback["suppress_trailing_zeros"] is False
    assert readback["linear_scale_factor"] == pytest.approx(0.001)
    assert readback["oblique_stroke_size"] == pytest.approx(2.5)
    assert readback["extension_line_offset"] == pytest.approx(0.625)
    assert readback["baseline_spacing"] == pytest.approx(3.75)


@pytest.mark.parametrize(("separator", "code"), [(".", 46), (",", 44)])
def test_dxf_dimstyle_keeps_integer_decimal_separator_code(
    tmp_path, separator, code
):
    backend = CadDocument(tmp_path)
    backend.new_drawing(f"dimstyle-{code}.dxf")

    result = backend.create_or_update_dimstyle(
        "DWT-DIM",
        overall_scale=1.0,
        text_height=3.5,
        arrow_size=2.5,
        text_gap=0.625,
        decimal_separator=separator,
    )
    style = backend.doc.dimstyles.get("DWT-DIM")
    readback = next(
        item for item in backend.list_dimstyles()["dimstyles"]
        if item["name"] == "DWT-DIM"
    )

    assert type(style.dxf.dimdsep) is int
    assert style.dxf.dimdsep == code
    assert result["decimal_separator"] == separator
    assert readback["decimal_separator"] == separator


def test_dxf_layout_rename_delete_and_model_last_layout_protection(tmp_path):
    backend = CadDocument(tmp_path)
    backend.new_drawing("layouts.dxf")
    backend.doc.layouts.new("A1")

    renamed = backend.rename_layout("A1", "A-01")
    assert renamed == {"before": "A1", "after": "A-01", "verified": True}
    with pytest.raises(ValueError, match="Model"):
        backend.rename_layout("Model", "MODEL-NEW")
    protected = backend.delete_layout(["Model"], dry_run=True)
    assert protected["results"][0]["status"] == "skipped"
    deleted = backend.delete_layout(["A-01"], dry_run=False)
    assert deleted["results"][0]["status"] == "deleted"
    last = backend.delete_layout(["Layout1"], dry_run=True)
    assert last["results"][0]["reason"] == "不能删除最后一个图纸空间布局"


def test_dxf_attribute_block_fill_and_missing_tag_report(tmp_path):
    backend = CadDocument(tmp_path)
    backend.new_drawing("attributes.dxf")
    created = backend.create_or_update_attribute_block(
        "TAG", [0, 0, 0], [],
        [{"tag": "VALUE", "prompt": "值", "default_value": "0", "position": [0, 0, 0]}],
    )
    inserted = backend.insert_block(
        "TAG", 10, 20, attributes={"VALUE": "12.50", "UNKNOWN": "x"},
        layer="ANNO", x_scale=2, y_scale=3, z_scale=1,
    )
    updated = backend.set_block_attributes(
        inserted["handle"], {"VALUE": "13.50", "UNKNOWN": "x"}
    )

    assert created["attribute_tags"] == ["VALUE"]
    assert inserted["attributes_updated"] == ["VALUE"]
    assert inserted["missing_tags"] == ["UNKNOWN"]
    assert inserted["scales"] == {"x": 2.0, "y": 3.0, "z": 1.0}
    assert updated["updated"] == ["VALUE"]
    assert updated["missing_tags"] == ["UNKNOWN"]


@pytest.mark.parametrize(
    "z,positive,zero_format,expected",
    [(1500, True, "±0.000", "+1.500"), (-1500, True, "±0.000", "-1.500"), (0, True, "±0.000", "±0.000")],
)
def test_static_elevation_formats_positive_negative_and_zero(
    tmp_path, z, positive, zero_format, expected
):
    backend = CadDocument(tmp_path)
    backend.new_drawing(f"elevation-{z}.dxf")

    result = backend.insert_elevation_label(
        target_point=[0, 0, z],
        target_handle=None,
        elevation_source="auto",
        display_unit="m",
        precision=3,
        suppress_trailing_zeros=False,
        associative=False,
        block_name="ELEV",
        layer="ANNO",
        text_style="GB",
        positive_sign=positive,
        zero_format=zero_format,
    )

    assert result["value"] == expected
    assert result["associative"] is False


def test_coordinate_label_is_three_decimals_and_associative_dxf_refuses_zero_write(tmp_path):
    backend = CadDocument(tmp_path)
    backend.new_drawing("coordinate.dxf")
    static = backend.insert_coordinate_label(
        target_x=12500,
        target_y=-2500,
        target_handle=None,
        display_unit="m",
        precision=3,
        suppress_trailing_zeros=False,
        associative=False,
        block_name="COORD",
        layer="ANNO",
        text_style="GB",
    )
    count = len(backend.msp)

    assert static["values"] == {"X": "12.500", "Y": "-2.500"}
    with pytest.raises(Exception, match="未生成静态文字"):
        backend.insert_coordinate_label(
            target_x=0,
            target_y=0,
            target_handle=None,
            display_unit="m",
            precision=3,
            suppress_trailing_zeros=False,
            associative=True,
            block_name="COORD2",
            layer="ANNO",
            text_style="GB",
        )
    assert len(backend.msp) == count


def test_mcp_layer_extension_and_unknown_tag_result(monkeypatch, tmp_path):
    backend = CadDocument(tmp_path)
    backend.new_drawing("wrappers.dxf")
    monkeypatch.setattr(server, "_backend", backend)

    created = json.loads(server.cad_create_layer(server.CreateLayerInput(
        name="PLOT-OFF", plot=False, description="不出图", locked=True,
        transparency=30,
    )))

    assert created["ok"] is True
    layer = next(item for item in backend.list_layers_detailed()["layers"] if item["name"] == "PLOT-OFF")
    assert layer["plot"] is False
    assert layer["description"] == "不出图"
    assert layer["locked"] is True
    assert layer["transparency"] == 30
