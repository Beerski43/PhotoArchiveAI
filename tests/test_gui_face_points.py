"""顔の割り当ての一覧に顔の点を出し、点の順で並べる（#18）。"""

import os

import pytest
from PySide6.QtWidgets import QApplication

from photoarchive_ai import appearance, db, expression, gui, quality, scoring, selection
from tests.test_blurry_faces import _face

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    yield QApplication.instance() or QApplication([])


def _measure(connection, face_id, sharpness=200.0, eye_blink=0.0, mouth_smile=0.5):
    db.save_appearance(connection, [(face_id, appearance.Appearance(aligned=True, yaw=0.0, sharpness=sharpness))])
    db.save_expressions(connection, [(face_id, expression.Expression(True, eye_blink, mouth_smile))])


@pytest.fixture()
def database(tmp_path):
    """同じ人物の顔: 全部測った高い点と低い点・鮮明さだけ測った顔・何も測っていない顔。"""
    path = tmp_path / "points.db"
    connection = db.ensure_database(str(path))
    person = db.add_person(connection, "${PERSON_1}")
    ids = {}
    for index, name in enumerate(("low", "high", "partial", "unmeasured")):
        ids[name] = _face(connection, index, person_id=person, assign_source=db.ASSIGN_MANUAL)
    _measure(connection, ids["high"])
    _measure(connection, ids["low"], sharpness=10.0, eye_blink=1.0, mouth_smile=0.0)
    db.save_appearance(connection, [(ids["partial"], appearance.Appearance(aligned=True, yaw=0.0, sharpness=200.0))])
    connection.commit()
    connection.close()
    return path, person, ids


@pytest.fixture()
def window(database, tmp_path):
    path, person, ids = database
    rule = tmp_path / "rule.yml"
    rule.write_text("count_per_year: 5\n", encoding="utf-8")
    window = gui.AppWindow(str(path), rule_path=str(rule), output_root=str(tmp_path / "out"))
    yield window
    window.close()


def _show_person(window, person, order):
    """人物の顔を ``order`` で並べる。"""
    faces = window.assignment
    for row in range(faces.person_list.count()):
        data = faces.person_list.item(row).data(gui.Qt.UserRole)
        if data is not None and data["id"] == person:
            faces.person_list.setCurrentRow(row)
    index = next(i for i, (_, value) in enumerate(gui.ORDER_CHOICES) if value == order)
    faces.order_box.setCurrentIndex(index)
    faces._reset_page()


def _shown(window):
    faces = window.assignment
    return [faces.face_list.item(i).text() for i in range(faces.face_list.count())]


def _shown_ids(window):
    faces = window.assignment
    return [faces.face_list.item(i).data(gui.Qt.UserRole)["id"] for i in range(faces.face_list.count())]


def test_the_points_are_the_ones_select_uses(database):
    """**`select` と同じ式・同じ物差し**（写真でいちばん大きい顔と、写真の縦横）。"""
    path, _person, ids = database
    connection = db.ensure_database(str(path))
    media_id = db.faces_by_ids(connection, [ids["high"]])[0]["media_id"]
    # 同じ写真に大きい顔を足すと大きさの点が下がる。縁に接する大きさの写真なら見切れの点も下がる
    db.add_face(connection, media_id=media_id, bbox=(0, 100, 100, 0), embedding=[0.0] * db.EMBEDDING_DIM,
                embed_version=db.embedding_model.ACTIVE.version)
    db.save_photo_quality(connection, [(media_id, (20.0, 3.0, 0.5, 0.5, 10, 10))], quality.MODEL_VERSION)
    connection.commit()
    weights = scoring.Weights.default()
    faces = db.list_faces(connection)
    target = next(face for face in faces if face["id"] == ids["high"])

    point = selection.face_points(connection, faces, weights)[ids["high"]]

    assert point.score == pytest.approx(scoring.face_score(
        target, weights, db.largest_face_sizes(connection)[media_id],
        db.photo_quality(connection, quality.MODEL_VERSION)[media_id],
    ))
    assert point.score < scoring.face_score(target, weights), "写真の物差しが効いている"
    assert not point.partial
    connection.close()


