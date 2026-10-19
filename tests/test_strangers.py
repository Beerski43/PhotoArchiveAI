"""家族の写真に写った**他人(減点)の顔**の点を下げる（#100・#110）。

**減点するのは、利用者が選んだ顔だけ**（#110・2026-10-17 の利用者の決定）。顔認証外にした顔のうち「減点する」の印を
付けた顔だけを、家族と比べた大きさ（目立ち具合）× 30 で引く。未分類の顔と、印の無い顔認証外の顔は引かない。
以前（#100）は未分類・顔認証外の顔すべてを、家族の顔までの距離（家族らしくなさ）を掛けて引いていた。
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


def _weights(connection):
    return strangers.weights(connection, db.family_faces(connection))


def test_the_prominence_is_a_straight_line_between_its_bounds():
    assert strangers.prominence(strangers.SMALL) == 0.0
    assert strangers.prominence(0.65) == pytest.approx(0.5)
    assert strangers.prominence(1.4) == 1.0


def test_only_the_faces_marked_to_penalize_count(connection, father):
    """**選んだ顔だけ。** 未分類と、印の無い顔認証外の顔は引かない（#110）。"""
    photo = _media(connection, 1)
    _face(connection, photo, person_id=father, source=db.ASSIGN_MANUAL)
    _face(connection, photo, source=db.ASSIGN_REJECTED, penalize=True)
    _face(connection, photo, source=db.ASSIGN_REJECTED)  # 顔認証外にしただけ
    _face(connection, photo)  # 未分類
    assert _weights(connection) == {photo: pytest.approx(1.0)}


def test_a_small_marked_face_weighs_little(connection, father):
    """大きさを掛ける（利用者の決定）。背景に小さく写っただけなら引かない。"""
    photo = _media(connection, 1)
    _face(connection, photo, person_id=father, source=db.ASSIGN_MANUAL)
    _face(connection, photo, size=65, source=db.ASSIGN_REJECTED, penalize=True)
    _face(connection, photo, size=20, source=db.ASSIGN_REJECTED, penalize=True)
    assert _weights(connection) == {photo: pytest.approx(strangers.prominence(0.65))}


def test_photos_without_family_are_not_looked_at(connection, father):
    photo = _media(connection, 1)
    _face(connection, photo, source=db.ASSIGN_REJECTED, penalize=True)
    assert _weights(connection) == {}


def test_a_close_persons_face_is_not_the_yardstick(connection, father):
    """大きさは**家族の顔**と比べる。親しい人が大きく写っていても物差しにしない。"""
    friend = db.add_person(connection, "友")
    db.set_person_scoring(connection, friend, close=True, weight=1.0)
    photo = _media(connection, 1)
    _face(connection, photo, size=50, person_id=father, source=db.ASSIGN_MANUAL)
    _face(connection, photo, size=200, person_id=friend, source=db.ASSIGN_MANUAL)
    _face(connection, photo, size=50, source=db.ASSIGN_REJECTED, penalize=True)
    assert _weights(connection) == {photo: pytest.approx(1.0)}


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


def test_the_penalty_is_subtracted_from_the_photo_score(connection, father):
    """重み 1 あたり `STRANGER_PENALTY`（30・利用者の決定）を引く。"""
    assert scoring.STRANGER_PENALTY == 30.0
    alone = _media(connection, 1)
    _face(connection, alone, person_id=father, source=db.ASSIGN_MANUAL)
    crowded = _media(connection, 2)
    _face(connection, crowded, person_id=father, source=db.ASSIGN_MANUAL)
    _face(connection, crowded, source=db.ASSIGN_REJECTED, penalize=True)
    rows = db.family_faces(connection)
    scores = scoring.family_photo_scores(rows, strangers.weights(connection, rows))
    assert scores[alone] - scores[crowded] == pytest.approx(30.0)


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
    assert scoring.family_photo_scores(rows, strangers.weights(connection, rows), off)[crowded] < 0
    order = [m["id"] for m in select_media(
        connection, {"family_only": False, "remove_similar": False}
    ) if m["id"] in (clear, crowded, nobody)]
    assert order == [clear, crowded, nobody]
