"""Preview behavior: real pixels, durable fallback, and unchanged source drawings."""

import base64
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import ezdxf
import numpy as np
from mcp.types import CallToolResult, ImageContent
from PIL import Image
from pydantic import ValidationError
import pytest

from cad_mcp import server
from cad_mcp.core.tool_results import decode_tool_result, encode_tool_result
from cad_mcp.drawing.autocad import AcadBackend
from cad_mcp.drawing.document import CadDocument
from cad_mcp.drawing import preview


REGION = dict(x_min=0, y_min=0, x_max=10, y_max=10, width=200, height=200)


@pytest.mark.parametrize("changes", [
    {"x_max": 0}, {"y_max": -1}, {"y_min": 10}, {"x_max": 1e-8},
    {"x_min": float("nan")}, {"y_min": float("inf")}, {"x_max": 1e13},
    {"width": 63}, {"height": 2049}, {"background": "red"}, {"unknown": 1},
])
def test_region_rejects_invalid_bounds_sizes_and_fields(changes):
    with pytest.raises(ValidationError):
        server.PreviewRegionInput(**(REGION | changes))


@pytest.mark.parametrize("background", ["white", "black"])
def test_crop_clips_crossing_entities_and_excludes_outside_pixels(tmp_path, background):
    doc = ezdxf.new()
    # Both endpoints are outside: intersection, rather than containment, matters.
    doc.modelspace().add_line((-10, 5), (20, 5), dxfattribs={"color": 1})
    doc.modelspace().add_line((15, 0), (15, 10), dxfattribs={"color": 5})
    metadata = preview.render_region(doc, log_dir=tmp_path / "custom-log",
                                     mode="dxf", background=background, **REGION)
    path = Path(metadata["image_path"])
    assert path.is_absolute() and path.parent.parent == tmp_path / "images"
    assert len(path.parent.name) == 10  # YYYY-MM-DD
    with Image.open(path) as image:
        assert image.size == (200, 200)
        pixels = np.asarray(image.convert("RGB")).reshape(-1, 3)
        assert any(r > 160 and g < 100 and b < 100 for r, g, b in pixels)
        assert not any(b > 160 and r < 100 and g < 100 for r, g, b in pixels)
        assert image.convert("RGB").getpixel((100, 10)) == (
            (255, 255, 255) if background == "white" else (0, 0, 0))


def test_requested_canvas_keeps_geometry_proportions(tmp_path):
    doc = ezdxf.new()
    doc.modelspace().add_lwpolyline([(1, 1), (9, 1), (9, 9), (1, 9)],
                                  close=True, dxfattribs={"color": 1})
    metadata = preview.render_region(doc, mode="dxf", log_dir=tmp_path / "logs",
                                     **(REGION | {"height": 100}))
    with Image.open(metadata["image_path"]) as image:
        rgb = image.convert("RGB")
        points = [(x, y) for y in range(100) for x in range(200)
                  if (lambda c: c[0] > 160 and c[1] < 100 and c[2] < 100)(rgb.getpixel((x, y)))]
    xs, ys = zip(*points)
    assert abs((max(xs) - min(xs)) - (max(ys) - min(ys))) <= 2
    assert min(xs) > 40 and max(xs) < 160  # letterboxing, not stretching


def test_live_unsaved_content_is_rendered_without_saving_or_editing_source(tmp_path):
    drawing = CadDocument(tmp_path / "drawings")
    drawing.new_drawing("source.dxf")
    original = drawing.current_path.read_bytes()
    drawing.msp.add_circle((5, 5), 2, dxfattribs={"color": 1})
    before = [(e.dxf.handle, dict(e.dxf.all_existing_dxf_attribs())) for e in drawing.msp]
    db_size = len(drawing.doc.entitydb)
    metadata = drawing.preview_region(log_dir=tmp_path / "logs", **REGION)
    assert drawing.current_path.read_bytes() == original
    assert len(ezdxf.readfile(drawing.current_path).modelspace()) == 0
    assert [(e.dxf.handle, dict(e.dxf.all_existing_dxf_attribs())) for e in drawing.msp] == before
    assert len(drawing.doc.entitydb) == db_size
    with Image.open(metadata["image_path"]) as image:
        assert any(r > 160 and g < 100 and b < 100
                   for r, g, b in np.asarray(image.convert("RGB")).reshape(-1, 3))


def test_empty_region_produces_valid_blank_image_with_exact_bounds(tmp_path):
    metadata = preview.render_region(ezdxf.new(), log_dir=tmp_path / "logs", mode="dxf", **REGION)
    assert metadata["bounds"] == {k: REGION[k] for k in ("x_min", "y_min", "x_max", "y_max")}
    with Image.open(metadata["image_path"]) as image:
        assert image.convert("RGB").getextrema() == ((255, 255),) * 3


def test_odd_pixel_dimensions_and_saved_names_are_exact_and_unique(tmp_path):
    arguments = REGION | {"width": 201, "height": 129}
    doc = ezdxf.new()
    first = preview.render_region(doc, log_dir=tmp_path / "logs", mode="dxf", **arguments)
    second = preview.render_region(doc, log_dir=tmp_path / "logs", mode="dxf", **arguments)
    assert first["image_path"] != second["image_path"]
    for metadata in (first, second):
        with Image.open(metadata["image_path"]) as image:
            assert image.size == (201, 129)


