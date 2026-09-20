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
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
)

import numpy as np

SCHEMA_VERSION = 4

EMBEDDING_DIM = 128
EMBEDDING_DTYPE = np.float32

ASSIGN_MANUAL = "manual"
ASSIGN_AUTO = "auto"
ASSIGN_REJECTED = "rejected"

#: 「日付として読める撮影日時」だけを取り出す式。読めなければ NULL。
#:
#: **壊れた EXIF を「いちばん新しい」として先頭に出さないため。** カメラが
#: `TTTT-TT-TTTTT:TT:TT` を書くことがあり（実データで Media 67件・顔123件）、
#: 文字の大小で並べると `T` は数字より大きいので、**新しい順の1ページ目を
#: まるごと占領する。** `0000-00-00` も同じ理由で外す（こちらは最後に来る）。
#:
#: `gui._format_timestamp` が画面で行う判断と同じもの。**片方だけ直さない。**
#: 並びと表示で「読める」の意味がずれると、日時が出ていない写真が日付の
#: あるところに紛れる。
#:
#: **索引もこの式で張る**（式インデックス）。`ORDER BY` に同じ式を書けば
#: 索引を順に歩ける。文字列を2か所に書くと綴りがずれて索引が使われなくなるので、
#: 必ずこの定数を使うこと。
SHOOTING_DATE_SORT_KEY = (
    "CASE WHEN shooting_date GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]*'"
    " AND shooting_date NOT LIKE '0000%' THEN shooting_date END"
)

def folder_expression(column: str = "path") -> str:
    """``Media.path`` から、それを収めているフォルダを取り出す SQL 式。

    SQLite に ``dirname`` が無いので ``rtrim`` で作る。``replace(path,'/','')``
    は「そのパスに出てくるスラッシュ以外の文字の集合」なので、``rtrim`` は
    **最後の ``/`` の手前で必ず止まる。** 末尾の ``/`` は ``substr`` で落とす。

    ``/a/b/c.jpg`` → ``/a/b`` ／ ``c.jpg`` → ``""``（フォルダ無し）。

    **索引もこの式で張る**（`idx_media_folder`）。`SHOOTING_DATE_SORT_KEY` と
    同じ理由で、**式を書き写さずに必ずこの関数を通すこと。** 綴りが1文字でも
    ずれると索引が使われなくなる。

    **同じ判断が `folder_of` にもある。** 表示と比較は Python 側で、絞り込みは
    SQL 側で要るため。**片方だけ直すと絞り込みが黙って外れる**ので、
    両者が一致することをテストで固定してある。
    """
    trimmed = f"rtrim({column}, replace({column}, '/', ''))"
    return f"substr({trimmed}, 1, length({trimmed}) - 1)"


def folder_of(path: str) -> str:
    """``folder_expression`` の Python 版。**同じ答えを返すこと。**"""
    head, separator, _ = path.rpartition("/")
    return head if separator else ""


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
    "memo TEXT,"
    # 生年月日 YYYY-MM-DD。未設定は NULL。撮影時の年齢の計算に使う。
    "birth_date TEXT,"
    # 一覧に並べる順。**利用者が画面で入れ替えた順序**を持つ。
    # 未設定(NULL)は名前順の位置に置く（並べ替えたことのない人物）。
    "display_order INTEGER"
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
    # 撮影日時の新しい順に顔を並べるため（§10.4）。**この索引が無いと、ページを
    # 送るたびに未割当の顔を全件並べ直す**（実データ 58,547 件で 220〜435ms）。
    f"CREATE INDEX IF NOT EXISTS idx_media_shooting ON Media({SHOOTING_DATE_SORT_KEY} DESC, id)",
    # フォルダで顔を絞るため（§10.4）。**無いと、ページを送るたびに Media を
    # 全件走査する**（実データ 70,297 件で 1ページ 0.407秒 → 0.017秒）。
    # **列を足す移行は要らない。** `create_tables` が開くたびに
    # `CREATE INDEX IF NOT EXISTS` を流すので、既存DBにもその場で張られる
    # （実データで 2.1 秒・+5MB。`PRAGMA user_version` は 4 のまま）。
    f"CREATE INDEX IF NOT EXISTS idx_media_folder ON Media({folder_expression()})",
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


