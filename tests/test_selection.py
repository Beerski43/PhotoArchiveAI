import json
from pathlib import Path

import pytest

from photoarchive_ai import db, scoring
from photoarchive_ai.appearance import Appearance
from photoarchive_ai.matcher import MATCH_RULE
from photoarchive_ai.selection import (
    _build_duplicate_groups,
    _get_media_year,
    copy_selected_media,
    load_rule,
    select_media,
    stale_assignment_notice,
)
from photoarchive_ai.db import connect, create_tables, save_media, save_media_scores


@pytest.fixture
def connection(tmp_path: Path):
    conn = connect(str(tmp_path / "selection.db"))
    create_tables(conn)
    yield conn
    conn.close()


def _add(connection, path, *, type="image", file_hash=None, shooting_date=None,
         created_time="2020-01-01T00:00:00", quality=None, smile=None, faces=()):
    """メディアを1件足す。``faces`` は写っている顔（`_face` で作る）。"""
    media_id = save_media(connection, {
        "path": path,
        "filename": Path(path).name,
        "type": type,
        "file_hash": file_hash or path,
        "file_size": 100,
        "created_time": created_time,
        "shooting_date": shooting_date,
    })
    if quality is not None or smile is not None:
        save_media_scores(connection, media_id, smile, quality)
    for spec in faces:
        _add_face(connection, media_id, **spec)
    connection.commit()
    return media_id


def _face(person=None, *, sharpness=200.0, yaw=0.0, aligned=True, smile=100.0,
          source=db.ASSIGN_MANUAL, rule=None):
    """顔の仕様。``person`` が None なら他人（未割当）。既定は「くっきり・正面・笑顔」。"""
    return dict(person=person, sharpness=sharpness, yaw=yaw, aligned=aligned, smile=smile,
                source=source, rule=rule)


def _person(connection, name):
    row = connection.execute("SELECT id FROM Person WHERE name = ?", (name,)).fetchone()
    return row[0] if row else db.add_person(connection, name)


def _add_face(connection, media_id, *, person, sharpness, yaw, aligned, smile, source, rule):
    person_id = _person(connection, person) if person else None
    face_id = db.add_face(
        connection,
        media_id=media_id,
        bbox=(0, 10, 10, 0),
        embedding=[0.0] * db.EMBEDDING_DIM,
        embed_version=db.embedding_model.ACTIVE.version,
        smile_score=smile,
        person_id=person_id,
        assign_source=source if person_id else None,
    )
    db.save_appearance(connection, [(face_id, Appearance(aligned, yaw if aligned else None, sharpness))])
    if rule is not None:
        connection.execute("UPDATE Face SET assign_rule = ? WHERE id = ?", (rule, face_id))
    return face_id


def _paths(selected):
    return [media["path"] for media in selected]


def test_select_media_filters_by_rule(tmp_path: Path):
    db_path = tmp_path / "select_test.db"
    connection = connect(str(db_path))
    create_tables(connection)

    media_record_photo = {
        "path": "2025/photo.jpg",
        "filename": "photo.jpg",
        "type": "image",
        "file_hash": "hash1",
        "file_size": 100,
        "created_time": "2025-01-01T12:00:00",
        "shooting_date": "2025-01-02",
    }
    media_record_video = {
        "path": "2025/video.mp4",
        "filename": "video.mp4",
        "type": "video",
        "file_hash": "hash2",
        "file_size": 200,
        "created_time": "2025-01-03T12:00:00",
        "shooting_date": "2025-01-03",
    }

    save_media(connection, media_record_photo)
    save_media(connection, media_record_video)

    rule = {"include_video": False, "date": {"start": "2025-01-01", "end": "2025-12-31"}}
    selected = select_media(connection, rule)

    assert len(selected) == 1
    assert selected[0]["type"] == "image"
    assert selected[0]["path"] == media_record_photo["path"]

    output_dir = tmp_path / "output"
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "2025").mkdir()
    source_file = source_root / media_record_photo["path"]
    source_file.write_text("dummy")

    copied = copy_selected_media(selected, str(output_dir), str(source_root))
    assert copied == 1
    expected_output_file = output_dir / "2025" / source_file.name
    assert expected_output_file.exists()


