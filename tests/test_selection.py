import json
from pathlib import Path

import pytest

from photoarchive_ai.selection import (
    _build_duplicate_groups,
    _get_media_year,
    copy_selected_media,
    load_rule,
    select_media,
)
from photoarchive_ai.db import connect, create_tables, save_media, save_media_scores


@pytest.fixture
def connection(tmp_path: Path):
    conn = connect(str(tmp_path / "selection.db"))
    create_tables(conn)
    yield conn
    conn.close()


def _add(connection, path, *, type="image", file_hash=None, shooting_date=None,
         created_time="2020-01-01T00:00:00", family=None, quality=None, smile=None):
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
    if family is not None:
        connection.execute(
            "INSERT INTO AnalysisResult (media_id, family_score) VALUES (?, ?)"
            " ON CONFLICT(media_id) DO UPDATE SET family_score = excluded.family_score",
            (media_id, family),
        )
    connection.commit()
    return media_id


def test_select_media_filters_by_rule(tmp_path: Path):
    db_path = tmp_path / "select_test.db"
    connection = connect(str(db_path))
    create_tables(connection)

    media_record_photo = {
        "path": "2025/photo.jpg",
        "filename": "photo.jpg",
        "type": "photo",
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
    assert selected[0]["type"] == "photo"
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


def test_family_only_keeps_media_with_a_family_score(connection):
    _add(connection, "a.jpg", family=80.0)
    _add(connection, "b.jpg", family=0.0)
    _add(connection, "c.jpg")  # AnalysisResult の行そのものが無い

    selected = select_media(connection, {"family_only": True})

    assert [media["path"] for media in selected] == ["a.jpg"]


def test_results_are_ordered_by_family_then_quality_then_smile(connection):
    _add(connection, "low.jpg", family=10.0, quality=90.0, smile=90.0)
    _add(connection, "high.jpg", family=90.0, quality=10.0, smile=10.0)
    _add(connection, "middle.jpg", family=10.0, quality=95.0, smile=10.0)

    selected = select_media(connection, {})

    assert [media["path"] for media in selected] == ["high.jpg", "middle.jpg", "low.jpg"]


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
