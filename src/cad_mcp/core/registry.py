"""MCP 管理实体的 XData 编码、摘要和增量同步索引。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from cad_mcp.core.contracts import CadOperation, DrawingPlan


XDATA_APP_NAME = "CAD_MCP_ENV"
XDATA_SCHEMA_VERSION = 2
XDATA_CHUNK_SIZE_BYTES = 240


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def operation_fingerprint(operation: CadOperation) -> str:
    payload = {
        "kind": operation.kind,
        "semantic_id": operation.semantic_id,
        "view_id": operation.view_id,
        "role": operation.role,
        "params": operation.params,
        "metadata": operation.metadata,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def metadata_for_operation(
    operation: CadOperation,
    revision: str,
    project_id: str = "",
    *,
    entity_index: int = 0,
    entity_count: int = 1,
) -> Dict[str, Any]:
    fingerprint = operation_fingerprint(operation)
    return {
        **operation.metadata,
        "schema": XDATA_SCHEMA_VERSION,
        "project_id": project_id,
        "semantic_id": operation.semantic_id,
        "view_id": operation.view_id,
        "role": operation.role,
        "operation_kind": operation.kind,
        "operation_group": fingerprint,
        "entity_index": entity_index,
        "entity_count": entity_count,
        "revision": revision,
        "geometry_hash": fingerprint,
    }


def pack_metadata(metadata: Mapping[str, Any]) -> List[str]:
    payload = canonical_json(dict(metadata))
    chunks: List[str] = []
    current: List[str] = []
    current_size = 0
    for character in payload:
        encoded_size = len(character.encode("utf-8"))
        if current and current_size + encoded_size > XDATA_CHUNK_SIZE_BYTES:
            chunks.append("".join(current))
            current = []
            current_size = 0
        current.append(character)
        current_size += encoded_size
    if current:
        chunks.append("".join(current))
    return chunks or ["{}"]


def unpack_metadata(chunks: Sequence[str]) -> Dict[str, Any]:
    payload = "".join(str(chunk) for chunk in chunks)
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("CAD_MCP_ENV XData 必须解码为对象")
    return value


@dataclass
class ManagedEntityRecord:
    handle: str
    semantic_id: str
    view_id: Optional[str]
    role: str
    geometry_hash: str
    project_id: Optional[str] = None
    operation_group: Optional[str] = None
    entity_index: Optional[int] = None
    entity_count: Optional[int] = None
    materialized_hash: Optional[str] = None
    current_state_hash: Optional[str] = None
    manually_modified: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


def index_records(records: Iterable[ManagedEntityRecord]) -> Dict[str, List[ManagedEntityRecord]]:
    result: Dict[str, List[ManagedEntityRecord]] = {}
    for record in records:
        key = f"{record.semantic_id}|{record.view_id or ''}|{record.role}"
        result.setdefault(key, []).append(record)
    return result


def operation_key(operation: CadOperation) -> str:
    return f"{operation.semantic_id}|{operation.view_id or ''}|{operation.role}"


def _difference_entry(
    operation: Optional[CadOperation],
    records: Sequence[ManagedEntityRecord],
    **extra: Any,
) -> Dict[str, Any]:
    handles = [record.handle for record in records]
    return {
        "operation": operation,
        "handle": handles[0] if handles else None,
        "handles": handles,
        **extra,
    }


def _operation_group_complete(records: Sequence[ManagedEntityRecord]) -> bool:
    """Validate v2 group cardinality when cardinality metadata is available."""

    counts = {record.entity_count for record in records}
    if counts == {None}:
        return True
    if None in counts or len(counts) != 1:
        return False
    expected = next(iter(counts))
    if expected is None or expected < 1 or len(records) != expected:
        return False
    indices = [record.entity_index for record in records]
    return all(isinstance(index, int) for index in indices) and set(indices) == set(range(expected))


def reconcile_plan(
    plan: DrawingPlan,
    existing: Sequence[ManagedEntityRecord],
    policy: str = "update_managed",
) -> Dict[str, Any]:
    """只计算增量差异，不执行删除或修改。"""

    if policy not in {"create_only", "update_managed", "replace_scope"}:
        raise ValueError("policy 必须是 create_only/update_managed/replace_scope")
    if policy == "replace_scope" and not plan.view_ids:
        raise ValueError("replace_scope 必须显式提供至少一个 view_id，禁止空范围删除")

    current_project = [record for record in existing
                       if record.project_id == plan.project_id]
    legacy_records = [record for record in existing if not record.project_id]
    indexed = index_records(current_project)
    legacy_indexed = index_records(legacy_records)
    creates: List[CadOperation] = []
    unchanged: List[Dict[str, Any]] = []
    replaces: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    consumed_handles = set()
    conflict_handles = set()
    seen_operations = set()
    pending: List[tuple[CadOperation, str]] = []

    for operation in plan.operations:
        fingerprint = operation_fingerprint(operation)
        dedupe_key = (operation_key(operation), fingerprint)
        if dedupe_key in seen_operations:
            raise ValueError(
                f"计划包含重复操作: {operation.semantic_id}/{operation.role}")
        seen_operations.add(dedupe_key)
        pending.append((operation, fingerprint))

    # First reserve every exact group. This makes reconciliation independent of
    # plan order when several operations share one semantic key.
    unmatched: List[tuple[CadOperation, str]] = []
    for operation, fingerprint in pending:
        exact = [
            record for record in indexed.get(operation_key(operation), [])
            if record.handle not in consumed_handles
            and record.geometry_hash == fingerprint
        ]
        if not exact:
            unmatched.append((operation, fingerprint))
            continue
        groups: Dict[str, List[ManagedEntityRecord]] = {}
        for record in exact:
            group = record.operation_group or record.geometry_hash
            groups.setdefault(group, []).append(record)
        if len(groups) != 1:
            consumed_handles.update(record.handle for record in exact)
            conflicts.append(_difference_entry(
                operation, exact, reason="ambiguous_managed_operation_group"))
            conflict_handles.update(record.handle for record in exact)
            continue
        records = next(iter(groups.values()))
        consumed_handles.update(record.handle for record in records)
        modified = [record for record in records if record.manually_modified]
        complete = _operation_group_complete(records)
        if complete and not modified:
            unchanged.append(_difference_entry(operation, records))
            continue
        if policy == "create_only":
            reason = ("incomplete_managed_operation_group" if not complete
                      else "managed_entity_manually_modified")
            conflicts.append(_difference_entry(operation, records, reason=reason))
            conflict_handles.update(record.handle for record in records)
            continue
        if modified and policy != "replace_scope":
            conflicts.append(_difference_entry(
                operation, modified,
                reason="managed_entity_manually_modified"))
            conflict_handles.update(record.handle for record in modified)
            continue
        replaces.append(_difference_entry(
            operation, records,
            previous_hashes=[fingerprint],
            new_hash=fingerprint,
            incomplete_group=not complete,
            manual_change_overridden=bool(modified),
        ))

    # Only changed or new operations reach the second pass, after all exact
    # groups have been reserved above.
    for operation, fingerprint in unmatched:
        legacy = legacy_indexed.get(operation_key(operation), [])
        if legacy:
            conflicts.append(_difference_entry(
                operation, legacy, reason="legacy_missing_project_id"))
            conflict_handles.update(record.handle for record in legacy)
            continue
        candidates = [
            record for record in indexed.get(operation_key(operation), [])
            if record.handle not in consumed_handles
        ]
        if not candidates:
            creates.append(operation)
            continue
        if policy == "create_only":
            conflicts.append(_difference_entry(
                operation, candidates,
                reason="managed_entity_differs_create_only"))
            conflict_handles.update(record.handle for record in candidates)
            continue
        groups: Dict[str, List[ManagedEntityRecord]] = {}
        for record in candidates:
            group = record.operation_group or record.geometry_hash
            groups.setdefault(group, []).append(record)
        if len(groups) != 1:
            conflicts.append(_difference_entry(
                operation, candidates,
                reason="ambiguous_managed_operation_group"))
            conflict_handles.update(record.handle for record in candidates)
            continue
        records = next(iter(groups.values()))
        consumed_handles.update(record.handle for record in records)
        modified = [record for record in records if record.manually_modified]
        if modified and policy != "replace_scope":
            conflicts.append(_difference_entry(
                operation, modified,
                reason="managed_entity_manually_modified"))
            conflict_handles.update(record.handle for record in modified)
            continue
        replaces.append(_difference_entry(
            operation, records,
            previous_hashes=sorted({record.geometry_hash for record in records}),
            new_hash=fingerprint,
            incomplete_group=not _operation_group_complete(records),
            manual_change_overridden=bool(modified),
        ))
    deletes: List[ManagedEntityRecord] = []
    if policy == "replace_scope":
        scope_views = set(plan.view_ids)
        scoped_legacy = [record for record in legacy_records
                         if record.view_id in scope_views
                         and record.handle not in conflict_handles]
        for record in scoped_legacy:
            conflicts.append(_difference_entry(
                None, [record], reason="legacy_missing_project_id"))
        deletes = [record for record in current_project
                   if record.handle not in consumed_handles
                   and record.view_id in scope_views]
    return {
        "policy": policy,
        "create": creates,
        "replace": replaces,
        "conflict": conflicts,
        "delete": deletes,
        "unchanged": unchanged,
        "statistics": {
            "create": len(creates),
            "replace": len(replaces),
            "conflict": len(conflicts),
            "delete": len(deletes),
            "unchanged": len(unchanged),
        },
    }
