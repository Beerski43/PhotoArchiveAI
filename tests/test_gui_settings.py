"""設定の窓（#131）。`config/app_settings.yml` を画面で直し、開いている画面に効かせる。

**リポジトリには本物の設定がある。** 探す場所（環境変数・カレント・リポジトリ直下）を
すべて `tmp_path` に向けてから窓を開く（`isolated_config`）。
"""

import os

import pytest
import yaml
from PySide6.QtWidgets import QApplication, QMessageBox

from photoarchive_ai import config, db, gui, gui_settings
from tests.helpers import write_image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """設定を探す場所を全部 tmp_path の中にする。返すのは、窓が書く先。"""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.delenv(config.CONFIG_ENV_VAR, raising=False)
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "no-repo")
    return work / "config" / "app_settings.yml"


@pytest.fixture()
def answers(monkeypatch):
    """確認の窓の答え。出た文を `shown` に残す。"""
    state = {"question": QMessageBox.Yes, "shown": []}

    def question(_parent, _title, text, *args, **kwargs):
        state["shown"].append(text)
        return state["question"]

    def warning(_parent, _title, text, *args, **kwargs):
        state["shown"].append(text)
        return QMessageBox.Ok

    monkeypatch.setattr(QMessageBox, "question", question)
    monkeypatch.setattr(QMessageBox, "warning", warning)
    monkeypatch.setattr(QMessageBox, "information", warning)
    return state


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def _media(tmp_path, name):
    folder = tmp_path / name
    write_image(folder / "a.jpg", color=(20, 100, 150))
    return folder


@pytest.fixture()
def window(tmp_path):
    database = tmp_path / "test.db"
    db.ensure_database(str(database)).close()
    window = gui.AppWindow(str(database))
    yield window
    window.jobs.stop_and_wait(timeout=30)
    window.assignment.connection.close()
    window.deleteLater()


# ----------------------------------------------------------------------
# 書き戻す（config）
# ----------------------------------------------------------------------


def test_settings_are_written_where_they_will_be_read_next_time(isolated_config, tmp_path, monkeypatch):
    # 無ければカレントの config/app_settings.yml
    assert config.settings_write_path() == isolated_config
    # 環境変数があればそちら
    elsewhere = tmp_path / "elsewhere.yml"
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(elsewhere))
    assert config.settings_write_path() == elsewhere
    # いま読んでいるファイルがあればそれ
    _write(isolated_config, {"database_path": "a.db"})
    monkeypatch.delenv(config.CONFIG_ENV_VAR)
    assert config.settings_write_path() == isolated_config


def test_writing_keeps_other_keys_and_a_backup(isolated_config):
    _write(isolated_config, {"database_path": "old.db", "dlib_model_dir": "/models", "output_root": "/out"})
    before = isolated_config.read_text(encoding="utf-8")

    config.write_settings(isolated_config, {"database_path": "new.db", "source_roots": ["/a"], "output_root": None})

    written = yaml.safe_load(isolated_config.read_text(encoding="utf-8"))
    # 画面に出さないキーは残る。空にしたキーは消える
    assert written == {"database_path": "new.db", "dlib_model_dir": "/models", "source_roots": ["/a"]}
    assert (isolated_config.parent / "app_settings.yml.bak").read_text(encoding="utf-8") == before
    assert config.load_settings() == written


def test_an_unreadable_settings_file_is_not_overwritten(isolated_config):
    isolated_config.parent.mkdir(parents=True)
    isolated_config.write_text("database_path: [broken\n", encoding="utf-8")

    with pytest.raises(config.SettingsWriteError):
        config.write_settings(isolated_config, {"database_path": "new.db"})
    assert isolated_config.read_text(encoding="utf-8") == "database_path: [broken\n"


def test_only_the_dialog_keys_can_be_written(isolated_config):
    with pytest.raises(KeyError):
        config.write_settings(isolated_config, {"dlib_model_dir": "/x"})


# ----------------------------------------------------------------------
# 保存の前に確かめる
# ----------------------------------------------------------------------


def test_nested_roots_and_an_output_overlapping_a_root_are_refused(tmp_path):
    outer = _media(tmp_path, "photos")
    inner = _media(outer, "2019")
    other = _media(tmp_path, "phone")

    errors, _ = gui_settings.check_paths([str(outer), str(inner)], None)
    assert any("入れ子" in error for error in errors)

    for output in (other, other / "out", tmp_path):  # 同じ・内側・外側（root を含む）
        errors, _ = gui_settings.check_paths([str(other)], str(output))
        assert any("重なっています" in error for error in errors), output

    errors, warnings = gui_settings.check_paths([str(other)], str(tmp_path / "out"))
    assert errors == []
    assert any("まだありません" in warning for warning in warnings)


def test_a_missing_root_asks_instead_of_refusing(tmp_path):
    errors, warnings = gui_settings.check_paths([str(tmp_path / "unmounted")], None)
    assert errors == []
    assert any("見つかりません" in warning for warning in warnings)


# ----------------------------------------------------------------------
# 窓
# ----------------------------------------------------------------------


