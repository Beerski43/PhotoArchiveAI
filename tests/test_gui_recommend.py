"""人物の画面で、未割当の顔を「この人物に似た順」に並べる（#69）。

**探す時間を削るための画面。** 他人の顔の 99.1% が家族の写真に混ざっていて
まとめて消せない（2026-10-08 実測）ので、本人を1ページ目に集める。
"""

import math
import os

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from photoarchive_ai import db, recommend
from photoarchive_ai import gui as photoarchive_gui

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def _vector(angle_degrees):
    vector = np.zeros(db.EMBEDDING_DIM, dtype=np.float32)
    vector[0] = math.cos(math.radians(angle_degrees))
    vector[1] = math.sin(math.radians(angle_degrees))
    return vector


def _face(connection, name, angle, shooting_date="2020-01-01T00:00:00"):
    media_id = db.save_media(
        connection,
        {
            "path": f"/photos/{name}.jpg",
            "filename": f"{name}.jpg",
            "type": "image",
            "file_hash": name,
            "file_size": 1,
            "created_time": "2020-01-01T00:00:00",
            "shooting_date": shooting_date,
        },
    )
    return db.add_face(
        connection,
        media_id=media_id,
        bbox=(0, 10, 10, 0),
        embedding=_vector(angle),
        embed_version=db.embedding_model.ACTIVE.version,
        thumbnail=b"",
    )


@pytest.fixture()
def window_and_faces(tmp_path):
    """ひより（手本2件・2011年生まれ）と旺志朗（手本なし）、未割当の顔。"""
    database = tmp_path / "recommend.db"
    connection = db.ensure_database(str(database))
    hiyori = db.add_person(connection, "ひより", "長女", birth_date="2011-05-03")
    oshiro = db.add_person(connection, "旺志朗", "次男", birth_date="2019-01-01")
    teachers = [_face(connection, "t0", 0), _face(connection, "t5", 5)]
    db.assign_faces(connection, teachers, hiyori, db.ASSIGN_MANUAL)
    faces = {
        "遠い": _face(connection, "far", 80),
        "近い": _face(connection, "near", 2),
        "中くらい": _face(connection, "mid", 40),
        "誕生前": _face(connection, "before", 1, shooting_date="2010-01-01T00:00:00"),
        "否定済み": _face(connection, "denied", 3),
        "手本": teachers[0],
    }
    db.reject_faces_for_person(connection, [faces["否定済み"]], hiyori)
    connection.commit()
    connection.close()

    window = photoarchive_gui.MainWindow(str(database))
    yield window, faces, {"ひより": hiyori, "旺志朗": oshiro}
    window.connection.close()


def _select_person(window, person_id):
    for row in range(window.person_list.count()):
        person = window.person_list.item(row).data(Qt.UserRole)
        if person is not None and person["id"] == person_id:
            window.person_list.setCurrentRow(row)
            return
    raise AssertionError("左の一覧に見つからない")


def _show_unassigned(window, person_id):
    _select_person(window, person_id)
    window.source_box.setCurrentText(photoarchive_gui.UNASSIGNED_FOR_PERSON_FILTER)


def _ids(window):
    return [
        window.face_list.item(i).data(Qt.UserRole)["id"]
        for i in range(window.face_list.count())
    ]


def _choose_order(window, order):
    window.order_box.setCurrentIndex(
        next(i for i, (_, value) in enumerate(photoarchive_gui.ORDER_CHOICES) if value == order)
    )


def test_unassigned_faces_of_a_person_are_listed_most_similar_first(window_and_faces):
    """**種別「未割当」を選ぶと、この人物に似た順に並ぶ。** 選び直さなくてよい。"""
    window, faces, persons = window_and_faces

    _show_unassigned(window, persons["ひより"])

    assert window._current_order() == recommend.ORDER_SIMILAR
    assert _ids(window) == [faces["近い"], faces["中くらい"], faces["遠い"]]
    assert "全 3 件" in window.page_label.text()


def test_the_reverse_order_lists_the_least_similar_first(window_and_faces):
    window, faces, persons = window_and_faces
    _show_unassigned(window, persons["ひより"])

    _choose_order(window, recommend.ORDER_DISSIMILAR)

    assert _ids(window) == [faces["遠い"], faces["中くらい"], faces["近い"]]


def test_faces_that_cannot_be_this_person_are_not_offered(window_and_faces):
    """**誕生前の写真と「この人物ではない」と決めた顔は候補に出さない。**
    どれだけ似ていても（ここではどちらも手本のすぐ隣）。"""
    window, faces, persons = window_and_faces

    _show_unassigned(window, persons["ひより"])

    assert faces["誕生前"] not in _ids(window)
    assert faces["否定済み"] not in _ids(window)


def test_a_person_without_teachers_says_the_order_is_not_by_similarity(window_and_faces):
    """**手本が0件なら、そうと分かる表示を出す。** 黙って id 順に並べると、
    似た順のつもりで見てしまう。旺志朗の誕生前の顔も外れる。"""
    window, faces, persons = window_and_faces

    _show_unassigned(window, persons["旺志朗"])

    assert photoarchive_gui.NO_TEACHERS_NOTICE in window.page_label.text()
    assert _ids(window) == sorted(
        [faces["遠い"], faces["近い"], faces["中くらい"], faces["否定済み"]]
    )


