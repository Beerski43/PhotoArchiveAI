"""取りこぼし率の実測（1件抜き交差検証）。

**この測定が甘く出ると、閾値の判断ごと間違える。** 手本を抜いたつもりで
抜けていない、対象外にすべき顔を取りこぼしに数える、といった穴が無いかを見る。
"""

import unicodedata

import numpy as np
import pytest

from photoarchive_ai import db
from photoarchive_ai.evaluation import (
    CORRECT,
    MISSED,
    WRONG,
    evaluate_match,
    format_report,
)


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


def _add_face(connection, media_id, vector, person_id, assign_source=db.ASSIGN_MANUAL):
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


def _row(summary, threshold):
    for row in summary["thresholds"]:
        if abs(row["threshold"] - threshold) < 1e-9:
            return row
    raise AssertionError(f"閾値 {threshold} の行がない")


def _two_people_in_separate_photos(connection, spread=0.01):
    """2人ぶんの手本を、1枚1顔で別々の写真に置く。

    ``spread`` が同一人物の中での散らばり。大きくすると同じ人物の手本からも
    遠くなり、取りこぼしが起きるようになる。
    """
    alice = db.add_person(connection, "Alice")
    bob = db.add_person(connection, "Bob")
    index = 0
    for person, base in ((alice, 0.0), (bob, 1.0)):
        for step in range(3):
            index += 1
            media = _add_media(connection, index)
            _add_face(connection, media, _vector(base + step * spread, 0.0), person)
    return alice, bob


def test_a_face_returns_to_its_own_person_when_the_teachers_are_close(connection):
    _two_people_in_separate_photos(connection, spread=0.01)

    summary = evaluate_match(connection, thresholds=[0.4])

    assert summary["teachers"] == 6
    assert summary["evaluated"] == 6
    assert summary["skipped"] == 0
    row = _row(summary, 0.4)
    assert (row[CORRECT], row[MISSED], row[WRONG]) == (6, 0, 0)
    assert row["accuracy"] == 1.0
    assert row["miss_rate"] == 0.0


def test_lowering_the_threshold_turns_correct_answers_into_missed_ones(connection):
    """**この測定の目的そのもの。** 閾値を絞るほど取りこぼしが増えること。

    同一人物の手本を 0.2 ずつ離して置くと、閾値 0.1 では自分の手本にも
    届かなくなる。ここが動かないなら、閾値を変えても結果が変わっておらず、
    測定として成り立っていない。
    """
    _two_people_in_separate_photos(connection, spread=0.2)

    summary = evaluate_match(connection, thresholds=[0.1, 0.5])

    tight, loose = _row(summary, 0.1), _row(summary, 0.5)
    assert tight[MISSED] > loose[MISSED]
    assert tight[CORRECT] < loose[CORRECT]
    assert tight[MISSED] + tight[CORRECT] + tight[WRONG] == summary["evaluated"]


def test_a_face_that_lands_on_another_person_is_counted_as_wrong_not_missed(connection):
    """誤りと取りこぼしを混ぜない。**直し方が違う。**

    取りこぼしは閾値を緩めれば拾えるが、誤りは緩めるほど増える。
    """
    alice = db.add_person(connection, "Alice")
    bob = db.add_person(connection, "Bob")
    for index, (person, value) in enumerate(
        ((alice, 0.0), (alice, 1.00), (bob, 1.01), (bob, 1.02)), start=1
    ):
        _add_face(connection, _add_media(connection, index), _vector(value, 0.0), person)

    summary = evaluate_match(connection, thresholds=[0.4])

    row = _row(summary, 0.4)
    # 1.00 に置いた Alice の顔は、自分の手本(0.0)より Bob の手本のほうが近い。
    assert row[WRONG] == 1
    assert row["per_person"][alice][WRONG] == 1


def test_a_teacher_in_the_same_photo_is_not_allowed_to_answer_for_the_face(connection):
    """同じ写真に写る同一人物の手本を既定で外すこと。

    外さないと、抜いた顔とほぼ同じ手本が残って必ず当たる。実運用で紐づけたい
    のは別の写真に写った顔なので、それでは取りこぼし率が0に見えてしまう。
    """
    alice = db.add_person(connection, "Alice")
    bob = db.add_person(connection, "Bob")
    shared = _add_media(connection, 1)
    _add_face(connection, shared, _vector(0.0, 0.0), alice)
    _add_face(connection, shared, _vector(0.001, 0.0), alice)
    # Bob は遠くに2枚。Alice の顔が Bob に吸われないようにするため。
    for index, offset in enumerate((0.0, 0.01), start=2):
        _add_face(connection, _add_media(connection, index), _vector(5.0 + offset, 0.0), bob)

    excluded = evaluate_match(connection, thresholds=[0.4])
    kept = evaluate_match(connection, thresholds=[0.4], keep_same_media=True)

    # 既定では Alice の2枚は手本を失い、評価から外れる。
    assert excluded["skipped"] == 2
    assert excluded["skipped_per_person"][alice] == 2
    # 残すと、隣に置いた自分自身のような手本で必ず当たる。
    assert kept["skipped"] == 0
    assert _row(kept, 0.4)[CORRECT] == 4


def test_a_person_with_a_single_teacher_is_left_out_instead_of_counted_as_missed(connection):
    """手本が1件だけの人物を取りこぼしに数えない。

    その1件を抜くと手本が0件になり、**構造上かならず**取りこぼす。
    混ぜると取りこぼし率が実態より悪く出て、閾値を緩めすぎる方向に判断が傾く。
    """
    _two_people_in_separate_photos(connection, spread=0.01)
    lonely = db.add_person(connection, "Carol")
    _add_face(connection, _add_media(connection, 99), _vector(0.0, 5.0), lonely)

    summary = evaluate_match(connection, thresholds=[0.4])

    assert summary["teachers"] == 7
    assert summary["evaluated"] == 6
    assert summary["skipped"] == 1
    assert summary["skipped_per_person"] == {lonely: 1}
    assert _row(summary, 0.4)[MISSED] == 0