def expected_columns() -> Dict[str, set]:
    """現行スキーマが持つ列を、``SCHEMA`` から実際に作って調べる。

    列の一覧をここに書き写すと、**スキーマを変えたときに片方だけ古くなる。**
    空のデータベースに一度作って読み取れば、正本は ``SCHEMA`` のままになる。
    """
    probe = sqlite3.connect(":memory:")
    try:
        for statement in SCHEMA:
            probe.execute(statement)
        return {
            table: {row[1] for row in probe.execute(f"PRAGMA table_info({table})")}
            for table in KNOWN_TABLES
            if probe.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()[0]
        }
    finally:
        probe.close()


def missing_columns(connection: sqlite3.Connection) -> Dict[str, List[str]]:
    """現行スキーマが要求する列のうち、このDBに**実際に無い**もの。

    **版の数字を信じない。** ``CREATE TABLE IF NOT EXISTS`` は既存のテーブルを
    変えないので、列が足りないまま版だけ進んだデータベースがありうる
    （実際に起きた。`create_tables` が移行していないDBに版を刻んでいた）。
    数字ではなく形を見れば、その状態を検出して直せる。
    """
    gaps: Dict[str, List[str]] = {}
    for table, columns in expected_columns().items():
        present = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if not present:
            # まだ無いテーブルは `create_tables` が作る。欠けとは数えない。
            continue
        missing = sorted(columns - present)
        if missing:
            gaps[table] = missing
    return gaps


def describe_missing_columns(gaps: Dict[str, List[str]]) -> str:
    """欠けている列を `Person.birth_date` の形で並べる。"""
    return ", ".join(
        f"{table}.{column}" for table, columns in sorted(gaps.items()) for column in columns
    )


#: `ALTER TABLE ... ADD COLUMN` で足せる列と、その型。
#:
#: **移行のたびにコードを足さないため。** 版を上げて列を1本増やすたびに
#: `migration.py` へ `if "..." not in columns` を書き足していくと、
#: 足し忘れが「版だけ進んで列が無い」状態を生む（それ自体が #48 の事故）。
#: ここに1行足せば、移行は `db.missing_columns` が見つけたものを埋める。
#:
#: **既存行に入るのは NULL** なので、NOT NULL の列はここへ書けない。
#: 既定値が要るなら、読み出し側で NULL を解釈すること。
ADDABLE_COLUMNS = {
    ("Person", "birth_date"): "TEXT",
    ("Person", "display_order"): "INTEGER",
}


MIGRATE_HINT = "`photoarchive migrate --db <データベース>` を実行してください。"


def create_tables(connection: sqlite3.Connection) -> None:
    """最新スキーマを用意する。旧スキーマのDBは移行を促して中断する。

    **すでにテーブルがあるDBには、版を刻まない。** ``SCHEMA`` は
    ``CREATE TABLE IF NOT EXISTS`` なので既存のテーブルを変えず、それでいて
    最後に ``PRAGMA user_version`` を書くと、**中身が古いまま「移行済み」の
    印だけが付く。** そうなると `migrate` が「すでに最新です」と言って
    何もしなくなり、**欠けた列は二度と足されない**（実データで発生）。
    """
    version = get_schema_version(connection)
    if version > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"データベースのスキーマ({version})がこのアプリケーション"
            f"({SCHEMA_VERSION})より新しいため開けません。"
        )
    if has_any_table(connection):
        if version < SCHEMA_VERSION:
            raise SchemaVersionError(
                f"データベースのスキーマ({version})が古い形式です"
                f"（このアプリケーションは {SCHEMA_VERSION}）。{MIGRATE_HINT}"
            )
        gaps = missing_columns(connection)
        if gaps:
            raise SchemaVersionError(
                f"データベースの版は {version} ですが、実際の形が追いついていません"
                f"（欠けている列: {describe_missing_columns(gaps)}）。{MIGRATE_HINT}"
            )
    # ここまで来たDBだけが、作成と版の記録を受けてよい。**足りないテーブルは
    # 作る**（移行の途中でテーブルを組み直す経路が、ここで `Face` を作る）。
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


class _KeepBirthDate:
    """``update_person`` の ``birth_date`` 既定値。「誕生日は触らない」を表す。

    ``None`` は「未設定に戻す」という**指示**なので、既定値として使えない。
    区別しないと、**名前だけ直すつもりの呼び出しで誕生日が消える。**
    `KEEP_AGE` とまったく同じ罠（CLAUDE.md §8）。
    """

    def __repr__(self) -> str:  # pragma: no cover - 表示用
        return "KEEP_BIRTH_DATE"


KEEP_BIRTH_DATE = _KeepBirthDate()


