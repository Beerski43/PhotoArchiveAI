"""設定ファイルの探索。

以前は「カレントディレクトリの config/app_settings.json」1か所だけを見て
いたため、リポジトリルート以外から起動すると**例外にもならず空の設定が
返っていた**。
"""

import json
from pathlib import Path

from photoarchive_ai import config


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_no_settings_anywhere_gives_an_empty_mapping(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert config.find_settings_path() is None
    assert config.load_settings() == {}


def test_the_current_directory_is_used_when_it_has_a_config(tmp_path, monkeypatch):
    _write(tmp_path / "config/app_settings.json", {"database_path": "here.db"})
    monkeypatch.chdir(tmp_path)

    assert config.load_settings() == {"database_path": "here.db"}


def test_the_repository_is_used_when_run_from_somewhere_else(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _write(repo / "config/app_settings.json", {"database_path": "repo.db"})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(config, "REPO_ROOT", repo)
    monkeypatch.chdir(elsewhere)

    assert config.load_settings() == {"database_path": "repo.db"}


def test_the_current_directory_wins_over_the_repository(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _write(repo / "config/app_settings.json", {"database_path": "repo.db"})
    here = tmp_path / "here"
    _write(here / "config/app_settings.json", {"database_path": "here.db"})
    monkeypatch.setattr(config, "REPO_ROOT", repo)
    monkeypatch.chdir(here)

    assert config.load_settings() == {"database_path": "here.db"}


def test_the_environment_variable_wins_over_everything(tmp_path, monkeypatch):
    here = tmp_path / "here"
    _write(here / "config/app_settings.json", {"database_path": "here.db"})
    chosen = _write(tmp_path / "chosen.json", {"database_path": "chosen.db"})
    monkeypatch.chdir(here)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(chosen))

    assert config.load_settings() == {"database_path": "chosen.db"}


def test_a_broken_settings_file_does_not_stop_the_command(tmp_path, monkeypatch):
    path = tmp_path / "config/app_settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ not json", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert config.load_settings() == {}


def test_settings_that_are_not_a_mapping_are_ignored(tmp_path, monkeypatch):
    _write(tmp_path / "config/app_settings.json", ["not", "a", "mapping"])
    monkeypatch.chdir(tmp_path)

    assert config.load_settings() == {}


def test_the_accessors_read_the_expected_keys():
    settings = {
        "database_path": "db",
        "source_root": "src",
        "output_root": "out",
        "rule_path": "rule",
        "dlib_model_dir": "models",
    }

    assert config.get_database_path(settings) == "db"
    assert config.get_source_root(settings) == "src"
    assert config.get_output_root(settings) == "out"
    assert config.get_rule_path(settings) == "rule"
    assert config.get_dlib_model_dir(settings) == "models"
    assert config.get_dlib_model_dir({}) is None
