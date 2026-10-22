"""Regressions for the failures recorded by the 2026-10-22 live validation."""

from datetime import date
from types import SimpleNamespace

import pytest
from ezdxf import bbox

from cad_mcp.drawing.document import CadDocument, PAPER_SIZES
from cad_mcp.drawing.pdf_output import repair_autocad_metadata
from cad_mcp.drawing.title_block import title_block_cells


@pytest.mark.parametrize("size", list(PAPER_SIZES))
@pytest.mark.parametrize("long", [False, True])
def test_title_text_fits_individual_cells_and_inner_frame(tmp_path, size, long):
    cad = CadDocument(tmp_path)
    cad.new_drawing("title.dxf")
    params = dict(title="实机标题栏" if not long else "测试图名" * 15,
                  project="软件测试" if not long else "测试工程" * 15,
                  design="设计人", check="审核人", sheet_no="A-001", ratio="1:100")
    message = cad.draw_title_block(420, 297, size=size, **params)
    width, height = PAPER_SIZES[size]
    assert f"{width}×{height}" in message
    bounds, _, _, cells = title_block_cells(width, **params)
    assert bounds[0] >= 25 and bounds[1] == 10 and bounds[2] == width - 10
    entities = list(cad.msp.query("TEXT"))
    assert len(entities) == 6
    for entity, (text, cell, _) in zip(entities, cells):
        assert entity.dxf.text == text
        extent = bbox.extents([entity])
        assert cell[0] - 1e-6 <= extent.extmin.x <= extent.extmax.x <= cell[2] + 1e-6
        assert cell[1] - 1e-6 <= extent.extmin.y <= extent.extmax.y <= cell[3] + 1e-6
    assert entities[-1].dxf.text == f"日期 {date.today().isoformat()}"


def broken_pdf():
    payload = b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/>\n'
    objects = [b"<< /Type /Catalog /Metadata 2 0 R >>",
               b"<</Subtype/XML/Type/Metadata>>stream\n" + payload + b"endstream",
               b"<< /Producer (pdfplot17.hdi) >>"]
    data = b"%PDF-1.7\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(data)
    data += b"xref\n0 4\n0000000000 65535 f \n"
    data += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    data += f"trailer\n<< /Size 4 /Root 1 0 R /Info 3 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return data, payload, xref


def test_pdf_metadata_repair_preserves_original_page_bytes(tmp_path):
    original, payload, previous = broken_pdf()
    path = tmp_path / "plot.pdf"
    path.write_bytes(original)
    assert repair_autocad_metadata(path)
    result = path.read_bytes()
    assert result.startswith(original)
    assert f"/Length {len(payload)} >>\nstream\n".encode() in result
    assert f"/Prev {previous}".encode() in result
    assert result.endswith(b"%%EOF\n")


@pytest.mark.parametrize("mutation", ["truncated", "signed", "encrypted", "wrong_xref"])
def test_pdf_unrecognized_damage_is_not_silently_published(tmp_path, mutation):
    data, _, _ = broken_pdf()
    if mutation == "truncated":
        data = data[:-10]
    elif mutation == "signed":
        data += b"/ByteRange"
    elif mutation == "encrypted":
        data += b"/Encrypt"
    else:
        data = data.replace(b"0000000009 00000 n", b"0000000010 00000 n")
        # Corrupt the root reference as well, independent of fixture offsets.
        data = data.replace(b"/Root 1 0 R", b"/Root invalid")
    path = tmp_path / "broken.pdf"
    path.write_bytes(data)
    with pytest.raises((RuntimeError, ValueError)):
        repair_autocad_metadata(path)
    assert path.read_bytes() == data


def test_pdf_without_identified_driver_defect_is_unchanged(tmp_path):
    path = tmp_path / "normal.pdf"
    path.write_bytes(b"%PDF-1.7\nnormal fixture\n%%EOF\n")
    assert repair_autocad_metadata(path) is False


def test_block_arrays_pass_raw_dispatch_pointers(monkeypatch):
    from cad_mcp.dwt import autocad
    pointer = object()
    block = SimpleNamespace(_oleobj_=pointer)
    wrapped = SimpleNamespace(_obj=block)
    monkeypatch.setattr(autocad.win32com.client, "VARIANT", lambda kind, values: (kind, values))
    kind, values = autocad._dispatch_array([wrapped])
    assert kind == autocad.pythoncom.VT_ARRAY | autocad.pythoncom.VT_DISPATCH
    assert values == [pointer]


@pytest.mark.parametrize("saved,dbmod,path,protected", [
    (True, 0, "", True), (True, 0, "Drawing1.dwg", True),
    (False, 1, "", False), (True, 0, "C:/drawings/blank.dwg", False)])
