"""人物ごとの重みと親しい人（#110）。

利用者の決定（2026-10-17）:

- 人物に「家族／親しい人」の区分を持たせる。**親しい人だけの写真は家族の写真に数えない。** 家族と一緒に
  写っていれば、その人の重みぶん加点する
- 人物ごとの重み（既定 1.0）は、**顔の点と人数の加点の両方**にかける。全員 1.0 なら以前と同じ点
"""

import pytest

from photoarchive_ai import db, scoring
from photoarchive_ai.appearance import Appearance
from photoarchive_ai.migration import migrate_database, needs_migration


@pytest.fixture()
def connection(tmp_path):
    connection = db.ensure_database(str(tmp_path / "test.db"))
    yield connection
    connection.close()


def _media(connection, index):
    return db.save_media(connection, {
        "path": f"/p/{index}.jpg", "filename": f"{index}.jpg", "type": "image",
        "file_hash": f"h{index}", "file_size": 1, "created_time": "2019-01-01T00:00:00",
        "shooting_date": "2019-05-03T10:00:00",
    })


def _face(connection, media_id, person_id, *, sharpness=200.0):
    face_id = db.add_face(
        connection, media_id=media_id, bbox=(10, 60, 60, 10), embedding=None,
        embed_version=db.embedding_model.ACTIVE.version,
        person_id=person_id, assign_source=db.ASSIGN_MANUAL,
    )
    db.save_appearance(connection, [(face_id, Appearance(True, 0.0, sharpness))])
    return face_id


def _scores(connection):
    weights = scoring.Weights.default()._replace(technical=0, aesthetic=0)
    return scoring.family_photo_scores(db.family_faces(connection), weights=weights)


@pytest.fixture()
def people(connection):
    return db.add_person(connection, "父"), db.add_person(connection, "母"), db.add_person(connection, "友")


def test_with_every_weight_at_one_the_score_is_the_one_from_before(connection, people):
    """全員 1.0・区分なし: いちばん良い顔の点 ＋ はっきり写った家族1人につき 20（**1人目から**・PR #112 の指摘1）。

    以前は2人目からだったが、家族の写った写真がそろって 20 ずれるだけなので並びは変わらない。
    """
    father, mother, _ = people
    photo = _media(connection, 1)
    _face(connection, photo, father)
    _face(connection, photo, mother)
    rows = db.family_faces(connection)
    assert {row["person_weight"] for row in rows} == {1.0}
    face = scoring.family_face_score(1, 0.0, 200.0, None, None)
    assert _scores(connection)[photo] == pytest.approx(face + 2 * scoring.MEMBER_BONUS)


def test_a_person_weight_scales_the_face_and_the_member_bonus(connection, people):
    """顔の点と人数の加点の両方にかける。人数の加点は「はっきり写った家族の重みの和」。"""
    father, mother, _ = people
    db.set_person_scoring(connection, mother, close=False, weight=2.0)
    alone = _media(connection, 1)
    _face(connection, alone, mother)
    together = _media(connection, 2)
    _face(connection, together, father)
    _face(connection, together, mother)
    face = scoring.family_face_score(1, 0.0, 200.0, None, None)
    scores = _scores(connection)
    assert scores[alone] == pytest.approx(2.0 * face + 2.0 * scoring.MEMBER_BONUS)
    assert scores[together] == pytest.approx(2.0 * face + 3.0 * scoring.MEMBER_BONUS)


def test_a_close_person_adds_only_when_family_is_in_the_photo(connection, people):
    father, _, friend = people
    db.set_person_scoring(connection, friend, close=True, weight=0.5)
    with_family = _media(connection, 1)
    _face(connection, with_family, father)
    _face(connection, with_family, friend)
    friend_only = _media(connection, 2)
    _face(connection, friend_only, friend)
    scores = _scores(connection)
    face = scoring.family_face_score(1, 0.0, 200.0, None, None)
    assert scores[with_family] == pytest.approx(face + 1.5 * scoring.MEMBER_BONUS)
    assert friend_only not in scores  # 親しい人だけの写真は家族の写真ではない


