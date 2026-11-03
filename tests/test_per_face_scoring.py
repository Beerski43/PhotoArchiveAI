"""`select` の点を「顔ごとの点の和」と「写真全体の点」の比重で出す（#139）。

利用者の式（2026-11-03）:

    家族の加点 = Σ(顔の点 × 人物の重み)
    他人の減点 = Σ(他人(減点)の顔の点) × 重み
    総合 = (家族の加点 − 他人の減点) × 比重 / 100 + 写真全体の点 × (100 − 比重) / 100

顔の大きさは、その写真でいちばん大きい顔と比べる（利用者の決定）。
"""

import logging
from pathlib import Path

import pytest

from photoarchive_ai import db, scoring, selection
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


def _face(connection, media_id, size, person_id=None):
    face_id = db.add_face(
        connection, media_id=media_id, bbox=(0, size, size, 0), embedding=None,
        embed_version=db.embedding_model.ACTIVE.version, person_id=person_id,
        assign_source=db.ASSIGN_MANUAL if person_id else None,
    )
    db.save_appearance(connection, [(face_id, Appearance(True, 0.0, 200.0))])
    return face_id


def test_the_size_is_measured_against_the_largest_face_in_the_photo():
    assert scoring.size_level(100.0, 100.0) == 1.0
    assert scoring.size_level(30.0, 100.0) == 0.0
    assert scoring.size_level(65.0, 100.0) == pytest.approx(0.5)
    assert scoring.size_level(50.0, None) == 1.0  # 比べる顔が無い


def test_the_largest_face_counts_every_detected_face(connection):
    """物差しは**検出したすべての顔**（割り当ての無い顔も）。"""
    person = db.add_person(connection, "父")
    photo = _media(connection, 1)
    _face(connection, photo, 40, person)
    _face(connection, photo, 100)  # 未分類
    other = _media(connection, 2)
    _face(connection, other, 30, person)
    assert db.largest_face_sizes(connection) == {photo: 100.0, other: 30.0}
    assert db.largest_face_sizes(connection, [other]) == {other: 30.0}


def test_a_family_face_small_next_to_a_large_face_scores_less(connection):
    """隣に大きな顔が写っていると、家族の顔の大きさの点が下がる。1人だけの写真は大きさが満点。"""
    person = db.add_person(connection, "父")
    alone = _media(connection, 1)
    _face(connection, alone, 40, person)
    beside = _media(connection, 2)
    _face(connection, beside, 40, person)
    _face(connection, beside, 100)  # 未分類（点には入らないが、物差しにはなる）
    rows = db.family_faces(connection)
    scores = scoring.family_photo_scores(rows, largest=db.largest_face_sizes(connection))
    weights = scoring.Weights.default()
    face_total = sum(getattr(weights, key) for key in scoring.FACE_ITEMS)
    lost = 1.0 - scoring.size_level(40.0, 100.0)
    assert scores[alone] - scores[beside] == pytest.approx(
        weights.ratio() * 100.0 * weights.size * lost / face_total
    )


def test_face_item_weights_are_shares(connection):
    """顔の項目の重みは**比**。全部を2倍にしても点は変わらない。"""
    face = {"aligned": 1, "yaw": 0.3, "sharpness": 80.0, "eye_blink": 0.1, "mouth_smile": 0.1,
            "bbox_top": 0, "bbox_right": 50, "bbox_bottom": 50, "bbox_left": 0}
    weights = scoring.Weights.default()
    doubled = weights._replace(**{key: 2 * getattr(weights, key) for key in scoring.FACE_ITEMS})
    assert scoring.face_score(face, doubled) == pytest.approx(scoring.face_score(face, weights))
    assert 0.0 < scoring.face_score(face, weights) < 100.0
    nothing = weights._replace(**{key: 0.0 for key in scoring.FACE_ITEMS})
    assert scoring.face_score(face, nothing) == 0.0


def test_the_old_weight_keys_are_skipped_with_a_warning(tmp_path: Path, caplog):
    """#139 で無くなった `member_bonus` と `stranger_penalty` は、止めずに警告して読み飛ばす。"""
    path = tmp_path / "rule.yml"
    path.write_text("weights:\n  member_bonus: 17\n  stranger_penalty: 6\n  smile: 0.4\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="photoarchive.selection"):
        rule = selection.load_rule(str(path))
    weights = selection.rule_weights(rule)
    assert weights.smile == 0.4
    assert weights.strangers == scoring.WEIGHT_STRANGERS  # 古い値を新しい意味で読まない
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "weights.member_bonus" in messages and "weights.stranger_penalty" in messages


def test_a_misspelled_weight_key_is_still_refused():
    with pytest.raises(ValueError, match="知らない項目"):
        selection.rule_weights({"weights": {"sharpnes": 0.3}})


def test_the_ratio_must_be_between_0_and_100():
    assert selection.rule_weights({"weights": {"face_ratio": 100}}).face_ratio == 100.0
    assert selection.rule_weights({"weights": {"face_ratio": 0}}).face_ratio == 0.0
    with pytest.raises(ValueError, match="face_ratio"):
        selection.rule_weights({"weights": {"face_ratio": 101}})
