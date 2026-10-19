"""Opt-in real AutoCAD smoke test for the DWT baseline contract.

The default suite must never attach to or start a user's AutoCAD session.  Set
CAD_MCP_RUN_ACAD_INTEGRATION=1 explicitly to create one isolated, unsaved
drawing, verify settings readback, and close that drawing without saving.
"""

from __future__ import annotations

import os

import pytest

from cad_mcp.drawing.autocad import AcadBackend
from cad_mcp.server import _AcadLockProxy


pytestmark = pytest.mark.acad

if os.environ.get("CAD_MCP_RUN_ACAD_INTEGRATION") != "1":
    pytest.skip(
        "set CAD_MCP_RUN_ACAD_INTEGRATION=1 to run the isolated AutoCAD smoke test",
        allow_module_level=True,
    )


def test_real_autocad_dwt_settings_roundtrip_without_save(tmp_path):
    cad = _AcadLockProxy(AcadBackend(tmp_path))
    opened = False
    try:
        created = cad.new_drawing("cad-mcp-dwt-integration.dwg")
        opened = True
        assert created["mode"] == "acad"

        result = cad.set_drawing_settings(
            insertion_units="millimeters",
            measurement_system="metric",
            linear_unit_format="decimal",
            linear_precision=2,
            angular_unit_format="decimal_degrees",
            angular_precision=2,
            associative_dimensions=True,
            mirror_text=False,
            display_lineweights=True,
        )
        assert result["after"]["insertion_units"] == "millimeters"
        assert result["after"]["measurement_system"] == "metric"
        assert result["after"]["linear_precision"] == 2
        assert result["after"]["associative_dimensions"] is True
        assert result["after"]["mirror_text"] is False
        assert not result["after"].get("unverified_fields")
    finally:
        if opened:
            cad.close_drawing(save=False)
