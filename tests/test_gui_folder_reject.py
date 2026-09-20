"""フォルダで絞り、まとめて除外する（Issue #55）。

一覧の**ページをまたいで**効くこと、**手本を巻き込まない**ことが要。
"""

import os
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from photoarchive_ai import db
from photoarchive_ai import gui as photoarchive_gui

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


WEDDING = "/photos/2007/wedding"
HOME = "/photos/2007/home"


def _add_media(connection, path: str) -> int:
    return db.save_media(
        connection,
        {
            "path": path,
            "filename": Path(path).name,
            "type": "image",
            "file_hash": path,
            "file_size": 100,
            "created_time": "2007-12-22T10:00:00",
            "shooting_date": "2007-12-22T10:00:00",
        },
    )


def _add_faces(connection, media_id: int, count: int) -> list:
    return [
        db.add_face(
            connection,
            media_id=media_id,
            bbox=(0, 10, 10, 0),
            embedding=[0.0] * 128,
            embed_version="test",
            thumbnail=b"",
        )
        for _ in range(count)
    ]


def _seed(connection, wedding_faces: int = 5, home_faces: int = 2) -> dict:
    wedding = _add_media(connection, f"{WEDDING}/a.jpg")
    home = _add_media(connection, f"{HOME}/b.jpg")
    ids = {
        "wedding": _add_faces(connection, wedding, wedding_faces),
        "home": _add_faces(connection, home, home_faces),
    }
    connection.commit()
    return ids


@pytest.fixture()
def window(tmp_path):
    database = tmp_path / "folders.db"
    connection = db.ensure_database(str(database))
    seeded = _seed(connection)
    connection.close()
    window = photoarchive_gui.MainWindow(str(database))
    window.seeded = seeded
    yield window
    window.connection.close()


def _answer_yes(monkeypatch):
    monkeypatch.setattr(
        QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes
    )


def _answer_no(monkeypatch):
    monkeypatch.setattr(
        QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.No
    )


# ---------------------------------------------------------------------------
# 絞り込み
# ---------------------------------------------------------------------------


def test_choosing_a_folder_narrows_the_face_list(window):
    assert window.face_list.count() == 7

    window.folder = WEDDING
    window._reset_page()

    assert window.face_list.count() == 5
    assert "全 5 件" in window.page_label.text()
    assert "wedding" in window.folder_label.text()


def test_clearing_the_folder_brings_every_face_back(window):
    window.folder = WEDDING
    window._reset_page()
    window._clear_folder()

    assert window.folder is None
    assert window.face_list.count() == 7
    assert window.folder_label.text() == "フォルダ: すべて"


def test_bulk_button_is_disabled_until_a_folder_is_chosen(window):
    """**未選択のまま押せると、一度の押し間違いで未割当が全部飛ぶ。**

    隠さずに押せなくする（隠すと操作自体が無いと思われる）。
    """
    assert not window.bulk_folder_button.isEnabled()
    assert "フォルダを選ぶ" in window.bulk_folder_button.toolTip()

    window.folder = WEDDING
    window._reset_page()
    assert window.bulk_folder_button.isEnabled()


# ---------------------------------------------------------------------------
# まとめて除外
# ---------------------------------------------------------------------------


def test_bulk_reject_covers_the_whole_folder_not_just_the_page(tmp_path, monkeypatch):
    """**表示中のページではなくフォルダ全体に効く。**"""
    monkeypatch.setattr(photoarchive_gui, "PAGE_SIZE", 2)
    database = tmp_path / "paged.db"
    connection = db.ensure_database(str(database))
    _seed(connection, wedding_faces=5, home_faces=2)
    connection.close()

    window = photoarchive_gui.MainWindow(str(database))
    try:
        _answer_yes(monkeypatch)
        window.folder = WEDDING
        window._reset_page()
        assert window.face_list.count() == 2  # 1ページ分しか出ていない

        window._bulk_folder_action()

        assert db.count_faces(window.connection, assign_source=db.ASSIGN_REJECTED) == 5
        # 別のフォルダは触らない
        assert db.count_faces(window.connection, unassigned=True) == 2
        assert window.face_list.count() == 0
    finally:
        window.connection.close()


def test_bulk_reject_leaves_manual_faces_alone(window, monkeypatch):
    """**手本を巻き込まない。** ここが一括除外でいちばん大事な一線。"""
    _answer_yes(monkeypatch)
    person_id = db.add_person(window.connection, "なつ")
    manual = window.seeded["wedding"][0]
    db.assign_faces(window.connection, [manual], person_id, age=3)

    window.folder = WEDDING
    window._reset_page()
    window._bulk_folder_action()

    kept = db.get_face(window.connection, manual)
    assert kept["assign_source"] == db.ASSIGN_MANUAL
    assert kept["person_id"] == person_id
    assert kept["age"] == 3
    assert db.count_faces(window.connection, assign_source=db.ASSIGN_REJECTED) == 4


def test_bulk_reject_does_nothing_when_the_confirmation_is_declined(window, monkeypatch):
    _answer_no(monkeypatch)
    window.folder = WEDDING
    window._reset_page()
    window._bulk_folder_action()

    assert db.count_faces(window.connection, assign_source=db.ASSIGN_REJECTED) == 0
    assert db.count_faces(window.connection, unassigned=True) == 7


