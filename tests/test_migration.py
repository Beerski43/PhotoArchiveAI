import sqlite3

import pytest

from photoarchive_ai import db
from photoarchive_ai.migration import (
    backup_database,
    describe_migration,
    migrate_database,
    needs_migration,
)

LEGACY_SCHEMA = [
    "CREATE TABLE Media (id INTEGER PRIMARY KEY AUTOINCREMENT,path TEXT UNIQUE NOT NULL,"
    "filename TEXT NOT NULL,type TEXT NOT NULL,file_hash TEXT NOT NULL,file_size INTEGER NOT NULL,"
    "created_time TEXT NOT NULL,shooting_date TEXT,analyzed_date TEXT,analyzer_version TEXT)",
    "CREATE TABLE Person (id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,relation TEXT,"
    "memo TEXT, age INTEGER)",
    "CREATE TABLE FaceEmbedding (id INTEGER PRIMARY KEY AUTOINCREMENT,media_id INTEGER,"
    "person_id INTEGER,embedding TEXT NOT NULL,similarity_score REAL,face_image BLOB,"
    "added_at TEXT NOT NULL, age INTEGER,FOREIGN KEY(media_id) REFERENCES Media(id),"
    "FOREIGN KEY(person_id) REFERENCES Person(id))",
    "CREATE TABLE AnalysisResult (media_id INTEGER PRIMARY KEY,face_count INTEGER,"
    "family_score REAL,smile_score REAL,quality_score REAL,duplicate_group TEXT,"
    "event_category TEXT,FOREIGN KEY(media_id) REFERENCES Media(id))",
    "CREATE INDEX idx_media_hash ON Media(file_hash)",
    "CREATE INDEX idx_media_type ON Media(type)",
    "CREATE INDEX idx_face_person ON FaceEmbedding(person_id)",
]


def _build_legacy_database(path):
    connection = sqlite3.connect(str(path))
    for statement in LEGACY_SCHEMA:
        connection.execute(statement)
    connection.executemany(
        "INSERT INTO Media (path, filename, type, file_hash, file_size, created_time,"
        " shooting_date, analyzed_date, analyzer_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (f"/photos/{index}.jpg", f"{index}.jpg", "image", f"hash{index}", 100 + index,
             "2026-01-01T00:00:00", "2026-01-01T00:00:00", "2026-09-01T00:00:00", "1.4")
            for index in range(3)
        ],
    )
    connection.executemany(
        "INSERT INTO Person (name, relation, memo, age) VALUES (?, ?, ?, ?)",
        [("父", "father", "", 40), ("長男", "son", "", 8)],
    )
    connection.executemany(
        "INSERT INTO FaceEmbedding (media_id, person_id, embedding, similarity_score,"
        " face_image, added_at, age) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(1, 1, "[0.1, 0.2]", 50.0, b"jpeg", "2026-09-01T00:00:00", None)],
    )
    connection.execute(
        "INSERT INTO AnalysisResult (media_id, face_count, family_score, smile_score,"
        " quality_score) VALUES (1, 1, 50.0, 10.0, 20.0)"
    )
    connection.commit()
    connection.close()


def test_migrate_keeps_media_and_person_and_drops_faces(tmp_path):
    database = tmp_path / "legacy.db"
    _build_legacy_database(database)
    assert needs_migration(str(database)) is True

    result = migrate_database(str(database))

    assert result["migrated"] is True
    assert result["backup"] is not None
    assert result["after"] == {"media": 3, "persons": 2, "faces": 0, "unscanned": 3}

    connection = db.connect(str(database))
    try:
        assert db.get_schema_version(connection) == db.SCHEMA_VERSION
        media = db.list_media(connection)
        assert [row["path"] for row in media] == [f"/photos/{i}.jpg" for i in range(3)]
        # ハッシュが温存されるので、移行後の scan で再計算が不要になる
        assert [row["file_hash"] for row in media] == ["hash0", "hash1", "hash2"]
        assert all(row["face_count"] is None for row in media)
        assert all("analyzed_date" not in row for row in media)

        persons = db.list_persons(connection)
        assert {row["name"] for row in persons} == {"父", "長男"}
        assert all("age" not in row for row in persons)

        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "FaceEmbedding" not in tables
        assert "Face" in tables

        columns = {row[1] for row in connection.execute("PRAGMA table_info(AnalysisResult)")}
        assert columns == {"media_id", "family_score", "smile_score", "quality_score"}
    finally:
        connection.close()


