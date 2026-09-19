import os

import pytest
from PySide6.QtWidgets import QApplication

from photoarchive_ai import db
from photoarchive_ai import gui as photoarchive_gui

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


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
            embedding=[0.0] * 128,
            embed_version="test",
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