def add_person(
    connection: sqlite3.Connection,
    name: str,
    relation: Optional[str] = None,
    memo: Optional[str] = None,
    birth_date: Optional[str] = None,
) -> int:
    """人物を登録する。``birth_date`` は ``YYYY-MM-DD``。未設定は ``None``。"""
    cursor = connection.cursor()
    cursor.execute(
        "INSERT INTO Person (name, relation, memo, birth_date, display_order)"
        " VALUES (?, ?, ?, ?, ?)",
        (name, relation, memo, birth_date, _next_display_order(connection)),
    )
    connection.commit()
    return cursor.lastrowid


def update_person(
    connection: sqlite3.Connection,
    person_id: int,
    name: str,
    relation: Optional[str],
    memo: Optional[str],
    birth_date: Any = KEEP_BIRTH_DATE,
) -> None:
    """人物を更新する。

    ``birth_date`` を**省くと触らない**。``None`` は「未設定へ戻す」指示。
    既定を ``None`` にしていたため、**名前だけ直すつもりの呼び出しで
    登録済みの誕生日が消えていた**（`assign_faces` の ``age`` と同じ罠）。
    """
    if birth_date is KEEP_BIRTH_DATE:
        connection.execute(
            "UPDATE Person SET name = ?, relation = ?, memo = ? WHERE id = ?",
            (name, relation, memo, person_id),
        )
    else:
        connection.execute(
            "UPDATE Person SET name = ?, relation = ?, memo = ?, birth_date = ? WHERE id = ?",
            (name, relation, memo, birth_date, person_id),
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
    """人物を、画面に並べる順で返す。

    **利用者が入れ替えた順（`display_order`）が先。** 並べ替えたことのない
    人物（NULL）は、そのあとに名前順で続く。名前順だけだと、よく使う人物を
    上に置けない。
    """
    rows = connection.execute(
        "SELECT * FROM Person"
        " ORDER BY display_order IS NULL ASC, display_order ASC, name ASC"
    ).fetchall()
    return [dict(row) for row in rows]


def set_person_order(connection: sqlite3.Connection, person_ids: Sequence[int]) -> None:
    """渡された並びを `display_order` に書く。**渡された順がそのまま画面の順。**

    一覧に出ている人物**全員**を渡すこと。一部だけ渡すと、渡さなかった人物の
    `display_order` が古いままになり、並びが混ざる。
    """
    connection.executemany(
        "UPDATE Person SET display_order = ? WHERE id = ?",
        [(order, person_id) for order, person_id in enumerate(person_ids)],
    )
    connection.commit()


def _next_display_order(connection: sqlite3.Connection) -> int:
    """新しい人物を**末尾**に置くための順序値。

    先頭に入れると、利用者が並べ替えた結果を勝手に崩すことになる。

    **`display_order` が NULL の行が残っていると、末尾にならない。**
    `list_persons` は値を持つ行を先に出すので、`MAX` が NULL のときに 0 を
    返すと、**その人物だけが全員より前に出る**（`migrate` 直後のDBがこの状態。
    実データもそうだった）。**足す前に、いま画面に出ている順をそのまま
    書き戻す。** 見た目は変わらないまま、全員が値を持つ状態になる。
    """
    unordered = connection.execute(
        "SELECT COUNT(*) FROM Person WHERE display_order IS NULL"
    ).fetchone()[0]
    if unordered:
        set_person_order(connection, [person["id"] for person in list_persons(connection)])
    row = connection.execute("SELECT MAX(display_order) FROM Person").fetchone()
    return 0 if row[0] is None else int(row[0]) + 1


# ---------------------------------------------------------------------------
# Face
# ---------------------------------------------------------------------------

#: `list_faces` の並び順。**画面ごとに見たい順序が違う。**
#: 既定を変えないこと（`match` と `evaluate` もこの関数を通る）。
ORDER_QUALITY = "quality"
ORDER_SHOT_DESC = "shooting_desc"
ORDER_AGE = "age"

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
    folder: Optional[str] = None,
) -> int:
    """``list_faces`` と同じ条件での件数。ページャの総数に使う。"""
    where, params = _face_filter(
        assign_source, person_id, unassigned, min_age, max_age, folder=folder
    )
    row = connection.execute(f"SELECT COUNT(*) FROM Face{where}", params).fetchone()
    return int(row[0])


