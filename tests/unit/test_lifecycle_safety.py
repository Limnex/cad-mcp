from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cad_mcp.drawing import autocad as cad_acad
from cad_mcp import server as cad_mcp_server
from cad_mcp.drawing.autocad import AcadBackend
from cad_mcp.drawing.document import CadDocument


def test_switch_mode_refuses_to_drop_open_dxf_document(tmp_path, monkeypatch) -> None:
    backend = CadDocument(tmp_path)
    backend.new_drawing("open.dxf")
    monkeypatch.setattr(cad_mcp_server, "_backend", backend)
    result = json.loads(cad_mcp_server.cad_switch_mode(
        cad_mcp_server.SwitchModeInput(mode="acad")))
    assert result["ok"] is False
    assert "打开的图纸" in result["error"]
    assert backend.doc is not None


def test_dxf_close_explicitly_discards_in_memory_document(tmp_path) -> None:
    backend = CadDocument(tmp_path)
    backend.new_drawing("discard.dxf")
    backend.draw_line(0, 0, 1, 0)
    message = backend.close_drawing(save=False)
    assert "放弃内存修改" in message
    assert backend.doc is None
    assert backend.msp is None
    assert backend.current_path is None


def test_close_failure_is_not_wrapped_as_success(monkeypatch) -> None:
    class Backend:
        def close_drawing(self, save):
            raise RuntimeError("close failed")

    monkeypatch.setattr(cad_mcp_server, "_backend", Backend())
    result = json.loads(cad_mcp_server.cad_close_drawing(
        cad_mcp_server.CloseDrawingInput(save=False)))
    assert result["ok"] is False
    assert "close failed" in result["error"]


def test_acad_new_drawing_rejects_existing_target_before_connect(tmp_path) -> None:
    target = tmp_path / "exists.dwg"
    target.write_bytes(b"existing")
    backend = AcadBackend(tmp_path)
    with pytest.raises(FileExistsError, match="未新建也未覆盖"):
        backend.new_drawing(target.name)


def test_acad_new_drawing_preserves_template_layers_and_current_layer(tmp_path, monkeypatch) -> None:
    template = tmp_path / "qnew.dwt"
    template.write_bytes(b"dwt")

    class Layers:
        def Add(self, name):
            raise AssertionError(f"new_drawing must not create layer {name}")

    class Document:
        def __init__(self):
            self.ModelSpace = object()
            self.Layers = Layers()

    class Documents:
        Count = 0

        def Add(self, path):
            assert path == str(template.resolve())
            return document

    class Application:
        def __init__(self):
            self.Preferences = SimpleNamespace(
                Files=SimpleNamespace(QNewTemplateFile=str(template))
            )
            self.Documents = Documents()

    backend = AcadBackend(tmp_path)
    document = Document()
    monkeypatch.setattr(backend, "_connect", lambda: Application())
    monkeypatch.setattr(backend, "_wait_acad_ready", lambda acad: None)
    monkeypatch.setattr(backend, "_use_doc", lambda doc, expected_path=None: setattr(backend, "doc", doc))
    monkeypatch.setattr(backend, "_ensure_chinese_textstyle", lambda: None)
    monkeypatch.setattr(
        backend, "set_active_layer",
        lambda name: (_ for _ in ()).throw(
            AssertionError(f"new_drawing must not switch active layer to {name}")
        ),
    )

    result = backend.new_drawing("from-template.dwg")

    assert result["template_source"] == "cad_qnew"
    assert result["template_path"] == str(template.resolve())
    assert backend.doc is document
    assert backend.current_path == (tmp_path / "from-template.dwg").resolve()


@pytest.mark.parametrize("absolute", [False, True])
def test_acad_open_drawing_accepts_writable_document(tmp_path, monkeypatch, absolute) -> None:
    managed = tmp_path / "managed"
    target = tmp_path / ("external" if absolute else "managed") / "writable.dwg"
    target.parent.mkdir()
    target.write_bytes(b"dwg")

    class ModelSpace:
        Count = 3

    class Document:
        def __init__(self):
            self.ReadOnly = False
            self.ModelSpace = ModelSpace()

    class Documents:
        def Open(self, path):
            assert path == str(target.resolve())
            return document

    class Application:
        def __init__(self):
            self.Documents = Documents()

    backend = AcadBackend(managed)
    document = Document()
    monkeypatch.setattr(backend, "_connect", lambda: Application())
    monkeypatch.setattr(backend, "_find_open_document", lambda acad, path: None)
    monkeypatch.setattr(backend, "_assert_no_external_lock", lambda path: None)

    def use_doc(opened, expected_path=None):
        assert opened is document
        assert expected_path == target.resolve()
        backend.doc = opened
        backend.msp = opened.ModelSpace

    monkeypatch.setattr(backend, "_use_doc", use_doc)

    message = backend.open_drawing(str(target) if absolute else target.name)

    assert "共 3 个图元" in message
    assert backend.doc is document
    assert backend.current_path == target.resolve()


