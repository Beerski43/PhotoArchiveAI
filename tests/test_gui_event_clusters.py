"""行事（フォルダ×日）で絞り、束ごとにまとめて処理する画面（Issue #61）。

**ここで守っているのは「手本を巻き込まない」こと。** 束ねた結果をまとめて
押せる画面なので、1回の操作が数百件に効く。手本が消えると `match` の土台が
崩れるため、触るのは `assign_source IS NULL` の顔だけであることを固定する。
"""

import os

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from photoarchive_ai import clustering, db, embedding
from photoarchive_ai import gui as photoarchive_gui

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def _vector(angle: float) -> list:
    """向きだけが違う単位ベクトル。**コサイン距離が角度そのものになる。**"""
    values = [0.0] * embedding.ACTIVE.dimensions
    values[0] = float(np.cos(angle))
    values[1] = float(np.sin(angle))
    return values


def _add_media(connection, path: str, shooting_date: str) -> int:
    return db.save_media(
        connection,
        {
            "path": path,
            "filename": path.rsplit("/", 1)[-1],
            "type": "image",
            "file_hash": path,
            "file_size": 100,
            "created_time": "2011-04-16T10:00:00",
            "shooting_date": shooting_date,
        },
    )


def _add_face(connection, media_id: int, angle: float, **kwargs) -> int:
    return db.add_face(
        connection,
        media_id=media_id,
        bbox=(0, 10, 10, 0),
        embedding=_vector(angle),
        embed_version=embedding.ACTIVE.version,
        thumbnail=b"",
        quality_score=1.0,
        **kwargs,
    )


@pytest.fixture()
def seeded(tmp_path):
    """1つの行事に、近い顔3件と離れた顔2件。別の日にもう1件。"""
    path = tmp_path / "events.db"
    connection = db.ensure_database(str(path))
    wedding = _add_media(connection, "/photos/wedding/a.jpg", "2011-04-16T10:00:00")
    wedding_second = _add_media(connection, "/photos/wedding/b.jpg", "2011-04-16T11:00:00")
    next_day = _add_media(connection, "/photos/wedding/c.jpg", "2011-04-17T10:00:00")
    faces = {
        "near": [
            _add_face(connection, wedding, 0.0),
            _add_face(connection, wedding_second, 0.02),
        ],
        "far": [_add_face(connection, wedding, 2.0)],
        "next_day": [_add_face(connection, next_day, 0.0)],
    }
    connection.commit()
    yield connection, faces, str(path)
    connection.close()


def _open_dialog(connection, day="2011-04-16", **kwargs):
    return photoarchive_gui.EventClusterDialog(
        None, connection, "/photos/wedding", day, **kwargs
    )


# ---------------------------------------------------------------------------
# 行事の選択
# ---------------------------------------------------------------------------


def test_the_picker_offers_events_not_folders(seeded):
    connection, _faces, _path = seeded
    dialog = photoarchive_gui.EventPickerDialog(None, connection)
    labels = [
        dialog.event_list.item(row).text() for row in range(dialog.event_list.count())
    ]
    # **同じフォルダが日ごとに分かれていること。** これが PR #56 との違い。
    assert any("2011-04-16" in label for label in labels)
    assert any("2011-04-17" in label for label in labels)
    assert dialog.selected_event()[0] == "/photos/wedding"


def test_the_picker_filter_matches_what_is_on_screen(seeded):
    connection, _faces, _path = seeded
    dialog = photoarchive_gui.EventPickerDialog(None, connection)
    dialog.filter_edit.setText("2011-04-17")
    assert dialog.event_list.count() == 1
    assert dialog.selected_event() == ("/photos/wedding", "2011-04-17")


def test_the_main_window_filters_by_the_chosen_event(seeded):
    connection, faces, path = seeded
    window = photoarchive_gui.MainWindow(path)
    try:
        assert window.face_list.count() == 4
        window.event = ("/photos/wedding", "2011-04-16")
        window._reset_page()
        shown = {
            window.face_list.item(row).data(photoarchive_gui.Qt.UserRole)["id"]
            for row in range(window.face_list.count())
        }
        assert shown == set(faces["near"] + faces["far"])
    finally:
        window.connection.close()


