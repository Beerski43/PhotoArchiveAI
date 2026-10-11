"""連写・似た写真を束ねる（#86・`similar.py`）。"""

import pytest

from photoarchive_ai import similar
from photoarchive_ai.dates import taken_moment
from tests.helpers import burst_of, exif_with_thumbnail, scene, write_photo


# ---------------------------------------------------------------------------
# 見た目の値
# ---------------------------------------------------------------------------


def test_a_burst_shot_is_close_and_another_scene_is_far(tmp_path):
    first = scene(1)
    a = similar.measure(write_photo(tmp_path / "a.jpg", first))
    b = similar.measure(write_photo(tmp_path / "b.jpg", burst_of(first, 2)))
    c = similar.measure(write_photo(tmp_path / "c.jpg", scene(3)))
    assert similar.distance(a, b) <= similar.DEFAULT_DISTANCE
    assert similar.distance(a, c) > similar.DEFAULT_DISTANCE


def test_the_exif_thumbnail_is_used_without_decoding_the_photo(tmp_path, monkeypatch):
    """**元写真を丸ごと読まない**（NFS）。サムネイルがあれば、先頭だけで足りる。"""
    image = scene(1, size=(1600, 1200))
    thumbnail = image.resize((160, 120))
    path = write_photo(tmp_path / "a.jpg", image, thumbnail=thumbnail)
    # EXIF の直後で切る。写真の本体が無くても測れることを確かめる
    whole = (tmp_path / "a.jpg").read_bytes()
    exif = exif_with_thumbnail(thumbnail)
    end = whole.index(exif) + len(exif)
    assert end + 64 < len(whole)
    (tmp_path / "a.jpg").write_bytes(whole[: end + 64])

    def no_full_decode(*args, **kwargs):
        raise AssertionError("元写真をデコードした")

    monkeypatch.setattr(similar.Image.Image, "draft", no_full_decode)
    value = similar.measure(path)
    assert similar.decode(value)[0] == similar.FROM_EXIF


def test_without_a_thumbnail_the_photo_itself_is_measured(tmp_path):
    value = similar.measure(write_photo(tmp_path / "a.jpg", scene(1)))
    assert similar.decode(value)[0] == similar.FROM_IMAGE


def test_values_from_the_thumbnail_and_the_photo_are_not_compared(tmp_path):
    """黒帯の有無で距離がずれるので、取った場所が違えば比べない（束ねない）。"""
    image = scene(1)
    with_thumbnail = similar.measure(
        write_photo(tmp_path / "a.jpg", image, thumbnail=image.resize((160, 120)))
    )
    without = similar.measure(write_photo(tmp_path / "b.jpg", image))
    assert similar.distance(with_thumbnail, without) is None


def test_an_unreadable_file_gives_no_value(tmp_path):
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not a jpeg")
    assert similar.measure(str(broken)) is None
    assert similar.measure(str(tmp_path / "missing.jpg")) is None


@pytest.mark.parametrize("text", [None, "", "dhash0/exif:00ff", "dhash8/other:00ff", "dhash8/exif:zz"])
def test_values_of_another_version_or_broken_values_are_measured_again(text):
    assert similar.decode(text) is None


def test_a_value_round_trips():
    assert similar.decode(similar.encode(similar.FROM_EXIF, 0xABC)) == (similar.FROM_EXIF, 0xABC)


# ---------------------------------------------------------------------------
# 撮影日時を秒まで
# ---------------------------------------------------------------------------


def test_the_moment_needs_a_readable_date_with_a_time():
    assert taken_moment("2019-05-03T10:20:30").second == 30
    assert taken_moment("2019-05-03") is None
    assert taken_moment("0000-00-00T00:00:00") is None
    assert taken_moment("TTTT-TT-TTTTT:TT:TT") is None
    assert taken_moment(None) is None


# ---------------------------------------------------------------------------
# 候補の連なりと場面
# ---------------------------------------------------------------------------


def _media(media_id, path, when, type="image"):
    return {"id": media_id, "path": path, "type": type, "shooting_date": when}


def _ids(runs):
    return [[media["id"] for media in run] for run in runs]


def test_photos_of_the_same_event_are_candidates_however_far_apart_in_time():
    """**同じ行事（フォルダ×日）の中なら、時間は問わない**（#93・利用者の決定）。

    #86 は隣と 10 秒以内しか候補にせず、時間の離れた似た写真が両方選ばれていた。
    """
    media = [
        _media(1, "/p/a/1.jpg", "2019-05-03T10:00:00"),
        _media(2, "/p/a/2.jpg", "2019-05-03T10:00:08"),
        _media(3, "/p/a/3.jpg", "2019-05-03T15:30:00"),
        # 別の日は別の行事
        _media(4, "/p/a/4.jpg", "2019-05-04T10:00:00"),
    ]
    assert _ids(similar.candidate_groups(media, {}, seconds=10)) == [[1, 2, 3]]


