"""保存済みサムネイルから特徴量を作り直す（Issue #59）。

**ここが壊れると、手作業で積み上げた手本126件と除外268件が消える。**
`scan --force-rescan` との決定的な違いがそこなので、いちばん厚く見張る。
"""

import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from photoarchive_ai import db, embedding, face, reembed

OLD_VERSION = embedding.DLIB_RESNET.version


def _thumbnail(color=(200, 120, 90), size=120) -> bytes:
    array = np.zeros((size, size, 3), dtype=np.uint8)
    array[:, :] = color
    buffer = io.BytesIO()
    Image.fromarray(array).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


@pytest.fixture()
def connection(tmp_path):
    connection = db.ensure_database(str(tmp_path / "reembed.db"))
    yield connection
    connection.close()


def _add_media(connection, path="/photos/a.jpg") -> int:
    return db.save_media(
        connection,
        {
            "path": path,
            "filename": Path(path).name,
            "type": "image",
            "file_hash": path,
            "file_size": 100,
            "created_time": "2012-10-06T10:00:00",
            "shooting_date": "2012-10-06T10:00:00",
        },
    )


def _add_old_face(connection, media_id, *, color=(200, 120, 90), size=120) -> int:
    """**古い版**（dlib 128次元）の特徴量を持つ顔。作り直しの対象になる。

    **`db.add_face` を通さず、生の BLOB を入れる。** `encode_embedding` は
    いま使うモデルの次元(512)でしか保存を許さないので、公開 API からは
    旧版の顔を作れない。**それが正しい**（混ざるのを防いでいる）。
    実データは差し替え前に入った旧版の顔を持っているので、その状態を
    そのまま再現する。
    """
    cursor = connection.execute(
        "INSERT INTO Face (media_id, bbox_top, bbox_right, bbox_bottom, bbox_left,"
        " embedding, embed_version, thumbnail, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, '2026-09-20T00:00:00')",
        (
            media_id,
            0,
            size,
            size,
            0,
            np.full(embedding.DLIB_RESNET.dimensions, 0.25, dtype=np.float32).tobytes(),
            OLD_VERSION,
            _thumbnail(color, size),
        ),
    )
    connection.commit()
    return int(cursor.lastrowid)


def _raw(connection, face_id: int) -> dict:
    row = connection.execute(
        "SELECT embed_version, person_id, assign_source, assign_score, assigned_at, age,"
        " length(embedding) AS size FROM Face WHERE id = ?",
        (face_id,),
    ).fetchone()
    return dict(row)


# ---------------------------------------------------------------------------
# いちばん大事な一線
# ---------------------------------------------------------------------------


def test_reembed_leaves_every_assignment_alone(connection):
    """**手本・除外・年齢に触らない。**

    `scan --force-rescan` は顔の行を作り直すので、GUI で積み上げたもの
    （実データで手本126件・除外268件）が消える。`reembed` はそれを避けるために
    ある。書き換えるのは `embedding` と `embed_version` だけ。
    """
    person_id = db.add_person(connection, "なつ")
    media_id = _add_media(connection)
    manual = _add_old_face(connection, media_id, color=(200, 120, 90))
    rejected = _add_old_face(connection, media_id, color=(10, 200, 60))
    plain = _add_old_face(connection, media_id, color=(90, 90, 200))
    db.assign_faces(connection, [manual], person_id, age=3)
    db.reject_faces(connection, [rejected])
    before = {face_id: _raw(connection, face_id) for face_id in (manual, rejected, plain)}

    summary = reembed.reembed_faces(connection)

    assert summary["written"] == 3
    for face_id, previous in before.items():
        now = _raw(connection, face_id)
        assert now["person_id"] == previous["person_id"]
        assert now["assign_source"] == previous["assign_source"]
        assert now["assign_score"] == previous["assign_score"]
        assert now["assigned_at"] == previous["assigned_at"]
        assert now["age"] == previous["age"]
        # 版と特徴量だけが変わる
        assert now["embed_version"] == embedding.ACTIVE.version
        assert now["size"] == embedding.ACTIVE.dimensions * 4
    assert db.count_faces(connection, assign_source=db.ASSIGN_MANUAL) == 1
    assert db.count_faces(connection, assign_source=db.ASSIGN_REJECTED) == 1