def test_migrate_is_idempotent(tmp_path):
    database = tmp_path / "legacy.db"
    _build_legacy_database(database)
    migrate_database(str(database))

    assert needs_migration(str(database)) is False
    result = migrate_database(str(database))
    assert result["migrated"] is False

    connection = db.connect(str(database))
    try:
        assert len(db.list_media(connection)) == 3
    finally:
        connection.close()


def test_ensure_database_refuses_legacy_schema(tmp_path):
    database = tmp_path / "legacy.db"
    _build_legacy_database(database)
    with pytest.raises(db.SchemaVersionError):
        db.ensure_database(str(database))


def test_describe_migration_counts_what_will_be_dropped(tmp_path):
    """移行の前に「何が消えるか」を数えられること。

    実データは 70,000 件規模で作り直しが利かない。実行前に件数を見せられ
    ないと、利用者が移行してよいか判断できない。
    """
    database = tmp_path / "legacy.db"
    _build_legacy_database(database)

    summary = describe_migration(str(database))

    assert summary["schema_version"] == 0
    assert summary["media"] == 3
    assert summary["persons"] == 2
    # 顔と解析結果は移行で作り直す(特徴量の規約が変わったため)
    assert summary["faces_to_drop"] == 1
    assert summary["analysis_to_drop"] == 1
    assert "FaceEmbedding" in summary["tables"]


def test_describe_migration_on_a_current_database_has_nothing_to_drop(tmp_path):
    database = tmp_path / "current.db"
    connection = db.ensure_database(str(database))
    db.add_person(connection, "父")
    connection.commit()
    connection.close()

    summary = describe_migration(str(database))

    assert summary["schema_version"] == db.SCHEMA_VERSION
    assert summary["persons"] == 1
    assert summary["faces_to_drop"] == 0


def test_backup_database_writes_to_an_explicit_path(tmp_path):
    database = tmp_path / "legacy.db"
    _build_legacy_database(database)
    target = tmp_path / "backups" / "2026" / "before-migration.db"

    created = backup_database(str(database), str(target))

    # 親ディレクトリが無くても作る
    assert created == target
    assert target.is_file()
    assert target.read_bytes() == database.read_bytes()


def test_backup_database_defaults_to_a_timestamped_sibling(tmp_path):
    database = tmp_path / "legacy.db"
    _build_legacy_database(database)

    created = backup_database(str(database))

    assert created.parent == database.parent
    assert created.name.startswith("legacy.db.bak-")
    assert created.read_bytes() == database.read_bytes()


def test_migrate_can_skip_the_backup_and_the_vacuum(tmp_path):
    """VACUUM は 773MB 級のDBで時間がかかるので、外せること。"""
    database = tmp_path / "legacy.db"
    _build_legacy_database(database)

    result = migrate_database(str(database), vacuum=False, make_backup=False)

    assert result["migrated"] is True
    assert result["backup"] is None
    assert list(tmp_path.glob("legacy.db.bak-*")) == []
    # 中身は通常の移行と同じ
    assert result["after"] == {"media": 3, "persons": 2, "faces": 0, "unscanned": 3}


def test_migrate_reports_its_progress(tmp_path):
    database = tmp_path / "legacy.db"
    _build_legacy_database(database)
    messages = []

    migrate_database(str(database), log=messages.append)

    assert any("バックアップ" in message for message in messages)
    assert any("VACUUM" in message for message in messages)
    assert any("移行が完了" in message for message in messages)


def test_migrate_creates_the_schema_for_an_empty_file(tmp_path):
    database = tmp_path / "empty.db"
    sqlite3.connect(str(database)).close()

    result = migrate_database(str(database))

    assert result["migrated"] is True
    assert result["schema_version"] == db.SCHEMA_VERSION
    connection = db.connect(str(database))
    try:
        assert db.list_media(connection) == []
    finally:
        connection.close()


def test_migrate_refuses_a_database_that_does_not_exist(tmp_path):
    with pytest.raises(FileNotFoundError):
        migrate_database(str(tmp_path / "missing.db"))


def test_embedding_blob_roundtrip():
    values = [index / 128.0 for index in range(128)]
    blob = db.encode_embedding(values)
    assert len(blob) == 128 * 4
    restored = db.decode_embedding(blob)
    assert restored.shape == (128,)
    assert restored[0] == 0.0
    assert abs(float(restored[127]) - values[127]) < 1e-6
    assert db.encode_embedding(None) is None
    assert db.decode_embedding(None) is None


# ---------------------------------------------------------------------------
# v2 → v3（列を足すだけ。何も破棄しない）
# ---------------------------------------------------------------------------

V2_PERSON_DDL = (
    "CREATE TABLE Person (id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "name TEXT NOT NULL,relation TEXT,memo TEXT)"
)


