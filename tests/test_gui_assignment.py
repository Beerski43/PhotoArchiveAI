import os

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from photoarchive_ai import db
from photoarchive_ai import gui as photoarchive_gui

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def _always_accepts_age(age):
    """年齢を入れて OK を押す `FaceAgeDialog` の代わり。"""

    class _Dialog:
        def __init__(self, parent=None, summary="", initial_age=None):
            pass

        def exec(self):
            return QDialog.Accepted

        def age(self):
            return age

    return _Dialog


def _seed(connection, count: int) -> None:
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
    for index in range(count):
        db.add_face(
            connection,
            media_id=media_id,
            bbox=(0, 10, 10, 0),
            embedding=[0.0] * db.EMBEDDING_DIM,
            embed_version=db.embedding_model.ACTIVE.version,
            thumbnail=b"",
            quality_score=float(index),
        )
    connection.commit()


@pytest.fixture()
def window(tmp_path):
    database = tmp_path / "gui.db"
    connection = db.ensure_database(str(database))
    _seed(connection, 5)
    connection.close()
    window = photoarchive_gui.MainWindow(str(database))
    yield window
    window.connection.close()


def test_unassigned_faces_are_listed(window):
    assert window.face_list.count() == 5
    assert "全 5 件" in window.page_label.text()


def test_face_list_is_paged(tmp_path, monkeypatch):
    monkeypatch.setattr(photoarchive_gui, "PAGE_SIZE", 2)
    database = tmp_path / "paged.db"
    connection = db.ensure_database(str(database))
    _seed(connection, 5)
    connection.close()

    window = photoarchive_gui.MainWindow(str(database))
    try:
        # 数万件規模でも固まらないよう、1ページ分しか読まない
        assert window.face_list.count() == 2
        assert "1 / 3 ページ" in window.page_label.text()
        window._next_page()
        assert window.face_list.count() == 2
        assert "2 / 3 ページ" in window.page_label.text()
        window._next_page()
        assert window.face_list.count() == 1
    finally:
        window.connection.close()


def test_assign_and_unassign_faces(window):
    person_id = db.add_person(window.connection, "Alice")
    face_ids = [row["id"] for row in db.list_faces(window.connection, unassigned=True)][:2]

    window.assign_faces(face_ids, person_id, age=7)
    window.reload_faces()

    assert db.count_faces(window.connection, assign_source=db.ASSIGN_MANUAL) == 2
    assert window.face_list.count() == 3
    assert db.get_face(window.connection, face_ids[0])["age"] == 7

    db.unassign_faces(window.connection, face_ids)
    window.reload_faces()
    assert window.face_list.count() == 5


def test_reject_faces_removes_them_from_the_unassigned_list(window):
    face_id = db.list_faces(window.connection, unassigned=True)[0]["id"]
    db.reject_faces(window.connection, [face_id])
    window.reload_faces()

    assert window.face_list.count() == 4
    assert db.get_face(window.connection, face_id)["assign_source"] == db.ASSIGN_REJECTED


def test_deleting_person_returns_faces_to_the_unassigned_list(window):
    person_id = db.add_person(window.connection, "Alice")
    face_ids = [row["id"] for row in db.list_faces(window.connection, unassigned=True)][:2]
    window.assign_faces(face_ids, person_id)

    db.delete_person(window.connection, person_id)
    window.reload_faces()

    assert window.face_list.count() == 5
    assert db.get_face(window.connection, face_ids[0])["person_id"] is None


def test_face_age_dialog_keeps_zero_distinct_from_unset():
    """0歳と「未設定」を取り違えないこと。

    旧実装は `value() or None` だったため、0歳が「未設定」に潰れていた。
    """
    dialog = photoarchive_gui.FaceAgeDialog()
    assert dialog.age() is None
    dialog.age_input.setValue(0)
    assert dialog.age() == 0
    dialog.age_input.setValue(12)
    assert dialog.age() == 12


@pytest.mark.parametrize(
    ("typed", "expected"), [("5", 5), ("12", 12), ("0", 0), ("150", 150)]
)
def test_the_age_can_be_typed_straight_from_the_keyboard(qt_app, typed, expected):
    """**キーボードで数字を打てること。**

    `setSpecialValueText` を使っているので、入力欄には数字ではなく「未設定」と
    いう**文字**が入っている。そのまま数字を打つと "未設定5" になって検証に
    落ち、**何も起きない。** 利用者からは「▲を押さないと入力できない」と
    見える（実際にそう報告された）。

    0歳と150歳（範囲の両端）も打てることまで見る。
    """
    dialog = photoarchive_gui.FaceAgeDialog()
    dialog.show()
    qt_app.processEvents()

    QTest.keyClicks(dialog.age_input, typed)

    assert dialog.age() == expected
    dialog.close()


def test_typing_nothing_leaves_the_age_unset(qt_app):
    """打鍵しなければ「未設定」のまま。全選択しても値を変えない。"""
    dialog = photoarchive_gui.FaceAgeDialog()
    dialog.show()
    qt_app.processEvents()

    assert dialog.age() is None
    assert dialog.age_input.text() == "未設定"
    dialog.close()


def test_the_age_filter_can_also_be_typed(window, qt_app):
    """年齢の絞り込みも同じ作りなので、同じように打てること。

    「指定なし」の文字が入っている点は年齢の入力と同じ。片方だけ直すと、
    次に触った人が「こちらは打てるのに、あちらは打てない」と混乱する。
    """
    person_id = db.add_person(window.connection, "父")
    person = next(p for p in db.list_persons(window.connection) if p["id"] == person_id)
    dialog = photoarchive_gui.RegisteredFacesDialog(window, window.connection, person)
    dialog.show()
    qt_app.processEvents()

    dialog.min_age.setFocus()
    QTest.keyClicks(dialog.min_age, "3")

    assert dialog.min_age.value() == 3
    dialog.close()