def test_another_folder_or_other_family_members_are_not_candidates():
    media = [
        _media(1, "/p/a/1.jpg", "2019-05-03T10:00:00"),
        _media(2, "/p/b/2.jpg", "2019-05-03T10:00:01"),
        _media(3, "/p/a/3.jpg", "2019-05-03T12:00:00"),
        _media(4, "/p/a/4.jpg", "2019-05-03T10:00:03"),
    ]
    family = {1: frozenset({7}), 2: frozenset({7}), 3: frozenset({7}), 4: frozenset({7, 8})}
    # 1 と 3 は同じフォルダ・同じ日・同じ家族。2 はフォルダが、4 は写っている家族が違う
    assert _ids(similar.candidate_groups(media, family, seconds=10)) == [[1, 3]]


def test_another_family_in_between_does_not_split_the_candidates():
    """#86 は家族の違う写真が1枚挟まるだけで連なりが切れていた。"""
    media = [
        _media(1, "/p/a/1.jpg", "2019-05-03T10:00:00"),
        _media(2, "/p/a/2.jpg", "2019-05-03T10:00:01"),
        _media(3, "/p/a/3.jpg", "2019-05-03T10:00:02"),
    ]
    family = {1: frozenset({7}), 2: frozenset({8}), 3: frozenset({7})}
    assert _ids(similar.candidate_groups(media, family, seconds=10)) == [[1, 3]]


def test_a_burst_across_midnight_is_still_one_candidate():
    """**日付をまたぐ連写は、撮影日時の近さでつなぐ**（#86 の束を新しい候補から落とさない）。"""
    media = [
        _media(1, "/p/a/1.jpg", "2019-12-31T23:59:58"),
        _media(2, "/p/a/2.jpg", "2020-01-01T00:00:03"),
        _media(3, "/p/a/3.jpg", "2020-01-01T00:30:00"),
        _media(4, "/p/a/4.jpg", "2019-12-31T20:00:00"),
    ]
    assert _ids(similar.candidate_groups(media, {}, seconds=10)) == [[4, 1, 2, 3]]
    # 間隔を 0 にすれば、日付で切れる
    assert _ids(similar.candidate_groups(media, {}, seconds=0)) == [[4, 1], [2, 3]]


def test_photos_without_a_time_and_videos_are_never_bundled():
    media = [
        _media(1, "/p/a/1.jpg", "2019-05-03"),
        _media(2, "/p/a/2.jpg", "2019-05-03"),
        _media(3, "/p/a/3.mp4", "2019-05-03T10:00:00", type="video"),
        _media(4, "/p/a/4.mp4", "2019-05-03T10:00:01", type="video"),
        _media(5, "/p/a/5.jpg", None),
    ]
    assert similar.candidate_groups(media, {}) == []


def _look(bits):
    return similar.encode(similar.FROM_EXIF, bits)


def _ones(count):
    """下位 ``count`` ビットが1の値。0 との距離が ``count`` になる。"""
    return (1 << count) - 1


def test_scenes_split_a_group_by_looks():
    group = [_media(i, f"/p/a/{i}.jpg", "2019-05-03T10:00:00") for i in (1, 2, 3, 4)]
    looks = {
        1: _look(0),
        2: _look(0b111),  # 1 と距離3
        3: _look((1 << 64) - 1),  # 誰とも遠い
        4: None,  # 測れなかった → 束ねない
    }
    assert _ids(similar.scenes(group, looks, max_distance=5)) == [[1, 2], [3], [4]]


def test_a_photo_close_only_to_a_dropped_photo_is_kept():
    """**数珠つなぎにしない**（#93）。残した写真と遠ければ、外した写真と近くても残す。

    行事まるごとを連結で束ねると、実データで挙式と披露宴の写真まで1つの場面になった。
    """
    group = [_media(i, f"/p/a/{i}.jpg", "2019-05-03T10:00:00") for i in (1, 2, 3)]
    # 1〜2 は距離4、2〜3 は距離4、1〜3 は距離8
    looks = {1: _look(0), 2: _look(_ones(4)), 3: _look(_ones(8))}
    assert _ids(similar.scenes(group, looks, max_distance=5)) == [[1, 2], [3]]


def test_photos_far_apart_in_time_need_to_look_closer():
    """**時間の離れた2枚は `FAR_DISTANCE`（14）で見る**。連写は `max_distance`（16）のまま。

    実データで、時間の離れた 15〜16 の組に別の場面が混ざった（利用者の決定・#93）。
    """
    assert similar.FAR_DISTANCE == 14
    looks = {1: _look(0), 2: _look(_ones(15)), 3: _look(_ones(14))}
    burst = [
        _media(1, "/p/a/1.jpg", "2019-05-03T10:00:00"),
        _media(2, "/p/a/2.jpg", "2019-05-03T10:00:05"),
    ]
    assert _ids(similar.scenes(burst, looks, max_distance=16, seconds=10)) == [[1, 2]]
    apart = [
        _media(1, "/p/a/1.jpg", "2019-05-03T10:00:00"),
        _media(2, "/p/a/2.jpg", "2019-05-03T15:00:00"),
        _media(3, "/p/a/3.jpg", "2019-05-03T16:00:00"),
    ]
    assert _ids(similar.scenes(apart, looks, max_distance=16, seconds=10)) == [[1, 3], [2]]
    # ルールの距離のほうが小さければ、そちらに従う
    # （3 は 2 と距離1なので、2 の場面に入る）
    assert _ids(similar.scenes(apart, looks, max_distance=10, seconds=10)) == [[1], [2, 3]]