def test_save_failure_never_reports_success(tmp_path, monkeypatch):
    drawing = CadDocument(tmp_path / "drawings")
    drawing.new_drawing("source.dxf")
    monkeypatch.setattr(server, "_backend", drawing)
    monkeypatch.setattr(server, "_SETTINGS", replace(server._SETTINGS, log_dir=tmp_path / "logs"))
    # A file where the images directory should be prevents publishing any artifact.
    (tmp_path / "images").write_text("occupied", encoding="utf-8")
    result = json.loads(server.cad_preview_region(server.PreviewRegionInput(**REGION)))
    assert result["ok"] is False and "image_path" not in result
    assert result["error_detail"]["code"] == "IO_ERROR"


def test_native_content_matches_saved_file_and_survives_json_transport(tmp_path):
    metadata = preview.render_region(ezdxf.new(), log_dir=tmp_path / "logs", mode="dxf", **REGION)
    result = preview.preview_result(metadata)
    restored = decode_tool_result(json.loads(json.dumps(encode_tool_result(result))))
    assert isinstance(restored, CallToolResult)
    payload = json.loads(restored.content[0].text)
    assert payload["image_included"] is True and payload["image_path"] == metadata["image_path"]
    assert isinstance(restored.content[1], ImageContent)
    assert base64.b64decode(restored.content[1].data) == Path(payload["image_path"]).read_bytes()


@pytest.mark.parametrize("failure", ["encoding", "size", "path_only"])
def test_inline_failure_or_opt_out_preserves_durable_path(tmp_path, monkeypatch, failure):
    metadata = preview.render_region(ezdxf.new(), log_dir=tmp_path / "logs", mode="dxf", **REGION)
    if failure == "encoding":
        def fail(_):
            raise RuntimeError("simulated image encoding failure")
        monkeypatch.setattr(preview.base64, "b64encode", fail)
    if failure == "size":
        monkeypatch.setattr(preview, "MAX_INLINE_IMAGE_BYTES", 1)
    result = preview.preview_result(metadata, include_image=failure != "path_only")
    assert len(result.content) == 1
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is True and payload["image_included"] is False
    assert ("image_error" in payload) == (failure != "path_only")
    assert Path(payload["image_path"]).is_file()


def test_no_open_drawing_returns_error_without_image_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_backend", CadDocument(tmp_path / "drawings"))
    monkeypatch.setattr(server, "_SETTINGS", replace(server._SETTINGS, log_dir=tmp_path / "logs"))
    result = json.loads(server.cad_preview_region(server.PreviewRegionInput(**REGION)))
    assert result["ok"] is False
    assert result["error_detail"]["code"] == "PRECONDITION_FAILED"
    assert not (tmp_path / "images").exists()


@pytest.mark.parametrize("value", [None, [], {}, {"_cad_mcp_result": "other", "value": {}},
                                  {"_cad_mcp_result": "CallToolResult", "value": {"content": [{}]}}])
def test_transport_rejects_malformed_worker_results(value):
    with pytest.raises(ValueError):
        decode_tool_result(value)


@pytest.mark.parametrize("fault", [None, "export", "render", "missing_file"])
def test_acad_snapshot_uses_live_export_and_cleans_up(tmp_path, monkeypatch, fault):
    events, snapshots = [], []
    original = tmp_path / "drawing.dwg"
    original.write_bytes(b"original DWG must remain untouched")
    digest = hashlib.sha256(original.read_bytes()).hexdigest()

    class Selection:
        def Delete(self):
            events.append("delete selection")

    class Selections:
        def Add(self, name):
            assert name.startswith("CAD_MCP_PREVIEW_")
            events.append("add selection")
            return Selection()

    class Document:
        SelectionSets = Selections()

        def Export(self, path, extension, selection):
            events.append("export DXF")
            assert extension == "DXF"
            snapshots.append(Path(path + ".dxf"))
            if fault == "export":
                raise RuntimeError("simulated export failure")
            if fault != "missing_file":
                doc = ezdxf.new()
                doc.modelspace().add_circle((5, 5), 2)
                doc.saveas(snapshots[-1])

    backend = AcadBackend(tmp_path / "drawings")
    backend.doc = Document()
    backend.current_path = original
    monkeypatch.setattr(backend, "_check_doc", lambda: None)
    if fault == "render":
        def fail(*args, **kwargs):
            raise RuntimeError("simulated render failure")
        monkeypatch.setattr(preview, "render_region", fail)
    if fault:
        with pytest.raises(RuntimeError):
            backend.preview_region(log_dir=tmp_path / "logs", **REGION)
        assert not (tmp_path / "images").exists()
    else:
        metadata = backend.preview_region(log_dir=tmp_path / "logs", **REGION)
        assert metadata["mode"] == "acad" and metadata["source"] == "autocad_dxf_export"
        assert Path(metadata["image_path"]).is_file()
    assert events == ["add selection", "export DXF", "delete selection"]
    assert snapshots and all(not path.parent.exists() for path in snapshots)
    assert hashlib.sha256(original.read_bytes()).hexdigest() == digest
    assert backend.current_path == original