def test_saving_applies_roots_output_and_rule_to_the_open_tabs(window, tmp_path, isolated_config, answers):
    assert window.cornerWidget(gui.Qt.TopLeftCorner) is window.settings_button
    assert window.scan_panel.roots == []
    media = _media(tmp_path, "media")
    rule = tmp_path / "rule.yml"
    _write(rule, {"weights": {"sharpness": 0.5}})
    output = tmp_path / "out"
    output.mkdir()

    dialog = gui_settings.SettingsDialog(window.database_path, window)
    assert not isolated_config.exists()
    dialog.add_root(str(media))
    dialog.output_edit.setText(str(output))
    dialog.rule_edit.setText(str(rule))
    assert dialog.save()
    window.apply_settings(dialog.saved)

    assert yaml.safe_load(isolated_config.read_text(encoding="utf-8")) == {
        "source_roots": [str(media)],
        "output_root": str(output),
        "rule_path": str(rule),
    }
    assert not dialog.database_needs_restart
    # 取り込みタブの root・選定タブの出力先とルール（読み直して重みに出る）
    assert window.scan_panel.roots == [str(media)]
    assert window.scan_panel.start_button.isEnabled()
    assert window.selection.output_root == str(output)
    assert window.selection.rule_path == str(rule)
    assert window.selection.weight_spins["sharpness"].value() == pytest.approx(0.5)
    assert window.selection.source_roots == [str(media)]
    assert window.assignment.source_roots == [str(media)]


def test_the_same_rule_path_keeps_the_weights_moved_on_screen(window, tmp_path):
    rule = tmp_path / "rule.yml"
    _write(rule, {"weights": {"sharpness": 0.5}})
    window.selection.set_paths(str(rule), None, [])
    window.selection.weight_spins["sharpness"].setValue(0.2)

    assert not window.selection.set_paths(str(rule), str(tmp_path / "out"), [])
    assert window.selection.weight_spins["sharpness"].value() == pytest.approx(0.2)


def test_a_new_database_path_waits_for_the_next_start(window, tmp_path, isolated_config, answers):
    dialog = gui_settings.SettingsDialog(window.database_path, window)
    dialog.database_edit.setText(str(tmp_path / "other.db"))

    assert dialog.save()
    assert dialog.database_needs_restart
    window.apply_settings(dialog.saved)
    # 開いている接続は差し替えない
    assert window.assignment.db_path == window.database_path

    same = gui_settings.SettingsDialog(window.database_path, window)
    same.database_edit.setText(window.database_path)
    assert same.save()
    assert not same.database_needs_restart


def test_refused_settings_are_not_written(window, tmp_path, isolated_config, answers):
    media = _media(tmp_path, "media")
    dialog = gui_settings.SettingsDialog(window.database_path, window)
    dialog.add_root(str(media))
    dialog.output_edit.setText(str(media / "out"))

    assert not dialog.save()
    assert not isolated_config.exists()
    assert any("重なっています" in text for text in answers["shown"])

    # 無い root は聞く。いいえなら書かない
    answers["question"] = QMessageBox.No
    dialog.output_edit.setText("")
    dialog.add_root(str(tmp_path / "unmounted"))
    assert not dialog.save()
    assert not isolated_config.exists()
    answers["question"] = QMessageBox.Yes
    assert dialog.save()
    assert config.get_source_roots(config.load_settings()) == [str(media), str(tmp_path / "unmounted")]


def test_the_dialog_shows_the_current_file_and_keeps_other_keys(window, isolated_config, answers):
    _write(isolated_config, {"database_path": "data/x.db", "source_roots": ["/a", "/b"], "dlib_model_dir": "/m"})

    dialog = gui_settings.SettingsDialog(window.database_path, window)
    assert dialog.database_edit.text() == "data/x.db"
    assert dialog.roots() == ["/a", "/b"]
    dialog.roots_list.setCurrentRow(0)
    dialog.remove_selected_root()
    assert dialog.roots() == ["/b"]
    assert dialog.save()  # /b は無いが、はいと答える

    written = yaml.safe_load(isolated_config.read_text(encoding="utf-8"))
    assert written == {"database_path": "data/x.db", "source_roots": ["/b"], "dlib_model_dir": "/m"}


def test_a_broken_settings_file_cannot_be_saved_over(window, isolated_config):
    isolated_config.parent.mkdir(parents=True)
    isolated_config.write_text("source_roots: [broken\n", encoding="utf-8")

    dialog = gui_settings.SettingsDialog(window.database_path, window)
    assert not dialog.save_button.isEnabled()
    assert "読めない" in dialog.read_error


def test_the_corner_button_opens_saves_and_applies(window, tmp_path, isolated_config, answers, monkeypatch):
    media = _media(tmp_path, "media")

    def exec_(dialog):
        dialog.add_root(str(media))
        dialog.database_edit.setText(str(tmp_path / "other.db"))
        dialog._save_clicked()
        return dialog.result()

    monkeypatch.setattr(gui_settings.SettingsDialog, "exec", exec_)
    saved = window.open_settings()

    assert saved["source_roots"] == [str(media)]
    assert window.scan_panel.roots == [str(media)]
    assert any("次に起動したときから" in text for text in answers["shown"])

    # 閉じただけなら何も変えない
    monkeypatch.setattr(gui_settings.SettingsDialog, "exec", lambda dialog: 0)
    assert window.open_settings() is None