def test_acad_open_drawing_rejects_readonly_document_and_closes_it(
        tmp_path, monkeypatch) -> None:
    target = tmp_path / "readonly.dwg"
    target.write_bytes(b"dwg")

    class Document:
        ReadOnly = True

        def __init__(self):
            self.close_calls = []

        def Close(self, save):
            self.close_calls.append(save)

    class Documents:
        def Open(self, path):
            assert path == str(target.resolve())
            return document

    class Application:
        def __init__(self):
            self.Documents = Documents()

    backend = AcadBackend(tmp_path)
    document = Document()
    monkeypatch.setattr(backend, "_connect", lambda: Application())
    monkeypatch.setattr(backend, "_find_open_document", lambda acad, path: None)
    monkeypatch.setattr(backend, "_assert_no_external_lock", lambda path: None)
    monkeypatch.setattr(
        backend, "_use_doc",
        lambda *args, **kwargs: pytest.fail("read-only document must not be bound"))

    with pytest.raises(PermissionError, match="以只读方式打开"):
        backend.open_drawing(target.name)

    assert document.close_calls == [False]
    assert backend.doc is None


def test_failed_dxf_to_dwg_conversion_never_falls_back_to_save(tmp_path) -> None:
    class Document:
        def __init__(self):
            self.save_called = False

        def SaveAs(self, path, file_format):
            raise RuntimeError("conversion failed")

        def Save(self):
            self.save_called = True

    backend = AcadBackend(tmp_path)
    backend.doc = Document()
    backend.msp = object()
    backend.current_path = tmp_path / "source.dxf"
    backend._check_doc = lambda: None
    with pytest.raises(RuntimeError, match="保存失败"):
        backend.save()
    assert backend.doc.save_called is False
    assert backend.current_path.suffix == ".dxf"


def test_make_block_delete_failure_raises_for_proxy_rollback(tmp_path, monkeypatch) -> None:
    class Entity:
        Handle = "A"

        def Delete(self):
            raise RuntimeError("delete failed")

    class Blocks:
        Count = 0

        def Add(self, point, name):
            return object()

    class Document:
        def CopyObjects(self, objects, block):
            return None

    Document.Blocks = Blocks()

    entity = Entity()
    backend = AcadBackend(tmp_path)
    backend.doc = Document()
    backend.msp = object()
    backend._check_doc = lambda: None
    backend._find = lambda handle: entity
    monkeypatch.setattr(
        cad_acad.win32com.client, "VARIANT", lambda variant_type, values: values)
    with pytest.raises(RuntimeError, match="源图元删除不完整"):
        backend.make_block("B1", ["10"])


def test_acad_quit_while_disconnected_never_connects_or_starts(
        tmp_path, monkeypatch) -> None:
    backend = AcadBackend(tmp_path)
    released = []
    monkeypatch.setattr(
        backend, "_connect",
        lambda: pytest.fail("quit must not connect to or start AutoCAD"))
    monkeypatch.setattr(
        backend, "_release_autocad_lock", lambda: released.append(True))

    message = backend.quit()

    assert "未连接 AutoCAD" in message
    assert "未启动或接管任何实例" in message
    assert backend.acad is None
    assert released == [True]


def test_closing_last_document_retains_exact_application_reference(tmp_path) -> None:
    class Documents:
        Count = 0

    class Application:
        def __init__(self):
            self.Documents = Documents()

    backend = AcadBackend(tmp_path)
    application = Application()
    backend.acad = application

    backend._maybe_release_acad()

    assert backend.acad is application


def test_acad_quit_uses_only_current_application_and_clears_state(
        tmp_path, monkeypatch) -> None:
    class Application:
        def __init__(self):
            self.quit_calls = 0

        def Quit(self):
            self.quit_calls += 1

    backend = AcadBackend(tmp_path)
    application = Application()
    backend.acad = application
    backend.doc = object()
    backend.msp = object()
    backend.current_path = tmp_path / "open.dwg"
    backend._own_document = True
    backend._undo_mark_open = True
    released = []
    monkeypatch.setattr(
        backend, "_connect",
        lambda: pytest.fail("quit must use only the current AutoCAD instance"))
    monkeypatch.setattr(
        backend, "_release_autocad_lock", lambda: released.append(True))

    message = backend.quit()

    assert application.quit_calls == 1
    assert message == "已退出 AutoCAD，文件锁已释放"
    assert backend.acad is None
    assert backend.doc is None
    assert backend.msp is None
    assert backend.current_path is None
    assert backend._own_document is False
    assert backend._undo_mark_open is False
    assert released == [True]
