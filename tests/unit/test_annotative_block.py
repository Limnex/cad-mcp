from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cad_mcp import server
from cad_mcp.drawing.autocad import AcadBackend, _lisp_string
from cad_mcp.drawing.document import CadDocument


def test_make_block_schema_preserves_non_annotative_default() -> None:
    params = server.MakeBlockInput(name="B1", handles=["10"])
    assert params.annotative is False
    assert server.MakeBlockInput.model_json_schema()["properties"]["annotative"]["type"] == "boolean"


def test_dxf_annotative_true_rejects_without_writing(tmp_path, monkeypatch) -> None:
    backend = CadDocument(tmp_path)
    backend.new_drawing("scratch.dxf")
    entity = backend.msp.add_line((0, 0), (10, 0))
    handle = str(int(entity.dxf.handle, 16))
    before_blocks = len(backend.doc.blocks)
    before_entities = len(backend.msp)
    monkeypatch.setattr(server, "_backend", backend)

    response = json.loads(server.cad_make_block(server.MakeBlockInput(
        name="ANNO", handles=[handle], annotative=True)))

    assert response["ok"] is False
    assert response["error_detail"]["code"] == "UNSUPPORTED_MODE"
    assert len(backend.doc.blocks) == before_blocks
    assert len(backend.msp) == before_entities
    assert backend._get_entity(handle) is entity


def test_dxf_false_keeps_existing_message_contract(tmp_path, monkeypatch) -> None:
    backend = CadDocument(tmp_path)
    backend.new_drawing("scratch.dxf")
    entity = backend.msp.add_line((0, 0), (10, 0))
    monkeypatch.setattr(server, "_backend", backend)

    response = json.loads(server.cad_make_block(server.MakeBlockInput(
        name="PLAIN", handles=[str(int(entity.dxf.handle, 16))])))

    assert response["ok"] is True
    assert isinstance(response["message"], str)
    assert "annotative" not in response
    assert "PLAIN" in backend.doc.blocks


def _mock_acad_backend(tmp_path, *, xdata=None):
    backend = AcadBackend(tmp_path)
    entity = SimpleNamespace(Handle="A")
    block = SimpleNamespace(
        Name="ANNO", Count=1,
        GetXData=lambda app: xdata if xdata is not None else (
            (1001, 1000, 1002, 1070, 1070, 1002),
            ("AcadAnnotative", "AnnotativeData", "{", 1, 1, "}"),
        ),
    )
    doc = SimpleNamespace(
        Utility=SimpleNamespace(TranslateCoordinates=lambda point, src, dst, displacement: (3.0, 4.0, 0.0)),
        Blocks=SimpleNamespace(Item=lambda name: block),
    )
    backend.doc = doc
    backend.msp = SimpleNamespace(Count=0)
    backend._check_doc = lambda: None
    backend._block_exists = lambda name: False
    backend._find = lambda handle: entity if handle == "10" else None
    commands = []
    backend._send_command_and_wait = commands.append
    return backend, commands


def test_acad_annotative_command_and_readback(tmp_path) -> None:
    backend, commands = _mock_acad_backend(tmp_path)

    result = backend.make_block("ANNO", ["10"], 3, 4, annotative=True)

    assert result["annotative"] is True
    assert result["verified"] is True
    assert result["readback"]["entity_count"] == 1
    assert result["readback"]["source_handles_removed"] is True
    assert len(commands) == 1
    assert '"_Annotative" "_Yes"' in commands[0]
    assert '"_Mode" "_Delete" "_non" (list 3 4 0)' in commands[0]
    assert '(handent cadmcp_anno_h)' in commands[0]


def test_acad_annotative_missing_handle_stops_before_command(tmp_path) -> None:
    backend, commands = _mock_acad_backend(tmp_path)
    with pytest.raises(ValueError, match="不存在于模型空间"):
        backend.make_block("ANNO", ["99"], annotative=True)
    assert commands == []


def test_acad_annotative_readback_mismatch_is_failure(tmp_path) -> None:
    backend, commands = _mock_acad_backend(tmp_path, xdata=(None, None))
    with pytest.raises(RuntimeError, match="无法回读验证"):
        backend.make_block("ANNO", ["10"], annotative=True)
    assert len(commands) == 1


def test_acad_annotative_readback_failure_triggers_proxy_rollback(tmp_path) -> None:
    backend, _ = _mock_acad_backend(tmp_path, xdata=(None, None))
    rollback = []
    backend._acquire_autocad_lock = lambda: None
    backend._wait_operation_ready = lambda: None
    backend._begin_undo = lambda: True
    backend._commit_undo = lambda started: pytest.fail("failed write committed")
    backend._rollback_undo = lambda started: rollback.append(started) or True
    backend._release_autocad_lock = lambda: None

    with pytest.raises(RuntimeError, match="无法回读验证") as failure:
        server._AcadLockProxy(backend).make_block("ANNO", ["10"], annotative=True)

    assert rollback == [True]
    # A boolean undo reply alone cannot prove that geometry/XData were restored.
    assert failure.value.rollback_status == "failed_or_unverified"


def test_lisp_string_escapes_untrusted_name() -> None:
    assert _lisp_string('A") (alert "bad') == '"A\\") (alert \\"bad"'
    with pytest.raises(ValueError, match="控制字符"):
        _lisp_string("A\nB")