def test_the_margin_leaves_a_face_between_two_people_unassigned(connection):
    """2人の中間にある顔は、閾値を満たしても割り当てない（match と同じ判定）。"""
    alice = db.add_person(connection, "Alice")
    bob = db.add_person(connection, "Bob")
    for index, (person, value) in enumerate(
        ((alice, 0.0), (alice, 0.02), (bob, 0.04), (bob, 0.06)), start=1
    ):
        _add_face(connection, _add_media(connection, index), _vector(value, 0.0), person)

    generous = evaluate_match(connection, thresholds=[0.4], margin=0.0)
    strict = evaluate_match(connection, thresholds=[0.4], margin=0.5)

    assert _row(strict, 0.4)[MISSED] == strict["evaluated"]
    assert _row(generous, 0.4)[MISSED] == 0


def test_automatic_assignments_are_not_used_as_the_answer_key(connection):
    """手本は手動割り当てだけ。自動の結果を正解として数えない。

    自動の誤りを正解として数えると、誤ったまま「精度が高い」と出る。
    """
    alice = db.add_person(connection, "Alice")
    for index, offset in enumerate((0.0, 0.01), start=1):
        _add_face(connection, _add_media(connection, index), _vector(offset, 0.0), alice)
    _add_face(
        connection,
        _add_media(connection, 3),
        _vector(0.02, 0.0),
        alice,
        assign_source=db.ASSIGN_AUTO,
    )

    summary = evaluate_match(connection, thresholds=[0.4])

    assert summary["teachers"] == 2
    assert summary["teachers_per_person"] == {alice: 2}


def test_a_database_without_any_assigned_face_says_what_to_do(connection):
    summary = evaluate_match(connection)

    assert summary["teachers"] == 0
    assert summary["evaluated"] == 0
    assert all(row["accuracy"] is None for row in summary["thresholds"])
    assert "photoarchive-gui" in format_report(summary)


def test_the_report_shows_each_threshold_and_each_person(connection):
    alice, bob = _two_people_in_separate_photos(connection, spread=0.01)

    report = format_report(evaluate_match(connection, thresholds=[0.4, 0.45]))

    assert "0.40" in report and "0.45" in report
    assert "Alice" in report and "Bob" in report
    assert "取りこぼし" in report


def test_the_report_tells_the_user_when_nothing_could_be_evaluated(connection):
    """全員が手本1件なら、率ではなく次にやることを出す。

    0.0% と出すと「取りこぼしが無い」と読めてしまう。
    """
    for index, name in enumerate(("Alice", "Bob"), start=1):
        person = db.add_person(connection, name)
        _add_face(connection, _add_media(connection, index), _vector(index * 1.0, 0.0), person)

    report = format_report(evaluate_match(connection, thresholds=[0.4]))

    assert "2枚以上" in report
    assert "%" not in report


def test_the_person_column_lines_up_when_names_mix_japanese_and_ascii(connection):
    """列を文字数で揃えると、日本語の名前が混ざった時点でずれる。

    端末では日本語は2桁ぶんを占めるので、見た目の幅で揃える。
    """
    index = 0
    for name in ("父", "Grandma"):
        person = db.add_person(connection, name)
        for step in range(2):
            index += 1
            media = _add_media(connection, index)
            _add_face(connection, media, _vector(len(name) + step * 0.01, 0.0), person)

    report = format_report(evaluate_match(connection, thresholds=[0.4]))
    lines = [line for line in report.splitlines() if "父" in line or "Grandma" in line]

    assert len(lines) == 2
    widths = {
        sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in line)
        for line in lines
    }
    assert len(widths) == 1, f"人物の行の幅が揃っていない: {lines}"


def test_evaluate_leaves_every_row_untouched(connection):
    """読み出しだけで完結すること。仕様 §5.4 / §8.5 の約束をここで固定する。

    「DBには書かない」は docstring・仕様書・README・PR 本文で約束しているが、
    守っていることを確かめる仕掛けが無かった。将来 `evaluate` が実測値を
    `AnalysisResult` に残す、といった拡張をしたときに静かに破れる。

    **ファイルのバイト比較にはしない。** `ensure_database` の
    `CREATE TABLE IF NOT EXISTS` がコミットするだけでヘッダの change counter が
    進むので、中身が同じでもハッシュは変わる（`match` / `select` でも同じ）。
    """
    _two_people_in_separate_photos(connection)
    media_id = db.list_faces(connection, with_thumbnail=False)[0]["media_id"]
    db.save_media_scores(connection, media_id, smile_score=50.0, quality_score=60.0)
    connection.execute(
        "UPDATE AnalysisResult SET family_score = ? WHERE media_id = ?", (77.0, media_id)
    )
    connection.commit()

    def snapshot():
        faces = {
            row["id"]: (row["person_id"], row["assign_source"], row["assign_score"], row["age"])
            for row in db.list_faces(connection, with_thumbnail=False)
        }
        results = {
            row["media_id"]: (row["smile_score"], row["quality_score"], row["family_score"])
            for row in connection.execute("SELECT * FROM AnalysisResult").fetchall()
        }
        return faces, results

    before = snapshot()

    evaluate_match(connection)

    assert snapshot() == before
    # family_score を潰していないことまで押さえる（scan と match が互いの値を
    # 消した過去がある）。
    assert before[1][media_id][2] == 77.0
