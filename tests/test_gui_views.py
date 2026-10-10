"""左の一覧を「見るもの」にし、顔の操作を右クリックに寄せた画面（#67）。

**この画面の作りは「1件あたりの手数を減らす」ためにある。** 手作業の量が
この製品の精度の上限で、他人の顔の 99.1% が家族の写真に混ざっていて
まとめて消せない（2026-10-08 実測）。ここのテストは、その手数が増える方向に
戻っていないかを見張る。
"""

import os

import pytest
from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from photoarchive_ai import db
from photoarchive_ai import gui as photoarchive_gui

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def _accepting_age_dialog(age=None):
    """年齢を入れて OK を押す `FaceAgeDialog` の代わり。"""

    class _Dialog:
        def __init__(self, parent=None, summary="", initial_age=None):
            pass

        def exec(self):
            return QDialog.Accepted

        def age(self):
            return age

    return _Dialog


def _add_face(connection, media_id, quality=0.0):
    return db.add_face(
        connection,
        media_id=media_id,
        bbox=(0, 10, 10, 0),
        embedding=[0.0] * db.EMBEDDING_DIM,
        embed_version=db.embedding_model.ACTIVE.version,
        thumbnail=b"",
        quality_score=quality,
    )


def _media(connection, path, shooting_date, file_hash):
    return db.save_media(
        connection,
        {
            "path": path,
            "filename": path.rsplit("/", 1)[-1],
            "type": "image",
            "file_hash": file_hash,
            "file_size": 100,
            "created_time": "2026-01-01T00:00:00",
            "shooting_date": shooting_date,
        },
    )


@pytest.fixture()
def seeded(tmp_path):
    """2人の人物と、未割当・手本・自動・除外の顔がそろったDB。"""
    database = tmp_path / "views.db"
    connection = db.ensure_database(str(database))
    old_photo = _media(connection, "/photos/2009/a.jpg", "2009-05-05T10:00:00", "h1")
    new_photo = _media(connection, "/photos/2018/b.jpg", "2018-07-07T10:00:00", "h2")
    faces = {
        "未割当・古い": _add_face(connection, old_photo, 10.0),
        "未割当・新しい": _add_face(connection, new_photo, 20.0),
        "手本": _add_face(connection, new_photo, 30.0),
        "自動・弱い": _add_face(connection, new_photo, 40.0),
        "自動・強い": _add_face(connection, new_photo, 50.0),
        "除外": _add_face(connection, new_photo, 60.0),
    }
    person4 = db.add_person(connection, "${PERSON_4}", "長女", birth_date="2011-05-03")
    person3 = db.add_person(connection, "${PERSON_3}", "長男", birth_date="2007-01-09")
    db.assign_faces(connection, [faces["手本"]], person4, db.ASSIGN_MANUAL)
    db.apply_auto_assignments(
        connection,
        [(faces["自動・弱い"], person4, 20.0), (faces["自動・強い"], person3, 90.0)],
    )
    db.reject_faces(connection, [faces["除外"]])
    connection.commit()
    connection.close()

    window = photoarchive_gui.MainWindow(str(database))
    yield window, faces, {"${PERSON_4}": person4, "${PERSON_3}": person3}
    window.connection.close()


def _rows(window):
    return [
        (
            window.person_list.item(row).data(photoarchive_gui.SCOPE_ROLE),
            window.person_list.item(row).text(),
        )
        for row in range(window.person_list.count())
    ]


def _select(window, scope=None, person_id=None):
    for row in range(window.person_list.count()):
        item = window.person_list.item(row)
        person = item.data(Qt.UserRole)
        if person_id is not None and person is not None and person["id"] == person_id:
            window.person_list.setCurrentRow(row)
            return
        if person_id is None and item.data(photoarchive_gui.SCOPE_ROLE) == scope:
            window.person_list.setCurrentRow(row)
            return
    raise AssertionError("左の一覧に見つからない")


def _labels(window):
    return [window.face_list.item(i).text() for i in range(window.face_list.count())]


def _ids(window):
    return [
        window.face_list.item(i).data(Qt.UserRole)["id"]
        for i in range(window.face_list.count())
    ]


