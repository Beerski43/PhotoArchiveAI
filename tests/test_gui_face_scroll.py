"""顔の一覧のスクロール位置（#18）。

利用者「顔を右クリックで何かしら実行すると、表示されている一覧画面が勝手に中央付近に移動する。場所を維持してほしい」。
"""

import os

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from photoarchive_ai import db
from photoarchive_ai import gui as photoarchive_gui
from tests.test_blurry_faces import _face, _jpeg

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
FACES = 60


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture()
def shown(tmp_path, qt_app, monkeypatch):
    """人物の自動割り当ての顔を、1画面に収まらない数だけ並べて、下の方までスクロールした窓。"""
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)
    path = tmp_path / "scroll.db"
    connection = db.ensure_database(str(path))
    person = db.add_person(connection, "${PERSON_1}")
    for index in range(FACES):
        _face(connection, index, person_id=person, assign_source=db.ASSIGN_AUTO)
    connection.close()
    window = photoarchive_gui.MainWindow(str(path))
    window.resize(1000, 560)
    window.show()
    for row in range(window.person_list.count()):
        data = window.person_list.item(row).data(photoarchive_gui.Qt.UserRole)
        if data is not None and data["id"] == person:
            window.person_list.setCurrentRow(row)
    qt_app.processEvents()
    bar = window.face_list.verticalScrollBar()
    assert bar.maximum() > 300, "1画面に収まっていると確かめられない"
    bar.setValue(bar.maximum() // 2 + 100)
    qt_app.processEvents()
    yield window, bar
    window.close()


def _select_visible_face(window):
    """いま見えている顔を選ぶ（右クリックと同じ）。"""
    item = window.face_list.itemAt(window.face_list.viewport().rect().center())
    assert item is not None
    window.face_list.clearSelection()
    item.setSelected(True)
    window.face_list.setCurrentItem(item)
    return item


def test_an_operation_keeps_the_scroll_position(shown, qt_app):
    """**顔を処理しても一覧の場所は動かない。** 確定は顔が減らない操作。"""
    window, bar = shown
    _select_visible_face(window)
    before = bar.value()

    window.action_confirm.trigger()
    qt_app.processEvents()

    assert bar.value() == before


def test_an_operation_that_removes_faces_keeps_the_scroll_position(shown, qt_app):
    """顔が一覧から消える操作でも、場所を保つ。"""
    window, bar = shown
    _select_visible_face(window)
    count = window.face_list.count()
    before = bar.value()

    window.action_unassign.trigger()
    qt_app.processEvents()

    assert window.face_list.count() == count - 1
    assert bar.value() == before


def test_undo_keeps_the_scroll_position(shown, qt_app):
    window, bar = shown
    _select_visible_face(window)
    before = bar.value()
    window.action_confirm.trigger()
    qt_app.processEvents()

    window.action_undo.trigger()
    qt_app.processEvents()

    assert bar.value() == before


def test_the_position_is_clamped_when_the_list_gets_shorter(shown, qt_app):
    """短くなって元の場所が無ければ、いちばん下で止まる。"""
    window, bar = shown
    bar.setValue(bar.maximum())
    qt_app.processEvents()
    for row in range(window.face_list.count() // 2, window.face_list.count()):
        window.face_list.item(row).setSelected(True)
    old_maximum = bar.maximum()

    window.action_unassign.trigger()
    qt_app.processEvents()

    assert 0 < bar.maximum() < old_maximum
    assert bar.value() == bar.maximum()


def test_reloading_the_person_list_keeps_the_place_in_the_same_view(shown, qt_app, monkeypatch):
    """**裏の処理の終わりや人物の編集でも、同じ一覧なら場所を保つ**（PR #143 の指摘2）。

    取り込みは「そのあいだも顔の割り当てを続けられます」と案内している。`scan` が裏で終わった瞬間に
    作業中のページと場所を失っていた（`_reload_person_list` が必ず 1 ページ目の先頭へ戻していた）。
    """
    window, bar = shown
    monkeypatch.setattr(photoarchive_gui, "PAGE_SIZE", 25)
    window._reset_page()
    window._next_page()
    qt_app.processEvents()
    bar.setValue(bar.maximum())
    qt_app.processEvents()
    page, position = window.page, bar.value()
    assert page == 1 and position > 0
    order = window._current_order()

    window._reload_person_list()
    qt_app.processEvents()

    assert window.page == page
    assert bar.value() == position
    assert window._current_order() == order


def test_choosing_another_person_starts_from_the_top(shown, qt_app, monkeypatch):
    """別の人物へ移ったら先頭から。**人物を足して選んだときも、別の一覧なので先頭から。**"""
    window, bar = shown
    monkeypatch.setattr(photoarchive_gui, "PAGE_SIZE", 25)
    other = db.add_person(window.connection, "${PERSON_2}")
    media = window.face_list.item(0).data(photoarchive_gui.Qt.UserRole)["media_id"]
    for _ in range(30):
        db.add_face(
            window.connection, media_id=media, bbox=(0, 10, 10, 0), embedding=[0.0] * db.EMBEDDING_DIM,
            embed_version=db.embedding_model.ACTIVE.version, person_id=other,
            assign_source=db.ASSIGN_AUTO, thumbnail=_jpeg(),
        )
    window.connection.commit()
    window._reset_page()
    window._next_page()
    qt_app.processEvents()
    assert window.page == 1

    window._reload_person_list(select_person_id=other)
    qt_app.processEvents()

    assert window._current_person()["id"] == other
    assert window.page == 0
    assert bar.value() == 0


def test_moving_to_another_page_or_view_starts_from_the_top(shown, qt_app, monkeypatch):
    """**ページや表示を変えたら先頭から。** 場所を保つのは同じ一覧を作り直すときだけ。"""
    window, bar = shown
    monkeypatch.setattr(photoarchive_gui, "PAGE_SIZE", 25)
    window._reset_page()
    qt_app.processEvents()
    bar.setValue(bar.maximum())
    qt_app.processEvents()
    assert bar.value() > 0

    window._next_page()
    qt_app.processEvents()
    assert bar.value() == 0

    bar.setValue(bar.maximum())
    qt_app.processEvents()
    window.order_box.setCurrentIndex(window.order_box.currentIndex() + 1)
    qt_app.processEvents()
    assert bar.value() == 0
