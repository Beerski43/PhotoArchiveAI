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


def _make_dialog_class(accepted: bool, values=("新しい名前", "mother", "新しいメモ", (0, 0, 0))):
    """`PersonDialog` の代わり。開いたときの初期値を記録する。"""

    class _FakeDialog:
        opened_with = None

        def __init__(self, parent=None, name="", relation="", memo="", birth_date=""):
            _FakeDialog.opened_with = (name, relation, memo, birth_date)

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
            # 撮影日時を持たせておかないと、情報欄のテストが
            # 「不明（EXIFなし）」でも通ってしまう。
            "shooting_date": "2017-12-16T18:46:32",
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
    assert dialog.opened_with == ("父", "father", "元のメモ", "")
    # 誕生日は文字列のまま渡す。ダイアログが年・月・日に割る
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
        photoarchive_gui,
        "PersonDialog",
        _make_dialog_class(accepted=True, values=("", "", "", (0, 0, 0))),
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
    assert "撮影日時: 2017-12-16 18:46:32" in info


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


# ---------------------------------------------------------------------------
# レビュー（PR #38）で見つかった経路
# ---------------------------------------------------------------------------


def test_a_broken_exif_date_is_treated_as_missing():
    """`0000:00:00` を書くカメラがある（実データで Media 55件・顔 33件）。

    そのまま出すと**撮影日時を持っているように見え、ファイル日時の
    フォールバックまで消える。** いちばん手がかりが要る写真で手がかりが減る。
    """
    info = photoarchive_gui.format_media_info(
        {
            "path": "/photo/2019/190815-17大島キャンプ/P1015698.jpg",
            "shooting_date": "0000-00-00T00:00:00",
            "created_time": "2019-08-15T12:00:00",
        },
        source_root="/photo",
    )

    assert "撮影日時: 不明（EXIFなし）" in info
    assert "ファイル日時: 2019-08-15 12:00:00" in info
    assert "0000" not in info


def test_another_shape_of_broken_exif_is_also_treated_as_missing():
    """**先頭の文字だけを見て弾かない。**

    実データには `TTTT-TT-TTTTT:TT:TT` を書くカメラもいた（Media 67件）。
    `0000` で始まるかだけを見ていたので素通りし、**画面にそのまま出ていた。**
    日付として読めるかどうかで判断する。
    """
    for broken in ("TTTT-TT-TTTTT:TT:TT", "いつか", "2019-13-01T00:00:00"):
        info = photoarchive_gui.format_media_info(
            {
                "path": "/photo/2019/a.jpg",
                "shooting_date": broken,
                "created_time": "2019-08-15T12:00:00",
            },
            source_root="/photo",
        )

        assert "撮影日時: 不明（EXIFなし）" in info, broken
        assert broken not in info
        # 手がかりとしてファイル日時に落ちること
        assert "ファイル日時: 2019-08-15 12:00:00" in info, broken


def test_a_photo_directly_under_the_source_root_says_so():
    """`フォルダ: .` では何のことか読めない。"""
    info = photoarchive_gui.format_media_info(
        {"path": "/photo/a.jpg", "shooting_date": None}, source_root="/photo"
    )

    assert "フォルダ: （source_root 直下）" in info


