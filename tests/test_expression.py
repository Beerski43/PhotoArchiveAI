"""顔の表情（目閉じ・笑顔）を測って、`select` の並びに使う（#95）。

**実データで分かったこと**（2026-10-11）: 古い笑顔（`scoring.estimate_smile_score`）は
無表情の顔の 86% に 100 を付けていた。目閉じを見分ける材料はどこにも無かった。
"""

import io

import numpy as np
import pytest
from PIL import Image

from photoarchive_ai import db, expression, scoring
from photoarchive_ai.selection import family_scores
from tests.fakes import EXPRESSION_STATE

#: conftest が差し替える前の読み込み口（collect のときに取っておく）。
_REAL_LOADER = expression._load_landmarker


@pytest.fixture()
def connection(tmp_path):
    connection = db.ensure_database(str(tmp_path / "test.db"))
    yield connection
    connection.close()


def _jpeg(size=(80, 100)) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(np.full((size[1], size[0], 3), 128, dtype=np.uint8)).save(
        buffer, format="JPEG"
    )
    return buffer.getvalue()


def _face(connection, index, person_id, source=db.ASSIGN_MANUAL, thumbnail=True):
    media_id = db.save_media(connection, {
        "path": f"/p/{index}.jpg", "filename": f"{index}.jpg", "type": "image",
        "file_hash": f"h{index}", "file_size": 1, "created_time": "2019-01-01T00:00:00",
    })
    return media_id, db.add_face(
        connection, media_id=media_id, bbox=(0, 10, 10, 0),
        embedding=[0.0] * db.EMBEDDING_DIM, embed_version=db.embedding_model.ACTIVE.version,
        thumbnail=_jpeg() if thumbnail else None, person_id=person_id, assign_source=source,
    )


def _expression_of(connection, face_id):
    return tuple(connection.execute(
        "SELECT expression_found, eye_blink, mouth_smile FROM Face WHERE id = ?", (face_id,)
    ).fetchone())


def _blendshapes(blink_left, blink_right, smile_left, smile_right):
    return {
        "eyeBlinkLeft": blink_left, "eyeBlinkRight": blink_right,
        "mouthSmileLeft": smile_left, "mouthSmileRight": smile_right,
    }


def test_the_eyes_are_closed_only_as_far_as_the_more_open_eye():
    """**片目だけのウインクは閉じたことにしない。** 両目の小さいほうを取る。"""
    measured = expression.from_blendshapes(_blendshapes(0.9, 0.1, 0.2, 0.4))
    assert measured.found
    assert measured.eye_blink == pytest.approx(0.1)
    assert measured.mouth_smile == pytest.approx(0.3)


def test_the_thumbnail_is_padded_before_the_landmarker_sees_it():
    """**サムネイルは顔がきつく切れている。** 余白なしでは実データの 5% で顔が取れなかった。"""
    rgb = np.full((100, 80, 3), 200, dtype=np.uint8)
    padded = expression.pad(rgb)
    assert padded.shape == (150, 130, 3)
    assert padded[0, 0].tolist() == [0, 0, 0]
    assert padded[75, 65].tolist() == [200, 200, 200]


def test_fill_missing_writes_the_expression_of_assigned_faces_only(connection):
    person_id = db.add_person(connection, "父")
    _, manual = _face(connection, 1, person_id)
    _, unassigned = _face(connection, 2, None, source=None)
    EXPRESSION_STATE["blendshapes"] = _blendshapes(0.6, 0.7, 0.5, 0.3)

    assert expression.fill_missing(connection) == 1

    found, blink, smile = _expression_of(connection, manual)
    assert (found, blink, smile) == (1, pytest.approx(0.6), pytest.approx(0.4))
    assert _expression_of(connection, unassigned) == (None, None, None)
    # 2回目は何も測らない（差分だけ）
    assert expression.fill_missing(connection) == 0


def test_a_face_the_landmarker_cannot_find_is_recorded_and_not_measured_again(connection):
    """**取れなかった顔を毎回測り直さない。** 未計測（NULL）と分けて 0 を書く。"""
    person_id = db.add_person(connection, "父")
    _, face_id = _face(connection, 1, person_id)
    EXPRESSION_STATE["blendshapes"] = None

    expression.fill_missing(connection)

    assert _expression_of(connection, face_id) == (0, None, None)
    assert db.faces_without_expression(connection, (db.ASSIGN_MANUAL,)) == []


def test_nothing_is_written_when_the_model_is_missing(connection):
    """**「測れない」と「顔が取れなかった」を混ぜない**（CLAUDE.md §8）。

    モデルの無い環境で 0 を書くと、あとでモデルを置いても二度と測り直されない。
    """
    person_id = db.add_person(connection, "父")
    _, face_id = _face(connection, 1, person_id)
    EXPRESSION_STATE["available"] = False

    assert expression.fill_missing(connection) == 0
    assert expression.measure(np.zeros((10, 10, 3), dtype=np.uint8)) is None
    assert _expression_of(connection, face_id) == (None, None, None)


