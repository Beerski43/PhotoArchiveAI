import numpy as np
import pytest

from photoarchive_ai import db
from photoarchive_ai.matcher import match_faces

#: **このファイルのテストはユークリッド距離を前提に座標を組んでいる。**
#: いま使うモデル(ArcFace)はコサイン距離なので、`metric` を明示して渡す。
#: ここで試しているのは matcher の**判断の仕組み**（閾値・マージン・手本の
#: 選び方・冪等性）で、尺度に依存しない。尺度そのものは
#: `test_matcher_metrics.py` で確かめる。
EUCLIDEAN = db.embedding_model.METRIC_EUCLIDEAN


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
    vector = np.zeros(db.EMBEDDING_DIM, dtype=np.float32)
    vector[: len(values)] = values
    return vector


def _add_face(connection, media_id, vector, person_id=None, assign_source=None):
    face_id = db.add_face(
        connection,
        media_id=media_id,
        bbox=(0, 10, 10, 0),
        embedding=vector,
        embed_version=db.embedding_model.ACTIVE.version,
        person_id=person_id,
        assign_source=assign_source,
    )
    connection.commit()
    return face_id


def test_match_assigns_the_correct_person_with_uneven_teacher_counts(connection):
    """人物ごとに手本の枚数が違っても、正しい人物に紐づくこと。

    旧実装は全人物の特徴量を1本のリストに連結しておきながら、人数で剰余を
    取って人物を引いていた (`person_ids[best_index % len(person_ids)]`)。
    1人に2枚以上登録した時点で紐づく人物が壊れていた。
    """
    alice = db.add_person(connection, "Alice")
    bob = db.add_person(connection, "Bob")
    carol = db.add_person(connection, "Carol")

    media = _add_media(connection, 1)
    # Alice は3枚、Bob は1枚、Carol は2枚
    for offset in (0.00, 0.01, 0.02):
        _add_face(connection, media, _vector(0.0 + offset, 0.0), alice, db.ASSIGN_MANUAL)
    _add_face(connection, media, _vector(1.0, 0.0), bob, db.ASSIGN_MANUAL)
    for offset in (0.00, 0.01):
        _add_face(connection, media, _vector(0.0, 1.0 + offset), carol, db.ASSIGN_MANUAL)

    target_media = _add_media(connection, 2)
    carol_face = _add_face(connection, target_media, _vector(0.0, 1.005))

    summary = match_faces(connection, metric=EUCLIDEAN, threshold=0.5, margin=0.05)

    assert summary["teachers"] == 6
    assert summary["assigned"] == 1
    record = db.get_face(connection, carol_face)
    assert record["person_id"] == carol
    assert record["assign_source"] == db.ASSIGN_AUTO


def test_match_leaves_distant_faces_unassigned(connection):
    """似ていない顔は未割当のまま残すこと。

    旧実装は閾値判定が無く、どれだけ遠くても必ず誰かに紐づけていた (#22)。
    """
    person = db.add_person(connection, "Alice")
    media = _add_media(connection, 1)
    _add_face(connection, media, _vector(0.0, 0.0), person, db.ASSIGN_MANUAL)

    stranger_media = _add_media(connection, 2)
    stranger = _add_face(connection, stranger_media, _vector(5.0, 5.0))

    summary = match_faces(connection, metric=EUCLIDEAN, threshold=0.5)

    assert summary["assigned"] == 0
    assert summary["unassigned"] == 1
    record = db.get_face(connection, stranger)
    assert record["person_id"] is None
    assert record["assign_source"] is None


def test_match_rejects_ambiguous_faces(connection):
    """2人の中間にある顔は、どちらにも決めずに残す。"""
    alice = db.add_person(connection, "Alice")
    bob = db.add_person(connection, "Bob")
    media = _add_media(connection, 1)
    _add_face(connection, media, _vector(0.0, 0.0), alice, db.ASSIGN_MANUAL)
    _add_face(connection, media, _vector(0.2, 0.0), bob, db.ASSIGN_MANUAL)

    target = _add_media(connection, 2)
    ambiguous = _add_face(connection, target, _vector(0.1, 0.0))

    match_faces(connection, metric=EUCLIDEAN, threshold=0.5, margin=0.05)

    assert db.get_face(connection, ambiguous)["person_id"] is None


