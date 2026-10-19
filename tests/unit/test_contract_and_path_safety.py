from __future__ import annotations

import pytest
import ezdxf
from pydantic import ValidationError

from cad_mcp.drawing.autocad import AcadBackend
from cad_mcp.core.contracts import (
    EnvironmentModel,
    Facility,
    OutlineSpec,
    ProfileDefinition,
    RuleDefinition,
    ScheduleColumn,
    ScheduleDefinition,
    ViewDefinition,
)
from cad_mcp.drawing.document import CadDocument
from cad_mcp.core.rules import RuleSet, load_rule_set
from cad_mcp.core.validation import validate_model


def _model(**changes) -> EnvironmentModel:
    values = {
        "project_id": "P1",
        "project_name": "测试项目",
    }
    values.update(changes)
    return EnvironmentModel(**values)


def _facility(**changes) -> Facility:
    values = {
        "id": "F1",
        "name": "水池",
        "facility_type": "tank",
        "outline": OutlineSpec(shape="rectangle", length=10, width=5),
    }
    values.update(changes)
    return Facility(**values)


def test_semantic_contract_rejects_nested_non_finite_values() -> None:
    with pytest.raises(ValidationError, match="不能包含"):
        _model(metadata={"nested": {"value": float("nan")}})


def test_semantic_contract_rejects_unknown_schema_version() -> None:
    with pytest.raises(ValidationError, match="literal_error"):
        _model(schema_version="9.9")


def test_rule_contract_rejects_unknown_target_type() -> None:
    with pytest.raises(ValidationError, match="literal_error"):
        RuleDefinition(
            rule_id="R1", target_type="facility_typo", operator="required")


def test_rule_set_rejects_nested_non_finite_values() -> None:
    with pytest.raises(ValidationError, match="不能包含"):
        RuleSet(
            rule_set_id="RS1", name="规则集",
            metadata={"nested": [float("inf")]})


def test_rule_loader_requires_absolute_path() -> None:
    with pytest.raises(ValueError, match="绝对路径"):
        load_rule_set("rules.json")