def test_the_real_loader_gives_up_quietly_without_the_model_file(monkeypatch):
    """実物の読み込み口: `models/` に無ければ None（落ちない・何も読みに行かない）。"""
    from photoarchive_ai import face

    monkeypatch.setattr(face, "find_model_file", lambda _name: None)
    expression.reset_model_cache()
    try:
        assert _REAL_LOADER() is None
    finally:
        expression.reset_model_cache()


def _row(person_id, eye_blink=0.0, mouth_smile=0.0, sharpness=200.0):
    return {
        "person_id": person_id, "aligned": 1, "yaw": 0.0, "sharpness": sharpness,
        "eye_blink": eye_blink, "mouth_smile": mouth_smile,
    }


def test_closed_eyes_lower_the_face_and_an_open_smile_raises_it():
    closed = scoring.family_photo_score([_row(1, eye_blink=0.6)])
    open_neutral = scoring.family_photo_score([_row(1)])
    smiling = scoring.family_photo_score([_row(1, mouth_smile=0.4)])
    assert closed < open_neutral < smiling
    assert smiling - open_neutral == pytest.approx(100.0 * scoring.WEIGHT_SMILE)
    # 目を閉じた家族は人数に数えないので、1人目からの加点（PR #112 の指摘1）も付かない
    assert open_neutral - closed == pytest.approx(100.0 * scoring.WEIGHT_EYES + scoring.MEMBER_BONUS)


def test_a_family_member_with_closed_eyes_is_not_counted_as_clearly_pictured():
    """**目を閉じた家族は人数に数えない**（利用者の決定・ボケと同じ扱い）。"""
    both_open = scoring.family_photo_score([_row(1), _row(2)])
    one_closed = scoring.family_photo_score([_row(1), _row(2, eye_blink=0.5)])
    assert both_open - one_closed == pytest.approx(scoring.MEMBER_BONUS)


def test_an_unmeasured_expression_neither_helps_nor_excludes():
    """**分からないものを外さない。** モデルの無い環境で人数の加点が全部消えないように。"""
    unknown = [_row(1, eye_blink=None, mouth_smile=None), _row(2, eye_blink=None)]
    assert scoring.is_clear_face(1, 200.0, None)
    assert scoring.eyes_level(None) == scoring.smile_level(None) == scoring.UNKNOWN
    assert scoring.family_photo_score(unknown) > scoring.family_photo_score(unknown[:1])


def test_half_closed_eyes_are_scored_between_open_and_closed():
    assert scoring.eyes_level(scoring.EYES_OPEN) == 1.0
    assert scoring.eyes_level(0.4) == pytest.approx(0.5)
    assert scoring.eyes_level(scoring.EYES_CLOSED) == 0.0


def test_select_measures_the_expressions_and_puts_the_open_eyes_first(connection):
    """`select` の並び（`family_scores`）は、未計測の表情を先に測ってから使う。"""
    person_id = db.add_person(connection, "父")
    closed_media, closed_face = _face(connection, 1, person_id)
    open_media, open_face = _face(connection, 2, person_id)
    EXPRESSION_STATE["blendshapes"] = _blendshapes(0.8, 0.9, 0.0, 0.0)
    expression.fill_missing(connection, assign_sources=(db.ASSIGN_MANUAL,))
    connection.execute(
        "UPDATE Face SET expression_found = NULL WHERE id = ?", (open_face,)
    )
    connection.commit()
    EXPRESSION_STATE["blendshapes"] = _blendshapes(0.0, 0.0, 0.0, 0.0)

    scores, _ = family_scores(connection)

    assert _expression_of(connection, open_face)[0] == 1
    assert scores[open_media] > scores[closed_media]


def test_match_measures_the_expressions_before_writing_the_family_score(connection):
    """`match` の `family_score` も同じ式なので、手本の表情を先に測る。"""
    from photoarchive_ai.matcher import match_faces

    person_id = db.add_person(connection, "父")
    _, face_id = _face(connection, 1, person_id)

    match_faces(connection)

    assert _expression_of(connection, face_id)[0] == 1


def test_a_dry_run_of_match_writes_no_expression(connection):
    from photoarchive_ai.matcher import match_faces

    person_id = db.add_person(connection, "父")
    _, face_id = _face(connection, 1, person_id)

    match_faces(connection, dry_run=True)

    assert _expression_of(connection, face_id) == (None, None, None)


def test_a_lone_smile_does_not_push_out_two_family_members_pictured_clearly():
    """**人数の加点は 20**（利用者の決定）。10 のままだと、1人だけ写った笑顔の写真が
    家族のそろった写真を押しのけた（実データの出力で1人の写真が 97 → 240 枚）。

    **境目はちょうど同点**（1人の満点の笑顔 35+25+20+20 = 2人の無表情 80+20）。
    それ以上には押しのけない。同点のときの順は `select` の並びの次の手がかり
    （`quality_score` 以降）で決まる（PR #97 のレビュー指摘2）。
    """
    lone_smile = scoring.family_photo_score([_row(1, mouth_smile=1.0)])
    two_straight_faces = scoring.family_photo_score([_row(1), _row(2)])
    two_with_a_faint_smile = scoring.family_photo_score([_row(1, mouth_smile=0.03), _row(2)])
    assert two_straight_faces == pytest.approx(lone_smile)
    assert two_with_a_faint_smile > lone_smile