def test_a_relative_source_root_is_anchored_to_the_settings_file(tmp_path, monkeypatch):
    """相対の `source_root` を、**起動した場所に左右されず**に解くこと。

    cwd 起点だと、リポジトリ直下以外から起動したときに相対化が静かに外れ、
    `GUI_USAGE.md` が約束している「`source_root` からの相対」ではなく、
    読めない NFS の絶対パスに戻る。
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "app_settings.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PHOTOARCHIVE_CONFIG", str(config_dir / "app_settings.json"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    resolved = photoarchive_gui.resolve_source_root("mediaFiles/suzukiFamily")

    assert resolved == str(tmp_path / "mediaFiles/suzukiFamily")


def test_an_absolute_source_root_is_left_alone(tmp_path):
    """絶対パスの `source_root` は触らない。"""
    assert photoarchive_gui.resolve_source_root("/mnt/photo") == "/mnt/photo"
    assert photoarchive_gui.resolve_source_root(None) is None


def test_the_preview_does_not_keep_the_previous_photo_when_the_image_cannot_be_decoded(
    window, tmp_path, monkeypatch
):
    """デコードに失敗したとき、前の写真の画像を残さない。

    情報欄は先に新しい写真で上書きしているので、画像だけ残すと
    **上下で別の写真**になる。
    """
    window.face_list.setCurrentRow(0)
    window._show_preview()
    assert not window.preview_label.pixmap().isNull()

    monkeypatch.setattr(
        photoarchive_gui.face, "load_face_image_bytes", lambda path, bbox: b"not an image"
    )
    window._show_preview()

    assert window.preview_label.pixmap().isNull()
    assert "画像を表示できません" in window.preview_label.text()


def test_the_age_dialog_says_how_many_faces_get_the_same_age():
    """**1回の入力が選択中の全件に入る**ことを、入れる前に知らせる。

    プレビューに出ているのは最後に選んだ1枚の撮影日時だけなので、
    撮影年をまたいで選ぶと、画面の日時を見て入れた年齢が別の年の顔にも入る。
    """
    # `shooting_dates` は**顔1件につき1件**。件数を合わせて渡す
    spanning = photoarchive_gui.summarize_selection(
        2, ["2012-01-01T00:00:00", "2019-08-15T12:00:00"]
    )
    same_day = photoarchive_gui.summarize_selection(
        3, ["2019-08-15T12:00:00", "2019-08-15T13:00:00", "2019-08-15T14:00:00"]
    )

    assert "2 件すべてに同じ年齢を入れます" in spanning
    assert "2012-01-01 〜 2019-08-15 にまたがっています" in spanning
    assert "3 件すべてに同じ年齢を入れます" in same_day
    assert "またがって" not in same_day
    # 1件だけならプレビューと食い違わないので、何も足さない
    assert photoarchive_gui.summarize_selection(1, ["2019-08-15T12:00:00"]) == ""
    # 装飾記号を書かない（QLabel は Markdown を解釈せず、そのまま出る）
    assert "**" not in spanning


def test_the_summary_reaches_the_age_dialog(window, qt_app):
    """要約がダイアログに実際に載ること。"""
    dialog = photoarchive_gui.FaceAgeDialog(summary="2 件すべてに同じ年齢を入れます。")
    dialog.show()
    qt_app.processEvents()

    labels = [
        child.text()
        for child in dialog.findChildren(photoarchive_gui.QLabel)
    ]
    assert any("2 件すべてに同じ年齢を入れます。" in text for text in labels)
    dialog.close()


# ---------------------------------------------------------------------------
# 誕生日と撮影時の年齢（#48）
# ---------------------------------------------------------------------------


def test_the_age_is_counted_from_the_birthday_not_the_year():
    """**誕生日を迎える前なら1引く。** 年の引き算だけだと1歳ずれる。"""
    assert photoarchive_gui.calculate_age("2011-05-03", "2017-12-16T18:46:32") == 6
    # 同じ年でも誕生日の前日なら、まだ歳を取っていない
    assert photoarchive_gui.calculate_age("2011-05-03", "2018-05-02T09:00:00") == 6
    assert photoarchive_gui.calculate_age("2011-05-03", "2018-05-03T09:00:00") == 7
    # 生まれた当日は0歳
    assert photoarchive_gui.calculate_age("2011-05-03", "2011-05-03T09:00:00") == 0


def test_the_age_is_not_calculated_when_either_side_is_missing():
    """**どちらか一方でも欠けていれば計算しない**（仕様書 §8.4）。

    撮影日時は実データの 15.8% で欠けており、誕生日は登録するまで全員が未設定。
    """
    assert photoarchive_gui.calculate_age(None, "2017-12-16T18:46:32") is None
    assert photoarchive_gui.calculate_age("2011-05-03", None) is None
    assert photoarchive_gui.calculate_age("", "") is None


def test_a_broken_exif_date_does_not_produce_an_age():
    """カメラが書く `0000:00:00` を、日付として扱わない。

    `_format_timestamp` はこれを「撮影日時: 不明」にしている。ここで通すと、
    **日時が不明と出ている写真に年齢だけが出る**という食い違いになる。
    """
    assert photoarchive_gui.calculate_age("2011-05-03", "0000-00-00T00:00:00") is None
    assert photoarchive_gui.parse_date("0000-00-00T00:00:00") is None
    # 日付として読めない値も同じ扱い
    assert photoarchive_gui.parse_date("いつか") is None


def test_a_photo_taken_before_the_birthday_says_so():
    """**行を消さない。** 人物の選び間違いや日付の誤りに気づけるようにする。"""
    age = photoarchive_gui.calculate_age("2011-05-03", "2010-01-01T00:00:00")

    assert age is not None and age < 0
    assert photoarchive_gui.format_age(age) == "誕生前"
    assert photoarchive_gui.format_age(None) is None
    assert photoarchive_gui.format_age(0) == "0歳"


def test_the_preview_shows_the_age_of_the_selected_person():
    """情報欄の最後に「誰が何歳か」を出す。"""
    info = photoarchive_gui.format_media_info(
        {"path": "/photo/2017/クリスマス/a.JPG", "shooting_date": "2017-12-16T18:46:32"},
        source_root="/photo",
        person={"name": "なつ", "birth_date": "2011-05-03"},
    )

    assert info.splitlines()[-1] == "なつ: 6歳"


def test_the_preview_leaves_the_age_line_out_when_it_cannot_be_calculated():
    """人物未選択・誕生日未設定・撮影日時なしなら、**行そのものを出さない。**

    「不明」を並べるより、無いほうがよい。
    """
    photo = {"path": "/photo/2017/クリスマス/a.JPG", "shooting_date": "2017-12-16T18:46:32"}
    no_exif = {"path": "/photo/2013/七五三/b.JPG", "shooting_date": None}
    natsu = {"name": "なつ", "birth_date": "2011-05-03"}

    assert "なつ" not in photoarchive_gui.format_media_info(photo, person=None)
    assert "歳" not in photoarchive_gui.format_media_info(
        photo, person={"name": "父", "birth_date": None}
    )
    assert "歳" not in photoarchive_gui.format_media_info(no_exif, person=natsu)


def test_the_age_line_follows_the_person_selection(window, monkeypatch):
    """人物を選び直したら年齢の行が入れ替わる。**元写真は読み直さない。**"""
    connection = window.connection
    db.update_person(
        connection, db.list_persons(connection)[0]["id"], "父", "father", "", birth_date="1980-01-01"
    )
    db.add_person(connection, "なつ", "daughter", "", birth_date="2011-05-03")
    window._reload_person_list()
    window.face_list.setCurrentRow(0)
    window._show_preview()

    reads = []
    monkeypatch.setattr(
        photoarchive_gui.face,
        "load_face_image_bytes",
        lambda path, bbox: reads.append(path) or b"",
    )
    # 写真は 2017-12-16 撮影（window フィクスチャ）
    window.person_list.setCurrentRow(0)  # なつ（名前順で先頭）
    assert window.preview_info.text().splitlines()[-1] == "なつ: 6歳"

    window.person_list.setCurrentRow(1)  # 父
    assert window.preview_info.text().splitlines()[-1] == "父: 37歳"
    assert reads == []


# ---------------------------------------------------------------------------
# 誕生日の登録
# ---------------------------------------------------------------------------


def test_a_birth_date_can_be_registered_and_cleared(window, monkeypatch):
    monkeypatch.setattr(
        photoarchive_gui,
        "PersonDialog",
        _make_dialog_class(accepted=True, values=("父", "father", "", (1980, 1, 2))),
    )
    window.person_list.setCurrentRow(0)
    window._edit_person()

    assert db.list_persons(window.connection)[0]["birth_date"] == "1980-01-02"

    # 3つとも空なら「未設定へ戻す」
    monkeypatch.setattr(
        photoarchive_gui,
        "PersonDialog",
        _make_dialog_class(accepted=True, values=("父", "father", "", (0, 0, 0))),
    )
    window.person_list.setCurrentRow(0)
    window._edit_person()

    assert db.list_persons(window.connection)[0]["birth_date"] is None


def test_a_partly_filled_birth_date_is_rejected(window, monkeypatch):
    """**年月日まで必須**（Issue #48 の判断3）。月日の分からない誕生日から
    年齢は出せないので、中途半端に持たない。"""
    warned = []
    monkeypatch.setattr(
        photoarchive_gui.QMessageBox, "warning", lambda *args: warned.append(args[2])
    )
    for parts in ((1980, 0, 0), (1980, 1, 0), (0, 1, 2), (1980, 2, 30)):
        monkeypatch.setattr(
            photoarchive_gui,
            "PersonDialog",
            _make_dialog_class(accepted=True, values=("父", "father", "", parts)),
        )
        window.person_list.setCurrentRow(0)
        window._edit_person()

    assert len(warned) == 4
    # 一部だけ入っている場合と、暦に無い日とで、言うことを変える
    assert "すべて入れてください" in warned[0]
    assert "存在しない日付" in warned[3]
    # 何も保存されていない（名前も誕生日も元のまま）
    person = db.list_persons(window.connection)[0]
    assert person["birth_date"] is None and person["name"] == "父"


def test_the_birth_date_is_built_from_three_numbers():
    """年・月・日を別々に受け取る。**区切り文字を間違えようがない。**"""
    assert photoarchive_gui.build_birth_date(2011, 5, 3) == "2011-05-03"
    # 3つとも未入力なら未設定
    assert photoarchive_gui.build_birth_date(0, 0, 0) is None
    # 分解も同じ形に戻る
    assert photoarchive_gui.split_birth_date("2011-05-03") == (2011, 5, 3)
    assert photoarchive_gui.split_birth_date(None) == (0, 0, 0)
    assert photoarchive_gui.split_birth_date("0000-00-00") == (0, 0, 0)


def test_the_edit_dialog_opens_with_the_stored_birth_date(window, monkeypatch):
    db.update_person(
        window.connection,
        db.list_persons(window.connection)[0]["id"],
        "父",
        "father",
        "元のメモ",
        birth_date="1980-01-02",
    )
    window._reload_person_list()
    dialog = _make_dialog_class(accepted=False)
    monkeypatch.setattr(photoarchive_gui, "PersonDialog", dialog)
    window.person_list.setCurrentRow(0)

    window._edit_person()

    assert dialog.opened_with == ("父", "father", "元のメモ", "1980-01-02")


def test_the_person_dialog_round_trips_a_birth_date(qt_app):
    """実物のダイアログが誕生日を持ち帰ること（フェイクだけでは確かめられない）。

    **保存済みの誕生日は、年・月・日の欄に割って表示する。**
    """
    dialog = photoarchive_gui.PersonDialog(
        name="なつ", relation="daughter", memo="メモ", birth_date="2011-05-03"
    )

    assert (dialog.birth_year.value(), dialog.birth_month.value()) == (2011, 5)
    assert dialog.birth_day.value() == 3
    assert dialog.values() == ("なつ", "daughter", "メモ", (2011, 5, 3))

    # 未設定の人物は3つとも空で開く
    empty = photoarchive_gui.PersonDialog(name="父")
    assert empty.values()[3] == (0, 0, 0)
    dialog.close()
    empty.close()


def test_typing_a_birth_date_straight_from_the_keyboard(qt_app):
    """「--」の文字が入った欄でも、打鍵で置き換わること。

    年齢の入力と同じ作り。全選択しておかないと ▲ を押すしかなくなる。
    """
    from PySide6.QtTest import QTest

    dialog = photoarchive_gui.PersonDialog(name="なつ")
    dialog.show()
    qt_app.processEvents()

    dialog.birth_year.setFocus()
    QTest.keyClicks(dialog.birth_year, "2011")
    dialog.birth_month.setFocus()
    QTest.keyClicks(dialog.birth_month, "5")

    assert dialog.values()[3][:2] == (2011, 5)
    dialog.close()


def test_the_person_details_show_the_birth_date(window):
    """登録したことが画面から見えないと、年齢が出ない理由が分からない。"""
    window.person_list.setCurrentRow(0)
    assert "誕生日: 未設定" in window.details_label.text()

    db.update_person(
        window.connection,
        db.list_persons(window.connection)[0]["id"],
        "父",
        "father",
        "",
        birth_date="1980-01-02",
    )
    window._reload_person_list()
    window.person_list.setCurrentRow(0)

    assert "誕生日: 1980-01-02" in window.details_label.text()


# ---------------------------------------------------------------------------
# 年齢ダイアログの初期値
# ---------------------------------------------------------------------------


def test_the_suggested_age_needs_every_selected_face_to_agree():
    """**1回の入力が選択中の全件に入る。** 食い違うなら初期値を出さない。"""
    # 同じ年に撮られた顔だけなら、その年齢
    assert photoarchive_gui.suggested_age(
        "2011-05-03", ["2017-12-16T00:00:00", "2017-12-20T00:00:00"]
    ) == 6
    # 年をまたいで選んでいる。片方を初期値にすると黙って間違いが入る
    assert (
        photoarchive_gui.suggested_age(
            "2011-05-03", ["2012-01-01T00:00:00", "2019-08-15T00:00:00"]
        )
        is None
    )
    # 撮影日時が1件も無い / 誕生日が未設定
    assert photoarchive_gui.suggested_age("2011-05-03", []) is None
    assert photoarchive_gui.suggested_age(None, ["2017-12-16T00:00:00"]) is None
    # 誕生前は初期値にならない（負の値は「未設定」の席）
    assert photoarchive_gui.suggested_age("2011-05-03", ["2010-01-01T00:00:00"]) is None


def test_the_age_dialog_opens_with_the_calculated_age(qt_app):
    """計算値を初期値に入れる。**機械が入れた値だと分かるようにする。**"""
    dialog = photoarchive_gui.FaceAgeDialog(initial_age=6)
    dialog.show()
    qt_app.processEvents()

    assert dialog.age() == 6
    labels = [child.text() for child in dialog.findChildren(photoarchive_gui.QLabel)]
    assert any("誕生日から計算した年齢" in text for text in labels)
    dialog.close()

    # 計算できなければ、これまで通り「未設定」で開く
    unset = photoarchive_gui.FaceAgeDialog()
    assert unset.age() is None
    labels = [child.text() for child in unset.findChildren(photoarchive_gui.QLabel)]
    assert not any("誕生日から計算した年齢" in text for text in labels)
    unset.close()


def test_assigning_faces_offers_the_calculated_age_without_saving_it(window, monkeypatch):
    """**自動保存はしない**（Issue #48 の判断2）。取り消せば何も入らない。"""
    opened = {}

    class _FakeAgeDialog:
        def __init__(self, parent=None, summary="", initial_age=None):
            opened["summary"] = summary
            opened["initial_age"] = initial_age

        def exec(self):
            return QDialog.Rejected

        def age(self):
            return opened["initial_age"]

    monkeypatch.setattr(photoarchive_gui, "FaceAgeDialog", _FakeAgeDialog)
    db.update_person(
        window.connection,
        db.list_persons(window.connection)[0]["id"],
        "父",
        "father",
        "",
        birth_date="1980-01-02",
    )
    window._reload_person_list()
    window.person_list.setCurrentRow(0)
    window.face_list.setCurrentRow(0)

    window._assign_selected()

    # 写真は 2017-12-16 撮影
    assert opened["initial_age"] == 37
    # 取り消したので、顔は未割当のまま
    assert db.list_faces(window.connection, with_thumbnail=False)[0]["person_id"] is None