def test_a_burst_member_far_from_the_representative_needs_to_look_closer():
    """**「時間の離れた2枚」は代表との差で決まる**（隣との差ではない。PR #94 のレビュー指摘1）。

    8 秒おきの連写でも、代表から 16 秒後の3枚目は距離 14 で見る。定義を変えたら気づけるように。
    """
    burst = [
        _media(1, "/p/a/1.jpg", "2019-05-03T10:00:00"),
        _media(2, "/p/a/2.jpg", "2019-05-03T10:00:08"),
        _media(3, "/p/a/3.jpg", "2019-05-03T10:00:16"),
    ]
    looks = {1: _look(0), 2: _look(_ones(8)), 3: _look(_ones(15))}
    assert _ids(similar.candidate_groups(burst, {}, seconds=10)) == [[1, 2, 3]]
    assert _ids(similar.scenes(burst, looks, max_distance=16, seconds=10)) == [[1, 2], [3]]


def test_a_burst_across_midnight_does_not_bundle_the_other_days_photos():
    """**0時をまたぐ連写がつないだ別の日の写真どうしは束ねない**（PR #94 のレビュー指摘2）。

    候補の組は連写で前後の日がつながるが、別の日は別の行事（利用者の決定）。
    """
    media = {
        1: _media(1, "/p/a/1.jpg", "2019-12-31T20:00:00"),
        2: _media(2, "/p/a/2.jpg", "2019-12-31T23:59:58"),
        3: _media(3, "/p/a/3.jpg", "2020-01-01T00:00:03"),
        4: _media(4, "/p/a/4.jpg", "2020-01-01T12:00:00"),
    }
    assert _ids(similar.candidate_groups(list(media.values()), {}, seconds=10)) == [[1, 2, 3, 4]]
    far = (1 << 64) - 1
    # 1 と 4 は距離5（別の日）、連写の 2 と 3 は同じ絵（0時をまたぐ）
    looks = {1: _look(0), 4: _look(_ones(5)), 2: _look(far), 3: _look(far)}
    ordered = [media[1], media[4], media[2], media[3]]
    assert _ids(similar.scenes(ordered, looks, max_distance=16, seconds=10)) == [[1], [4], [2, 3]]


# ---------------------------------------------------------------------------
# 移行（v7 → v8）
# ---------------------------------------------------------------------------


def test_a_version_7_database_gains_the_look_column_and_keeps_its_faces(tmp_path):
    """**列を足すだけ。顔を1件も失わない**（CLAUDE.md §4）。値は `select` が測るまで NULL。"""
    import sqlite3

    from photoarchive_ai import db
    from photoarchive_ai.migration import migrate_database, needs_migration

    database = tmp_path / "v7.db"
    connection = db.ensure_database(str(database))
    person = db.add_person(connection, "${PERSON_1}")
    media_id = db.save_media(connection, {
        "path": "/p/2019/a.jpg", "filename": "a.jpg", "type": "image", "file_hash": "h",
        "file_size": 1, "created_time": "2019-01-01T00:00:00",
    })
    for source in (db.ASSIGN_MANUAL, db.ASSIGN_AUTO, None):
        db.add_face(
            connection, media_id=media_id, bbox=(0, 10, 10, 0),
            embedding=[0.0] * db.EMBEDDING_DIM, embed_version=db.embedding_model.ACTIVE.version,
            person_id=person if source else None, assign_source=source,
        )
    connection.commit()
    connection.close()

    raw = sqlite3.connect(str(database))
    raw.execute("ALTER TABLE Media DROP COLUMN look_hash")
    raw.execute("PRAGMA user_version = 7")
    raw.commit()
    raw.close()
    assert needs_migration(str(database))

    messages = []
    migrate_database(str(database), make_backup=False, log=messages.append)

    raw = sqlite3.connect(str(database))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert raw.execute("SELECT COUNT(*) FROM Face").fetchone()[0] == 3
        assert raw.execute(
            "SELECT COUNT(*) FROM Face WHERE assign_source = 'manual'"
        ).fetchone()[0] == 1
        assert raw.execute("SELECT look_hash FROM Media").fetchone() == (None,)
    finally:
        raw.close()
    assert any("look_hash" in message for message in messages)
    assert needs_migration(str(database)) is False