def test_assigning_from_the_list_improves_the_order_right_away(window_and_faces):
    """**割り当てた顔は手本になり、次の並びに効く。** 計算し直しは増えた手本の分だけ。"""
    window, faces, persons = window_and_faces
    _show_unassigned(window, persons["ひより"])
    assert _ids(window)[-1] == faces["遠い"]

    window.assign_faces([faces["中くらい"]], persons["ひより"])
    window.connection.commit()
    window.reload_faces()

    assert _ids(window) == [faces["近い"], faces["遠い"]]
    # 遠い（80度）は、新しい手本（40度）のほうが近い。
    assert window.similarity.distances[faces["遠い"]] == pytest.approx(
        1 - math.cos(math.radians(40)), abs=1e-6
    )


def test_the_similarity_is_kept_while_paging(window_and_faces, monkeypatch):
    """**ページを送るたびに計算し直さない。**"""
    window, _faces, persons = window_and_faces
    _show_unassigned(window, persons["ひより"])
    calls = []
    original = recommend.nearest_distances
    monkeypatch.setattr(
        recommend,
        "nearest_distances",
        lambda *args, **kwargs: calls.append(1) or original(*args, **kwargs),
    )

    window._next_page()
    window._previous_page()
    window.reload_faces()

    assert calls == []


def test_similarity_orders_need_a_person(window_and_faces):
    """**比べる人物が居ない表示では、似た順を押せない。** 理由はツールチップに出す。"""
    window, _faces, persons = window_and_faces
    model = window.order_box.model()
    index = next(
        i for i, (_, value) in enumerate(photoarchive_gui.ORDER_CHOICES)
        if value == recommend.ORDER_SIMILAR
    )

    for row in range(3):
        window.person_list.setCurrentRow(row)
        assert model.item(index).isEnabled() is False
        assert "人物を選んでください" in model.item(index).toolTip()

    _show_unassigned(window, persons["ひより"])
    assert model.item(index).isEnabled() is True
    score = next(
        i for i, (_, value) in enumerate(photoarchive_gui.ORDER_CHOICES)
        if value == db.ORDER_SCORE_ASC
    )
    assert model.item(score).isEnabled() is False, "未割当の顔は確信度を持たない"


def test_a_persons_own_faces_can_be_listed_least_similar_first(window_and_faces):
    """**似ていない順は、割り当ての誤りを探す並びにもなる。** 自分自身は手本から外して測る
    （外さないと、手本が全部 0 で並ばない）。"""
    window, faces, persons = window_and_faces
    _select_person(window, persons["ひより"])
    window.assign_faces([faces["遠い"]], persons["ひより"])
    window.connection.commit()

    _choose_order(window, recommend.ORDER_DISSIMILAR)

    assert _ids(window)[0] == faces["遠い"]


def test_the_menu_on_a_persons_unassigned_faces_offers_what_makes_sense(window_and_faces):
    """**未割当の顔には戻す先が無い。** 出すのは「この人物ではない」と「誰でもない顔」。
    割り当て先は「人物に割り当て」（1〜9）。"""
    window, faces, persons = window_and_faces
    _show_unassigned(window, persons["ひより"])
    window.face_list.item(0).setSelected(True)

    assert [action.text() for action in window._menu_actions()] == [
        photoarchive_gui.ACTION_NOT_THIS_PERSON,
        photoarchive_gui.ACTION_REJECT,
    ]
    assert window.action_unassign.isEnabled() is False
    menu = window._build_face_menu()
    assert menu.actions()[0].text() == photoarchive_gui.ASSIGN_MENU


def test_not_this_person_removes_the_face_from_the_candidates(window_and_faces):
    """**「この人物ではない」と押したら、その人物の候補から消える。**"""
    window, faces, persons = window_and_faces
    _show_unassigned(window, persons["ひより"])
    window.face_list.item(0).setSelected(True)

    window._reject_for_person_selected()

    assert faces["近い"] not in _ids(window)


def test_rejecting_a_persons_unassigned_face_does_not_ask_for_confirmation(
    window_and_faces, monkeypatch
):
    """未割当の除外は毎件通る操作で、**確認を挟むと作業そのものが遅くなる。**"""
    window, faces, persons = window_and_faces
    _show_unassigned(window, persons["ひより"])
    window.face_list.item(0).setSelected(True)
    monkeypatch.setattr(
        photoarchive_gui.QMessageBox,
        "question",
        lambda *args, **kwargs: pytest.fail("確認を出さない"),
    )

    window._reject_selected()

    assert faces["近い"] not in _ids(window)


def test_switching_back_from_unassigned_restores_the_persons_order(window_and_faces):
    window, _faces, persons = window_and_faces
    _show_unassigned(window, persons["ひより"])

    window.source_box.setCurrentIndex(0)

    assert window._current_order() == db.ORDER_AGE
