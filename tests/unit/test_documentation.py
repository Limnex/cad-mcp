"""Keep public documentation, examples and links consistent with the code."""
import inspect
import json
from pathlib import Path
import re
import runpy
import unicodedata
from urllib.parse import unquote, urlsplit

from pydantic import ValidationError

from cad_mcp import server
from cad_mcp.config import load_config_payload
from cad_mcp.core.contracts import EnvironmentModel
from cad_mcp.dwt.contracts import DraftingProfileV2
from cad_mcp.engineering.block_audit import BlockAuditManifest
from cad_mcp.engineering.templates import TemplateManifest


ROOT = Path(__file__).resolve().parents[2]


def markdown_files():
    return sorted([*ROOT.glob("*.md"), *(ROOT / "docs").rglob("*.md"),
                   *(ROOT / "skills").rglob("*.md")])


def anchors(text):
    found = set()
    counts = {}
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        if fenced:
            continue
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if not match:
            continue
        heading = re.sub(r"<[^>]+>", "", match[1]).lower()
        slug = "".join(c for c in heading if c in "-_ " or
                       unicodedata.category(c)[0] in "LN").replace(" ", "-")
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        found.add(slug if count == 0 else f"{slug}-{count}")
    return found


def test_generated_tool_docs_match_runtime():
    generator = runpy.run_path(str(ROOT / "tools/generate_tool_reference.py"))
    outputs = generator["render"](server)
    for path, expected in outputs.items():
        assert path.read_text(encoding="utf-8") == expected, path
    snapshot = json.loads(outputs[ROOT / "docs/tool-schemas.json"])
    assert snapshot["tool_count"] == len(server._FULL_TOOL_REGISTRY)
    assert {t["name"] for t in snapshot["tools"]} == set(server._FULL_TOOL_REGISTRY)


def test_markdown_local_links_and_anchors():
    problems = []
    for path in markdown_files():
        text = path.read_text(encoding="utf-8")
        for target in re.findall(r"(?<!!)\[[^\]\n]+\]\(([^)\n]+)\)", text):
            target = target.strip().strip("<>")
            url = urlsplit(target)
            if url.scheme or url.netloc:
                continue
            linked = (path.parent / unquote(url.path)).resolve() if url.path else path
            if not linked.exists():
                problems.append((path.relative_to(ROOT).as_posix(), target, "missing file"))
            elif url.fragment and linked.suffix.lower() == ".md":
                if unquote(url.fragment) not in anchors(linked.read_text(encoding="utf-8")):
                    problems.append((path.relative_to(ROOT).as_posix(), target, "missing heading"))
    assert not problems, problems


def test_documented_json_request_examples_match_input_models():
    models_by_name = {
        name: inspect.signature(inspect.unwrap(t.fn)).parameters["params"].annotation
        for name, t in server._FULL_TOOL_REGISTRY.items()
    }
    models = set(models_by_name.values())
    checked = []
    fill_kinds = set()
    for path in markdown_files():
        text = path.read_text(encoding="utf-8")
        for block in re.findall(r"```json\s*\n(.*?)\n```", text, re.S):
            payload = json.loads(block)
            if not isinstance(payload, dict):
                continue
            if "params" in payload:
                params = payload["params"]
            elif "tool_name" in payload and "arguments" in payload:
                gateway = server.GatewayCallParams.model_validate(payload)
                target = models_by_name[gateway.tool_name]
                target.model_validate(gateway.arguments["params"])
                checked.append((path, gateway.tool_name))
                continue
            elif ({"handles", "radius"} <= set(payload) or
                  {"manifest_path", "layout_id", "page_setup_name"} <= set(payload)):
                # Some contracts show the contents of params without its envelope.
                params = payload
            else:
                # Non-request JSON (for example a block-library configuration)
                # is parsed here and validated by the bundled-model test below.
                continue
            matching = []
            for model in models:
                if not set(params) <= set(model.model_fields):
                    continue
                try:
                    model.model_validate(params)
                    matching.append(model)
                except ValidationError:
                    pass
            assert matching, (str(path.relative_to(ROOT)), sorted(params))
            checked.append((path, sorted(params)))
            if {"boundary_handle", "points"} <= set(params):
                fill_kinds.add("handle" if params["boundary_handle"] is not None else "points")
    assert checked, "No documented request examples were discovered"
    assert fill_kinds == {"handle", "points"}, "Both required-null fill variants must be documented"


def test_bundled_json_examples_match_current_models():
    examples = ROOT / "skills/cad-mcp/examples"
    checks = {
        examples / "environment-model.json": EnvironmentModel,
        examples / "template-manifest.json": TemplateManifest,
        examples / "block-library_manifest.json": BlockAuditManifest,
        examples / "dwt-profile-v2.schema-example.json": DraftingProfileV2,
        ROOT / "skills/cad-mcp/config/user/general-environment-dwt-course-v2.json": DraftingProfileV2,
    }
    for path, model in checks.items():
        model.model_validate_json(path.read_text(encoding="utf-8"))
    load_config_payload(json.loads((ROOT / "examples/config.example.json").read_text(encoding="utf-8")))
