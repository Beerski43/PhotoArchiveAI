import numpy as np
import pytest

from photoarchive_ai import db
from photoarchive_ai.matcher import match_faces


@pytest.fixture()
def connection(tmp_path):
    connection = db.ensure_database(str(tmp_path / "test.db"))
    yield connection
    connection.close()


def _add_media(connection, index: int) -> int:
    return db.save_media(
        connection,
        {
            "path": f"/photos/{index}.jpg",
            "filename": f"{index}.jpg",
            "type": "image",
            "file_hash": f"hash{index}",
            "file_size": 100,
            "created_time": "2026-01-01T00:00:00",
        },
    )


def _vector(*values) -> np.ndarray:
    vector = np.zeros(128, dtype=np.float32)
    vector[: len(values)] = values
    return vector


def _add_face(connection, media_id, vector, person_id=None, assign_source=None):
    face_id = db.add_face(
        connection,
        media_id=media_id,
        bbox=(0, 10, 10, 0),
        embedding=vector,
        embed_version="test",
        person_id=person_id,
        assign_source=assign_source,
    )
    connection.commit()
    return face_id


def test_match_assigns_the_correct_person_with_uneven_teacher_counts(connection):
    """人物ごとに手本の枚数が違っても、正しい人物に紐づくこと。

    旧実装は全人物の特徴量を1本のリストに連結しておきながら、人数で剰余を
    取って人物を引いていた (`person_ids[best_index % len(person_ids)]`)。
    1人に2枚以上登録した時点で紐づく人物が壊れていた。
    """
    alice = db.add_person(connection, "Alice")
    bob = db.add_person(connection, "Bob")
    carol = db.add_person(connection, "Carol")

    media = _add_media(connection, 1)
    # Alice は3枚、Bob は1枚、Carol は2枚
    for offset in (0.00, 0.01, 0.02):
        _add_face(connection, media, _vector(0.0 + offset, 0.0), alice, db.ASSIGN_MANUAL)
    _add_face(connection, media, _vector(1.0, 0.0), bob, db.ASSIGN_MANUAL)
    for offset in (0.00, 0.01):
        _add_face(connection, media, _vector(0.0, 1.0 + offset), carol, db.ASSIGN_MANUAL)

    target_media = _add_media(connection, 2)
    carol_face = _add_face(connection, target_media, _vector(0.0, 1.005))

    summary = match_faces(connection, threshold=0.5, margin=0.05)

    assert summary["teachers"] == 6
    assert summary["assigned"] == 1
    record = db.get_face(connection, carol_face)
    assert record["person_id"] == carol
    assert record["assign_source"] == db.ASSIGN_AUTO


def test_match_leaves_distant_faces_unassigned(connection):
    """似ていない顔は未割当のまま残すこと。

    旧実装は閾値判定が無く、どれだけ遠くても必ず誰かに紐づけていた (#22)。
    """
    person = db.add_person(connection, "Alice")
    media = _add_media(connection, 1)
    _add_face(connection, media, _vector(0.0, 0.0), person, db.ASSIGN_MANUAL)

    stranger_media = _add_media(connection, 2)
    stranger = _add_face(connection, stranger_media, _vector(5.0, 5.0))

    summary = match_faces(connection, threshold=0.5)

    assert summary["assigned"] == 0
    assert summary["unassigned"] == 1
    record = db.get_face(connection, stranger)
    assert record["person_id"] is None
    assert record["assign_source"] is None


def test_match_rejects_ambiguous_faces(connection):
    """2人の中間にある顔は、どちらにも決めずに残す。"""
    alice = db.add_person(connection, "Alice")
    bob = db.add_person(connection, "Bob")
    media = _add_media(connection, 1)
    _add_face(connection, media, _vector(0.0, 0.0), alice, db.ASSIGN_MANUAL)
    _add_face(connection, media, _vector(0.2, 0.0), bob, db.ASSIGN_MANUAL)

    target = _add_media(connection, 2)
    ambiguous = _add_face(connection, target, _vector(0.1, 0.0))

    match_faces(connection, threshold=0.5, margin=0.05)

    assert db.get_face(connection, ambiguous)["person_id"] is None


def test_match_does_not_learn_from_auto_or_rejected_faces(connection):
    alice = db.add_person(connection, "Alice")
    media = _add_media(connection, 1)
    _add_face(connection, media, _vector(0.0, 0.0), alice, db.ASSIGN_AUTO)
    _add_face(connection, media, _vector(0.0, 0.0), None, db.ASSIGN_REJECTED)

    summary = match_faces(connection, threshold=0.5, reset=False)

    assert summary["teachers"] == 0
    assert summary["assigned"] == 0


def test_match_is_idempotent_and_reset_keeps_manual(connection):
    alice = db.add_person(connection, "Alice")
    media = _add_media(connection, 1)
    manual = _add_face(connection, media, _vector(0.0, 0.0), alice, db.ASSIGN_MANUAL)
    target_media = _add_media(connection, 2)
    _add_face(connection, target_media, _vector(0.001, 0.0))

    first = match_faces(connection, threshold=0.5)
    second = match_faces(connection, threshold=0.5)

    assert first["assigned"] == second["assigned"] == 1
    assert second["reset"] == 1
    assert db.get_face(connection, manual)["assign_source"] == db.ASSIGN_MANUAL
    assert db.count_faces(connection, assign_source=db.ASSIGN_AUTO) == 1


def test_match_updates_family_score(connection):
    alice = db.add_person(connection, "Alice")
    media = _add_media(connection, 1)
    _add_face(connection, media, _vector(0.0, 0.0), alice, db.ASSIGN_MANUAL)
    other_media = _add_media(connection, 2)
    _add_face(connection, other_media, _vector(0.001, 0.0))
    stranger_media = _add_media(connection, 3)
    _add_face(connection, stranger_media, _vector(9.0, 0.0))

    match_faces(connection, threshold=0.5)

    assert db.get_analysis_result(connection, media)["family_score"] == 100.0
    assert db.get_analysis_result(connection, other_media)["family_score"] > 0.0
    assert db.get_analysis_result(connection, stranger_media).get("family_score") in (None, 0.0)


def test_match_without_teachers_does_nothing(connection):
    media = _add_media(connection, 1)
    face_id = _add_face(connection, media, _vector(0.0, 0.0))

    summary = match_faces(connection, threshold=0.5)

    assert summary["teachers"] == 0
    assert db.get_face(connection, face_id)["person_id"] is None


def test_match_dry_run_does_not_write(connection):
    alice = db.add_person(connection, "Alice")
    media = _add_media(connection, 1)
    _add_face(connection, media, _vector(0.0, 0.0), alice, db.ASSIGN_MANUAL)
    target_media = _add_media(connection, 2)
    target = _add_face(connection, target_media, _vector(0.001, 0.0))

    summary = match_faces(connection, threshold=0.5, dry_run=True)

    assert summary["assigned"] == 1
    assert db.get_face(connection, target)["person_id"] is None
