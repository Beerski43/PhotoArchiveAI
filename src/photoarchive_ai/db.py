import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS Media ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "path TEXT UNIQUE NOT NULL,"
    "filename TEXT NOT NULL,"
    "type TEXT NOT NULL,"
    "file_hash TEXT NOT NULL,"
    "file_size INTEGER NOT NULL,"
    "created_time TEXT NOT NULL,"
    "shooting_date TEXT,"
    "analyzed_date TEXT,"
    "analyzer_version TEXT"
    ")",
    "CREATE TABLE IF NOT EXISTS Person ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "name TEXT NOT NULL,"
    "relation TEXT,"
    "memo TEXT"
    ")",
    "CREATE TABLE IF NOT EXISTS FaceEmbedding ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "media_id INTEGER,"
    "person_id INTEGER,"
    "embedding TEXT NOT NULL,"
    "similarity_score REAL,"
    "face_image BLOB,"
    "age INTEGER,"
    "added_at TEXT NOT NULL,"
    "FOREIGN KEY(media_id) REFERENCES Media(id),"
    "FOREIGN KEY(person_id) REFERENCES Person(id)"
    ")",
    "CREATE TABLE IF NOT EXISTS AnalysisResult ("
    "media_id INTEGER PRIMARY KEY,"
    "face_count INTEGER,"
    "family_score REAL,"
    "smile_score REAL,"
    "quality_score REAL,"
    "duplicate_group TEXT,"
    "event_category TEXT,"
    "FOREIGN KEY(media_id) REFERENCES Media(id)"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_media_hash ON Media(file_hash)",
    "CREATE INDEX IF NOT EXISTS idx_media_type ON Media(type)",
    "CREATE INDEX IF NOT EXISTS idx_face_person ON FaceEmbedding(person_id)",
]


def connect(database_path: str) -> sqlite3.Connection:
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(database_path))
    connection.row_factory = sqlite3.Row
    return connection


def create_tables(connection: sqlite3.Connection) -> None:
    cursor = connection.cursor()
    for statement in SCHEMA:
        cursor.execute(statement)
    face_columns = {row[1] for row in cursor.execute("PRAGMA table_info(FaceEmbedding)").fetchall()}
    if "age" not in face_columns:
        cursor.execute("ALTER TABLE FaceEmbedding ADD COLUMN age INTEGER")
    connection.commit()


def ensure_database(database_path: str) -> sqlite3.Connection:
    connection = connect(database_path)
    create_tables(connection)
    return connection


def initialize_database(database_path: str) -> None:
    with ensure_database(database_path):
        pass


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    if row is None:
        return {}
    return dict(row)


def save_media(connection: sqlite3.Connection, media: Dict[str, Any]) -> int:
    cursor = connection.cursor()
    row = cursor.execute(
        "SELECT id, file_hash FROM Media WHERE path = ?",
        (media["path"],),
    ).fetchone()
    if row is None:
        cursor.execute(
            "INSERT INTO Media (path, filename, type, file_hash, file_size, created_time, shooting_date, analyzed_date, analyzer_version)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                media["path"],
                media["filename"],
                media["type"],
                media["file_hash"],
                media["file_size"],
                media["created_time"],
                media.get("shooting_date"),
                media.get("analyzed_date"),
                media.get("analyzer_version"),
            ),
        )
        connection.commit()
        return cursor.lastrowid
    if row["file_hash"] != media["file_hash"]:
        cursor.execute(
            "UPDATE Media SET filename=?, type=?, file_hash=?, file_size=?, created_time=?, shooting_date=?, analyzed_date=?, analyzer_version=? WHERE path=?",
            (
                media["filename"],
                media["type"],
                media["file_hash"],
                media["file_size"],
                media["created_time"],
                media.get("shooting_date"),
                media.get("analyzed_date"),
                media.get("analyzer_version"),
                media["path"],
            ),
        )
        connection.commit()
        return row["id"]
    return row["id"]


def list_media(connection: sqlite3.Connection) -> List[Dict[str, Any]]:
    cursor = connection.cursor()
    rows = cursor.execute("SELECT * FROM Media ORDER BY path").fetchall()
    return [dict(row) for row in rows]


