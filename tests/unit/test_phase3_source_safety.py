from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cad_mcp.drawing import autocad as cad_acad
from cad_mcp.dwt import autocad as cad_dwt_acad
from cad_mcp.drawing.autocad import AcadBackend
from cad_mcp.core.contracts import RuleDefinition
from cad_mcp.dwt.autocad import AcadDwtMixin
from cad_mcp.dwt.contracts import MAX_JSON_BYTES, read_utf8_json_object
from cad_mcp.server import CloneTemplateDrawingInput, ImportBlockDefinitionsInput
from cad_mcp.engineering.templates import load_template_manifest


def test_lock_detection_uses_real_sibling_names_and_never_deletes(tmp_path) -> None:
    drawing = tmp_path / "plant.dwg"
    backend = AcadBackend(tmp_path)
    expected = (tmp_path / "plant.dwl", tmp_path / "plant.dwl2")
    assert backend._lock_paths(drawing) == expected
    expected[1].write_text("owned elsewhere", encoding="utf-8")
    with pytest.raises(PermissionError, match="不会自动删除"):
        backend._assert_no_external_lock(drawing)
    assert expected[1].read_text(encoding="utf-8") == "owned elsewhere"


def test_block_import_hash_mismatch_stops_before_opening_source(tmp_path) -> None:
    source = tmp_path / "library.dwg"
    source.write_bytes(b"source")

    class Backend(AcadDwtMixin):
        doc = object()

        def _check_doc(self):
            return None

        def _open_source_document_readonly(self, _source):
            raise AssertionError("哈希不匹配时不得打开源图纸")

    with pytest.raises(cad_dwt_acad.ConflictError, match="SHA-256"):
        Backend().import_block_definitions(
            source_path=str(source),
            block_names=["TAG"],
            expected_sha256="0" * 64,
        )


def test_clone_contract_requires_hash_and_rejects_invalid_hash() -> None:
    with pytest.raises(ValidationError):
        CloneTemplateDrawingInput(source_path="C:/source.dwg", output_filename="copy.dwg")
    with pytest.raises(ValidationError):
        CloneTemplateDrawingInput(
            source_path="C:/source.dwg",
            output_filename="copy.dwg",
            expected_sha256="not-a-hash",
        )
    preview = ImportBlockDefinitionsInput(
        source_path="C:/source.dwg", block_names=["TAG"], dry_run=True
    )
    assert preview.expected_sha256 is None


def test_clone_rolls_back_existing_target_if_source_changes(monkeypatch, tmp_path) -> None:
    source = tmp_path / "source.dwg"
    source.write_bytes(b"source")
    output = tmp_path / "copy.dwg"
    output.write_bytes(b"existing target")
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    real_sha256 = cad_acad.sha256_file
    source_reads = 0

    def drifting_sha256(path):
        nonlocal source_reads
        if Path(path).resolve() == source.resolve():
            source_reads += 1
            return expected if source_reads == 1 else "f" * 64
        return real_sha256(path)

    monkeypatch.setattr(cad_acad, "sha256_file", drifting_sha256)
    backend = AcadBackend(tmp_path)
    with pytest.raises(RuntimeError, match="发生变化"):
        backend.clone_template_drawing(
            str(source), "copy.dwg", expected, overwrite=True
        )
    assert output.read_bytes() == b"existing target"
    assert not list(tmp_path.glob("*.backup.dwg"))


def test_rule_contract_bounds_field_paths_and_nested_parameters() -> None:
    base = {
        "rule_id": "rule",
        "target_type": "project",
        "operator": "required",
    }
    with pytest.raises(ValidationError, match="120"):
        RuleDefinition.model_validate({**base, "fields": ["x" * 121]})
    nested = "leaf"
    for _ in range(20):
        nested = {"child": nested}
    with pytest.raises(ValidationError, match="嵌套层级"):
        RuleDefinition.model_validate({**base, "parameters": {"root": nested}})


def _manifest_payload(source_name: str, source_hash: str) -> dict:
    return {
        "schema_version": "1.0",
        "template_id": "test-template",
        "source_dwg": source_name,
        "source_sha256": source_hash,
        "sheets": [{
            "id": "A3",
            "paper_size": "A3",
            "paper_width": 420,
            "paper_height": 297,
            "block_name": "FRAME_A3",
            "source_bounds": [0, 0, 420, 297],
            "usable_regions": [{
                "id": "main", "x": 10, "y": 10, "width": 300, "height": 200
            }],
            "fixed_insert_scale": 1,
        }],
    }


