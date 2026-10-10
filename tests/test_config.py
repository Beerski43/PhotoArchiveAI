"""設定ファイルの探索。

以前は「カレントディレクトリの config/app_settings.json」1か所だけを見て
いたため、リポジトリルート以外から起動すると**例外にもならず空の設定が
返っていた**。
"""

import json
import logging
from pathlib import Path

import yaml

from photoarchive_ai import config


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_no_settings_anywhere_gives_an_empty_mapping(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert config.find_settings_path() is None
    assert config.load_settings() == {}


def test_the_current_directory_is_used_when_it_has_a_config(tmp_path, monkeypatch):
    _write(tmp_path / "config/app_settings.yml", {"database_path": "here.db"})
    monkeypatch.chdir(tmp_path)

    assert config.load_settings() == {"database_path": "here.db"}


def test_the_repository_is_used_when_run_from_somewhere_else(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _write(repo / "config/app_settings.yml", {"database_path": "repo.db"})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(config, "REPO_ROOT", repo)
    monkeypatch.chdir(elsewhere)

    assert config.load_settings() == {"database_path": "repo.db"}


def test_the_current_directory_wins_over_the_repository(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _write(repo / "config/app_settings.yml", {"database_path": "repo.db"})
    here = tmp_path / "here"
    _write(here / "config/app_settings.yml", {"database_path": "here.db"})
    monkeypatch.setattr(config, "REPO_ROOT", repo)
    monkeypatch.chdir(here)

    assert config.load_settings() == {"database_path": "here.db"}


def test_the_environment_variable_wins_over_everything(tmp_path, monkeypatch):
    here = tmp_path / "here"
    _write(here / "config/app_settings.yml", {"database_path": "here.db"})
    chosen = _write(tmp_path / "chosen.yml", {"database_path": "chosen.db"})
    monkeypatch.chdir(here)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(chosen))

    assert config.load_settings() == {"database_path": "chosen.db"}


def test_a_broken_settings_file_does_not_stop_the_command(tmp_path, monkeypatch):
    path = tmp_path / "config/app_settings.yml"
    path.parent.mkdir(parents=True)
    path.write_text("database_path: [unclosed", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert config.load_settings() == {}


def test_settings_that_are_not_a_mapping_are_ignored(tmp_path, monkeypatch):
    _write(tmp_path / "config/app_settings.yml", ["not", "a", "mapping"])
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


def test_the_repository_candidate_is_skipped_for_a_normal_install(tmp_path, monkeypatch):
    """editable install でなければ、リポジトリ直下の候補を出さない。

    通常のインストールでは REPO_ROOT が site-packages の外側
    (lib/python3.x) を指すだけで、探しても必ず空振りする。候補に残すと
    「探した場所」のログが誤解を招く。
    """
    # 実際の通常インストールの形。REPO_ROOT は lib/python3.12 を指すので、
    # REPO_ROOT 自身には site-packages が現れない。
    module = tmp_path / "venv/lib/python3.12/site-packages/photoarchive_ai/config.py"
    monkeypatch.setattr(config, "MODULE_PATH", module)
    monkeypatch.setattr(config, "REPO_ROOT", module.parents[2])
    monkeypatch.chdir(tmp_path)

    candidates = [str(path) for path in config.candidate_config_paths()]

    assert candidates == [str(tmp_path / "config/app_settings.yml")]


def test_the_repository_candidate_is_offered_for_an_editable_install(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(config, "MODULE_PATH", repo / "src/photoarchive_ai/config.py")
    monkeypatch.setattr(config, "REPO_ROOT", repo)
    monkeypatch.chdir(elsewhere)

    candidates = [str(path) for path in config.candidate_config_paths()]

    assert candidates == [
        str(elsewhere / "config/app_settings.yml"),
        str(repo / "config/app_settings.yml"),
    ]


def test_the_same_place_is_not_listed_twice(tmp_path, monkeypatch):
    """リポジトリ直下で実行したとき、候補が重複しないこと。"""
    monkeypatch.setattr(config, "MODULE_PATH", tmp_path / "src/photoarchive_ai/config.py")
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)

    candidates = [str(path) for path in config.candidate_config_paths()]

    assert candidates == [str(tmp_path / "config/app_settings.yml")]


# ---------------------------------------------------------------------------
# #27: YAML だけを読む。古い JSON は読まずに、変換を促す
# ---------------------------------------------------------------------------


def test_the_sample_settings_file_is_yaml_and_readable():
    """管理しているサンプルが YAML として読め、既定のルールも YAML を指すこと。"""
    repo = Path(__file__).resolve().parents[1]
    sample = yaml.safe_load((repo / "config/app_settings.sample.yml").read_text(encoding="utf-8"))

    assert sample["database_path"] == "data/photoarchive.db"
    assert sample["rule_path"].endswith(".yml")
    assert not (repo / "config/app_settings.sample.json").exists()


def test_comments_and_japanese_paths_are_read(tmp_path, monkeypatch):
    """YAML にした理由の1つは注釈が書けること。日本語のパスもそのまま読む。"""
    path = tmp_path / "config/app_settings.yml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "# 実データの根\nsource_root: /mnt/nfs/写真/${PERSON_2}携帯  # 注釈\ndlib_model_dir: null\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assert config.load_settings() == {"source_root": "/mnt/nfs/写真/${PERSON_2}携帯", "dlib_model_dir": None}


def test_a_leftover_json_settings_file_is_not_read_but_reported(tmp_path, monkeypatch, caplog):
    """古い app_settings.json だけがあるとき、**読まずに**変換を促す。

    黙って空の設定にすると、`scan` が「source root が要る」とだけ言って止まり、
    形式が変わったことに辿り着けない。
    """
    legacy = tmp_path / "config/app_settings.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"database_path": "old.db"}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with caplog.at_level(logging.WARNING, logger="photoarchive_ai.config"):
        settings = config.load_settings()

    assert settings == {}
    assert config.find_legacy_settings_path() == legacy
    assert "app_settings.yml" in caplog.text
    assert "YAML" in caplog.text


def test_a_json_file_named_by_the_environment_variable_is_not_read(tmp_path, monkeypatch, caplog):
    """環境変数が古い JSON を指していても読まない（中身は YAML として読めてしまう）。"""
    legacy = tmp_path / "old.json"
    legacy.write_text(json.dumps({"database_path": "old.db"}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(legacy))

    with caplog.at_level(logging.WARNING, logger="photoarchive_ai.config"):
        assert config.load_settings() == {}

    assert config.find_legacy_settings_path() == legacy
    assert "old.yml" in caplog.text


def test_the_yaml_file_wins_and_the_json_is_not_mentioned(tmp_path, monkeypatch, caplog):
    """変換し終えて JSON が残っているだけなら、YAML を読み、警告も出さない。"""
    _write(tmp_path / "config/app_settings.yml", {"database_path": "new.db"})
    (tmp_path / "config/app_settings.json").write_text('{"database_path": "old.db"}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with caplog.at_level(logging.WARNING, logger="photoarchive_ai.config"):
        assert config.load_settings() == {"database_path": "new.db"}

    assert caplog.text == ""


def test_the_cli_names_the_leftover_json_instead_of_asking_for_a_database(tmp_path, monkeypatch):
    """CLI が「DB のパスが要る」ではなく、古い設定ファイルのことを言うこと。"""
    import sys

    import pytest

    from photoarchive_ai import cli

    legacy = tmp_path / "config/app_settings.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"database_path": "old.db"}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["photoarchive", "init-db"])

    with pytest.raises(SystemExit) as raised:
        cli.main()

    assert "app_settings.json" in str(raised.value)
    assert "YAML" in str(raised.value)
    assert not (tmp_path / "old.db").exists()


def test_select_help_does_not_offer_json_rules(capsys):
    """`--rule` のヘルプが JSON を受け付けると言わないこと（PR #74 のレビュー指摘1）。"""
    import sys

    import pytest

    from photoarchive_ai import cli

    original = sys.argv
    sys.argv = ["photoarchive", "select", "--help"]
    try:
        with pytest.raises(SystemExit):
            cli.main()
    finally:
        sys.argv = original

    out = capsys.readouterr().out
    assert "JSON or YAML" not in out
    assert "YAML" in out


def test_the_legacy_message_says_when_renaming_alone_is_enough(tmp_path):
    """「拡張子を変えるだけ」はタブ字下げの JSON では成り立たない（PR #74 のレビュー指摘2）。"""
    message = config.legacy_settings_message(tmp_path / "config/app_settings.json")

    assert "空白で字下げしていれば" in message
    assert "タブ" in message


def test_a_renamed_json_indented_with_tabs_is_named_as_the_cause(tmp_path, monkeypatch, caplog):
    """タブ字下げの JSON の拡張子だけを変えたとき、WARNING がタブを名指しすること。"""
    path = tmp_path / "config/app_settings.yml"
    path.parent.mkdir(parents=True)
    path.write_text('{\n\t"database_path": "a.db"\n}\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with caplog.at_level(logging.WARNING, logger="photoarchive_ai.config"):
        assert config.load_settings() == {}

    assert "タブで字下げしています" in caplog.text


def test_the_gui_names_the_leftover_json_instead_of_asking_for_a_database(tmp_path, monkeypatch):
    """GUI も「DB のパスが要る」ではなく古い設定ファイルのことを言う（PR #74 のレビュー指摘3）。

    PySide6 を立ち上げる前に `SystemExit` で抜けるので、画面は要らない。
    """
    import sys

    import pytest

    from photoarchive_ai import gui

    legacy = tmp_path / "config/app_settings.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"database_path": "old.db"}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["photoarchive-gui"])

    with pytest.raises(SystemExit) as raised:
        gui.main()

    assert str(legacy) in str(raised.value)
    assert not (tmp_path / "old.db").exists()


def test_the_stop_message_does_not_repeat_the_warning(tmp_path, monkeypatch, caplog, capsys):
    """WARNING と止める文が、同じ長い案内を2回出さないこと（PR #74 のレビュー指摘3）。"""
    import sys

    import pytest

    from photoarchive_ai import cli

    legacy = tmp_path / "config/app_settings.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"database_path": "old.db"}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["photoarchive", "init-db"])

    with caplog.at_level(logging.WARNING, logger="photoarchive_ai.config"):
        with pytest.raises(SystemExit) as raised:
            cli.main()

    assert config.legacy_settings_message(legacy) in caplog.text
    assert config.legacy_settings_message(legacy) not in str(raised.value)