def test_assigning_several_faces_warns_that_one_age_covers_them_all(window, monkeypatch):
    """#41 で入れた知らせが、**まとめて選ぶことがいちばん多い場所**にも出る。"""
    connection = window.connection
    older = db.save_media(
        connection,
        {
            "path": "/photo/2012/a.JPG",
            "filename": "a.JPG",
            "type": "image",
            "file_hash": "hash2",
            "file_size": 1,
            "created_time": "2012-01-01T00:00:00",
            "shooting_date": "2012-01-01T00:00:00",
        },
    )
    db.add_face(
        connection,
        media_id=older,
        bbox=(0, 40, 40, 0),
        embedding=[0.0] * 128,
        embed_version="test",
        thumbnail=b"",
    )
    connection.commit()
    window.reload_faces()

    opened = {}

    class _FakeAgeDialog:
        def __init__(self, parent=None, summary="", initial_age=None):
            opened["summary"] = summary
            opened["initial_age"] = initial_age

        def exec(self):
            return QDialog.Rejected

        def age(self):
            return None

    monkeypatch.setattr(photoarchive_gui, "FaceAgeDialog", _FakeAgeDialog)
    db.update_person(
        connection,
        db.list_persons(connection)[0]["id"],
        "父",
        "father",
        "",
        birth_date="1980-01-02",
    )
    window._reload_person_list()
    window.person_list.setCurrentRow(0)
    window.face_list.selectAll()

    window._assign_selected()

    assert "2 件すべてに同じ年齢を入れます" in opened["summary"]
    assert "2012-01-01 〜 2017-12-16 にまたがっています" in opened["summary"]
    # 年をまたいでいるので、初期値は出さない
    assert opened["initial_age"] is None