def get_media_for_analysis(connection: sqlite3.Connection, analyzer_version: str) -> List[Dict[str, Any]]:
    cursor = connection.cursor()
    rows = cursor.execute(
        "SELECT * FROM Media WHERE analyzed_date IS NULL OR analyzer_version != ? ORDER BY path",
        (analyzer_version,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_media_by_path(connection: sqlite3.Connection, path: str) -> Optional[Dict[str, Any]]:
    cursor = connection.cursor()
    row = cursor.execute("SELECT * FROM Media WHERE path = ?", (path,)).fetchone()
    return _row_to_dict(row)


def add_person(
    connection: sqlite3.Connection,
    name: str,
    relation: Optional[str] = None,
    memo: Optional[str] = None,
) -> int:
    cursor = connection.cursor()
    cursor.execute(
        "INSERT INTO Person (name, relation, memo) VALUES (?, ?, ?)",
        (name, relation, memo),
    )
    connection.commit()
    return cursor.lastrowid


def update_person(
    connection: sqlite3.Connection,
    person_id: int,
    name: str,
    relation: Optional[str],
    memo: Optional[str],
) -> None:
    cursor = connection.cursor()
    cursor.execute(
        "UPDATE Person SET name = ?, relation = ?, memo = ? WHERE id = ?",
        (name, relation, memo, person_id),
    )
    connection.commit()


def delete_person(connection: sqlite3.Connection, person_id: int) -> None:
    cursor = connection.cursor()
    cursor.execute("DELETE FROM FaceEmbedding WHERE person_id = ?", (person_id,))
    cursor.execute("DELETE FROM Person WHERE id = ?", (person_id,))
    connection.commit()


def list_persons(connection: sqlite3.Connection) -> List[Dict[str, Any]]:
    cursor = connection.cursor()
    rows = cursor.execute("SELECT * FROM Person ORDER BY name").fetchall()
    return [dict(row) for row in rows]


def get_person_embeddings(connection: sqlite3.Connection, person_id: int) -> List[List[float]]:
    cursor = connection.cursor()
    rows = cursor.execute(
        "SELECT embedding FROM FaceEmbedding WHERE person_id = ?", (person_id,)
    ).fetchall()
    embeddings = []
    for row in rows:
        if row["embedding"]:
            embeddings.append(json.loads(row["embedding"]))
    return embeddings


def add_face_embedding(
    connection: sqlite3.Connection,
    person_id: Optional[int],
    embedding: List[float],
    similarity_score: Optional[float] = None,
    face_image: Optional[bytes] = None,
    media_id: Optional[int] = None,
    age: Optional[int] = None,
) -> int:
    cursor = connection.cursor()
    cursor.execute(
        "INSERT INTO FaceEmbedding (media_id, person_id, embedding, similarity_score, face_image, added_at, age) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            media_id,
            person_id,
            json.dumps(embedding),
            similarity_score,
            face_image,
            datetime.utcnow().isoformat(),
            age,
        ),
    )
    connection.commit()
    return cursor.lastrowid


def list_face_embeddings(connection: sqlite3.Connection, person_id: Optional[int] = None) -> List[Dict[str, Any]]:
    cursor = connection.cursor()
    if person_id is None:
        rows = cursor.execute("SELECT * FROM FaceEmbedding ORDER BY added_at DESC").fetchall()
    else:
        rows = cursor.execute(
            "SELECT * FROM FaceEmbedding WHERE person_id = ? ORDER BY added_at DESC", (person_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def save_analysis_result(
    connection: sqlite3.Connection,
    media_id: int,
    face_count: int,
    family_score: float,
    smile_score: float,
    quality_score: float,
    duplicate_group: Optional[str] = None,
    event_category: Optional[str] = None,
) -> None:
    cursor = connection.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO AnalysisResult (media_id, face_count, family_score, smile_score, quality_score, duplicate_group, event_category) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (media_id, face_count, family_score, smile_score, quality_score, duplicate_group, event_category),
    )
    connection.commit()


def get_analysis_result(connection: sqlite3.Connection, media_id: int) -> Optional[Dict[str, Any]]:
    cursor = connection.cursor()
    row = cursor.execute("SELECT * FROM AnalysisResult WHERE media_id = ?", (media_id,)).fetchone()
    return _row_to_dict(row)


def get_media_with_analysis(connection: sqlite3.Connection) -> List[Dict[str, Any]]:
    cursor = connection.cursor()
    rows = cursor.execute(
        "SELECT M.*, AR.face_count, AR.family_score, AR.smile_score, AR.quality_score, AR.duplicate_group, AR.event_category FROM Media M LEFT JOIN AnalysisResult AR ON M.id = AR.media_id ORDER BY M.path"
    ).fetchall()
    return [dict(row) for row in rows]
