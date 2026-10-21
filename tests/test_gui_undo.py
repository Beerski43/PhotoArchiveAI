"""GUI で顔の操作を元に戻す・やり直す（#116）。戻し方そのものは `test_undo.py`。"""

import os

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from photoarchive_ai import db
from photoarchive_ai import gui as photoarchive_gui

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def _accepting_age(age):
    class _Dialog:
        def __init__(self, parent=None, summary="", initial_age=None):
            pass

        def exec(self):
            return QDialog.Accepted

        def age(self):
            return age

    return _Dialog


@pytest.fixture()
def window(tmp_path, monkeypatch):
    database = tmp_path / "gui.db"
    connection = db.ensure_database(str(database))
    media_id = db.save_media(
        connection,
        {
            "path": "/photos/a.jpg",
            "filename": "a.jpg",
            "type": "image",
            "file_hash": "hash",
            "file_size": 100,
            "created_time": "2026-01-01T00:00:00",
        },
    )
    for _ in range(3):
        db.add_face(
            connection,
            media_id=media_id,
            bbox=(0, 10, 10, 0),
            embedding=[0.0] * db.EMBEDDING_DIM,
            embed_version=db.embedding_model.ACTIVE.version,
            thumbnail=b"",
        )
    db.add_person(connection, "父")
    connection.commit()
    connection.close()
    monkeypatch.setattr(photoarchive_gui, "FaceAgeDialog", _accepting_age(5))
    window = photoarchive_gui.MainWindow(str(database))
    yield window
    window.connection.close()


def _father(window):
    return next(row for row in db.list_persons(window.connection) if row["name"] == "父")


def _assign_all(window):
    window.face_list.selectAll()
    window._assign_selected(_father(window))


def test_an_assignment_is_undone_and_the_list_comes_back(window):
    """割り当てた顔が未分類の一覧に戻り、件数も戻る。やり直すとまた割り当たる。"""
    _assign_all(window)
    assert window.face_list.count() == 0

    window.action_undo.trigger()

    assert window.face_list.count() == 3
    assert db.count_faces(window.connection, assign_source=db.ASSIGN_MANUAL) == 0
    assert "元に戻しました" in window.preview_status.text()

    window.action_redo.trigger()

    assert window.face_list.count() == 0
    assert db.count_faces(window.connection, assign_source=db.ASSIGN_MANUAL) == 3


def test_the_menu_says_what_will_be_undone(window):
    """何が戻るのかを名前で出す（「元に戻す」だけでは押すのが怖い）。"""
    menu_texts = lambda: [action.text() for action in window._build_face_menu().actions()]
    assert photoarchive_gui.ACTION_UNDO in menu_texts()
    assert window.action_undo.isEnabled() is False

    _assign_all(window)

    assert "元に戻す: 3 件を 父 に登録しました" in menu_texts()
    assert window.action_undo.isEnabled() and not window.action_redo.isEnabled()
    # 右クリックは目に見えないので、一覧の下の案内にも出す
    assert "Ctrl+Z 元に戻す" in window.hint_label.text()


def test_ctrl_z_undoes_and_plain_z_is_left_alone(window, qt_app):
    """打鍵は Ctrl+Z。**`Z` だけは「この人物ではない」の取り消しのまま**（別の操作）。"""
    assert [key.toString() for key in window.action_undo.shortcuts()] == ["Ctrl+Z"]
    assert [key.toString() for key in window.action_redo.shortcuts()] == ["Ctrl+Shift+Z", "Ctrl+Y"]
    assert window.action_undo_rejection.shortcut().toString() == "Z"
    window.show()
    qt_app.processEvents()
    _assign_all(window)
    window.face_list.setFocus()
    qt_app.processEvents()

    QTest.keyClick(window.face_list, Qt.Key_Z, Qt.ControlModifier)
    qt_app.processEvents()

    assert db.count_faces(window.connection, assign_source=db.ASSIGN_MANUAL) == 0


def test_a_conflict_is_explained_and_nothing_is_written(window, monkeypatch):
    """戻せないときは理由を出し、何も書かない（`undo.History._apply`）。"""
    shown = []
    monkeypatch.setattr(
        QMessageBox, "information", staticmethod(lambda _parent, _title, text: shown.append(text))
    )
    _assign_all(window)
    face_ids = [row["id"] for row in db.list_faces(window.connection)]
    db.assign_faces(window.connection, face_ids[:1], _father(window)["id"], db.ASSIGN_AUTO)

    window.action_undo.trigger()

    assert shown and "割り当てが変わっています" in shown[0]
    assert db.count_faces(window.connection, assign_source=db.ASSIGN_MANUAL) == 2
    assert window.action_undo.isEnabled() is False


def test_an_event_cluster_operation_is_undone_from_the_dialog_and_the_main_window(window, monkeypatch):
    """束ねる画面の操作も同じ履歴に積む。**画面の中でも、閉じたあとの親でも戻せる。**"""
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    dialog = photoarchive_gui.EventClusterDialog(
        window, window.connection, "/photos", None, history=window.history
    )
    dialog.cluster_list.setCurrentRow(0)
    dialog._reject_cluster()
    rejected = db.count_faces(window.connection, assign_source=db.ASSIGN_REJECTED)
    assert rejected > 0
    assert "済" in dialog.cluster_list.item(0).text()

    dialog.action_undo.trigger()

    assert db.count_faces(window.connection, assign_source=db.ASSIGN_REJECTED) == 0
    assert "済" not in dialog.cluster_list.item(0).text()

    dialog.action_redo.trigger()
    window._update_undo_actions()
    assert window.action_undo.text().startswith("元に戻す: まとめて顔認証外にした")

    window.action_undo.trigger()

    assert db.count_faces(window.connection, assign_source=db.ASSIGN_REJECTED) == 0


@pytest.mark.parametrize("in_dialog", [False, True])
def test_a_write_error_is_explained_and_can_be_retried(window, monkeypatch, in_dialog):
    """DB に書けなかったら知らせ、**もう一度押せば戻る**（PR #117 のレビュー指摘1）。"""
    import sqlite3

    from photoarchive_ai import undo

    warned = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda _parent, _title, text: warned.append(text))
    )
    _assign_all(window)
    real_write = undo.write

    def locked(_connection, _state):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(undo, "write", locked)
    target = window
    if in_dialog:
        target = photoarchive_gui.EventClusterDialog(
            window, window.connection, "/photos", None, history=window.history
        )
    target.action_undo.trigger()

    assert warned and "書けませんでした" in warned[0]
    assert db.count_faces(window.connection, assign_source=db.ASSIGN_MANUAL) == 3
    assert target.action_undo.isEnabled()
    if not in_dialog:
        assert window.action_undo.text() == "元に戻す: 3 件を 父 に登録しました"

    monkeypatch.setattr(undo, "write", real_write)
    target.action_undo.trigger()

    assert db.count_faces(window.connection, assign_source=db.ASSIGN_MANUAL) == 0


def test_the_penalty_step_is_named_in_the_past_tense(window, monkeypatch):
    """操作の名前は「〜しました」。「減点します」だと、戻すと減点するように読める（PR #117 のレビュー指摘2）。"""
    window.face_list.selectAll()
    window._set_penalty_selected(True)

    assert window.action_undo.text() == "元に戻す: 3 件を他人(減点)にしました"
