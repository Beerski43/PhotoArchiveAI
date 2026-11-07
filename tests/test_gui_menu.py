"""GUI のメニュー（#132）。タブは2枚で、ほかの処理はメニューから別の窓で動かす。

`evaluate` と `unassign-auto` もここから流す。
"""

import os
import sqlite3
import time

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from photoarchive_ai import db, evaluation, gui, jobs
from photoarchive_ai import gui_evaluate, gui_select
from tests.fakes import install_fake_backends

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def _vector(axis: int, tilt: float = 0.0) -> list:
    vector = np.zeros(db.EMBEDDING_DIM, dtype=np.float32)
    vector[axis] = 1.0
    vector[axis + 1] = tilt
    return (vector / np.linalg.norm(vector)).tolist()


def _face(connection, index, axis, tilt=0.0):
    media_id = db.save_media(
        connection,
        {
            "path": f"/photos/m{index}.jpg",
            "filename": f"m{index}.jpg",
            "type": "image",
            "file_hash": f"h{index}",
            "file_size": 100,
            "created_time": "2026-01-01T00:00:00",
            "shooting_date": "2020-06-01T10:00:00",
        },
    )
    return db.add_face(
        connection,
        media_id=media_id,
        bbox=(0, 10, 10, 0),
        embedding=_vector(axis, tilt),
        embed_version=db.embedding_model.ACTIVE.version,
        thumbnail=b"",
    )


@pytest.fixture()
def seeded(tmp_path):
    """2人の手本（2枚ずつ）・それぞれの自動割り当て・顔認証外。"""
    database = tmp_path / "menu.db"
    connection = db.ensure_database(str(database))
    first = db.add_person(connection, "人物イ")
    second = db.add_person(connection, "人物ロ")
    db.assign_faces(connection, [_face(connection, 0, 0), _face(connection, 1, 0, 0.1)], first, db.ASSIGN_MANUAL)
    db.assign_faces(connection, [_face(connection, 2, 5), _face(connection, 3, 5, 0.1)], second, db.ASSIGN_MANUAL)
    faces = {
        "イの自動": _face(connection, 4, 0, 0.05),
        "ロの自動": _face(connection, 5, 5, 0.05),
        "顔認証外": _face(connection, 6, 9),
    }
    db.assign_faces(connection, [faces["イの自動"]], first, db.ASSIGN_AUTO)
    db.assign_faces(connection, [faces["ロの自動"]], second, db.ASSIGN_AUTO)
    db.reject_faces(connection, [faces["顔認証外"]])
    connection.commit()
    connection.close()
    return database, first, second, faces


@pytest.fixture()
def window(seeded, tmp_path, monkeypatch):
    database, *_rest = seeded
    window = gui.AppWindow(str(database))
    window.evaluate_window.initializer = install_fake_backends
    window.evaluate_window.log_file = str(tmp_path / "logs" / "evaluate.log")
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
    yield window
    window.jobs.stop_and_wait(timeout=30)
    window.assignment.connection.close()
    window.deleteLater()


@pytest.fixture()
def shown(monkeypatch):
    messages = []
    for kind in ("information", "warning", "critical"):
        monkeypatch.setattr(
            QMessageBox,
            kind,
            lambda parent, title, text, *a, _kind=kind, **k: messages.append((_kind, title, text)),
        )
    return messages


def _wait(window, timeout=60):
    deadline = time.time() + timeout
    while window.jobs.is_running() and time.time() < deadline:
        window.jobs.poll()
        QCoreApplication.processEvents()
        time.sleep(0.05)
    assert not window.jobs.is_running(), "裏の処理が終わらない"


def _rows(connection):
    return {row["id"]: row for row in db.list_faces(connection)}


# ---------------------------------------------------------------------------
# メニューの有効・無効
# ---------------------------------------------------------------------------


