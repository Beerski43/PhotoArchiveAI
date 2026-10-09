"""顔の見え方（整列・向き・鮮明さ）と、整列できない手本の扱い（#66）。

**実データで分かったこと**: 5点整列ができない手本（特徴量が整列されていない）が
他人を引き寄せていた。**割り当ての根拠から外すだけで誤りが −26%**。ただし
**手本を丸ごと消すと誤りが +418 件増えた**（その人物が2位の対抗馬として他人を
止めていた役目まで消えるため）。ここでは、その2つを見張る。
"""

import io
import sqlite3

import numpy as np
import pytest
from PIL import Image

from photoarchive_ai import appearance, db, scoring
from photoarchive_ai.evaluation import evaluate_match
from photoarchive_ai.matcher import MATCH_RULE, match_faces
from photoarchive_ai.migration import migrate_database, needs_migration
from tests.fakes import APPEARANCE_STATE

EUCLIDEAN = db.embedding_model.METRIC_EUCLIDEAN


@pytest.fixture()
def connection(tmp_path):
    connection = db.ensure_database(str(tmp_path / "test.db"))
    yield connection
    connection.close()


def _jpeg(array: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(array.astype(np.uint8)).save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def _checkerboard(size=120, cell=6) -> np.ndarray:
    """くっきりした絵（鮮明さが高い）。"""
    yy, xx = np.mgrid[0:size, 0:size]
    board = (((yy // cell) + (xx // cell)) % 2) * 255
    return np.stack([board] * 3, axis=-1)


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
    vector = np.zeros(db.EMBEDDING_DIM, dtype=np.float32)
    vector[: len(values)] = values
    return vector


def _add_face(connection, media_id, vector, person_id=None, assign_source=None, aligned=None):
    face_id = db.add_face(
        connection,
        media_id=media_id,
        bbox=(0, 10, 10, 0),
        embedding=vector,
        embed_version=db.embedding_model.ACTIVE.version,
        person_id=person_id,
        assign_source=assign_source,
        thumbnail=_jpeg(_checkerboard()),
    )
    if aligned is not None:
        db.save_appearance(
            connection,
            [(face_id, appearance.Appearance(aligned=aligned, yaw=None, sharpness=100.0))],
        )
    connection.commit()
    return face_id


# ---------------------------------------------------------------------------
# 見え方を測る
# ---------------------------------------------------------------------------


def test_a_frontal_face_has_no_yaw_and_a_turned_face_has_some():
    frontal = np.array([[38, 52], [74, 52], [56, 72], [42, 92], [70, 92]], dtype=float)
    turned = frontal.copy()
    turned[2, 0] = 70.0  # 鼻が右目の近くまで寄っている
    assert appearance.yaw_from_points(frontal) == pytest.approx(0.0)
    assert appearance.yaw_from_points(turned) == pytest.approx(14.0 / 36.0)


def test_a_blurred_face_is_less_sharp_than_a_crisp_one():
    import cv2

    crisp = _checkerboard().astype(np.uint8)
    blurred = cv2.GaussianBlur(crisp, (15, 15), 5)
    assert appearance.sharpness_of(blurred) < appearance.sharpness_of(crisp) / 5


def test_a_face_without_landmarks_is_recorded_as_not_aligned():
    APPEARANCE_STATE["aligned"] = False
    result = appearance.measure_thumbnail(_jpeg(_checkerboard()))
    assert result.aligned is False
    assert result.yaw is None


def test_nothing_is_recorded_when_the_landmark_model_is_unavailable(monkeypatch):
    """**「測れない」と「整列できなかった」を混ぜない。**

    FaceMesh が読めない環境で `aligned=False` を書くと、手本が全部根拠から外れ、
    しかも「測った」ことになって二度と測り直されない。
    """
    monkeypatch.setattr(scoring, "_load_mediapipe_face_mesh", lambda: None)
    assert appearance.measure_thumbnail(_jpeg(_checkerboard())) is None


def test_filling_measures_only_assigned_faces_and_can_skip_writing(connection):
    person = db.add_person(connection, "ひより")
    media = _add_media(connection, 1)
    teacher = _add_face(connection, media, _vector(1.0), person, db.ASSIGN_MANUAL)
    loose = _add_face(connection, media, _vector(0.0, 1.0))

    measured = appearance.fill_missing(connection, write=False)
    assert set(measured) == {teacher}
    assert db.get_face(connection, teacher)["sharpness"] is None, "write=False は書かない"

    appearance.fill_missing(connection)
    assert db.get_face(connection, teacher)["aligned"] == 1
    assert db.get_face(connection, teacher)["sharpness"] > 0
    assert db.get_face(connection, loose)["sharpness"] is None, "割り当ての無い顔は測らない"
    assert db.faces_without_appearance(connection, (db.ASSIGN_MANUAL,)) == []


# ---------------------------------------------------------------------------
# 整列できない手本は根拠にしない。ただし対抗馬としては残す
# ---------------------------------------------------------------------------


def test_a_face_close_only_to_an_unaligned_teacher_is_left_unassigned(connection):
    person = db.add_person(connection, "ひより")
    _add_face(connection, _add_media(connection, 1), _vector(1.0), person, db.ASSIGN_MANUAL, False)
    target = _add_face(connection, _add_media(connection, 2), _vector(1.0, 0.01))

    summary = match_faces(connection, metric=EUCLIDEAN, threshold=0.5, margin=0.05)

    assert db.get_face(connection, target)["person_id"] is None
    assert summary["unusable_teachers"] == 1


def test_an_unaligned_teacher_still_blocks_a_stranger_as_the_runner_up(connection):
    """**手本を消すのではなく、根拠にしないだけ。** 対抗馬としては効き続けること。

    実データでは、整列できない手本を丸ごと消すと誤りが +418 件増えた。
    虎太朗の手本が、ひよりと見分けのつかない赤ちゃんの顔を「2位」として止めていた。
    """
    hiyori = db.add_person(connection, "ひより")
    kotaro = db.add_person(connection, "虎太朗")
    _add_face(connection, _add_media(connection, 1), _vector(1.0), hiyori, db.ASSIGN_MANUAL, True)
    # 虎太朗の手本は整列できないが、候補のすぐ近くにいる
    _add_face(connection, _add_media(connection, 2), _vector(1.02), kotaro, db.ASSIGN_MANUAL, False)
    target = _add_face(connection, _add_media(connection, 3), _vector(1.01))

    match_faces(connection, metric=EUCLIDEAN, threshold=0.5, margin=0.05)

    assert db.get_face(connection, target)["person_id"] is None, (
        "虎太朗が2位としてマージンを潰すので、ひよりにも付けない"
    )


def test_the_score_comes_from_the_aligned_teacher_that_decided(connection):
    """`assign_score` は受け入れを決めた（整列できた）手本までの距離から出す。

    `select` がこの値で並べるので、受け入れと同じ根拠でなければならない。
    """
    person = db.add_person(connection, "ひより")
    _add_face(connection, _add_media(connection, 1), _vector(1.0), person, db.ASSIGN_MANUAL, False)
    _add_face(connection, _add_media(connection, 2), _vector(1.3), person, db.ASSIGN_MANUAL, True)
    target = _add_face(connection, _add_media(connection, 3), _vector(1.0))

    match_faces(connection, metric=EUCLIDEAN, threshold=0.5, margin=0.05)

    face = db.get_face(connection, target)
    assert face["person_id"] == person
    expected = scoring.distance_to_similarity(0.3)
    assert face["assign_score"] == pytest.approx(expected, abs=1e-4)


def test_unmeasured_teachers_are_measured_before_matching(connection):
    """未計測の手本は `match` が測ってから使う（測った値は DB に残る）。"""
    person = db.add_person(connection, "ひより")
    teacher = _add_face(connection, _add_media(connection, 1), _vector(1.0), person, db.ASSIGN_MANUAL)
    target = _add_face(connection, _add_media(connection, 2), _vector(1.0, 0.01))
    APPEARANCE_STATE["aligned"] = False

    match_faces(connection, metric=EUCLIDEAN, threshold=0.5, margin=0.05)

    assert db.get_face(connection, teacher)["aligned"] == 0
    assert db.get_face(connection, target)["person_id"] is None


def test_auto_assignments_record_the_rule_and_manual_ones_clear_it(connection):
    person = db.add_person(connection, "ひより")
    _add_face(connection, _add_media(connection, 1), _vector(1.0), person, db.ASSIGN_MANUAL, True)
    target = _add_face(connection, _add_media(connection, 2), _vector(1.0, 0.01))

    match_faces(connection, metric=EUCLIDEAN, threshold=0.5, margin=0.05)
    assert db.get_face(connection, target)["assign_rule"] == MATCH_RULE

    # 人が確定したら、規則の印は消える（auto の行だけが持つ）
    db.assign_faces(connection, [target], person)
    assert db.get_face(connection, target)["assign_rule"] is None


def test_evaluate_applies_the_same_rule(connection):
    """**`evaluate` も同じ規則で数える。** 揃えないと実測値が嘘になる。"""
    person = db.add_person(connection, "ひより")
    _add_face(connection, _add_media(connection, 1), _vector(1.0), person, db.ASSIGN_MANUAL, False)
    _add_face(connection, _add_media(connection, 2), _vector(1.01), person, db.ASSIGN_MANUAL, False)

    summary = evaluate_match(connection, thresholds=[0.5], margin=0.05, metric=EUCLIDEAN)

    row = summary["thresholds"][0]
    assert summary["unusable_teachers"] == 2
    assert row["correct"] == 0 and row["missed"] == 2, "整列できない手本しか無いので当てない"


def test_evaluate_does_not_write_what_it_measures(connection):
    person = db.add_person(connection, "ひより")
    teacher = _add_face(connection, _add_media(connection, 1), _vector(1.0), person, db.ASSIGN_MANUAL)
    _add_face(connection, _add_media(connection, 2), _vector(1.01), person, db.ASSIGN_MANUAL)

    evaluate_match(connection, thresholds=[0.5], margin=0.05, metric=EUCLIDEAN)

    assert db.get_face(connection, teacher)["sharpness"] is None


# ---------------------------------------------------------------------------
# v4 → v5 の移行（列を足すだけ。顔は減らない）
# ---------------------------------------------------------------------------


def test_a_version_4_database_gains_the_appearance_columns_and_keeps_its_faces(tmp_path):
    """**移行して顔が減らないこと**（CLAUDE.md §4）。実データが通る経路。"""
    path = tmp_path / "v4.db"
    connection = db.ensure_database(str(path))
    person = db.add_person(connection, "ひより")
    media = _add_media(connection, 1)
    for index in range(4):
        _add_face(connection, media, _vector(float(index)), person, db.ASSIGN_MANUAL)
    connection.close()

    raw = sqlite3.connect(str(path))
    for column in ("aligned", "yaw", "sharpness", "assign_rule"):
        raw.execute(f"ALTER TABLE Face DROP COLUMN {column}")
    raw.execute("PRAGMA user_version = 4")
    raw.commit()
    raw.close()
    assert needs_migration(str(path)) is True

    migrate_database(str(path), make_backup=False)

    raw = sqlite3.connect(str(path))
    try:
        columns = {row[1] for row in raw.execute("PRAGMA table_info(Face)")}
        assert {"aligned", "yaw", "sharpness", "assign_rule"} <= columns
        assert raw.execute("SELECT COUNT(*) FROM Face").fetchone()[0] == 4
        assert (
            raw.execute("SELECT COUNT(*) FROM Face WHERE assign_source = 'manual'").fetchone()[0]
            == 4
        )
        assert raw.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    finally:
        raw.close()
    assert needs_migration(str(path)) is False


# ---------------------------------------------------------------------------
# #66 手本の年齢で閾値に上限を置く（8歳以下 0.35・年齢不明 0.40）
# ---------------------------------------------------------------------------


def _dated_media(connection, index: int, shooting_date: str) -> int:
    return db.save_media(
        connection,
        {
            "path": f"/photos/d{index}.jpg",
            "filename": f"d{index}.jpg",
            "type": "image",
            "file_hash": f"dated{index}",
            "file_size": 100,
            "created_time": shooting_date,
            "shooting_date": shooting_date,
        },
    )


def test_the_age_limits_only_tighten():
    model = db.embedding_model.ACTIVE
    assert model.limit_for_age(0, 0.45) == 0.35
    assert model.limit_for_age(8, 0.45) == 0.35
    assert model.limit_for_age(9, 0.45) == 0.45
    assert model.limit_for_age(None, 0.45) == 0.40
    assert model.limit_for_age(float("nan"), 0.45) == 0.40
    # 閾値より緩めることは無い
    assert model.limit_for_age(3, 0.30) == 0.30
    assert model.limit_for_age(None, 0.30) == 0.30


def test_a_baby_teacher_needs_a_closer_face_than_an_adult_teacher(connection):
    """**赤ちゃんの顔は誰でも互いに近い**（ひよりの誤りの 83% が 0〜5歳の手本に
    引き寄せられていた）。8歳以下の手本は 0.35 までしか受け入れない。"""
    baby = db.add_person(connection, "ひより", birth_date="2010-12-08")
    adult = db.add_person(connection, "義行", birth_date="1978-09-11")
    _add_face(connection, _dated_media(connection, 1, "2012-01-01T00:00:00"), _vector(1.0), baby,
              db.ASSIGN_MANUAL, True)
    _add_face(connection, _dated_media(connection, 2, "2012-01-01T00:00:00"), _vector(0.0, 0.0, 5.0),
              adult, db.ASSIGN_MANUAL, True)
    near_baby = _add_face(connection, _dated_media(connection, 3, "2012-02-01T00:00:00"),
                          _vector(1.0, 0.40))
    near_adult = _add_face(connection, _dated_media(connection, 4, "2012-02-01T00:00:00"),
                           _vector(0.0, 0.40, 5.0))

    match_faces(connection, metric=EUCLIDEAN, threshold=0.45, margin=0.05)

    assert db.get_face(connection, near_baby)["person_id"] is None
    assert db.get_face(connection, near_adult)["person_id"] == adult


def test_a_teacher_of_unknown_age_is_limited_to_0_40(connection):
    person = db.add_person(connection, "ひより")  # 誕生日なし・年齢なし
    _add_face(connection, _add_media(connection, 1), _vector(1.0), person, db.ASSIGN_MANUAL, True)
    inside = _add_face(connection, _add_media(connection, 2), _vector(1.0, 0.38))
    outside = _add_face(connection, _add_media(connection, 3), _vector(1.0, 0.0, 0.42))

    match_faces(connection, metric=EUCLIDEAN, threshold=0.45, margin=0.05)

    assert db.get_face(connection, inside)["person_id"] == person
    assert db.get_face(connection, outside)["person_id"] is None


def test_the_confirmed_age_wins_over_the_calculated_one(connection):
    """確定値（`Face.age`）を優先する。計算値は誕生日と撮影日時から。"""
    person = db.add_person(connection, "ひより", birth_date="2010-12-08")
    teacher = _add_face(connection, _dated_media(connection, 1, "2012-01-01T00:00:00"), _vector(1.0),
                        person, db.ASSIGN_MANUAL, True)
    assert db.load_manual_faces(connection).ages.tolist() == [1.0]
    db.set_face_age(connection, teacher, 20)
    assert db.load_manual_faces(connection).ages.tolist() == [20.0]


def test_a_rejected_young_teacher_does_not_hand_the_face_to_someone_else(connection):
    """**上限は受け入れにだけ効かせ、勝者とマージンには効かせない。**

    距離に足し引きする形で入れると、勝つ人物が入れ替わり、マージンで止まって
    いた他人が別の人物へ流れた（実データで +258 件）。
    """
    baby = db.add_person(connection, "ひより", birth_date="2010-12-08")
    adult = db.add_person(connection, "奈津子", birth_date="1975-05-08")
    _add_face(connection, _dated_media(connection, 1, "2012-01-01T00:00:00"), _vector(1.0), baby,
              db.ASSIGN_MANUAL, True)
    # 大人の手本は候補から 0.44（閾値の内側）。赤ちゃんの手本は 0.40 で1位。
    # 距離に 0.10 を足す形なら赤ちゃんが 0.50 に退き、大人が差 0.06 で勝ってしまう。
    _add_face(connection, _dated_media(connection, 2, "2012-01-01T00:00:00"),
              _vector(1.0, 0.40, 0.44), adult, db.ASSIGN_MANUAL, True)
    target = _add_face(connection, _dated_media(connection, 3, "2012-02-01T00:00:00"),
                       _vector(1.0, 0.40))

    match_faces(connection, metric=EUCLIDEAN, threshold=0.45, margin=0.05)

    assert db.get_face(connection, target)["person_id"] is None, (
        "赤ちゃんが1位で、大人との差 0.04 はマージンに足りない。上限で外れても大人へは渡さない"
    )


def test_evaluate_applies_the_age_limits_too(connection):
    person = db.add_person(connection, "ひより", birth_date="2010-12-08")
    for index in range(2):
        _add_face(connection, _dated_media(connection, index, f"2012-0{index + 1}-01T00:00:00"),
                  _vector(1.0, 0.40 * index), person, db.ASSIGN_MANUAL, True)

    summary = evaluate_match(connection, thresholds=[0.45], margin=0.05, metric=EUCLIDEAN)

    assert summary["thresholds"][0]["correct"] == 0, "1歳の手本どうしは 0.35 までしか当てない"