def test_registered_faces_dialog_pages_through_every_assigned_face(window, monkeypatch):
    """割り当て済みの顔にページャがあること。

    以前は先頭の1ページぶんしか読まず、201件目以降の顔に手が届かなかった。
    確定(auto→manual への昇格)で手本を増やしていくと簡単に超える。
    """
    monkeypatch.setattr(photoarchive_gui, "PAGE_SIZE", 2)
    person_id = db.add_person(window.connection, "父")
    face_ids = [row["id"] for row in db.list_faces(window.connection, unassigned=True)]
    window.assign_faces(face_ids, person_id)

    person = next(p for p in db.list_persons(window.connection) if p["id"] == person_id)
    dialog = photoarchive_gui.RegisteredFacesDialog(window, window.connection, person)

    assert dialog.total == 5
    assert dialog.face_list.count() == 2
    assert dialog.prev_button.isEnabled() is False

    seen = []
    for _ in range(3):
        seen.extend(item.data(photoarchive_gui.Qt.UserRole)["id"] for item in
                    [dialog.face_list.item(i) for i in range(dialog.face_list.count())])
        dialog._next_page()

    assert sorted(seen) == sorted(face_ids)
    assert dialog.next_button.isEnabled() is False

    dialog._previous_page()
    assert dialog.face_list.count() == 2


def test_the_age_filter_returns_to_the_first_page(window, monkeypatch):
    monkeypatch.setattr(photoarchive_gui, "PAGE_SIZE", 2)
    person_id = db.add_person(window.connection, "父")
    face_ids = [row["id"] for row in db.list_faces(window.connection, unassigned=True)]
    window.assign_faces(face_ids, person_id)
    person = next(p for p in db.list_persons(window.connection) if p["id"] == person_id)
    dialog = photoarchive_gui.RegisteredFacesDialog(window, window.connection, person)

    dialog._next_page()
    assert dialog.page == 1

    dialog.min_age.setValue(3)
    assert dialog.page == 0


def test_assigning_without_an_age_keeps_the_one_already_recorded(window):
    person_id = db.add_person(window.connection, "父")
    face_id = db.list_faces(window.connection, unassigned=True)[0]["id"]

    window.assign_faces([face_id], person_id, age=7)
    assert db.get_face(window.connection, face_id)["age"] == 7

    # 年齢を指定しない割り当ては年齢を触らない。
    window.assign_faces([face_id], person_id)
    assert db.get_face(window.connection, face_id)["age"] == 7


def test_an_age_can_be_cleared_back_to_unset(window):
    """一度入れた年齢を「未設定」へ戻せること。

    仕様上、未設定と0歳は別の状態。戻せないと間違えて入れた年齢を
    直せず、0歳として扱うしかなくなる。
    """
    person_id = db.add_person(window.connection, "父")
    face_id = db.list_faces(window.connection, unassigned=True)[0]["id"]
    window.assign_faces([face_id], person_id, age=7)

    window.assign_faces([face_id], person_id, age=None)

    assert db.get_face(window.connection, face_id)["age"] is None


def test_zero_is_stored_as_zero_and_not_as_unset(window):
    person_id = db.add_person(window.connection, "父")
    face_id = db.list_faces(window.connection, unassigned=True)[0]["id"]

    window.assign_faces([face_id], person_id, age=0)

    assert db.get_face(window.connection, face_id)["age"] == 0


def test_changing_an_age_later_also_offers_the_calculated_value(window, monkeypatch):
    """「割り当て済みを確認」から年齢を直すときも、計算値を初期値に入れる。

    こちらだけ手計算のままだと、**あとから直すときにいちばん手間がかかる。**
    """
    connection = window.connection
    # _seed のメディアは撮影日時を持たない。年齢を出すには EXIF が要る。
    connection.execute("UPDATE Media SET shooting_date = '2017-12-16T18:46:32'")
    person_id = db.add_person(connection, "${PERSON_2}", birth_date="2011-05-03")
    face_ids = [row["id"] for row in db.list_faces(connection, unassigned=True)]
    window.assign_faces(face_ids, person_id)
    person = next(p for p in db.list_persons(connection) if p["id"] == person_id)
    dialog = photoarchive_gui.RegisteredFacesDialog(window, connection, person)
    dialog.face_list.selectAll()

    opened = {}

    class _FakeAgeDialog:
        def __init__(self, parent=None, summary="", initial_age=None):
            opened["initial_age"] = initial_age

        def exec(self):
            return QDialog.Rejected

        def age(self):
            return None

    monkeypatch.setattr(photoarchive_gui, "FaceAgeDialog", _FakeAgeDialog)
    dialog._set_age_selected()

    assert opened["initial_age"] == 6
    # 取り消したので、年齢は未設定のまま
    assert all(row["age"] is None for row in db.list_faces(connection, person_id=person_id))
    dialog.close()


def _media_with_date(connection, path, shooting_date, file_hash):
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


def test_the_unassigned_list_starts_with_the_newest_photo(window):
    """割り当てる画面は**撮影日時の新しい順**（#53）。

    品質スコア順だと、同じ人の同じ日の写真がページをまたいで散らばる。
    日付順なら**同じ行事の写真が固まる**ので、まとめて選んで一度に割り当てられる。
    """
    connection = window.connection
    # _seed のメディアは撮影日時を持たない。持たない顔は最後に来るはず
    for index, date in enumerate(("2012-01-01T00:00:00", "2021-12-31T00:00:00")):
        media_id = _media_with_date(connection, f"/photos/d{index}.jpg", date, f"h{index}")
        db.add_face(
            connection,
            media_id=media_id,
            bbox=(0, 10, 10, 0),
            embedding=[0.0] * db.EMBEDDING_DIM,
            embed_version=db.embedding_model.ACTIVE.version,
            thumbnail=b"",
            # 品質スコアは日付と逆に振る。品質順のままなら並びが変わらない
            quality_score=100.0 if index == 0 else 1.0,
        )
    connection.commit()
    window._reset_page()

    listed = [
        db.get_media_by_id(connection, window.face_list.item(row).data(Qt.UserRole)["media_id"])[
            "shooting_date"
        ]
        for row in range(window.face_list.count())
    ]

    assert listed[0] == "2021-12-31T00:00:00"
    assert listed[1] == "2012-01-01T00:00:00"
    # 撮影日時の無い顔は最後にまとまる
    assert set(listed[2:]) == {None}