def test_load_rule_reads_json(tmp_path: Path):
    rule_path = tmp_path / "rule.json"
    rule_data = {"family_only": True}
    rule_path.write_text(json.dumps(rule_data), encoding="utf-8")

    loaded = load_rule(str(rule_path))
    assert loaded == rule_data


def test_load_rule_reads_yaml(tmp_path: Path):
    rule_path = tmp_path / "rule.yml"
    rule_path.write_text("family_only: true\ncount_per_year: 3\n", encoding="utf-8")

    assert load_rule(str(rule_path)) == {"family_only": True, "count_per_year": 3}


def test_load_rule_raises_for_a_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_rule(str(tmp_path / "absent.json"))


def test_family_only_keeps_media_where_a_family_member_is_assigned(connection):
    _add(connection, "family.jpg", faces=[_face("${PERSON_4}")])
    _add(connection, "stranger.jpg", faces=[_face(None)])
    _add(connection, "nobody.jpg")

    assert _paths(select_media(connection, {"family_only": True})) == ["family.jpg"]


def test_a_blurred_family_photo_comes_after_a_crisp_one(connection):
    """**利用者の要望の核心。** 本人が写っていても、ボケていたら意味がない。"""
    _add(connection, "blurred.jpg", faces=[_face("${PERSON_4}", sharpness=20.0)])
    _add(connection, "crisp.jpg", faces=[_face("${PERSON_4}", sharpness=300.0)])

    assert _paths(select_media(connection, {})) == ["crisp.jpg", "blurred.jpg"]


def test_a_profile_comes_after_a_frontal_face(connection):
    _add(connection, "profile.jpg", faces=[_face("${PERSON_4}", yaw=0.7)])
    _add(connection, "frontal.jpg", faces=[_face("${PERSON_4}", yaw=0.0)])
    _add(connection, "unaligned.jpg", faces=[_face("${PERSON_4}", aligned=False)])

    selected = _paths(select_media(connection, {}))
    # 横顔と整列できない顔は、どちらも正面らしさ 0（同点）
    assert selected[0] == "frontal.jpg"
    assert set(selected[1:]) == {"profile.jpg", "unaligned.jpg"}


def test_a_smile_ranks_above_a_straight_face(connection):
    _add(connection, "straight.jpg", faces=[_face("${PERSON_4}", smile=0.0)])
    _add(connection, "smile.jpg", faces=[_face("${PERSON_4}", smile=100.0)])

    assert _paths(select_media(connection, {})) == ["smile.jpg", "straight.jpg"]


def test_a_crisp_stranger_does_not_lift_a_blurred_family_photo(connection):
    """**写真の点は家族の顔だけから作る。** 以前は「写っている顔の最良値」で、
    隣の他人がくっきり笑っていれば、ボケた家族の写真が上位に来た。"""
    _add(connection, "blurred-with-stranger.jpg", quality=99.0, smile=100.0,
         faces=[_face("${PERSON_4}", sharpness=20.0), _face(None, sharpness=500.0)])
    _add(connection, "crisp-family.jpg", quality=10.0, smile=0.0,
         faces=[_face("${PERSON_4}", sharpness=300.0)])

    assert _paths(select_media(connection, {}))[0] == "crisp-family.jpg"


def test_more_clear_family_members_rank_higher_but_blurred_ones_do_not_count(connection):
    _add(connection, "one.jpg", faces=[_face("${PERSON_4}")])
    _add(connection, "two.jpg", faces=[_face("${PERSON_4}"), _face("${PERSON_3}")])
    _add(connection, "one-and-a-blur.jpg", faces=[_face("${PERSON_4}"), _face("${PERSON_3}", sharpness=20.0)])

    selected = _paths(select_media(connection, {}))
    assert selected[0] == "two.jpg"
    assert set(selected[1:]) == {"one.jpg", "one-and-a-blur.jpg"}
    assert scoring.family_photo_score(
        db.family_faces(connection, [selected_id(connection, "one-and-a-blur.jpg")])
    ) == scoring.family_photo_score(db.family_faces(connection, [selected_id(connection, "one.jpg")]))


def selected_id(connection, path):
    return connection.execute("SELECT id FROM Media WHERE path = ?", (path,)).fetchone()[0]


