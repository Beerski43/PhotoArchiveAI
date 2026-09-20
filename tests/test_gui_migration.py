"""起動時に、移行が要ることに気づいて、その場で実行できること。

これまでは「`photoarchive migrate` を実行してください」と言って終了していた。
**GUI しか使わない利用者は、そこで手が止まる。**
"""

import os
import sqlite3

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from photoarchive_ai import db, migration
from photoarchive_ai import gui as photoarchive_gui
from tests.test_migration import _build_v2_database

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def silent_message_box(monkeypatch):
    """出た知らせを集める。テストでモーダルを開かない。"""
    shown = {"information": [], "critical": []}
    monkeypatch.setattr(
        photoarchive_gui.QMessageBox,
        "information",
        lambda *args: shown["information"].append(args[2]),
    )
    monkeypatch.setattr(
        photoarchive_gui.QMessageBox,
        "critical",
        lambda *args: shown["critical"].append(args[2]),
    )
    return shown


def _person_columns(path) -> set:
    connection = sqlite3.connect(str(path))
    try:
        return {row[1] for row in connection.execute("PRAGMA table_info(Person)")}
    finally:
        connection.close()


def _make_dialog(accepted: bool):
    """`MigrationDialog` の代わり。出された文面を記録する。"""

    class _FakeDialog:
        summary = None

        def __init__(self, parent=None, summary="", backs_up=True):
            _FakeDialog.summary = summary

        def exec(self):
            return QDialog.Accepted if accepted else QDialog.Rejected

    return _FakeDialog


def test_a_current_database_opens_without_asking_anything(tmp_path, monkeypatch):
    """移行が要らないなら、**何も出さずに先へ進む。**"""
    database = tmp_path / "fresh.db"
    db.ensure_database(str(database)).close()
    dialog = _make_dialog(accepted=True)
    monkeypatch.setattr(photoarchive_gui, "MigrationDialog", dialog)

    assert photoarchive_gui.ensure_migrated(str(database)) is True
    assert dialog.summary is None


def test_choosing_to_run_migrates_the_database(tmp_path, monkeypatch, silent_message_box):
    """「移行を実行」で、その場で移行され、起動を続けられること。"""
    database = tmp_path / "v2.db"
    _build_v2_database(database)
    dialog = _make_dialog(accepted=True)
    monkeypatch.setattr(photoarchive_gui, "MigrationDialog", dialog)

    assert photoarchive_gui.ensure_migrated(str(database)) is True

    assert "birth_date" in _person_columns(database)
    assert migration.needs_migration(str(database)) is False
    # 移行した後は、普通に開ける
    db.ensure_database(str(database)).close()
    # 何をしたかを知らせる
    assert any("移行が完了しました" in text for text in silent_message_box["information"])


def test_the_confirmation_says_what_is_kept(tmp_path, monkeypatch):
    """**何が残るのかを見せてから実行する。**

    「破棄します」と読める案内を出すと、実行をためらって移行が進まない。
    文面は CLI と同じものを使う。
    """
    database = tmp_path / "v2.db"
    _build_v2_database(database)
    dialog = _make_dialog(accepted=False)
    monkeypatch.setattr(photoarchive_gui, "MigrationDialog", dialog)

    photoarchive_gui.ensure_migrated(str(database))

    assert "は保持します" in dialog.summary
    assert "顔データ 3 件はそのまま残ります" in dialog.summary
    assert "破棄" not in dialog.summary


def test_choosing_to_quit_leaves_the_database_alone(tmp_path, monkeypatch):
    """「終了」を選んだら、**データベースに触らない。**"""
    database = tmp_path / "v2.db"
    _build_v2_database(database)
    monkeypatch.setattr(photoarchive_gui, "MigrationDialog", _make_dialog(accepted=False))

    assert photoarchive_gui.ensure_migrated(str(database)) is False

    assert "birth_date" not in _person_columns(database)
    connection = sqlite3.connect(str(database))
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    connection.close()


def test_a_failed_migration_does_not_let_the_window_open(
    tmp_path, monkeypatch, silent_message_box
):
    """**失敗を成功のように見せない。** 開いてしまうと、次に落ちるのは保存のとき。"""
    database = tmp_path / "v2.db"
    _build_v2_database(database)
    monkeypatch.setattr(photoarchive_gui, "MigrationDialog", _make_dialog(accepted=True))

    def explode(path, log=None):
        if log is not None:
            log("バックアップを作成しました: v2.db.bak-test")
        raise RuntimeError("integrity_check failed: すごく壊れている")

    monkeypatch.setattr(photoarchive_gui.migration, "migrate_database", explode)

    assert photoarchive_gui.ensure_migrated(str(database)) is False

    reported = "\n".join(silent_message_box["critical"])
    assert "すごく壊れている" in reported
    # **どこまで進んだかも出す**（バックアップを取ったかどうかが要る）
    assert "バックアップを作成しました" in reported
    assert silent_message_box["information"] == []


def test_a_database_whose_version_ran_ahead_is_offered_the_migration(tmp_path, monkeypatch):
    """版だけ進んで列が足りていないDBも、起動時に拾えること。

    実データがこの状態だった（`Person.birth_date` が無いまま版だけ 3）。
    """
    database = tmp_path / "stamped.db"
    _build_v2_database(database)
    connection = sqlite3.connect(str(database))
    connection.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION}")
    connection.commit()
    connection.close()
    monkeypatch.setattr(photoarchive_gui, "MigrationDialog", _make_dialog(accepted=True))

    assert photoarchive_gui.ensure_migrated(str(database)) is True
    assert "birth_date" in _person_columns(database)


def test_the_dialog_does_not_start_on_the_run_button(qt_app):
    """**Enter の連打で走り出さないこと。** 既定は「終了」。"""
    dialog = photoarchive_gui.MigrationDialog(summary="移行対象: x.db")
    dialog.show()
    qt_app.processEvents()

    assert dialog.quit_button.isDefault() is True
    assert dialog.run_button.isDefault() is False
    assert "移行対象: x.db" in "\n".join(
        child.text() for child in dialog.findChildren(photoarchive_gui.QLabel)
    )
    dialog.close()
