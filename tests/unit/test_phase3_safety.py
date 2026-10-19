from __future__ import annotations

import pytest
from pydantic import ValidationError

from cad_mcp.drawing import document as cad_drawing
from cad_mcp.core import geometry as cad_geometry
from cad_mcp.core import compiler as cad_compiler
from cad_mcp.core.contracts import CadOperation, EnvironmentModel
from cad_mcp.drawing.document import CadDocument
from cad_mcp.core.geometry import scan_line_circle_issues, trim_polyline_detailed, validate_table_data
from cad_mcp.server import ArrayRectInput, DrawTableInput


def test_open_polyline_trim_uses_pick_position_on_whole_path() -> None:
    points, closed, cuts = trim_polyline_detailed(
        [(0, 0), (10, 0), (10, 10)],
        False,
        [((5, -1), (5, 1))],
        (10, 8),
    )
    assert points == [(5.0, 0.0), (10.0, 0.0), (10.0, 10.0)]
    assert closed is False
    assert cuts == [(5.0, 0.0)]


def test_closed_polyline_trim_retains_interval_containing_pick() -> None:
    points, closed, cuts = trim_polyline_detailed(
        [(0, 0), (10, 0), (10, 10), (0, 10)],
        True,
        [((2, -1), (2, 1)), ((8, -1), (8, 1))],
        (5, 0),
    )
    assert points == [(2.0, 0.0), (8.0, 0.0)]
    assert closed is False
    assert cuts == [(2.0, 0.0), (8.0, 0.0)]


def test_trim_rejects_ambiguous_closed_and_self_crossing_paths() -> None:
    with pytest.raises(ValueError, match="至少需要两个"):
        trim_polyline_detailed(
            [(0, 0), (10, 0), (10, 10), (0, 10)],
            True,
            [((5, -1), (5, 1))],
            (8, 0),
        )
    with pytest.raises(ValueError, match="多个位置"):
        trim_polyline_detailed(
            [(0, 0), (10, 10), (0, 10), (10, 0)],
            False,
            [((2, -1), (2, 1))],
            (5, 5),
        )


def test_trim_deduplicates_cut_at_internal_vertex() -> None:
    points, _, cuts = trim_polyline_detailed(
        [(0, 0), (10, 0), (10, 10)],
        False,
        [((9, -1), (11, 1))],
        (10, 8),
    )
    assert points == [(10.0, 0.0), (10.0, 10.0)]
    assert cuts == [(10.0, 0.0)]


def test_dxf_trim_rejects_bulge_without_mutating_entity(tmp_path) -> None:
    cad = CadDocument(tmp_path)
    cad.new_drawing("bulge.dxf")
    subject = cad.msp.add_lwpolyline(
        [(0, 0, 0, 0, 1), (10, 0, 0, 0, 0)], format="xyseb"
    )
    handle = cad._handle_str(subject.dxf.handle)
    cutter = cad.draw_line(5, -10, 5, 10)["handle"]
    before = list(subject.get_points("xyb"))
    with pytest.raises(ValueError, match="圆弧段"):
        cad.trim(handle, [cutter], 8, 0)
    assert list(subject.get_points("xyb")) == before


def test_array_and_table_limits_fail_before_backend_mutation(tmp_path) -> None:
    with pytest.raises(ValidationError, match="超过单次上限"):
        ArrayRectInput(
            handles=[str(index) for index in range(100)],
            rows=200,
            cols=200,
            row_spacing=1,
            col_spacing=1,
        )
    ArrayRectInput(handles=["1"], rows=100, cols=100,
                   row_spacing=1, col_spacing=1)

    cad = CadDocument(tmp_path)
    cad.new_drawing("bulk.dxf")
    handle = cad.draw_line(0, 0, 1, 0)["handle"]
    before = len(cad.msp)
    with pytest.raises(ValueError, match="超过单次上限"):
        cad.array_rect([handle] * 100, 2, 101, 1, 1)
    assert len(cad.msp) == before

    with pytest.raises(ValueError, match="单元格"):
        validate_table_data([1] * 21, ["h"] * 21, [["x"] * 21] * 1000)
    with pytest.raises(ValidationError, match="200"):
        DrawTableInput(
            origin_x=0,
            origin_y=0,
            col_widths=[10],
            row_height=10,
            headers=["h"],
            rows=[["x" * 201]],
        )


def test_spatial_scan_reports_incomplete_instead_of_false_pass(monkeypatch) -> None:
    monkeypatch.setattr(cad_geometry, "MAX_DRAWING_COMPARISONS", 1)
    circles = [(str(index), (0.0, 0.0), 1.0) for index in range(4)]
    result = scan_line_circle_issues([], circles, 1.0)
    assert result["complete"] is False
    assert "比较次数" in str(result["stopped_reason"])


def test_drawing_check_and_entity_listing_are_bounded(monkeypatch, tmp_path) -> None:
    cad = CadDocument(tmp_path)
    cad.new_drawing("bounded.dxf")
    for index in range(3):
        cad.draw_line(index * 10, 0, index * 10 + 1, 0)

    page = cad.list_entities(offset=0, limit=2)
    assert sum("|" in line for line in page.splitlines()) == 2
    assert "offset=2" in page
    assert sum("|" in line for line in cad.list_entities(offset=2, limit=2).splitlines()) == 1

    monkeypatch.setattr(cad_drawing, "MAX_DRAWING_SCAN_ENTITIES", 2)
    report = cad.check_drawing(tolerance=1)
    assert "未完成" in report
    assert "未报告检查通过" in report


def test_compiler_applies_table_limits_to_template_tables(monkeypatch) -> None:
    model = EnvironmentModel.model_validate({
        "project_id": "bounded-plan",
        "project_name": "bounded-plan",
        "schedules": [{
            "id": "schedule",
            "source_type": "drawing",
            "columns": [{"key": "project_id", "title": "项目", "width": 10}],
            "origin": [0, 0],
            "row_height": 10,
        }],
    })
    oversized = CadOperation(
        kind="template_table",
        semantic_id="schedule",
        role="schedule",
        params={
            "col_widths": [1] * 21,
            "headers": ["h"] * 21,
            "rows": [["x"] * 21] * 1000,
        },
    )
    monkeypatch.setattr(cad_compiler, "compile_schedule", lambda *_args: [oversized])
    with pytest.raises(ValueError, match="单元格"):
        cad_compiler.compile_environment_model(model)