def _build_v2_database(path):
    """`birth_date` が無いだけの、現行と同じスキーマ（版2）。

    現行スキーマを作ってから `Person` を版2の形に戻す。DDL を丸ごと書き写すと、
    本物のスキーマが変わったときに気づけない。
    """
    connection = db.ensure_database(str(path))
    media_id = db.save_media(
        connection,
        {
            "path": "/photos/1.jpg",
            "filename": "1.jpg",
            "type": "image",
            "file_hash": "hash1",
            "file_size": 100,
            "created_time": "2026-01-01T00:00:00",
        },
    )
    person_id = db.add_person(connection, "父", "father", "メモ")
    for index in range(3):
        db.add_face(
            connection,
            media_id=media_id,
            bbox=(0, 10, 10, 0),
            embedding=[0.0] * 128,
            embed_version="test",
            person_id=person_id if index == 0 else None,
            assign_source=db.ASSIGN_MANUAL if index == 0 else None,
        )
    db.save_media_scores(connection, media_id, smile_score=10.0, quality_score=20.0)
    connection.commit()
    connection.close()

    # Person を版2の形（birth_date なし）へ戻し、版も 2 に落とす。
    connection = sqlite3.connect(str(path))
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("ALTER TABLE Person RENAME TO Person_old")
    connection.execute(V2_PERSON_DDL)
    connection.execute(
        "INSERT INTO Person (id, name, relation, memo)"
        " SELECT id, name, relation, memo FROM Person_old"
    )
    connection.execute("DROP TABLE Person_old")
    connection.execute("PRAGMA user_version = 2")
    connection.commit()
    connection.close()


def test_a_version_2_database_keeps_every_face_when_migrated(tmp_path):
    """**v2 からの移行で顔を1件も失わないこと。**

    v1 → v2 は顔を破棄する（旧特徴量が dlib と互換でないため）。その経路へ
    v2 のDBを流し込むと、**使えるはずの顔と、数時間かけたスキャンの結果が
    消える。** 実データは顔 58,000 件規模で、作り直しは現実的でない。
    """
    database = tmp_path / "v2.db"
    _build_v2_database(database)
    connection = sqlite3.connect(str(database))
    before = {
        "faces": connection.execute("SELECT COUNT(*) FROM Face").fetchone()[0],
        "media": connection.execute("SELECT COUNT(*) FROM Media").fetchone()[0],
        "analysis": connection.execute("SELECT COUNT(*) FROM AnalysisResult").fetchone()[0],
        "manual": connection.execute(
            "SELECT COUNT(*) FROM Face WHERE assign_source = 'manual'"
        ).fetchone()[0],
        "scanned": connection.execute(
            "SELECT COUNT(*) FROM Media WHERE face_count IS NOT NULL"
        ).fetchone()[0],
    }
    connection.close()
    assert before["faces"] == 3 and before["manual"] == 1

    migrate_database(str(database), vacuum=False, make_backup=False)

    connection = sqlite3.connect(str(database))
    try:
        after = {
            "faces": connection.execute("SELECT COUNT(*) FROM Face").fetchone()[0],
            "media": connection.execute("SELECT COUNT(*) FROM Media").fetchone()[0],
            "analysis": connection.execute("SELECT COUNT(*) FROM AnalysisResult").fetchone()[0],
            "manual": connection.execute(
                "SELECT COUNT(*) FROM Face WHERE assign_source = 'manual'"
            ).fetchone()[0],
            "scanned": connection.execute(
                "SELECT COUNT(*) FROM Media WHERE face_count IS NOT NULL"
            ).fetchone()[0],
        }
        columns = {row[1] for row in connection.execute("PRAGMA table_info(Person)")}
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()

    assert after == before, "v2 からの移行で何かが失われている"
    assert "birth_date" in columns
    assert version == db.SCHEMA_VERSION


def test_a_version_2_database_keeps_the_values_already_stored(tmp_path):
    """既存の人物の値が消えず、`birth_date` だけが未設定で増えること。"""
    database = tmp_path / "v2.db"
    _build_v2_database(database)

    migrate_database(str(database), vacuum=False, make_backup=False)

    connection = db.connect(str(database))
    try:
        person = db.list_persons(connection)[0]
    finally:
        connection.close()
    assert (person["name"], person["relation"], person["memo"]) == ("父", "father", "メモ")
    assert person["birth_date"] is None