def _menu_texts(window):
    return [action.text() for action in window._menu_actions()]


# ---------------------------------------------------------------------------
# 左の一覧 = 見るもの
# ---------------------------------------------------------------------------


def test_the_left_list_puts_the_views_above_the_persons(seeded):
    """**表示と人物が同じ一覧に並ぶ。** 表示の選択は左へ畳んだ（combo を廃止）。

    同じことを2か所で選ばせると、どちらが効いているのか分からなくなる。
    """
    window, _faces, _persons = seeded

    rows = _rows(window)

    assert [scope for scope, _ in rows[:3]] == [
        photoarchive_gui.SCOPE_UNASSIGNED,
        photoarchive_gui.SCOPE_AUTO,
        photoarchive_gui.SCOPE_REJECTED,
    ]
    assert rows[3][0] is None, "4行目は区切り線"
    assert not (
        window.person_list.item(3).flags() & Qt.ItemFlag.ItemIsSelectable
    ), "区切り線は選べない"
    assert [scope for scope, _ in rows[4:]] == [photoarchive_gui.SCOPE_PERSON] * 2
    assert not hasattr(window, "filter_box"), "表示の combo は残さない"


def test_the_left_list_shows_how_much_work_is_left(seeded):
    """**残りの件数を出す。** 減っていくのが見えること自体が作業の支えになる。"""
    window, faces, persons = seeded

    rows = dict(_rows(window))
    assert rows[photoarchive_gui.SCOPE_UNASSIGNED].endswith("2")
    assert rows[photoarchive_gui.SCOPE_AUTO].endswith("2")
    assert rows[photoarchive_gui.SCOPE_REJECTED].endswith("1")
    person4 = window.person_list.item(4).text()
    assert "手本 1" in person4 and "自動 1" in person4

    # 操作したら、その場で数え直す。
    _select(window, scope=photoarchive_gui.SCOPE_UNASSIGNED)
    window.face_list.selectAll()
    window._reject_selected()

    assert dict(_rows(window))[photoarchive_gui.SCOPE_UNASSIGNED].endswith("0")
    assert dict(_rows(window))[photoarchive_gui.SCOPE_REJECTED].endswith("3")


def test_selecting_a_person_shows_the_faces_assigned_to_them(seeded):
    """**これが旧「割り当て済みを確認」。** 別ウィンドウを開かずに同じことをする。"""
    window, faces, persons = seeded

    _select(window, person_id=persons["${PERSON_4}"])

    assert sorted(_ids(window)) == sorted([faces["手本"], faces["自動・弱い"]])
    assert "名前: ${PERSON_4}" in window.details_label.text()
    assert "手本: 1 件" in window.details_label.text()


def test_the_person_filters_only_appear_for_a_person(seeded):
    """年齢の絞り込みは**人物の誕生日が無いと計算できない。**

    全体の表示では意味を持たないので出さない（`Face.age` は実データで
    割り当て済み 22,511 件のうち 126 件しか入っていない）。
    """
    window, _faces, persons = seeded

    _select(window, scope=photoarchive_gui.SCOPE_UNASSIGNED)
    assert window.person_filter_row.isVisibleTo(window) is False

    _select(window, person_id=persons["${PERSON_4}"])
    assert window.person_filter_row.isVisibleTo(window) is True


def test_each_view_starts_with_the_order_that_suits_it(seeded):
    """**並びはその表示で何をするかで決まる。**

    未割当は撮影日時順（同じ行事が固まる）、自動割当は確信度の低い順
    （誤りに早く当たる）、人物は年齢順（成長の順に並ぶ）。
    """
    window, faces, persons = seeded

    _select(window, scope=photoarchive_gui.SCOPE_UNASSIGNED)
    assert window._current_order() == db.ORDER_SHOT_DESC
    assert _ids(window) == [faces["未割当・新しい"], faces["未割当・古い"]]

    _select(window, scope=photoarchive_gui.SCOPE_AUTO)
    assert window._current_order() == db.ORDER_SCORE_ASC
    assert _ids(window) == [faces["自動・弱い"], faces["自動・強い"]]

    _select(window, person_id=persons["${PERSON_4}"])
    assert window._current_order() == db.ORDER_AGE

    # 同じ表示を見ているあいだは、選んだ並びを勝手に戻さない。
    window.order_box.setCurrentIndex(
        next(i for i, (_, value) in enumerate(photoarchive_gui.ORDER_CHOICES)
             if value == db.ORDER_QUALITY)
    )
    window._reset_page()
    assert window._current_order() == db.ORDER_QUALITY


