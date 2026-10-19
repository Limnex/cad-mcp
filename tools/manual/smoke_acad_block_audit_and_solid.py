#!/usr/bin/env python3
"""AutoCAD 实机冒烟：闭合边界实心填充回读 + DWG 块规格审计。"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import win32com.client


if os.environ.get("CAD_RUN_ACAD_TESTS") != "1":
    print("SKIP: 实机 AutoCAD 测试未启用；如需执行，请设置 CAD_RUN_ACAD_TESTS=1")
    raise SystemExit(0)

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = PLUGIN_ROOT / "src"
RUN_ID = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
TEST_DIR = PLUGIN_ROOT / ".cache" / "manual" / f"acad-block-audit-solid-{RUN_ID}"
os.environ["CAD_LOG_DIR"] = str(TEST_DIR / "logs")
TEST_DIR.mkdir(parents=True, exist_ok=False)
sys.path.insert(0, str(SOURCE_DIR))

from cad_mcp.drawing.autocad import AcadBackend, _ComRetryProxy  # noqa: E402
from cad_mcp.dwt.contracts import sha256_file  # noqa: E402
from cad_mcp.server import _AcadLockProxy  # noqa: E402


def _manifest(source: Path, source_hash: str, expected_fingerprint: str | None) -> dict:
    return {
        "schema_version": "1.0",
        "source_dwg": str(source.resolve()),
        "source_sha256": source_hash,
        "blocks": [
            {
                "name": "AUDIT_SMOKE_BLOCK",
                "fingerprint_quantization": 0.001,
                "expected_fingerprint": expected_fingerprint,
                "connection_tolerance": 0.001,
                "symmetry_tests": [
                    {
                        "id": "horizontal-axis",
                        "kind": "reflection",
                        "tolerance": 0.001,
                        "axis_point": [0.0, 30.0, 0.0],
                        "axis_direction": [1.0, 0.0, 0.0],
                        "center": None,
                        "order": None,
                    },
                    {
                        "id": "vertical-axis",
                        "kind": "reflection",
                        "tolerance": 0.001,
                        "axis_point": [50.0, 0.0, 0.0],
                        "axis_direction": [0.0, 1.0, 0.0],
                        "center": None,
                        "order": None,
                    },
                ],
                "checks": [
                    {
                        "id": "size",
                        "path": "/geometry/bounding_box/size",
                        "operator": "within",
                        "expected": [100.0, 60.0, 0.0],
                        "tolerance": 0.001,
                    },
                    {
                        "id": "entity-count",
                        "path": "/properties/entity_count_recursive",
                        "operator": "equals",
                        "expected": 1,
                        "tolerance": None,
                    },
                    {
                        "id": "layers",
                        "path": "/properties/layers",
                        "operator": "unordered_equals",
                        "expected": ["AUDIT_GEOM"],
                        "tolerance": None,
                    },
                    {
                        "id": "linetypes",
                        "path": "/properties/linetypes",
                        "operator": "unordered_equals",
                        "expected": ["ByLayer"],
                        "tolerance": None,
                    },
                    {
                        "id": "lineweights",
                        "path": "/properties/lineweights",
                        "operator": "unordered_equals",
                        "expected": [-1],
                        "tolerance": None,
                    },
                    {
                        "id": "nested-references",
                        "path": "/properties/nested_references",
                        "operator": "unordered_equals",
                        "expected": [],
                        "tolerance": None,
                    },
                ],
            }
        ],
    }


def main() -> None:
    try:
        active_acad = win32com.client.GetActiveObject("AutoCAD.Application")
    except Exception as exc:
        raise SystemExit(
            "FAIL: 未检测到已由用户打开的 AutoCAD；本脚本不会自动冷启动。"
        ) from exc

    backend = AcadBackend(TEST_DIR)
    # 直接注入已存在的 COM 实例，确保后端的 _connect 不可能进入冷启动分支。
    backend.acad = _ComRetryProxy(active_acad)
    cad = _AcadLockProxy(backend)
    created_own_document = False
    try:
        drawing = cad.new_drawing(f"block-audit-solid-{RUN_ID}.dwg")
        created_own_document = True
        source = Path(drawing["path"])

        polygon = [(200.0, 0.0), (300.0, 0.0), (300.0, 60.0), (200.0, 60.0)]
        dry_run = cad.fill_closed_boundary_solid(
            boundary_handle=None,
            points=polygon,
            layer="SOLID_FILL",
            associative=True,
            keep_boundary=True,
            dry_run=True,
        )
        assert dry_run["dry_run"] is True and dry_run["would_write"] is False, dry_run

        filled = cad.fill_closed_boundary_solid(
            boundary_handle=None,
            points=polygon,
            layer="SOLID_FILL",
            associative=True,
            keep_boundary=True,
            dry_run=False,
        )
        assert filled["verified"] is True, filled
        assert filled["readback"]["entity_type"] == "AcDbHatch", filled
        assert filled["readback"]["pattern_name"].casefold() == "solid", filled
        assert filled["readback"]["loop_count"] == 1, filled

        rectangle = cad.draw_rectangle(0.0, 0.0, 100.0, 60.0, "AUDIT_GEOM")
        cad.make_block("AUDIT_SMOKE_BLOCK", [rectangle["handle"]], 0.0, 0.0)
        cad.save()
        source_hash = sha256_file(source)
        backend.close_document(False)
        created_own_document = False

        manifest_path = TEST_DIR / "block-audit-solid_manifest.json"
        manifest = _manifest(source, source_hash, None)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        first = cad.audit_block_definition(
            source_path=str(source),
            block_name="AUDIT_SMOKE_BLOCK",
            manifest_path=str(manifest_path),
        )
        assert first["source_unchanged"] is True, first
        assert first["comparison"]["overall_status"] == "无法验证", first
        assert first["block"]["entity_tree_complete"] is True, first
        assert len(first["fingerprint"]["sha256"]) == 64, first
        assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash

        manifest["blocks"][0]["expected_fingerprint"] = first["fingerprint"]["sha256"]
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        verified = cad.audit_block_definition(
            source_path=str(source),
            block_name="AUDIT_SMOKE_BLOCK",
            manifest_path=str(manifest_path),
        )
        assert verified["comparison"]["overall_status"] == "通过", verified
        assert all(
            item["status"] == "通过"
            for item in verified["comparison"]["items"]
        ), verified
        assert verified["source_hash"] == source_hash, verified

        result_path = TEST_DIR / "verified-result.json"
        result_path.write_text(
            json.dumps(
                {"solid_fill": filled, "block_audit": verified},
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        print(f"PASS: AutoCAD 块审计与实心填充回读通过；证据目录: {TEST_DIR}")
    finally:
        if created_own_document:
            try:
                backend.close_document(False)
            except Exception as exc:
                print(f"WARN: 临时测试图纸关闭失败: {exc}")


if __name__ == "__main__":
    main()