def test_rule_loader_rejects_non_standard_json_constants(tmp_path) -> None:
    path = tmp_path / "rules.json"
    path.write_text(
        '{"schema_version":"1.0","rule_set_id":"RS1","name":"规则集",'
        '"rules":[],"metadata":{"bad":NaN}}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="非有限数值"):
        load_rule_set(path)


def test_inside_rule_preserves_requested_error_severity() -> None:
    rule = RuleDefinition(
        rule_id="R1", target_type="facility", operator="inside", severity="error")
    report = validate_model(_model(), [rule])
    assert report.ok is False
    assert report.issues[-1].severity == "error"


def test_cross_references_and_unsupported_clipping_are_errors() -> None:
    profile = ProfileDefinition(
        id="PR1", name="纵断面", datum_elevation=0,
        horizontal_scale_denominator=100,
        vertical_scale_denominator=50,
        network_system_id="N404", segment_ids=["S404"],
    )
    schedule = ScheduleDefinition(
        id="SC1", view_id="V404", source_type="facility",
        columns=[ScheduleColumn(key="name", title="名称", width=20)],
        origin=(0, 0), row_height=5,
    )
    view = ViewDefinition(
        id="V1", name="平面图", view_type="plan", scale_denominator=100,
        clipping_polygon=[(0, 0), (10, 0), (0, 10)],
    )
    report = validate_model(_model(profiles=[profile], schedules=[schedule], views=[view]))
    rule_ids = {issue.rule_id for issue in report.issues}
    assert report.ok is False
    assert {"PROFILE_UNKNOWN_NETWORK", "SCHEDULE_UNKNOWN_VIEW",
            "VIEW_CLIPPING_UNSUPPORTED"} <= rule_ids


def test_relation_type_error_becomes_validation_issue() -> None:
    rule = RuleDefinition(
        rule_id="R1", target_type="facility", operator="relation",
        fields=["attributes", "name"], parameters={"comparison": "lt"})
    report = validate_model(_model(facilities=[_facility()]), [rule])
    assert report.ok is False
    assert any(issue.rule_id == "R1" and "无法比较" in issue.message
               for issue in report.issues)


def test_invalid_range_configuration_becomes_validation_issue() -> None:
    rule = RuleDefinition(
        rule_id="R1", target_type="facility", operator="range",
        fields=["rotation"], parameters={"min": "not-a-number"})
    report = validate_model(_model(facilities=[_facility()]), [rule])
    assert report.ok is False
    assert any(issue.rule_id == "RULE_CONFIGURATION_INVALID"
               for issue in report.issues)


@pytest.mark.parametrize(
    "parameters",
    [{"min": "1e999"}, {"min": 10, "max": 5}],
)
def test_invalid_range_boundaries_become_configuration_issues(parameters) -> None:
    rule = RuleDefinition(
        rule_id="R1", target_type="facility", operator="range",
        fields=["rotation"], parameters=parameters)
    report = validate_model(_model(facilities=[_facility()]), [rule])
    assert report.ok is False
    assert any(issue.rule_id == "RULE_CONFIGURATION_INVALID"
               for issue in report.issues)


def test_non_finite_numeric_string_becomes_validation_issue() -> None:
    rule = RuleDefinition(
        rule_id="R1", target_type="facility", operator="range",
        fields=["attributes.threshold"])
    report = validate_model(
        _model(facilities=[_facility(attributes={"threshold": "NaN"})]),
        [rule],
    )
    assert report.ok is False
    assert any(issue.rule_id == "R1" and "有限数值" in issue.message
               for issue in report.issues)


@pytest.mark.parametrize(
    "rule",
    [
        RuleDefinition(
            rule_id="R-required", target_type="facility", operator="required"),
        RuleDefinition(
            rule_id="R-style", target_type="facility", operator="style_match",
            parameters={"expected": ["not", "an", "object"]}),
        RuleDefinition(
            rule_id="R-artifact", target_type="project",
            operator="artifact_present", parameters={"required_types": "plan"}),
        RuleDefinition(
            rule_id="R-network", target_type="facility", operator="connectivity"),
    ],
)
def test_malformed_rule_combinations_fail_closed(rule) -> None:
    report = validate_model(_model(), [rule])
    assert report.ok is False
    assert any(issue.rule_id == "RULE_CONFIGURATION_INVALID"
               for issue in report.issues)


def test_schedule_format_mismatch_becomes_validation_issue() -> None:
    schedule = ScheduleDefinition(
        id="SC1", source_type="facility",
        columns=[ScheduleColumn(
            key="name", title="名称", width=20, format="length_2")],
        origin=(0, 0), row_height=5,
    )
    report = validate_model(_model(facilities=[_facility()], schedules=[schedule]))
    assert report.ok is False
    assert any(issue.rule_id == "SCHEDULE_FORMAT_INVALID"
               for issue in report.issues)


@pytest.mark.parametrize("backend_type", [CadDocument, AcadBackend])
@pytest.mark.parametrize(
    "filename",
    ["", ".", "..", r"sub\plant.dwg", "sub/plant.dwg", "CON", "LPT1.dxf",
     "plant.", "plant ", "bad?.dwg", "bad\x01.dwg", "a" * 256],
)
def test_unsafe_output_filenames_are_rejected(tmp_path, backend_type, filename) -> None:
    backend = backend_type(tmp_path)
    with pytest.raises(ValueError):
        backend._safe_path(filename)


def test_output_extensions_are_normalized_inside_drawings_directory(tmp_path) -> None:
    assert CadDocument(tmp_path)._safe_path("plant").name == "plant.dxf"
    assert AcadBackend(tmp_path)._safe_path("plant").name == "plant.dwg"
    assert AcadBackend(tmp_path)._safe_path("plant.dxf").name == "plant.dxf"


def test_absolute_path_is_only_allowed_for_opening_existing_dxf(tmp_path) -> None:
    managed = tmp_path / "managed"
    external = tmp_path / "external"
    external.mkdir()
    target = external / "项目 总图.dxf"
    ezdxf.new().saveas(target)
    backend = CadDocument(managed)

    with pytest.raises(ValueError, match="文件名非法"):
        backend._safe_path(str(target))
    message = backend.open_drawing(str(target))

    assert target.name in message
    assert backend.current_path == target.resolve()
    assert backend.doc is not None
    backend.msp.add_line((0, 0), (1, 1))
    backend.save()
    assert len(ezdxf.readfile(target).modelspace()) == 1

    with pytest.raises(ValueError, match="文件名非法"):
        backend.save_as(str(external / "copy.dxf"))
    with pytest.raises(ValueError, match="文件名非法"):
        AcadBackend(managed)._safe_path(str(external / "copy.dwg"))


@pytest.mark.parametrize("backend_type, suffix", [
    (CadDocument, ".dxf"),
    (AcadBackend, ".dwg"),
])
def test_open_rejects_missing_absolute_file(tmp_path, backend_type, suffix) -> None:
    backend = backend_type(tmp_path / "managed")
    with pytest.raises(FileNotFoundError, match="不存在或不是普通文件"):
        backend.open_drawing(str(tmp_path / "external" / f"missing{suffix}"))


@pytest.mark.parametrize("value", [
    r"\\server\share\drawing.dwg",
    r"C:\folder\..\drawing.dwg",
])
def test_acad_open_rejects_unsafe_absolute_path_before_connect(
    tmp_path, monkeypatch, value,
) -> None:
    backend = AcadBackend(tmp_path / "managed")
    monkeypatch.setattr(backend, "_connect", lambda: pytest.fail("must not connect"))
    with pytest.raises(ValueError, match="本机盘符绝对路径"):
        backend.open_drawing(value)


def test_acad_open_rejects_wrong_extension_before_connect(tmp_path, monkeypatch) -> None:
    target = tmp_path / "external" / "drawing.pdf"
    backend = AcadBackend(tmp_path / "managed")
    monkeypatch.setattr(backend, "_connect", lambda: pytest.fail("must not connect"))
    with pytest.raises(ValueError, match="扩展名非法"):
        backend.open_drawing(str(target))


def test_backend_operations_reject_misleading_file_extensions(tmp_path) -> None:
    with pytest.raises(ValueError, match="只接受 .dxf"):
        CadDocument(tmp_path)._safe_path("plant.dwg")

    backend = AcadBackend(tmp_path)
    with pytest.raises(ValueError, match="扩展名非法"):
        backend._safe_path("plant.pdf")
    assert backend._safe_path(
        "report", default_suffix=".pdf", allowed_suffixes=(".pdf",)
    ).name == "report.pdf"


def test_acad_dwg_writers_reject_dxf_targets_before_connect(tmp_path) -> None:
    backend = AcadBackend(tmp_path)
    with pytest.raises(ValueError, match="扩展名非法"):
        backend.new_drawing("wrong.dxf")
    source = tmp_path / "source.dwg"
    source.write_bytes(b"test source")
    with pytest.raises(ValueError, match="扩展名非法"):
        backend.clone_template_drawing(str(source), "wrong.dxf")


@pytest.mark.parametrize("method_name", ["clone_template_drawing", "inspect_template_drawing"])
def test_acad_template_source_requires_absolute_path_before_connect(
    tmp_path, method_name,
) -> None:
    backend = AcadBackend(tmp_path)
    method = getattr(backend, method_name)
    arguments = (("relative.dwg", "copy.dwg")
                 if method_name == "clone_template_drawing"
                 else ("relative.dwg",))
    with pytest.raises(ValueError, match="绝对路径"):
        method(*arguments)