def test_a_person_with_weight_zero_adds_no_member_bonus(connection, people):
    """**重み 0 の人物は加点しない**（PR #112 の指摘1。「重みの和 − 最小」では写るだけで +20 だった）。"""
    father, mother, _ = people
    db.set_person_scoring(connection, mother, close=False, weight=0.0)
    alone = _media(connection, 1)
    _face(connection, alone, father)
    together = _media(connection, 2)
    _face(connection, together, father)
    _face(connection, together, mother)
    scores = _scores(connection)
    assert scores[together] == pytest.approx(scores[alone])


def test_a_lighter_person_adds_their_own_weight_not_the_heavier_one(connection, people):
    """軽い人物は**自分の重みぶん**だけ足す（重い人物ぶんを足さない・PR #112 の指摘1）。"""
    father, mother, _ = people
    db.set_person_scoring(connection, mother, close=False, weight=2.0)
    photo = _media(connection, 1)
    _face(connection, photo, father)
    _face(connection, photo, mother)
    weights = scoring.Weights.default()._replace(technical=0, aesthetic=0)
    parts = scoring.photo_breakdown(db.family_faces(connection), 0.0, weights)
    alone_members = 2.0 * scoring.MEMBER_BONUS
    assert parts["members"] - alone_members == pytest.approx(1.0 * scoring.MEMBER_BONUS)


def test_family_only_drops_a_photo_with_only_a_close_person(connection, people):
    from photoarchive_ai.selection import select_media

    father, _, friend = people
    db.set_person_scoring(connection, friend, close=True, weight=1.0)
    family_photo = _media(connection, 1)
    _face(connection, family_photo, father)
    friend_only = _media(connection, 2)
    _face(connection, friend_only, friend)
    chosen = [m["id"] for m in select_media(connection, {"family_only": True, "remove_similar": False})]
    assert chosen == [family_photo]


def test_the_breakdown_shows_the_close_person_and_adds_up(connection, people):
    father, mother, friend = people
    db.set_person_scoring(connection, friend, close=True, weight=1.5)
    db.set_person_scoring(connection, father, close=False, weight=3.0)
    photo = _media(connection, 1)
    for person in people:
        _face(connection, photo, person)
    weights = scoring.Weights.default()._replace(technical=0, aesthetic=0)
    parts = scoring.photo_breakdown(db.family_faces(connection), 0.0, weights)
    assert parts["close"] == pytest.approx(1.5 * scoring.MEMBER_BONUS)
    assert parts["members"] == pytest.approx((3.0 + 1.0) * scoring.MEMBER_BONUS)
    assert parts["total"] == pytest.approx(_scores(connection)[photo])


def test_a_negative_or_infinite_weight_is_refused(connection, people):
    father, _, _ = people
    for bad in (-1.0, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            db.set_person_scoring(connection, father, close=False, weight=bad)


def test_a_version_11_database_gains_the_columns_and_keeps_everything(tmp_path):
    """列を足すだけ。**人物・手本・除外が消えない**（CLAUDE.md §4）。既存の人物は家族・重み 1.0。"""
    database = tmp_path / "v11.db"
    connection = db.ensure_database(str(database))
    person = db.add_person(connection, "父")
    photo = _media(connection, 1)
    manual = _face(connection, photo, person)
    rejected = db.add_face(
        connection, media_id=photo, bbox=(0, 5, 5, 0), embedding=None,
        embed_version=db.embedding_model.ACTIVE.version, assign_source=db.ASSIGN_REJECTED,
    )
    for table, column in (("Person", "category"), ("Person", "weight"), ("Face", "penalize")):
        connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    connection.execute("PRAGMA user_version = 11")
    connection.commit()
    connection.close()
    assert needs_migration(str(database))

    migrate_database(str(database), make_backup=False)

    connection = db.ensure_database(str(database))
    try:
        assert db.get_schema_version(connection) == db.SCHEMA_VERSION
        sources = dict(connection.execute("SELECT id, assign_source FROM Face"))
        assert sources == {manual: db.ASSIGN_MANUAL, rejected: db.ASSIGN_REJECTED}
        row = db.family_faces(connection)[0]
        assert (row["person_id"], row["person_weight"], db.is_family(row)) == (person, 1.0, True)
    finally:
        connection.close()