def test_match_does_not_learn_from_auto_or_rejected_faces(connection):
    alice = db.add_person(connection, "Alice")
    media = _add_media(connection, 1)
    _add_face(connection, media, _vector(0.0, 0.0), alice, db.ASSIGN_AUTO)
    _add_face(connection, media, _vector(0.0, 0.0), None, db.ASSIGN_REJECTED)

    summary = match_faces(connection, metric=EUCLIDEAN, threshold=0.5, reset=False)

    assert summary["teachers"] == 0
    assert summary["assigned"] == 0


def test_match_is_idempotent_and_reset_keeps_manual(connection):
    alice = db.add_person(connection, "Alice")
    media = _add_media(connection, 1)
    manual = _add_face(connection, media, _vector(0.0, 0.0), alice, db.ASSIGN_MANUAL)
    target_media = _add_media(connection, 2)
    _add_face(connection, target_media, _vector(0.001, 0.0))

    first = match_faces(connection, metric=EUCLIDEAN, threshold=0.5)
    second = match_faces(connection, metric=EUCLIDEAN, threshold=0.5)

    assert first["assigned"] == second["assigned"] == 1
    assert second["reset"] == 1
    assert db.get_face(connection, manual)["assign_source"] == db.ASSIGN_MANUAL
    assert db.count_faces(connection, assign_source=db.ASSIGN_AUTO) == 1


def test_match_updates_family_score(connection):
    alice = db.add_person(connection, "Alice")
    media = _add_media(connection, 1)
    _add_face(connection, media, _vector(0.0, 0.0), alice, db.ASSIGN_MANUAL)
    other_media = _add_media(connection, 2)
    _add_face(connection, other_media, _vector(0.001, 0.0))
    stranger_media = _add_media(connection, 3)
    _add_face(connection, stranger_media, _vector(9.0, 0.0))

    match_faces(connection, metric=EUCLIDEAN, threshold=0.5)

    assert db.get_analysis_result(connection, media)["family_score"] == 100.0
    assert db.get_analysis_result(connection, other_media)["family_score"] > 0.0
    assert db.get_analysis_result(connection, stranger_media).get("family_score") in (None, 0.0)


def test_match_without_teachers_does_nothing(connection):
    media = _add_media(connection, 1)
    face_id = _add_face(connection, media, _vector(0.0, 0.0))

    summary = match_faces(connection, metric=EUCLIDEAN, threshold=0.5)

    assert summary["teachers"] == 0
    assert db.get_face(connection, face_id)["person_id"] is None


def test_match_dry_run_does_not_write(connection):
    alice = db.add_person(connection, "Alice")
    media = _add_media(connection, 1)
    _add_face(connection, media, _vector(0.0, 0.0), alice, db.ASSIGN_MANUAL)
    target_media = _add_media(connection, 2)
    target = _add_face(connection, target_media, _vector(0.001, 0.0))

    summary = match_faces(connection, metric=EUCLIDEAN, threshold=0.5, dry_run=True)

    assert summary["assigned"] == 1
    assert db.get_face(connection, target)["person_id"] is None