def test_dragging_a_person_above_the_views_does_not_move_them(seeded):
    """**表示の3行はドラッグで動かない。** 人物の並び順だけを保存する。"""
    window, _faces, persons = seeded

    # 2人目（${PERSON_3}）を一覧の先頭へ落としたのと同じこと。
    moved = window.person_list.model().moveRow(QModelIndex(), 5, QModelIndex(), 0)
    assert moved

    rows = _rows(window)
    assert [scope for scope, _ in rows[:3]] == [
        photoarchive_gui.SCOPE_UNASSIGNED,
        photoarchive_gui.SCOPE_AUTO,
        photoarchive_gui.SCOPE_REJECTED,
    ], "表示は必ず先頭へ戻る"
    # **表示の行は掴めない。人物の行は掴める。**
    assert not (window.person_list.item(0).flags() & Qt.ItemFlag.ItemIsDragEnabled)
    assert not (window.person_list.item(0).flags() & Qt.ItemFlag.ItemIsDropEnabled)
    assert window.person_list.item(4).flags() & Qt.ItemFlag.ItemIsDragEnabled
    # 区切り線は選べない・掴めない・落とせない
    assert window.person_list.item(3).flags() == Qt.ItemFlag.NoItemFlags
    # 人物の並びは入れ替わっている（${PERSON_3}が先頭）。
    assert [person["name"] for person in db.list_persons(window.connection)] == [
        "${PERSON_3}",
        "${PERSON_4}",
    ]


# ---------------------------------------------------------------------------
# 右クリックのメニュー
# ---------------------------------------------------------------------------


def test_the_menu_only_offers_what_the_view_can_do(seeded):
    """**関係のない操作を出さない。** ボタンで並べると増え続けた。"""
    window, _faces, persons = seeded

    _select(window, scope=photoarchive_gui.SCOPE_UNASSIGNED)
    assert _menu_texts(window) == [photoarchive_gui.ACTION_REJECT]

    _select(window, scope=photoarchive_gui.SCOPE_REJECTED)
    assert _menu_texts(window) == [photoarchive_gui.ACTION_UNASSIGN]

    _select(window, scope=photoarchive_gui.SCOPE_AUTO)
    assert _menu_texts(window) == [
        photoarchive_gui.ACTION_CONFIRM,
        photoarchive_gui.ACTION_UNASSIGN,
        photoarchive_gui.ACTION_NOT_THIS_PERSON,
        photoarchive_gui.ACTION_REJECT,
    ]

    _select(window, person_id=persons["${PERSON_4}"])
    assert _menu_texts(window) == [
        photoarchive_gui.ACTION_CONFIRM,
        photoarchive_gui.ACTION_DETACH,
        photoarchive_gui.ACTION_NOT_THIS_PERSON,
        photoarchive_gui.ACTION_REJECT,
        photoarchive_gui.ACTION_SET_AGE,
    ]

    # 「この人物ではない」の一覧は別物。取り消すことしかできない。
    window.source_box.setCurrentIndex(
        window.source_box.findText(photoarchive_gui.NOT_THIS_PERSON_FILTER)
    )
    assert _menu_texts(window) == [photoarchive_gui.ACTION_UNDO_REJECTION]


def test_the_menu_lists_every_person_as_an_assign_target(seeded):
    """**割り当て先はメニューが持つ。** 左の選択は「見るもの」なので、
    人物を選び直す往復が要らない。
    """
    window, _faces, _persons = seeded

    _select(window, scope=photoarchive_gui.SCOPE_UNASSIGNED)
    window.face_list.selectAll()
    menu = window._build_face_menu()
    submenu = next(action.menu() for action in menu.actions() if action.menu())

    assert [action.data()["name"] for action in submenu.actions()] == [
        "${PERSON_4}",
        "${PERSON_3}",
    ]
    assert menu.actions()[0].text() == photoarchive_gui.ASSIGN_MENU