def test_bulk_reject_reports_when_the_folder_has_nothing_left(window, monkeypatch):
    """対象が0件のときは、確認も進み具合も出さずに知らせるだけ。"""
    seen = []
    monkeypatch.setattr(
        QMessageBox, "information", lambda parent, title, text: seen.append(title)
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: pytest.fail("対象が無いのに確認を出している"),
    )
    db.reject_faces(window.connection, window.seeded["wedding"])

    window.folder = WEDDING
    window._reset_page()
    window._bulk_folder_action()

    assert seen == ["対象なし"]


# ---------------------------------------------------------------------------
# まとめて取り消し
# ---------------------------------------------------------------------------


def test_bulk_undo_returns_the_whole_folder_to_unassigned(window, monkeypatch):
    """**1,357 件のフォルダを7ページ送らずに戻せること。**

    既存の「未割当に戻す」は選択した顔にしか効かず、ページをまたげない。
    """
    _answer_yes(monkeypatch)
    window.folder = WEDDING
    window._reset_page()
    window._bulk_folder_action()
    assert db.count_faces(window.connection, assign_source=db.ASSIGN_REJECTED) == 5

    window.filter_box.setCurrentText(photoarchive_gui.FILTER_REJECTED)
    assert "取り消す" in window.bulk_folder_button.text()
    window._bulk_folder_action()

    assert db.count_faces(window.connection, assign_source=db.ASSIGN_REJECTED) == 0
    assert db.count_faces(window.connection, unassigned=True) == 7


def test_bulk_button_follows_the_displayed_list(window):
    """表示を切り替えたら、まとめて処理の意味も変わる。"""
    window.folder = WEDDING
    window._reset_page()
    assert "除外" in window.bulk_folder_button.text()

    window.filter_box.setCurrentText(photoarchive_gui.FILTER_AUTO)
    assert "自動割当" in window.bulk_folder_button.text()

    window.filter_box.setCurrentText(photoarchive_gui.FILTER_REJECTED)
    assert "除外をすべて取り消す" in window.bulk_folder_button.text()


# ---------------------------------------------------------------------------
# フォルダ選択ダイアログ
# ---------------------------------------------------------------------------


def test_folder_picker_lists_folders_with_counts(window):
    dialog = photoarchive_gui.FolderPickerDialog(window, window.connection)
    try:
        assert dialog.folder_list.count() == 2
        # 未割当の多い順
        assert dialog.folder_list.item(0).text().endswith(WEDDING)
        assert "未割当 5" in dialog.folder_list.item(0).text()
        assert dialog.selected_folder() == WEDDING
    finally:
        dialog.deleteLater()


def test_folder_picker_filters_by_the_displayed_name(window):
    dialog = photoarchive_gui.FolderPickerDialog(window, window.connection)
    try:
        dialog.filter_edit.setText("home")
        assert dialog.folder_list.count() == 1
        assert dialog.selected_folder() == HOME
        assert "1 / 2 フォルダ" in dialog.summary_label.text()

        dialog.filter_edit.setText("")
        assert dialog.folder_list.count() == 2
    finally:
        dialog.deleteLater()


def test_folder_picker_shows_folders_relative_to_source_root(window):
    """**絶対パスは長すぎて読めない**（実データは `/mnt/nfs/...` から始まる）。"""
    dialog = photoarchive_gui.FolderPickerDialog(
        window, window.connection, source_root="/photos"
    )
    try:
        assert dialog.folder_list.item(0).text().endswith("2007/wedding")
        # 絞り込みも画面に出ている名前で効く
        dialog.filter_edit.setText("2007/home")
        assert dialog.folder_list.count() == 1
        # 絞り込みに使うのは絶対パスのほう
        assert dialog.selected_folder() == HOME
    finally:
        dialog.deleteLater()


def test_choosing_a_folder_from_the_dialog_applies_it(window, monkeypatch):
    class _Picker:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return QDialog.Accepted

        def selected_folder(self):
            return WEDDING

    monkeypatch.setattr(photoarchive_gui, "FolderPickerDialog", _Picker)
    window._choose_folder()

    assert window.folder == WEDDING
    assert window.face_list.count() == 5


def test_cancelling_the_dialog_keeps_the_current_folder(window, monkeypatch):
    class _Picker:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return QDialog.Rejected

        def selected_folder(self):  # pragma: no cover - 呼ばれてはいけない
            raise AssertionError("キャンセルなのに選択を読んでいる")

    monkeypatch.setattr(photoarchive_gui, "FolderPickerDialog", _Picker)
    window.folder = HOME
    window._reset_page()
    window._choose_folder()

    assert window.folder == HOME


# ---------------------------------------------------------------------------
# 表示
# ---------------------------------------------------------------------------


def test_format_folder_matches_the_preview_information():
    """**同じフォルダが画面によって違う名前で出ないこと。**"""
    media = {"path": "/photos/2007/wedding/a.jpg", "shooting_date": "2007-12-22T10:00:00"}
    info = photoarchive_gui.format_media_info(media, source_root="/photos")
    assert "フォルダ: 2007/wedding" in info
    assert photoarchive_gui.format_folder("/photos/2007/wedding", "/photos") == "2007/wedding"


def test_format_folder_keeps_paths_outside_source_root_absolute():
    assert photoarchive_gui.format_folder("/elsewhere/2007", "/photos") == "/elsewhere/2007"
    assert photoarchive_gui.format_folder("/photos", "/photos") == "（source_root 直下）"
    assert photoarchive_gui.format_folder("/photos/2007") == "/photos/2007"
