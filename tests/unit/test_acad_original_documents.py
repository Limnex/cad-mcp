"""Check real-test preservation rules without connecting to AutoCAD."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def policy(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "integration" / "test_semantic_acad.py"
    spec = importlib.util.spec_from_file_location("semantic_document_policy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "fresh_com", lambda value: value)
    return module


def blank(**changes):
    return {"name": "Drawing1.dwg", "path": "", "dwgtitled": 0, "saved": True,
            "dbmod": 0, "model_entities": 0, "paper_entities": 2,
            "paper_content_entities": 0, **changes}


def collection(items):
    return SimpleNamespace(Count=len(items), Item=items.__getitem__)


@pytest.mark.parametrize("path", ["", "Drawing1.dwg"])
def test_pristine_untitled_blank_may_disappear(policy, path):
    original = blank(path=path)
    assert policy._disposable_blank(original)
    assert policy._document_changes([original], []) == []
    assert policy._final_document_changes([original], []) == []


@pytest.mark.parametrize("changes", [
    {"path": "C:/drawings/empty.dwg", "dwgtitled": 1},
    {"dwgtitled": 1}, {"saved": False}, {"dbmod": 1},
    {"model_entities": 1}, {"paper_content_entities": 1},
    {"path": "C:/drawings/Drawing1.dwg"},
])
def test_every_other_original_is_still_required(policy, changes):
    original = blank(**changes)
    assert not policy._disposable_blank(original)
    assert policy._document_changes([original], [dict(original)]) == []
    assert policy._document_changes([original], [])[0]["reason"] == "missing"
    assert policy._final_document_changes([original], [])[0]["reason"] == "missing"


@pytest.mark.parametrize("missing", list(blank()))
def test_incomplete_snapshot_is_never_excluded(policy, missing):
    original = blank()
    del original[missing]
    assert not policy._disposable_blank(original)


@pytest.mark.parametrize("changes", [
    {"saved": False}, {"dbmod": 1}, {"model_entities": 1},
    {"paper_entities": 3}, {"paper_content_entities": 1},
])
def test_retained_blank_cannot_be_changed_silently(policy, changes):
    original = blank()
    current = blank(**changes)
    result = policy._document_changes([original], [current])
    assert result == [{"before": original, "after": current, "reason": "changed"}]


@pytest.mark.parametrize("field,value", [
    ("name", "renamed.dwg"), ("path", "C:/drawings/renamed.dwg"),
    ("saved", False), ("dbmod", 1), ("model_entities", 3),
    ("paper_entities", 5), ("paper_content_entities", 2), ("dwgtitled", 0),
])
def test_existing_saved_original_keeps_all_snapshot_checks(policy, field, value):
    original = blank(path="C:/drawings/work.dwg", dwgtitled=1, model_entities=2)
    result = policy._document_changes([original], [{**original, field: value}])
    assert result and result[0]["before"] == original


def test_mixed_baseline_excludes_only_blank_and_detects_leaked_test_drawing(policy):
    empty = blank()
    saved = blank(name="work.dwg", path="C:/drawings/work.dwg", dwgtitled=1, model_entities=2)
    unsaved = blank(name="Drawing2.dwg", saved=False, dbmod=1, model_entities=1)
    original = [empty, saved, unsaved]
    current = [unsaved, saved]
    assert policy._final_document_changes(original, current) == []
    replacement_blank = blank(name="Drawing3.dwg")
    assert policy._final_document_changes(original, [*current, replacement_blank]) == []
    leaked = blank(name="test.dwg", path="C:/test.dwg", dwgtitled=1)
    assert policy._final_document_changes(original, [*current, leaked])[-1] == {
        "before": None, "after": leaked, "reason": "unexpected"}
    assert policy._final_document_changes(original, [saved])[0]["before"] == unsaved


@pytest.mark.parametrize("original", [None, blank()])
def test_restore_skips_only_absent_blank_or_no_original(policy, original):
    app = SimpleNamespace(Documents=collection([]))
    assert policy._restore_original_active(app, original) is False


def test_restore_missing_nonblank_original_fails(policy):
    app = SimpleNamespace(Documents=collection([]))
    with pytest.raises(AssertionError, match="missing_original_active_document"):
        policy._restore_original_active(app, blank(model_entities=1))


@pytest.mark.parametrize("original", [blank(), blank(path="C:/work.dwg", dwgtitled=1)])
def test_restore_resolves_and_activates_currently_open_original(policy, original):
    activated = []
    document = SimpleNamespace(Name=original["name"], FullName=original["path"],
                               Activate=lambda: activated.append(True))
    app = SimpleNamespace(Documents=collection([document]))
    assert policy._restore_original_active(app, original) is True
    assert activated == [True]


@pytest.mark.parametrize("paper_kinds,expected_blank", [
    ([], True), (["AcDbViewport", "AcDbViewport"], True),
    (["AcDbViewport", "AcDbText"], False), (["AcDbBlockReference"], False),
])
def test_snapshot_checks_every_paper_layout_for_actual_content(policy, paper_kinds, expected_blank):
    variables = {"DBMOD": 0, "DWGTITLED": 0}
    entity = lambda kind: SimpleNamespace(ObjectName=kind)
    document = SimpleNamespace(
        Name="Drawing1.dwg", FullName="", Saved=True,
        GetVariable=variables.__getitem__, ModelSpace=collection([]),
        Layouts=collection([
            SimpleNamespace(ModelType=True),
            SimpleNamespace(ModelType=False, Block=collection([entity("AcDbViewport")])),
            SimpleNamespace(ModelType=False, Block=collection(list(map(entity, paper_kinds)))),
        ]))
    row = policy._documents(SimpleNamespace(Documents=collection([document])))[0]
    assert row["paper_entities"] == 1 + len(paper_kinds)
    assert policy._disposable_blank(row) is expected_blank