def test_the_assigned_list_is_ordered_by_age(window):
    """「割り当て済みを確認」は**年齢順**（#53）。

    成長の順に並ぶので、年齢の入れ間違いや、別人が混ざっているのに気づきやすい。
    """
    connection = window.connection
    person_id = db.add_person(connection, "${PERSON_2}")
    face_ids = [row["id"] for row in db.list_faces(connection, unassigned=True)]
    for face_id, age in zip(face_ids, (8, 2, 5, None, 0)):
        window.assign_faces([face_id], person_id, age=age)

    person = next(p for p in db.list_persons(connection) if p["id"] == person_id)
    dialog = photoarchive_gui.RegisteredFacesDialog(window, connection, person)

    ages = [
        dialog.face_list.item(row).data(Qt.UserRole)["age"]
        for row in range(dialog.face_list.count())
    ]

    assert ages[:4] == [0, 2, 5, 8]
    # **未設定は最後。** 先頭に来ると、年齢順に見ていく邪魔になる
    assert ages[-1] is None
    dialog.close()


def test_rebuilding_the_list_does_not_reload_the_preview(window, monkeypatch):
    """**一覧を作り直すたびに元写真を読み直さない。**

    `clear()` は項目を1つずつ外すので、そのたびに `itemSelectionChanged` が
    出る。プレビューがそれに繋がっていたため、**200件を選んで割り当てると
    元写真を NFS から100回読み直し、1回の操作に17秒かかっていた**
    （実データで実測。1枚あたり 220ms）。
    """
    reads = []
    monkeypatch.setattr(
        photoarchive_gui.face,
        "load_face_image_bytes",
        lambda path, bbox: reads.append(path) or b"",
    )
    db.add_person(window.connection, "父")
    window._reload_person_list()
    window.person_list.setCurrentRow(0)
    window.face_list.selectAll()
    monkeypatch.setattr(photoarchive_gui, "FaceAgeDialog", _always_accepts_age(5))
    # 選んだときの1回は正しい読み出し。数えるのは**作り直しのぶん**だけ
    reads.clear()

    window._assign_selected()

    assert reads == []


def test_the_progress_is_reported_for_every_face(window, monkeypatch):
    """**進み具合が件数で出ること。** 複数枚を一度に処理するときの手がかり。"""
    reported = []

    class _Spy(photoarchive_gui.WorkProgress):
        def __call__(self, done, total):
            reported.append((done, total))

        def finish(self):
            pass

    monkeypatch.setattr(photoarchive_gui, "WorkProgress", _Spy)
    monkeypatch.setattr(photoarchive_gui, "FaceAgeDialog", _always_accepts_age(5))
    db.add_person(window.connection, "父")
    window._reload_person_list()
    window.person_list.setCurrentRow(0)
    window.face_list.selectAll()

    window._assign_selected()

    # 最初に 0、最後に全件。件数は選んだ顔の数と一致する
    assert reported[0] == (0, 5)
    assert reported[-1] == (5, 5)


def test_the_cursor_is_restored_even_when_the_work_fails():
    """**砂時計を戻し忘れない。** 戻し損ねると、以後ずっと砂時計のままになる。"""
    before = QApplication.overrideCursor()

    with pytest.raises(RuntimeError):
        with photoarchive_gui.busy_cursor():
            raise RuntimeError("途中で失敗した")

    assert QApplication.overrideCursor() is before


def test_setting_the_age_of_many_faces_commits_once(window, monkeypatch):
    """**1件ずつコミットしない。** 200件なら 200 回の書き込み確定になる。"""
    person_id = db.add_person(window.connection, "父")
    face_ids = [row["id"] for row in db.list_faces(window.connection, unassigned=True)]
    window.assign_faces(face_ids, person_id)
    statements = []
    window.connection.set_trace_callback(statements.append)
    try:
        db.set_faces_age(window.connection, face_ids, 7)
    finally:
        window.connection.set_trace_callback(None)

    assert sum("COMMIT" in s.upper() for s in statements) == 1
    assert all(row["age"] == 7 for row in db.list_faces(window.connection, person_id=person_id))


# ---------------------------------------------------------------------------
# 除外の取り消し
# ---------------------------------------------------------------------------


def test_a_rejected_face_can_be_put_back_to_unassigned(window):
    """**除外を取り消せること。**

    除外した顔は「割り当て済みを確認」に出てこない（あちらは人物で絞るが、
    除外した顔は `person_id` を持たない）。そのため、いったん除外すると
    **誰かに割り当てる以外に戻す手段が無かった。** 「決めきれないので保留に
    戻す」ができない。
    """
    face_ids = [row["id"] for row in db.list_faces(window.connection, unassigned=True)]
    window.face_list.selectAll()
    window._reject_selected()
    assert db.count_faces(window.connection, assign_source=db.ASSIGN_REJECTED) == len(face_ids)

    window.filter_box.setCurrentText(photoarchive_gui.FILTER_REJECTED)
    window._reset_page()
    window.face_list.selectAll()
    window._unassign_selected()

    assert db.count_faces(window.connection, assign_source=db.ASSIGN_REJECTED) == 0
    assert db.count_faces(window.connection, unassigned=True) == len(face_ids)
    assert all(
        db.get_face(window.connection, face_id)["assign_source"] is None
        for face_id in face_ids
    )


