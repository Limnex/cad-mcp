"""Opt-in strict semantic tests in a separate drawing in already running CAD.

Unexpected failures leave the test document open and stop subsequent CAD writes.
A separate owned transition document protects untouched startup drawings during reopen.
Only a completely successful run closes its own test documents.
"""

import asyncio
from dataclasses import replace
import faulthandler
import json
import os
from pathlib import Path
import time
from uuid import uuid4

import pytest

from cad_mcp import server
from cad_mcp.core.semantics import drawing_entities, _transaction_state
from cad_mcp.drawing.autocad import AcadBackend
from cad_mcp.drawing.chamfer import fresh_com
from cad_mcp.server import _AcadLockProxy


pytestmark = pytest.mark.acad
if os.environ.get("CAD_MCP_RUN_ACAD_INTEGRATION") != "1":
    pytest.skip("requires explicit authorization and an already running AutoCAD", allow_module_level=True)


def _documents(application):
    documents = fresh_com(application.Documents)
    rows = []
    for index in range(int(documents.Count)):
        document = fresh_com(documents.Item(index))
        rows.append({
            "name": str(document.Name),
            "path": str(document.FullName),
            "saved": bool(document.Saved),
            "model_entities": int(fresh_com(document.ModelSpace).Count),
            "dbmod": int(document.GetVariable("DBMOD")),
            "dwgtitled": int(document.GetVariable("DWGTITLED")),
        })
    return sorted(rows, key=lambda row: (row["path"], row["name"]))


def _document_changes(original, current):
    observed = {(row["path"].casefold(), row["name"].casefold()): row for row in current}
    changes = []
    for before in original:
        after = observed.get((before["path"].casefold(), before["name"].casefold()))
        if after != before:
            changes.append({"before": before, "after": after,
                            "reason": "missing" if after is None else "changed"})
    return changes


def _close_identity(raw, expected_path):
    """Observe the actual bound document; never rely on current_path alone."""
    document = fresh_com(raw.doc) if raw.doc is not None else None
    actual = str(document.FullName) if document is not None else ""
    name = str(document.Name) if document is not None else None
    current = str(raw.current_path) if raw.current_path is not None else None
    expected = str(Path(expected_path).resolve()) if expected_path else None
    matches = bool(expected and actual and current and name
                   and str(Path(actual).resolve()).casefold() == expected.casefold()
                   and str(Path(current).resolve()).casefold() == expected.casefold()
                   and name.casefold() == Path(expected).name.casefold())
    return {"expected_path": expected, "actual_path": actual,
            "current_path": current, "bound_name": name, "matches": matches}