def test_the_assign_menu_shows_the_age_and_warns_about_photos_before_birth(seeded):
    """**撮影時の年齢を添える。** 誰の顔かを決めるのに、いちばん効く手がかり。

    **誕生前の写真には印を付ける。** `match` は誕生日で候補を外すので、
    この矛盾を作れるのは手作業だけ（実データで誤一致の原因になっていた）。
    """
    window, faces, _persons = seeded

    _select(window, scope=photoarchive_gui.SCOPE_UNASSIGNED)
    # 2018-07-07 の写真を選ぶ → ${PERSON_4}7歳・${PERSON_3}11歳
    window.face_list.clearSelection()
    for row in range(window.face_list.count()):
        if window.face_list.item(row).data(Qt.UserRole)["id"] == faces["未割当・新しい"]:
            window.face_list.item(row).setSelected(True)
    labels = [action.text() for action in window.assign_actions]
    assert "（7歳）" in labels[0], labels
    assert "（11歳）" in labels[1], labels
    assert "⚠" not in " ".join(labels)

    # 2009-05-05 の写真は、${PERSON_4}が生まれる前。
    window.face_list.clearSelection()
    for row in range(window.face_list.count()):
        if window.face_list.item(row).data(Qt.UserRole)["id"] == faces["未割当・古い"]:
            window.face_list.item(row).setSelected(True)
    labels = [action.text() for action in window.assign_actions]
    assert "⚠誕生前の写真あり" in labels[0], labels
    assert "⚠" not in labels[1], "${PERSON_3}は生まれている"


def test_a_face_can_be_assigned_from_the_menu_without_selecting_the_person(
    seeded, monkeypatch
):
    """**人物を選び直さずに割り当てられる。**

    以前は左で選択中の人物へ割り当てていたので、未割当の作業中に選択が
    外れていると「先に人物を選択してください」で止まっていた。
    """
    window, faces, persons = seeded
    monkeypatch.setattr(photoarchive_gui, "FaceAgeDialog", _accepting_age_dialog(7))

    _select(window, scope=photoarchive_gui.SCOPE_UNASSIGNED)
    assert window._current_person() is None
    window.face_list.selectAll()
    target = next(
        action for action in window.assign_actions if action.data()["name"] == "${PERSON_3}"
    )
    target.trigger()

    rows = {row["id"]: row for row in db.list_faces(window.connection)}
    for face_id in (faces["未割当・古い"], faces["未割当・新しい"]):
        assert rows[face_id]["person_id"] == persons["${PERSON_3}"]
        assert rows[face_id]["assign_source"] == db.ASSIGN_MANUAL
        assert rows[face_id]["age"] == 7
    assert "完了" in window.preview_status.text()


def test_the_digit_keys_assign_to_the_persons_in_order(seeded, monkeypatch, qt_app):
    """**打鍵で割り当てられること。** 1件あたりの手数がそのまま総時間。

    並び順は左の一覧のまま。よく割り当てる人物を上へ動かせば、押す数字も前に来る。
    **顔の一覧に焦点があるときだけ効く**（年齢の入力欄で数字を打ったときに
    割り当てが走っては困る）。
    """
    window, faces, persons = seeded
    monkeypatch.setattr(photoarchive_gui, "FaceAgeDialog", _accepting_age_dialog(7))
    window.show()
    qt_app.processEvents()

    _select(window, scope=photoarchive_gui.SCOPE_UNASSIGNED)
    window.face_list.setFocus()
    window.face_list.selectAll()
    qt_app.processEvents()

    QTest.keyClick(window.face_list, Qt.Key_2)
    qt_app.processEvents()

    rows = {row["id"]: row for row in db.list_faces(window.connection)}
    assert rows[faces["未割当・古い"]]["person_id"] == persons["${PERSON_3}"], (
        "2 は左の一覧の2人目（${PERSON_3}）"
    )
    assert [action.shortcut().toString() for action in window.assign_actions] == [
        "1",
        "2",
    ]
    window.hide()