def test_an_auto_assignment_can_also_be_put_back(window):
    """自動で付いた割り当ても、同じボタンで外せる。"""
    person_id = db.add_person(window.connection, "父")
    face_ids = [row["id"] for row in db.list_faces(window.connection, unassigned=True)]
    db.assign_faces(window.connection, face_ids, person_id, db.ASSIGN_AUTO, assign_score=50.0)

    window.filter_box.setCurrentText(photoarchive_gui.FILTER_AUTO)
    window._reset_page()
    window.face_list.selectAll()
    window._unassign_selected()

    assert db.count_faces(window.connection, assign_source=db.ASSIGN_AUTO) == 0
    assert db.count_faces(window.connection, unassigned=True) == len(face_ids)


def test_the_unassign_button_is_disabled_while_showing_unassigned_faces(window):
    """**隠さずに押せなくする。** 隠すと「そんな操作は無い」と思われる。

    戻す先が無いときに押せると、何も起きない操作を押させることになる。
    """
    window.filter_box.setCurrentText(photoarchive_gui.FILTER_UNASSIGNED)
    window._reset_page()
    assert window.unassign_button.isEnabled() is False
    assert "戻す先がありません" in window.unassign_button.toolTip()

    window.filter_box.setCurrentText(photoarchive_gui.FILTER_REJECTED)
    window._reset_page()
    assert window.unassign_button.isEnabled() is True
    assert "未割当に戻します" in window.unassign_button.toolTip()


def test_putting_a_face_back_says_done(window):
    """戻したあとも、プレビューに「完了」を出して薄くする（他の操作と同じ）。"""
    window.face_list.selectAll()
    window._reject_selected()
    window.filter_box.setCurrentText(photoarchive_gui.FILTER_REJECTED)
    window._reset_page()
    window.face_list.selectAll()

    window._unassign_selected()

    assert "未割当に戻しました" in window.preview_status.text()


def test_putting_faces_back_does_not_reload_the_preview(window, monkeypatch):
    """戻すときも、一覧の作り直しで元写真を読み直さない（割り当てと同じ）。"""
    reads = []
    monkeypatch.setattr(
        photoarchive_gui.face,
        "load_face_image_bytes",
        lambda path, bbox: reads.append(path) or b"",
    )
    window.face_list.selectAll()
    window._reject_selected()
    window.filter_box.setCurrentText(photoarchive_gui.FILTER_REJECTED)
    window._reset_page()
    window.face_list.selectAll()
    reads.clear()

    window._unassign_selected()

    assert reads == []




def _assigned_person_with_faces(connection, window, *, birth_date, shooting_date, source):
    """1人ぶんの顔を、割り当て元（手本か自動か）を指定して用意する。"""
    if shooting_date is None:
        connection.execute("UPDATE Media SET shooting_date = NULL")
    else:
        connection.execute("UPDATE Media SET shooting_date = ?", (shooting_date,))
    person_id = db.add_person(connection, "${PERSON_2}", birth_date=birth_date)
    face_ids = [row["id"] for row in db.list_faces(connection, unassigned=True)]
    db.assign_faces(connection, face_ids, person_id, source)
    connection.commit()
    person = next(p for p in db.list_persons(connection) if p["id"] == person_id)
    return photoarchive_gui.RegisteredFacesDialog(window, connection, person), face_ids


def _labels(dialog):
    return [dialog.face_list.item(i).text() for i in range(dialog.face_list.count())]


def test_an_automatic_face_shows_the_age_calculated_from_the_birth_date(window):
    """**自動割り当ての顔は `Face.age` が未設定**なので、今まで年齢が出なかった。

    `match` は年齢を書かない（実データで年齢が入っているのは手本の126件だけ）。
    **自動割り当てが正しいかを人が見るとき、撮影時の年齢がいちばん効く手がかり。**
    """
    dialog, _ = _assigned_person_with_faces(
        window.connection,
        window,
        birth_date="2011-05-03",
        shooting_date="2017-12-16T18:46:32",
        source=db.ASSIGN_AUTO,
    )

    # 2011-05-03 生まれが 2017-12-16 に写っていれば6歳。
    assert all("(6歳)" in label for label in _labels(dialog)), _labels(dialog)


def test_a_calculated_age_is_told_apart_from_one_a_person_confirmed(window):
    """**計算値と確定値を同じ見た目にしない。** どちらが人の確かめた値か分からなくなる。

    括弧つきが計算値。`Face.age` に書き戻さないのも同じ理由（Issue #48 の判断2）。
    """
    connection = window.connection
    dialog, face_ids = _assigned_person_with_faces(
        connection,
        window,
        birth_date="2011-05-03",
        shooting_date="2017-12-16T18:46:32",
        source=db.ASSIGN_AUTO,
    )
    db.set_face_age(connection, face_ids[0], 6)
    connection.commit()
    dialog.reload()

    labels = _labels(dialog)
    # **年齢の部分だけで見分ける。** ラベルには "(自動 0)" も入るので、
    # 括弧の有無をラベル全体で見てはいけない。
    assert any(label.endswith(" 6歳") for label in labels), labels
    assert any(label.endswith(" (6歳)") for label in labels), labels
    # **計算しただけの年齢を DB に書き戻していないこと。**
    unset = {row["id"] for row in db.list_faces(connection) if row["age"] is None}
    assert unset == set(face_ids[1:])


def test_a_face_taken_before_the_person_was_born_says_so(window):
    """**「誕生前」は誤割り当てのいちばん強い手がかり。** 負の数でも落とさない。"""
    dialog, _ = _assigned_person_with_faces(
        window.connection,
        window,
        birth_date="2011-05-03",
        shooting_date="2009-12-28T15:19:49",
        source=db.ASSIGN_AUTO,
    )

    assert all("(誕生前)" in label for label in _labels(dialog)), _labels(dialog)