def test_the_original_photo_is_never_read(connection):
    """**元写真を読まない。** 実データでは NFS から 441GB を読み直すことになる。"""
    media_id = _add_media(connection, "/does/not/exist/a.jpg")
    face_id = _add_old_face(connection, media_id)

    summary = reembed.reembed_faces(connection)

    assert summary["written"] == 1
    assert not Path("/does/not/exist/a.jpg").exists()
    assert _raw(connection, face_id)["embed_version"] == embedding.ACTIVE.version


# ---------------------------------------------------------------------------
# 対象の選び方
# ---------------------------------------------------------------------------


def test_faces_already_on_the_current_version_are_left_out(connection):
    media_id = _add_media(connection)
    fresh = db.add_face(
        connection,
        media_id=media_id,
        bbox=(0, 120, 120, 0),
        embedding=[0.5] * db.EMBEDDING_DIM,
        embed_version=embedding.ACTIVE.version,
        thumbnail=_thumbnail(),
    )
    old = _add_old_face(connection, media_id, color=(10, 200, 60))

    assert db.count_faces_to_reembed(connection, embedding.ACTIVE.version) == 1
    summary = reembed.reembed_faces(connection)

    assert summary["written"] == 1
    assert _raw(connection, fresh)["embed_version"] == embedding.ACTIVE.version
    assert _raw(connection, old)["embed_version"] == embedding.ACTIVE.version


def test_running_twice_does_nothing_the_second_time(connection):
    """**途中で止めても続きから再開できる**ことの裏返し。"""
    media_id = _add_media(connection)
    _add_old_face(connection, media_id)

    assert reembed.reembed_faces(connection)["written"] == 1
    second = reembed.reembed_faces(connection)
    assert second["target"] == 0
    assert second["written"] == 0


def test_a_limit_stops_early_and_the_rest_is_picked_up_next_time(connection):
    """**止めた時点までが残る。** 塊ごとに確定しているので取りこぼさない。"""
    media_id = _add_media(connection)
    for index in range(5):
        _add_old_face(connection, media_id, color=(10 * index + 20, 120, 90))

    first = reembed.reembed_faces(connection, limit=2, chunk_size=1)
    assert first["written"] == 2

    rest = reembed.reembed_faces(connection, chunk_size=1)
    assert rest["target"] == 3
    assert rest["written"] == 3
    assert db.count_faces_to_reembed(connection, embedding.ACTIVE.version) == 0


def test_thumbnails_that_are_too_small_are_skipped_and_counted(connection):
    """**小さすぎる顔を引き伸ばさない。** 中身の無い特徴量は誤った紐づけの種。

    実データでは 443 件（0.76%）が該当する。版が古いまま残るので、照合の
    対象からも外れる。
    """
    media_id = _add_media(connection)
    tiny = _add_old_face(connection, media_id, size=8)
    normal = _add_old_face(connection, media_id, color=(10, 200, 60))

    summary = reembed.reembed_faces(connection)

    assert summary["written"] == 1
    assert summary["too_small"] == 1
    # 版が古いまま → 照合の対象外
    assert _raw(connection, tiny)["embed_version"] == OLD_VERSION
    assert _raw(connection, normal)["embed_version"] == embedding.ACTIVE.version


def test_faces_without_a_thumbnail_are_not_counted_as_targets(connection):
    """材料が無いものを分母に入れない（100% に届かないまま終わる）。"""
    media_id = _add_media(connection)
    db.add_face(
        connection,
        media_id=media_id,
        bbox=(0, 120, 120, 0),
        embedding=None,
        embed_version=OLD_VERSION,
        thumbnail=None,
    )
    assert db.count_faces_to_reembed(connection, embedding.ACTIVE.version) == 0