def test_attribute_block_annotative_schema_and_dxf_guard(tmp_path, monkeypatch) -> None:
    params = server.CreateOrUpdateAttributeBlockInput(block_name="TAG")
    assert params.annotative is False
    assert server.CreateOrUpdateAttributeBlockInput.model_json_schema()[
        "properties"]["annotative"]["type"] == "boolean"

    backend = CadDocument(tmp_path)
    backend.new_drawing("scratch.dxf")
    before_blocks = len(backend.doc.blocks)
    monkeypatch.setattr(server, "_backend", backend)
    response = json.loads(server.cad_create_or_update_attribute_block(
        server.CreateOrUpdateAttributeBlockInput(
            block_name="TAG",
            attribute_definitions=[server.AttributeDefinitionInput(tag="VALUE")],
            annotative=True,
            dry_run=True,
        )))
    assert response["ok"] is False
    assert response["error_detail"]["code"] == "UNSUPPORTED_MODE"
    assert len(backend.doc.blocks) == before_blocks
    with pytest.raises(Exception, match="仅支持 acad"):
        backend.create_or_update_attribute_block(
            "TAG", [0, 0, 0], [], [{"tag": "VALUE"}], annotative=True)
    assert len(backend.doc.blocks) == before_blocks


def _mock_annotative_attribute_backend(tmp_path, *, drop_attribute=False):
    backend = AcadBackend(tmp_path)
    items = []
    next_handle = iter(range(10, 100))

    class Entity:
        EntityName = "AcDbLine"

        def __init__(self, handle=None):
            self.Handle = format(
                next(next_handle) if handle is None else handle, "X")

        def Copy(self):
            copied = Entity()
            items.append(copied)
            return copied

    class Attribute(Entity):
        EntityName = "AcDbAttributeDefinition"
        StyleName = "Standard"

        def __init__(self, height, mode, prompt, position, tag, value):
            super().__init__()
            self.Height = height
            self.Constant = bool(mode & 2)
            self.PromptString = prompt
            self.InsertionPoint = position
            self.TagString = tag
            self.TextString = value
            self.Rotation = 0.0

    class Reference:
        deleted = False

        def GetAttributes(self):
            return [
                SimpleNamespace(TagString=entity.TagString)
                for entity in block_items
                if isinstance(entity, Attribute) and not entity.Constant
            ]

        def Delete(self):
            self.deleted = True

    class ModelSpace:
        def __init__(self):
            self.references = []

        @property
        def Count(self):
            return len(items)

        def Item(self, index):
            return items[index]

        def AddAttribute(self, height, mode, prompt, position, tag, value):
            attribute = Attribute(height, mode, prompt, position, tag, value)
            items.append(attribute)
            return attribute

        def InsertBlock(self, *args):
            reference = Reference()
            self.references.append(reference)
            return reference

    class Block:
        Name = "ANNOTATED"

        @property
        def Count(self):
            return len(block_items)

        def Item(self, index):
            return block_items[index]

    class Blocks:
        def Item(self, name):
            assert name == "ANNOTATED"
            return Block()

    class Styles:
        def Item(self, name):
            assert name == "Standard"
            return object()

    model = ModelSpace()
    source = Entity()
    items.append(source)
    block_items = []
    backend.msp = model
    backend.doc = SimpleNamespace(Blocks=Blocks(), TextStyles=Styles())
    backend._check_doc = lambda: None
    backend._block_exists = lambda name: False
    backend._find = lambda handle: next(
        (entity for entity in items if int(entity.Handle, 16) == int(handle)),
        None,
    )

    def make_native(name, handles, x, y):
        assert name == "ANNOTATED"
        assert len(handles) == 2
        selected = {
            int(handle) for handle in handles
        }
        block_items[:] = [
            entity for entity in items
            if int(entity.Handle, 16) in selected
        ]
        items[:] = [
            entity for entity in items
            if int(entity.Handle, 16) not in selected
        ]
        if drop_attribute:
            block_items[:] = [
                entity for entity in block_items
                if not isinstance(entity, Attribute)
            ]
        return {
            "verified": True,
            "annotative": True,
            "readback": {"annotative_xdata": [(1070, 1)]},
        }

    backend._make_annotative_block = make_native
    return backend, source, model


def test_acad_annotative_attribute_block_has_editable_reference(tmp_path) -> None:
    backend, source, model = _mock_annotative_attribute_backend(tmp_path)
    result = backend.create_or_update_attribute_block(
        "ANNOTATED", [0, 0, 0], [str(int(source.Handle, 16))],
        [{"tag": "VALUE", "prompt": "Value", "default_value": "A1",
          "text_height": 2.5, "position": [1, 2, 0]}],
        annotative=True,
    )
    assert result["annotative"] is True
    assert result["verified"] is True
    assert result["attribute_tags"] == ["VALUE"]
    assert result["readback"]["editable_tags"] == ["VALUE"]
    assert model.references[0].deleted is True
    assert source in [model.Item(i) for i in range(model.Count)]


def test_acad_annotative_attribute_mismatch_rolls_back(tmp_path) -> None:
    backend, source, _ = _mock_annotative_attribute_backend(
        tmp_path, drop_attribute=True)
    rollback = []
    backend._acquire_autocad_lock = lambda: None
    backend._wait_operation_ready = lambda: None
    backend._begin_undo = lambda: True
    backend._commit_undo = lambda started: pytest.fail("failed write committed")
    backend._rollback_undo = lambda started: rollback.append(started) or True
    backend._release_autocad_lock = lambda: None
    with pytest.raises(RuntimeError, match="属性标签读回不一致") as failure:
        server._AcadLockProxy(backend).create_or_update_attribute_block(
            "ANNOTATED", [0, 0, 0], [str(int(source.Handle, 16))],
            [{"tag": "VALUE"}], annotative=True)
    assert rollback == [True]
    assert failure.value.rollback_status == "failed_or_unverified"