def test_no_age_is_shown_when_the_shooting_date_is_missing(window):
    """**撮影日時が無ければ年齢は出せない。** 実データの 15.8% が該当する。"""
    dialog, _ = _assigned_person_with_faces(
        window.connection,
        window,
        birth_date="2011-05-03",
        shooting_date=None,
        source=db.ASSIGN_AUTO,
    )

    assert all("歳" not in label for label in _labels(dialog)), _labels(dialog)


def test_no_age_is_shown_when_the_person_has_no_birth_date(window):
    """誕生日が未登録なら計算できない。"""
    dialog, _ = _assigned_person_with_faces(
        window.connection,
        window,
        birth_date=None,
        shooting_date="2017-12-16T18:46:32",
        source=db.ASSIGN_AUTO,
    )

    assert all("歳" not in label for label in _labels(dialog)), _labels(dialog)


def test_confirm_is_blocked_until_an_automatic_face_is_selected(window):
    """**確定は自動割り当てにしか効かない。**

    手本に押しても `assigned_at` が書き換わるだけで意味のある変化が起きない。
    押せてしまうと「何かが起きた」と誤解する。
    """
    dialog, _ = _assigned_person_with_faces(
        window.connection,
        window,
        birth_date="2011-05-03",
        shooting_date="2017-12-16T18:46:32",
        source=db.ASSIGN_MANUAL,
    )

    assert not dialog.confirm_button.isEnabled(), "何も選んでいないので押せない"

    dialog.face_list.selectAll()

    assert not dialog.confirm_button.isEnabled(), "手本だけなので押せない"
    assert "自動割り当ての顔を選んでいるときだけ" in dialog.confirm_button.toolTip()


def test_confirm_becomes_available_when_the_selection_holds_an_automatic_face(window):
    """自動が1件でも混ざっていれば押せる。**混在した選択で押せなくしない。**"""
    connection = window.connection
    dialog, face_ids = _assigned_person_with_faces(
        connection,
        window,
        birth_date="2011-05-03",
        shooting_date="2017-12-16T18:46:32",
        source=db.ASSIGN_MANUAL,
    )
    db.assign_faces(connection, face_ids[:1], dialog.person["id"], db.ASSIGN_AUTO)
    connection.commit()
    dialog.reload()

    dialog.face_list.selectAll()

    assert dialog.confirm_button.isEnabled()
    assert dialog.confirm_button.toolTip() == photoarchive_gui.CONFIRM_TOOLTIP_READY


def test_confirm_goes_back_to_blocked_after_the_list_is_rebuilt(window):
    """確定したあと、一覧を作り直すと選択が消える。**押せたままにしない。**

    `_fill_face_list` は作り直すあいだ信号を止めるので
    `itemSelectionChanged` が出ない。明示的に見直す必要がある。
    """
    connection = window.connection
    dialog, _ = _assigned_person_with_faces(
        connection,
        window,
        birth_date="2011-05-03",
        shooting_date="2017-12-16T18:46:32",
        source=db.ASSIGN_AUTO,
    )
    dialog.face_list.selectAll()
    assert dialog.confirm_button.isEnabled()

    dialog._confirm_selected()

    assert dialog.face_list.selectedItems() == []
    assert not dialog.confirm_button.isEnabled()
    assert {row["assign_source"] for row in db.list_faces(connection)} == {db.ASSIGN_MANUAL}


def _select_source(dialog, label):
    """種別を選ぶ。**コンボボックスを直に引く。**

    「この人物ではない」は `SOURCE_FILTERS` には無い（`assign_source` の値では
    ないため）ので、定数の一覧から引くと取りこぼす。
    """
    index = dialog.source_box.findText(label)
    assert index >= 0, f"種別に {label} が無い"
    dialog.source_box.setCurrentIndex(index)


def test_the_list_can_be_filtered_down_to_the_automatic_faces(window):
    """**自動割り当てを見直すときは、自動だけを見たい。**"""
    connection = window.connection
    dialog, face_ids = _assigned_person_with_faces(
        connection,
        window,
        birth_date="2011-05-03",
        shooting_date="2017-12-16T18:46:32",
        source=db.ASSIGN_MANUAL,
    )
    db.assign_faces(connection, face_ids[:2], dialog.person["id"], db.ASSIGN_AUTO)
    connection.commit()
    dialog.reload()
    assert dialog.total == len(face_ids), "「すべて」では全部見える"

    _select_source(dialog, "自動のみ")

    assert dialog.total == 2
    assert all("(自動" in label for label in _labels(dialog)), _labels(dialog)


def test_the_list_can_be_filtered_down_to_the_confirmed_faces(window):
    """**手本を見直すときは、確定済みだけを見たい。**"""
    connection = window.connection
    dialog, face_ids = _assigned_person_with_faces(
        connection,
        window,
        birth_date="2011-05-03",
        shooting_date="2017-12-16T18:46:32",
        source=db.ASSIGN_MANUAL,
    )
    db.assign_faces(connection, face_ids[:2], dialog.person["id"], db.ASSIGN_AUTO)
    connection.commit()
    dialog.reload()

    _select_source(dialog, "確定済みのみ")

    assert dialog.total == len(face_ids) - 2
    assert all("(自動" not in label for label in _labels(dialog)), _labels(dialog)
    # 確定済みだけを選んでいるので、確定ボタンは押せない。
    dialog.face_list.selectAll()
    assert not dialog.confirm_button.isEnabled()


def test_changing_the_source_filter_returns_to_the_first_page(window, monkeypatch):
    """年齢の絞り込みと同じ。**絞ったのに後ろのページのままだと空に見える。**"""
    monkeypatch.setattr(photoarchive_gui, "PAGE_SIZE", 2)
    connection = window.connection
    dialog, face_ids = _assigned_person_with_faces(
        connection,
        window,
        birth_date="2011-05-03",
        shooting_date="2017-12-16T18:46:32",
        source=db.ASSIGN_MANUAL,
    )
    db.assign_faces(connection, face_ids[:1], dialog.person["id"], db.ASSIGN_AUTO)
    connection.commit()
    dialog.reload()
    dialog._next_page()
    assert dialog.page == 1

    _select_source(dialog, "自動のみ")

    assert dialog.page == 0
    assert dialog.total == 1


