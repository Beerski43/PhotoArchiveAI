"""GUI の「取り込み」タブ（#129）。`scan` を裏の別プロセスで流し、止められ、閉じるときは待つ。"""

import os
import sqlite3
import time

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from photoarchive_ai import db
from photoarchive_ai import gui
from photoarchive_ai import gui_scan
from tests.fakes import install_fake_backends
from tests.helpers import write_image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def source(tmp_path):
    source = tmp_path / "media"
    for index in range(3):
        write_image(source / f"{index}.jpg", color=(20 + index * 10, 100, 150))
    return source


@pytest.fixture()
def window(tmp_path, source, monkeypatch):
    database = tmp_path / "test.db"
    db.ensure_database(str(database)).close()
    window = gui.AppWindow(str(database), source_roots=[str(source)])
    window.scan_panel.initializer = install_fake_backends
    window.scan_panel.log_file = str(tmp_path / "logs" / "scan.log")
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: QMessageBox.Ok)
    yield window
    window.jobs.stop_and_wait(timeout=30)
    window.assignment.connection.close()
    window.deleteLater()


def _wait_until_finished(window, timeout=60):
    deadline = time.time() + timeout
    while window.jobs.is_running() and time.time() < deadline:
        window.jobs.poll()
        QCoreApplication.processEvents()
        time.sleep(0.05)
    assert not window.jobs.is_running(), "裏の scan が終わらない"


def test_the_third_tab_runs_a_scan_in_the_background(window, tmp_path):
    assert window.tabText(2) == "取り込み"
    panel = window.scan_panel
    assert [panel.roots_list.item(i).text() for i in range(panel.roots_list.count())] == [
        str(tmp_path / "media")
    ]

    panel.start_scan()

    assert window.jobs.is_running()
    # 実行中は match を押せない・始めるボタンも押せない。顔の割り当ての画面はそのまま
    assert not window.assignment.match_button.isEnabled()
    assert not panel.start_button.isEnabled()
    assert window.job_status.isVisibleTo(window)
    _wait_until_finished(window)

    assert panel.last_final["kind"] == "done"
    assert panel.last_final["summary"]["processed"] == 3
    assert "終わりました" in panel.log.toPlainText()
    assert window.assignment.match_button.isEnabled()
    assert panel.start_button.isEnabled()
    assert not window.job_status.isVisibleTo(window)
    assert db.count_faces(window.assignment.connection) == 3


def test_the_stop_button_stops_and_the_next_scan_continues(window):
    panel = window.scan_panel
    panel.start_scan()
    # 「止める」を押す（子が一覧を作るより先に届けば 0 件、途中なら途中まで）
    window.job_status.stop_button.click()
    _wait_until_finished(window)

    assert panel.last_final["kind"] in ("stopped", "done")
    if panel.last_final["kind"] == "stopped":
        assert "続きから" in panel.log.toPlainText()
    done_before = panel.last_final["summary"]["processed"]

    panel.start_scan()
    _wait_until_finished(window)

    assert panel.last_final["kind"] == "done"
    assert panel.last_final["summary"]["processed"] == 3 - done_before
    assert db.count_faces(window.assignment.connection) == 3


def test_a_scan_that_cannot_start_is_reported(tmp_path, window, monkeypatch):
    panel = window.scan_panel
    panel.roots = [str(tmp_path / "empty")]
    (tmp_path / "empty").mkdir()
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: warned.append(args[2]) or QMessageBox.Ok)

    panel.start_scan()
    _wait_until_finished(window)

    assert panel.last_final["kind"] == "aborted"
    assert warned and "中断" in warned[0]
    assert window.assignment.match_button.isEnabled()


def test_closing_while_scanning_waits_for_the_stop(window):
    window.scan_panel.start_scan()
    assert window.jobs.is_running()

    event = QCloseEvent()
    window.closeEvent(event)

    assert event.isAccepted()
    assert not window.jobs.is_running()
    assert window.scan_panel.last_final["kind"] in ("stopped", "done")


def test_closing_can_be_cancelled_while_scanning(window, monkeypatch):
    window.scan_panel.start_scan()
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.No)

    event = QCloseEvent()
    window.closeEvent(event)

    assert not event.isAccepted()
    assert window.jobs.is_running()
    _wait_until_finished(window)


def test_roots_fall_back_to_the_recorded_ones(tmp_path):
    connection = db.ensure_database(str(tmp_path / "test.db"))
    db.record_scan_root(connection, "/recorded/root")
    connection.commit()

    assert gui_scan.resolve_scan_roots([], connection) == ["/recorded/root"]
    assert gui_scan.resolve_scan_roots(["/from/settings"], connection) == ["/from/settings"]
    connection.close()


def test_the_window_waits_for_other_writers(window):
    timeout = window.assignment.connection.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout == gui.BUSY_TIMEOUT_MS


def test_a_locked_write_is_rolled_back_and_reported(tmp_path, window, monkeypatch):
    """待ちきれずに落ちた書き込みは、書きかけを捨てて知らせる（黙って次の commit に混ぜない）。"""
    connection = window.assignment.connection
    shown = []
    monkeypatch.setattr(gui.QMessageBox, "warning", lambda *args, **kwargs: shown.append(args[2]))
    connection.execute("INSERT INTO Person (name) VALUES ('書きかけ')")

    message = gui.report_unhandled_error(connection, window, sqlite3.OperationalError("database is locked"))

    assert message == gui.WRITE_LOCKED and shown == [gui.WRITE_LOCKED]
    connection.commit()
    assert connection.execute("SELECT COUNT(*) FROM Person").fetchone()[0] == 0


def test_the_error_handler_reports_slot_failures(window, monkeypatch):
    import sys

    shown = []
    monkeypatch.setattr(gui.QMessageBox, "warning", lambda *args, **kwargs: shown.append(args[2]))
    monkeypatch.setattr(sys, "excepthook", lambda *args: None)
    gui.install_error_handler(window)

    try:
        raise sqlite3.OperationalError("database is locked")
    except sqlite3.OperationalError as error:
        sys.excepthook(type(error), error, error.__traceback__)

    assert shown == [gui.WRITE_LOCKED]


def test_status_lines_read_naturally():
    assert gui_scan.describe_event("scan", {"kind": "listing", "current": 400, "total": None}) == (
        "スキャン: ファイルを数えています（400 件）"
    )
    assert "1,000 / 2,000" in gui_scan.describe_event(
        "scan", {"kind": "listing", "current": 1000, "total": 2000}
    )
    assert gui_scan.describe_event(
        "scan", {"kind": "progress", "current": 3, "total": 10, "detail": "a.jpg"}
    ) == "スキャン: 3 / 10 件（a.jpg）"
    assert "ありません" in gui_scan.describe_event("scan", {"kind": "progress", "current": 0, "total": 0})
    assert "何も消していません" in gui_scan.describe_final("scan", {"kind": "aborted", "message": "x"})