def test_the_menu_bar_has_import_faces_and_settings(window):
    titles = [action.text() for action in window.menuBar().actions()]
    assert titles == ["取り込み(&I)", "顔の割り当て(&F)", "写真の選定(&P)", "設定(&S)"]
    assert [action.text() for action in window.selection_menu.actions()] == [
        "ルールを読み込む…",
        "ルールを保存",
        "別のファイルに保存…",
    ]
    assert [action.text() for action in window.import_menu.actions()] == [
        "取り込みウィザード…",
        "写真の良し悪しを測る（assess）…",
    ]
    assert [action.text() for action in window.face_menu.actions()] == [
        "自動割り当て（match）…",
        "自動割り当てを取り消す（unassign-auto）…",
        "精度を測る（evaluate）…",
        "",
        gui.ACTION_UNDO,
        gui.ACTION_REDO,
    ]
    assert window.tabs.count() == 2
    assert window.tabs.cornerWidget() is window.job_status, "進み具合はタブの右上のまま"


def test_the_face_items_work_only_on_the_assignment_tab(window):
    face_items = (window.match_action, window.unassign_action, window.evaluate_action)
    assert window.face_menu.isEnabled() and all(item.isEnabled() for item in face_items)

    window.tabs.setCurrentWidget(window.selection)

    assert not window.face_menu.isEnabled()
    assert not any(item.isEnabled() for item in face_items)
    assert window.import_action.isEnabled() and window.settings_action.isEnabled()

    window.tabs.setCurrentWidget(window.assignment)
    assert window.face_menu.isEnabled() and all(item.isEnabled() for item in face_items)


def test_the_rule_items_work_only_on_the_selection_tab(window):
    """**ルールの操作はメニューから**（#18・利用者の指定）。タブのボタンは無い。"""
    assert not window.selection_menu.isEnabled()
    window.tabs.setCurrentWidget(window.selection)
    assert window.selection_menu.isEnabled()
    window.tabs.setCurrentWidget(window.assignment)
    assert not window.selection_menu.isEnabled()
    assert not any(hasattr(window.selection, name) for name in ("load_rule_button", "save_button", "save_as_button"))


@pytest.mark.parametrize(
    "action, method",
    [("load_rule_action", "load_rule_file"), ("save_rule_action", "save_rule"), ("save_rule_as_action", "save_rule_as")],
)
def test_each_rule_item_runs_its_operation(tmp_path, monkeypatch, action, method):
    called = []
    monkeypatch.setattr(gui_select.SelectionPanel, method, lambda self: called.append(method))
    database = tmp_path / "menu.db"
    db.ensure_database(str(database)).close()
    window = gui.AppWindow(str(database), output_root=str(tmp_path / "out"))
    window.tabs.setCurrentWidget(window.selection)
    getattr(window, action).trigger()
    assert called == [method]
    window.close()


def test_undo_and_redo_run_from_the_menu(window):
    """**戻す・進むはメニューからも**（#18・利用者の指定）。顔の一覧の打鍵と同じ項目なので、押せる・押せないも同じ。"""
    menu = window.face_menu.actions()
    assert window.assignment.action_undo in menu and window.assignment.action_redo in menu
    assert not window.assignment.action_undo.isEnabled(), "戻すものが無ければ押せない"

    calls = []
    window.assignment._undo_or_redo = lambda redo: calls.append(redo)
    window.assignment.action_undo.setEnabled(True)
    window.assignment.action_redo.setEnabled(True)
    # 打鍵の経路と同じ triggered を通す（作ったときの lambda が self の属性を引く）
    window.assignment.action_undo.trigger()
    window.assignment.action_redo.trigger()
    assert calls == [False, True]


def test_the_toolbars_follow_the_tab_and_use_the_menu_actions(window, qt_app):
    """**タブに関係するアイコンは、そのタブのときだけ。** 項目はメニューと同じなので、押せる・押せないも一致する。"""
    window.show()
    qt_app.processEvents()
    assert window.common_toolbar.isVisible()
    assert window.face_toolbar.isVisible() and not window.selection_toolbar.isVisible()
    assert window.match_action in window.face_toolbar.actions()
    assert window.settings_action in window.common_toolbar.actions()

    window.tabs.setCurrentWidget(window.selection)
    qt_app.processEvents()
    assert window.common_toolbar.isVisible()
    assert window.selection_toolbar.isVisible() and not window.face_toolbar.isVisible()
    assert window.save_rule_action in window.selection_toolbar.actions()