def test_zero_can_be_used_as_an_age_filter_bound(window):
    """**0 を「指定なし」に使うと、0歳で絞れなくなる。**

    `FaceAgeDialog` は最小値を -1 にしてこれを避けているのに、**絞り込み側だけ
    0 を特別扱いにしていた。** QSpinBox の `specialValueText` は最小値のときに出る。
    """
    connection = window.connection
    dialog, face_ids = _assigned_person_with_faces(
        connection,
        window,
        birth_date="2011-05-03",
        shooting_date="2017-12-16T18:46:32",
        source=db.ASSIGN_MANUAL,
    )
    db.set_face_age(connection, face_ids[0], 0)
    for face_id in face_ids[1:]:
        db.set_face_age(connection, face_id, 6)
    connection.commit()

    assert dialog._age_range() == (None, None), "初期値は両方「指定なし」"

    dialog.min_age.setValue(0)
    dialog.max_age.setValue(0)

    assert dialog._age_range() == (0, 0), "0 が None に化けないこと"
    assert dialog.total == 1
    assert [item.data(Qt.UserRole)["id"] for item in
            [dialog.face_list.item(i) for i in range(dialog.face_list.count())]] == [face_ids[0]]


def test_the_age_filter_uses_the_calculated_age_when_face_age_is_unset(window):
    """**`Face.age` だけを見ていると、絞り込みが何もしないのと同じになる。**

    `match` は年齢を書かないので、自動割り当ての顔は全部未設定。実データでは
    割り当て済み 22,511 件のうち `Face.age` が入っているのは 126 件だけだった
    （2026-10-07）。画面には計算年齢が出ているので、それで絞れること。
    """
    connection = window.connection
    dialog, face_ids = _assigned_person_with_faces(
        connection,
        window,
        birth_date="2011-05-03",
        shooting_date="2017-12-16T18:46:32",  # 6歳
        source=db.ASSIGN_AUTO,
    )
    assert all(row["age"] is None for row in db.list_faces(connection)), "年齢は未設定"

    dialog.min_age.setValue(6)
    dialog.max_age.setValue(6)
    assert dialog.total == len(face_ids), "計算年齢 6歳 で全件残る"

    dialog.min_age.setValue(7)
    dialog.max_age.setValue(7)
    assert dialog.total == 0, "7歳では1件も残らない"


def test_the_calculated_age_window_respects_the_birthday_itself(window):
    """**誕生日の当日に年齢が上がる。** 境界で1年ぶんずれないこと。"""
    connection = window.connection
    dialog, _ = _assigned_person_with_faces(
        connection,
        window,
        birth_date="2011-05-03",
        shooting_date="2017-05-02T12:00:00",  # 誕生日の前日 → まだ5歳
        source=db.ASSIGN_AUTO,
    )

    dialog.min_age.setValue(5)
    dialog.max_age.setValue(5)
    assert dialog.total > 0, "前日は5歳"

    connection.execute("UPDATE Media SET shooting_date = '2017-05-03T12:00:00'")
    connection.commit()
    dialog.reload()
    assert dialog.total == 0, "当日は6歳なので、5歳の絞り込みからは外れる"
    dialog.min_age.setValue(6)
    dialog.max_age.setValue(6)
    assert dialog.total > 0


def test_faces_with_no_age_at_all_are_left_out_unless_asked_for(window):
    """**年齢を出せない顔は、範囲を指定したら外す。**

    「7〜9歳」と指定したとき、年齢の分からない顔はその範囲に入るとは言えない。
    チェックを入れれば戻る。
    """
    connection = window.connection
    dialog, face_ids = _assigned_person_with_faces(
        connection,
        window,
        birth_date="2011-05-03",
        shooting_date="2017-12-16T18:46:32",
        source=db.ASSIGN_AUTO,
    )
    # 1件だけ撮影日時を壊して、年齢を出せなくする。
    connection.execute(
        "UPDATE Media SET shooting_date = 'TTTT-TT-TTTTT:TT:TT' WHERE id ="
        " (SELECT media_id FROM Face WHERE id = ?)", (face_ids[0],)
    )
    connection.commit()
    dialog.reload()

    assert not dialog.include_unknown_age.isChecked(), "既定は含めない"
    dialog.min_age.setValue(6)
    dialog.max_age.setValue(6)
    without = dialog.total

    dialog.include_unknown_age.setChecked(True)

    assert dialog.total > without, "チェックを入れると年齢不明が戻る"


def test_clearing_the_age_filter_shows_everything_again(window):
    """**「指定なし」に戻せば、年齢不明も含めて全部見える。**

    年齢を入れていない顔が一覧から永久に消えてしまわないこと。
    """
    connection = window.connection
    dialog, face_ids = _assigned_person_with_faces(
        connection,
        window,
        birth_date=None,  # 誕生日が無いので年齢は1件も出せない
        shooting_date="2017-12-16T18:46:32",
        source=db.ASSIGN_AUTO,
    )

    dialog.min_age.setValue(6)
    dialog.max_age.setValue(6)
    assert dialog.total == 0, "年齢を出せないので、範囲を指定すると残らない"

    dialog.min_age.setValue(-1)
    dialog.max_age.setValue(-1)

    assert dialog.total == len(face_ids)


