"""家族の写真に目立って写った他人の点を下げる（#100）。

**重みは「家族らしくなさ」×「目立ち具合」**（利用者の決定）。実データで分かったこと
（2026-10-12）: 家族の写った写真の未割当の顔のうち 1,876 個が、写真にまだいない家族の
手本まで照合の閾値以内だった。並べると家族の子の取りこぼしに見える。これを引かない。
"""

import numpy as np
import pytest

from photoarchive_ai import db, scoring, strangers
from photoarchive_ai.appearance import Appearance


@pytest.fixture()
def connection(tmp_path):
    connection = db.ensure_database(str(tmp_path / "test.db"))
    yield connection
    connection.close()


def _unit(index):
    vector = np.zeros(db.EMBEDDING_DIM, dtype=np.float32)
    vector[index] = 1.0
    return vector


def _blend(first, second, weight):
    """``first`` から ``second`` へ寄せたベクトル。コサイン距離は寄せるほど大きい。"""
    return (np.cos(weight) * _unit(first) + np.sin(weight) * _unit(second)).astype(np.float32)


def _media(connection, index):
    return db.save_media(connection, {
        "path": f"/p/{index}.jpg", "filename": f"{index}.jpg", "type": "image",
        "file_hash": f"h{index}", "file_size": 1, "created_time": "2019-01-01T00:00:00",
        "shooting_date": "2019-05-03T10:00:00",
    })


def _face(connection, media_id, *, size=100, vector=None, person_id=None, source=None):
    face_id = db.add_face(
        connection, media_id=media_id, bbox=(0, size, size, 0),
        embedding=None if vector is None else vector,
        embed_version=db.embedding_model.ACTIVE.version,
        person_id=person_id, assign_source=source if person_id or source else None,
    )
    db.save_appearance(connection, [(face_id, Appearance(True, 0.0, 200.0))])
    return face_id


@pytest.fixture()
def family(connection):
    """家族2人。手本はそれぞれ別の写真に1枚ずつ（父 = 軸0、子 = 軸1）。"""
    father = db.add_person(connection, "父")
    child = db.add_person(connection, "子")
    _face(connection, _media(connection, 100), vector=_unit(0), person_id=father,
          source=db.ASSIGN_MANUAL)
    _face(connection, _media(connection, 101), vector=_unit(1), person_id=child,
          source=db.ASSIGN_MANUAL)
    return father, child


def _weights(connection):
    return strangers.weights(connection, db.family_faces(connection))


def test_the_levels_are_straight_lines_between_their_bounds():
    assert strangers.prominence(strangers.SMALL) == 0.0
    assert strangers.prominence(0.65) == pytest.approx(0.5)
    assert strangers.prominence(1.4) == 1.0
    threshold = db.embedding_model.ACTIVE.threshold
    assert strangers.unlikeness(threshold) == 0.0
    assert strangers.unlikeness((threshold + strangers.FAR) / 2) == pytest.approx(0.5)
    assert strangers.unlikeness(float("inf")) == 1.0
    assert strangers.unlikeness(None) == 0.0


def test_a_large_stranger_weighs_one_and_a_small_one_nothing(connection, family):
    father, _ = family
    photo = _media(connection, 1)
    _face(connection, photo, size=100, vector=_unit(0), person_id=father, source=db.ASSIGN_AUTO)
    _face(connection, photo, size=100, vector=_unit(5))  # 誰にも似ていない・同じ大きさ
    _face(connection, photo, size=20, vector=_unit(6))  # 背景の小さな人
    assert _weights(connection) == {photo: pytest.approx(1.0)}


def test_a_face_like_a_family_member_missing_from_the_photo_is_not_counted(connection, family):
    """**取りこぼした家族の顔を引かない。** 写真にいない子の手本に近い未割当の顔は 0。"""
    father, _ = family
    photo = _media(connection, 1)
    _face(connection, photo, vector=_unit(0), person_id=father, source=db.ASSIGN_MANUAL)
    _face(connection, photo, vector=_blend(1, 5, 0.3))  # 子に近い（距離 約 0.04）
    assert _weights(connection) == {}


