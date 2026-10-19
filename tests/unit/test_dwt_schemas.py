import hashlib
import json

import pytest
from pydantic import ValidationError

from cad_mcp.dwt.contracts import DraftingProfileV2, load_dwt_profile_v2
from cad_mcp.engineering.standards import DraftingProfile, load_drafting_profile


def minimal_v2() -> dict:
    return {
        "schema_version": "2.0",
        "profile_id": "generic-mm-v2",
        "name": "通用毫米 DWT 配置",
        "standard_references": [],
        "drawing_settings": {},
        "layer_styles": {},
        "text_styles": {},
        "dimension_styles": {},
        "linetype_settings": {},
        "plot_policy": {},
        "page_setups": {},
        "layouts": {},
        "block_library": {},
        "metadata": {},
    }


def write_json(path, payload) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def test_load_v2_returns_absolute_path_hash_and_schema(tmp_path):
    path = tmp_path / "profile.json"
    write_json(path, minimal_v2())

    profile, evidence = load_dwt_profile_v2(path)
    through_legacy_entrypoint, entrypoint_evidence = load_drafting_profile(path)

    assert isinstance(profile, DraftingProfileV2)
    assert isinstance(through_legacy_entrypoint, DraftingProfileV2)
    assert evidence["path"] == str(path.resolve())
    assert evidence["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert evidence["schema_version"] == "2.0"
    assert entrypoint_evidence["sha256"] == evidence["sha256"]


def test_nested_unknown_fields_and_strict_types_are_rejected():
    payload = minimal_v2()
    payload["drawing_settings"] = {"linear_precision": 2, "typo": True}
    with pytest.raises(ValidationError, match="extra_forbidden"):
        DraftingProfileV2.model_validate(payload)

    payload = minimal_v2()
    payload["drawing_settings"] = {"linear_precision": "2"}
    with pytest.raises(ValidationError, match="int_type"):
        DraftingProfileV2.model_validate(payload)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_json_constants_are_rejected(tmp_path, constant):
    path = tmp_path / "non-finite.json"
    path.write_text(
        json.dumps(minimal_v2(), ensure_ascii=False)[:-1]
        + f', "metadata": {{"bad": {constant}}}}}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="非有限数值"):
        load_dwt_profile_v2(path)


def test_non_finite_nested_python_value_is_rejected():
    payload = minimal_v2()
    payload["metadata"] = {"nested": [{"bad": float("inf")}]}
    with pytest.raises(ValidationError, match="finite"):
        DraftingProfileV2.model_validate(payload)


def test_invalid_enum_is_rejected():
    payload = minimal_v2()
    payload["plot_policy"] = {"target_mode": "both"}
    with pytest.raises(ValidationError, match="literal_error"):
        DraftingProfileV2.model_validate(payload)


@pytest.mark.parametrize("separator", [".", ","])
def test_dimension_style_decimal_separator_accepts_supported_values(separator):
    payload = minimal_v2()
    payload["dimension_styles"] = {
        "DWT-DIM": {
            "text_style": "STANDARD",
            "text_height": 3.5,
            "arrow_size": 2.5,
            "decimal_separator": separator,
        }
    }

    profile = DraftingProfileV2.model_validate(payload)

    assert profile.dimension_styles["DWT-DIM"].decimal_separator == separator


def test_dimension_style_decimal_separator_rejects_other_character():
    payload = minimal_v2()
    payload["dimension_styles"] = {
        "DWT-DIM": {
            "text_style": "STANDARD",
            "text_height": 3.5,
            "arrow_size": 2.5,
            "decimal_separator": ";",
        }
    }

    with pytest.raises(ValidationError, match="literal_error"):
        DraftingProfileV2.model_validate(payload)


def test_profile_path_must_be_absolute(monkeypatch, tmp_path):
    path = tmp_path / "profile.json"
    write_json(path, minimal_v2())
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="完整绝对路径"):
        load_drafting_profile("profile.json")


def test_non_utf8_profile_is_rejected(tmp_path):
    path = tmp_path / "utf16.json"
    path.write_text(json.dumps(minimal_v2(), ensure_ascii=False), encoding="utf-16")

    with pytest.raises(ValueError, match="UTF-8"):
        load_dwt_profile_v2(path)


def test_block_library_path_and_hash_are_verified(tmp_path):
    library = tmp_path / "library.dwg"
    library.write_bytes(b"isolated block library fixture")
    expected = hashlib.sha256(library.read_bytes()).hexdigest()
    payload = minimal_v2()
    payload["block_library"] = {
        "sources": [{
            "source_path": str(library),
            "expected_sha256": expected,
            "block_names": ["GENERIC_TAG"],
        }]
    }
    path = tmp_path / "profile.json"
    write_json(path, payload)

    _, evidence = load_dwt_profile_v2(path)

    assert evidence["verified_block_libraries"] == [{
        "source_path": str(library.resolve()),
        "sha256": expected,
        "verified": True,
        "block_names": ["GENERIC_TAG"],
    }]

    payload["block_library"]["sources"][0]["expected_sha256"] = "0" * 64
    write_json(path, payload)
    with pytest.raises(ValueError, match="SHA-256 不匹配"):
        load_dwt_profile_v2(path)


def test_backend_resource_references_are_deferred_not_guessed(tmp_path):
    payload = minimal_v2()
    payload["text_styles"] = {
        "GENERIC": {"font_file": "verified-later.shx"}
    }
    payload["plot_policy"] = {
        "target_mode": "ctb",
        "required_style_sheets": ["verified-later.ctb"],
        "preferred_device": "verified-later.pc3",
    }
    path = tmp_path / "profile.json"
    write_json(path, payload)

    _, evidence = load_dwt_profile_v2(path)

    assert "font:verified-later.shx" in evidence["backend_verification_required"]
    assert "plot_style_sheet:verified-later.ctb" in evidence["backend_verification_required"]
    assert "plot_device:verified-later.pc3" in evidence["backend_verification_required"]


def test_legacy_v1_profile_remains_compatible(tmp_path):
    payload = {
        "schema_version": "1.0",
        "profile_id": "legacy",
        "name": "旧配置",
        "paper_text_heights": [3.5],
        "base_lineweights_mm": [0.25],
    }
    path = tmp_path / "legacy.json"
    write_json(path, payload)

    profile, evidence = load_drafting_profile(path)

    assert isinstance(profile, DraftingProfile)
    assert evidence["schema_version"] == "1.0"