def test_dry_run_is_not_blinded_by_a_previous_match(connection):
    """2回目以降の --dry-run が空振りしないこと。

    dry-run は自動割り当てを取り消さないので、候補を「未割当」に
    限ると、前回 auto が付いた顔を数え落として件数も距離の分布も
    実際より小さく出ていた。--threshold を決める目安にならない。
    """
    media = _add_media(connection, 1)
    _add_face(connection, media, _vector(0.0), person_id=_person(connection, "父"),
              assign_source=db.ASSIGN_MANUAL)
    for offset in range(3):
        _add_face(connection, _add_media(connection, 10 + offset), _vector(0.01 * offset))

    first = match_faces(connection, metric=EUCLIDEAN, dry_run=True)
    match_faces(connection, metric=EUCLIDEAN)
    second = match_faces(connection, metric=EUCLIDEAN, dry_run=True)

    assert first["candidates"] == 3
    assert second["candidates"] == first["candidates"]
    assert second["assigned"] == first["assigned"]
    assert second["histogram"] == first["histogram"]


def test_dry_run_still_writes_nothing_after_a_real_match(connection):
    media = _add_media(connection, 1)
    person_id = _person(connection, "父")
    _add_face(connection, media, _vector(0.0), person_id=person_id,
              assign_source=db.ASSIGN_MANUAL)
    _add_face(connection, _add_media(connection, 2), _vector(0.01))

    match_faces(connection, metric=EUCLIDEAN)
    before = {row["id"]: row["assign_source"] for row in db.list_faces(connection)}
    match_faces(connection, metric=EUCLIDEAN, dry_run=True)

    assert {row["id"]: row["assign_source"] for row in db.list_faces(connection)} == before


def test_progress_reaches_the_end_even_when_some_faces_have_no_embedding(connection):
    """進捗の分母が、実際に比べる顔の数と合っていること。"""
    person_id = _person(connection, "父")
    _add_face(connection, _add_media(connection, 1), _vector(0.0), person_id=person_id,
              assign_source=db.ASSIGN_MANUAL)
    _add_face(connection, _add_media(connection, 2), _vector(0.01))
    # 小さすぎて特徴量が作れなかった顔。match の対象外。
    db.add_face(
        connection,
        media_id=_add_media(connection, 3),
        bbox=(0, 10, 10, 0),
        embedding=None,
        embed_version=db.embedding_model.ACTIVE.version,
    )
    connection.commit()

    seen = []
    match_faces(connection, metric=EUCLIDEAN, progress_callback=lambda *args: seen.append(args))

    assert seen
    current, total, _ = seen[-1]
    assert current == total == 1


def _person(connection, name: str) -> int:
    return db.add_person(connection, name)


def _dated_media(connection, index: int, shooting_date):
    """撮影日時を持つメディア。``shooting_date`` に壊れた値も渡せる。"""
    media_id = _add_media(connection, index)
    connection.execute(
        "UPDATE Media SET shooting_date = ? WHERE id = ?", (shooting_date, media_id)
    )
    connection.commit()
    return media_id


def _births(connection, **people):
    """名前→誕生日 で人物を作り、``{名前: id}`` を返す。"""
    return {
        name: db.add_person(connection, name, birth_date=birth)
        for name, birth in people.items()
    }


def test_a_person_is_not_assigned_to_a_photo_taken_before_they_were_born(connection):
    """**生まれる前の写真には写れない。** 動かせない事実なので候補から外す。

    実データでは、${PERSON_4}の手本（赤ん坊の顔が11件）が 2004〜2007 年の写真の
    赤ん坊を 351 件引き寄せていた。**赤ん坊の顔は兄弟間でほとんど区別がつかない。**
    """
    people = _births(connection, 兄="2009-02-01", 妹="2010-12-01")
    teacher = _dated_media(connection, 1, "2011-06-01T10:00:00")
    _add_face(connection, teacher, _vector(1.0, 0.0), people["妹"], db.ASSIGN_MANUAL)
    # 妹が生まれる2年前の写真。顔は妹の手本とそっくり。
    before = _dated_media(connection, 2, "2008-06-01T10:00:00")
    _add_face(connection, before, _vector(1.0, 0.0))

    summary = match_faces(connection, threshold=0.4, margin=0.0, metric=EUCLIDEAN)

    assert summary["assigned"] == 0
    assert summary["unassigned"] == 1
    assert summary["no_candidate"] == 1, "候補が1人も残らなかったぶん"
    assert db.list_faces(connection, unassigned=True)[0]["person_id"] is None