def test_describing_a_version_2_migration_does_not_threaten_to_drop_faces(tmp_path):
    """**「破棄します」と出さないこと。**

    列を足すだけなのに消えると読める案内を出すと、実行をためらって移行が
    進まない。CLI はこの値を見て文面を切り替える。
    """
    database = tmp_path / "v2.db"
    _build_v2_database(database)

    info = describe_migration(str(database))

    assert info["rebuilds"] is False
    assert info["faces_to_drop"] == 0
    assert info["analysis_to_drop"] == 0
    assert info["faces_kept"] == 3


def test_a_legacy_database_ends_up_with_the_birth_date_column(tmp_path):
    """v1 からの移行でも、最後は現行スキーマ（`birth_date` あり）になること。"""
    database = tmp_path / "legacy.db"
    _build_legacy_database(database)

    migrate_database(str(database), vacuum=False, make_backup=False)

    connection = sqlite3.connect(str(database))
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(Person)")}
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        persons = connection.execute("SELECT COUNT(*) FROM Person").fetchone()[0]
    finally:
        connection.close()

    assert "birth_date" in columns
    assert version == db.SCHEMA_VERSION
    assert persons == 2, "人物は温存される"


def test_migrating_a_version_2_database_twice_changes_nothing(tmp_path):
    database = tmp_path / "v2.db"
    _build_v2_database(database)
    migrate_database(str(database), vacuum=False, make_backup=False)

    result = migrate_database(str(database), vacuum=False, make_backup=False)

    assert result["migrated"] is False


# ---------------------------------------------------------------------------
# 版だけが進むのを防ぐ（実データで起きた）
# ---------------------------------------------------------------------------


def test_opening_an_old_database_does_not_stamp_it_as_current(tmp_path):
    """**移行していないDBに、版の印だけを付けない。**

    `SCHEMA` は `CREATE TABLE IF NOT EXISTS` なので既存のテーブルを変えない。
    それなのに最後へ `PRAGMA user_version` を書くと、**中身は版2のまま
    「版3」の印が付く。** そうなると `migrate` が「すでに最新です」と言って
    何もせず、**欠けた列は二度と足されない。**

    実データで起きた。GUI で誕生日を入れても、次に開くと空欄に戻っていた
    （保存が `no such column: birth_date` で静かに落ちていた）。
    """
    database = tmp_path / "v2.db"
    _build_v2_database(database)

    with pytest.raises(db.SchemaVersionError) as error:
        db.ensure_database(str(database))

    assert "migrate" in str(error.value)
    # **印を付けずに断ること。** 付けてしまうと、この後 migrate が効かない
    connection = sqlite3.connect(str(database))
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    connection.close()
    assert needs_migration(str(database)) is True


def test_a_database_whose_version_ran_ahead_is_still_repaired(tmp_path):
    """**版の数字ではなく、実際の形を見る。**

    すでに印だけ付いてしまったデータベース（実データがこの状態だった）を、
    `migrate` が直せること。数字しか見ないと「最新です」と答えて、
    **直す手立てが無くなる。**
    """
    database = tmp_path / "stamped.db"
    _build_v2_database(database)
    connection = sqlite3.connect(str(database))
    connection.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION}")
    connection.commit()
    connection.close()

    assert needs_migration(str(database)) is True
    # **この状態のDBを黙って使わせない。** 実データがこうなっており、GUI の
    # 保存が `no such column` で静かに落ちていた
    with pytest.raises(db.SchemaVersionError) as error:
        db.ensure_database(str(database))
    assert "Person.birth_date" in str(error.value)

    messages = []
    migrate_database(str(database), make_backup=False, log=messages.append)

    connection = sqlite3.connect(str(database))
    columns = {row[1] for row in connection.execute("PRAGMA table_info(Person)")}
    faces = connection.execute("SELECT COUNT(*) FROM Face").fetchone()[0]
    connection.close()
    assert "birth_date" in columns
    # **顔を巻き込まない。** 版は3のままなので、再構築の経路へ落ちてはいけない
    assert faces == 3
    assert any("列が足りていません" in message for message in messages)

    # 直ったので、今度は普通に開ける
    db.ensure_database(str(database)).close()
    assert needs_migration(str(database)) is False


def test_the_expected_columns_come_from_the_schema_itself(tmp_path):
    """列の一覧を別に書き写さないこと。**写すと片方だけ古くなる。**"""
    expected = db.expected_columns()

    assert "birth_date" in expected["Person"]
    # `SCHEMA` から実際に作って読んでいるので、DDL と食い違いようがない
    database = tmp_path / "fresh.db"
    connection = db.ensure_database(str(database))
    try:
        for table, columns in expected.items():
            present = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            assert present == columns
        assert db.missing_columns(connection) == {}
    finally:
        connection.close()
