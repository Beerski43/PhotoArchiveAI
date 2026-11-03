"""家族の写真に写った**他人(減点)の顔**の点を下げる（#100・#110・#139）。

**減点するのは、利用者が選んだ顔だけ**（#110・2026-10-17 の利用者の決定）。顔認証外にした顔のうち「減点する」の印を
付けた顔だけを引く。未分類の顔と、印の無い顔認証外の顔は引かない。
**引く点は、家族と同じ項目で出した顔の点 × 重み**（#139・利用者の決定）。以前（#100・#110）は大きさだけで引いていた。
"""

import pytest

from photoarchive_ai import db, scoring, strangers
from photoarchive_ai.appearance import Appearance


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


def _face(connection, media_id, *, size=100, person_id=None, source=None, penalize=False):
    face_id = db.add_face(
        connection, media_id=media_id, bbox=(0, size, size, 0), embedding=None,
        embed_version=db.embedding_model.ACTIVE.version,
        person_id=person_id, assign_source=source if person_id or source else None,
    )
    db.save_appearance(connection, [(face_id, Appearance(True, 0.0, 200.0))])
    if penalize:
        assert db.set_face_penalty(connection, [face_id], True) == 1
    return face_id


@pytest.fixture()
def father(connection):
    return db.add_person(connection, "父")


def _strangers(connection):
    return strangers.faces(connection, db.family_faces(connection))


def _scores(connection, weights=None):
    rows = db.family_faces(connection)
    return scoring.family_photo_scores(
        rows, strangers.faces(connection, rows), weights, largest=db.largest_face_sizes(connection)
    )


def test_only_the_faces_marked_to_penalize_count(connection, father):
    """**選んだ顔だけ。** 未分類と、印の無い顔認証外の顔は引かない（#110）。"""
    photo = _media(connection, 1)
    _face(connection, photo, person_id=father, source=db.ASSIGN_MANUAL)
    marked = _face(connection, photo, source=db.ASSIGN_REJECTED, penalize=True)
    _face(connection, photo, source=db.ASSIGN_REJECTED)  # 顔認証外にしただけ
    _face(connection, photo)  # 未分類
    found = _strangers(connection)
    assert list(found) == [photo]
    assert [face["id"] for face in found[photo]] == [marked]


def test_photos_without_family_are_not_looked_at(connection, father):
    photo = _media(connection, 1)
    _face(connection, photo, source=db.ASSIGN_REJECTED, penalize=True)
    assert _strangers(connection) == {}


def test_a_crisp_stranger_takes_more_than_a_blurred_one(connection, father):
    """**他人の顔も家族と同じ項目で点を出す**（#139・利用者の決定）。ボケて横を向いた他人はあまり引かない。"""
    crisp = _media(connection, 1)
    _face(connection, crisp, person_id=father, source=db.ASSIGN_MANUAL)
    _face(connection, crisp, source=db.ASSIGN_REJECTED, penalize=True)
    blurred = _media(connection, 2)
    _face(connection, blurred, person_id=father, source=db.ASSIGN_MANUAL)
    face = _face(connection, blurred, source=db.ASSIGN_REJECTED, penalize=True)
    db.save_appearance(connection, [(face, Appearance(False, None, 20.0))])
    scores = _scores(connection)
    assert scores[crisp] < scores[blurred]


def test_a_small_stranger_takes_less_than_a_large_one(connection, father):
    """大きさも顔の項目。背景に小さく写った他人は、家族と同じ大きさの他人より引かない。"""
    large = _media(connection, 1)
    _face(connection, large, person_id=father, source=db.ASSIGN_MANUAL)
    _face(connection, large, source=db.ASSIGN_REJECTED, penalize=True)
    small = _media(connection, 2)
    _face(connection, small, person_id=father, source=db.ASSIGN_MANUAL)
    _face(connection, small, size=20, source=db.ASSIGN_REJECTED, penalize=True)
    scores = _scores(connection)
    assert scores[large] < scores[small]


def test_the_penalty_is_the_stranger_face_score_times_the_weight_and_the_ratio(connection, father):
    """引く点 = 他人の顔の点 × `strangers` × 比重（#139）。重み 0 なら引かない。"""
    alone = _media(connection, 1)
    _face(connection, alone, person_id=father, source=db.ASSIGN_MANUAL)
    crowded = _media(connection, 2)
    _face(connection, crowded, person_id=father, source=db.ASSIGN_MANUAL)
    _face(connection, crowded, source=db.ASSIGN_REJECTED, penalize=True)
    weights = scoring.Weights.default()
    stranger = _strangers(connection)[crowded][0]
    expected = weights.ratio() * weights.strangers * scoring.face_score(stranger, weights, 100.0)
    assert expected > 0
    scores = _scores(connection, weights)
    assert scores[alone] - scores[crowded] == pytest.approx(expected)
    heavy = _scores(connection, weights._replace(strangers=2.0))
    assert scores[alone] - heavy[crowded] == pytest.approx(2 * expected)
    off = _scores(connection, weights._replace(strangers=0.0))
    assert off[alone] == pytest.approx(off[crowded])