def test_not_this_person_records_the_rejection_and_clears_the_assignment(window):
    """**除外は2種類ある。** こちらは「その人物ではない」。

    実データでは、${PERSON_4}の自動割り当てを見直して解除した 1,785 件が
    `match` を流すと戻ってくる状態だった。**解除では判断が残らない。**
    """
    connection = window.connection
    dialog, face_ids = _assigned_person_with_faces(
        connection,
        window,
        birth_date="2011-05-03",
        shooting_date="2017-12-16T18:46:32",
        source=db.ASSIGN_AUTO,
    )
    person_id = dialog.person["id"]
    dialog.face_list.selectAll()

    dialog._reject_for_person_selected()

    assert db.count_person_rejections(connection, person_id) == len(face_ids)
    rows = {row["id"]: row for row in db.list_faces(connection)}
    assert all(rows[f]["person_id"] is None for f in face_ids), "割り当ては外れる"
    assert all(rows[f]["assign_source"] is None for f in face_ids), "除外にはしない"


def test_nobody_s_face_is_a_different_button_and_asks_first(window, monkeypatch):
    """**「誰でもない顔」はどの人物にも付かなくなる。** 取り返しがつきにくいので確認する。"""
    connection = window.connection
    dialog, face_ids = _assigned_person_with_faces(
        connection,
        window,
        birth_date="2011-05-03",
        shooting_date="2017-12-16T18:46:32",
        source=db.ASSIGN_AUTO,
    )
    dialog.face_list.selectAll()
    asked = {}

    def fake_question(parent, title, text, *args, **kwargs):
        asked["text"] = text
        return photoarchive_gui.QMessageBox.StandardButton.Yes

    monkeypatch.setattr(photoarchive_gui.QMessageBox, "question", fake_question)

    dialog._reject_selected()

    assert "どの人物にも自動で付かなくなります" in asked["text"]
    assert "この人物ではない" in asked["text"], "もう一方のボタンを案内すること"
    rows = {row["id"]: row for row in db.list_faces(connection)}
    assert all(rows[f]["assign_source"] == db.ASSIGN_REJECTED for f in face_ids)
    # こちらは否定の記録ではない。
    assert db.count_person_rejections(connection, dialog.person["id"]) == 0


def test_saying_no_to_the_confirmation_changes_nothing(window, monkeypatch):
    """確認で「いいえ」なら何も起きないこと。"""
    connection = window.connection
    dialog, face_ids = _assigned_person_with_faces(
        connection,
        window,
        birth_date="2011-05-03",
        shooting_date="2017-12-16T18:46:32",
        source=db.ASSIGN_AUTO,
    )
    dialog.face_list.selectAll()
    monkeypatch.setattr(
        photoarchive_gui.QMessageBox,
        "question",
        lambda *a, **k: photoarchive_gui.QMessageBox.StandardButton.No,
    )

    dialog._reject_selected()

    rows = {row["id"]: row for row in db.list_faces(connection)}
    assert all(rows[f]["assign_source"] == db.ASSIGN_AUTO for f in face_ids)


def test_the_rejections_can_be_listed_and_undone(window):
    """**押し間違いから戻れること。** 種別から見直せる。"""
    connection = window.connection
    dialog, face_ids = _assigned_person_with_faces(
        connection,
        window,
        birth_date="2011-05-03",
        shooting_date="2017-12-16T18:46:32",
        source=db.ASSIGN_AUTO,
    )
    person_id = dialog.person["id"]
    dialog.face_list.selectAll()
    dialog._reject_for_person_selected()

    _select_source(dialog, photoarchive_gui.NOT_THIS_PERSON_FILTER)

    assert dialog.total == len(face_ids)
    assert dialog.face_list.count() == len(face_ids)
    assert "この人物ではない" in dialog.page_label.text()

    dialog.face_list.selectAll()
    dialog._undo_rejection_selected()

    assert db.count_person_rejections(connection, person_id) == 0


def test_the_rejection_list_still_shows_the_calculated_age(window):
    """見直すときも年齢が要る。**年齢が誤りを見分ける手がかりだから。**"""
    connection = window.connection
    dialog, _ = _assigned_person_with_faces(
        connection,
        window,
        birth_date="2011-05-03",
        shooting_date="2017-12-16T18:46:32",
        source=db.ASSIGN_AUTO,
    )
    dialog.face_list.selectAll()
    dialog._reject_for_person_selected()

    _select_source(dialog, photoarchive_gui.NOT_THIS_PERSON_FILTER)

    assert all("(6歳)" in label for label in _labels(dialog)), _labels(dialog)


def _window_total(window):
    """いま画面が見ている件数。`MainWindow` は総数を局所変数で持つので数え直す。"""
    return db.count_faces(window.connection, **window._filter_arguments())


def _seed_months(connection, dates):
    """撮影年月の違う写真を1枚1顔で用意する。"""
    for index, shooting_date in enumerate(dates, start=100):
        media_id = db.save_media(
            connection,
            {
                "path": f"/photos/m{index}.jpg",
                "filename": f"m{index}.jpg",
                "type": "image",
                "file_hash": f"hash-m{index}",
                "file_size": 100,
                "created_time": "2026-01-01T00:00:00",
            },
        )
        connection.execute(
            "UPDATE Media SET shooting_date = ? WHERE id = ?", (shooting_date, media_id)
        )
        db.add_face(
            connection,
            media_id=media_id,
            bbox=(0, 10, 10, 0),
            embedding=[0.0] * db.EMBEDDING_DIM,
            embed_version=db.embedding_model.ACTIVE.version,
            thumbnail=b"",
        )
    connection.commit()


def _set_months(window, start=None, end=None, undated=False):
    """撮影年月の範囲を選ぶ。``None`` は「指定なし」。"""
    window.undated_only_box.setChecked(undated)
    for box, value in ((window.month_from_box, start), (window.month_to_box, end)):
        index = box.findText(value if value else photoarchive_gui.MONTH_ANY)
        assert index >= 0, f"年月に {value} が無い"
        box.setCurrentIndex(index)


