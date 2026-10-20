import json
import math

import pytest
from pydantic import ValidationError

from cad_mcp.drawing.document import CadDocument
from cad_mcp import server as server


NEW_DWT_TOOLS = {
    "cad_get_drawing_settings",
    "cad_set_drawing_settings",
    "cad_list_layers_detailed",
    "cad_list_textstyles",
    "cad_list_dimstyles",
    "cad_list_layouts_detailed",
    "cad_get_plot_environment",
    "cad_list_page_setups",
    "cad_convert_plot_style_mode",
    "cad_rename_layout",
    "cad_create_or_update_page_setup",
    "cad_preview_dwt_profile",
    "cad_apply_dwt_profile",
    "cad_validate_dwt_profile",
    "cad_rename_layer",
    "cad_delete_layer",
    "cad_delete_layout",
    "cad_create_or_update_attribute_block",
    "cad_set_block_attributes",
    "cad_bind_block_attribute_field",
    "cad_import_block_definitions",
    "cad_export_block_library",
    "cad_inspect_block_library",
    "cad_audit_block_definition",
    "cad_fill_closed_boundary_solid",
    "cad_insert_coordinate_label",
    "cad_insert_elevation_label",
    "cad_create_or_update_ctb",
}


def test_all_26_dwt_interfaces_are_registered_with_strict_object_schemas():
    tools = {tool.name: tool for tool in server.mcp._tool_manager.list_tools()}

    assert len(tools) == 107
    assert NEW_DWT_TOOLS.issubset(tools)
    for name in NEW_DWT_TOOLS:
        schema = tools[name].parameters
        assert schema["type"] == "object"
        assert schema["required"] == ["params"]
        for definition in schema.get("$defs", {}).values():
            if definition.get("type") == "object":
                assert definition.get("additionalProperties") is False, name


@pytest.mark.parametrize(
    "model,payload",
    [
        (server.SetDrawingSettingsInput, {"typo": True}),
        (server.DeleteLayerInput, {"names": ["X"], "typo": True}),
        (
            server.CreateOrUpdatePageSetupInput,
            {
                "name": "PDF-A3",
                "device": "real.pc3",
                "media_name": "A3",
                "plot_style_sheet": "real.ctb",
                "plot_scale": {"mode": "standard", "typo": 1},
            },
        ),
        (
            server.CreateOrUpdateAttributeBlockInput,
            {
                "block_name": "TAG",
                "attribute_definitions": [{"tag": "A", "typo": True}],
            },
        ),
    ],
)
def test_new_tool_models_reject_unknown_fields_recursively(model, payload):
    with pytest.raises(ValidationError, match="extra_forbidden"):
        model.model_validate(payload)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_new_tool_models_reject_non_finite_numbers(bad):
    with pytest.raises(ValidationError):
        server.InsertCoordinateLabelInput(target_x=bad, target_y=0)
    with pytest.raises(ValidationError):
        server.PageSetupScaleInput(mode="custom", numerator=1, denominator=bad)
    with pytest.raises(ValidationError):
        server.AttributeDefinitionInput(tag="A", position=[0, bad])


def test_dxf_only_boundaries_return_structured_unsupported_mode(monkeypatch, tmp_path):
    backend = CadDocument(tmp_path)
    backend.new_drawing("unsupported.dxf")
    monkeypatch.setattr(server, "_backend", backend)
    before = len(backend.doc.entitydb)

    responses = [
        server.cad_bind_block_attribute_field(
            server.BindBlockAttributeFieldInput(
                block_handle="1",
                attribute_tag="X",
                source_handle="2",
                source_property="x",
            )
        ),
        server.cad_inspect_block_library(
            server.InspectBlockLibraryInput(source_path=str(tmp_path / "library.dwg"))
        ),
        server.cad_create_or_update_ctb(
            server.CreateOrUpdateCtbInput(
                name="TEST.ctb", target_directory=str(tmp_path), dry_run=True
            )
        ),
    ]

    for raw in responses:
        result = json.loads(raw)
        assert result["ok"] is False
        assert result["error_detail"]["code"] == "UNSUPPORTED_MODE"
        assert result["error_detail"]["retryable"] is False
    assert len(backend.doc.entitydb) == before
    assert not (tmp_path / "TEST.ctb").exists()


def test_dry_run_attribute_block_is_zero_write(monkeypatch, tmp_path):
    backend = CadDocument(tmp_path)
    backend.new_drawing("dry-run.dxf")
    monkeypatch.setattr(server, "_backend", backend)
    before = len(backend.doc.blocks)

    result = json.loads(server.cad_create_or_update_attribute_block(
        server.CreateOrUpdateAttributeBlockInput(
            block_name="TAG",
            attribute_definitions=[server.AttributeDefinitionInput(tag="VALUE")],
            dry_run=True,
        )
    ))

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["would_write"] is False
    assert len(backend.doc.blocks) == before