def test_a_face_like_someone_already_in_the_photo_is_a_stranger(connection, family):
    """**同じ人は1枚に1人。** 写っている父に似ていても、父ではない。"""
    father, child = family
    photo = _media(connection, 1)
    _face(connection, photo, vector=_unit(0), person_id=father, source=db.ASSIGN_MANUAL)
    _face(connection, photo, vector=_unit(1), person_id=child, source=db.ASSIGN_MANUAL)
    _face(connection, photo, vector=_blend(0, 5, 0.3))
    # 家族の全員がすでに写っているので、残りの顔は家族ではありえない
    assert _weights(connection) == {photo: pytest.approx(1.0)}


def test_a_rejected_face_counts_and_a_face_without_features_does_not(connection, family):
    father, _ = family
    photo = _media(connection, 1)
    _face(connection, photo, vector=_unit(0), person_id=father, source=db.ASSIGN_MANUAL)
    _face(connection, photo, size=65, source=db.ASSIGN_REJECTED, vector=_unit(1))
    _face(connection, photo, size=100)  # 特徴量が無い → 分からないので引かない
    assert _weights(connection) == {photo: pytest.approx(strangers.prominence(0.65))}


def test_photos_without_family_are_not_looked_at(connection, family):
    photo = _media(connection, 1)
    _face(connection, photo, vector=_unit(5))
    assert _weights(connection) == {}


def test_without_any_family_face_to_compare_unassigned_faces_are_not_counted(connection):
    """比べる家族の顔が無ければ、分からないので引かない。"""
    person = db.add_person(connection, "父")
    photo = _media(connection, 1)
    _face(connection, photo, person_id=person, source=db.ASSIGN_MANUAL)  # 特徴量なし
    _face(connection, photo, vector=_unit(5))
    assert _weights(connection) == {}


def test_the_penalty_is_subtracted_from_the_photo_score(connection, family):
    """重み 1 あたり `STRANGER_PENALTY`（30・利用者の決定）を引く。"""
    assert scoring.STRANGER_PENALTY == 30.0
    father, _ = family
    alone = _media(connection, 1)
    _face(connection, alone, vector=_unit(0), person_id=father, source=db.ASSIGN_MANUAL)
    crowded = _media(connection, 2)
    _face(connection, crowded, vector=_unit(0), person_id=father, source=db.ASSIGN_MANUAL)
    _face(connection, crowded, vector=_unit(5))
    rows = db.family_faces(connection)
    scores = scoring.family_photo_scores(rows, strangers.weights(connection, rows))
    assert scores[alone] - scores[crowded] == pytest.approx(30.0)


def test_select_and_match_use_the_same_penalty(connection, family):
    """`select` の並びと `match` が書く `family_score` は同じ式（CLAUDE.md §7）。"""
    from photoarchive_ai.selection import family_scores

    father, _ = family
    crowded = _media(connection, 2)
    _face(connection, crowded, vector=_unit(0), person_id=father, source=db.ASSIGN_MANUAL)
    _face(connection, crowded, vector=_unit(5))
    db.recompute_family_scores(connection)
    scores, _ = family_scores(connection)
    stored = db.get_analysis_result(connection, crowded)["family_score"]
    assert stored == pytest.approx(scores[crowded])


def test_a_family_photo_with_a_stranger_still_ranks_above_a_photo_without_family(
    connection, family
):
    """**点は負になりうるが、家族の写った写真は家族のいない写真より前**（PR #101 の
    レビュー指摘1・利用者の決定）。`family_only: false` で、ボケて横を向いた父と大きな
    他人2人の写真（20 − 30×2 = −40 点）が、他人しか写っていない写真より後ろに来ていた。"""
    from photoarchive_ai.selection import select_media

    father, _ = family
    clear = _media(connection, 1)
    _face(connection, clear, vector=_unit(0), person_id=father, source=db.ASSIGN_MANUAL)
    crowded = _media(connection, 2)
    blurred = _face(connection, crowded, vector=_unit(0), person_id=father,
                    source=db.ASSIGN_MANUAL)
    db.save_appearance(connection, [(blurred, Appearance(False, None, 20.0))])
    _face(connection, crowded, vector=_unit(5))
    _face(connection, crowded, vector=_unit(6))
    stranger_only = _media(connection, 3)
    _face(connection, stranger_only, vector=_unit(7))

    rows = db.family_faces(connection)
    assert scoring.family_photo_scores(rows, strangers.weights(connection, rows))[crowded] < 0
    order = [m["id"] for m in select_media(
        connection, {"family_only": False, "remove_similar": False}
    ) if m["id"] in (clear, crowded, stranger_only)]
    assert order == [clear, crowded, stranger_only]