def test_a_change_made_in_the_gui_reaches_select_without_running_match(connection):
    """**保存済みの `family_score` を読まない。** GUI は `AnalysisResult` を更新しないので、
    読むと人が直した結果が次の `match` まで `select` に届かない（仕様書 §10.6）。"""
    _add(connection, "a.jpg", faces=[_face("${PERSON_4}")])
    db.recompute_family_scores(connection)
    face_id = connection.execute("SELECT id FROM Face").fetchone()[0]

    db.unassign_faces(connection, [face_id])

    assert _paths(select_media(connection, {"family_only": True})) == []


def test_match_writes_the_same_score_that_select_uses(connection):
    """`family_score`（match が書く）と select の点は同じ式から出る。"""
    _add(connection, "a.jpg", faces=[_face("${PERSON_4}", sharpness=80.0, yaw=0.3, smile=40.0)])
    db.recompute_family_scores(connection)
    stored = connection.execute("SELECT family_score FROM AnalysisResult").fetchone()[0]

    assert select_media(connection, {})[0]["family_score"] == pytest.approx(stored)


def test_select_measures_family_faces_that_were_never_measured(connection):
    media_id = _add(connection, "a.jpg")
    person_id = db.add_person(connection, "${PERSON_4}")
    face_id = db.add_face(
        connection, media_id=media_id, bbox=(0, 10, 10, 0),
        embedding=[0.0] * db.EMBEDDING_DIM, embed_version=db.embedding_model.ACTIVE.version,
        person_id=person_id, assign_source=db.ASSIGN_MANUAL, thumbnail=_crisp_jpeg(),
    )
    connection.commit()

    select_media(connection, {})

    assert db.get_face(connection, face_id)["sharpness"] is not None