def test_right_clicking_an_unselected_face_selects_it_first(seeded):
    """**右クリックした顔を処理すること。** 選択と違う顔に効いては困る。"""
    window, faces, _persons = seeded

    _select(window, scope=photoarchive_gui.SCOPE_UNASSIGNED)
    window.face_list.item(0).setSelected(True)
    target = window.face_list.item(1)

    window._select_under_cursor(window.face_list.visualItemRect(target).center())

    assert window._selected_face_ids() == [target.data(Qt.UserRole)["id"]]

    # すでに選んでいる顔を右クリックしたら、選択を崩さない。
    window.face_list.selectAll()
    window._select_under_cursor(window.face_list.visualItemRect(target).center())
    assert len(window._selected_face_ids()) == window.face_list.count()


def test_the_hint_line_names_the_keys_that_work_here(seeded):
    """**右クリックは目に見えない。** 何ができるかを1行で出す。"""
    window, _faces, persons = seeded

    _select(window, scope=photoarchive_gui.SCOPE_UNASSIGNED)
    window.face_list.selectAll()

    hint = window.hint_label.text()
    assert "右クリック" in hint
    assert "2 件選択中" in hint
    assert "1〜2 人物に割り当て" in hint
    assert f"X {photoarchive_gui.ACTION_REJECT}" in hint
    # その表示で効かない打鍵は出さない。
    assert photoarchive_gui.ACTION_SET_AGE not in hint


# ---------------------------------------------------------------------------
# 全員ぶんの表示（自動割当）
# ---------------------------------------------------------------------------


def test_the_auto_view_names_the_person_on_each_face(seeded):
    """**誰に付いた顔かが分からないと見直せない。** 名前と撮影時の年齢を出す。"""
    window, _faces, _persons = seeded

    _select(window, scope=photoarchive_gui.SCOPE_AUTO)

    labels = _labels(window)
    assert any("${PERSON_4} (7歳)" in label for label in labels), labels
    assert any("${PERSON_3} (11歳)" in label for label in labels), labels


def test_confirming_in_the_auto_view_keeps_each_face_with_its_own_person(seeded):
    """**全員ぶんをまとめて確定できること。** 別々の人物に付いていても、
    その顔に付いている人物へ確定する。
    """
    window, faces, persons = seeded

    _select(window, scope=photoarchive_gui.SCOPE_AUTO)
    window.face_list.selectAll()
    assert window.action_confirm.isEnabled()

    window._confirm_selected()

    rows = {row["id"]: row for row in db.list_faces(window.connection)}
    assert rows[faces["自動・弱い"]]["person_id"] == persons["${PERSON_4}"]
    assert rows[faces["自動・強い"]]["person_id"] == persons["${PERSON_3}"]
    assert {
        rows[faces["自動・弱い"]]["assign_source"],
        rows[faces["自動・強い"]]["assign_source"],
    } == {db.ASSIGN_MANUAL}


def test_not_this_person_in_the_auto_view_records_it_per_person(seeded):
    """「この人物ではない」も、**その顔に付いている人物ごとに記録する。**"""
    window, faces, persons = seeded

    _select(window, scope=photoarchive_gui.SCOPE_AUTO)
    window.face_list.selectAll()
    window._reject_for_person_selected()

    assert db.count_person_rejections(window.connection, persons["${PERSON_4}"]) == 1
    assert db.count_person_rejections(window.connection, persons["${PERSON_3}"]) == 1
    assert db.face_ids(
        window.connection, rejected_for_person=persons["${PERSON_4}"]
    ) == [faces["自動・弱い"]]


def test_confirm_is_blocked_unless_an_automatic_face_is_selected(seeded):
    """**確定は自動割り当てにしか効かない。** 押せない理由はツールチップに出す。"""
    window, _faces, persons = seeded

    _select(window, person_id=persons["${PERSON_4}"])
    window.source_box.setCurrentIndex(
        window.source_box.findText("確定済みのみ")
    )
    window.face_list.selectAll()

    assert window.action_confirm.isEnabled() is False
    assert window.action_confirm.toolTip() == photoarchive_gui.CONFIRM_TOOLTIP_BLOCKED


# ---------------------------------------------------------------------------
# 除外の確認
# ---------------------------------------------------------------------------


