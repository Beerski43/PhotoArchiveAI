"""顔の操作を元に戻す・やり直す（`undo.py`・#116）。GUI を通さずに確かめる。"""

import pytest

from photoarchive_ai import db, undo


def _seed(connection, count: int):
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
    ids = [
        db.add_face(
            connection,
            media_id=media_id,
            bbox=(0, 10, 10, 0),
            embedding=[0.0] * db.EMBEDDING_DIM,
            embed_version=db.embedding_model.ACTIVE.version,
            thumbnail=b"",
        )
        for _ in range(count)
    ]
    connection.commit()
    return ids


@pytest.fixture()
def connection(tmp_path):
    connection = db.ensure_database(str(tmp_path / "undo.db"))
    yield connection
    connection.close()


def _state(connection, face_ids):
    return undo.capture(connection, face_ids)


def test_an_assignment_is_undone_and_redone_with_every_column(connection):
    """年齢と割り当ての時刻まで戻る。やり直すと、操作の直後と同じになる。"""
    faces = _seed(connection, 3)
    person = db.add_person(connection, "父")
    db.assign_faces(connection, faces[:1], person, db.ASSIGN_MANUAL, age=4)
    history = undo.History()
    before = _state(connection, faces)

    history.record(
        connection, "割り当てた", faces,
        lambda: db.assign_faces(connection, faces, person, db.ASSIGN_MANUAL, age=7),
    )
    after = _state(connection, faces)
    assert after != before

    step = history.undo(connection)
    assert step.label == "割り当てた"
    assert _state(connection, faces) == before
    assert db.get_face(connection, faces[0])["age"] == 4
    assert db.get_face(connection, faces[1])["person_id"] is None

    history.redo(connection)
    assert _state(connection, faces) == after


def test_not_this_person_and_its_record_are_undone(connection):
    """「この人物ではない」の記録（`FaceRejection`）も戻る。手本も元の人物へ戻る。"""
    faces = _seed(connection, 2)
    person = db.add_person(connection, "父")
    db.assign_faces(connection, faces, person, db.ASSIGN_MANUAL)
    history = undo.History()

    history.record(
        connection, "この人物ではない", faces,
        lambda: db.reject_faces_for_person(connection, faces, person),
    )
    assert db.load_person_rejections(connection) == {faces[0]: {person}, faces[1]: {person}}

    history.undo(connection)

    assert db.load_person_rejections(connection) == {}
    assert {db.get_face(connection, face)["person_id"] for face in faces} == {person}


def test_clearing_a_rejection_is_undone_with_its_original_time(connection):
    """「この人物ではない」の取り消し（打鍵 Z）も戻せる。記録の時刻も元のまま。"""
    faces = _seed(connection, 1)
    person = db.add_person(connection, "父")
    db.reject_faces_for_person(connection, faces, person)
    recorded = _state(connection, faces).rejections
    history = undo.History()

    history.record(
        connection, "取り消した", faces,
        lambda: db.clear_person_rejections(connection, faces, person),
    )
    history.undo(connection)

    assert _state(connection, faces).rejections == recorded


def test_a_penalty_mark_is_undone_back_to_unassigned(connection):
    """未分類 → 他人(減点)（顔認証外にしてから印）を1回で戻す。"""
    faces = _seed(connection, 2)
    history = undo.History()

    def work():
        db.reject_faces(connection, faces)
        db.set_face_penalty(connection, faces, True)

    history.record(connection, "他人(減点)にした", faces, work)
    history.undo(connection)

    for face in faces:
        row = db.get_face(connection, face)
        assert (row["assign_source"], row["penalize"]) == (None, None)


def test_an_operation_that_changes_nothing_is_not_recorded(connection):
    """戻しても何も起きない操作は積まない（Ctrl+Z が空振りに見える）。"""
    faces = _seed(connection, 2)
    history = undo.History()

    history.record(connection, "戻した", faces, lambda: db.unassign_faces(connection, faces))

    assert not history.can_undo()


def test_only_the_newest_steps_are_kept(connection):
    faces = _seed(connection, 3)
    history = undo.History(max_steps=2)
    for face in faces:
        history.record(connection, f"顔 {face}", [face], lambda face=face: db.reject_faces(connection, [face]))

    assert [step.label for step in history.undo_steps] == [f"顔 {faces[1]}", f"顔 {faces[2]}"]
    assert undo.MAX_STEPS == 50


def test_a_new_operation_drops_what_could_be_redone(connection):
    faces = _seed(connection, 2)
    history = undo.History()
    history.record(connection, "1", faces[:1], lambda: db.reject_faces(connection, faces[:1]))
    history.undo(connection)
    assert history.can_redo()

    history.record(connection, "2", faces[1:], lambda: db.reject_faces(connection, faces[1:]))

    assert not history.can_redo()