def test_an_event_without_a_readable_day_is_filtered_by_the_undated_mark(tmp_path):
    """**`day=None` を「日で絞らない」と取り違えないこと。**

    取り違えると、日付の読めない顔を選んだつもりでフォルダ全体が対象になる。
    """
    path = tmp_path / "undated.db"
    connection = db.ensure_database(str(path))
    broken = _add_media(connection, "/photos/wedding/x.jpg", "TTTT-TT-TTTTT:TT:TT")
    good = _add_media(connection, "/photos/wedding/y.jpg", "2011-04-16T10:00:00")
    broken_face = _add_face(connection, broken, 0.0)
    _add_face(connection, good, 0.0)
    connection.commit()
    connection.close()

    window = photoarchive_gui.MainWindow(str(path))
    try:
        window.event = ("/photos/wedding", None)
        window._reset_page()
        shown = [
            window.face_list.item(row).data(photoarchive_gui.Qt.UserRole)["id"]
            for row in range(window.face_list.count())
        ]
        assert shown == [broken_face]
    finally:
        window.connection.close()


def test_the_cluster_dialog_bundles_only_undated_faces_of_an_undated_event(tmp_path):
    """**日付不明の行事を束ねたら、その顔だけを束ねること。**

    一覧の絞り込みは `None` を `db.UNDATED` に直していたのに、束ねる画面は
    `None` をそのまま渡していた。`None` は「日で絞らない」なので、**同じ
    フォルダの別の日の顔まで束に入り、まとめて押すとそちらにも効いた**
    （実データで日付つきの未割当 18,000 件が 363 フォルダで巻き込まれる。
    最悪の例は「日付不明 2 件」の行事に 985 件。PR #62 のレビュー指摘1）。

    **一覧の経路しか見ていなかったので、この経路は通っていなかった。**
    """
    path = tmp_path / "mix.db"
    connection = db.ensure_database(str(path))
    broken = _add_media(connection, "/photos/mix/broken.jpg", "TTTT-TT-TTTTT:TT:TT")
    dated = _add_media(connection, "/photos/mix/ok.jpg", "2011-04-16T10:00:00")
    undated_face = _add_face(connection, broken, 0.0)
    dated_face = _add_face(connection, dated, 0.0)
    connection.commit()
    try:
        dialog = photoarchive_gui.EventClusterDialog(None, connection, "/photos/mix", None)
        assert sorted(dialog.records) == [undated_face]
        bundled = {face_id for cluster in dialog.clusters for face_id in cluster.face_ids}
        assert bundled == {undated_face}
        assert dated_face not in dialog._pending_ids(0)
    finally:
        connection.close()


def test_the_two_paths_turn_an_event_into_the_same_filter(seeded):
    """一覧と束ねる画面が、**同じ変換**を通ること（`event_filters` 1か所）。"""
    connection, _faces, path = seeded
    window = photoarchive_gui.MainWindow(path)
    try:
        window.event = ("/photos/wedding", None)
        filters = window._filter_arguments()
        assert filters["day"] is db.UNDATED
        assert photoarchive_gui.event_filters("/photos/wedding", None)["day"] is db.UNDATED
        assert (
            photoarchive_gui.event_filters("/photos/wedding", "2011-04-16")["day"]
            == "2011-04-16"
        )
    finally:
        window.connection.close()


# ---------------------------------------------------------------------------
# 束ねる
# ---------------------------------------------------------------------------


def test_the_dialog_bundles_only_the_chosen_day(seeded):
    connection, faces, _path = seeded
    dialog = _open_dialog(connection)
    bundled = {face_id for cluster in dialog.clusters for face_id in cluster.face_ids}
    assert bundled == set(faces["near"] + faces["far"])


def test_near_faces_land_in_one_cluster(seeded):
    connection, faces, _path = seeded
    dialog = _open_dialog(connection)
    sizes = sorted(cluster.size for cluster in dialog.clusters)
    assert sizes == [1, 2]
    biggest = dialog.clusters[0]
    assert set(biggest.face_ids) == set(faces["near"])


def test_rejected_and_auto_faces_are_left_out_of_the_bundles(seeded):
    """**人が判断した顔と、`match` が付けた顔は束ねない。**

    除外した顔を束ねても決定は減らず、自動割当を材料にすると誤りが次の判断の
    根拠になる。
    """
    connection, faces, _path = seeded
    person_id = db.add_person(connection, name="なつ")
    db.reject_faces(connection, faces["far"])
    db.assign_faces(connection, [faces["near"][1]], person_id, db.ASSIGN_AUTO)

    dialog = _open_dialog(connection)
    bundled = {face_id for cluster in dialog.clusters for face_id in cluster.face_ids}
    assert bundled == {faces["near"][0]}


def test_a_cluster_shows_the_teacher_it_contains(seeded):
    connection, faces, _path = seeded
    person_id = db.add_person(connection, name="なつ")
    db.assign_faces(connection, [faces["near"][0]], person_id, db.ASSIGN_MANUAL)

    dialog = _open_dialog(connection)
    assert dialog.cluster_list.item(0).text().startswith("束 1 — 2 件")
    assert "手本: なつ" in dialog.cluster_list.item(0).text()