# ---------------------------------------------------------------------------
# レビュー対応（PR #51）
# ---------------------------------------------------------------------------


def test_the_suggested_age_is_withheld_when_a_face_has_no_shooting_date():
    """**撮影日時の分からない顔が1件でもあれば、初期値を出さない。**

    以前は `ages.discard(None)` で「分からない」を捨てていたので、10件のうち
    9件が EXIF 無しでも、残る1件の年齢が10件すべての初期値になった。
    **その顔には、別の写真から計算した年齢が黙って保存される**（実データでは
    `Media.shooting_date` が 15.8% 欠けている）。
    """
    # 読める1件だけなら出る
    assert photoarchive_gui.suggested_age("2011-05-03", ["2017-12-16T18:46:32"]) == 6
    # 撮影日時の無い顔が混ざったら出さない
    assert (
        photoarchive_gui.suggested_age("2011-05-03", ["2017-12-16T18:46:32", None]) is None
    ), "撮影日時の無い顔があるのに初期値を出している"


def test_the_suggested_age_is_withheld_when_a_shooting_date_is_broken():
    """壊れた EXIF も「分からない」として扱う。

    `0000-00-00` は `parse_date` が弾くと決めた値（実データで Media 55件）。
    年齢が計算できない以上、日時の無い顔と同じ扱いにする。
    """
    assert (
        photoarchive_gui.suggested_age(
            "2011-05-03", ["2017-12-16T18:46:32", "0000-00-00T00:00:00"]
        )
        is None
    )


