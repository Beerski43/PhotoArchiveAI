"""顔を「この人物に似た順」に並べる（#69）。

**並びの点は、その人物の手本との最小距離。** 守ることは `recommend` の冒頭に
ある（整列できない手本を使わない・版の違う特徴量を比べない・自分自身を
手本にしない・距離の無い顔は最後）。
"""

import math

import numpy as np
import pytest

from photoarchive_ai import db, recommend


def _vector(angle_degrees):
    """角度だけが違う単位ベクトル。**コサイン距離は 1 - cos(角度の差)。**"""
    vector = np.zeros(db.EMBEDDING_DIM, dtype=np.float32)
    vector[0] = math.cos(math.radians(angle_degrees))
    vector[1] = math.sin(math.radians(angle_degrees))
    return vector


@pytest.fixture()
def connection(tmp_path):
    connection = db.ensure_database(str(tmp_path / "recommend.db"))
    yield connection
    connection.close()


def _media(connection, name, shooting_date="2020-01-01T00:00:00"):
    return db.save_media(
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


def _face(connection, angle, version=None, name=None):
    media_id = _media(connection, name or f"m{angle}-{version}")
    return db.add_face(
        connection,
        media_id=media_id,
        bbox=(0, 10, 10, 0),
        embedding=None if angle is None else _vector(angle),
        embed_version=version or db.embedding_model.ACTIVE.version,
        thumbnail=b"",
    )


def test_rank_puts_faces_without_a_distance_last_in_both_directions():
    """**値の無い顔は、どちらの向きでも最後。** 逆にしたとたん先頭に来ると、
    見たい顔が2ページ目以降に押し出される。"""
    distances = {1: 0.5, 2: 0.1, 3: float("inf"), 5: 0.1}

    assert recommend.rank([1, 2, 3, 4, 5], distances) == [2, 5, 1, 3, 4]
    assert recommend.rank([1, 2, 3, 4, 5], distances, descending=True) == [1, 2, 5, 3, 4]


def test_nearest_distance_ignores_the_face_itself():
    """**確定済みの顔を並べるとき、自分との距離 0 で全件が並ばなくなるのを防ぐ。**"""
    teachers = np.vstack([_vector(0), _vector(60)])

    best = recommend.nearest_distances(
        np.asarray([10, 11]), teachers, np.asarray([10, 11]), teachers
    )

    assert best == pytest.approx([0.5, 0.5])


def test_nearest_distance_is_infinite_when_only_itself_is_a_teacher():
    best = recommend.nearest_distances(
        np.asarray([10]), np.vstack([_vector(0)]), np.asarray([10]), np.vstack([_vector(0)])
    )
    assert np.isinf(best[0])


def test_nearest_distance_works_in_chunks(monkeypatch):
    """**候補 × 手本の行列を一度に作らない**（塊に割っても答えが変わらない）。"""
    monkeypatch.setattr(recommend, "CHUNK_SIZE", 2)
    candidates = np.vstack([_vector(angle) for angle in (0, 30, 60, 90, 120)])
    teachers = np.vstack([_vector(90)])
    reported = []

    best = recommend.nearest_distances(
        np.arange(5), candidates, np.asarray([99]), teachers,
        progress=lambda done, total: reported.append((done, total)),
    )

    expected = [1 - math.cos(math.radians(90 - angle)) for angle in (0, 30, 60, 90, 120)]
    assert best == pytest.approx(expected, abs=1e-6)
    assert reported == [(2, 5), (4, 5), (5, 5)]


def test_similarity_ranks_unassigned_faces_by_the_nearest_teacher(connection):
    """**点は手本との最小距離**（2026-10-08 の測定と同じ）。平均ではない。"""
    person = db.add_person(connection, "${PERSON_4}", "長女")
    teachers = [_face(connection, 0, name="t0"), _face(connection, 90, name="t90")]
    db.assign_faces(connection, teachers, person, db.ASSIGN_MANUAL)
    near_second = _face(connection, 85, name="c85")
    far = _face(connection, 45, name="c45")
    near_first = _face(connection, 3, name="c3")
    connection.commit()

    similarity = recommend.PersonSimilarity(person)
    count = similarity.update(connection, [near_second, far, near_first])

    assert count == 2
    assert recommend.rank([near_second, far, near_first], similarity.distances) == [
        near_first, near_second, far,
    ]


def test_teachers_that_could_not_be_aligned_are_not_used(connection):
    """**整列できない手本は根拠にしない。** 整列できない顔の特徴量は中身に関係なく
    近くなり、使うと横倒しの顔や顔でないものが上位に来る（2026-10-10）。"""
    person = db.add_person(connection, "${PERSON_4}", "長女")
    good = _face(connection, 0, name="good")
    unaligned = _face(connection, 90, name="bad")
    db.assign_faces(connection, [good, unaligned], person, db.ASSIGN_MANUAL)
    connection.execute("UPDATE Face SET aligned = 0 WHERE id = ?", (unaligned,))
    candidate = _face(connection, 90, name="cand")
    connection.commit()

    similarity = recommend.PersonSimilarity(person)
    count = similarity.update(connection, [candidate])

    assert count == 1
    assert similarity.distances[candidate] == pytest.approx(1.0, abs=1e-6), (
        "整列できない手本（距離 0）ではなく、整列できる手本との距離"
    )


def test_faces_of_another_embedding_version_are_not_compared(connection):
    """**別の埋め込み空間の距離を比べるのは常に誤り**（CLAUDE.md §7）。"""
    person = db.add_person(connection, "${PERSON_4}", "長女")
    teacher = _face(connection, 0, name="t")
    old_teacher = _face(connection, 90, version="old-model", name="told")
    db.assign_faces(connection, [teacher, old_teacher], person, db.ASSIGN_MANUAL)
    old = _face(connection, 0, version="old-model", name="old")
    missing = _face(connection, None, name="missing")
    candidate = _face(connection, 90, name="cand")
    connection.commit()

    similarity = recommend.PersonSimilarity(person)
    assert similarity.update(connection, [old, missing, candidate]) == 1

    assert similarity.distances[candidate] == pytest.approx(1.0, abs=1e-6)
    assert old not in similarity.distances and missing not in similarity.distances
    assert recommend.rank([old, missing, candidate], similarity.distances) == [
        candidate, old, missing,
    ]


def test_similarity_with_no_teachers_says_so(connection):
    """**手本が0件なら0を返す。** 呼び出し側が「並べられない」と出す。"""
    person = db.add_person(connection, "${PERSON_5}", "次男")
    candidate = _face(connection, 0)
    connection.commit()

    similarity = recommend.PersonSimilarity(person)

    assert similarity.update(connection, [candidate]) == 0
    assert similarity.distances == {}


def test_similarity_only_measures_faces_it_has_not_seen(connection, monkeypatch):
    """**ページを送るたびに計算し直さない。** 足りない顔の分だけ測る。"""
    person = db.add_person(connection, "${PERSON_4}", "長女")
    db.assign_faces(connection, [_face(connection, 0, name="t")], person, db.ASSIGN_MANUAL)
    first, second = _face(connection, 10, name="a"), _face(connection, 20, name="b")
    connection.commit()
    measured = []
    original = recommend.nearest_distances

    def spy(candidate_ids, *args, **kwargs):
        measured.append(sorted(candidate_ids.tolist()))
        return original(candidate_ids, *args, **kwargs)

    monkeypatch.setattr(recommend, "nearest_distances", spy)
    similarity = recommend.PersonSimilarity(person)

    similarity.update(connection, [first])
    similarity.update(connection, [first, second])
    similarity.update(connection, [first, second])

    assert measured == [[first], [second]]


def test_adding_a_teacher_updates_the_ranking_without_measuring_everything_again(
    connection, monkeypatch
):
    """**割り当てるたびに並びが良くなる。** 増えた手本とだけ比べる。"""
    person = db.add_person(connection, "${PERSON_4}", "長女")
    db.assign_faces(connection, [_face(connection, 0, name="t0")], person, db.ASSIGN_MANUAL)
    near_new = _face(connection, 88, name="c88")
    near_old = _face(connection, 10, name="c10")
    connection.commit()
    similarity = recommend.PersonSimilarity(person)
    similarity.update(connection, [near_new, near_old])
    assert recommend.rank([near_new, near_old], similarity.distances) == [near_old, near_new]

    new_teacher = _face(connection, 90, name="t90")
    db.assign_faces(connection, [new_teacher], person, db.ASSIGN_MANUAL)
    connection.commit()
    teacher_counts = []
    original = recommend.nearest_distances

    def spy(candidate_ids, candidates, teacher_ids, *args, **kwargs):
        teacher_counts.append(len(teacher_ids))
        return original(candidate_ids, candidates, teacher_ids, *args, **kwargs)

    monkeypatch.setattr(recommend, "nearest_distances", spy)
    similarity.update(connection, [near_new, near_old])

    assert teacher_counts == [1], "増えた1件の手本とだけ比べる"
    assert recommend.rank([near_new, near_old], similarity.distances) == [near_new, near_old]


def test_removing_a_teacher_measures_everything_again(connection):
    """**手本が減ったら測り直す。** 最小距離は手本を減らすと大きくなりうる。"""
    person = db.add_person(connection, "${PERSON_4}", "長女")
    keep = _face(connection, 0, name="t0")
    wrong = _face(connection, 90, name="t90")
    db.assign_faces(connection, [keep, wrong], person, db.ASSIGN_MANUAL)
    candidate = _face(connection, 90, name="cand")
    connection.commit()
    similarity = recommend.PersonSimilarity(person)
    similarity.update(connection, [candidate])
    assert similarity.distances[candidate] == pytest.approx(0.0, abs=1e-6)

    db.unassign_faces(connection, [wrong])
    connection.commit()
    similarity.update(connection, [candidate])

    assert similarity.distances[candidate] == pytest.approx(1.0, abs=1e-6)