def test_assigning_a_cluster_never_touches_the_teacher_in_it(seeded, monkeypatch):
    """**いちばん大事な一線。** 束に手本が混ざっていても、手本は触らない。"""
    connection, faces, _path = seeded
    natsu = db.add_person(connection, name="なつ")
    hiyori = db.add_person(connection, name="ひより")
    teacher, other = faces["near"]
    db.assign_faces(connection, [teacher], natsu, db.ASSIGN_MANUAL, age=3)

    dialog = _open_dialog(connection)
    monkeypatch.setattr(photoarchive_gui, "FaceAgeDialog", _accepting_age_dialog(7))
    dialog.person_box.setCurrentIndex(
        [dialog.person_box.itemText(i) for i in range(dialog.person_box.count())].index("ひより")
    )
    dialog.cluster_list.setCurrentRow(0)
    dialog._assign_cluster()

    kept = db.get_face(connection, teacher)
    assert (kept["person_id"], kept["assign_source"], kept["age"]) == (
        natsu,
        db.ASSIGN_MANUAL,
        3,
    )
    moved = db.get_face(connection, other)
    assert (moved["person_id"], moved["assign_source"], moved["age"]) == (
        hiyori,
        db.ASSIGN_MANUAL,
        7,
    )


def test_a_cluster_becomes_done_after_it_is_assigned(seeded, monkeypatch):
    """押したあと、その束が「済」になること。**同じ束を二度押さないため。**"""
    connection, faces, _path = seeded
    db.add_person(connection, name="なつ")
    dialog = _open_dialog(connection)
    monkeypatch.setattr(photoarchive_gui, "FaceAgeDialog", _accepting_age_dialog(None))
    dialog.cluster_list.setCurrentRow(0)
    dialog._assign_cluster()

    assert "済" in dialog.cluster_list.item(0).text()
    assert dialog._pending_ids(0) == []
    assert dialog.assign_button.isEnabled() is False


def test_rejecting_a_cluster_only_rejects_the_pending_faces(seeded, monkeypatch):
    connection, faces, _path = seeded
    person_id = db.add_person(connection, name="なつ")
    teacher, other = faces["near"]
    db.assign_faces(connection, [teacher], person_id, db.ASSIGN_MANUAL)
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )

    dialog = _open_dialog(connection)
    dialog.cluster_list.setCurrentRow(0)
    dialog._reject_cluster()

    assert db.get_face(connection, teacher)["assign_source"] == db.ASSIGN_MANUAL
    assert db.get_face(connection, other)["assign_source"] == db.ASSIGN_REJECTED


def test_only_one_page_of_thumbnails_is_read_at_a_time(seeded, monkeypatch):
    """**束が大きくてもサムネイルは1ページ分だけ読む。**

    実データの最大の束は 406 件。全部読むと画面が固まる（CLAUDE.md §8）。
    """
    connection, _faces, _path = seeded
    monkeypatch.setattr(photoarchive_gui, "CLUSTER_PAGE_SIZE", 1)
    dialog = _open_dialog(connection)
    dialog.cluster_list.setCurrentRow(0)
    assert dialog.face_list.count() == 1
    assert "1 / 2 ページ" in dialog.page_label.text()
    dialog._next_page()
    assert "2 / 2 ページ" in dialog.page_label.text()


def test_people_born_after_the_event_are_not_offered(seeded):
    """**その日に生まれていない人物は選べない。**

    まとめて押すときに画面に出るのは人物名だけなので、選べると気づけない。
    """
    connection, _faces, _path = seeded
    db.add_person(connection, name="なつ", birth_date="2001-05-03")
    db.add_person(connection, name="まだ", birth_date="2020-01-01")

    dialog = _open_dialog(connection)
    offered = [dialog.person_box.itemText(i) for i in range(dialog.person_box.count())]
    assert offered == ["なつ"]


def test_people_without_a_birth_date_stay_in_the_list(seeded):
    """誕生日が未設定の人物は残す。**分からないことを理由に消さない。**"""
    connection, _faces, _path = seeded
    db.add_person(connection, name="不明")
    dialog = _open_dialog(connection)
    assert [dialog.person_box.itemText(i) for i in range(dialog.person_box.count())] == ["不明"]


def test_too_many_faces_is_reported_instead_of_truncated(seeded, monkeypatch):
    """上限を超えた行事は、**黙って先頭だけ束ねない。**"""
    connection, _faces, path = seeded
    monkeypatch.setattr(clustering, "MAX_FACES", 1)
    warned = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *args, **kwargs: warned.append(args))
    )
    window = photoarchive_gui.MainWindow(path)
    try:
        window.event = ("/photos/wedding", "2011-04-16")
        window._cluster_event()
        assert warned
    finally:
        window.connection.close()