def face_ids(
    connection: sqlite3.Connection,
    assign_source: Optional[str] = None,
    person_id: Optional[int] = None,
    unassigned: bool = False,
    min_age: Optional[int] = None,
    max_age: Optional[int] = None,
    folder: Optional[str] = None,
) -> List[int]:
    """``list_faces`` と同じ条件に当たる顔の id を**全件**返す。

    **まとめて処理する操作のためにある。** `list_faces` はページ単位で読むので、
    「このフォルダの未割当をすべて除外」のように**ページをまたぐ操作**には使えない。
    ここは id だけを読むのでサムネイルの BLOB を持ち上げない
    （実データで最大のフォルダが 1,357 件）。
    """
    where, params = _face_filter(
        assign_source, person_id, unassigned, min_age, max_age, folder=folder
    )
    rows = connection.execute(f"SELECT id FROM Face{where} ORDER BY id", params).fetchall()
    return [int(row[0]) for row in rows]


def folder_face_counts(connection: sqlite3.Connection) -> List[Dict[str, Any]]:
    """フォルダごとの顔の件数を、**未割当の多い順**に返す。

    `{"folder", "unassigned", "manual", "rejected", "total"}` を持つ。

    **手本（`'manual'`）の件数も返す。** 結婚式や学校行事のフォルダは
    「ほぼ他人」であって「全部他人」ではなく、家族も写っている。
    まとめて除外する前に、そこに手本があるかどうかが見えないと押せない。

    実データ（Media 70,297 / Face 58,606）で 1,042 フォルダ・0.27 秒。
    """
    folder = folder_expression("m.path")
    rows = connection.execute(
        f"SELECT {folder} AS folder,"
        " SUM(CASE WHEN f.assign_source IS NULL THEN 1 ELSE 0 END) AS unassigned,"
        " SUM(CASE WHEN f.assign_source = ? THEN 1 ELSE 0 END) AS manual,"
        " SUM(CASE WHEN f.assign_source = ? THEN 1 ELSE 0 END) AS rejected,"
        " COUNT(*) AS total"
        " FROM Media m JOIN Face f ON f.media_id = m.id"
        " GROUP BY folder ORDER BY unassigned DESC, folder ASC",
        (ASSIGN_MANUAL, ASSIGN_REJECTED),
    ).fetchall()
    return [dict(row) for row in rows]