def test_template_manifest_resolves_relative_source_and_verifies_hash(tmp_path) -> None:
    source = tmp_path / "frame.dwg"
    source.write_bytes(b"dwg-template")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(_manifest_payload(source.name, digest)), encoding="utf-8"
    )
    manifest = load_template_manifest(manifest_path)
    assert Path(manifest.source_dwg) == source.resolve()
    assert manifest.source_sha256 == digest


def test_template_manifest_rejects_missing_hash_non_finite_and_oversize(tmp_path) -> None:
    source = tmp_path / "frame.dwg"
    source.write_bytes(b"dwg-template")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    payload = _manifest_payload(source.name, digest)

    missing_hash = tmp_path / "missing-hash.json"
    missing = dict(payload)
    missing.pop("source_sha256")
    missing_hash.write_text(json.dumps(missing), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_template_manifest(missing_hash)

    non_finite = tmp_path / "nan.json"
    bad = _manifest_payload(source.name, digest)
    bad["sheets"][0]["paper_width"] = float("nan")
    non_finite.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="NaN"):
        load_template_manifest(non_finite)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"{" + b" " * MAX_JSON_BYTES + b"}")
    with pytest.raises(ValueError, match="超过上限"):
        read_utf8_json_object(oversized)


class _FakeLayout:
    def __init__(self, name):
        self.Name = name
        self.ConfigName = "Original.pc3"
        self.PlotType = 7
        self.CenterPlot = False
        self.UseStandardScale = False
        self.StandardScale = 3

    def GetWindowToPlot(self):
        return getattr(self,"window",((1,2),(3,4)))

    def SetWindowToPlot(self, lower, upper):
        self.window = (tuple(lower.value),tuple(upper.value))

    def GetPlotDeviceNames(self):
        return ("DWG To PDF.pc3", "Original.pc3")


class _FakePlot:
    def __init__(self, should_succeed: bool = True):
        self.QuietErrorMode = False
        self.should_succeed = should_succeed

    def PlotToFile(self, filename, _device):
        if not self.should_succeed:
            return False
        Path(filename).write_bytes(b"%PDF-1.4\nmock body")
        return True


def _pdf_backend(tmp_path, should_succeed=True):
    backend = AcadBackend(tmp_path)
    model = _FakeLayout("Model")
    previous = _FakeLayout("Previous")
    plot = _FakePlot(should_succeed)
    backend.doc = SimpleNamespace(
        Name="test.dwg", FullName="C:/test.dwg", Regen=lambda mode: None,
        Plot=plot,
        ActiveLayout=previous,
        Layouts=SimpleNamespace(Item=lambda name: model if name == "Model" else previous),
    )
    backend.acad = SimpleNamespace(ActiveDocument=backend.doc)
    backend._model_plot_window = lambda:([0,0],[420,297])
    return backend, model, previous, plot


def test_pdf_export_is_atomic_and_restores_all_settings(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cad_acad.time, "sleep", lambda _seconds: None)
    backend, model, previous, plot = _pdf_backend(tmp_path)
    original = {
        name: getattr(model, name)
        for name in ("ConfigName", "PlotType", "CenterPlot", "UseStandardScale", "StandardScale")
    }
    output = tmp_path / "output.pdf"
    backend._plot_to_pdf(output)
    assert output.read_bytes().startswith(b"%PDF-")
    assert {name: getattr(model, name) for name in original} == original
    assert plot.QuietErrorMode is False
    assert backend.doc.ActiveLayout is previous
    assert not list(tmp_path.glob("*.tmp.pdf"))


def test_pdf_export_failure_leaves_no_output_and_restores_settings(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cad_acad.time, "sleep", lambda _seconds: None)
    backend, model, previous, plot = _pdf_backend(tmp_path, should_succeed=False)
    output = tmp_path / "failed.pdf"
    with pytest.raises(RuntimeError, match="拒绝"):
        backend._plot_to_pdf(output)
    assert not output.exists()
    assert model.ConfigName == "Original.pc3"
    assert plot.QuietErrorMode is False
    assert backend.doc.ActiveLayout is previous
    assert not list(tmp_path.glob("*.tmp.pdf"))