def test_the_marked_faces_are_measured_before_they_are_scored(connection, father):
    """他人(減点)の顔の見え方・表情も測る（#139）。印の無い顔認証外の顔は測らない。"""
    photo = _media(connection, 1)
    marked = db.add_face(
        connection, media_id=photo, bbox=(0, 100, 100, 0), embedding=None,
        embed_version=db.embedding_model.ACTIVE.version, person_id=None,
        assign_source=db.ASSIGN_REJECTED, thumbnail=b"x",
    )
    db.set_face_penalty(connection, [marked], True)
    db.add_face(
        connection, media_id=photo, bbox=(0, 100, 100, 0), embedding=None,
        embed_version=db.embedding_model.ACTIVE.version, person_id=None,
        assign_source=db.ASSIGN_REJECTED, thumbnail=b"x",
    )
    assert db.faces_without_appearance(connection, (db.ASSIGN_MANUAL,)) == []
    assert db.faces_without_appearance(connection, (db.ASSIGN_MANUAL,), penalized=True) == [marked]
    assert db.faces_without_expression(connection, (db.ASSIGN_MANUAL,), penalized=True) == [marked]
def test_the_mark_is_only_for_rejected_faces_and_is_cleared_when_the_face_moves(connection, father):
    photo = _media(connection, 1)
    unassigned = _face(connection, photo)
    assert db.set_face_penalty(connection, [unassigned], True) == 0  # 顔認証外にしていない顔には付かない
    rejected = _face(connection, photo, source=db.ASSIGN_REJECTED, penalize=True)
    db.reject_faces(connection, [rejected])  # 顔認証外にし直しても印は残る
    assert connection.execute("SELECT penalize FROM Face WHERE id = ?", (rejected,)).fetchone()[0] == 1
    db.unassign_faces(connection, [rejected])  # 顔認証外を外したら印も消える
    assert connection.execute("SELECT penalize FROM Face WHERE id = ?", (rejected,)).fetchone()[0] is None
    db.reject_faces(connection, [rejected])
    assert connection.execute("SELECT penalize FROM Face WHERE id = ?", (rejected,)).fetchone()[0] is None


def test_select_and_match_use_the_same_penalty(connection, father):
    """`select` の並びと `match` が書く `family_score` は同じ式（CLAUDE.md §7）。"""
    from photoarchive_ai.selection import family_scores

    crowded = _media(connection, 2)
    _face(connection, crowded, person_id=father, source=db.ASSIGN_MANUAL)
    _face(connection, crowded, source=db.ASSIGN_REJECTED, penalize=True)
    db.recompute_family_scores(connection)
    scores, _ = family_scores(connection)
    stored = db.get_analysis_result(connection, crowded)["family_score"]
    assert stored == pytest.approx(scores[crowded])


def test_a_family_photo_with_a_penalized_face_still_ranks_above_a_photo_without_family(
    connection, father
):
    """**点は負になりうるが、家族の写った写真は家族のいない写真より前**（PR #101 の
    レビュー指摘1・利用者の決定）。"""
    from photoarchive_ai.selection import select_media

    clear = _media(connection, 1)
    _face(connection, clear, person_id=father, source=db.ASSIGN_MANUAL)
    crowded = _media(connection, 2)
    blurred = _face(connection, crowded, person_id=father, source=db.ASSIGN_MANUAL)
    db.save_appearance(connection, [(blurred, Appearance(False, None, 20.0))])
    _face(connection, crowded, source=db.ASSIGN_REJECTED, penalize=True)
    _face(connection, crowded, source=db.ASSIGN_REJECTED, penalize=True)
    nobody = _media(connection, 3)
    _face(connection, nobody)

    rows = db.family_faces(connection)
    off = scoring.Weights.default()._replace(technical=0, aesthetic=0)
    assert scoring.family_photo_scores(rows, strangers.faces(connection, rows), off)[crowded] < 0
    order = [m["id"] for m in select_media(
        connection, {"family_only": False, "remove_similar": False}
    ) if m["id"] in (clear, crowded, nobody)]
    assert order == [clear, crowded, nobody]
