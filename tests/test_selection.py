import json
from pathlib import Path

from photoarchive_ai.selection import load_rule, select_media, copy_selected_media
from photoarchive_ai.db import connect, create_tables, save_media


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
        "analyzed_date": None,
        "analyzer_version": None,
    }
    media_record_video = {
        "path": "2025/video.mp4",
        "filename": "video.mp4",
        "type": "video",
        "file_hash": "hash2",
        "file_size": 200,
        "created_time": "2025-01-03T12:00:00",
        "shooting_date": "2025-01-03",
        "analyzed_date": None,
        "analyzer_version": None,
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
