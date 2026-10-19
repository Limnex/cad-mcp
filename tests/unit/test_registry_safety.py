from __future__ import annotations

import pytest

from cad_mcp.core.contracts import CadOperation, DrawingPlan
from cad_mcp.drawing.document import CadDocument
from cad_mcp.core.registry import (
    ManagedEntityRecord,
    operation_fingerprint,
    pack_metadata,
    reconcile_plan,
    unpack_metadata,
)
from cad_mcp.core.transactions import execute_operation


def _operation() -> CadOperation:
    return CadOperation(
        kind="line",
        semantic_id="S1",
        view_id="V1",
        role="geometry",
        params={"x1": 0, "y1": 0, "x2": 1, "y2": 0},
    )


def _plan(operation: CadOperation | None = None) -> DrawingPlan:
    return DrawingPlan(
        project_id="P2",
        revision="2",
        view_ids=["V1"],
        operations=[] if operation is None else [operation],
    )


def _record(handle: str, fingerprint: str, **kwargs) -> ManagedEntityRecord:
    return ManagedEntityRecord(
        handle=handle,
        semantic_id="S1",
        view_id="V1",
        role="geometry",
        geometry_hash=fingerprint,
        **kwargs,
    )


def test_replace_scope_never_deletes_foreign_project() -> None:
    foreign = _record("P1-H1", "old", project_id="P1")
    difference = reconcile_plan(_plan(), [foreign], "replace_scope")
    assert difference["delete"] == []
    assert difference["conflict"] == []


def test_replace_scope_requires_explicit_nonempty_view_scope() -> None:
    plan = DrawingPlan(project_id="P2", revision="2", operations=[])
    with pytest.raises(ValueError, match="view_id"):
        reconcile_plan(plan, [], "replace_scope")


def test_legacy_record_blocks_replace_scope_without_being_deleted() -> None:
    legacy = _record("LEGACY", "old")
    difference = reconcile_plan(_plan(), [legacy], "replace_scope")
    assert difference["delete"] == []
    assert difference["conflict"][0]["reason"] == "legacy_missing_project_id"


def test_multi_handle_operation_is_consumed_as_one_exact_group() -> None:
    operation = _operation()
    fingerprint = operation_fingerprint(operation)
    records = [
        _record("A", fingerprint, project_id="P2", operation_group=fingerprint,
                entity_index=0, entity_count=2),
        _record("B", fingerprint, project_id="P2", operation_group=fingerprint,
                entity_index=1, entity_count=2),
    ]
    difference = reconcile_plan(_plan(operation), records, "replace_scope")
    assert difference["delete"] == []
    assert difference["replace"] == []
    assert difference["unchanged"][0]["handles"] == ["A", "B"]


def test_changed_multi_handle_operation_replaces_whole_unambiguous_group() -> None:
    operation = _operation()
    records = [
        _record("A", "old", project_id="P2", operation_group="old-group"),
        _record("B", "old", project_id="P2", operation_group="old-group"),
    ]
    difference = reconcile_plan(_plan(operation), records, "update_managed")
    assert difference["replace"][0]["handles"] == ["A", "B"]
    assert difference["delete"] == []


def test_missing_member_of_exact_multi_handle_group_forces_rebuild() -> None:
    operation = _operation()
    fingerprint = operation_fingerprint(operation)
    remaining = _record(
        "A", fingerprint, project_id="P2", operation_group=fingerprint,
        entity_index=0, entity_count=2)
    difference = reconcile_plan(_plan(operation), [remaining], "update_managed")
    assert difference["unchanged"] == []
    assert difference["replace"][0]["handles"] == ["A"]
    assert difference["replace"][0]["incomplete_group"] is True


def test_exact_groups_are_reserved_before_changed_groups() -> None:
    changed = _operation()
    exact = changed.model_copy(update={
        "params": {"x1": 10, "y1": 0, "x2": 11, "y2": 0}})
    exact_fingerprint = operation_fingerprint(exact)
    records = [
        _record("OLD", "old-group", project_id="P2",
                operation_group="old-group"),
        _record("EXACT", exact_fingerprint, project_id="P2",
                operation_group=exact_fingerprint),
    ]
    plan = DrawingPlan(
        project_id="P2", revision="2", view_ids=["V1"],
        operations=[changed, exact])
    difference = reconcile_plan(plan, records, "update_managed")
    assert difference["unchanged"][0]["handles"] == ["EXACT"]
    assert difference["replace"][0]["handles"] == ["OLD"]


def test_create_only_reports_conflict_instead_of_duplicate_create() -> None:
    difference = reconcile_plan(
        _plan(_operation()),
        [_record("A", "old", project_id="P2")],
        "create_only",
    )
    assert difference["create"] == []
    assert difference["conflict"][0]["reason"] == "managed_entity_differs_create_only"


def test_xdata_chunks_are_utf8_byte_bounded_and_round_trip() -> None:
    metadata = {"text": "汉" * 240, "project_id": "P2"}
    chunks = pack_metadata(metadata)
    assert all(len(chunk.encode("utf-8")) <= 240 for chunk in chunks)
    assert unpack_metadata(chunks) == metadata


def test_execute_operation_writes_group_metadata_to_every_handle() -> None:
    class Backend:
        def __init__(self):
            self.metadata = {}

        def draw_line(self, *args, **kwargs):
            return {"handles": ["A", "B"]}

        def get_entity_state_hash(self, handle):
            return f"state-{handle}"

        def set_entity_metadata(self, handle, metadata):
            self.metadata[handle] = metadata

    backend = Backend()
    execute_operation(
        backend, _operation(), project_id="P2", revision="2")
    assert set(backend.metadata) == {"A", "B"}
    assert backend.metadata["A"]["project_id"] == "P2"
    assert backend.metadata["A"]["entity_index"] == 0
    assert backend.metadata["B"]["entity_index"] == 1
    assert backend.metadata["A"]["entity_count"] == 2
    assert backend.metadata["A"]["operation_group"] == backend.metadata["B"]["operation_group"]


def test_dxf_table_returns_every_created_handle(tmp_path) -> None:
    backend = CadDocument(tmp_path)
    backend.new_drawing("table.dxf")
    before = len(backend.msp)
    result = backend.draw_table(
        0, 0, [20, 30], 5, ["A", "B"], [["1", "2"]], "TITLE", "TABLE")
    assert result["handles_count"] == len(result["handles"])
    assert result["handles_count"] == len(backend.msp) - before
    assert len(set(result["handles"])) == result["handles_count"]
