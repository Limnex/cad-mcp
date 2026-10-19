import json

from cad_mcp import cli


def test_config_set_and_show(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.json"
    drawings = tmp_path / "drawings"
    monkeypatch.setenv("CAD_CONFIG_FILE", str(path))
    monkeypatch.delenv("CAD_DRAWINGS_DIR", raising=False)

    assert cli.main(["config", "set", "drawings-dir", str(drawings)]) == 0
    assert cli.main(["config", "show"]) == 0

    output = capsys.readouterr().out
    start = output.index("{")
    settings = json.loads(output[start:])
    assert settings["drawings_dir"] == str(drawings)
    assert settings["config_path"] == str(path)


def test_config_unset_restores_default(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.json"
    monkeypatch.setenv("CAD_CONFIG_FILE", str(path))

    monkeypatch.delenv("CAD_MODE", raising=False)
    cli.main(["config", "set", "mode", "dxf"])
    cli.main(["config", "unset", "mode"])

    assert json.loads(path.read_text(encoding="utf-8")) == {}
    capsys.readouterr()
    assert cli.main(["config", "show"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "acad"