def test_removing_an_impossible_person_lets_the_margin_through(connection):
    """**絞るのは `_best_match` に渡す前でなければならない。**

    あとから捨てると、ありえない人物が「2位」に居座ってマージンを潰し、
    判断が保留のままになる。**実データではこれが効いて、未割当だった
    457 件が正しく兄へ付いた。**
    """
    people = _births(connection, 兄="2009-02-01", 妹="2010-12-01")
    teacher = _dated_media(connection, 1, "2011-06-01T10:00:00")
    # 兄と妹の手本が近い（赤ん坊どうしで区別がつかない状況）。
    _add_face(connection, teacher, _vector(0.00, 0.0), people["兄"], db.ASSIGN_MANUAL)
    _add_face(connection, teacher, _vector(0.02, 0.0), people["妹"], db.ASSIGN_MANUAL)
    # 妹が生まれる前の写真。兄に 0.01、妹に 0.01 で、マージン 0.2 を満たさない。
    before = _dated_media(connection, 2, "2009-06-01T10:00:00")
    _add_face(connection, before, _vector(0.01, 0.0))

    summary = match_faces(connection, threshold=0.4, margin=0.2, metric=EUCLIDEAN)

    assert summary["assigned"] == 1, "妹が候補から外れて、兄に決まる"
    assert summary["per_person"] == {people["兄"]: 1}
    assert summary["no_candidate"] == 0


def test_a_face_without_a_shooting_date_is_not_filtered(connection):
    """**分からないものを弾かない。** 実データの約16%に撮影日時が無い。"""
    people = _births(connection, 妹="2010-12-01")
    teacher = _dated_media(connection, 1, "2011-06-01T10:00:00")
    _add_face(connection, teacher, _vector(1.0, 0.0), people["妹"], db.ASSIGN_MANUAL)
    undated = _dated_media(connection, 2, None)
    _add_face(connection, undated, _vector(1.0, 0.0))

    summary = match_faces(connection, threshold=0.4, margin=0.0, metric=EUCLIDEAN)

    assert summary["assigned"] == 1
    assert summary["no_candidate"] == 0


@pytest.mark.parametrize("broken", ["0000-00-00", "TTTT-TT-TTTTT:TT:TT", "", "not a date"])
def test_a_broken_shooting_date_does_not_filter_anyone_out(connection, broken):
    """**壊れた日付で候補を絞らない。**

    このリポジトリは「読める撮影日時か」の判断で2度壊れている
    （`0000-00-00` だけを見ていて `TTTT-TT-TTTTT:TT:TT` が素通りした）。
    判断は `dates.parse_date` の1か所に預けてあり、**ここには書かない**。
    """
    people = _births(connection, 妹="2010-12-01")
    teacher = _dated_media(connection, 1, "2011-06-01T10:00:00")
    _add_face(connection, teacher, _vector(1.0, 0.0), people["妹"], db.ASSIGN_MANUAL)
    broken_media = _dated_media(connection, 2, broken)
    _add_face(connection, broken_media, _vector(1.0, 0.0))

    summary = match_faces(connection, threshold=0.4, margin=0.0, metric=EUCLIDEAN)

    assert summary["assigned"] == 1, f"{broken!r} で絞り込んではいけない"
    assert summary["no_candidate"] == 0


def test_a_person_without_a_birth_date_is_never_filtered_out(connection):
    """誕生日は任意の項目。登録していない人物を外さない。"""
    person = db.add_person(connection, "名無し")
    teacher = _dated_media(connection, 1, "2011-06-01T10:00:00")
    _add_face(connection, teacher, _vector(1.0, 0.0), person, db.ASSIGN_MANUAL)
    old = _dated_media(connection, 2, "1990-06-01T10:00:00")
    _add_face(connection, old, _vector(1.0, 0.0))

    summary = match_faces(connection, threshold=0.4, margin=0.0, metric=EUCLIDEAN)

    assert summary["assigned"] == 1
    assert summary["no_candidate"] == 0


