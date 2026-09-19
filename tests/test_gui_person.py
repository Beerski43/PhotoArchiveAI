"""GUI の人物編集とプレビューの確認。

`_edit_person` と `_show_preview` は、これまでどのテストからも呼ばれて
いなかった。どちらもモーダルやファイル読み出しを伴うが、ダイアログを
差し替え、画像を `tmp_path` に置けば通しで確かめられる。
"""

import os

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog

from photoarchive_ai import db
from photoarchive_ai import gui as photoarchive_gui
from tests.helpers import write_image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def _make_dialog_class(accepted: bool, values=("新しい名前", "mother", "新しいメモ")):
    """`PersonDialog` の代わり。開いたときの初期値を記録する。"""

    class _FakeDialog:
        opened_with = None

        def __init__(self, parent=None, name="", relation="", memo=""):
            _FakeDialog.opened_with = (name, relation, memo)

        def exec(self):
            return QDialog.Accepted if accepted else QDialog.Rejected

        def values(self):
            return values

    return _FakeDialog


@pytest.fixture()
def window(tmp_path):
    """人物1件と、実在する写真に紐づく顔1件を持つウィンドウ。"""
    photo = write_image(tmp_path / "photos" / "family.jpg", size=(200, 200))
    database = tmp_path / "gui.db"
    connection = db.ensure_database(str(database))
    db.add_person(connection, "父", "father", "元のメモ")
    media_id = db.save_media(
        connection,
        {
            "path": str(photo),
            "filename": photo.name,
            "type": "image",
            "file_hash": "hash",
            "file_size": photo.stat().st_size,
            "created_time": "2026-01-01T00:00:00",
        },
    )
    db.add_face(
        connection,
        media_id=media_id,
        bbox=(20, 120, 80, 40),
        embedding=[0.0] * 128,
        embed_version="test",
        thumbnail=b"",
    )
    connection.commit()
    connection.close()

    window = photoarchive_gui.MainWindow(str(database))
    # 表示していないウィジェットは大きさが決まらない。プレビューの縮小に
    # 必要なので、明示的に与える。
    window.preview_label.resize(320, 240)
    yield window
    window.connection.close()


# ---------------------------------------------------------------------------
# _edit_person
# ---------------------------------------------------------------------------


def test_editing_a_person_saves_the_new_values(window, monkeypatch):
    dialog = _make_dialog_class(accepted=True)
    monkeypatch.setattr(photoarchive_gui, "PersonDialog", dialog)
    window.person_list.setCurrentRow(0)

    window._edit_person()

    # 編集ダイアログは今の値で開く
    assert dialog.opened_with == ("父", "father", "元のメモ")
    person = db.list_persons(window.connection)[0]
    assert (person["name"], person["relation"], person["memo"]) == (
        "新しい名前",
        "mother",
        "新しいメモ",
    )
    # 一覧の表示も入れ替わっている
    assert "新しい名前" in window.person_list.item(0).text()


def test_cancelling_the_edit_changes_nothing(window, monkeypatch):
    monkeypatch.setattr(photoarchive_gui, "PersonDialog", _make_dialog_class(accepted=False))
    window.person_list.setCurrentRow(0)

    window._edit_person()

    assert db.list_persons(window.connection)[0]["name"] == "父"


def test_an_empty_name_is_rejected(window, monkeypatch):
    monkeypatch.setattr(
        photoarchive_gui, "PersonDialog", _make_dialog_class(accepted=True, values=("", "", ""))
    )
    window.person_list.setCurrentRow(0)

    window._edit_person()

    # 名前は必須。空で上書きされない
    assert db.list_persons(window.connection)[0]["name"] == "父"


def test_editing_without_a_selection_does_not_open_a_dialog(window, monkeypatch):
    dialog = _make_dialog_class(accepted=True)
    monkeypatch.setattr(photoarchive_gui, "PersonDialog", dialog)
    window.person_list.setCurrentRow(-1)

    window._edit_person()

    assert dialog.opened_with is None


# ---------------------------------------------------------------------------
# _show_preview
# ---------------------------------------------------------------------------


def test_selecting_a_face_previews_it_from_the_original_photo(window):
    window.face_list.setCurrentRow(0)

    window._show_preview()

    pixmap = window.preview_label.pixmap()
    assert pixmap is not None and not pixmap.isNull()
    # サムネイル(160px)ではなく元写真から取り直しているので、
    # プレビュー枠いっぱいまで使える
    assert pixmap.width() > 0 and pixmap.height() > 0
    assert window.preview_label.text() == ""


def test_the_preview_names_the_file_when_the_original_is_gone(window, tmp_path):
    # 元写真だけ消えた状態(NFS が未マウントのときに起きる)
    (tmp_path / "photos" / "family.jpg").unlink()
    window.face_list.setCurrentRow(0)

    window._show_preview()

    text = window.preview_label.text()
    assert "元写真を開けません" in text
    assert "family.jpg" in text
    assert window.preview_label.pixmap().isNull()


def test_the_preview_does_nothing_without_a_selection(window):
    window.face_list.clearSelection()
    before = window.preview_label.text()

    window._show_preview()

    assert window.preview_label.text() == before


def test_the_preview_uses_the_last_selected_face(window):
    """複数選択したときは最後に選んだ顔を出す。"""
    connection = window.connection
    media_id = db.list_faces(connection, with_thumbnail=False)[0]["media_id"]
    second = db.add_face(
        connection,
        media_id=media_id,
        bbox=(0, 40, 40, 0),
        embedding=[0.0] * 128,
        embed_version="test",
        thumbnail=b"",
    )
    connection.commit()
    window.reload_faces()
    window.face_list.selectAll()

    window._show_preview()

    assert window.face_list.selectedItems()[-1].data(Qt.UserRole)["id"] == second
    assert not window.preview_label.pixmap().isNull()
