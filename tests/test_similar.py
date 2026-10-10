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


def test_shots_in_the_same_folder_a_few_seconds_apart_form_a_run():
    media = [
        _media(1, "/p/a/1.jpg", "2019-05-03T10:00:00"),
        _media(2, "/p/a/2.jpg", "2019-05-03T10:00:08"),
        # 隣どうしが近ければ連なる（最初の1枚からは 16 秒）
        _media(3, "/p/a/3.jpg", "2019-05-03T10:00:16"),
        _media(4, "/p/a/4.jpg", "2019-05-03T10:05:00"),
    ]
    assert _ids(similar.candidate_runs(media, {}, seconds=10)) == [[1, 2, 3]]


def test_another_folder_or_other_family_members_break_the_run():
    media = [
        _media(1, "/p/a/1.jpg", "2019-05-03T10:00:00"),
        _media(2, "/p/b/2.jpg", "2019-05-03T10:00:01"),
        _media(3, "/p/a/3.jpg", "2019-05-03T10:00:02"),
        _media(4, "/p/a/4.jpg", "2019-05-03T10:00:03"),
    ]
    family = {1: frozenset({7}), 3: frozenset({7}), 4: frozenset({7, 8})}
    # 1 と 3 は同じフォルダで2秒差・同じ家族。4 は写っている家族が違う
    assert _ids(similar.candidate_runs(media, family, seconds=10)) == [[1, 3]]


def test_photos_without_a_time_and_videos_are_never_bundled():
    media = [
        _media(1, "/p/a/1.jpg", "2019-05-03"),
        _media(2, "/p/a/2.jpg", "2019-05-03"),
        _media(3, "/p/a/3.mp4", "2019-05-03T10:00:00", type="video"),
        _media(4, "/p/a/4.mp4", "2019-05-03T10:00:01", type="video"),
        _media(5, "/p/a/5.jpg", None),
    ]
    assert similar.candidate_runs(media, {}) == []


def test_scenes_split_a_run_by_looks():
    run = [_media(i, f"/p/a/{i}.jpg", "2019-05-03T10:00:00") for i in (1, 2, 3, 4)]
    looks = {
        1: similar.encode(similar.FROM_EXIF, 0),
        2: similar.encode(similar.FROM_EXIF, 0b111),  # 1 と距離3
        3: similar.encode(similar.FROM_EXIF, (1 << 64) - 1),  # 誰とも遠い
        4: None,  # 測れなかった → 束ねない
    }
    assert sorted(_ids(similar.scenes(run, looks, max_distance=5))) == [[1, 2], [3], [4]]


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
