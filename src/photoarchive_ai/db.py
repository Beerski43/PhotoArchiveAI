"""SQLite persistence layer.

スキーマの考え方:

- ``Media``  : ファイルそのものと、顔検出の実施状態。``face_count`` は
               NULL=未scan / 0=顔なし / N=検出数 を表す。
- ``Face``   : 写真から検出された顔。人物への紐づけは ``person_id`` と
               ``assign_source`` ('manual' / 'auto' / 'rejected') で表す。
               手動割当だけが自動紐づけ (match) の手本になる。
- ``Person`` : 人物。
- ``AnalysisResult`` : メディア単位のスコア。smile/quality は scan が、
               family は match が書く。
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

import numpy as np

SCHEMA_VERSION = 2

EMBEDDING_DIM = 128
EMBEDDING_DTYPE = np.float32

ASSIGN_MANUAL = "manual"
ASSIGN_AUTO = "auto"
ASSIGN_REJECTED = "rejected"

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
    "face_count INTEGER,"
    "face_scanned_at TEXT,"
    "detector_version TEXT"
    ")",
    "CREATE TABLE IF NOT EXISTS Person ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "name TEXT NOT NULL,"
    "relation TEXT,"
    "memo TEXT"
    ")",
    "CREATE TABLE IF NOT EXISTS Face ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "media_id INTEGER NOT NULL,"
    "bbox_top INTEGER NOT NULL,"
    "bbox_right INTEGER NOT NULL,"
    "bbox_bottom INTEGER NOT NULL,"
    "bbox_left INTEGER NOT NULL,"
    "detection_score REAL,"
    "embedding BLOB,"
    "embed_version TEXT NOT NULL,"
    "thumbnail BLOB,"
    "smile_score REAL,"
    "quality_score REAL,"
    "person_id INTEGER,"
    "assign_source TEXT,"
    "assign_score REAL,"
    "assigned_at TEXT,"
    "age INTEGER,"
    "created_at TEXT NOT NULL,"
    "FOREIGN KEY(media_id) REFERENCES Media(id) ON DELETE CASCADE,"
    "FOREIGN KEY(person_id) REFERENCES Person(id) ON DELETE SET NULL"
    ")",
    "CREATE TABLE IF NOT EXISTS AnalysisResult ("
    "media_id INTEGER PRIMARY KEY,"
    "family_score REAL,"
    "smile_score REAL,"
    "quality_score REAL,"
    "FOREIGN KEY(media_id) REFERENCES Media(id) ON DELETE CASCADE"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_media_hash ON Media(file_hash)",
    "CREATE INDEX IF NOT EXISTS idx_media_type ON Media(type)",
    "CREATE INDEX IF NOT EXISTS idx_media_unscanned ON Media(id) WHERE face_count IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_face_media ON Face(media_id)",
    "CREATE INDEX IF NOT EXISTS idx_face_person ON Face(person_id)",
    "CREATE INDEX IF NOT EXISTS idx_face_manual ON Face(person_id) WHERE assign_source = 'manual'",
    "CREATE INDEX IF NOT EXISTS idx_face_unassigned ON Face(quality_score DESC, id) WHERE assign_source IS NULL",
]

KNOWN_TABLES = ("Media", "Person", "Face", "FaceEmbedding", "AnalysisResult")


class SchemaVersionError(RuntimeError):
    """既存DBのスキーマが古く、移行が必要なときに送出する。"""


# ---------------------------------------------------------------------------
# connection
# ---------------------------------------------------------------------------


def connect(database_path: str) -> sqlite3.Connection:
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(database_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.DatabaseError:
        # メモリDBや一部のファイルシステムではWALに切り替えられない
        pass
    return connection


def get_schema_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def has_any_table(connection: sqlite3.Connection) -> bool:
    placeholders = ",".join("?" for _ in KNOWN_TABLES)
    row = connection.execute(
        f"SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name IN ({placeholders})",
        KNOWN_TABLES,
    ).fetchone()
    return row[0] > 0


def create_tables(connection: sqlite3.Connection) -> None:
    """最新スキーマを用意する。旧スキーマのDBは移行を促して中断する。"""
    version = get_schema_version(connection)
    if version == 0 and has_any_table(connection):
        raise SchemaVersionError(
            "データベースのスキーマが古い形式です。"
            "`photoarchive migrate --db <データベース>` を実行してください。"
        )
    if version > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"データベースのスキーマ({version})がこのアプリケーション"
            f"({SCHEMA_VERSION})より新しいため開けません。"
        )
    cursor = connection.cursor()
    for statement in SCHEMA:
        cursor.execute(statement)
    cursor.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    connection.commit()


def ensure_database(database_path: str) -> sqlite3.Connection:
    connection = connect(database_path)
    try:
        create_tables(connection)
    except Exception:
        connection.close()
        raise
    return connection


def initialize_database(database_path: str) -> None:
    with ensure_database(database_path):
        pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _row_to_dict(row: Optional[sqlite3.Row]) -> Dict[str, Any]:
    if row is None:
        return {}
    return dict(row)


# ---------------------------------------------------------------------------
# embedding encoding
# ---------------------------------------------------------------------------


def encode_embedding(embedding: Optional[Sequence[float]]) -> Optional[bytes]:
    """128次元の埋め込みを float32 のバイト列にする。"""
    if embedding is None:
        return None
    array = np.asarray(embedding, dtype=EMBEDDING_DTYPE)
    if array.shape != (EMBEDDING_DIM,):
        raise ValueError(f"embedding must have shape ({EMBEDDING_DIM},), got {array.shape}")
    return array.tobytes()


def decode_embedding(blob: Optional[bytes]) -> Optional[np.ndarray]:
    if blob is None:
        return None
    return np.frombuffer(blob, dtype=EMBEDDING_DTYPE)


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------

# ファイルそのものの属性。中身が同じでも作り直されうる。
_MEDIA_FILE_COLUMNS = (
    "filename",
    "type",
    "file_hash",
    "file_size",
    "created_time",
    "shooting_date",
)
# 顔検出の実施状態。ファイル属性の更新では触らない。
_MEDIA_SCAN_COLUMNS = (
    "face_count",
    "face_scanned_at",
    "detector_version",
)
_MEDIA_WRITE_COLUMNS = ("path",) + _MEDIA_FILE_COLUMNS + _MEDIA_SCAN_COLUMNS


def save_media(connection: sqlite3.Connection, media: Dict[str, Any]) -> int:
    """パスをキーにメディアを登録・更新し、そのidを返す。

    ハッシュが変わっている場合はファイル情報を更新し、顔検出の状態を
    リセットする(``face_count`` を NULL に戻す)。既存の ``Face`` と
    ``AnalysisResult`` は呼び出し側が削除する。

    **ハッシュが同じでもファイル属性は書き戻す。** 中身は同じでも更新時刻
    だけが変わることがあり(コピーや touch)、書き戻さないと差分スキャンが
    毎回「変わったかもしれない」と判断して SHA-256 のために全体を読み直す。
    ハッシュが同じなら再び書き戻されないので、これが恒久的に続く。
    このときは ``face_count`` などの検出状態を触らない。触ると検出済みの
    メディアが未スキャンに戻ってしまう。
    """
    cursor = connection.cursor()
    row = cursor.execute(
        "SELECT id, file_hash FROM Media WHERE path = ?",
        (media["path"],),
    ).fetchone()
    if row is None:
        values = tuple(media.get(column) for column in _MEDIA_WRITE_COLUMNS)
        placeholders = ",".join("?" for _ in _MEDIA_WRITE_COLUMNS)
        cursor.execute(
            f"INSERT INTO Media ({','.join(_MEDIA_WRITE_COLUMNS)}) VALUES ({placeholders})",
            values,
        )
        connection.commit()
        return cursor.lastrowid

    if row["file_hash"] != media["file_hash"]:
        columns = _MEDIA_FILE_COLUMNS + _MEDIA_SCAN_COLUMNS
    else:
        columns = _MEDIA_FILE_COLUMNS
    assignments = ",".join(f"{column}=?" for column in columns)
    cursor.execute(
        f"UPDATE Media SET {assignments} WHERE path = ?",
        tuple(media.get(column) for column in columns) + (media["path"],),
    )
    connection.commit()
    return row["id"]


def list_media(connection: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = connection.execute("SELECT * FROM Media ORDER BY path").fetchall()
    return [dict(row) for row in rows]


def get_media_by_path(connection: sqlite3.Connection, path: str) -> Optional[Dict[str, Any]]:
    row = connection.execute("SELECT * FROM Media WHERE path = ?", (path,)).fetchone()
    return _row_to_dict(row)


def get_media_by_id(connection: sqlite3.Connection, media_id: int) -> Optional[Dict[str, Any]]:
    row = connection.execute("SELECT * FROM Media WHERE id = ?", (media_id,)).fetchone()
    return _row_to_dict(row)


def load_media_index(connection: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """scan の差分判定用に、パスをキーにした一覧を1クエリで読み出す。"""
    rows = connection.execute(
        "SELECT id, path, file_hash, file_size, created_time, face_count, detector_version FROM Media"
    ).fetchall()
    return {row["path"]: dict(row) for row in rows}


def update_media_scan_state(
    connection: sqlite3.Connection,
    media_id: int,
    face_count: Optional[int],
    face_scanned_at: Optional[str],
    detector_version: Optional[str],
) -> None:
    connection.execute(
        "UPDATE Media SET face_count = ?, face_scanned_at = ?, detector_version = ? WHERE id = ?",
        (face_count, face_scanned_at, detector_version, media_id),
    )


def delete_media(connection: sqlite3.Connection, media_ids: Iterable[int]) -> int:
    """メディアを削除する。Face と AnalysisResult は外部キーで連鎖削除される。"""
    media_ids = list(media_ids)
    if not media_ids:
        return 0
    cursor = connection.cursor()
    deleted = 0
    for start in range(0, len(media_ids), 500):
        chunk = media_ids[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        cursor.execute(f"DELETE FROM Media WHERE id IN ({placeholders})", chunk)
        deleted += cursor.rowcount
    connection.commit()
    return deleted


def get_media_with_analysis(connection: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = connection.execute(
        "SELECT M.*, AR.family_score, AR.smile_score, AR.quality_score"
        " FROM Media M LEFT JOIN AnalysisResult AR ON M.id = AR.media_id"
        " ORDER BY M.path"
    ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Person
# ---------------------------------------------------------------------------


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
    connection.execute(
        "UPDATE Person SET name = ?, relation = ?, memo = ? WHERE id = ?",
        (name, relation, memo, person_id),
    )
    connection.commit()


def delete_person(connection: sqlite3.Connection, person_id: int) -> None:
    """人物を削除する。紐づいていた顔は削除せず未割当に戻す。"""
    cursor = connection.cursor()
    cursor.execute(
        "UPDATE Face SET person_id = NULL, assign_source = NULL, assign_score = NULL,"
        " assigned_at = NULL WHERE person_id = ?",
        (person_id,),
    )
    cursor.execute("DELETE FROM Person WHERE id = ?", (person_id,))
    connection.commit()


def list_persons(connection: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = connection.execute("SELECT * FROM Person ORDER BY name").fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Face
# ---------------------------------------------------------------------------

FACE_LIST_COLUMNS = (
    "id",
    "media_id",
    "person_id",
    "assign_source",
    "assign_score",
    "age",
    "quality_score",
    "smile_score",
)


def add_face(
    connection: sqlite3.Connection,
    media_id: int,
    bbox: Tuple[int, int, int, int],
    embedding: Optional[Sequence[float]],
    embed_version: str,
    thumbnail: Optional[bytes] = None,
    detection_score: Optional[float] = None,
    smile_score: Optional[float] = None,
    quality_score: Optional[float] = None,
    person_id: Optional[int] = None,
    assign_source: Optional[str] = None,
    assign_score: Optional[float] = None,
    age: Optional[int] = None,
) -> int:
    """検出された顔を1件登録する。``bbox`` は (top, right, bottom, left)。"""
    top, right, bottom, left = bbox
    now = _utc_now()
    cursor = connection.cursor()
    cursor.execute(
        "INSERT INTO Face (media_id, bbox_top, bbox_right, bbox_bottom, bbox_left,"
        " detection_score, embedding, embed_version, thumbnail, smile_score, quality_score,"
        " person_id, assign_source, assign_score, assigned_at, age, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            media_id,
            int(top),
            int(right),
            int(bottom),
            int(left),
            detection_score,
            encode_embedding(embedding),
            embed_version,
            thumbnail,
            smile_score,
            quality_score,
            person_id,
            assign_source,
            assign_score,
            now if person_id is not None else None,
            age,
            now,
        ),
    )
    return cursor.lastrowid


def delete_faces_for_media(connection: sqlite3.Connection, media_id: int) -> int:
    cursor = connection.cursor()
    cursor.execute("DELETE FROM Face WHERE media_id = ?", (media_id,))
    return cursor.rowcount


def count_manual_faces_for_media(connection: sqlite3.Connection, media_id: int) -> int:
    row = connection.execute(
        "SELECT COUNT(*) FROM Face WHERE media_id = ? AND assign_source = ?",
        (media_id, ASSIGN_MANUAL),
    ).fetchone()
    return int(row[0])


def count_faces(
    connection: sqlite3.Connection,
    assign_source: Optional[str] = None,
    person_id: Optional[int] = None,
    unassigned: bool = False,
    min_age: Optional[int] = None,
    max_age: Optional[int] = None,
) -> int:
    """``list_faces`` と同じ条件での件数。ページャの総数に使う。"""
    where, params = _face_filter(assign_source, person_id, unassigned, min_age, max_age)
    row = connection.execute(f"SELECT COUNT(*) FROM Face{where}", params).fetchone()
    return int(row[0])


def _face_filter(
    assign_source: Optional[str],
    person_id: Optional[int],
    unassigned: bool,
    min_age: Optional[int] = None,
    max_age: Optional[int] = None,
) -> Tuple[str, List[Any]]:
    """顔の絞り込み条件。``list_faces`` と ``count_faces`` で同じものを使う。

    年齢の未設定(NULL)は、範囲を指定しても常に残す。年齢を入れていない顔が
    一覧から消えてしまうと、そもそも年齢を入れられなくなるため。
    """
    clauses: List[str] = []
    params: List[Any] = []
    if unassigned:
        clauses.append("assign_source IS NULL")
    elif assign_source is not None:
        clauses.append("assign_source = ?")
        params.append(assign_source)
    if person_id is not None:
        clauses.append("person_id = ?")
        params.append(person_id)
    if min_age is not None:
        clauses.append("(age IS NULL OR age >= ?)")
        params.append(min_age)
    if max_age is not None:
        clauses.append("(age IS NULL OR age <= ?)")
        params.append(max_age)
    if not clauses:
        return "", params
    return " WHERE " + " AND ".join(clauses), params


def shooting_dates_for_faces(
    connection: sqlite3.Connection, face_ids: Sequence[int]
) -> List[str]:
    """選んだ顔が写っているメディアの撮影日時を、昇順で返す。

    **年齢をまとめて入れるときに、撮影日時がまたがっていないかを見るため。**
    EXIF の無いメディアは日時を持たないので、返る件数は顔の件数と一致しない。
    """
    if not face_ids:
        return []
    placeholders = ",".join("?" for _ in face_ids)
    rows = connection.execute(
        "SELECT DISTINCT m.shooting_date FROM Face f JOIN Media m ON m.id = f.media_id"
        f" WHERE f.id IN ({placeholders}) AND m.shooting_date IS NOT NULL"
        " ORDER BY m.shooting_date",
        tuple(face_ids),
    ).fetchall()
    return [row[0] for row in rows]


def list_faces(
    connection: sqlite3.Connection,
    assign_source: Optional[str] = None,
    person_id: Optional[int] = None,
    unassigned: bool = False,
    limit: Optional[int] = None,
    offset: int = 0,
    with_thumbnail: bool = False,
    min_age: Optional[int] = None,
    max_age: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """顔を一覧する。

    サムネイルBLOBは件数が増えると重いので、``with_thumbnail`` を指定した
    ときだけ読み出す。並び順は品質スコアの高い順で、割り当てやすい顔が
    先に出るようにしている。
    """
    columns = list(FACE_LIST_COLUMNS)
    if with_thumbnail:
        columns.append("thumbnail")
    where, params = _face_filter(assign_source, person_id, unassigned, min_age, max_age)
    query = (
        f"SELECT {','.join(columns)} FROM Face{where}"
        " ORDER BY quality_score DESC, id ASC"
    )
    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        params = params + [limit, offset]
    rows = connection.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def get_face(connection: sqlite3.Connection, face_id: int) -> Optional[Dict[str, Any]]:
    row = connection.execute("SELECT * FROM Face WHERE id = ?", (face_id,)).fetchone()
    return _row_to_dict(row)


class _KeepAge:
    """``assign_faces`` の ``age`` 既定値。「年齢は触らない」を表す。

    ``None`` は「未設定に戻す」という**指示**なので、既定値として使えない。
    区別しないと、一度入れた年齢を未設定へ戻せなくなる。
    """

    def __repr__(self) -> str:  # pragma: no cover - 表示用
        return "KEEP_AGE"


KEEP_AGE = _KeepAge()


def assign_faces(
    connection: sqlite3.Connection,
    face_ids: Sequence[int],
    person_id: int,
    assign_source: str = ASSIGN_MANUAL,
    assign_score: Optional[float] = None,
    age: Any = KEEP_AGE,
) -> int:
    """顔を人物へ割り当てる。

    ``age`` を省くと年齢は触らない。``None`` を明示すると未設定へ戻す。
    """
    if not face_ids:
        return 0
    now = _utc_now()
    cursor = connection.cursor()
    if isinstance(age, _KeepAge):
        cursor.executemany(
            "UPDATE Face SET person_id = ?, assign_source = ?, assign_score = ?,"
            " assigned_at = ? WHERE id = ?",
            [(person_id, assign_source, assign_score, now, face_id) for face_id in face_ids],
        )
    else:
        cursor.executemany(
            "UPDATE Face SET person_id = ?, assign_source = ?, assign_score = ?,"
            " assigned_at = ?, age = ? WHERE id = ?",
            [(person_id, assign_source, assign_score, now, age, face_id) for face_id in face_ids],
        )
    connection.commit()
    return cursor.rowcount


def unassign_faces(connection: sqlite3.Connection, face_ids: Sequence[int]) -> int:
    if not face_ids:
        return 0
    cursor = connection.cursor()
    cursor.executemany(
        "UPDATE Face SET person_id = NULL, assign_source = NULL, assign_score = NULL,"
        " assigned_at = NULL WHERE id = ?",
        [(face_id,) for face_id in face_ids],
    )
    connection.commit()
    return cursor.rowcount


def reject_faces(connection: sqlite3.Connection, face_ids: Sequence[int]) -> int:
    """「誰でもない顔」として、未割当一覧からも自動紐づけからも外す。"""
    if not face_ids:
        return 0
    now = _utc_now()
    cursor = connection.cursor()
    cursor.executemany(
        "UPDATE Face SET person_id = NULL, assign_source = ?, assign_score = NULL,"
        " assigned_at = ? WHERE id = ?",
        [(ASSIGN_REJECTED, now, face_id) for face_id in face_ids],
    )
    connection.commit()
    return cursor.rowcount


def set_face_age(connection: sqlite3.Connection, face_id: int, age: Optional[int]) -> None:
    connection.execute("UPDATE Face SET age = ? WHERE id = ?", (age, face_id))
    connection.commit()


def load_manual_embeddings(
    connection: sqlite3.Connection,
) -> Tuple[np.ndarray, np.ndarray]:
    """自動紐づけの手本を読み出す。

    自動紐づけの結果 (``assign_source='auto'``) は教師に含めない。混ぜると
    誤った紐づけが次回以降の基準として増幅されるため。

    戻り値は (埋め込み行列 (K, 128), 人物ID配列 (K,)) で、**同じ添字が同じ顔**
    を指す。
    """
    rows = connection.execute(
        "SELECT person_id, embedding FROM Face"
        " WHERE person_id IS NOT NULL AND assign_source = ? AND embedding IS NOT NULL",
        (ASSIGN_MANUAL,),
    ).fetchall()
    if not rows:
        return (
            np.empty((0, EMBEDDING_DIM), dtype=EMBEDDING_DTYPE),
            np.empty((0,), dtype=np.int64),
        )
    matrix = np.vstack([decode_embedding(row["embedding"]) for row in rows])
    person_ids = np.asarray([row["person_id"] for row in rows], dtype=np.int64)
    return matrix, person_ids


class ManualFaces(NamedTuple):
    """手本の顔を、評価に必要な付随情報ごと持つ。

    ``face_ids`` / ``media_ids`` / ``person_ids`` / ``embeddings`` は
    **同じ添字が同じ顔**を指す。
    """

    face_ids: np.ndarray
    media_ids: np.ndarray
    person_ids: np.ndarray
    embeddings: np.ndarray


def load_manual_faces(connection: sqlite3.Connection) -> ManualFaces:
    """手本の顔を、face_id と media_id つきで読み出す。

    ``load_manual_embeddings`` との違いは添字を引ける情報が付くこと。
    交差検証は「いま抜いている顔はどれか」「同じ写真に写っている手本はどれか」
    を知る必要があるが、match 本体には不要なので関数を分けている。
    """
    rows = connection.execute(
        "SELECT id, media_id, person_id, embedding FROM Face"
        " WHERE person_id IS NOT NULL AND assign_source = ? AND embedding IS NOT NULL"
        " ORDER BY id",
        (ASSIGN_MANUAL,),
    ).fetchall()
    if not rows:
        return ManualFaces(
            np.empty((0,), dtype=np.int64),
            np.empty((0,), dtype=np.int64),
            np.empty((0,), dtype=np.int64),
            np.empty((0, EMBEDDING_DIM), dtype=EMBEDDING_DTYPE),
        )
    return ManualFaces(
        np.asarray([row["id"] for row in rows], dtype=np.int64),
        np.asarray([row["media_id"] for row in rows], dtype=np.int64),
        np.asarray([row["person_id"] for row in rows], dtype=np.int64),
        np.vstack([decode_embedding(row["embedding"]) for row in rows]),
    )


#: match が一度に読み出す顔の件数。matcher と二重に持たない。
MATCH_CHUNK_SIZE = 5000


def _match_candidate_filter(include_auto: bool) -> str:
    """match が対象にする顔の条件。

    ``include_auto`` は dry-run 用。本番実行は先に自動割り当てを取り消して
    から候補を数えるので、取り消しを行わない dry-run で ``auto`` を除くと、
    2回目以降の件数と距離の分布が実際より小さく出てしまう。
    """
    assigned = "(assign_source IS NULL OR assign_source = 'auto')" if include_auto else "assign_source IS NULL"
    return f" WHERE {assigned} AND embedding IS NOT NULL"


def count_match_candidates(
    connection: sqlite3.Connection, include_auto: bool = False
) -> int:
    """``iter_unassigned_embeddings`` が返すのと同じ集合の件数。

    進捗の分母に使う。``count_faces`` だと埋め込みを持たない顔まで数えて
    しまい、100% に届かないまま終わる。
    """
    where = _match_candidate_filter(include_auto)
    return int(connection.execute(f"SELECT COUNT(*) FROM Face{where}").fetchone()[0])


def iter_unassigned_embeddings(
    connection: sqlite3.Connection,
    chunk_size: int = MATCH_CHUNK_SIZE,
    include_auto: bool = False,
) -> Iterable[Tuple[np.ndarray, np.ndarray]]:
    """未割当かつ埋め込みを持つ顔を (id配列, 埋め込み行列) の塊で返す。"""
    where = _match_candidate_filter(include_auto)
    offset = 0
    while True:
        rows = connection.execute(
            f"SELECT id, embedding FROM Face{where} ORDER BY id LIMIT ? OFFSET ?",
            (chunk_size, offset),
        ).fetchall()
        if not rows:
            return
        ids = np.asarray([row["id"] for row in rows], dtype=np.int64)
        matrix = np.vstack([decode_embedding(row["embedding"]) for row in rows])
        yield ids, matrix
        offset += len(rows)


def apply_auto_assignments(
    connection: sqlite3.Connection,
    updates: Sequence[Tuple[int, int, float]],
) -> int:
    """(face_id, person_id, assign_score) をまとめて書き込む。"""
    if not updates:
        return 0
    now = _utc_now()
    cursor = connection.cursor()
    cursor.executemany(
        "UPDATE Face SET person_id = ?, assign_source = ?, assign_score = ?,"
        " assigned_at = ? WHERE id = ?",
        [
            (person_id, ASSIGN_AUTO, assign_score, now, face_id)
            for face_id, person_id, assign_score in updates
        ],
    )
    connection.commit()
    return cursor.rowcount


def reset_auto_assignments(connection: sqlite3.Connection) -> int:
    cursor = connection.cursor()
    cursor.execute(
        "UPDATE Face SET person_id = NULL, assign_source = NULL, assign_score = NULL,"
        " assigned_at = NULL WHERE assign_source = ?",
        (ASSIGN_AUTO,),
    )
    connection.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# AnalysisResult
# ---------------------------------------------------------------------------


def save_media_scores(
    connection: sqlite3.Connection,
    media_id: int,
    smile_score: Optional[float],
    quality_score: Optional[float],
) -> None:
    """scan が算出するスコアを保存する。``family_score`` は触らない。"""
    connection.execute(
        "INSERT INTO AnalysisResult (media_id, smile_score, quality_score)"
        " VALUES (?, ?, ?)"
        " ON CONFLICT(media_id) DO UPDATE SET"
        " smile_score = excluded.smile_score, quality_score = excluded.quality_score",
        (media_id, smile_score, quality_score),
    )


def recompute_family_scores(connection: sqlite3.Connection) -> None:
    """人物が紐づいた顔から family_score を計算し直す。

    手動割当は確信度100として扱う。
    """
    cursor = connection.cursor()
    cursor.execute("UPDATE AnalysisResult SET family_score = 0.0")
    cursor.execute(
        "INSERT INTO AnalysisResult (media_id, family_score)"
        " SELECT media_id, MAX(COALESCE(assign_score, 100.0)) FROM Face"
        " WHERE person_id IS NOT NULL GROUP BY media_id"
        " ON CONFLICT(media_id) DO UPDATE SET family_score = excluded.family_score"
    )
    connection.commit()


def get_analysis_result(connection: sqlite3.Connection, media_id: int) -> Optional[Dict[str, Any]]:
    row = connection.execute(
        "SELECT * FROM AnalysisResult WHERE media_id = ?", (media_id,)
    ).fetchone()
    return _row_to_dict(row)