def test_the_unassigned_list_can_be_filtered_by_a_month_range(tmp_path):
    """**いつからいつまでで絞れること。** 両端を含む。

    家族の写っていない写真は時期でまとまっている（結婚式・旅行先の他人）ので、
    その期間だけを開いてまとめて除外できる。
    """
    database = tmp_path / "gui.db"
    connection = db.ensure_database(str(database))
    _seed_months(connection, ["2015-07-01T10:00:00", "2015-08-14T10:00:00",
                              "2015-09-20T10:00:00", "2020-01-02T10:00:00"])
    connection.close()
    window = photoarchive_gui.MainWindow(str(database))
    try:
        assert _window_total(window) == 4, "初期状態は絞らない"

        _set_months(window, "2015-07", "2015-09")

        assert _window_total(window) == 3, "両端を含む"
        assert window._filter_arguments()["month_from"] == "2015-07"
        assert window._filter_arguments()["month_to"] == "2015-09"
        assert window.face_list.count() == 3, "一覧にも効いていること"
    finally:
        window.connection.close()


def test_only_one_end_of_the_range_can_be_given(tmp_path):
    """**片方だけの指定ができること。**「2015-08 以降すべて」など。"""
    database = tmp_path / "gui.db"
    connection = db.ensure_database(str(database))
    _seed_months(connection, ["2015-07-01T10:00:00", "2015-08-14T10:00:00",
                              "2020-01-02T10:00:00"])
    connection.close()
    window = photoarchive_gui.MainWindow(str(database))
    try:
        _set_months(window, start="2015-08")
        assert _window_total(window) == 2
        assert "month_to" not in window._filter_arguments()

        _set_months(window, end="2015-07")
        assert _window_total(window) == 1
        assert "month_from" not in window._filter_arguments()
    finally:
        window.connection.close()


def test_the_range_is_kept_in_order(tmp_path):
    """**上下を逆にしたまま空の一覧を見せない。**

    黙って0件にすると、絞り込みが壊れているように見える。
    """
    database = tmp_path / "gui.db"
    connection = db.ensure_database(str(database))
    _seed_months(connection, ["2015-07-01T10:00:00", "2020-01-02T10:00:00"])
    connection.close()
    window = photoarchive_gui.MainWindow(str(database))
    try:
        _set_months(window, "2015-07", "2015-07")

        # 下限を上限より後ろにする → 上限が押し上げられる
        window.month_from_box.setCurrentIndex(window.month_from_box.findText("2020-01"))

        assert window.month_to_box.currentText() == "2020-01"
        assert _window_total(window) == 1
    finally:
        window.connection.close()


def test_the_month_list_holds_only_months_that_have_faces(tmp_path):
    """**選んでも1件も出ない月を並べない。**"""
    database = tmp_path / "gui.db"
    connection = db.ensure_database(str(database))
    _seed_months(connection, ["2015-08-14T10:00:00", "2020-01-02T10:00:00"])
    media_id = db.save_media(
        connection,
        {
            "path": "/photos/none.jpg",
            "filename": "none.jpg",
            "type": "image",
            "file_hash": "hash-none",
            "file_size": 100,
            "created_time": "2026-01-01T00:00:00",
        },
    )
    connection.execute(
        "UPDATE Media SET shooting_date = '2018-06-01T10:00:00' WHERE id = ?", (media_id,)
    )
    connection.commit()
    connection.close()
    window = photoarchive_gui.MainWindow(str(database))
    try:
        months = [
            window.month_from_box.itemText(i)
            for i in range(window.month_from_box.count())
        ]
        assert months == [photoarchive_gui.MONTH_ANY, "2020-01", "2015-08"]
    finally:
        window.connection.close()


def test_undated_photos_can_be_picked_out_on_their_own(tmp_path):
    """**撮影日時なしは範囲では出せない。** 別に選べること。"""
    database = tmp_path / "gui.db"
    connection = db.ensure_database(str(database))
    _seed_months(connection, ["2015-08-14T10:00:00", "TTTT-TT-TTTTT:TT:TT", None])
    connection.close()
    window = photoarchive_gui.MainWindow(str(database))
    try:
        months = [
            window.month_from_box.itemText(i)
            for i in range(window.month_from_box.count())
        ]
        assert months == [photoarchive_gui.MONTH_ANY, "2015-08"]
        assert "TTTT-TT" not in months, "壊れた日付を月として並べない"

        # 広い範囲を指定しても、読めない顔は入らない。
        _set_months(window, "2015-08", "2015-08")
        assert _window_total(window) == 1

        _set_months(window, undated=True)

        assert window._filter_arguments()["undated_only"] is True
        assert _window_total(window) == 2
        assert not window.month_from_box.isEnabled(), "範囲とは排他だと見た目で分かる"
    finally:
        window.connection.close()


def test_the_undated_choice_is_hidden_when_every_photo_is_dated(tmp_path):
    """無い選択肢は出さない。"""
    database = tmp_path / "gui.db"
    connection = db.ensure_database(str(database))
    _seed_months(connection, ["2015-08-14T10:00:00"])
    connection.close()
    window = photoarchive_gui.MainWindow(str(database))
    try:
        assert not window.undated_only_box.isVisible()
    finally:
        window.connection.close()


def test_choosing_a_month_goes_back_to_the_first_page(tmp_path, monkeypatch):
    """絞ったのに後ろのページのままだと空に見える。"""
    monkeypatch.setattr(photoarchive_gui, "PAGE_SIZE", 1)
    database = tmp_path / "gui.db"
    connection = db.ensure_database(str(database))
    _seed_months(connection, ["2015-08-14T10:00:00", "2015-08-20T10:00:00",
                              "2020-01-02T10:00:00"])
    connection.close()
    window = photoarchive_gui.MainWindow(str(database))
    try:
        window._next_page()
        assert window.page == 1

        _set_months(window, "2015-08", "2015-08")

        assert window.page == 0
    finally:
        window.connection.close()