def test_the_points_say_what_was_not_measured(database):
    path, _person, ids = database
    connection = db.ensure_database(str(path))
    points = selection.face_points(connection, db.list_faces(connection))
    assert not points[ids["high"]].partial
    assert points[ids["partial"]].partial and points[ids["partial"]].score is not None
    assert points[ids["unmeasured"]] == selection.FacePoint(None, False)
    connection.close()


def test_the_face_label_shows_the_points():
    assert gui.format_points(selection.FacePoint(71.6, False)) == "72点"
    assert gui.format_points(selection.FacePoint(71.6, True)) == "約72点"
    assert gui.format_points(selection.FacePoint(None, False)) == "点 未計測"


@pytest.mark.parametrize(
    "order, expected",
    [
        (selection.ORDER_POINTS_DESC, ["high", "partial", "low", "unmeasured"]),
        (selection.ORDER_POINTS_ASC, ["low", "partial", "high", "unmeasured"]),
    ],
)
def test_the_faces_can_be_ordered_by_points_with_unmeasured_last(window, database, order, expected):
    _path, person, ids = database
    _show_person(window, person, order)
    assert _shown_ids(window) == [ids[name] for name in expected]
    texts = _shown(window)
    assert texts[0].endswith("点") and texts[-1].endswith("点 未計測")
    assert "約" in texts[1]


def test_the_order_by_points_covers_every_page(window, database, monkeypatch):
    """**ページの中だけで並べない。** 全件を点で並べてから分ける。ページをまたいで重複も欠落もしない。"""
    _path, person, ids = database
    monkeypatch.setattr(gui, "PAGE_SIZE", 2)
    _show_person(window, person, selection.ORDER_POINTS_DESC)
    assert _shown_ids(window) == [ids["high"], ids["partial"]]
    window.assignment.page = 1
    window.assignment.reload_faces()
    assert _shown_ids(window) == [ids["low"], ids["unmeasured"]]


def test_the_points_appear_in_other_orders_too(window, database):
    _path, person, _ids = database
    _show_person(window, person, db.ORDER_SHOT_DESC)
    assert all("点" in text for text in _shown(window))


def test_the_points_follow_the_weights_on_the_selection_tab(window, database):
    """顔の点は「選ぶ」と同じく画面の重みで出す。タブを戻すと出し直す。**重みが同じなら読み直さない**（選択が外れる）。"""
    _path, person, ids = database
    faces = window.assignment
    _show_person(window, person, selection.ORDER_POINTS_DESC)
    faces.face_list.item(0).setSelected(True)
    window.tabs.setCurrentWidget(window.selection)
    window.tabs.setCurrentWidget(faces)
    assert faces.face_list.selectedItems(), "重みが同じなら選択は残る"

    before = dict(zip(_shown_ids(window), _shown(window)))
    window.tabs.setCurrentWidget(window.selection)
    for key in scoring.FACE_ITEMS:
        window.selection.weight_spins[key].setValue(0.0)
    window.selection.weight_spins["smile"].setValue(1.0)
    window.tabs.setCurrentWidget(faces)

    assert dict(zip(_shown_ids(window), _shown(window))) != before
    point = selection.face_points(faces.connection, db.faces_by_ids(faces.connection, [ids["high"]]),
                                  window.selection.current_weights())[ids["high"]]
    assert point.score == pytest.approx(100.0 * scoring.smile_level(0.5))


def test_photo_quality_can_read_only_some_photos(tmp_path):
    connection = db.ensure_database(str(tmp_path / "q.db"))
    ids = [_face(connection, index) for index in range(3)]
    media = [db.faces_by_ids(connection, [face_id])[0]["media_id"] for face_id in ids]
    measured = (20.0, 3.0, 0.5, 0.5, 40, 30)
    db.save_photo_quality(connection, [(media_id, measured) for media_id in media], quality.MODEL_VERSION)
    assert sorted(db.photo_quality(connection, quality.MODEL_VERSION, media[:2])) == sorted(media[:2])
    assert sorted(db.photo_quality(connection, quality.MODEL_VERSION)) == sorted(media)
    connection.close()