def test_the_selection_notice_says_how_many_dates_are_unknown():
    """**初期値が出ない理由を伝える。** 黙っていると「なぜ空欄か」が分からない。"""
    notice = photoarchive_gui.summarize_selection(
        3, ["2017-12-16T18:46:32", None, "0000-00-00T00:00:00"]
    )

    assert "3 件すべてに同じ年齢を入れます" in notice
    assert "うち 2 件は撮影日時が分かりません" in notice


def test_a_broken_exif_date_does_not_appear_in_the_selection_notice():
    """**`0000:00:00` を撮影日時として画面に出さない。**

    `_format_timestamp` が「撮影日時: 不明」と出している写真が、同じ画面で
    日付を持っているように見えてしまう。
    """
    notice = photoarchive_gui.summarize_selection(
        2, ["0000-00-00T00:00:00", "2017-12-16T18:46:32"]
    )

    assert "0000" not in notice
    # 読める1件だけが範囲になる
    assert "2017-12-16" in notice
    assert "にまたがっています" not in notice


def test_the_age_line_appears_right_after_the_birth_date_is_registered(window, monkeypatch):
    """**誕生日を登録したら、その場で年齢の行が出る。**

    `_reload_person_list` の `clear()` で選択が外れ、`_on_person_selected(None)` が
    early return していたため、**人物を選び直すまで出なかった。** この機能を
    初めて使う人には、効いていないように見える。
    """
    window.person_list.setCurrentRow(0)
    window.face_list.setCurrentRow(0)
    window._show_preview()
    assert "歳" not in window.preview_info.text()

    monkeypatch.setattr(
        photoarchive_gui,
        "PersonDialog",
        _make_dialog_class(accepted=True, values=("父", "father", "", (1980, 1, 2))),
    )
    window._edit_person()

    # 写真は 2017-12-16 撮影
    assert window.preview_info.text().splitlines()[-1] == "父: 37歳"
    # 詳細欄も「人物を選択してください。」に戻らない
    assert "誕生日: 1980-01-02" in window.details_label.text()
    assert window._current_person()["name"] == "父"


def test_dropping_the_person_selection_also_drops_the_age_line(window):
    """**前の人物の年齢を残さない。** 誰の年齢なのか分からなくなる。"""
    db.update_person(
        window.connection,
        db.list_persons(window.connection)[0]["id"],
        "父",
        "father",
        "",
        birth_date="1980-01-02",
    )
    window._reload_person_list()
    window.person_list.setCurrentRow(0)
    window.face_list.setCurrentRow(0)
    window._show_preview()
    assert "父: 37歳" in window.preview_info.text()

    window.person_list.setCurrentRow(-1)

    assert "歳" not in window.preview_info.text()


def test_a_new_person_is_selected_so_the_age_shows_immediately(window, monkeypatch):
    """追加した人物も選ばれた状態になる（編集と同じ理由）。"""
    window.face_list.setCurrentRow(0)
    window._show_preview()
    monkeypatch.setattr(
        photoarchive_gui,
        "PersonDialog",
        _make_dialog_class(accepted=True, values=("なつ", "daughter", "", (2011, 5, 3))),
    )

    window._add_person()

    assert window._current_person()["name"] == "なつ"
    assert window.preview_info.text().splitlines()[-1] == "なつ: 6歳"