def test_every_toolbar_action_has_an_icon_and_a_name(window):
    for toolbar in (window.common_toolbar, window.face_toolbar, window.selection_toolbar):
        for action in toolbar.actions():
            assert not action.icon().isNull(), action.text()
            assert action.toolTip(), "アイコンだけなので、マウスを載せたら名前が出る"


def test_the_choose_and_export_icons_run_the_panel(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(gui_select.SelectionPanel, "choose", lambda self: called.append("choose"))
    monkeypatch.setattr(gui_select.SelectionPanel, "export", lambda self: called.append("export"))
    database = tmp_path / "menu.db"
    db.ensure_database(str(database)).close()
    window = gui.AppWindow(str(database), output_root=str(tmp_path / "out"))
    window.choose_action.trigger()
    window.export_action.trigger()
    assert called == ["choose", "export"]
    window.close()


def test_match_and_unassign_are_blocked_while_a_job_runs(window):
    window.evaluate_window.start()
    assert window.jobs.is_running()

    assert not window.match_action.isEnabled()
    assert not window.unassign_action.isEnabled()
    assert window.evaluate_action.isEnabled(), "窓は開ける（進み具合と前の結果を見る）"
    assert not window.evaluate_window.start_button.isEnabled()
    assert gui.MATCH_BLOCKED_BY_JOB in window.match_action.toolTip()
    _wait(window)

    assert window.match_action.isEnabled() and window.unassign_action.isEnabled()
    assert window.evaluate_window.start_button.isEnabled()


def test_a_finished_job_on_the_selection_tab_keeps_the_face_items_off(window):
    window.tabs.setCurrentWidget(window.selection)
    window.evaluate_window.start()
    _wait(window)
    assert not window.match_action.isEnabled()


def test_the_match_action_runs_match(window, monkeypatch, shown):
    called = []
    monkeypatch.setattr(gui, "MatchDialog", lambda *args, **kwargs: called.append(kwargs) or _Rejecting())
    window.match_action.trigger()
    assert called


class _Rejecting:
    def exec(self):
        return QDialog.Rejected


def test_closing_the_main_window_closes_the_other_windows(window):
    for other in (window.import_wizard, window.assess_window, window.evaluate_window):
        other.open_window()
    window.closeEvent(QCloseEvent())
    assert not any(
        other.isVisible() for other in (window.import_wizard, window.assess_window, window.evaluate_window)
    ), "残っているとアプリが終わらない"


# ---------------------------------------------------------------------------
# unassign-auto
# ---------------------------------------------------------------------------


def _accepting(backup, person_id=None):
    class _Dialog:
        offered = None

        def __init__(self, parent, counts, database_path, person=None, person_auto=0):
            _Dialog.offered = (person, person_auto)

        def chosen_person_id(self):
            return person_id

        def exec(self):
            return QDialog.Accepted

        def makes_backup(self):
            return backup

    return _Dialog


def _select_person(window, person_id):
    person_list = window.assignment.person_list
    for row in range(person_list.count()):
        person = person_list.item(row).data(gui.Qt.UserRole)
        if person and int(person["id"]) == person_id:
            person_list.setCurrentRow(row)
            return
    raise AssertionError("人物が左の一覧に無い")


def test_unassign_removes_only_automatic_assignments(window, seeded, monkeypatch, shown, tmp_path):
    _database, first, _second, faces = seeded
    connection = window.assignment.connection
    before = _rows(connection)
    monkeypatch.setattr(gui, "UnassignAutoDialog", _accepting(backup=False))

    window.unassign_action.trigger()

    rows = _rows(connection)
    assert rows[faces["イの自動"]]["assign_source"] is None
    assert rows[faces["ロの自動"]]["person_id"] is None
    assert rows[faces["顔認証外"]]["assign_source"] == db.ASSIGN_REJECTED, "顔認証外はそのまま"
    manual = [face_id for face_id, row in before.items() if row["assign_source"] == db.ASSIGN_MANUAL]
    assert len(manual) == 4
    assert all(rows[face_id]["assign_source"] == db.ASSIGN_MANUAL for face_id in manual), "手本はそのまま"
    kind, title, text = shown[-1]
    assert kind == "information" and "2 件を未分類に戻しました" in text and "控え: 取っていません" in text
    assert list(tmp_path.glob("*.bak-*")) == []
    assert db.face_counts(connection)["auto"] == 0, "左の件数も読み直せる"


def test_unassign_can_be_limited_to_the_selected_person(window, seeded, monkeypatch, shown):
    _database, first, second, faces = seeded
    _select_person(window, first)
    dialog = _accepting(backup=False, person_id=first)
    monkeypatch.setattr(gui, "UnassignAutoDialog", dialog)

    window.assignment._run_unassign_auto()

    person, person_auto = dialog.offered
    assert int(person["id"]) == first and person_auto == 1
    rows = _rows(window.assignment.connection)
    assert rows[faces["イの自動"]]["person_id"] is None
    assert rows[faces["ロの自動"]]["person_id"] == second, "ほかの人物には触らない"
    assert "人物イ" in shown[-1][2]


def test_unassign_takes_a_backup_when_chosen(window, seeded, monkeypatch, shown, tmp_path):
    monkeypatch.setattr(gui, "UnassignAutoDialog", _accepting(backup=True))

    window.assignment._run_unassign_auto()

    backups = list(tmp_path.glob("menu.db.bak-*"))
    assert len(backups) == 1
    assert str(backups[0]) in shown[-1][2]
    # 控えは取り消す前の状態
    old = sqlite3.connect(str(backups[0]))
    assert old.execute("SELECT COUNT(*) FROM Face WHERE assign_source = 'auto'").fetchone()[0] == 2
    old.close()


def test_cancelling_unassign_changes_nothing(window, monkeypatch, shown):
    monkeypatch.setattr(gui, "UnassignAutoDialog", lambda *args, **kwargs: _Rejecting())
    window.assignment._run_unassign_auto()
    assert db.face_counts(window.assignment.connection)["auto"] == 2
    assert shown == []


def test_without_automatic_assignments_it_says_so(window, shown, monkeypatch):
    db.unassign_auto(window.assignment.connection)
    opened = []
    monkeypatch.setattr(gui, "UnassignAutoDialog", lambda *args, **kwargs: opened.append(1))

    window.assignment._run_unassign_auto()

    assert not opened
    assert shown[-1][1] == "自動割当がありません"


def test_a_failed_unassign_is_reported_and_changes_nothing(window, monkeypatch, shown):
    def broken(connection, commit=True):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "recompute_family_scores", broken)
    monkeypatch.setattr(gui, "UnassignAutoDialog", _accepting(backup=False))

    window.assignment._run_unassign_auto()

    assert shown[-1][0] == "critical" and "何も変えていません" in shown[-1][2]
    assert db.face_counts(window.assignment.connection)["auto"] == 2, "途中で失敗したら残らない"