def test_rejecting_from_the_unassigned_view_does_not_ask(seeded, monkeypatch):
    """**毎件通る操作に確認を挟まない。** そこが遅さの正体になる。"""
    window, _faces, _persons = seeded
    asked = []
    monkeypatch.setattr(
        photoarchive_gui.QMessageBox,
        "question",
        lambda *args, **kwargs: asked.append(args)
        or photoarchive_gui.QMessageBox.StandardButton.Yes,
    )

    _select(window, scope=photoarchive_gui.SCOPE_UNASSIGNED)
    window.face_list.selectAll()
    window._reject_selected()

    assert asked == []
    assert db.count_faces(window.connection, assign_source=db.ASSIGN_REJECTED) == 3


def test_rejecting_an_assigned_face_asks_first(seeded, monkeypatch):
    """**割り当て済みに押すときは確認する。** 押し間違えると手作業の結果が消える。"""
    window, _faces, persons = seeded
    asked = {}

    def fake_question(parent, title, text, *args, **kwargs):
        asked["text"] = text
        return photoarchive_gui.QMessageBox.StandardButton.No

    monkeypatch.setattr(photoarchive_gui.QMessageBox, "question", fake_question)

    _select(window, person_id=persons["${PERSON_4}"])
    window.face_list.selectAll()
    window._reject_selected()

    assert "どの人物にも自動で付かなくなります" in asked["text"]
    assert "この人物ではない" in asked["text"], "もう一方の操作を案内すること"
    assert db.count_faces(window.connection, assign_source=db.ASSIGN_REJECTED) == 1


# ---------------------------------------------------------------------------
# 共通の絞り込みが、人物の表示にも効く
# ---------------------------------------------------------------------------


def test_the_month_range_also_narrows_a_person_s_faces(seeded):
    """**人物の表示でも撮影年月で絞れる。** 別ウィンドウには無かった。

    「2018年の${PERSON_4}だけ見直す」ができる。
    """
    window, faces, persons = seeded
    db.assign_faces(
        window.connection, [faces["未割当・古い"]], persons["${PERSON_3}"], db.ASSIGN_MANUAL
    )
    window._reload_person_list(select_person_id=persons["${PERSON_3}"])
    assert len(_ids(window)) == 2

    index = window.month_from_box.findText("2018-07")
    assert index >= 0
    window.month_from_box.setCurrentIndex(index)

    assert _ids(window) == [faces["自動・強い"]]


def test_the_rejection_list_can_also_be_narrowed_and_paged(seeded, monkeypatch):
    """「この人物ではない」の一覧も**同じ読み出し経路**に乗っていること。

    以前は専用の読み出しで、撮影年月の絞り込みもページャも効かなかった。
    """
    window, faces, persons = seeded
    db.reject_faces_for_person(
        window.connection,
        [faces["未割当・古い"], faces["未割当・新しい"]],
        persons["${PERSON_4}"],
    )
    monkeypatch.setattr(photoarchive_gui, "PAGE_SIZE", 1)

    _select(window, person_id=persons["${PERSON_4}"])
    window.source_box.setCurrentIndex(
        window.source_box.findText(photoarchive_gui.NOT_THIS_PERSON_FILTER)
    )

    assert window.face_list.count() == 1, "ページ単位で読むこと"
    assert "1 / 2 ページ（全 2 件）" in window.page_label.text()

    window.month_from_box.setCurrentIndex(window.month_from_box.findText("2018-07"))
    assert _ids(window) == [faces["未割当・新しい"]]


# ---------------------------------------------------------------------------
# 誕生前の判定（計算だけのテスト）
# ---------------------------------------------------------------------------


def test_a_broken_shooting_date_is_not_counted_as_before_birth():
    """**読めない撮影日時を「誕生前」に数えない。**

    このリポジトリは日付の判断で2度壊れている（`0000-00-00` だけを見ていて
    `TTTT-TT-TTTTT:TT:TT` が素通りした）。判断は `dates.parse_date` に1つだけ。
    """
    assert photoarchive_gui.has_pre_birth_photo("2011-05-03", ["2009-01-01T00:00:00"])
    assert not photoarchive_gui.has_pre_birth_photo(
        "2011-05-03", ["TTTT-TT-TTTTT:TT:TT", None, "0000-00-00T00:00:00"]
    )
    # 誕生日が無ければ判断できない。**分からないことを理由に警告しない。**
    assert not photoarchive_gui.has_pre_birth_photo(None, ["2009-01-01T00:00:00"])
    # 誕生日の当日は「誕生前」ではない。
    assert not photoarchive_gui.has_pre_birth_photo(
        "2011-05-03", ["2011-05-03T00:00:00"]
    )