def test_strict_semantics_against_existing_autocad(tmp_path, monkeypatch):
    raw = AcadBackend(tmp_path)
    cad = _AcadLockProxy(raw)
    transition_raw = AcadBackend(tmp_path / "transition")
    transition = _AcadLockProxy(transition_raw)
    monkeypatch.setattr(server, "_backend", cad)
    monkeypatch.setattr(server, "_SETTINGS", replace(server._SETTINGS, semantic_policy="strict", mode="acad"))
    project = "semantic-live-" + uuid4().hex
    application = None
    original_documents = None
    evidence = {"stage": "initialization", "checks": [], "calls": [], "passed": False,
                "document_snapshots": [], "close_identity_checks": [],
                "report_path": str(tmp_path / "semantic-live-report.json"),
                "hard_timeout_seconds": 30}
    stack_file = (tmp_path / "semantic-live-stack.txt").open("w", encoding="utf-8")

    def checkpoint():
        target = tmp_path / "semantic-live-report.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)

    def arm_timeout():
        # The watchdog terminates only this opt-in test process. It can interrupt
        # a COM call that never returns, without issuing more CAD commands.
        faulthandler.cancel_dump_traceback_later()
        faulthandler.dump_traceback_later(30, file=stack_file, exit=True)

    def capture_documents(label):
        if application is None:
            return
        arm_timeout()
        documents = _documents(application)
        snapshot = {"label": label, "stage": evidence["stage"],
                    "time": time.time(), "documents": documents,
                    "active_document": str(fresh_com(application.ActiveDocument).Name) if documents else None}
        if raw.doc is not None:
            bound = fresh_com(raw.doc)
            snapshot["bound_document"] = {"name": str(bound.Name),
                                          "path": str(bound.FullName),
                                          "current_path": str(raw.current_path) if raw.current_path else None}
        if transition_raw.doc is not None:
            owned_transition = fresh_com(transition_raw.doc)
            snapshot["transition_document"] = {"name": str(owned_transition.Name),
                                               "path": str(owned_transition.FullName),
                                               "owned": transition_raw._own_document}
        evidence["document_snapshots"].append(snapshot)
        changes = _document_changes(original_documents, documents) if original_documents is not None else []
        if changes:
            evidence.setdefault("first_original_document_change", {"label": label, "changes": changes})
        checkpoint()
        assert not changes, {"snapshot": label, "original_document_changes": changes}

    def stage(value):
        evidence["stage"] = value
        checkpoint()
        arm_timeout()
        print("LIVE_STAGE " + value, flush=True)
        capture_documents("stage:" + value)

    def call(name, params, *, expected_code=None):
        capture_documents("before:" + name)
        if name == "cad_close_drawing":
            identity = _close_identity(raw, evidence.get("drawing_path"))
            evidence["close_identity_checks"].append({"stage": evidence["stage"], **identity})
            checkpoint()
            assert identity["matches"], {"close_blocked_before_write": identity}
        evidence["active_call"] = {"tool": name, "started_at": time.time()}
        checkpoint()
        print("LIVE_CALL_BEGIN " + name, flush=True)
        arm_timeout()
        tool = server._FULL_TOOL_REGISTRY[name]
        result = asyncio.run(tool.run({"params": params}))
        try:
            payload = json.loads(result)
        except (TypeError, ValueError):
            payload = {"text": result}
        evidence["calls"].append({"tool": name, "result": payload})
        evidence["active_call"] = None
        checkpoint()
        arm_timeout()
        print("LIVE_CALL_END " + name, flush=True)
        capture_documents("after:" + name)
        if expected_code:
            assert payload.get("ok") is False, payload
            assert payload["error_detail"]["code"] == expected_code, payload
        else:
            assert payload.get("ok", True), payload
        return payload

    def check(label):
        evidence["checks"].append(label)
        print("LIVE_PASS " + label, flush=True)

    def binding(object_id):
        return {"project_id": project, "object_id": object_id, "role": "池体外轮廓", "view_id": "plan"}

    def register(object_id, name):
        return call("cad_register_semantic_object", {"object": {
            "project_id": project, "object_id": object_id, "name": name,
            "object_type": "secondary_clarifier", "source": "实机测试夹具，不作为工程设计",
            "status": "declared"}})

    try:
        stage("connect_existing_autocad_once")
        assert call("cad_health", {"reconnect": False})["connected"] is False
        connected = call("cad_health", {"reconnect": True})
        assert connected["connected"] is True, connected
        assert connected["semantic_policy"] == "strict", connected
        application = raw.acad
        original_documents = _documents(application)
        original_active = fresh_com(application.ActiveDocument) if original_documents else None
        evidence["original_documents"] = original_documents
        capture_documents("baseline")
        template, source = raw._resolve_new_drawing_template(application)
        evidence["template"] = {"path": str(template), "source": source}
        check("existing_autocad_connected_without_starting_an_instance")

        stage("create_separate_test_document")
        created = call("cad_new_drawing", {"filename": "semantic-live-" + project[-8:] + ".dwg"})
        evidence["drawing_path"] = created["path"]
        assert raw._own_document
        assert str(raw.doc.Name) not in {row["name"] for row in original_documents}
        baseline = set(drawing_entities(raw))
        check("separate_test_document_created")

        stage("reject_missing_and_unregistered_identity")
        circle = {"cx": 0, "cy": 0, "radius": 6000}
        call("cad_draw_circle", circle, expected_code="SEMANTIC_REQUIRED")
        assert set(drawing_entities(raw)) == baseline
        call("cad_draw_circle", {**circle, "semantic": binding("tank-01")}, expected_code="SEMANTIC_NOT_FOUND")
        assert set(drawing_entities(raw)) == baseline
        check("missing_identity_rejected_without_creating_geometry")

        stage("register_object_and_draw_circle")
        register("tank-01", "语义测试_二沉池01")
        drawn = call("cad_draw_circle", {**circle, "semantic": binding("tank-01")})
        handle = drawn["handle"]
        meaning = call("cad_get_entity_semantics", {"handle": handle})["entities"][0]
        assert meaning["status"] == "registered", meaning
        assert meaning["object"]["name"] == "语义测试_二沉池01", meaning
        assert meaning["object"]["object_type"] == "secondary_clarifier", meaning
        first_uid = meaning["metadata"]["entity_uid"]
        check("object_registration_and_entity_xdata_verified_in_real_dwg")

        stage("copy_with_explicit_target_identity")
        before_copy = set(drawing_entities(raw))
        call("cad_copy_entity", {"handle": handle, "dx": 20000, "dy": 0}, expected_code="SEMANTIC_REQUIRED")
        assert set(drawing_entities(raw)) == before_copy
        register("tank-02", "语义测试_二沉池02")
        call("cad_copy_entity", {"handle": handle, "dx": 20000, "dy": 0, "semantic": binding("tank-02")})
        added = set(drawing_entities(raw)) - before_copy
        assert len(added) == 1
        second_handle = added.pop()
        second = call("cad_get_entity_semantics", {"handle": second_handle})["entities"][0]
        assert second["metadata"]["entity_uid"] != first_uid
        assert second["object"]["object_id"] == "tank-02"
        check("copied_circle_has_explicit_object_and_fresh_identity")

        stage("simulate_manual_radius_change_in_test_document")
        raw._acquire_autocad_lock()
        started = False
        try:
            raw._wait_operation_ready()
            started = raw._begin_undo()
            assert started
            raw._find(handle).Radius = 7500.0
            raw._commit_undo(started)
        except Exception:
            raw._rollback_undo(started)
            raise
        finally:
            raw._release_autocad_lock()
        modified = call("cad_get_entity_properties", {"handle": handle})
        assert modified["properties"]["radius"] == 7500.0
        assert modified["semantic"]["status"] == "modified"
        assert modified["semantic"]["object"]["object_type"] == "secondary_clarifier"
        check("manual_geometry_change_detected_without_reclassifying_the_pool")

        stage("inject_metadata_failure_and_verify_real_undo")
        before_failure = _transaction_state(raw)
        original_writer = raw.set_entity_metadata

        def fail_after_metadata_write(new_handle, metadata):
            original_writer(new_handle, metadata)
            raise RuntimeError("intentional_live_semantic_failure_after_xdata_write")

        with monkeypatch.context() as scoped:
            scoped.setattr(raw, "set_entity_metadata", fail_after_metadata_write)
            failed = call("cad_draw_circle", {"cx": 40000, "cy": 0, "radius": 3000,
                "semantic": binding("tank-01")}, expected_code="INTERNAL_ERROR")
        assert failed["error_detail"]["details"]["rollback_status"] == "succeeded", failed
        assert _transaction_state(raw) == before_failure
        assert not getattr(raw, "_semantic_write_blocked", False)
        check("real_undo_removed_failed_geometry_and_xdata_and_preserved_previous_objects")

        stage("check_test_drawing_before_save")
        inspection = call("cad_check_drawing", {})
        assert "未完成" not in inspection.get("text", "")
        call("cad_check_semantics", {})
        stage("save_test_dwg")
        call("cad_save_drawing", {})
        assert raw.current_path.is_file()
        assert str(raw.doc.FullName).casefold() == str(raw.current_path).casefold()
        stage("create_owned_transition_document")
        capture_documents("before:create_transition_document")
        transition.new_drawing("semantic-transition-" + project[-8:] + ".dwg", template_path=str(template))
        transition_document = fresh_com(transition_raw.doc)
        transition_name = str(transition_document.Name)
        evidence["transition_document"] = {"name": transition_name,
                                            "current_path": str(transition_raw.current_path)}
        checkpoint()
        assert transition_raw._own_document
        assert transition_name not in {row["name"] for row in original_documents}
        assert transition_name != str(raw.doc.Name)
        # A real fixture entity keeps this owned document non-pristine, so an
        # OPEN cannot discard it as an untouched default startup document.
        transition.draw_line(0, 0, 1, 0, layer="0")
        assert int(fresh_com(transition_document.ModelSpace).Count) == 1
        capture_documents("after:create_transition_document")
        check("owned_transition_document_created_without_changing_originals")

        stage("close_saved_test_dwg")
        call("cad_close_drawing", {"save": False})
        transition_raw._acquire_autocad_lock()
        try:
            transition_document.Activate()
        finally:
            transition_raw._release_autocad_lock()
        capture_documents("after:activate_transition_document")
        assert str(fresh_com(application.ActiveDocument).Name) == transition_name
        stage("reopen_saved_test_dwg")
        call("cad_open_drawing", {"filename": evidence["drawing_path"]})
        stage("verify_semantics_in_reopened_test_dwg")
        restored = call("cad_get_entity_semantics", {"handle": handle})["entities"][0]
        assert restored["metadata"]["entity_uid"] == first_uid
        assert restored["object"]["name"] == "语义测试_二沉池01"
        assert restored["status"] == "modified"
        assert call("cad_get_entity_properties", {"handle": handle})["properties"]["radius"] == 7500.0
        assert call("cad_get_entity_semantics", {"handle": second_handle})["entities"][0]["status"] == "registered"
        check("identity_and_modified_state_survive_real_dwg_save_and_reopen")

        stage("close_only_test_document_and_restore_original_active_document")
        assert str(raw.current_path).casefold() == evidence["drawing_path"].casefold()
        call("cad_close_drawing", {"save": False})
        if original_active is not None:
            raw._acquire_autocad_lock()
            try:
                original_active.Activate()
            finally:
                raw._release_autocad_lock()
        capture_documents("after:restore_original_active_document")
        stage("close_only_owned_transition_document")
        transition_document = fresh_com(transition_raw.doc)
        transition_identity = {"owned": transition_raw._own_document,
                               "expected_name": transition_name,
                               "actual_name": str(transition_document.Name),
                               "actual_path": str(transition_document.FullName),
                               "model_entities": int(fresh_com(transition_document.ModelSpace).Count)}
        evidence["transition_close_identity"] = transition_identity
        checkpoint()
        assert transition_identity["owned"]
        assert transition_identity["actual_name"] == transition_identity["expected_name"]
        assert transition_identity["actual_path"] == ""
        assert transition_identity["model_entities"] == 1
        capture_documents("before:close_transition_document")
        transition.close_document(save=False)
        assert transition_raw.doc is None
        assert not transition_raw.current_path.is_file()
        capture_documents("after:close_transition_document")
        check("owned_transition_document_closed_without_save")
        after_documents = _documents(application)
        evidence["final_documents"] = after_documents
        assert after_documents == original_documents
        check("original_documents_counts_paths_and_saved_flags_unchanged")
        evidence["passed"] = True
        evidence["stage"] = "completed"
    except BaseException as exc:
        evidence["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        evidence["stopped_without_further_cad_writes"] = True
        evidence["test_document_left_bound"] = raw.doc is not None
        evidence["transition_document_left_bound"] = transition_raw.doc is not None
        raise
    finally:
        faulthandler.cancel_dump_traceback_later()
        checkpoint()
        stack_file.close()
        print("LIVE_REPORT " + evidence["report_path"], flush=True)