def test_the_iterator_does_not_skip_rows_while_they_are_being_rewritten(connection):
    """**`OFFSET` で送ると、書き換えるたびに対象が縮んで顔を飛ばす。**

    `id` を進めるキーセット法になっていることを、塊を小さくして確かめる。
    """
    media_id = _add_media(connection)
    expected = [
        _add_old_face(connection, media_id, color=(20 + 10 * index, 120, 90))
        for index in range(7)
    ]

    summary = reembed.reembed_faces(connection, chunk_size=2)

    assert summary["written"] == 7
    for face_id in expected:
        assert _raw(connection, face_id)["embed_version"] == embedding.ACTIVE.version


# ---------------------------------------------------------------------------
# dry-run と報告
# ---------------------------------------------------------------------------


def test_a_dry_run_writes_nothing(connection):
    media_id = _add_media(connection)
    face_id = _add_old_face(connection, media_id)

    summary = reembed.reembed_faces(connection, dry_run=True)

    assert summary["target"] == 1
    assert summary["written"] == 0
    assert _raw(connection, face_id)["embed_version"] == OLD_VERSION
    assert "見積り" in reembed.format_summary(summary)
    assert "元写真は読みません" in reembed.format_summary(summary)


def test_the_summary_says_what_was_left_behind(connection):
    media_id = _add_media(connection)
    _add_old_face(connection, media_id, size=8)
    _add_old_face(connection, media_id, color=(10, 200, 60))

    text = reembed.format_summary(reembed.reembed_faces(connection))

    assert "作り直した顔: 1" in text
    assert "照合の対象外" in text
    assert "もう一度実行すれば続きから" in text


def test_progress_reaches_the_end(connection):
    """**分母に届くこと。** 届かないと、終わったのかどうか画面から分からない。"""
    media_id = _add_media(connection)
    for index in range(3):
        _add_old_face(connection, media_id, color=(20 + 10 * index, 120, 90))
    seen = []

    reembed.reembed_faces(
        connection, progress_callback=lambda current, total, detail: seen.append((current, total))
    )

    assert seen[-1] == (3, 3)


# ---------------------------------------------------------------------------
# 作り直したあとに照合が成り立つこと
# ---------------------------------------------------------------------------


def test_match_only_sees_the_current_version(connection):
    """**版の違う特徴量を混ぜて距離を取らない。**

    混ざると `np.vstack` が落ちるか、次元が同じモデル同士なら**黙って
    無意味な距離**が出る。
    """
    person_id = db.add_person(connection, "なつ")
    media_id = _add_media(connection)
    old_manual = _add_old_face(connection, media_id)
    db.assign_faces(connection, [old_manual], person_id, age=3)

    # 作り直す前は、手本が1件も見えない（版が違う）
    teachers, _ = db.load_manual_embeddings(connection)
    assert teachers.shape == (0, db.EMBEDDING_DIM)

    reembed.reembed_faces(connection)

    teachers, person_ids = db.load_manual_embeddings(connection)
    assert teachers.shape == (1, db.EMBEDDING_DIM)
    assert list(person_ids) == [person_id]


def test_a_face_keeps_matching_itself_after_the_rebuild(connection):
    """作り直した手本で、同じ見た目の顔が同じ人物に紐づくこと。"""
    from photoarchive_ai.matcher import match_faces

    person_id = db.add_person(connection, "なつ")
    teacher_media = _add_media(connection, "/photos/teacher.jpg")
    target_media = _add_media(connection, "/photos/target.jpg")
    teacher = _add_old_face(connection, teacher_media, color=(200, 120, 90))
    target = _add_old_face(connection, target_media, color=(200, 120, 90))
    db.assign_faces(connection, [teacher], person_id, age=3)

    reembed.reembed_faces(connection)
    summary = match_faces(connection)

    assert summary["teachers"] == 1
    assert db.get_face(connection, target)["person_id"] == person_id
