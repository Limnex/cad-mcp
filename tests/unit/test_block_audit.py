import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cad_mcp.engineering.block_audit import (
    BlockCheck,
    BlockSpecCard,
    SymmetryTest,
    build_block_audit,
    compare_check,
    load_block_audit_manifest,
)
from cad_mcp.drawing.autocad import AcadBackend


class Collection:
    def __init__(self, items):
        self.items = list(items)

    @property
    def Count(self):
        return len(self.items)

    def Item(self, index):
        return self.items[index]


class Entity(SimpleNamespace):
    def GetBoundingBox(self):
        return self.bounds


class Block:
    def __init__(self, name, entities):
        self.Name = name
        self.Origin = (0.0, 0.0, 0.0)
        self.entities = list(entities)

    @property
    def Count(self):
        return len(self.entities)

    def Item(self, index):
        return self.entities[index]


def line(handle, start, end, layer="GEOM"):
    return Entity(
        EntityName="AcDbLine",
        Handle=handle,
        Layer=layer,
        Linetype="ByLayer",
        Lineweight=-1,
        Color=256,
        StartPoint=(*start, 0.0),
        EndPoint=(*end, 0.0),
        Length=((end[0] - start[0]) ** 2 + (end[1] - start[1]) ** 2) ** 0.5,
        bounds=(
            (min(start[0], end[0]), min(start[1], end[1]), 0.0),
            (max(start[0], end[0]), max(start[1], end[1]), 0.0),
        ),
    )


def rectangle_entities():
    return [
        line("1", (0, 0), (10, 0)),
        line("2", (10, 0), (10, 10)),
        line("3", (10, 10), (0, 10)),
        line("4", (0, 10), (0, 0)),
    ]


def spec(expected_fingerprint=None):
    return BlockSpecCard(
        name="BOX",
        fingerprint_quantization=0.001,
        expected_fingerprint=expected_fingerprint,
        connection_tolerance=0.001,
        symmetry_tests=[
            SymmetryTest(
                id="horizontal",
                kind="reflection",
                tolerance=0.001,
                axis_point=(0, 5, 0),
                axis_direction=(1, 0, 0),
                center=None,
                order=None,
            ),
            SymmetryTest(
                id="vertical",
                kind="reflection",
                tolerance=0.001,
                axis_point=(5, 0, 0),
                axis_direction=(0, 1, 0),
                center=None,
                order=None,
            ),
        ],
        checks=[
            BlockCheck(
                id="size",
                path="/geometry/bounding_box/size",
                operator="within",
                expected=[10, 10, 0],
                tolerance=0.001,
            ),
            BlockCheck(
                id="layers",
                path="/properties/layers",
                operator="unordered_equals",
                expected=["GEOM"],
                tolerance=None,
            ),
        ],
    )


def test_manifest_is_strict_requires_suffix_and_absolute_source(tmp_path):
    source = tmp_path / "blocks.dwg"
    payload = {
        "schema_version": "1.0",
        "source_dwg": str(source.resolve()),
        "source_sha256": "0" * 64,
        "blocks": [spec().model_dump(mode="json")],
    }
    valid = tmp_path / "blocks_manifest.json"
    valid.write_text(json.dumps(payload), encoding="utf-8")

    resolved, manifest = load_block_audit_manifest(valid)

    assert resolved == valid.resolve()
    assert manifest.blocks[0].expected_fingerprint is None
    payload["typo"] = True
    valid.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValidationError, match="extra_forbidden"):
        load_block_audit_manifest(valid)
    wrong_name = tmp_path / "manifest.json"
    wrong_name.write_text(json.dumps({key: value for key, value in payload.items() if key != "typo"}), encoding="utf-8")
    with pytest.raises(ValueError, match="_manifest.json"):
        load_block_audit_manifest(wrong_name)


def test_block_audit_returns_tree_geometry_symmetry_comparison_and_fingerprint():
    block = Block("BOX", rectangle_entities())
    document = SimpleNamespace(Blocks=Collection([block]))

    result = build_block_audit(block, document, spec())

    assert result["block"]["entity_tree_complete"] is True
    assert result["geometry"]["bounding_box"]["size"] == [10.0, 10.0, 0.0]
    assert result["geometry"]["center"]["bbox_center"] == [5.0, 5.0, 0.0]
    assert [item["status"] for item in result["geometry"]["symmetry"]] == ["通过", "通过"]
    assert result["properties"]["entity_types"] == {"AcDbLine": 4}
    assert len(result["fingerprint"]["sha256"]) == 64
    assert result["comparison"]["overall_status"] == "无法验证"
    assert result["comparison"]["counts"] == {"通过": 4, "无法验证": 1}
    assert {
        item["id"] for item in result["comparison"]["items"]
    } >= {"symmetry:horizontal", "symmetry:vertical"}