def test_being_born_on_the_day_of_the_photo_still_counts(connection):
    """**誕生日当日は0歳。** 境界で1日ぶんずれて弾かないこと。"""
    people = _births(connection, 赤ん坊="2011-06-01")
    teacher = _dated_media(connection, 1, "2012-06-01T10:00:00")
    _add_face(connection, teacher, _vector(1.0, 0.0), people["赤ん坊"], db.ASSIGN_MANUAL)
    same_day = _dated_media(connection, 2, "2011-06-01T18:00:00")
    _add_face(connection, same_day, _vector(1.0, 0.0))

    summary = match_faces(connection, threshold=0.4, margin=0.0, metric=EUCLIDEAN)

    assert summary["assigned"] == 1
    assert summary["no_candidate"] == 0


def test_the_distance_histogram_leaves_out_faces_with_no_candidate(connection):
    """候補が1人も残らなかった顔には距離が無い。**分布に混ぜない。**"""
    people = _births(connection, 妹="2010-12-01")
    teacher = _dated_media(connection, 1, "2011-06-01T10:00:00")
    _add_face(connection, teacher, _vector(1.0, 0.0), people["妹"], db.ASSIGN_MANUAL)
    before = _dated_media(connection, 2, "2005-06-01T10:00:00")
    _add_face(connection, before, _vector(1.0, 0.0))

    summary = match_faces(connection, threshold=0.4, margin=0.0, metric=EUCLIDEAN)

    assert sum(summary["histogram"].values()) == 0
    assert summary["no_candidate"] == 1


def test_a_face_marked_as_not_this_person_is_not_assigned_to_them_again(connection):
    """**「割り当てを解除」だけでは、match を流すたびに同じ誤りが戻る。**

    `match` は手本と閾値だけで結果が決まるので、未割当に戻した判断はどこにも
    残らない。実データでは、${PERSON_4}の自動割り当てを見直して解除した **1,785 件**が
    これに当たった。**否定を残して初めて、その判断が次の match に効く。**
    """
    people = _births(connection, 兄="2009-02-01", 妹="2010-12-01")
    teacher = _dated_media(connection, 1, "2011-06-01T10:00:00")
    _add_face(connection, teacher, _vector(1.0, 0.0), people["妹"], db.ASSIGN_MANUAL)
    target_media = _dated_media(connection, 2, "2012-06-01T10:00:00")
    face_id = _add_face(connection, target_media, _vector(1.0, 0.0))

    assert match_faces(connection, threshold=0.4, margin=0.0, metric=EUCLIDEAN)["assigned"] == 1

    # 人が「これは妹ではない」と押す。
    db.reject_faces_for_person(connection, [face_id], people["妹"])

    summary = match_faces(connection, threshold=0.4, margin=0.0, metric=EUCLIDEAN)

    assert summary["assigned"] == 0
    assert db.get_face(connection, face_id)["person_id"] is None


def test_not_this_person_still_allows_another_person(connection):
    """**「この人物ではない」は「誰でもない」ではない。**

    兄弟の赤ん坊は互いによく似ている。妹を否定したら、**兄には付いてよい。**
    `assign_source='rejected'` との違いがここ。
    """
    people = _births(connection, 兄="2009-02-01", 妹="2010-12-01")
    teacher = _dated_media(connection, 1, "2011-06-01T10:00:00")
    _add_face(connection, teacher, _vector(1.00, 0.0), people["妹"], db.ASSIGN_MANUAL)
    _add_face(connection, teacher, _vector(1.02, 0.0), people["兄"], db.ASSIGN_MANUAL)
    target = _dated_media(connection, 2, "2012-06-01T10:00:00")
    face_id = _add_face(connection, target, _vector(1.01, 0.0))

    db.reject_faces_for_person(connection, [face_id], people["妹"])
    summary = match_faces(connection, threshold=0.4, margin=0.0, metric=EUCLIDEAN)

    assert summary["per_person"] == {people["兄"]: 1}