def test_the_unassign_dialog_defaults_to_cancel_and_backup(window, seeded):
    database, *_rest = seeded
    counts = db.face_counts(window.assignment.connection)
    dialog = gui.UnassignAutoDialog(window, counts, str(database))
    assert dialog.backup_box.isChecked()
    assert dialog.cancel_button.isDefault() and not dialog.run_button.isDefault()
    assert "全員の自動割当 2 件" in dialog.detail.text()
    assert "手本" in dialog.detail.text() and "顔認証外" in dialog.detail.text()
    assert dialog.chosen_person_id() is None


def test_the_unassign_dialog_cannot_run_for_a_person_without_automatic_assignments(window, seeded):
    database, first, *_rest = seeded
    counts = db.face_counts(window.assignment.connection)
    dialog = gui.UnassignAutoDialog(
        window, counts, str(database), person={"id": first, "name": "人物イ"}, person_auto=0
    )
    assert dialog.run_button.isEnabled(), "既定は全員（2件ある）"
    dialog.person_button.setChecked(True)
    assert dialog.chosen_person_id() == first
    assert "人物イ の自動割当 0 件" in dialog.detail.text()
    assert not dialog.run_button.isEnabled()


# ---------------------------------------------------------------------------
# evaluate
# ---------------------------------------------------------------------------