# ---------------------------------------------------------------------------
# 行事のまとめ処理が、表示に合わせて意味を変える
# ---------------------------------------------------------------------------


def test_the_bulk_event_action_follows_the_view(seeded):
    """**表示を切り替えたら、まとめて処理の意味も変える。**

    「この人物ではない」の一覧には、まとめて効く操作がない。**押せなくする**
    （押せると、何が起きるか分からないまま数百件に効いてしまう）。
    """
    window, _faces, persons = seeded
    window.event = ("/photos/2018", "2018-07-07")
    window._sync_event_controls()

    _select(window, scope=photoarchive_gui.SCOPE_UNASSIGNED)
    assert "未割当をすべて除外" in window.bulk_event_button.text()
    assert window.bulk_event_button.isEnabled()

    _select(window, person_id=persons["${PERSON_4}"])
    assert "${PERSON_4}" in window.bulk_event_button.text()
    assert "解除" in window.bulk_event_button.text()

    window.source_box.setCurrentIndex(
        window.source_box.findText(photoarchive_gui.NOT_THIS_PERSON_FILTER)
    )
    assert window.bulk_event_button.isEnabled() is False
    assert "この表示では無し" in window.bulk_event_button.text()


# ---------------------------------------------------------------------------
# サムネイルの枠（利用者が報告した不具合）
# ---------------------------------------------------------------------------


def _thumbnail_bytes(size: int) -> bytes:
    """``size`` 四方の JPEG。**保存してあるサムネイルは大きさがまちまち。**"""
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (size, size), (120, 120, 120)).save(buffer, format="JPEG")
    return buffer.getvalue()


def test_a_small_thumbnail_at_the_top_does_not_shrink_the_whole_page(tmp_path):
    """**先頭の顔が小さくても、ページ全体の枠は縮まない。**

    `setUniformItemSizes(True)` は**先頭の項目から枠の寸法を決める。**
    保存してあるサムネイルは大きさがまちまち（短辺の中央 160px・**112px 未満が
    7.3%**）なので、先頭にたまたま小さい顔が来たページでは、**枠がその顔に
    合わせて縮み、残りのサムネイルが切り詰められて下の文字も枠の外に出た**
    （利用者が報告。実データの未割当1ページ目は先頭が 101px・残りが 160px）。
    """
    database = tmp_path / "cells.db"
    connection = db.ensure_database(str(database))
    # 撮影日時の新しい順に並ぶので、**小さいサムネイルの顔が先頭に来る**。
    plan = [("2020-01-01T10:00:00", 101), ("2019-01-01T10:00:00", 160),
            ("2018-01-01T10:00:00", 160)]
    for index, (shooting_date, size) in enumerate(plan):
        media_id = _media(connection, f"/photos/{index}.jpg", shooting_date, f"h{index}")
        db.add_face(
            connection,
            media_id=media_id,
            bbox=(0, 10, 10, 0),
            embedding=[0.0] * db.EMBEDDING_DIM,
            embed_version=db.embedding_model.ACTIVE.version,
            thumbnail=_thumbnail_bytes(size),
        )
    connection.commit()
    connection.close()

    window = photoarchive_gui.MainWindow(str(database))
    try:
        hints = [
            window.face_list.item(row).sizeHint()
            for row in range(window.face_list.count())
        ]

        assert len(hints) == 3
        assert len({(hint.width(), hint.height()) for hint in hints}) == 1, (
            "枠は全件そろうこと"
        )
        assert hints[0].width() == photoarchive_gui.ITEM_WIDTH
        assert hints[0].height() == photoarchive_gui.ITEM_HEIGHT
        # **枠を明示していないと、先頭の項目の大きさがそのまま効く。**
        assert window.face_list.gridSize().height() == photoarchive_gui.ITEM_HEIGHT
    finally:
        window.connection.close()


