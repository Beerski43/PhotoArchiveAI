"""旧スキーマのデータベースを現行スキーマへ移行する。

方針:

- ``Media`` と ``Person`` の行は温存する。``Media.file_hash`` を残すことで、
  移行後の ``scan`` がサイズと更新時刻の一致でハッシュ再計算を丸ごと省ける。
- 顔データ (``FaceEmbedding``) は破棄する。旧実装の埋め込みは顔ランドマークの
  座標を並べただけのもので、現行の dlib 埋め込みとは互換性がない。
- ``AnalysisResult`` も破棄する。smile/quality は再スキャンで計算し直され、
  family は誤った紐づけの産物のため。
- 全 ``Media`` は ``face_count IS NULL`` (未スキャン) に戻る。

列の削除とテーブルの入れ替えを安全に行うため、ALTER ではなくテーブル再構築
方式をとる。SQLite は部分インデックスが参照する列を DROP COLUMN できない。
"""

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import db

MEDIA_NEW_DDL = (
    "CREATE TABLE Media_new ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "path TEXT UNIQUE NOT NULL,"
    "filename TEXT NOT NULL,"
    "type TEXT NOT NULL,"
    "file_hash TEXT NOT NULL,"
    "file_size INTEGER NOT NULL,"
    "created_time TEXT NOT NULL,"
    "shooting_date TEXT,"
    "face_count INTEGER,"
    "face_scanned_at TEXT,"
    "detector_version TEXT"
    ")"
)

PERSON_NEW_DDL = (
    "CREATE TABLE Person_new ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "name TEXT NOT NULL,"
    "relation TEXT,"
    "memo TEXT"
    ")"
)

ANALYSIS_NEW_DDL = (
    "CREATE TABLE AnalysisResult_new ("
    "media_id INTEGER PRIMARY KEY,"
    "family_score REAL,"
    "smile_score REAL,"
    "quality_score REAL,"
    "FOREIGN KEY(media_id) REFERENCES Media(id) ON DELETE CASCADE"
    ")"
)


def _table_names(connection: sqlite3.Connection) -> set:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def backup_database(database_path: str, backup_path: Optional[str] = None) -> Path:
    source = Path(database_path)
    if backup_path is None:
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        target = source.with_name(f"{source.name}.bak-{stamp}")
    else:
        target = Path(backup_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target


def describe_migration(database_path: str) -> Dict[str, Any]:
    """移行で何が起きるかを事前に集計する。"""
    connection = sqlite3.connect(str(database_path))
    connection.row_factory = sqlite3.Row
    try:
        tables = _table_names(connection)
        info: Dict[str, Any] = {
            "schema_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
            "tables": sorted(tables),
            "media": 0,
            "persons": 0,
            "faces_to_drop": 0,
            "analysis_to_drop": 0,
        }
        if "Media" in tables:
            info["media"] = connection.execute("SELECT COUNT(*) FROM Media").fetchone()[0]
        if "Person" in tables:
            info["persons"] = connection.execute("SELECT COUNT(*) FROM Person").fetchone()[0]
        if "FaceEmbedding" in tables:
            info["faces_to_drop"] = connection.execute(
                "SELECT COUNT(*) FROM FaceEmbedding"
            ).fetchone()[0]
        if "AnalysisResult" in tables:
            info["analysis_to_drop"] = connection.execute(
                "SELECT COUNT(*) FROM AnalysisResult"
            ).fetchone()[0]
        return info
    finally:
        connection.close()


def needs_migration(database_path: str) -> bool:
    path = Path(database_path)
    if not path.exists():
        return False
    connection = sqlite3.connect(str(path))
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version >= db.SCHEMA_VERSION:
            return False
        return bool(_table_names(connection) & set(db.KNOWN_TABLES))
    finally:
        connection.close()


def migrate_database(
    database_path: str,
    backup_path: Optional[str] = None,
    vacuum: bool = True,
    make_backup: bool = True,
    log: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """データベースを現行スキーマへ移行する。すでに現行なら何もしない。"""

    def emit(message: str) -> None:
        if log is not None:
            log(message)

    path = Path(database_path)
    if not path.exists():
        raise FileNotFoundError(f"Database does not exist: {database_path}")

    result: Dict[str, Any] = {"migrated": False, "backup": None}

    probe = sqlite3.connect(str(path))
    try:
        version = int(probe.execute("PRAGMA user_version").fetchone()[0])
        tables = _table_names(probe)
    finally:
        probe.close()

    if version >= db.SCHEMA_VERSION:
        emit("スキーマはすでに最新です。移行は不要です。")
        result["schema_version"] = version
        return result

    if not (tables & set(db.KNOWN_TABLES)):
        # 空のファイル。単に最新スキーマを作る。
        connection = db.connect(str(path))
        try:
            db.create_tables(connection)
        finally:
            connection.close()
        emit("空のデータベースに最新スキーマを作成しました。")
        result.update(migrated=True, schema_version=db.SCHEMA_VERSION)
        return result

    summary = describe_migration(str(path))
    result["before"] = summary

    if make_backup:
        backup = backup_database(str(path), backup_path)
        result["backup"] = str(backup)
        emit(f"バックアップを作成しました: {backup}")

    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"integrity_check failed: {integrity}")

        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")

        connection.execute(MEDIA_NEW_DDL)
        connection.execute(PERSON_NEW_DDL)
        connection.execute(ANALYSIS_NEW_DDL)

        connection.execute(
            "INSERT INTO Media_new"
            " (id, path, filename, type, file_hash, file_size, created_time, shooting_date,"
            "  face_count, face_scanned_at, detector_version)"
            " SELECT id, path, filename, type, file_hash, file_size, created_time, shooting_date,"
            "  NULL, NULL, NULL FROM Media"
        )
        connection.execute(
            "INSERT INTO Person_new (id, name, relation, memo)"
            " SELECT id, name, relation, memo FROM Person"
        )

        for table in ("FaceEmbedding", "Face", "AnalysisResult", "Media", "Person"):
            connection.execute(f"DROP TABLE IF EXISTS {table}")

        connection.execute("ALTER TABLE Media_new RENAME TO Media")
        connection.execute("ALTER TABLE Person_new RENAME TO Person")
        connection.execute("ALTER TABLE AnalysisResult_new RENAME TO AnalysisResult")

        connection.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION}")
        connection.commit()
    except Exception:
        connection.rollback()
        connection.close()
        raise

    try:
        # Face テーブルと各インデックスは通常のスキーマ生成に任せる。
        connection.execute("PRAGMA foreign_keys = ON")
        db.create_tables(connection)
        if vacuum:
            emit("VACUUM を実行しています...")
            connection.execute("VACUUM")
        after = {
            "media": connection.execute("SELECT COUNT(*) FROM Media").fetchone()[0],
            "persons": connection.execute("SELECT COUNT(*) FROM Person").fetchone()[0],
            "faces": connection.execute("SELECT COUNT(*) FROM Face").fetchone()[0],
            "unscanned": connection.execute(
                "SELECT COUNT(*) FROM Media WHERE face_count IS NULL"
            ).fetchone()[0],
        }
    finally:
        connection.close()

    result.update(migrated=True, schema_version=db.SCHEMA_VERSION, after=after)
    emit(
        "移行が完了しました: "
        f"Media {after['media']}件 / Person {after['persons']}件 / "
        f"Face {after['faces']}件 (未スキャン {after['unscanned']}件)"
    )
    return result