def test_evaluate_runs_in_the_background_and_shows_the_same_report_as_the_cli(window, seeded):
    database, *_rest = seeded
    panel = window.evaluate_window
    panel.thresholds_edit.setText("0.3, 0.45")
    panel.margin_spin.setValue(0.05)
    panel.keep_same_media_check.setChecked(True)
    before = _rows(window.assignment.connection)

    panel.start_button.click()
    assert window.jobs.is_running()
    assert window.jobs.job._process.pid != os.getpid()
    _wait(window)

    assert panel.last_final["kind"] == "done"
    connection = window.assignment.connection
    expected = evaluation.format_report(
        evaluation.evaluate_match(connection, thresholds=[0.3, 0.45], margin=0.05, keep_same_media=True)
    )
    assert panel.report.toPlainText() == expected
    assert "閾値ごとの実測" in expected and "マージン 0.05" in expected
    assert _rows(connection) == before, "DB に書かない"
    assert "精度の測定が終わりました" in window.statusBar().currentMessage()


def test_evaluate_uses_the_cli_defaults(window):
    panel = window.evaluate_window
    assert evaluation.parse_thresholds(panel.thresholds_edit.text()) == list(evaluation.DEFAULT_THRESHOLDS)
    assert panel.margin_spin.value() == pytest.approx(gui.matcher.DEFAULT_MARGIN)
    assert not panel.keep_same_media_check.isChecked()


@pytest.mark.parametrize("value", ["0.4,およそ0.5", "nan", "-1", " , "])
def test_evaluate_refuses_bad_thresholds_before_starting(window, shown, value):
    window.evaluate_window.thresholds_edit.setText(value)
    window.evaluate_window.start()
    assert not window.jobs.is_running()
    assert shown[-1][0] == "warning" and "閾値" in shown[-1][2]


def test_evaluate_without_teachers_says_so(tmp_path):
    database = tmp_path / "empty.db"
    db.ensure_database(str(database)).close()
    window = gui.AppWindow(str(database))
    window.evaluate_window.initializer = install_fake_backends
    window.evaluate_window.log_file = str(tmp_path / "logs" / "evaluate.log")
    try:
        window.evaluate_window.start()
        _wait(window)
        assert "手本になる顔がありません" in window.evaluate_window.report.toPlainText()
    finally:
        window.assignment.connection.close()
        window.deleteLater()


def test_closing_the_evaluate_window_keeps_it_running_and_the_result_stays(window):
    panel = window.evaluate_window
    panel.open_window()
    panel.start()

    panel.close()

    assert window.jobs.is_running() and not window.jobs.job.stop_requested
    _wait(window)
    panel.open_window()
    assert "閾値ごとの実測" in panel.report.toPlainText()
    panel.close()


def test_a_stopped_evaluation_shows_no_partial_numbers(window, monkeypatch):
    panel = window.evaluate_window
    # 必ず止まった知らせにする（本物の止まり方は `run_evaluate` のテストで見る）
    panel._finished("evaluate", {"kind": "stopped", "summary": {"evaluated": 3}})
    assert "途中の結果は出しません" in panel.report.toPlainText()
    assert "正解" not in panel.report.toPlainText()


def test_a_failed_evaluation_is_reported(window, shown):
    window.evaluate_window._finished("evaluate", {"kind": "failed", "message": "壊れました"})
    assert shown[-1][0] == "warning" and "壊れました" in shown[-1][2]


def test_other_jobs_do_not_touch_the_evaluate_report(window):
    panel = window.evaluate_window
    panel.report.setPlainText("前の結果")
    panel._finished("scan", {"kind": "done", "summary": {}})
    assert panel.report.toPlainText() == "前の結果"


def test_run_evaluate_stops_without_a_result(seeded):
    database, *_rest = seeded
    events = []

    with pytest.raises(jobs._Stopped) as stopped:
        jobs.run_evaluate(events.append, lambda: True, str(database), [0.45], 0.08)

    assert stopped.value.summary == {"evaluated": 0}
    assert events == [] or all(event["kind"] == "progress" for event in events)