def test_the_cell_leaves_room_for_the_thumbnail_and_two_lines_of_text():
    """**サムネイルの下の文字が枠から出ないこと。**

    自動割当の表示は `13391 (自動 55) ${PERSON_4} (0歳)` のように長く、2行になる。
    """
    assert photoarchive_gui.ITEM_HEIGHT >= photoarchive_gui.THUMBNAIL_SIZE + 32
    assert photoarchive_gui.ITEM_WIDTH > photoarchive_gui.THUMBNAIL_SIZE


def test_both_face_lists_are_built_the_same_way():
    """メイン画面と束ねる画面で、一覧の設定を2か所に書かない。"""
    from PySide6.QtWidgets import QListWidget

    widget = photoarchive_gui.make_face_list(QListWidget.SelectionMode.NoSelection)

    assert widget.gridSize().width() == photoarchive_gui.ITEM_WIDTH
    assert widget.iconSize().width() == photoarchive_gui.THUMBNAIL_SIZE
    assert widget.wordWrap() is True
    assert widget.uniformItemSizes() is True


# ---------------------------------------------------------------------------
# 並び順は全件に効き、意味のない並びは選べない（2026-10-09 利用者の報告）
# ---------------------------------------------------------------------------


def test_orders_that_mean_nothing_in_the_view_cannot_be_chosen(seeded):
    """**未割当では年齢と確信度の並びを押せない。** 理由はツールチップに出す。

    未割当の顔は人物が決まっていないので年齢を出せず、確信度も持たない
    （実データの未割当 31,275 件で、年齢は 2 件・確信度は 0 件）。選べると
    id 順のまま何も変わらず、**並べ替えが壊れているように見える。**
    """
    window, _faces, persons = seeded

    def enabled():
        model = window.order_box.model()
        return {
            value: model.item(index).isEnabled()
            for index, (_, value) in enumerate(photoarchive_gui.ORDER_CHOICES)
        }

    _select(window, scope=photoarchive_gui.SCOPE_UNASSIGNED)
    state = enabled()
    assert state[db.ORDER_AGE] is False
    assert state[db.ORDER_SCORE_ASC] is False
    assert state[db.ORDER_SHOT_DESC] is True
    age_index = next(
        i for i, (_, v) in enumerate(photoarchive_gui.ORDER_CHOICES) if v == db.ORDER_AGE
    )
    assert "年齢を出せません" in window.order_box.model().item(age_index).toolTip()

    _select(window, person_id=persons["${PERSON_4}"])
    assert all(enabled().values()), "人物の表示ではどの並びも意味を持つ"


def test_the_person_view_is_sorted_by_the_shown_age_on_every_page(tmp_path, monkeypatch):
    """**画面に出ている年齢が、ページをまたいで若い順に並ぶこと。**

    以前は確定値（`Face.age`）だけで並べていたので、括弧つきの計算年齢しか
    持たない顔（実データの 98%）は id 順のまま2ページ目以降に散っていた。
    """
    import re

    monkeypatch.setattr(photoarchive_gui, "PAGE_SIZE", 2)
    database = tmp_path / "ages.db"
    connection = db.ensure_database(str(database))
    person_id = db.add_person(connection, "${PERSON_4}", birth_date="2010-12-01")
    # id の順と年齢の順をわざと逆にする
    for index, year in enumerate((2024, 2016, 2020, 2012, 2018)):
        media_id = _media(connection, f"/photos/{index}.jpg", f"{year}-06-01T10:00:00", f"h{index}")
        face_id = _add_face(connection, media_id)
        db.assign_faces(connection, [face_id], person_id, db.ASSIGN_AUTO)
    connection.commit()
    connection.close()

    window = photoarchive_gui.MainWindow(str(database))
    try:
        _select(window, person_id=person_id)
        ages = []
        for _ in range(3):
            ages += [
                int(match.group(1))
                for match in (re.search(r"\((\d+)歳\)", label) for label in _labels(window))
                if match
            ]
            window._next_page()

        assert ages == [1, 5, 7, 9, 13], ages
    finally:
        window.connection.close()
