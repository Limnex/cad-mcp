import inspect

import pytest
from pydantic import ValidationError

from cad_mcp import server as server
from cad_mcp.core.contracts import OutlineSpec


def test_all_tool_input_models_use_strict_base():
    input_models = [
        value
        for name, value in vars(server).items()
        if name.endswith("Input")
        and name != "ToolInput"
        and inspect.isclass(value)
        and issubclass(value, server.ToolInput)
    ]

    assert len(input_models) == 111
    assert all(model.model_config.get("extra") == "forbid" for model in input_models)
    assert all(model.model_config.get("allow_inf_nan") is False for model in input_models)


def test_unknown_tool_field_is_rejected():
    with pytest.raises(ValidationError, match="extra_forbidden"):
        server.NewDrawingInput.model_validate(
            {"filename": "plant.dxf", "filenmae": "typo.dxf"}
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_tool_coordinate_is_rejected(value):
    with pytest.raises(ValidationError, match="finite_number"):
        server.DrawLineInput.model_validate(
            {"x1": value, "y1": 0, "x2": 10, "y2": 10}
        )


def test_non_finite_semantic_model_value_is_rejected():
    with pytest.raises(ValidationError, match="finite_number"):
        OutlineSpec(shape="circle", radius=float("inf"))
