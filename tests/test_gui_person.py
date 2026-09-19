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


# ---------------------------------------------------------------------------
# プレビューの撮影日時とフォルダ
# ---------------------------------------------------------------------------


def test_the_shooting_date_is_shown_when_the_photo_has_one():
    """EXIF の撮影日時をそのまま読める形で出す。

    **年齢はこの日付から計算する。** 出ていないと、利用者は写真を別の
    ビューアで開いて確かめるしかない。
    """
    info = photoarchive_gui.format_media_info(
        {
            "path": "/photo/2017/171216クリスマスパーティー/a.JPG",
            "shooting_date": "2017-12-16T18:46:32",
            "created_time": "2020-01-01T00:00:00",
        },
        source_root="/photo",
    )

    assert "撮影日時: 2017-12-16 18:46:32" in info
    # 撮影日時があるときに、紛らわしいファイル日時は出さない
    assert "ファイル日時" not in info


def test_a_photo_without_exif_says_so_and_falls_back_to_the_file_time():
    """撮影日時が無い写真が実データに11,090件ある。

    **ファイルの日時を撮影日時として出さない。** コピーで変わるので、
    取り違えると年齢を間違える。別の名前で、EXIF が無いときだけ出す。
    """
    info = photoarchive_gui.format_media_info(
        {
            "path": "/photo/2013/130914七五三/b.JPG",
            "shooting_date": None,
            "created_time": "2013-03-24T13:56:26",
        },
        source_root="/photo",
    )

    assert "撮影日時: 不明（EXIFなし）" in info
    assert "ファイル日時: 2013-03-24 13:56:26" in info


def test_the_folder_is_shown_relative_to_the_source_root():
    """フォルダ名は日付を持っていることが多く、EXIF が無いときの手がかり。

    絶対パスのままだと NFS のマウント先が長すぎて読めない。
    """
    info = photoarchive_gui.format_media_info(
        {"path": "/photo/2013/130914七五三/b.JPG", "shooting_date": None},
        source_root="/photo",
    )

    assert "フォルダ: 2013/130914七五三" in info
    assert "ファイル: b.JPG" in info


def test_a_photo_outside_the_source_root_keeps_its_full_path():
    """`source_root` の外のメディアでも、欠けた表示にしない。"""
    info = photoarchive_gui.format_media_info(
        {"path": "/other/place/c.JPG", "shooting_date": None}, source_root="/photo"
    )

    assert "フォルダ: /other/place" in info


def test_the_folder_is_shown_without_a_source_root():
    """設定に `source_root` が無くても動く（GUI は DB だけでも起動できる）。"""
    info = photoarchive_gui.format_media_info({"path": "/photo/2013/c.JPG"})

    assert "フォルダ: /photo/2013" in info


def test_selecting_a_face_fills_the_information_under_the_preview(window, tmp_path):
    window.source_root = str(tmp_path)
    window.face_list.setCurrentRow(0)

    window._show_preview()

    info = window.preview_info.text()
    assert "フォルダ: photos" in info
    assert "ファイル: family.jpg" in info
    assert "撮影日時" in info


def test_the_information_is_still_shown_when_the_original_is_gone(window, tmp_path):
    """元写真が開けないときこそ、どのフォルダのどのファイルかが要る。

    情報の出どころはDBなので、画像が読めなくても出せる。
    """
    window.source_root = str(tmp_path)
    (tmp_path / "photos" / "family.jpg").unlink()
    window.face_list.setCurrentRow(0)

    window._show_preview()

    assert "ファイル: family.jpg" in window.preview_info.text()
    assert "元写真を開けません" in window.preview_label.text()
