"""顔の割り当て画面の見え方（#118）。

- **右クリックのメニューを GUI のウィンドウの中に収める。** Windows から X11 転送で
  開くと、Qt の知っている画面と見えている画面が食い違い、人物が多いと
  「人物に割り当て」の下の方が画面から飛び出て選べなかった
- **左の人物の領域は狭く、広げたぶんは顔の一覧へ**（利用者「真ん中のサムネ一覧を広くしたい」）
"""

import os

import pytest
from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import QApplication, QMenu, QSplitter

from photoarchive_ai import db
from photoarchive_ai import gui as photoarchive_gui

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

#: メニューが縦に収まらない人数。
MANY_PERSONS = 40


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def window(tmp_path):
    database = tmp_path / "layout.db"
    connection = db.ensure_database(str(database))
    media = db.save_media(
        connection,
        {
            "path": "/photos/2018/a.jpg",
            "filename": "a.jpg",
            "type": "image",
            "file_hash": "h1",
            "file_size": 100,
            "created_time": "2026-01-01T00:00:00",
            "shooting_date": "2018-07-07T10:00:00",
        },
    )
    db.add_face(
        connection,
        media_id=media,
        bbox=(0, 10, 10, 0),
        embedding=[0.0] * db.EMBEDDING_DIM,
        embed_version=db.embedding_model.ACTIVE.version,
        thumbnail=b"",
    )
    for number in range(1, MANY_PERSONS + 1):
        db.add_person(connection, f"人物{number:02d}", "")
    connection.commit()
    connection.close()

    window = photoarchive_gui.MainWindow(str(database))
    # 画面（offscreen は 800x800）より小さく、下寄りに置く。
    window.setGeometry(100, 300, 700, 400)
    window.show()
    QApplication.processEvents()
    window.face_list.selectAll()
    yield window
    window.close()
    window.connection.close()


def _assign_submenu(menu: QMenu) -> QMenu:
    return next(
        action.menu()
        for action in menu.actions()
        if action.menu() is not None
        and action.text() == photoarchive_gui.ASSIGN_MENU
    )


def _shown(menu: QMenu, at: QPoint) -> QRect:
    menu.popup(at)
    QApplication.processEvents()
    return menu.geometry()


def test_the_assign_menu_stays_inside_the_window_even_with_many_persons(window):
    """**ウィンドウの下端近くで右クリックしても、人物の一覧が飛び出さない。**"""
    bounds = photoarchive_gui.visible_bounds(window)
    menu = window._build_face_menu()
    submenu = _assign_submenu(menu)
    assert len(submenu.actions()) == MANY_PERSONS

    bottom = window.mapToGlobal(QPoint(200, window.height() - 10))
    assert bounds.contains(_shown(menu, bottom))
    assert bounds.contains(_shown(submenu, bottom + QPoint(150, 0)))
    menu.hide()


def test_persons_beyond_the_window_height_scroll_instead_of_disappearing(window):
    """**列に折り返さずスクロールにする。** 折り返しは Qt が自分の知っている
    画面の高さで決めるので、ウィンドウに合わせて縮めると下の人物が見えないまま残る。
    """
    submenu = _assign_submenu(window._build_face_menu())
    geometry = _shown(submenu, window.mapToGlobal(QPoint(10, 10)))
    last = submenu.actionGeometry(submenu.actions()[-1])

    assert geometry.height() <= photoarchive_gui.visible_bounds(window).height()
    # 1列のまま（最後の人物も1人目と同じ列にいて、スクロールした先にある）
    assert last.left() == submenu.actionGeometry(submenu.actions()[0]).left()
    assert last.top() > geometry.height()
    submenu.hide()


def test_the_person_menu_is_kept_inside_the_window_too(window, monkeypatch):
    """左の一覧の右クリックも同じ扱い。"""
    shown = []
    attach = window.menu_keeper.attach

    class _NotOpened:
        def exec(self, *args):
            pass

    def _attach(menu):
        shown.append(attach(menu))
        return _NotOpened()  # 本物の exec はテストを止める

    monkeypatch.setattr(window.menu_keeper, "attach", _attach)
    window._show_person_menu(QPoint(5, 5))
    assert len(shown) == 1
    assert "menu-scrollable: 1" in shown[0].styleSheet()


def test_clamp_into_moves_a_rect_back_inside():
    bounds = QRect(100, 100, 500, 400)
    assert photoarchive_gui.clamp_into(QRect(300, 450, 100, 200), bounds) == QPoint(300, 300)
    assert photoarchive_gui.clamp_into(QRect(550, 50, 100, 100), bounds) == QPoint(500, 100)
    # 枠より高いものは上端に揃える
    assert photoarchive_gui.clamp_into(QRect(200, 300, 100, 900), bounds) == QPoint(200, 100)


def test_the_person_panel_is_narrow_and_widening_goes_to_the_faces(window):
    """**左は既定で狭く、ウィンドウを広げたぶんはすべて顔の一覧へ。**"""
    window.resize(1180, 760)
    QApplication.processEvents()
    splitter = window.findChild(QSplitter)
    left_before, right_before = splitter.sizes()
    assert left_before == photoarchive_gui.PERSON_PANEL_WIDTH
    # 以前の既定（320px）より狭い
    assert left_before < 320

    window.resize(1600, 760)
    QApplication.processEvents()
    left_after, right_after = splitter.sizes()
    assert left_after == left_before
    assert right_after > right_before


def test_the_person_panel_can_be_narrowed_below_the_old_minimum(window):
    """ボタン3つが Qt の既定の最小（1つ 80px）を取って、左が 270px より狭くならなかった。"""
    panel = window.findChild(QSplitter).widget(0)
    assert panel.minimumSizeHint().width() <= photoarchive_gui.PERSON_PANEL_WIDTH