def _face_filter(
    assign_source: Optional[str],
    person_id: Optional[int],
    unassigned: bool,
    min_age: Optional[int] = None,
    max_age: Optional[int] = None,
    prefix: str = "",
    folder: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """顔の絞り込み条件。``list_faces`` と ``count_faces`` で同じものを使う。

    年齢の未設定(NULL)は、範囲を指定しても常に残す。年齢を入れていない顔が
    一覧から消えてしまうと、そもそも年齢を入れられなくなるため。

    ``prefix`` は `Media` と結合するときの別名（``"f."``）。**条件を2通り
    書き分けない。** 書き分けると、片方にだけ絞り込みが足される。

    ``folder`` は `Media.path` を収めているフォルダ（`folder_expression`）。
    **結合ではなく副問い合わせで書く。** `list_faces` には `Media` と結合する
    経路（撮影日時順）としない経路があり、結合で書くと**条件が2通りに割れる。**
    """
    clauses: List[str] = []
    params: List[Any] = []
    if unassigned:
        clauses.append(f"{prefix}assign_source IS NULL")
    elif assign_source is not None:
        clauses.append(f"{prefix}assign_source = ?")
        params.append(assign_source)
    if person_id is not None:
        clauses.append(f"{prefix}person_id = ?")
        params.append(person_id)
    if min_age is not None:
        clauses.append(f"({prefix}age IS NULL OR {prefix}age >= ?)")
        params.append(min_age)
    if max_age is not None:
        clauses.append(f"({prefix}age IS NULL OR {prefix}age <= ?)")
        params.append(max_age)
    if folder is not None:
        clauses.append(
            f"{prefix}media_id IN"
            f" (SELECT id FROM Media WHERE {folder_expression('path')} = ?)"
        )
        params.append(folder)
    if not clauses:
        return "", params
    return " WHERE " + " AND ".join(clauses), params


def shooting_dates_for_faces(
    connection: sqlite3.Connection, face_ids: Sequence[int]
) -> List[Optional[str]]:
    """選んだ顔**ごと**の撮影日時を、昇順で返す。**顔1件につき1件返す。**

    **年齢をまとめて入れるときに、撮影日時がまたがっていないかを見るため。**

    **`DISTINCT` で潰さない。撮影日時の無い顔を落とさない。** 潰すと
    「撮影日時の分からない顔が混ざっている」ことが呼び出し側から消え、
    **その顔にも別の写真から計算した年齢が黙って入る**（実データでは
    `Media.shooting_date` が 15.8% 欠けている）。分からないことは
    ``None`` として伝え、捨てるかどうかは呼び出し側が決める。

    読める日付かどうかはここでは判定しない（`0000-00-00` のような壊れた値も
    そのまま返す）。**判断を SQL と Python に割らない**ためで、
    `gui.parse_date` の1か所に持たせてある。
    """
    if not face_ids:
        return []
    placeholders = ",".join("?" for _ in face_ids)
    rows = connection.execute(
        "SELECT m.shooting_date FROM Face f JOIN Media m ON m.id = f.media_id"
        f" WHERE f.id IN ({placeholders})"
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
    order: str = ORDER_QUALITY,
    folder: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """顔を一覧する。

    サムネイルBLOBは件数が増えると重いので、``with_thumbnail`` を指定した
    ときだけ読み出す。

    ``order`` で並びを選ぶ。**既定は品質スコアの高い順**（`match` と
    `evaluate` もこの関数を通るので、既定を変えない）。

    - ``ORDER_QUALITY``: 品質スコアの高い順
    - ``ORDER_SHOT_DESC``: 撮影日時の新しい順。**撮影日時の無い顔は最後**
    - ``ORDER_AGE``: 年齢の若い順。**年齢が未設定の顔は最後**

    ``folder`` を渡すと、そのフォルダに置かれた写真の顔だけに絞る
    （`folder_expression`）。渡さなければ問い合わせは従来と変わらない。
    """
    columns = list(FACE_LIST_COLUMNS)
    if with_thumbnail:
        columns.append("thumbnail")

    if order == ORDER_SHOT_DESC:
        query, params = _shooting_date_query(
            columns, assign_source, person_id, unassigned, min_age, max_age, folder
        )
    else:
        where, params = _face_filter(
            assign_source, person_id, unassigned, min_age, max_age, folder=folder
        )
        if order == ORDER_AGE:
            # **未設定を最後に置く。** SQLite の NULL は最小なので、
            # そのまま昇順にすると年齢を入れていない顔が先頭を埋める。
            order_by = "age IS NULL ASC, age ASC, id ASC"
        else:
            order_by = "quality_score DESC, id ASC"
        query = f"SELECT {','.join(columns)} FROM Face{where} ORDER BY {order_by}"

    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        params = params + [limit, offset]
    rows = connection.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def _shooting_date_query(
    columns: List[str],
    assign_source: Optional[str],
    person_id: Optional[int],
    unassigned: bool,
    min_age: Optional[int],
    max_age: Optional[int],
    folder: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """撮影日時の新しい順に並べる問い合わせ。

    **`CROSS JOIN` は結合の順序を固定するためのもの**で、直積を作るわけでは
    ない。SQLite は左の表を外側に固定するので、`idx_media_shooting` を
    順に歩いて `LIMIT` の分だけ取れる。

    **外すと、ページを送るたびに未割当の顔を全件並べ直す**（実データ
    58,547 件で 220〜435ms。固定すると1ページ目 0.6ms）。`EXPLAIN QUERY PLAN`
    に `USE TEMP B-TREE FOR ORDER BY` が出たら、その状態に戻っている。

    **``folder`` を指定したときだけは、この索引を捨てて並べ替える**
    （`USE TEMP B-TREE FOR ORDER BY` が出る）。対象が `idx_media_folder` で
    1フォルダ（実データの最大で 1,357 件）に絞られたあとの並べ替えなので、
    実測 0.017秒で収まる（指定なしは 0.002秒）。
    """
    where, params = _face_filter(
        assign_source, person_id, unassigned, min_age, max_age, prefix="f.", folder=folder
    )
    selected = ",".join(f"f.{column}" for column in columns)
    sort_key = SHOOTING_DATE_SORT_KEY.replace("shooting_date", "m.shooting_date")
    query = (
        f"SELECT {selected} FROM Media m CROSS JOIN Face f ON f.media_id = m.id"
        f"{where} ORDER BY {sort_key} DESC, f.id ASC"
    )
    return query, params


def get_face(connection: sqlite3.Connection, face_id: int) -> Optional[Dict[str, Any]]:
    row = connection.execute("SELECT * FROM Face WHERE id = ?", (face_id,)).fetchone()
    return _row_to_dict(row)


#: 進み具合を知らせるときの単位。**小さすぎると通知そのものが重い。**
PROGRESS_CHUNK = 50

#: 進み具合の通知口。``(終わった件数, 全体の件数)`` を受け取る。
ProgressCallback = Optional[Callable[[int, int], None]]


def _executemany_with_progress(
    cursor: sqlite3.Cursor,
    statement: str,
    rows: List[tuple],
    progress: ProgressCallback,
) -> int:
    """``executemany`` を、進み具合を知らせながら流す。**触れた行数を返す。**

    **1つのトランザクションのまま分ける。** 分割してコミットすると、
    途中で失敗したときに半分だけ適用された状態が残る。ここで分けるのは
    **知らせる回数**だけで、確定するのは呼び出し側の1回の ``commit``。

    **件数は塊ごとに足す。** ``cursor.rowcount`` は**最後の
    ``executemany`` のぶんしか持たない**ので、呼び出し側がそれを返すと
    「割り当てた件数」が最大でも `PROGRESS_CHUNK` になってしまう。
    """
    total = len(rows)
    if progress is None:
        cursor.executemany(statement, rows)
        return cursor.rowcount
    affected = 0
    progress(0, total)
    for start in range(0, total, PROGRESS_CHUNK):
        cursor.executemany(statement, rows[start : start + PROGRESS_CHUNK])
        affected += cursor.rowcount
        progress(min(start + PROGRESS_CHUNK, total), total)
    return affected


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
    progress: ProgressCallback = None,
) -> int:
    """顔を人物へ割り当てる。

    ``age`` を省くと年齢は触らない。``None`` を明示すると未設定へ戻す。
    ``progress`` を渡すと ``(終わった件数, 全体の件数)`` を知らせる。
    """
    if not face_ids:
        return 0
    now = _utc_now()
    cursor = connection.cursor()
    if isinstance(age, _KeepAge):
        statement = (
            "UPDATE Face SET person_id = ?, assign_source = ?, assign_score = ?,"
            " assigned_at = ? WHERE id = ?"
        )
        rows = [(person_id, assign_source, assign_score, now, face_id) for face_id in face_ids]
    else:
        statement = (
            "UPDATE Face SET person_id = ?, assign_source = ?, assign_score = ?,"
            " assigned_at = ?, age = ? WHERE id = ?"
        )
        rows = [
            (person_id, assign_source, assign_score, now, age, face_id) for face_id in face_ids
        ]
    affected = _executemany_with_progress(cursor, statement, rows, progress)
    connection.commit()
    return affected


def unassign_faces(
    connection: sqlite3.Connection,
    face_ids: Sequence[int],
    progress: ProgressCallback = None,
) -> int:
    if not face_ids:
        return 0
    cursor = connection.cursor()
    affected = _executemany_with_progress(
        cursor,
        "UPDATE Face SET person_id = NULL, assign_source = NULL, assign_score = NULL,"
        " assigned_at = NULL WHERE id = ?",
        [(face_id,) for face_id in face_ids],
        progress,
    )
    connection.commit()
    return affected


def reject_faces(
    connection: sqlite3.Connection,
    face_ids: Sequence[int],
    progress: ProgressCallback = None,
) -> int:
    """「誰でもない顔」として、未割当一覧からも自動紐づけからも外す。"""
    if not face_ids:
        return 0
    now = _utc_now()
    cursor = connection.cursor()
    affected = _executemany_with_progress(
        cursor,
        "UPDATE Face SET person_id = NULL, assign_source = ?, assign_score = NULL,"
        " assigned_at = ? WHERE id = ?",
        [(ASSIGN_REJECTED, now, face_id) for face_id in face_ids],
        progress,
    )
    connection.commit()
    return affected


def set_face_age(connection: sqlite3.Connection, face_id: int, age: Optional[int]) -> None:
    connection.execute("UPDATE Face SET age = ? WHERE id = ?", (age, face_id))
    connection.commit()


def set_faces_age(
    connection: sqlite3.Connection,
    face_ids: Sequence[int],
    age: Optional[int],
    progress: ProgressCallback = None,
) -> int:
    """選んだ顔にまとめて年齢を入れる。``None`` は未設定へ戻す指示。

    **1件ずつ `set_face_age` を呼ばない。** 呼ぶたびにコミットするので、
    200件なら 200 回の書き込み確定になる。ここは1回で確定する。
    """
    if not face_ids:
        return 0
    cursor = connection.cursor()
    affected = _executemany_with_progress(
        cursor,
        "UPDATE Face SET age = ? WHERE id = ?",
        [(age, face_id) for face_id in face_ids],
        progress,
    )
    connection.commit()
    return affected


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
