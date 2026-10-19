from pathlib import Path

import pytest

from cad_mcp.core import transactions as cad_transactions
from cad_mcp.drawing.document import CadDocument


def test_filename_with_directory_components_is_rejected(tmp_path):
    document = CadDocument(tmp_path)

    with pytest.raises(ValueError, match="文件名非法"):
        document._safe_path(r"..\outside\plant.dxf")


def test_dxf_operation_plan_restores_snapshot_on_failure(monkeypatch, tmp_path):
    document = CadDocument(tmp_path)
    document.new_drawing("rollback.dxf")
    baseline_count = len(document.msp)

    def mutate_then_fail(backend, _plan, _policy):
        backend.draw_line(0, 0, 100, 100)
        raise RuntimeError("planned failure")

    monkeypatch.setattr(
        cad_transactions,
        "execute_reconciled_plan",
        mutate_then_fail,
    )

    with pytest.raises(RuntimeError, match="planned failure"):
        document.apply_operation_plan(object())

    assert len(document.msp) == baseline_count
    assert document.current_path == Path(tmp_path / "rollback.dxf")
