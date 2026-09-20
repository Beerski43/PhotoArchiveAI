"""旧スキーマのデータベースを現行スキーマへ移行する。

**版ごとに、やることがまったく違う。** 一括りに「移行」と呼ばないこと。

- **v1 → v2**: テーブル再構築。顔と解析結果を**破棄する**（下の方針）。
  破棄するのは旧特徴量が使えないからで、移行という操作の性質ではない
- **v2 → v3**: ``Person.birth_date`` を足すだけ。``ALTER TABLE`` の1文で、
  **何も破棄しない**。顔も解析結果も残る

**v1 の経路に v2 のDBを流し込まないこと。** 使えるはずの顔が消える。

v1 → v2 の方針:

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
    "memo TEXT,"
    "birth_date TEXT"
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
        # v2 からは列を足すだけで、何も捨てない。捨てる件数を出したままにすると
        # 「顔が消える」と読めてしまい、実行をためらわせる。
        info["rebuilds"] = info["schema_version"] < 2
        if not info["rebuilds"]:
            info["faces_to_drop"] = 0
            info["analysis_to_drop"] = 0
            if "Face" in tables:
                info["faces_kept"] = connection.execute(
                    "SELECT COUNT(*) FROM Face"
                ).fetchone()[0]
        return info
    finally:
        connection.close()


def describe_for_operator(database_path: str) -> str:
    """移行する前に利用者へ見せる説明。**CLI と GUI で同じ文面を使う。**

    2か所に書くと、片方だけが「破棄します」のまま残る。何が消えて何が残るかは
    実行をためらうかどうかを決める情報なので、**voice を1つにしておく。**
    """
    info = describe_migration(database_path)
    lines = [
        f"移行対象: {database_path}",
        f"  Media {info['media']} 件 / Person {info['persons']} 件 は保持します。",
    ]
    if info.get("rebuilds", True):
        # 旧スキーマ(v1)からの移行だけが、顔と解析結果を作り直す。
        lines.append(
            f"  顔データ {info['faces_to_drop']} 件と解析結果 {info['analysis_to_drop']} 件は破棄し、"
            " 顔検出をやり直します。"
        )
        lines.append("  移行後に VACUUM します（--no-vacuum で省けます）。")
    else:
        # **ここで「破棄します」と出してはいけない。** 列を足すだけなので何も消えない。
        # 消えると読めると、実行をためらって移行が進まなくなる。
        lines.append(
            f"  顔データ {info.get('faces_kept', 0)} 件はそのまま残ります"
            "（列を追加するだけの移行です）。"
        )
        # **黙って効かない引数を作らない。** 列を足すだけの移行はテーブルを
        # 組み直さないので VACUUM する理由が無い。`--no-vacuum` を付けても
        # 付けなくても同じ、という状態を画面に出す。
        lines.append("  VACUUM はしません（テーブルを組み直さないため）。")
    return "\n".join(lines)


def rebuilds_faces(database_path: str) -> bool:
    """この移行が顔を作り直すか（v1 からの移行だけが該当）。"""
    return bool(describe_migration(database_path).get("rebuilds", True))


def needs_migration(database_path: str) -> bool:
    """移行が要るか。**版の数字だけでなく、実際の形も見る。**

    列が足りないまま版だけ進んだデータベースがありうる（`db.missing_columns`）。
    数字しか見ないと、その状態を「最新」と答えて**直す手立てが無くなる。**
    """
    path = Path(database_path)
    if not path.exists():
        return False
    connection = sqlite3.connect(str(path))
    try:
        if not (_table_names(connection) & set(db.KNOWN_TABLES)):
            return False
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version < db.SCHEMA_VERSION:
            return True
        return bool(db.missing_columns(connection))
    finally:
        connection.close()


def _add_missing_columns(database_path: str, emit: Callable[[str], None]) -> Dict[str, Any]:
    """v2 以降のDBへ、足りない列を足すだけの移行。**何も破棄しない。**

    ``ALTER TABLE ... ADD COLUMN`` は既存行に NULL を入れるだけなので、
    顔・解析結果・メディアはそのまま残る。
    """
    connection = sqlite3.connect(str(database_path))
    connection.row_factory = sqlite3.Row
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"integrity_check failed: {integrity}")

        columns = {row["name"] for row in connection.execute("PRAGMA table_info(Person)")}
        if "birth_date" not in columns:
            connection.execute("ALTER TABLE Person ADD COLUMN birth_date TEXT")
            emit("Person に birth_date を追加しました（既存の行は未設定）。")

        remaining = db.missing_columns(connection)
        if remaining:
            # **直せなかった列があるまま版を刻まない。** 刻むと `migrate` が
            # 次から「すでに最新です」と言い、二度と直らなくなる。
            raise RuntimeError(
                "移行できない列が残っています: "
                f"{db.describe_missing_columns(remaining)}"
            )

        connection.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION}")
        connection.commit()
        after = {
            "media": connection.execute("SELECT COUNT(*) FROM Media").fetchone()[0],
            "persons": connection.execute("SELECT COUNT(*) FROM Person").fetchone()[0],
            "faces": connection.execute("SELECT COUNT(*) FROM Face").fetchone()[0],
            "unscanned": connection.execute(
                "SELECT COUNT(*) FROM Media WHERE face_count IS NULL"
            ).fetchone()[0],
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    emit(
        "移行が完了しました: "
        f"Media {after['media']}件 / Person {after['persons']}件 / "
        f"Face {after['faces']}件 (未スキャン {after['unscanned']}件)"
    )
    return after


def migrate_database(
    database_path: str,
    backup_path: Optional[str] = None,
    vacuum: bool = True,
    make_backup: bool = True,
    log: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """データベースを現行スキーマへ移行する。すでに現行なら何もしない。

    ``vacuum`` が効くのは **v1 からの移行だけ**。テーブルを組み直すので
    ファイルが縮む。列を足すだけの移行（v2 以降）は組み直さないため、
    VACUUM する理由が無く、この引数を見ない。**そのことは
    `describe_for_operator` が画面に出す。**
    """

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
        gaps = db.missing_columns(probe) if tables & set(db.KNOWN_TABLES) else {}
    finally:
        probe.close()

    if version >= db.SCHEMA_VERSION and not gaps:
        emit("スキーマはすでに最新です。移行は不要です。")
        result["schema_version"] = version
        return result

    if gaps and version >= db.SCHEMA_VERSION:
        # **版だけ進んでいて、形が追いついていない。** 列を足して辻褄を合わせる。
        emit(
            f"版は {version} ですが、列が足りていません"
            f"（{db.describe_missing_columns(gaps)}）。足りない列を追加します。"
        )

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

    if version >= 2:
        # v2 以降は列を足すだけ。**顔も解析結果も触らない。**
        # v1 の再構築経路へ流すと、使えるはずの顔が消える。
        # 版だけ進んで形が古いDBもここへ来る（版は 2 以上なので再構築しない）。
        after = _add_missing_columns(str(path), emit)
        result.update(migrated=True, schema_version=db.SCHEMA_VERSION, after=after)
        return result

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