def test_untouched_startup_document_is_protected_before_open(saved, dbmod, path, protected):
    from cad_mcp.drawing.document_guard import assert_safe_document_open
    from cad_mcp.core.runtime import ConflictError
    original = SimpleNamespace(Name="Drawing1.dwg", FullName=path, Saved=saved, GetVariable=lambda name: dbmod)
    app = SimpleNamespace(Documents=SimpleNamespace(Count=1, Item=lambda index: original))
    if protected:
        with pytest.raises(ConflictError, match="自动关闭"):
            assert_safe_document_open(app)
    else:
        assert_safe_document_open(app)
    assert original.Saved == saved and original.FullName == path


@pytest.mark.parametrize("corruption", [None, "reference", "property", "precision", "scale"])
def test_evaluated_attribute_requires_native_code_and_reference_proof(tmp_path, monkeypatch, corruption):
    from cad_mcp.drawing.autocad import AcadBackend
    from cad_mcp.drawing import native_fields
    from cad_mcp.core.runtime import UnsupportedOperationError
    attribute = SimpleNamespace(TagString="VALUE", Handle="B", TextString="", Update=lambda: None)
    block = SimpleNamespace(EntityName="AcDbBlockReference", HasAttributes=True, GetAttributes=lambda: [attribute])
    source = SimpleNamespace(Handle="A", StartPoint=(1000, 2000, 0))
    backend = AcadBackend(tmp_path)
    backend._check_doc = lambda: None
    backend._object_from_api_handle = lambda handle: {"1": block, "2": source}[handle]
    backend._field_property = lambda source, requested: ("StartPoint", 1)
    backend._field_object_id = lambda doc, source: "100"
    variables = {"FIELDEVAL": 0}
    backend.doc = SimpleNamespace(GetVariable=variables.__getitem__, SetVariable=variables.__setitem__,
                                  Regen=lambda mode: setattr(attribute, "TextString", "X=1.000 m"))
    native = {"code": 'X=\\AcObjProp Object(...).StartPoint \\f "%lu2%pr3%pt1%ct8[0.001]" m', "references": ["A"]}
    if corruption == "reference":
        native["references"] = ["C"]
    elif corruption == "property":
        native["code"] = native["code"].replace("StartPoint", "EndPoint")
    elif corruption == "precision":
        native["code"] = native["code"].replace("%pr3", "%pr2")
    elif corruption == "scale":
        native["code"] = native["code"].replace("%ct8[0.001]", "%ct8[1]")
    monkeypatch.setattr(native_fields, "read_attribute_field", lambda *args: native)
    def bind():
        return backend.bind_block_attribute_field(block_handle="1", attribute_tag="VALUE", source_type="object",
            source_handle="2", source_property="start_x", linear_scale_factor=0.001, prefix="X=", suffix=" m")
    if corruption:
        with pytest.raises(UnsupportedOperationError):
            bind()
    else:
        result = bind()
        assert result["field_expression_verified"] and not result["static_fallback"]
        assert variables["FIELDEVAL"] == 16


def test_native_field_reader_is_bounded_and_does_not_create_dictionaries():
    from cad_mcp.drawing.native_fields import field_reader
    script = field_reader("A", "C:/temp/receipt.txt")
    assert "dictsearch" in script and "256" in script
    assert "vl-string->list" in script
    for forbidden in ("entmod", "entmake", "GetExtensionDictionary", "SetVariable", "command-s"):
        assert forbidden.casefold() not in script.casefold()
    with pytest.raises(ValueError):
        field_reader('A") (command "ERASE")', "C:/temp/receipt.txt")


def test_model_plot_window_uses_actual_entities_and_projects_to_display_coordinates(tmp_path, monkeypatch):
    from cad_mcp.drawing.autocad import AcadBackend
    entity = SimpleNamespace(GetBoundingBox=lambda: ((0,0,0),(420,297,0)))
    backend = AcadBackend(tmp_path)
    backend.msp = SimpleNamespace(Count=1,Item=lambda index:entity)
    backend._scan_checkpoint = lambda index:None
    calls = []
    def translate(point, source, target, displacement):
        assert (source,target,displacement) == (0,2,False)
        calls.append(point.value)
        x,y,z = point.value
        return (y+10,-x+20,z)
    backend.doc = SimpleNamespace(Utility=SimpleNamespace(TranslateCoordinates=translate))
    low, high = backend._model_plot_window()
    assert low == pytest.approx([7.9,-402.1])
    assert high == pytest.approx([309.1,22.1])
    assert len(calls) == 8


def test_empty_model_is_rejected_before_plot_configuration(tmp_path):
    from cad_mcp.drawing.autocad import AcadBackend
    backend = AcadBackend(tmp_path)
    backend.msp = SimpleNamespace(Count=0)
    with pytest.raises(ValueError,match="为空"):
        backend._model_plot_window()