# ---------------------------------------------------------------------------
# 行事ごとのまとめ処理
# ---------------------------------------------------------------------------


def test_bulk_reject_covers_the_whole_event_but_not_other_days(seeded, monkeypatch):
    connection, faces, path = seeded
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    window = photoarchive_gui.MainWindow(path)
    try:
        window.event = ("/photos/wedding", "2011-04-16")
        window._reset_page()
        window._bulk_event_action()
        for face_id in faces["near"] + faces["far"]:
            assert db.get_face(window.connection, face_id)["assign_source"] == db.ASSIGN_REJECTED
        assert db.get_face(window.connection, faces["next_day"][0])["assign_source"] is None
    finally:
        window.connection.close()


def test_bulk_buttons_stay_disabled_until_an_event_is_chosen(seeded):
    """**行事を選ばないと押せない。** 選ばないと対象が未割当の全件になる。"""
    connection, _faces, path = seeded
    window = photoarchive_gui.MainWindow(path)
    try:
        assert window.bulk_event_button.isEnabled() is False
        assert window.cluster_event_button.isEnabled() is False
        assert "行事を選ぶ" in window.bulk_event_button.toolTip()
        window.event = ("/photos/wedding", "2011-04-16")
        window._sync_event_controls()
        assert window.bulk_event_button.isEnabled() is True
        assert window.cluster_event_button.isEnabled() is True
    finally:
        window.connection.close()


def test_the_bulk_button_changes_meaning_with_the_view(seeded):
    """**表示を切り替えたら、まとめて処理の意味も変える。**

    表示は左の一覧で選ぶ（#67）。
    """
    connection, _faces, path = seeded
    window = photoarchive_gui.MainWindow(path)
    try:
        window.event = ("/photos/wedding", "2011-04-16")
        window._sync_event_controls()
        assert "未割当をすべて除外" in window.bulk_event_button.text()

        for row in range(window.person_list.count()):
            if (
                window.person_list.item(row).data(photoarchive_gui.SCOPE_ROLE)
                == photoarchive_gui.SCOPE_REJECTED
            ):
                window.person_list.setCurrentRow(row)
                break
        assert "除外をすべて取り消す" in window.bulk_event_button.text()
    finally:
        window.connection.close()


def _accepting_age_dialog(age):
    """年齢を入れて OK を押す `FaceAgeDialog` の代わり。"""

    class _Dialog:
        def __init__(self, parent=None, summary="", initial_age=None):
            pass

        def exec(self):
            return QDialog.Accepted

        def age(self):
            return age

    return _Dialog


# ---------------------------------------------------------------------------
# 束を割る
# ---------------------------------------------------------------------------


def test_splitting_a_cluster_uses_a_tighter_distance(seeded):
    """**大きい束に別人が混ざっていたら割れること。**

    実データでいちばん大きい行事は 380 件の束を作る。割れないと、その束は
    まとめて押せない（押すと誤って数百件に効く）。
    """
    connection, faces, _path = seeded
    wedding = db.get_face(connection, faces["near"][0])["media_id"]
    # 既定の 0.45 では同じ束に入るが、0.40 では割れる距離（約 0.42）に置く。
    import math

    angle = math.acos(1.0 - 0.42)
    middle = _add_face(connection, wedding, angle)
    connection.commit()

    dialog = _open_dialog(connection)
    row = next(
        index
        for index, cluster in enumerate(dialog.clusters)
        if middle in cluster.face_ids and cluster.size > 1
    )
    before = dialog.clusters[row].size
    dialog.cluster_list.setCurrentRow(row)
    dialog._split_cluster()

    assert len(dialog.clusters) > 1
    assert all(cluster.size < before for cluster in dialog.clusters)
    # **行事全体の顔を落とさない。**
    bundled = {face_id for cluster in dialog.clusters for face_id in cluster.face_ids}
    assert bundled == set(faces["near"] + faces["far"] + [middle])


def test_a_cluster_that_cannot_be_split_says_so(seeded):
    """割れなかったことを黙らない。**押して何も起きないと壊れて見える。**"""
    connection, _faces, _path = seeded
    dialog = _open_dialog(connection)
    dialog.cluster_list.setCurrentRow(0)
    dialog._split_cluster()
    assert "割れませんでした" in dialog.notice_label.text()


def test_a_single_face_cluster_cannot_be_split(seeded):
    connection, faces, _path = seeded
    dialog = _open_dialog(connection)
    row = next(
        index for index, cluster in enumerate(dialog.clusters) if cluster.size == 1
    )
    dialog.cluster_list.setCurrentRow(row)
    assert dialog.split_button.isEnabled() is False