def test_a_face_changed_afterwards_is_not_overwritten(connection):
    """**戻す前に確かめる。** 操作のあとで `match` などが同じ顔を書き換えていたら、戻さない。

    古い控えを書くと、その後の結果を黙って上書きする。
    """
    faces = _seed(connection, 2)
    person = db.add_person(connection, "父")
    history = undo.History()
    history.record(
        connection, "顔認証外にした", faces, lambda: db.reject_faces(connection, faces)
    )
    # 別の画面・CLI・GUI の match が、同じ顔を書き換えた
    db.assign_faces(connection, faces[:1], person, db.ASSIGN_AUTO, assign_score=0.9)
    changed = _state(connection, faces)

    with pytest.raises(undo.UndoConflict, match="割り当てが変わっています"):
        history.undo(connection)

    assert _state(connection, faces) == changed
    assert not history.can_undo() and not history.can_redo()


def test_a_deleted_person_is_not_written_back(connection):
    """**消えた人物の id を書き戻さない。** 解除のあとで人物を削除すると、顔は解除のままでも戻せない。"""
    faces = _seed(connection, 1)
    person = db.add_person(connection, "父")
    db.assign_faces(connection, faces, person, db.ASSIGN_MANUAL)
    history = undo.History()
    history.record(connection, "解除した", faces, lambda: db.unassign_faces(connection, faces))
    db.delete_person(connection, person)

    with pytest.raises(undo.UndoConflict, match="削除されています"):
        history.undo(connection)

    assert db.get_face(connection, faces[0])["person_id"] is None


def test_an_older_step_on_other_faces_survives_a_conflict(connection):
    """戻せない操作だけを捨てる。**別の顔の、それより古い操作は戻せる。**"""
    faces = _seed(connection, 2)
    history = undo.History()
    history.record(connection, "1", faces[:1], lambda: db.reject_faces(connection, faces[:1]))
    history.record(connection, "2", faces[1:], lambda: db.reject_faces(connection, faces[1:]))
    db.unassign_faces(connection, faces[1:])

    with pytest.raises(undo.UndoConflict):
        history.undo(connection)
    assert history.undo(connection).label == "1"
    assert db.get_face(connection, faces[0])["assign_source"] is None


def test_many_faces_are_read_and_written_in_chunks(connection):
    """行事のまとめ処理は数千件に効く。SQLite の変数の上限を超えても戻る。"""
    faces = _seed(connection, 1_200)
    history = undo.History()
    history.record(connection, "まとめて", faces, lambda: db.reject_faces(connection, faces))

    history.undo(connection)

    assert db.count_faces(connection, assign_source=db.ASSIGN_REJECTED) == 0


def test_a_failed_write_leaves_nothing_half_done(connection, monkeypatch):
    """書き戻しの途中で落ちたら、何も書かない（1つのトランザクション）。"""
    faces = _seed(connection, 2)
    person = db.add_person(connection, "父")
    history = undo.History()
    history.record(
        connection, "割り当てた", faces,
        lambda: db.assign_faces(connection, faces, person, db.ASSIGN_MANUAL),
    )
    after = _state(connection, faces)
    broken = undo.FaceState(after.faces, frozenset({(faces[0], 999_999, "x")}))  # 無い人物への否定

    with pytest.raises(Exception):
        undo.write(connection, undo.FaceState(
            {face: (None,) * len(undo.FACE_COLUMNS) for face in faces}, broken.rejections
        ))

    assert _state(connection, faces) == after


def test_a_step_that_could_not_be_written_stays_in_history(connection, monkeypatch):
    """**書けなかっただけの操作は履歴に戻す**（PR #117 のレビュー指摘1）。

    CLI の `match` が書き込みロックを握っているあいだに戻すと `database is locked` になる。
    以前は先に履歴から取り出していたので、DB は何も変わっていないのに操作が消えた。
    捨てるのは、戻すと上書きになるとき（`UndoConflict`）だけ。
    """
    import sqlite3

    faces = _seed(connection, 2)
    person = db.add_person(connection, "父")
    history = undo.History()
    history.record(
        connection, "割り当てた", faces,
        lambda: db.assign_faces(connection, faces, person, db.ASSIGN_MANUAL),
    )
    real_write = undo.write

    def locked(_connection, _state):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(undo, "write", locked)
    with pytest.raises(sqlite3.OperationalError):
        history.undo(connection)
    assert history.can_undo() and not history.can_redo()
    assert db.count_faces(connection, assign_source=db.ASSIGN_MANUAL) == 2

    monkeypatch.setattr(undo, "write", real_write)
    history.undo(connection)
    assert db.count_faces(connection, assign_source=db.ASSIGN_MANUAL) == 0

    # やり直しも同じ
    monkeypatch.setattr(undo, "write", locked)
    with pytest.raises(sqlite3.OperationalError):
        history.redo(connection)
    assert history.can_redo() and not history.can_undo()
