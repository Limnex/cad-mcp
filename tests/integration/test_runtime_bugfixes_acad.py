"""Opt-in checks against an existing AutoCAD, using only an owned unsaved drawing."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time

import pytest

from cad_mcp.drawing import autocad
from cad_mcp.drawing.autocad import AcadBackend
from cad_mcp.server import _AcadLockProxy


pytestmark = pytest.mark.acad

if os.environ.get("CAD_MCP_RUN_ACAD_INTEGRATION") != "1":
    pytest.skip("requires an explicitly authorized, already running AutoCAD", allow_module_level=True)


def _document_snapshot(application):
    documents = application.Documents
    return sorted(
        (str(documents.Item(index).Name), str(documents.Item(index).FullName),
         bool(documents.Item(index).Saved))
        for index in range(int(documents.Count))
    )


def test_existing_autocad_runtime_bugfixes_without_saving(tmp_path):
    repo_root = Path(__file__).resolve().parents[2]
    assert Path(autocad.__file__).resolve().is_relative_to(repo_root / "src")
    raw = AcadBackend(tmp_path)
    cad = _AcadLockProxy(raw)
    connected = cad.health(reconnect=True)
    assert connected["connected"] is True
    application = raw.acad
    before = _document_snapshot(application)
    original_active = application.ActiveDocument if before else None
    timings = {}
    try:
        created = cad.new_drawing("cad-mcp-runtime-bugfix-validation.dwg")
        assert created["mode"] == "acad"
        assert raw._own_document is True
        cad.set_active_layer("CAD-MCP-VERIFY")
        for x1, x2 in [(0.0, 10.0), (10.0 + 1e-10, 20.0), (20.02, 30.0)]:
            cad.draw_line(x1, 0.0, x2, 0.0, "CAD-MCP-VERIFY")

        started = time.monotonic()
        info = cad.get_info()
        timings["get_info_seconds"] = round(time.monotonic() - started, 3)
        assert "CAD-MCP-VERIFY" in info

        started = time.monotonic()
        layers = cad.list_layers_detailed()
        timings["list_layers_seconds"] = round(time.monotonic() - started, 3)
        layer = next(item for item in layers["layers"] if item["name"] == "CAD-MCP-VERIFY")
        assert layer["entity_count"] == 3

        page1 = cad.query_entities(layer="CAD-MCP-VERIFY", limit=2, include_bounds=True)
        assert len(page1["entities"]) == 2
        assert page1["next_offset"] is not None
        page2 = cad.query_entities(layer="CAD-MCP-VERIFY", offset=page1["next_offset"], limit=2)
        assert len(page2["entities"]) == 1
        handles = [entity["handle"] for entity in page1["entities"] + page2["entities"]]
        assert len(set(handles)) == 3
        assert all(entity["bounds"] is not None for entity in page1["entities"])

        started = time.monotonic()
        report = cad.check_drawing(tolerance=1.0)
        timings["check_drawing_seconds"] = round(time.monotonic() - started, 3)
        assert "0.02mm" in report
        assert "相距 0.0mm" not in report
        assert "未完成" not in report
        assert not (tmp_path / "cad-mcp-runtime-bugfix-validation.dwg").exists()
    finally:
        if raw._own_document:
            cad.close_document(save=False)
        if original_active is not None:
            raw._acquire_autocad_lock()
            try:
                original_active.Activate()
            finally:
                raw._release_autocad_lock()

    assert _document_snapshot(application) == before
    print(json.dumps({
        "source": str(Path(autocad.__file__).resolve()),
        "timings": timings,
        "existing_documents_unchanged": True,
        "temporary_drawing_closed_without_save": True,
    }, ensure_ascii=False))


def test_isolated_worker_roundtrip_in_existing_autocad(monkeypatch, tmp_path):
    from cad_mcp import server
    from cad_mcp.core.isolation import ToolProcessSupervisor, _policy

    monkeypatch.setenv("CAD_MODE", "acad")
    monkeypatch.setenv("CAD_DRAWINGS_DIR", str(tmp_path / "drawings"))
    monkeypatch.setenv("CAD_LOG_DIR", str(tmp_path / "logs"))
    application = autocad.win32com.client.GetActiveObject("AutoCAD.Application")
    before = _document_snapshot(application)
    original_active = application.ActiveDocument if before else None
    supervisor = ToolProcessSupervisor(mode="acad")
    opened = False

    def call(name, params):
        arguments = {"params": params}
        target, write = _policy(name, arguments, server._FULL_TOOL_REGISTRY)
        return supervisor.call(name, arguments, target_name=target, write=write)

    try:
        connected = json.loads(call("cad_health", {"reconnect": True}))
        assert connected["connected"] is True
        created = json.loads(call("cad_new_drawing", {"filename": "cad-mcp-isolated-validation.dwg"}))
        assert created["ok"] is True
        opened = True
        layer = json.loads(call("cad_set_active_layer", {"name": "CAD-MCP-ISOLATED"}))
        assert layer["ok"] is True
        for x1, x2 in [(0.0, 10.0), (10.0 + 1e-10, 20.0), (20.02, 30.0)]:
            result = json.loads(call("cad_draw_line", {
                "x1": x1, "y1": 0, "x2": x2, "y2": 0, "layer": "CAD-MCP-ISOLATED"}))
            assert result["ok"] is True
        queried = json.loads(call("cad_query_entities", {"layer": "CAD-MCP-ISOLATED", "limit": 10}))
        assert queried["ok"] is True
        assert len(queried["entities"]) == 3
        layers = json.loads(call("cad_list_layers_detailed", {}))
        assert next(layer for layer in layers["layers"] if layer["name"] == "CAD-MCP-ISOLATED")["entity_count"] == 3
        checked = call("cad_check_drawing", {"tolerance": 1.0})
        assert "0.02mm" in checked
        assert "相距 0.0mm" not in checked
        health = json.loads(call("cad_health", {}))
        assert health["worker"]["generation"] == 1
        assert health["worker"]["write_result_unknown"] is False
        assert not (tmp_path / "drawings" / "cad-mcp-isolated-validation.dwg").exists()
    finally:
        try:
            if opened and not supervisor._write_unknown:
                closed = json.loads(call("cad_close_drawing", {"save": False}))
                assert closed["ok"] is True
        finally:
            supervisor.close()
        if original_active is not None:
            original_active.Activate()

    assert _document_snapshot(application) == before
    print(json.dumps({"isolated_worker_real_cad": "passed", "worker_generation": 1,
                      "existing_documents_unchanged": True,
                      "temporary_drawing_closed_without_save": True}, ensure_ascii=False))