def _crisp_jpeg() -> bytes:
    import io

    import numpy as np
    from PIL import Image

    yy, xx = np.mgrid[0:120, 0:120]
    board = ((((yy // 6) + (xx // 6)) % 2) * 255).astype(np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(np.stack([board] * 3, axis=-1)).save(buffer, format="JPEG")
    return buffer.getvalue()


def test_select_warns_when_auto_assignments_came_from_an_older_rule(connection):
    """**`match` と `select` の判定をずらさない。** 規則を変えたあと `match` を
    流し直していなければ、そう知らせる。"""
    _add(connection, "a.jpg", faces=[_face("${PERSON_4}", source=db.ASSIGN_AUTO, rule=None)])
    assert "1 件" in stale_assignment_notice(connection)

    connection.execute("UPDATE Face SET assign_rule = ?", (MATCH_RULE,))
    assert stale_assignment_notice(connection) is None


def test_media_without_family_are_ordered_by_quality_then_smile(connection):
    _add(connection, "low.jpg", quality=10.0, smile=90.0)
    _add(connection, "high.jpg", quality=90.0, smile=10.0)
    _add(connection, "family.jpg", quality=1.0, faces=[_face("${PERSON_4}")])

    assert _paths(select_media(connection, {})) == ["family.jpg", "high.jpg", "low.jpg"]


def test_count_per_year_limits_each_year_independently(connection):
    for index in range(3):
        _add(connection, f"2019/{index}.jpg", shooting_date=f"2019-01-0{index + 1}",
             quality=float(index))
    for index in range(2):
        _add(connection, f"2021/{index}.jpg", shooting_date=f"2021-01-0{index + 1}",
             quality=float(index))

    selected = select_media(connection, {"count_per_year": 2})
    years = sorted(media["path"][:4] for media in selected)

    assert years == ["2019", "2019", "2021", "2021"]


def test_count_per_year_keeps_the_best_of_each_year(connection):
    _add(connection, "2019/poor.jpg", shooting_date="2019-05-01", quality=5.0)
    _add(connection, "2019/best.jpg", shooting_date="2019-06-01", quality=95.0)

    selected = select_media(connection, {"count_per_year": 1})

    assert [media["path"] for media in selected] == ["2019/best.jpg"]


def test_remove_duplicate_keeps_one_row_per_file_hash(connection):
    _add(connection, "burst/1.jpg", file_hash="same", quality=10.0)
    _add(connection, "burst/2.jpg", file_hash="same", quality=90.0)
    _add(connection, "other.jpg", file_hash="different", quality=50.0)

    selected = select_media(connection, {"remove_duplicate": True})

    assert sorted(media["path"] for media in selected) == ["burst/2.jpg", "other.jpg"]


def test_duplicate_groups_fall_back_to_the_path_when_there_is_no_hash():
    grouped = _build_duplicate_groups([
        {"file_hash": "h", "path": "a.jpg"},
        {"file_hash": "h", "path": "b.jpg"},
        {"file_hash": None, "path": "c.jpg"},
    ])

    assert sorted(grouped) == ["c.jpg", "h"]
    assert len(grouped["h"]) == 2


def test_media_year_prefers_the_shooting_date_and_falls_back_to_created_time():
    assert _get_media_year({"shooting_date": "2018-04-05", "created_time": "2021-01-01"}) == 2018
    assert _get_media_year({"shooting_date": None, "created_time": "2021-01-01T10:00:00"}) == 2021
    assert _get_media_year({"shooting_date": None, "created_time": None}) is None
    assert _get_media_year({"shooting_date": "not a date", "created_time": None}) is None


def test_date_filter_falls_back_to_created_time_and_drops_unreadable_dates(connection):
    """Media.created_time は NOT NULL なので、日付が完全に無い行は作れない。

    撮影日が無いときは作成日時で判定し、読めない日付は範囲外として落とす。
    """
    _add(connection, "dated.jpg", shooting_date="2020-06-01")
    _add(connection, "by-created.jpg", shooting_date=None, created_time="2020-07-01T09:00:00")
    _add(connection, "out-of-range.jpg", shooting_date=None, created_time="2031-01-01T09:00:00")
    _add(connection, "unreadable.jpg", shooting_date="令和2年", created_time="2020-08-01T09:00:00")

    selected = select_media(connection, {"date": {"start": "2020-01-01", "end": "2020-12-31"}})

    assert sorted(media["path"] for media in selected) == ["by-created.jpg", "dated.jpg"]


def test_copy_keeps_the_layout_below_the_source_root(tmp_path: Path):
    source_root = tmp_path / "src"
    (source_root / "2019" / "trip").mkdir(parents=True)
    (source_root / "2019" / "trip" / "photo.jpg").write_text("a", encoding="utf-8")
    output = tmp_path / "out"

    copied = copy_selected_media(
        [{"path": "2019/trip/photo.jpg"}], str(output), str(source_root)
    )

    assert copied == 1
    assert (output / "2019" / "trip" / "photo.jpg").exists()


def test_copy_flattens_media_that_lives_outside_the_source_root(tmp_path: Path):
    source_root = tmp_path / "src"
    source_root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "stray.jpg").write_text("a", encoding="utf-8")
    output = tmp_path / "out"

    copied = copy_selected_media(
        [{"path": str(outside / "stray.jpg")}], str(output), str(source_root)
    )

    assert copied == 1
    assert (output / "stray.jpg").exists()


def test_copy_skips_entries_without_a_path_and_reports_progress(tmp_path: Path):
    source_root = tmp_path / "src"
    source_root.mkdir()
    (source_root / "photo.jpg").write_text("a", encoding="utf-8")
    seen = []

    copied = copy_selected_media(
        [{"path": None}, {"path": "photo.jpg"}],
        str(tmp_path / "out"),
        str(source_root),
        progress_callback=lambda *args: seen.append(args),
    )

    assert copied == 1
    assert seen == [(2, 2, "photo.jpg")]


def test_copy_makes_room_when_the_name_is_taken(tmp_path: Path):
    source_root = tmp_path / "src"
    (source_root / "a").mkdir(parents=True)
    (source_root / "b").mkdir()
    (source_root / "a" / "photo.jpg").write_text("a", encoding="utf-8")
    (source_root / "b" / "photo.jpg").write_text("b", encoding="utf-8")
    output = tmp_path / "out"

    copied = copy_selected_media(
        [{"path": str(source_root / "a" / "photo.jpg")},
         {"path": str(source_root / "b" / "photo.jpg")}],
        str(output),
        str(tmp_path / "unrelated"),
    )

    assert copied == 2
    assert (output / "photo.jpg").read_text(encoding="utf-8") == "a"
    assert (output / "photo_1.jpg").read_text(encoding="utf-8") == "b"
