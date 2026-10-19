import json
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = PLUGIN_ROOT


def test_portable_plugin_manifests_are_consistent():
    plugin = json.loads(
        (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    mcp = json.loads((PLUGIN_ROOT / ".mcp.json").read_text(encoding="utf-8"))
    marketplace = json.loads(
        (REPO_ROOT / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8")
    )

    assert plugin["name"] == "cad-mcp"
    assert plugin["skills"] == "./skills/"
    assert plugin["mcpServers"] == "./.mcp.json"
    assert mcp["mcpServers"]["cad-mcp"] == {
        "type": "stdio",
        "command": "cad-mcp",
        "args": [],
    }
    source = marketplace["plugins"][0]["source"]["path"]
    assert (REPO_ROOT / source).resolve() == PLUGIN_ROOT
    assert (PLUGIN_ROOT / "pyproject.toml").is_file()
    legacy = json.loads(
        (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )["plugins"][0]
    assert (REPO_ROOT / legacy["source"]).resolve() == PLUGIN_ROOT
    assert legacy["name"] == plugin["name"]
    assert legacy["version"] == plugin["version"]


def test_skill_and_examples_are_packaged():
    skill = PLUGIN_ROOT / "skills" / "cad-mcp"
    assert (skill / "SKILL.md").is_file()
    for example in (skill / "examples").glob("*.json"):
        json.loads(example.read_text(encoding="utf-8"))


def test_portable_files_do_not_embed_the_original_install_path():
    checked = [
        PLUGIN_ROOT / ".codex-plugin" / "plugin.json",
        PLUGIN_ROOT / ".mcp.json",
        REPO_ROOT / "README.md",
    ]
    checked.extend((REPO_ROOT / "docs").glob("*.md"))
    checked.extend((PLUGIN_ROOT / "skills" / "cad-mcp").rglob("*.md"))
    for path in checked:
        text = path.read_text(encoding="utf-8")
        assert "C:\\Users\\123" not in text