def test_run_evaluate_reports_progress_and_the_summary(seeded):
    database, *_rest = seeded
    events = []

    summary = jobs.run_evaluate(events.append, lambda: False, str(database), [0.45], 0.08)

    assert summary["teachers"] == 4
    assert events[-1] == {"kind": "progress", "current": 4, "total": 4, "detail": "手本"}
    assert "手本 4 件" in jobs.describe_summary(summary, "evaluate")


# ---------------------------------------------------------------------------
# PR #141 のレビュー対応
# ---------------------------------------------------------------------------


def test_stopping_evaluate_does_not_promise_to_resume(window, monkeypatch):
    """指摘3: 何も保存しない処理に「済んだぶんは保存」「続きから」と言わない。"""
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda parent, title, text: asked.append(text) or QMessageBox.No)
    window.evaluate_window.start()
    assert "続きから" not in window.job_status.stop_button.toolTip()

    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted()
    assert "続きから" not in asked[0] and "最初から" in asked[0]

    statuses = []
    window.jobs.status_changed.connect(statuses.append)
    window.jobs.request_stop()
    assert "保存" not in statuses[0] and "途中の結果は出しません" in statuses[0]
    _wait(window)


def test_other_jobs_still_promise_to_resume():
    """指摘3: `scan` などの説明は変えない。"""
    from photoarchive_ai import gui_scan

    for name in ("scan", "convert_heic", "assess", "measure_faces"):
        assert "続きから" in gui_scan.stop_tooltip(name)
        assert "書きかけを保存" in gui_scan.stopping_status(name)
        assert "続きから" in gui_scan.close_question(name)


def test_the_stop_tooltip_follows_the_job_that_runs(window):
    """指摘3: `evaluate` のあとに `scan` を流しても、説明が前の処理のまま残らない。"""
    from photoarchive_ai import gui_scan

    bar = window.job_status
    assert bar.stop_button.toolTip() == gui_scan.stop_tooltip(None)
    window.evaluate_window.start()
    assert bar.stop_button.toolTip() == gui_scan.stop_tooltip("evaluate")
    _wait(window)

    class _Scan:
        name = "scan"
        stop_requested = False

    window.jobs.job = _Scan()
    bar._show("スキャン: 1 / 2 件")
    assert "続きから" in bar.stop_button.toolTip()
    window.jobs.job = None


def test_a_failed_reload_after_unassign_does_not_say_nothing_changed(window, monkeypatch, shown):
    """指摘4: 取り消しを commit したあとに一覧の読み直しだけが落ちても、消えていないと思わせない。"""
    monkeypatch.setattr(gui, "UnassignAutoDialog", _accepting(backup=False))

    def broken(*args, **kwargs):
        raise RuntimeError("読み直せません")

    monkeypatch.setattr(window.assignment, "_reload_person_list", broken)

    window.assignment._run_unassign_auto()

    kind, title, text = shown[-1]
    assert kind == "critical" and "何も変えていません" not in text
    assert "2 件は取り消してあります" in text
    assert db.face_counts(window.assignment.connection)["auto"] == 0


def test_the_bulk_button_does_not_name_only_some_jobs():
    """指摘5: `evaluate` の実行中にも押せないので、処理の名前を挙げない（`match` の文と同じ）。"""
    assert "スキャン" not in gui.BULK_BLOCKED_BY_JOB
    assert "スキャン" not in gui.MATCH_BLOCKED_BY_JOB


def test_the_settings_item_shows_its_tooltip(window):
    """指摘6: ボタンのときに出ていた説明を、メニューでも出す。"""
    assert window.settings_menu.toolTipsVisible()
    assert "DB の場所" in window.settings_action.toolTip()


def test_the_module_keeps_the_threshold_text_in_step_with_the_defaults():
    assert gui_evaluate.DEFAULT_THRESHOLD_TEXT == ",".join(
        f"{value:g}" for value in evaluation.DEFAULT_THRESHOLDS
    )
