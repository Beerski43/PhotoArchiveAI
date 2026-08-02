import sqlite3
from pathlib import Path

from photoarchive_ai.db import connect, create_tables, save_media, list_media, get_media_by_path


def test_database_schema_and_media_crud(tmp_path: Path):
    db_path = tmp_path / "test_photoarchive.db"
    connection = connect(str(db_path))
    create_tables(connection)

    media_record = {
        "path": "2025/01/test.jpg",
        "filename": "test.jpg",
        "type": "photo",
        "file_hash": "dummyhash",
        "file_size": 12345,
        "created_time": "2025-01-01T00:00:00",
        "shooting_date": "2025-01-01",
        "analyzed_date": None,
        "analyzer_version": None,
    }

    media_id = save_media(connection, media_record)
    assert isinstance(media_id, int)

    media_list = list_media(connection)
    assert len(media_list) == 1
    assert media_list[0]["path"] == media_record["path"]

    found = get_media_by_path(connection, media_record["path"])
    assert found is not None
    assert found["filename"] == media_record["filename"]
    assert found["type"] == media_record["type"]

    connection.close()