def test_create_viewport_activates_layout_through_document_property(
        monkeypatch, tmp_path) -> None:
    layout = SimpleNamespace(Name="Sheet-A3")

    class Viewport:
        Handle = "A"

        def __init__(self):
            self.display_calls = []

        def Display(self, value):
            self.display_calls.append(value)

    viewport = Viewport()

    class PaperSpace:
        def AddPViewport(self, center, width, height):
            assert center == (100.0, 80.0, 0.0)
            assert width == 200
            assert height == 120
            return viewport

    class Document:
        def __init__(self):
            self.ActiveLayout = SimpleNamespace(Name="Model")
            self.PaperSpace = PaperSpace()
            self.variables = []

        def SetVariable(self, name, value):
            self.variables.append((name, value))

    backend = AcadBackend(tmp_path)
    backend.doc = Document()
    backend.msp = SimpleNamespace()
    monkeypatch.setattr(backend, "_ensure_connected", lambda: None)
    monkeypatch.setattr(backend, "_get_layout", lambda name: layout)
    monkeypatch.setattr(cad_acad, "_pt", lambda x, y, z=0.0: (x, y, z))
    monkeypatch.setattr(cad_acad, "_pt2", lambda values: tuple(values))
    # Activation is tested here; strict COM positioning and failed native
    # readback are exercised in test_sheet_layout, not by arbitrary attributes.
    def configure(viewport, desired, lock=True):
        assert backend.doc.ActiveLayout is layout
        assert backend.doc.MSpace is False
        assert desired["model_center"] == [500, 600, 0.0]
        assert desired["custom_scale"] == 0.01
        return {"paper_center": desired["paper_center"], "paper_size": desired["paper_size"],
                "model_center": desired["model_center"], "custom_scale": desired["custom_scale"],
                "locked": lock}
    monkeypatch.setattr(backend, "_configure_sheet_viewport", configure)

    result = backend.create_viewport(
        layout_name="Sheet-A3",
        center_x=100,
        center_y=80,
        width=200,
        height=120,
        model_center_x=500,
        model_center_y=600,
        scale_denominator=100,
        lock=True,
    )

    assert backend.doc.ActiveLayout is layout
    assert backend.doc.variables == [("TILEMODE", 0)]
    assert result["verified"] is True
    assert result["model_center"] == [500, 600]
    assert result["handle"] == "10"


def test_pdf_failure_never_deletes_concurrently_created_target(monkeypatch, tmp_path) -> None:
    backend, model, _, _ = _pdf_backend(tmp_path)
    output = tmp_path / "raced.pdf"

    def fail_after_foreign_create(_layout, path, _device):
        path.write_bytes(b"foreign file")
        raise RuntimeError("publish race")

    monkeypatch.setattr(backend, "_plot_layout_atomic", fail_after_foreign_create)
    with pytest.raises(RuntimeError, match="publish race"):
        backend._plot_to_pdf(output)
    assert output.read_bytes() == b"foreign file"
    assert model.ConfigName == "Original.pc3"


def test_pdf_interrupt_is_reraised_after_restoring_settings(monkeypatch, tmp_path) -> None:
    backend, model, _, _ = _pdf_backend(tmp_path)
    monkeypatch.setattr(
        backend,
        "_plot_layout_atomic",
        lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    with pytest.raises(KeyboardInterrupt):
        backend._plot_to_pdf(tmp_path / "interrupted.pdf")
    assert model.ConfigName == "Original.pc3"
    assert model.PlotType == 7



@pytest.mark.parametrize("success", [True, False])
def test_pdf_activates_bound_document_and_restores_previous_document(tmp_path, monkeypatch, success):
    monkeypatch.setattr(cad_acad.time, "sleep", lambda seconds: None)
    backend, model, previous, plot = _pdf_backend(tmp_path, should_succeed=success)
    other = SimpleNamespace(Name="other.dwg", FullName="C:/other.dwg")
    other.Activate = lambda: setattr(backend.acad,"ActiveDocument",other)
    backend.doc.Activate = lambda: setattr(backend.acad,"ActiveDocument",backend.doc)
    backend._wait_acad_ready = lambda app: None
    backend.acad.ActiveDocument = other
    original_plot = plot.PlotToFile
    def checked_plot(*args):
        assert backend.acad.ActiveDocument is backend.doc
        return original_plot(*args)
    plot.PlotToFile = checked_plot
    if success:
        backend._plot_to_pdf(tmp_path/"active.pdf")
    else:
        with pytest.raises(RuntimeError,match="拒绝"):
            backend._plot_to_pdf(tmp_path/"active.pdf")
    assert backend.acad.ActiveDocument is other
    assert backend.doc.ActiveLayout is previous
    assert model.GetWindowToPlot() == ((1,2),(3,4))