def test_rejecting_for_one_person_does_not_touch_another_persons_assignment(connection):
    """別の人物に割り当たっている顔を巻き込まない。"""
    people = _births(connection, 兄="2009-02-01", 妹="2010-12-01")
    media = _dated_media(connection, 1, "2012-06-01T10:00:00")
    brother_face = _add_face(
        connection, media, _vector(0.0, 0.0), people["兄"], db.ASSIGN_MANUAL
    )

    db.reject_faces_for_person(connection, [brother_face], people["妹"])

    row = db.get_face(connection, brother_face)
    assert row["person_id"] == people["兄"], "兄の割り当ては残る"
    assert row["assign_source"] == db.ASSIGN_MANUAL


def test_marking_not_this_person_clears_that_persons_assignment(connection):
    """その人物に割り当たっていたなら外す。**記録が矛盾しないように。**"""
    people = _births(connection, 妹="2010-12-01")
    media = _dated_media(connection, 1, "2012-06-01T10:00:00")
    face_id = _add_face(
        connection, media, _vector(0.0, 0.0), people["妹"], db.ASSIGN_MANUAL
    )

    db.reject_faces_for_person(connection, [face_id], people["妹"])

    row = db.get_face(connection, face_id)
    assert row["person_id"] is None
    assert row["assign_source"] is None


def test_the_rejection_can_be_undone(connection):
    """**押し間違いから戻れること。**"""
    people = _births(connection, 妹="2010-12-01")
    teacher = _dated_media(connection, 1, "2011-06-01T10:00:00")
    _add_face(connection, teacher, _vector(1.0, 0.0), people["妹"], db.ASSIGN_MANUAL)
    target = _dated_media(connection, 2, "2012-06-01T10:00:00")
    face_id = _add_face(connection, target, _vector(1.0, 0.0))
    db.reject_faces_for_person(connection, [face_id], people["妹"])
    assert match_faces(connection, threshold=0.4, margin=0.0, metric=EUCLIDEAN)["assigned"] == 0

    db.clear_person_rejections(connection, [face_id], people["妹"])

    assert match_faces(connection, threshold=0.4, margin=0.0, metric=EUCLIDEAN)["assigned"] == 1


def test_marking_the_same_face_twice_is_harmless(connection):
    """同じ顔を2回押しても落ちない（主キーの衝突）。"""
    people = _births(connection, 妹="2010-12-01")
    media = _dated_media(connection, 1, "2012-06-01T10:00:00")
    face_id = _add_face(connection, media, _vector(0.0, 0.0))

    db.reject_faces_for_person(connection, [face_id], people["妹"])
    db.reject_faces_for_person(connection, [face_id], people["妹"])

    assert db.count_person_rejections(connection, people["妹"]) == 1


def test_progress_is_reported_before_the_first_chunk(connection):
    """**照合の前の準備でも進み具合を知らせる。**

    自動割当の取り消しと手本の読み込みは、実データで約5秒かかる。そのあいだ
    何も知らせないと、GUI から流したときに窓が固まって見える（GNOME は5秒で
    「応答なし」と出す）。#67 で GUI から流す口を作って測った。
    """
    person_id = db.add_person(connection, "${PERSON_4}")
    _add_face(connection, _add_media(connection, 0), _vector(1.0), person_id, db.ASSIGN_MANUAL)
    _add_face(connection, _add_media(connection, 1), _vector(1.0))
    calls = []

    match_faces(connection, progress_callback=lambda done, total, detail: calls.append((done, total)))

    assert calls[0] == (0, 0), "取り消しが済んだところ（件数はまだ分からない）"
    assert calls[1] == (0, 1), "手本を読み終え、照合する件数が決まったところ"
    assert calls[-1] == (1, 1)
