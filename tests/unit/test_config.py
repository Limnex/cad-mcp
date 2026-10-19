import json
from pathlib import Path

import pytest

from cad_mcp.config import ConfigError, load_config, load_settings, save_config


@pytest.mark.parametrize("mode", ["acad", "dxf"])
def test_file_configuration_is_loaded(tmp_path, mode):
    path = tmp_path / "config.json"
    save_config(
        {
            "drawings_dir": str(tmp_path / "drawings"),
            "log_dir": str(tmp_path / "logs"),
            "mode": mode,
        },
        path,
    )

    settings = load_settings(env={}, path=path)

    assert settings.drawings_dir == tmp_path / "drawings"
    assert settings.log_dir == tmp_path / "logs"
    assert settings.mode == mode


def test_missing_configuration_defaults_to_acad(tmp_path):
    path = tmp_path / "missing-config.json"

    settings = load_settings(env={}, path=path)

    assert settings.mode == "acad"
    assert not path.exists()


def test_environment_overrides_file_configuration(tmp_path):
    path = tmp_path / "config.json"
    save_config({"mode": "acad"}, path)

    settings = load_settings(env={"CAD_MODE": "dxf"}, path=path)

    assert settings.mode == "dxf"


def test_invalid_or_unknown_fields_are_rejected(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"unknown": "value"}', encoding="utf-8")

    with pytest.raises(ConfigError, match="未知字段"):
        load_config(path)


def test_config_is_written_as_utf8_json(tmp_path):
    path = tmp_path / "config.json"

    save_config({"drawings_dir": str(tmp_path / "中文图纸")}, path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["drawings_dir"].endswith("中文图纸")
