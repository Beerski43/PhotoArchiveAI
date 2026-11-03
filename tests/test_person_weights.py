"""人物ごとの重みと親しい人（#110）。

利用者の決定（2026-10-17）:

- 人物に「家族／親しい人」の区分を持たせる。**親しい人だけの写真は家族の写真に数えない。** 家族と一緒に
  写っていれば、その人の重みぶん加点する
- 人物ごとの重み（既定 1.0）は、顔の点にかける

#139 で家族の加点が「顔ごとの点 × 人物の重み」の和になり、人数の加点（`member_bonus`）は無くなった。
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


#: 重みを付けていない、くっきり正面を向いた顔1つの点（同じ大きさの顔だけの写真なので、大きさは満点）。
CRISP = {
    "aligned": 1, "yaw": 0.0, "sharpness": 200.0, "eye_blink": None, "mouth_smile": None,
    "bbox_top": 10, "bbox_right": 60, "bbox_bottom": 60, "bbox_left": 10,
}
#: 写真全体の点を 0 にした重み（顔の点だけを見る）。
FACES_ONLY = scoring.Weights.default()._replace(technical=0, aesthetic=0, highlights=0, shadows=0)


def _face_points(weight_sum):
    """人物の重みの和が ``weight_sum`` のときの点（比重をかけたあと）。"""
    return FACES_ONLY.ratio() * weight_sum * scoring.face_score(CRISP, FACES_ONLY)


def _scores(connection):
    return scoring.family_photo_scores(db.family_faces(connection), weights=FACES_ONLY)


@pytest.fixture()
def people(connection):
    return db.add_person(connection, "父"), db.add_person(connection, "母"), db.add_person(connection, "友")


def test_with_every_weight_at_one_each_family_face_adds_its_score(connection, people):
    """全員 1.0・区分なし: 家族の加点は**顔ごとの点の和**（#139）。2人なら1人の2倍。"""
    father, mother, _ = people
    photo = _media(connection, 1)
    _face(connection, photo, father)
    _face(connection, photo, mother)
    rows = db.family_faces(connection)
    assert {row["person_weight"] for row in rows} == {1.0}
    assert _scores(connection)[photo] == pytest.approx(_face_points(2.0))


def test_a_person_weight_scales_that_persons_face(connection, people):
    father, mother, _ = people
    db.set_person_scoring(connection, mother, close=False, weight=2.0)
    alone = _media(connection, 1)
    _face(connection, alone, mother)
    together = _media(connection, 2)
    _face(connection, together, father)
    _face(connection, together, mother)
    scores = _scores(connection)
    assert scores[alone] == pytest.approx(_face_points(2.0))
    assert scores[together] == pytest.approx(_face_points(3.0))


def test_a_close_person_adds_only_when_family_is_in_the_photo(connection, people):
    father, _, friend = people
    db.set_person_scoring(connection, friend, close=True, weight=0.5)
    with_family = _media(connection, 1)
    _face(connection, with_family, father)
    _face(connection, with_family, friend)
    friend_only = _media(connection, 2)
    _face(connection, friend_only, friend)
    scores = _scores(connection)
    assert scores[with_family] == pytest.approx(_face_points(1.5))
    assert friend_only not in scores  # 親しい人だけの写真は家族の写真ではない


def test_a_person_with_weight_zero_adds_nothing(connection, people):
    """**重み 0 の人物は加点しない**（PR #112 の指摘1）。"""
    father, mother, _ = people
    db.set_person_scoring(connection, mother, close=False, weight=0.0)
    alone = _media(connection, 1)
    _face(connection, alone, father)
    together = _media(connection, 2)
    _face(connection, together, father)
    _face(connection, together, mother)
    scores = _scores(connection)
    assert scores[together] == pytest.approx(scores[alone])


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


def test_the_breakdown_adds_every_face_with_its_weight_and_matches_the_score(connection, people):
    father, mother, friend = people
    db.set_person_scoring(connection, friend, close=True, weight=1.5)
    db.set_person_scoring(connection, father, close=False, weight=3.0)
    photo = _media(connection, 1)
    for person in people:
        _face(connection, photo, person)
    parts = scoring.photo_breakdown(db.family_faces(connection), (), FACES_ONLY)
    faces = sum(parts[key] for key in scoring.FACE_ITEMS)
    assert faces == pytest.approx(_face_points(3.0 + 1.0 + 1.5))
    assert parts["total"] == pytest.approx(_scores(connection)[photo])


def test_a_negative_or_infinite_weight_is_refused(connection, people):
    father, _, _ = people
    for bad in (-1.0, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            db.set_person_scoring(connection, father, close=False, weight=bad)


def test_a_version_11_database_gains_the_columns_and_keeps_everything(tmp_path):
    """列を足すだけ。**人物・手本・顔認証外が消えない**（CLAUDE.md §4）。既存の人物は家族・重み 1.0。"""
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