def test_geometry_fingerprint_is_independent_of_entity_order_and_handles():
    first = Block("BOX", rectangle_entities())
    reversed_entities = list(reversed(rectangle_entities()))
    for index, entity in enumerate(reversed_entities, start=100):
        entity.Handle = str(index)
    second = Block("BOX", reversed_entities)

    first_result = build_block_audit(first, SimpleNamespace(Blocks=Collection([first])), spec())
    second_result = build_block_audit(second, SimpleNamespace(Blocks=Collection([second])), spec())

    assert first_result["fingerprint"]["sha256"] == second_result["fingerprint"]["sha256"]


def test_unknown_geometry_never_claims_complete_or_pass():
    unknown = Entity(
        EntityName="AcDbProxyEntity",
        Handle="9",
        Layer="X",
        Linetype="ByLayer",
        Lineweight=-1,
        Color=256,
        bounds=((0, 0, 0), (1, 1, 0)),
    )
    block = Block("BOX", [unknown])

    result = build_block_audit(block, SimpleNamespace(Blocks=Collection([block])), spec())

    assert result["block"]["entity_tree_complete"] is False
    assert result["fingerprint"]["complete"] is False
    assert result["comparison"]["overall_status"] != "通过"
    assert any(item["id"] == "entity_tree_completeness" for item in result["comparison"]["items"])


def test_symmetry_deviation_is_included_in_overall_status():
    entities = rectangle_entities()
    entities.append(line("5", (10, 10), (12, 10)))
    block = Block("BOX", entities)

    result = build_block_audit(block, SimpleNamespace(Blocks=Collection([block])), spec())

    assert result["comparison"]["overall_status"] == "偏差"
    assert any(
        item["id"].startswith("symmetry:") and item["status"] == "偏差"
        for item in result["comparison"]["items"]
    )


def test_comparison_uses_three_states_without_defaulting_missing_actual():
    document = {"value": 10, "items": ["A", "B"]}

    passed = compare_check(document, BlockCheck(
        id="value", path="/value", operator="within", expected=10.01, tolerance=0.02
    ))
    deviation = compare_check(document, BlockCheck(
        id="items", path="/items", operator="contains", expected=["C"], tolerance=None
    ))
    unverifiable = compare_check(document, BlockCheck(
        id="missing", path="/missing", operator="equals", expected=0, tolerance=None
    ))

    assert passed["status"] == "通过"
    assert deviation["status"] == "偏差"
    assert unverifiable["status"] == "无法验证"
    assert unverifiable["actual"] is None


def test_acad_block_audit_keeps_source_unchanged_and_uses_explicit_manifest(
    monkeypatch, tmp_path
):
    source = tmp_path / "library.dwg"
    source.write_bytes(b"read-only source")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_path = tmp_path / "library_manifest.json"
    manifest_path.write_text(json.dumps({
        "schema_version": "1.0",
        "source_dwg": str(source.resolve()),
        "source_sha256": source_hash,
        "blocks": [spec().model_dump(mode="json")],
    }), encoding="utf-8")
    block = Block("BOX", rectangle_entities())
    document = SimpleNamespace(Blocks=Collection([block]))
    backend = AcadBackend(tmp_path)
    monkeypatch.setattr(
        backend,
        "_open_source_document_readonly",
        lambda _source: (document, True, None),
    )
    closed = []
    monkeypatch.setattr(
        backend,
        "_close_source_document",
        lambda _document, opened_here, previous: closed.append((opened_here, previous)),
    )

    result = backend.audit_block_definition(
        source_path=str(source),
        block_name="BOX",
        manifest_path=str(manifest_path),
    )

    assert result["source_unchanged"] is True
    assert result["source_hash"] == source_hash
    assert result["block"]["name"] == "BOX"
    assert closed == [(True, None)]
    assert source.read_bytes() == b"read-only source"



def test_block_audit_byref_bounds_match_direct_audit_without_mutating_entities(monkeypatch):
    entities = rectangle_entities()
    block = Block("BOX", entities)
    document = SimpleNamespace(Blocks=Collection([block]))
    reference = build_block_audit(block, document, spec())
    before = [dict(vars(e)) for e in entities]

    def GetBoundingBox(self, minimum, maximum):
        minimum.value, maximum.value = self.bounds

    monkeypatch.setattr(Entity, "GetBoundingBox", GetBoundingBox)
    assert build_block_audit(block, document, spec()) == reference
    assert [dict(vars(e)) for e in entities] == before


def test_block_audit_invalid_bounds_remain_unverified(monkeypatch):
    entities = rectangle_entities()
    block = Block("BOX", entities)

    def GetBoundingBox(self, minimum, maximum):
        minimum.value, maximum.value = (0., 0., 0.), (float("inf"), 10., 0.)

    monkeypatch.setattr(Entity, "GetBoundingBox", GetBoundingBox)
    result = build_block_audit(block, SimpleNamespace(Blocks=Collection([block])), spec())
    assert result["geometry"]["bounding_box"] is None
    assert result["block"]["entity_tree_complete"] is False
    assert result["fingerprint"]["complete"] is False
    assert all("bounding_box" in node["unverified_fields"]
               for node in result["entity_tree"]["entities"])
    size = next(item for item in result["comparison"]["items"] if item["id"] == "size")
    assert size["status"] == "无法验证"
