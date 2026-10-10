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

# **別名で読む。** この module には `embedding` という名前の引数を取る関数が
# いくつもあり（`encode_embedding` / `add_face`）、同名だと関数の中で
# module が見えなくなる。将来そこでモデルの記述を使おうとして踏む。
from . import embedding as embedding_model
from .dates import Taken, age_at, calculate_age, folder_date_range, parse_date, taken_at

SCHEMA_VERSION = 8

#: 特徴量の次元数。**書き写さない。** いま使うモデルの記述から引く
#: （`embedding_model.ACTIVE`）。モデルを替えると変わる（dlib 128 / ArcFace 512）。
EMBEDDING_DIM = embedding_model.ACTIVE.dimensions
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

    **索引もこの式で張る**（`idx_media_event` の先頭列）。`SHOOTING_DATE_SORT_KEY` と
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


def day_expression(column: str = "shooting_date") -> str:
    """撮影日時の**日付の部分**（``YYYY-MM-DD``）を取り出す式。読めなければ NULL。

    **行事（フォルダ×日）の「日」。** 読めるかどうかの判断は
    `SHOOTING_DATE_SORT_KEY` に1つだけあり、ここはそれを切るだけ。
    **`substr(shooting_date, 1, 10)` と書かないこと** — `TTTT-TT-TTTTT:TT:TT`
    のような壊れた値は**10文字あるので長さでは弾けず**、「2026-10-04」の隣に
    「TTTT-TT-TT」という行事が並ぶ。
    """
    return f"substr({SHOOTING_DATE_SORT_KEY.replace('shooting_date', column)}, 1, 10)"


def month_expression(column: str = "shooting_date") -> str:
    """撮影日時の**年月**（``YYYY-MM``）を取り出す式。読めなければ NULL。

    `day_expression` と同じく、読めるかどうかの判断は
    `SHOOTING_DATE_SORT_KEY` に1つだけある。**`substr(shooting_date, 1, 7)` と
    書かないこと** — `TTTT-TT-TTTTT:TT:TT` のような壊れた値は長さでは弾けず、
    「2026-10」の隣に「TTTT-TT」という月が並ぶ。
    """
    return f"substr({SHOOTING_DATE_SORT_KEY.replace('shooting_date', column)}, 1, 7)"


class _Undated:
    """「撮影日時が読めない顔だけ」を表す印。``day`` に渡す。

    **``day=None`` は「日で絞らない」という意味**で、「日付が読めない顔」とは
    別の指示。同じ値で表すと、片方を頼んだつもりでもう片方が起きる
    （`KEEP_AGE` / `KEEP_BIRTH_DATE` と同じ理由。CLAUDE.md §8）。
    """

    def __repr__(self) -> str:  # pragma: no cover - 表示用
        return "UNDATED"


#: 日付の読めない顔だけに絞る印（`day` 引数へ）。
UNDATED = _Undated()


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
    "detector_version TEXT,"
    # **フォルダ名から起こした撮影時期**（v6・#65）。`YYYY-MM-DD` の区間で、
    # 両端を含む。起こせなければ NULL。**`shooting_date` には書き戻さない**
    # （EXIF 由来と推測を混ぜると、どちらなのか二度と分からなくなる）。
    # 読み方は `dates.folder_date_range` に1つだけある。ここはその出力の保存先で、
    # `save_media` と `refresh_folder_dates` が書く。
    "folder_date_from TEXT,"
    "folder_date_to TEXT,"
    # **写真の見た目の値**（v8・#86）。連写・似た写真を束ねるのに使う。
    # `similar.encode` の形（`dhash8/exif:0123456789abcdef`）。NULL=未計測。
    # `select` が候補の写真だけを測って書く。**ファイルのハッシュが変わったら消す**
    # （`save_media`）。
    "look_hash TEXT"
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
    # **顔の見え方**（v5。保存済みサムネイルから測る。`appearance.py`）。
    # NULL＝未計測。`match` と `select` が使う顔だけを、使う前に埋める。
    # aligned: 5点整列ができたか（1/0）。**できない顔の特徴量は整列されていない**
    # （`face.align_for_arcface` は縮小で通す）ので、手本の根拠にしない。
    "aligned INTEGER,"
    # yaw: 横向きの度合い（鼻のずれ ÷ 両目の間隔。正面で 0）。整列できなければ NULL。
    "yaw REAL,"
    # sharpness: 鮮明さ（112px にそろえたラプラシアン分散。小さいほどボケ）。
    "sharpness REAL,"
    # assign_rule: 自動割り当てを付けた規則の版（`matcher.MATCH_RULE`）。
    # **auto の行だけが意味を持つ。** 規則を変えたあとに古い判定が残っているかを
    # `select` が見分けるため。
    "assign_rule TEXT,"
    "FOREIGN KEY(media_id) REFERENCES Media(id) ON DELETE CASCADE,"
    "FOREIGN KEY(person_id) REFERENCES Person(id) ON DELETE SET NULL"
    ")",
    # **「この顔はこの人物ではない」という否定の記録。**
    #
    # `Face.assign_source='rejected'` は「**誰でもない顔**」で、どの人物にも
    # 二度と自動で付かなくなる。それとは別に「**この人物ではない**（ほかの人
    # かもしれない）」が要る。兄弟の赤ん坊の顔は互いによく似ており、
    # **解除（未割当へ戻す）だけでは `match` を流すたびに同じ誤りが戻る**
    # （実データで${PERSON_4}の 1,785 件で起きた）。
    #
    # 1つの顔が複数の人物を否定できるので (face_id, person_id) の対で持つ。
    "CREATE TABLE IF NOT EXISTS FaceRejection ("
    "face_id INTEGER NOT NULL,"
    "person_id INTEGER NOT NULL,"
    "created_at TEXT NOT NULL,"
    "PRIMARY KEY (face_id, person_id),"
    "FOREIGN KEY (face_id) REFERENCES Face(id) ON DELETE CASCADE,"
    "FOREIGN KEY (person_id) REFERENCES Person(id) ON DELETE CASCADE"
    ")",
    # **走査した root**（v7・#24）。`scan` が root を1つ走査し終えるたびに書く。
    #
    # 設定ファイルは git 管理外で、2026-10-02 に失ったとき **DB から root を戻せなかった。**
    # `Media.path` の共通接頭辞で推定すると親（`${NFS_ROOT}`）になり、
    # そのまま走査すると他家の写真まで入る。**root は推定せず、走査した事実だけを書く。**
    # 既存の root の内側（年フォルダだけの `--source`）は書かない（`record_scan_root`）。
    "CREATE TABLE IF NOT EXISTS ScanRoot ("
    "path TEXT PRIMARY KEY,"
    "last_scanned_at TEXT NOT NULL"
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
    # 行事（フォルダ×日）で顔を絞るため（§10.4）。**無いと、ページを送るたびに
    # Media を全件走査する**（実データ 70,297 件で 1ページ 0.407秒 → 0.014秒）。
    # **フォルダだけの索引は別に張らない。** 複合索引の先頭列がフォルダなので、
    # フォルダだけの絞り込みもこれで足りる（2本張ると、どちらが使われているのか
    # 分からなくなるうえに実データがそのぶん大きくなる）。
    # **列を足す移行は要らない。** `create_tables` が開くたびに
    # `CREATE INDEX IF NOT EXISTS` を流すので、既存DBにもその場で張られる
    # （実データで 2.5 秒・488MB → 517MB。`PRAGMA user_version` は 4 のまま）。
    f"CREATE INDEX IF NOT EXISTS idx_media_event ON Media({folder_expression()}, {day_expression()})",
]

KNOWN_TABLES = ("Media", "Person", "Face", "FaceEmbedding", "AnalysisResult", "ScanRoot")


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


def backup_to(
    source: sqlite3.Connection,
    target_path,
    progress: Optional[Callable[[int, int, int], None]] = None,
) -> Path:
    """``source`` の控えを ``target_path`` に取る。**`sqlite3 .backup` と同じ。**

    **ファイルを複写しない。** このDBは WAL で動いているので、確定した書き込みが
    まだ ``-wal`` のほうにだけ残っていることがある。本体のファイルを `cp` すると
    **その分が控えから黙って抜ける**（`match` の前後の控えを `cp` ではなく
    `.backup` で取っているのはこのため。2026-10-08 の申し送り）。

    ``progress`` は SQLite の通知をそのまま渡す（``status, remaining, total`` ページ）。
    """
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    destination = sqlite3.connect(str(target))
    try:
        source.backup(destination, pages=1024, progress=progress)
    finally:
        destination.close()
    return target


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
    ("Face", "aligned"): "INTEGER",
    ("Face", "yaw"): "REAL",
    ("Face", "sharpness"): "REAL",
    ("Face", "assign_rule"): "TEXT",
    ("Media", "folder_date_from"): "TEXT",
    ("Media", "folder_date_to"): "TEXT",
    ("Media", "look_hash"): "TEXT",
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
    """`EMBEDDING_DIM` 次元の埋め込みを float32 のバイト列にする（ArcFace は512）。"""
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
# パスから決まる列。**呼び出し側からは受け取らず、ここで起こす**（`_folder_dates`）。
_MEDIA_PATH_COLUMNS = (
    "folder_date_from",
    "folder_date_to",
)
# 中身から測った列。**中身が変わったら消す**（`save_media` は受け取らず NULL を書く）。
_MEDIA_CONTENT_COLUMNS = ("look_hash",)
_MEDIA_WRITE_COLUMNS = (
    ("path",)
    + _MEDIA_FILE_COLUMNS
    + _MEDIA_SCAN_COLUMNS
    + _MEDIA_PATH_COLUMNS
    + _MEDIA_CONTENT_COLUMNS
)


def _folder_dates(path: str) -> Tuple[Optional[str], Optional[str]]:
    """パスのフォルダ名から起こした撮影時期を、列に入れる形で返す。"""
    found = folder_date_range(path)
    if found is None:
        return None, None
    return found[0].isoformat(), found[1].isoformat()


def refresh_folder_dates(connection: sqlite3.Connection) -> int:
    """全メディアの `folder_date_from` / `folder_date_to` をパスから起こし直す。**変えた行数を返す。**

    パスしか読まないので **NFS には触れない**（実データ 70,297 件で約1秒・2026-10-10）。
    `migrate` が列を足したあとと、`scan` の終わりに呼ぶ。**読み方
    （`dates.folder_date_range`）を変えたとき、古い区間が残らないようにするため。**
    差分スキャンは変わっていないファイルを書き直さないので、`save_media` だけでは
    入れ替わらない。

    **確定（commit）は呼び出し側に任せる。** 移行は版を刻むまでを1つの取引にして
    いるので、途中で確定するとその順序が崩れる（PR #72 のレビュー指摘5）。
    """
    rows = connection.execute(
        "SELECT id, path, folder_date_from, folder_date_to FROM Media"
    ).fetchall()
    updates = []
    for row in rows:
        fresh = _folder_dates(row[1])
        if fresh != (row[2], row[3]):
            updates.append(fresh + (row[0],))
    if updates:
        connection.executemany(
            "UPDATE Media SET folder_date_from = ?, folder_date_to = ? WHERE id = ?",
            updates,
        )
    return len(updates)


def save_media(connection: sqlite3.Connection, media: Dict[str, Any]) -> int:
    """パスをキーにメディアを登録・更新し、そのidを返す。

    ハッシュが変わっている場合はファイル情報を更新し、顔検出の状態を
    リセットする(``face_count`` を NULL に戻す)。見た目の値(``look_hash``)も
    消す（別の写真の値で連写を束ねないため）。既存の ``Face`` と
    ``AnalysisResult`` は呼び出し側が削除する。

    **ハッシュが同じでもファイル属性は書き戻す。** 中身は同じでも更新時刻
    だけが変わることがあり(コピーや touch)、書き戻さないと差分スキャンが
    毎回「変わったかもしれない」と判断して SHA-256 のために全体を読み直す。
    ハッシュが同じなら再び書き戻されないので、これが恒久的に続く。
    このときは ``face_count`` などの検出状態を触らない。触ると検出済みの
    メディアが未スキャンに戻ってしまう。

    ``folder_date_from`` / ``folder_date_to`` は受け取らず、**パスから起こして書く**
    （`dates.folder_date_range`）。
    """
    media = dict(media)
    media["folder_date_from"], media["folder_date_to"] = _folder_dates(media["path"])
    # 中身から測った値は**受け取らない。** 読み直した行をそのまま渡されても、
    # 古い中身の値を新しい中身に付けない。
    for column in _MEDIA_CONTENT_COLUMNS:
        media[column] = None
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
        columns = (
            _MEDIA_FILE_COLUMNS
            + _MEDIA_SCAN_COLUMNS
            + _MEDIA_PATH_COLUMNS
            + _MEDIA_CONTENT_COLUMNS
        )
    else:
        columns = _MEDIA_FILE_COLUMNS + _MEDIA_PATH_COLUMNS
    assignments = ",".join(f"{column}=?" for column in columns)
    cursor.execute(
        f"UPDATE Media SET {assignments} WHERE path = ?",
        tuple(media.get(column) for column in columns) + (media["path"],),
    )
    connection.commit()
    return row["id"]


def save_look_hashes(connection: sqlite3.Connection, rows: Sequence[Tuple[int, str]]) -> int:
    """``(media_id, 見た目の値)`` を書く（#86）。値の形は `similar.encode`。"""
    if not rows:
        return 0
    cursor = connection.cursor()
    cursor.executemany(
        "UPDATE Media SET look_hash = ? WHERE id = ?", [(value, media_id) for media_id, value in rows]
    )
    connection.commit()
    return cursor.rowcount


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


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def record_scan_root(connection: sqlite3.Connection, root: str) -> bool:
    """走査し終えた root を記録する。記録したら True。

    - **既存の root の内側は書かない。** 年フォルダだけを ``--source`` で流すのは
      root の一部をやり直しただけで、新しい root ではない
    - **記録を消さない。** 既存の root を内側に含む root（親）は ``scan`` が走査の前に止める
      （``scanner.refuse_parents_of_recorded_roots``）。以前はここで内側の記録を消して
      親に置き換えており、間違えて親を1回走査しただけで記録が親だけになり、正しい root で
      走査し直しても戻らなかった（PR #75 のレビュー (a)）。ここまで来たら両方を残す
      （残れば、記録で走査するときに入れ子の検査で止まる）
    """
    root = str(root)
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    recorded = list_scan_roots(connection)
    if root in recorded:
        connection.execute("UPDATE ScanRoot SET last_scanned_at = ? WHERE path = ?", (now, root))
        return True
    if any(_is_within(root, existing) for existing in recorded):
        return False
    connection.execute("INSERT INTO ScanRoot (path, last_scanned_at) VALUES (?, ?)", (root, now))
    return True


def list_scan_roots(connection: sqlite3.Connection) -> List[str]:
    """記録された root。パスの順。"""
    return [row[0] for row in connection.execute("SELECT path FROM ScanRoot ORDER BY path")]


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
        "UPDATE Face SET person_id = NULL, assign_source = NULL, assign_score = NULL, assign_rule = NULL,"
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
#: 自動割り当ての確信度が**低い順**。**誤りに早く当たるための並び。**
#:
#: `assign_score` は距離から作った 0〜100 で、低いほど「似ていないのに
#: 割り当てた」顔。見直しは低いほうから見るのがいちばん効く
#: （実データで自動割り当て 15,467 件。2026-10-08）。
#: **確信度を持たない顔（手本・未割当）は最後**に置く。
ORDER_SCORE_ASC = "score_asc"
#: **上の4つの逆向き**（#69 のコメント「高い順があるなら低い順もあるべき」）。
#: **値を持たない顔は、どちらの向きでも最後。** 逆にしたとたんに撮影日時の
#: 無い顔や確信度の無い手本が先頭に並ぶと、見たいものが2ページ目以降へ押し出される。
ORDER_QUALITY_ASC = "quality_asc"
ORDER_SHOT_ASC = "shooting_asc"
ORDER_AGE_DESC = "age_desc"
ORDER_SCORE_DESC = "score_desc"

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
    day: Any = None,
    birth_date: Optional[str] = None,
    include_unknown_age: bool = True,
    month_from: Optional[str] = None,
    month_to: Optional[str] = None,
    undated_only: bool = False,
    rejected_for_person: Optional[int] = None,
    born_by: Optional[str] = None,
    not_rejected_for_person: Optional[int] = None,
) -> int:
    """``list_faces`` と同じ条件での件数。ページャの総数に使う。"""
    where, params = _face_filter(
        assign_source,
        person_id,
        unassigned,
        min_age,
        max_age,
        folder=folder,
        day=day,
        birth_date=birth_date,
        include_unknown_age=include_unknown_age,
        month_from=month_from,
        month_to=month_to,
        undated_only=undated_only,
        rejected_for_person=rejected_for_person,
        born_by=born_by,
        not_rejected_for_person=not_rejected_for_person,
    )
    row = connection.execute(f"SELECT COUNT(*) FROM Face{where}", params).fetchone()
    return int(row[0])


def face_counts(connection: sqlite3.Connection) -> Dict[str, Any]:
    """画面の左に出す件数を、**1回の問い合わせで**数える。

    返すもの。

    ```
    {"unassigned": 31275, "manual": 10577, "auto": 15467, "rejected": 1287,
     "by_person": {3: {"manual": 3325, "auto": 6208}, ...}}
    ```

    **人物ごとに `count_faces` を呼ばない。** 人数ぶんの問い合わせになり、
    人物を選び直すたびに増える。実データ（顔 58,606 件）では、この
    `GROUP BY` ひとつで足りる。

    **「残りがどれだけあるか」を画面に出すためにある。** 手作業の量が精度の
    上限（他人の顔の 99.1% が家族の写真に混ざる。2026-10-08 実測）なので、
    進み具合が見えること自体が作業の支えになる。
    """
    totals = {None: 0, ASSIGN_MANUAL: 0, ASSIGN_AUTO: 0, ASSIGN_REJECTED: 0}
    by_person: Dict[int, Dict[str, int]] = {}
    rows = connection.execute(
        "SELECT person_id, assign_source, COUNT(*) FROM Face"
        " GROUP BY person_id, assign_source"
    )
    for person_id, source, count in rows:
        count = int(count)
        if source not in totals:
            # 知らない種別が入っていても落とさない（移行の途中など）。
            continue
        totals[source] += count
        if source in (ASSIGN_MANUAL, ASSIGN_AUTO) and person_id is not None:
            entry = by_person.setdefault(int(person_id), {"manual": 0, "auto": 0})
            entry["manual" if source == ASSIGN_MANUAL else "auto"] += count
    return {
        "unassigned": totals[None],
        "manual": totals[ASSIGN_MANUAL],
        "auto": totals[ASSIGN_AUTO],
        "rejected": totals[ASSIGN_REJECTED],
        "by_person": by_person,
    }


def face_ids(
    connection: sqlite3.Connection,
    assign_source: Optional[str] = None,
    person_id: Optional[int] = None,
    unassigned: bool = False,
    min_age: Optional[int] = None,
    max_age: Optional[int] = None,
    folder: Optional[str] = None,
    day: Any = None,
    birth_date: Optional[str] = None,
    include_unknown_age: bool = True,
    month_from: Optional[str] = None,
    month_to: Optional[str] = None,
    undated_only: bool = False,
    rejected_for_person: Optional[int] = None,
    born_by: Optional[str] = None,
    not_rejected_for_person: Optional[int] = None,
) -> List[int]:
    """``list_faces`` と同じ条件に当たる顔の id を**全件**返す。

    **まとめて処理する操作のためにある。** `list_faces` はページ単位で読むので、
    「この行事の未割当をすべて除外」のように**ページをまたぐ操作**には使えない。
    ここは id だけを読むのでサムネイルの BLOB を持ち上げない
    （実データで最大の行事が 1,357 件）。

    **絞り込みの引数は `list_faces` と同じものを全部受け取る。** 画面が渡す
    条件はひと揃いで、**ここだけ受け取れないと「いま見えている一覧」と
    「まとめて処理する対象」がずれる。** 撮影年月を足したときに通し忘れて
    おり、年月で絞った状態で行事の「まとめて…」を押すと `TypeError` で
    落ちていた（#67 で修正。`test_db.py` が引数の揃いを見張る）。
    """
    where, params = _face_filter(
        assign_source,
        person_id,
        unassigned,
        min_age,
        max_age,
        folder=folder,
        day=day,
        birth_date=birth_date,
        include_unknown_age=include_unknown_age,
        month_from=month_from,
        month_to=month_to,
        undated_only=undated_only,
        rejected_for_person=rejected_for_person,
        born_by=born_by,
        not_rejected_for_person=not_rejected_for_person,
    )
    rows = connection.execute(f"SELECT id FROM Face{where} ORDER BY id", params).fetchall()
    return [int(row[0]) for row in rows]


def faces_by_ids(
    connection: sqlite3.Connection,
    face_ids: Sequence[int],
    with_thumbnail: bool = False,
) -> List[Dict[str, Any]]:
    """与えた id の顔を、**渡した並びのまま**返す。

    **サムネイル込みで呼ぶのは1ページ分ずつ。** 全件読むと数百MBになって
    画面が固まる（CLAUDE.md §8）。束の中身を見せるときは呼び出し側が区切る。

    `IN (...)` の変数の数に上限があるので、内部で塊に割って読む。
    """
    if not face_ids:
        return []
    columns = list(FACE_LIST_COLUMNS)
    if with_thumbnail:
        columns.append("thumbnail")
    found: Dict[int, Dict[str, Any]] = {}
    chunk = 500
    for start in range(0, len(face_ids), chunk):
        part = list(face_ids[start : start + chunk])
        placeholders = ",".join("?" for _ in part)
        rows = connection.execute(
            f"SELECT {','.join(columns)} FROM Face WHERE id IN ({placeholders})",
            tuple(part),
        ).fetchall()
        for row in rows:
            record = dict(row)
            found[int(record["id"])] = record
    # **渡した順を守る。** 品質スコアの高い順に渡されるので、束の先頭が
    # 代表の顔になる。SQL の `IN` は並びを保証しない。
    return [found[face_id] for face_id in face_ids if face_id in found]


def available_months(connection: sqlite3.Connection) -> List[str]:
    """顔のある写真の撮影年月（``YYYY-MM``）を新しい順に。読めないものは入らない。

    **顔のある写真だけを数える。** 一覧の絞り込みに使うので、選んでも1件も
    出ない月を並べても仕方がない。
    """
    return [
        row[0]
        for row in connection.execute(
            f"SELECT DISTINCT {month_expression()} AS m FROM Media"
            " WHERE id IN (SELECT DISTINCT media_id FROM Face)"
            " AND m IS NOT NULL ORDER BY m DESC"
        )
    ]


def count_undated_faces(connection: sqlite3.Connection) -> int:
    """撮影日時が読めない写真に写っている顔の件数。"""
    row = connection.execute(
        f"SELECT COUNT(*) FROM Face WHERE media_id IN"
        f" (SELECT id FROM Media WHERE {month_expression()} IS NULL)"
    ).fetchone()
    return int(row[0])


def face_paths(
    connection: sqlite3.Connection, face_ids: Sequence[int]
) -> Dict[int, str]:
    """顔 id から、その顔が写っているファイルのパスを引く。

    **サムネイルも特徴量も読まない。** 要るのは「人がその顔を見に行くための
    手がかり」だけで、`evaluate` が誤りになった顔を名指しするのに使う。
    `faces_by_ids` と分けているのは、あちらが `Face` の列しか返さないため。

    `IN (...)` の変数の数に上限があるので、内部で塊に割って読む。
    見つからない id は結果に入らない（呼び出し側が無い場合を書き分けられる）。
    """
    if not face_ids:
        return {}
    found: Dict[int, str] = {}
    chunk = 500
    for start in range(0, len(face_ids), chunk):
        part = list(face_ids[start : start + chunk])
        placeholders = ",".join("?" for _ in part)
        rows = connection.execute(
            "SELECT f.id AS face_id, m.path AS path FROM Face f"
            f" JOIN Media m ON m.id = f.media_id WHERE f.id IN ({placeholders})",
            tuple(part),
        ).fetchall()
        for row in rows:
            found[int(row["face_id"])] = row["path"]
    return found


def taken_by_face(
    connection: sqlite3.Connection, face_ids: Sequence[int]
) -> Dict[int, Optional[Taken]]:
    """顔 id ごとの撮影時期（`dates.Taken`）を、**id で引ける形**で返す。

    `taken_for_faces` との違いは引けること。あちらは並べた値だけを返すので
    「まとめて年齢を入れる範囲」を見せるのには足りるが、**一覧の1件ずつに
    年齢を出すにはどの顔のものか分からないと使えない。**

    EXIF の撮影日時が読めればその日、読めなければ**フォルダ名から起こした区間**
    （`Media.folder_date_from` / `folder_date_to`・#65）。どちらも無い顔は ``None``。
    **読めるかどうかの判断は `dates.taken_at` に預ける**（CLAUDE.md §8）。
    """
    if not face_ids:
        return {}
    found: Dict[int, Optional[Taken]] = {}
    chunk = 500
    for start in range(0, len(face_ids), chunk):
        part = list(face_ids[start : start + chunk])
        placeholders = ",".join("?" for _ in part)
        rows = connection.execute(
            "SELECT f.id AS face_id, m.shooting_date, m.folder_date_from, m.folder_date_to"
            f" FROM Face f JOIN Media m ON m.id = f.media_id WHERE f.id IN ({placeholders})",
            tuple(part),
        ).fetchall()
        for row in rows:
            found[int(row["face_id"])] = taken_at(
                row["shooting_date"], row["folder_date_from"], row["folder_date_to"]
            )
    return found


def load_faces_for_clustering(
    connection: sqlite3.Connection,
    folder: str,
    day: Any,
) -> List[Dict[str, Any]]:
    """1つの行事を束ねるのに要るぶんだけ読む。**サムネイルは読まない。**

    返すのは `{"id", "media_id", "person_id", "assign_source", "embedding"}` で、
    ``embedding`` は numpy 配列。**品質スコアの高い順**に並べる（束の先頭が
    代表の顔になる）。

    **``day`` に既定値を置かない。** これは「1つの行事を束ねる」関数なので、
    日で絞らない呼び出し（``day=None``）は常に誤り。日付が読めない行事は
    `UNDATED` を渡す。**既定値があったせいで、束ねる画面が `None` をそのまま
    渡し、同じフォルダの別の日の顔まで束に入れていた**（実データで日付つきの
    未割当 18,000 件。PR #62 のレビュー指摘1）。

    絞り方の約束。

    - **いま使うモデルの特徴量を持つ顔だけ**（`embed_version` の完全一致）。
      別の埋め込み空間の距離を比べるのは常に誤り（CLAUDE.md §7）
    - **未割当と手本だけ。** 除外した顔はもう人が判断済みで、束ねても決定は
      減らない。自動割当（`'auto'`）も入れない（`match` の結果を人の判断の
      材料に混ぜると、誤りが次の判断の根拠になる）
    - **手本は入れる。** 束に手本が混ざっていれば「この束はこの人」が分かる
    """
    where, params = _face_filter(None, None, False, None, None, folder=folder, day=day)
    clauses = [where[len(" WHERE ") :]] if where else []
    clauses.append("embedding IS NOT NULL")
    clauses.append("embed_version = ?")
    clauses.append("(assign_source IS NULL OR assign_source = ?)")
    params = list(params) + [embedding_model.ACTIVE.version, ASSIGN_MANUAL]
    rows = connection.execute(
        "SELECT id, media_id, person_id, assign_source, embedding FROM Face"
        f" WHERE {' AND '.join(clauses)}"
        " ORDER BY quality_score DESC, id ASC",
        params,
    ).fetchall()
    records = []
    for row in rows:
        record = dict(row)
        record["embedding"] = decode_embedding(record["embedding"])
        records.append(record)
    return records


def event_face_counts(connection: sqlite3.Connection) -> List[Dict[str, Any]]:
    """**行事（フォルダ×日）ごと**の顔の件数を、未割当の多い順に返す。

    `{"folder", "day", "unassigned", "manual", "rejected", "total"}` を持つ。
    ``day`` は撮影日時が読めない行事では ``None``（**フォルダだけが同じ顔の集まり**。
    実データでは顔 **6,190 件**がここに入る。**この関数は顔を全部数える**ので、
    束ねられる「特徴量あり・未割当」の 5,910 件とは別の数）。

    **フォルダではなく行事で数える。** 同じフォルダでも日をまたぐと同一人物の
    距離が開くため（dlib で 0.480 → 0.578）、束ねる単位をフォルダにすると粗い。
    これが PR #56 を取り下げて作り直した理由。

    **手本（`'manual'`）の件数も返す。** 結婚式や学校行事は「ほぼ他人」であって
    「全部他人」ではなく、家族も写っている。まとめて除外する前に、そこに手本が
    あるかどうかが見えないと押せない。

    実データ（Media 70,297 / Face 58,606）で 2,700 行事・0.4 秒。
    """
    folder = folder_expression("m.path")
    day = day_expression("m.shooting_date")
    rows = connection.execute(
        f"SELECT {folder} AS folder, {day} AS day,"
        " SUM(CASE WHEN f.assign_source IS NULL THEN 1 ELSE 0 END) AS unassigned,"
        " SUM(CASE WHEN f.assign_source = ? THEN 1 ELSE 0 END) AS manual,"
        " SUM(CASE WHEN f.assign_source = ? THEN 1 ELSE 0 END) AS rejected,"
        " COUNT(*) AS total"
        " FROM Media m JOIN Face f ON f.media_id = m.id"
        " GROUP BY folder, day ORDER BY unassigned DESC, day DESC, folder ASC",
        (ASSIGN_MANUAL, ASSIGN_REJECTED),
    ).fetchall()
    return [dict(row) for row in rows]


def _birth_year_shift(birth_date: str, years: int) -> Optional[str]:
    """誕生日を ``years`` 年ずらした日付（``YYYY-MM-DD``）。読めなければ ``None``。

    **年齢の範囲を「撮影日の範囲」に読み替えるために使う。** こうすると
    **SQL 側に年齢の計算を持ち込まずに済む**（日付の判断は `dates.parse_date` の
    1か所にある。CLAUDE.md §8）。

    2月29日生まれで、ずらした先に29日が無い年は**3月1日**にする。`dates.age_at` は
    閏年でない年の2月28日をまだ上がる前と数えるので、**窓の端もそこに合わせる**
    （28日に寄せていたため、2月28日に撮った写真が画面では0歳、絞り込みでは1歳に
    なっていた。PR #72 のレビュー指摘4）。
    """
    base = parse_date(birth_date)
    if base is None:
        return None
    try:
        return base.replace(year=base.year + years).isoformat()
    except ValueError:
        return base.replace(year=base.year + years, month=3, day=1).isoformat()


def _folder_age_known(birth_date: str, params: List[Any]) -> Optional[str]:
    """フォルダ名から起こした区間で、**年齢が1つに決まる**写真の条件（`Media` の列に対して）。

    年齢が決まるのは、区間の途中に誕生日が来ないとき（`dates.age_at` と同じ規則。
    **画面に出る年齢と、絞り込みが見る年齢を一致させる**）。もう1つ、区間の終わりまでに
    生まれていないなら「誕生前」で決まる。

    区間は必ず1つの暦年の中にある（`dates.folder_date_range` の作り）ので、
    その年の誕生日（``YYYY`` ＋ 誕生日の ``-MM-DD``）が区間に入るかを見ればよい。
    **SQL で年齢を計算しない**（日付の判断は `dates` に1つだけ）。文字列で比べるので、
    2月29日生まれは閏年でない年の3月1日に1つ上がる（`dates.age_at` と同じ）。

    誕生日が読めなければ ``None``。
    """
    born = parse_date(birth_date)
    if born is None:
        return None
    anniversary = "(substr(folder_date_from, 1, 4) || ?)"
    month_day = born.isoformat()[4:]
    params.extend([month_day, month_day, born.isoformat()])
    return (
        "(folder_date_from IS NOT NULL AND"
        f" (NOT (folder_date_from < {anniversary} AND {anniversary} <= folder_date_to)"
        " OR folder_date_to < ?))"
    )


def _age_clause(
    prefix: str,
    min_age: Optional[int],
    max_age: Optional[int],
    birth_date: Optional[str],
    include_unknown_age: bool,
    params: List[Any],
) -> Optional[str]:
    """年齢での絞り込み。**画面に出ている年齢と同じものを見る。**

    `Face.age` は人が確かめて入れた値で、**`match` は書かない。** 実データでは
    割り当て済み 22,511 件のうち `Face.age` が入っているのは **126 件だけ**
    （2026-10-07）。そのため `Face.age` だけを見ると、**絞り込みが何もしない**
    のと同じになっていた。

    そこで、画面の表示と同じ規則で見る。

    | その顔の年齢 | 判定 |
    |---|---|
    | `Face.age` が入っている | その値で判定する |
    | 入っていないが誕生日と撮影日時がある | **計算した年齢**で判定する |
    | どちらも無い | ``include_unknown_age`` で決める |

    計算のほうは**撮影日の範囲**に読み替える（`_birth_year_shift`）。
    年齢 ``a`` は「誕生日 + a年 以上、誕生日 + (a+1)年 未満」。

    EXIF が無ければ**フォルダ名から起こした区間**で見る（#65）。区間が年齢の範囲に
    まるごと入り、かつ年齢が1つに決まるときだけ当たる（`_folder_age_known`）。
    区間の途中に誕生日がある写真は年齢を出せないので、「年齢不明」の側に入る。
    """
    if min_age is None and max_age is None:
        return None

    known = [f"{prefix}age IS NOT NULL"]
    if min_age is not None:
        known.append(f"{prefix}age >= ?")
        params.append(min_age)
    if max_age is not None:
        known.append(f"{prefix}age <= ?")
        params.append(max_age)
    branches = ["(" + " AND ".join(known) + ")"]

    day = day_expression()
    if birth_date:
        window = [f"{day} IS NOT NULL"]
        lower = None if min_age is None else _birth_year_shift(birth_date, min_age)
        upper = None if max_age is None else _birth_year_shift(birth_date, max_age + 1)
        if lower is not None:
            window.append(f"{day} >= ?")
            params.append(lower)
        if upper is not None:
            # **上は含めない。** 誕生日の当日に次の年齢へ上がるため。
            window.append(f"{day} < ?")
            params.append(upper)
        branches.append(
            f"({prefix}age IS NULL AND {prefix}media_id IN"
            f" (SELECT id FROM Media WHERE {' AND '.join(window)}))"
        )
        # EXIF が無く、フォルダ名から起こした区間で年齢が決まる顔（#65）。
        known = _folder_age_known(birth_date, params)
        if known is not None:
            guessed = [f"{day} IS NULL", known]
            if lower is not None:
                guessed.append("folder_date_from >= ?")
                params.append(lower)
            if upper is not None:
                guessed.append("folder_date_to < ?")
                params.append(upper)
            branches.append(
                f"({prefix}age IS NULL AND {prefix}media_id IN"
                f" (SELECT id FROM Media WHERE {' AND '.join(guessed)}))"
            )

    if include_unknown_age:
        if birth_date:
            # 誕生日はあるが、撮影時期から年齢が出せない顔（EXIF が読めず、
            # フォルダ名の区間も無いか、区間の途中に誕生日がある）。
            unknown = [f"{day} IS NULL"]
            known = _folder_age_known(birth_date, params)
            if known is not None:
                unknown.append(f"NOT {known}")
            branches.append(
                f"({prefix}age IS NULL AND {prefix}media_id IN"
                f" (SELECT id FROM Media WHERE {' AND '.join(unknown)}))"
            )
        else:
            # 誕生日が無いので、`Face.age` の無い顔は1件も年齢を出せない。
            branches.append(f"{prefix}age IS NULL")

    return "(" + " OR ".join(branches) + ")"


def _face_filter(
    assign_source: Optional[str],
    person_id: Optional[int],
    unassigned: bool,
    min_age: Optional[int] = None,
    max_age: Optional[int] = None,
    prefix: str = "",
    folder: Optional[str] = None,
    day: Any = None,
    birth_date: Optional[str] = None,
    include_unknown_age: bool = True,
    month_from: Optional[str] = None,
    month_to: Optional[str] = None,
    undated_only: bool = False,
    rejected_for_person: Optional[int] = None,
    born_by: Optional[str] = None,
    not_rejected_for_person: Optional[int] = None,
) -> Tuple[str, List[Any]]:
    """顔の絞り込み条件。``list_faces`` と ``count_faces`` で同じものを使う。

    ``month_from`` / ``month_to`` は撮影年月の範囲（``"2015-08"`` 形式・**両端を含む**）。
    片方だけでもよい。``undated_only`` は「**EXIF の撮影日時が読めない顔だけ**」を見る
    指示（フォルダ名から推測した区間がある顔も入る）。**範囲とは別の問いなので
    併用しない。** 推測した区間がまるごと入る顔は、範囲の側にも入る（#65）。

    年齢は `_age_clause` が組み立てる。**`Face.age` だけを見ない** —
    実データでは割り当て済み 22,511 件のうち入っているのは 126 件だけで、
    それだけを見ると絞り込みが何もしないのと同じになる（2026-10-07）。
    ``birth_date`` を渡すと、画面の表示と同じく**計算した年齢**でも絞る。

    ``prefix`` は `Media` と結合するときの別名（``"f."``）。**条件を2通り
    書き分けない。** 書き分けると、片方にだけ絞り込みが足される。

    ``folder`` と ``day`` が行事（`folder_expression` / `day_expression`）。
    **結合ではなく副問い合わせで書く。** `list_faces` には `Media` と結合する
    経路（撮影日時順）としない経路があり、結合で書くと**条件が2通りに割れる。**

    ``day`` は ``None`` が「日で絞らない」、`UNDATED` が「撮影日時が読めない顔
    だけ」。**同じ値で表さない**（CLAUDE.md §8）。

    ``rejected_for_person`` は「**その人物ではない**と記録した顔だけ」
    （`FaceRejection`）。**`person_id` とは別の指示**で、この顔はその人物に
    割り当たっていないので一緒には使わない。**`誰でもない顔` にした顔は外す** —
    あちらは `match` の候補から顔ごと外れるので、否定の記録はもう何の仕事も
    していない（一覧に残すと、`誰でもない顔` を押したのにサムネイルが消えない）。
    **記録そのものは消さない**（消すと、除外を取り消した瞬間に `match` が
    またその人物へ付ける）。

    以前はこの条件だけ専用の関数（`rejected_face_ids_for_person`）で作って
    いたため、**撮影年月・行事・年齢の絞り込みとページャが効かなかった。**
    条件はここに1つだけ持つ。

    ``born_by`` と ``not_rejected_for_person`` は、**人物の画面で未割当の顔を
    見るとき**（#69）のもの。その人物の候補になりえない顔を外す。

    - ``born_by``（誕生日）: **撮影日がそれより前の顔を外す。** 生まれる前の
      写真には写れない（`matcher._persons_alive_at` と同じ事実）。撮影日時が
      読めない顔と、誕生日が読めない場合は外さない（**分からないものを弾かない**）
    - ``not_rejected_for_person``: その人物について「この人物ではない」と
      記録した顔を外す。``rejected_for_person`` の**逆**で、別の指示
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
    if rejected_for_person is not None:
        clauses.append(
            f"{prefix}id IN (SELECT face_id FROM FaceRejection WHERE person_id = ?)"
        )
        params.append(rejected_for_person)
        clauses.append(
            f"({prefix}assign_source IS NULL OR {prefix}assign_source <> ?)"
        )
        params.append(ASSIGN_REJECTED)
    if not_rejected_for_person is not None:
        clauses.append(
            f"{prefix}id NOT IN (SELECT face_id FROM FaceRejection WHERE person_id = ?)"
        )
        params.append(not_rejected_for_person)
    age_clause = _age_clause(
        prefix, min_age, max_age, birth_date, include_unknown_age, params
    )
    if age_clause is not None:
        clauses.append(age_clause)
    media_conditions: List[str] = []
    if folder is not None:
        media_conditions.append(f"{folder_expression('path')} = ?")
        params.append(folder)
    if day is UNDATED:
        media_conditions.append(f"{day_expression()} IS NULL")
    elif day is not None:
        media_conditions.append(f"{day_expression()} = ?")
        params.append(day)
    if undated_only:
        media_conditions.append(f"{month_expression()} IS NULL")
    else:
        # **読めない撮影日時は、EXIF の比較では範囲に入らない。** `month_expression`
        # が NULL を返し、比較の結果も NULL になって落ちる。代わりに
        # **フォルダ名から起こした区間がまるごと範囲に入る**ときだけ当てる
        # （#65。「2012年」までしか分からない写真は、10月だけを見たいときには
        # 出さない）。区間も無い写真を見たいときは `undated_only` で明示する。
        if month_from is not None or month_to is not None:
            exif, guessed = [], [f"{month_expression()} IS NULL", "folder_date_from IS NOT NULL"]
            exif_params: List[Any] = []
            guessed_params: List[Any] = []
            if month_from is not None:
                exif.append(f"{month_expression()} >= ?")
                exif_params.append(month_from)
                guessed.append("substr(folder_date_from, 1, 7) >= ?")
                guessed_params.append(month_from)
            if month_to is not None:
                exif.append(f"{month_expression()} <= ?")
                exif_params.append(month_to)
                guessed.append("substr(folder_date_to, 1, 7) <= ?")
                guessed_params.append(month_to)
            media_conditions.append(
                f"(({' AND '.join(exif)}) OR ({' AND '.join(guessed)}))"
            )
            params.extend(exif_params + guessed_params)
    # **日付の判断は `dates.parse_date` に預ける**（`_birth_year_shift` と同じ。
    # CLAUDE.md §8）。0年ずらすと、読めた誕生日を `YYYY-MM-DD` にそろえるだけになる。
    # EXIF が無ければフォルダ名の区間で見て、**区間の終わりまでに生まれていない**
    # ときだけ外す（#65。区間の途中で生まれたなら分からないので残す）。
    birth = None if not born_by else _birth_year_shift(born_by, 0)
    if birth is not None:
        day = day_expression()
        media_conditions.append(
            f"({day} >= ? OR ({day} IS NULL AND"
            " (folder_date_to IS NULL OR folder_date_to >= ?)))"
        )
        params.extend([birth, birth])
    if media_conditions:
        clauses.append(
            f"{prefix}media_id IN"
            f" (SELECT id FROM Media WHERE {' AND '.join(media_conditions)})"
        )
    if not clauses:
        return "", params
    return " WHERE " + " AND ".join(clauses), params


def taken_for_faces(
    connection: sqlite3.Connection, face_ids: Sequence[int]
) -> List[Optional[Taken]]:
    """選んだ顔**ごと**の撮影時期（`dates.Taken`）を返す。**顔1件につき1件返す。**

    **年齢をまとめて入れるときに、撮影時期がまたがっていないかを見るため。**

    **潰さない。撮影時期の分からない顔を落とさない。** 潰すと
    「撮影時期の分からない顔が混ざっている」ことが呼び出し側から消え、
    **その顔にも別の写真から計算した年齢が黙って入る**。分からないことは
    ``None`` として伝え、捨てるかどうかは呼び出し側が決める。

    並びは早い順で、分からない顔（``None``）は最後。EXIF が読めなければ
    フォルダ名から起こした区間を使う（`taken_by_face` と同じ）。
    """
    found = taken_by_face(connection, face_ids)
    values = [found.get(int(face_id)) for face_id in face_ids]
    return sorted(
        values,
        key=lambda taken: (taken is None, taken.earliest if taken else None, taken.latest if taken else None),
    )


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
    day: Any = None,
    birth_date: Optional[str] = None,
    include_unknown_age: bool = True,
    month_from: Optional[str] = None,
    month_to: Optional[str] = None,
    undated_only: bool = False,
    rejected_for_person: Optional[int] = None,
    born_by: Optional[str] = None,
    not_rejected_for_person: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """顔を一覧する。

    サムネイルBLOBは件数が増えると重いので、``with_thumbnail`` を指定した
    ときだけ読み出す。

    ``order`` で並びを選ぶ。**既定は品質スコアの高い順**（`match` と
    `evaluate` もこの関数を通るので、既定を変えない）。

    - ``ORDER_QUALITY``: 品質スコアの高い順
    - ``ORDER_SHOT_DESC``: 撮影日時の新しい順。**撮影日時の無い顔は最後**
    - ``ORDER_AGE``: 年齢の若い順。**画面に出ている年齢**（確定値か、誕生日と
      撮影日時から計算した値）で並べる。**年齢を出せない顔は最後**
      （`_ids_in_age_order`）
    - ``ORDER_SCORE_ASC``: 自動割り当ての確信度が低い順。**持たない顔は最後**
    - ``ORDER_QUALITY_ASC`` / ``ORDER_SHOT_ASC`` / ``ORDER_AGE_DESC`` /
      ``ORDER_SCORE_DESC``: 上の逆向き。**値を持たない顔はやはり最後**

    「この人物に似た順」はここでは並べない（手本との距離が要る。`recommend`）。

    ``folder`` / ``day`` を渡すと、その行事（フォルダ×日）の写真の顔だけに絞る
    （`_face_filter`）。渡さなければ問い合わせは従来と変わらない。
    """
    columns = list(FACE_LIST_COLUMNS)
    if with_thumbnail:
        columns.append("thumbnail")

    # **絞り込みの条件を組み立てるのはここ1回だけ。** 並び順ごとに
    # 書き分けていたため、引数が増えるたびに片方へ通し忘れる形だった。
    filters = dict(
        assign_source=assign_source,
        person_id=person_id,
        unassigned=unassigned,
        min_age=min_age,
        max_age=max_age,
        folder=folder,
        day=day,
        birth_date=birth_date,
        include_unknown_age=include_unknown_age,
        month_from=month_from,
        month_to=month_to,
        undated_only=undated_only,
        rejected_for_person=rejected_for_person,
        born_by=born_by,
        not_rejected_for_person=not_rejected_for_person,
    )
    if order in (ORDER_AGE, ORDER_AGE_DESC):
        # **年齢順だけは SQL で並べない。** 下の `_ids_in_age_order` を見ること。
        where, params = _face_filter(prefix="f.", **filters)
        ordered = _ids_in_age_order(
            connection, where, params, birth_date, descending=order == ORDER_AGE_DESC
        )
        if limit is not None:
            ordered = ordered[offset : offset + limit]
        return faces_by_ids(connection, ordered, with_thumbnail=with_thumbnail)
    if order in (ORDER_SHOT_DESC, ORDER_SHOT_ASC):
        # 撮影日時順だけは `Media` と結合するので、別名つきで条件を作る。
        where, params = _face_filter(prefix="f.", **filters)
        query = _shooting_date_query(columns, where, ascending=order == ORDER_SHOT_ASC)
    else:
        where, params = _face_filter(**filters)
        if order == ORDER_SCORE_ASC:
            # **確信度を持たない顔を最後に置く。** 低い順に見たいのだから、
            # NULL が先頭に来ると見直しの邪魔になる。
            order_by = "assign_score IS NULL ASC, assign_score ASC, id ASC"
        elif order == ORDER_SCORE_DESC:
            order_by = "assign_score IS NULL ASC, assign_score DESC, id ASC"
        elif order == ORDER_QUALITY_ASC:
            # SQLite は NULL を最小として扱う。**明示しないと先頭に来る。**
            order_by = "quality_score IS NULL ASC, quality_score ASC, id ASC"
        else:
            order_by = "quality_score DESC, id ASC"
        query = f"SELECT {','.join(columns)} FROM Face{where} ORDER BY {order_by}"

    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        params = params + [limit, offset]
    rows = connection.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def _ids_in_age_order(
    connection: sqlite3.Connection,
    where: str,
    params: List[Any],
    birth_date: Optional[str],
    descending: bool = False,
) -> List[int]:
    """条件に当たる顔の id を、**画面に出ている年齢の若い順**に**全件**返す。

    ``descending`` なら年齢の高い順（同じ年齢の中は撮影日時の新しい順）。
    **年齢を出せない顔はどちらでも最後。**

    **以前は `Face.age`（人が入れた確定値）だけで並べていた。** 実データでは
    ${PERSON_4}の 9,502 件のうち確定値は **199 件だけ**で、**残り 9,303 件は id 順の
    まま2ページ目以降に並んでいた**（2026-10-09）。画面には括弧つきの計算年齢が
    出ているので、利用者には「ページの中しか並んでいない」ように見えた。

    年齢の決め方は画面（`gui.face_age_label`）と同じ。

    | その顔 | 並べる年齢 |
    |---|---|
    | `Face.age` が入っている | その値 |
    | 入っていない | 誕生日と撮影時期から計算（`dates.age_at`。EXIF が無ければフォルダ名の区間） |
    | どちらも出せない | **最後** |

    誕生日は ``birth_date`` が来ていればそれ（**人物を選んでいる画面**。
    「この人物ではない」の一覧の顔は別の人物に付いていることがあるが、表示は
    選んでいる人物の年齢）、無ければ**その顔に付いている人物の誕生日**。

    **SQL に年齢の計算を持ち込まない**（日付の判断は `dates` に1つだけ。
    CLAUDE.md §8）。代わりに**サムネイル抜きで全件**読んで Python で並べる。
    人物の最大で約 1 万行なので軽い（ページのサムネイルは呼び出し側が
    `faces_by_ids` で1ページ分だけ読む）。

    同じ年齢の中は撮影日時の古い順、最後に id で順を固定する
    （**ページをまたいで重複・欠落させないため**）。
    """
    births: Dict[int, Optional[str]] = {}
    if not birth_date:
        births = {
            int(row[0]): row[1]
            for row in connection.execute("SELECT id, birth_date FROM Person")
        }
    rows = connection.execute(
        "SELECT f.id, f.age, f.person_id, m.shooting_date, m.folder_date_from, m.folder_date_to"
        f" FROM Face f JOIN Media m ON m.id = f.media_id{where}",
        params,
    ).fetchall()

    def key(row) -> tuple:
        age = row[1]
        taken = taken_at(row[3], row[4], row[5])
        if age is None:
            owner_birth = birth_date or births.get(row[2])
            age = age_at(owner_birth, taken)
        sign = -1 if descending else 1
        return (
            age is None,
            sign * age if age is not None else 0,
            taken is None,
            sign * taken.earliest.toordinal() if taken is not None else 0,
            int(row[0]),
        )

    return [int(row[0]) for row in sorted(rows, key=key)]


def _shooting_date_query(columns: List[str], where: str, ascending: bool = False) -> str:
    """撮影日時の新しい順（``ascending`` なら古い順）に並べる問い合わせ。

    ``where`` は `_face_filter(prefix="f.")` が作った条件。**この関数に
    絞り込みの引数を並べない** — 以前は `list_faces` と同じ13個を持っていて、
    **絞り込みを足すたびに2か所へ通す必要があった**（通し忘れで撮影年月が
    効かない経路ができた）。

    **`CROSS JOIN` は結合の順序を固定するためのもの**で、直積を作るわけでは
    ない。SQLite は左の表を外側に固定するので、`idx_media_shooting` を
    順に歩いて `LIMIT` の分だけ取れる。

    **外すと、ページを送るたびに未割当の顔を全件並べ直す**（実データ
    58,547 件で 220〜435ms。固定すると1ページ目 0.6ms）。`EXPLAIN QUERY PLAN`
    に `USE TEMP B-TREE FOR ORDER BY` が出たら、その状態に戻っている。

    **行事を指定したときだけは、この索引を捨てて並べ替える**
    （`USE TEMP B-TREE FOR ORDER BY` が出る）。対象が `idx_media_event` で
    1行事（実データの最大で 1,357 件）に絞られたあとの並べ替えなので、
    実測 0.017秒で収まる（指定なしは 0.002秒）。

    **古い順も撮影日時の無い顔を最後に置く。** SQLite は NULL を最小として
    扱うので、索引を逆向きに歩くと先頭に来る。`IS NULL` を先に置くと索引が
    使えなくなり、ページごとに並べ替える（既定の並びではないので許す）。
    """
    selected = ",".join(f"f.{column}" for column in columns)
    sort_key = SHOOTING_DATE_SORT_KEY.replace("shooting_date", "m.shooting_date")
    if ascending:
        order_by = f"{sort_key} IS NULL ASC, {sort_key} ASC, f.id ASC"
    else:
        order_by = f"{sort_key} DESC, f.id ASC"
    return (
        f"SELECT {selected} FROM Media m CROSS JOIN Face f ON f.media_id = m.id"
        f"{where} ORDER BY {order_by}"
    )


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
            " assign_rule = NULL, assigned_at = ? WHERE id = ?"
        )
        rows = [(person_id, assign_source, assign_score, now, face_id) for face_id in face_ids]
    else:
        statement = (
            "UPDATE Face SET person_id = ?, assign_source = ?, assign_score = ?,"
            " assign_rule = NULL, assigned_at = ?, age = ? WHERE id = ?"
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
        "UPDATE Face SET person_id = NULL, assign_source = NULL, assign_score = NULL, assign_rule = NULL,"
        " assigned_at = NULL WHERE id = ?",
        [(face_id,) for face_id in face_ids],
        progress,
    )
    connection.commit()
    return affected


def reject_faces_for_person(
    connection: sqlite3.Connection,
    face_ids: Sequence[int],
    person_id: int,
    progress: ProgressCallback = None,
) -> int:
    """「**この顔はこの人物ではない**」を記録し、割り当てを解除する。

    `reject_faces`（＝「誰でもない顔」）との違いはここ。

    | | この関数 | `reject_faces` |
    |---|---|---|
    | 意味 | **その人物ではない** | **誰でもない顔** |
    | ほかの人物への自動割り当て | **ありうる** | 無い |
    | `match` の候補 | 残る（その人物だけ外れる） | 外れる |

    **解除するだけでは足りない。** `match` は手本と閾値だけで決まるので、
    未割当に戻しただけだと**流すたびに同じ誤りが戻る**（実データで${PERSON_4}の
    1,785 件で起きた）。否定を残して初めて、その判断が次の `match` に効く。

    **手動割り当ても解除する。** その人物だと記録しながら、その人物ではないと
    記録するのは矛盾する。
    """
    if not face_ids:
        return 0
    now = _utc_now()
    cursor = connection.cursor()
    affected = _executemany_with_progress(
        cursor,
        "INSERT INTO FaceRejection (face_id, person_id, created_at) VALUES (?, ?, ?)"
        " ON CONFLICT(face_id, person_id) DO NOTHING",
        [(face_id, person_id, now) for face_id in face_ids],
        progress,
    )
    # その人物に割り当たっているものだけを外す。別の人物のものには触らない。
    cursor.executemany(
        "UPDATE Face SET person_id = NULL, assign_source = NULL, assign_score = NULL, assign_rule = NULL,"
        " assigned_at = NULL WHERE id = ? AND person_id = ?",
        [(face_id, person_id) for face_id in face_ids],
    )
    connection.commit()
    return affected


def clear_person_rejections(
    connection: sqlite3.Connection, face_ids: Sequence[int], person_id: int
) -> int:
    """「その人物ではない」の記録を取り消す。**押し間違いから戻れるように。**"""
    if not face_ids:
        return 0
    cursor = connection.cursor()
    placeholders = ",".join("?" for _ in face_ids)
    cursor.execute(
        f"DELETE FROM FaceRejection WHERE person_id = ? AND face_id IN ({placeholders})",
        (person_id, *face_ids),
    )
    connection.commit()
    return cursor.rowcount


def load_person_rejections(connection: sqlite3.Connection) -> Dict[int, set]:
    """``{face_id: {person_id, ...}}``。**`match` が候補を外すのに使う。**

    否定は顔の数に対して少ない（人が1件ずつ押した結果）ので、全件読んでよい。
    """
    rejections: Dict[int, set] = {}
    for row in connection.execute("SELECT face_id, person_id FROM FaceRejection"):
        rejections.setdefault(int(row["face_id"]), set()).add(int(row["person_id"]))
    return rejections


def count_person_rejections(connection: sqlite3.Connection, person_id: int) -> int:
    """その人物について「ではない」と記録された顔の件数。

    **「誰でもない顔」にした顔も数える。** 記録は残っているため。
    一覧に出る件数とは一致しないことがある
    （一覧は `誰でもない顔` にした顔を外す。`_face_filter` の
    ``rejected_for_person``）。
    """
    row = connection.execute(
        "SELECT COUNT(*) FROM FaceRejection WHERE person_id = ?", (person_id,)
    ).fetchone()
    return int(row[0])


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
        "UPDATE Face SET person_id = NULL, assign_source = ?, assign_score = NULL, assign_rule = NULL,"
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


def count_faces_to_reembed(connection: sqlite3.Connection, version: str) -> int:
    """``version`` と違う版の特徴量を持つ顔の件数。``reembed`` の分母に使う。

    **サムネイルを持たない顔は数えない。** 作り直す材料が無いので対象外
    （実データでは0件だが、`--allow-missing-embeddings` で入れた顔など
    理屈の上ではありうる）。
    """
    row = connection.execute(
        "SELECT COUNT(*) FROM Face"
        " WHERE (embed_version IS NULL OR embed_version != ?)"
        " AND thumbnail IS NOT NULL AND length(thumbnail) > 0",
        (version,),
    ).fetchone()
    return int(row[0])


def iter_faces_to_reembed(
    connection: sqlite3.Connection, version: str, chunk_size: int = 200
) -> Iterable[Tuple[int, bytes]]:
    """作り直す顔を ``(id, サムネイル)`` で順に返す。

    **`OFFSET` で送らない。** 書き換えるたびに対象集合が縮むので、`OFFSET` だと
    まだ処理していない顔を飛ばす。``id`` を進める形（キーセット法）にしてある。

    サムネイルの BLOB は塊ごとにしか持たない（全件読むと 269MB になる）。
    """
    last_id = 0
    while True:
        rows = connection.execute(
            "SELECT id, thumbnail FROM Face"
            " WHERE (embed_version IS NULL OR embed_version != ?)"
            " AND thumbnail IS NOT NULL AND length(thumbnail) > 0"
            " AND id > ? ORDER BY id LIMIT ?",
            (version, last_id, chunk_size),
        ).fetchall()
        if not rows:
            return
        for row in rows:
            yield int(row["id"]), row["thumbnail"]
        last_id = int(rows[-1]["id"])


def save_face_embeddings(
    connection: sqlite3.Connection,
    rows: Sequence[Tuple[int, Optional[bytes]]],
    version: str,
) -> int:
    """特徴量と版だけを書き戻す。**触れた行数を返す。**

    **特徴量が `None` の行も受ける。** 「いまのモデルで作ろうとしたが作れなかった」
    を表すのに要る（小さすぎる顔など）。版だけ進めて特徴量を NULL にしておくと、
    `scan` が同じ状況を記録する形（`scanner` は常に版を書き、特徴量は NULL）と
    そろい、**作り直しの対象から外れて「完了」に到達できる。**

    **割り当てに触らない。** `person_id` / `assign_source` / `assign_score` /
    `assigned_at` / `age` は列挙しないので、手本と除外はそのまま残る。
    これが `scan --force-rescan`（顔の行を作り直す）との決定的な違い。

    **塊ごとに確定する。** 実データで2時間を超える作業なので、途中で止めたときに
    そこまでが残るようにする。
    """
    if not rows:
        return 0
    cursor = connection.cursor()
    cursor.executemany(
        "UPDATE Face SET embedding = ?, embed_version = ? WHERE id = ?",
        [(blob, version, face_id) for face_id, blob in rows],
    )
    connection.commit()
    return len(rows)


def load_manual_embeddings(
    connection: sqlite3.Connection,
) -> Tuple[np.ndarray, np.ndarray]:
    """自動紐づけの手本を読み出す。

    自動紐づけの結果 (``assign_source='auto'``) は教師に含めない。混ぜると
    誤った紐づけが次回以降の基準として増幅されるため。

    戻り値は (埋め込み行列 (K, 次元数), 人物ID配列 (K,)) で、**同じ添字が同じ顔**
    を指す。

    **いま使うモデルと同じ版の特徴量だけを読む**（`embed_version`）。
    別の埋め込み空間の特徴量を混ぜて距離を取るのは常に誤りで、次元が違えば
    `np.vstack` が落ち、**次元が同じモデル同士なら黙って無意味な距離が出る。**
    `photoarchive reembed` の途中は必ず混在するので、ここで絞るのが要る。
    """
    rows = connection.execute(
        "SELECT person_id, embedding FROM Face"
        " WHERE person_id IS NOT NULL AND assign_source = ? AND embedding IS NOT NULL"
        " AND embed_version = ?",
        (ASSIGN_MANUAL, embedding_model.ACTIVE.version),
    ).fetchall()
    if not rows:
        return (
            np.empty((0, EMBEDDING_DIM), dtype=EMBEDDING_DTYPE),
            np.empty((0,), dtype=np.int64),
        )
    matrix = np.vstack([decode_embedding(row["embedding"]) for row in rows])
    person_ids = np.asarray([row["person_id"] for row in rows], dtype=np.int64)
    return matrix, person_ids


def embeddings_for_faces(
    connection: sqlite3.Connection, face_ids: Sequence[int]
) -> Tuple[np.ndarray, np.ndarray]:
    """与えた顔の特徴量を (id配列, 行列) で返す。**いまのモデルの版のものだけ。**

    特徴量の無い顔と版の違う顔は返さない（**距離を出せない**ので、呼び出し側が
    並びの最後に回す）。並びは id 順。`IN (...)` の上限があるので塊に割って読む。
    """
    ids: List[int] = []
    vectors: List[np.ndarray] = []
    chunk = 500
    ordered = sorted(set(int(face_id) for face_id in face_ids))
    for start in range(0, len(ordered), chunk):
        part = ordered[start : start + chunk]
        placeholders = ",".join("?" for _ in part)
        rows = connection.execute(
            f"SELECT id, embedding FROM Face WHERE id IN ({placeholders})"
            " AND embedding IS NOT NULL AND embed_version = ? ORDER BY id",
            (*part, embedding_model.ACTIVE.version),
        ).fetchall()
        for row in rows:
            ids.append(int(row[0]))
            vectors.append(decode_embedding(row[1]))
    if not ids:
        return np.empty((0,), dtype=np.int64), np.empty((0, EMBEDDING_DIM), dtype=EMBEDDING_DTYPE)
    return np.asarray(ids, dtype=np.int64), np.vstack(vectors)


class ManualFaces(NamedTuple):
    """手本の顔を、評価に必要な付随情報ごと持つ。

    ``face_ids`` / ``media_ids`` / ``person_ids`` / ``embeddings`` は
    **同じ添字が同じ顔**を指す。
    """

    face_ids: np.ndarray
    media_ids: np.ndarray
    person_ids: np.ndarray
    embeddings: np.ndarray
    #: 割り当ての根拠にしてよい手本か（**5点整列ができなかった手本は False**）。
    #: 未計測（NULL）は True。**分からないものを外さない。**
    #: 根拠にしない手本も、2位の対抗馬としては使う（`matcher._best_match`）。
    usable: np.ndarray
    #: 撮影時の年齢。確定値（`Face.age`）、無ければ誕生日と撮影日時から計算。
    #: 分からなければ NaN。**年齢ごとの閾値の上限**に使う（#66）。
    ages: np.ndarray


def load_manual_faces(connection: sqlite3.Connection) -> ManualFaces:
    """手本の顔を、face_id と media_id つきで読み出す。

    ``load_manual_embeddings`` との違いは添字を引ける情報が付くこと。
    交差検証は「いま抜いている顔はどれか」「同じ写真に写っている手本はどれか」
    を知る必要があるが、match 本体には不要なので関数を分けている。

    **`load_manual_embeddings` と同じく、同じ版の特徴量だけを読む。**
    """
    rows = connection.execute(
        "SELECT F.id, F.media_id, F.person_id, F.embedding, F.aligned, F.age,"
        " M.shooting_date, P.birth_date"
        " FROM Face F JOIN Media M ON M.id = F.media_id"
        " LEFT JOIN Person P ON P.id = F.person_id"
        " WHERE F.person_id IS NOT NULL AND F.assign_source = ? AND F.embedding IS NOT NULL"
        " AND F.embed_version = ? ORDER BY F.id",
        (ASSIGN_MANUAL, embedding_model.ACTIVE.version),
    ).fetchall()
    if not rows:
        return ManualFaces(
            np.empty((0,), dtype=np.int64),
            np.empty((0,), dtype=np.int64),
            np.empty((0,), dtype=np.int64),
            np.empty((0, EMBEDDING_DIM), dtype=EMBEDDING_DTYPE),
            np.empty((0,), dtype=bool),
            np.empty((0,), dtype=np.float64),
        )
    return ManualFaces(
        np.asarray([row["id"] for row in rows], dtype=np.int64),
        np.asarray([row["media_id"] for row in rows], dtype=np.int64),
        np.asarray([row["person_id"] for row in rows], dtype=np.int64),
        np.vstack([decode_embedding(row["embedding"]) for row in rows]),
        np.asarray([row["aligned"] != 0 for row in rows], dtype=bool),
        np.asarray([_teacher_age(row) for row in rows], dtype=np.float64),
    )


def _teacher_age(row) -> float:
    """手本の撮影時の年齢。**確定値を優先**し、無ければ計算する。分からなければ NaN。

    日付の判断は `dates` に預ける（CLAUDE.md §8。ここで日付を読まない）。

    **フォルダ名から起こした撮影時期は使わない**（#65・利用者が決定）。使うと
    日付の無かった手本 661 件に年齢が付き、8歳以下と分かった手本の上限が締まって、
    **正しい自動割り当てが 125 件外れた**（付いたのは 54 件。2026-10-10・実データの
    複製）。推測した日付を `match` で使うのは、誕生前の人物を外すところだけ
    （`matcher._persons_alive_at`）。
    """
    if row["age"] is not None:
        return float(row["age"])
    age = calculate_age(row["birth_date"], row["shooting_date"])
    return float("nan") if age is None or age < 0 else float(age)


def faces_without_appearance(
    connection: sqlite3.Connection, assign_sources: Sequence[str]
) -> List[int]:
    """見え方（`Face.sharpness` ほか）が未計測で、サムネイルのある顔の id。

    ``assign_sources`` の割り当ての顔だけ。**測るのは使う顔だけ**
    （全顔は約6万件で、1件 約17ms）。
    """
    if not assign_sources:
        return []
    placeholders = ",".join("?" for _ in assign_sources)
    rows = connection.execute(
        "SELECT id FROM Face WHERE sharpness IS NULL AND thumbnail IS NOT NULL"
        f" AND assign_source IN ({placeholders}) ORDER BY id",
        tuple(assign_sources),
    ).fetchall()
    return [int(row[0]) for row in rows]


def face_thumbnails(
    connection: sqlite3.Connection, face_ids: Sequence[int]
) -> Dict[int, Optional[bytes]]:
    """顔 id からサムネイルを引く。**呼び出し側が塊に割って渡すこと**（BLOB は重い）。"""
    found: Dict[int, Optional[bytes]] = {}
    for start in range(0, len(face_ids), 500):
        chunk = list(face_ids[start : start + 500])
        placeholders = ",".join("?" for _ in chunk)
        for row in connection.execute(
            f"SELECT id, thumbnail FROM Face WHERE id IN ({placeholders})", chunk
        ):
            found[int(row[0])] = row[1]
    return found


def save_appearance(connection: sqlite3.Connection, rows: Sequence[Tuple[int, Any]]) -> int:
    """``(face_id, appearance.Appearance)`` を書く。"""
    if not rows:
        return 0
    cursor = connection.cursor()
    cursor.executemany(
        "UPDATE Face SET aligned = ?, yaw = ?, sharpness = ? WHERE id = ?",
        [
            (int(bool(item.aligned)), item.yaw, item.sharpness, face_id)
            for face_id, item in rows
        ],
    )
    connection.commit()
    return cursor.rowcount


#: match が一度に読み出す顔の件数。matcher と二重に持たない。
MATCH_CHUNK_SIZE = 5000


def _match_candidate_filter(include_auto: bool) -> str:
    """match が対象にする顔の条件。

    ``include_auto`` は dry-run 用。本番実行は先に自動割り当てを取り消して
    から候補を数えるので、取り消しを行わない dry-run で ``auto`` を除くと、
    2回目以降の件数と距離の分布が実際より小さく出てしまう。

    **`embed_version` のプレースホルダを1つ持つ。** 呼び出し側は必ず
    `embedding_model.ACTIVE.version` を先頭の引数として渡すこと。
    """
    assigned = "(assign_source IS NULL OR assign_source = 'auto')" if include_auto else "assign_source IS NULL"
    return f" WHERE {assigned} AND embedding IS NOT NULL AND embed_version = ?"


def count_match_candidates(
    connection: sqlite3.Connection, include_auto: bool = False
) -> int:
    """``iter_unassigned_embeddings`` が返すのと同じ集合の件数。

    進捗の分母に使う。``count_faces`` だと埋め込みを持たない顔まで数えて
    しまい、100% に届かないまま終わる。
    """
    where = _match_candidate_filter(include_auto)
    row = connection.execute(
        f"SELECT COUNT(*) FROM Face{where}", (embedding_model.ACTIVE.version,)
    ).fetchone()
    return int(row[0])


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
            (embedding_model.ACTIVE.version, chunk_size, offset),
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
    rule: Optional[str] = None,
) -> int:
    """(face_id, person_id, assign_score) をまとめて書き込む。

    ``rule`` は割り当てを決めた規則の版（`matcher.MATCH_RULE`）。
    **`select` が「古い規則の判定が残っている」ことに気づくための印。**
    """
    if not updates:
        return 0
    now = _utc_now()
    cursor = connection.cursor()
    cursor.executemany(
        "UPDATE Face SET person_id = ?, assign_source = ?, assign_score = ?,"
        " assign_rule = ?, assigned_at = ? WHERE id = ?",
        [
            (person_id, ASSIGN_AUTO, assign_score, rule, now, face_id)
            for face_id, person_id, assign_score in updates
        ],
    )
    connection.commit()
    return cursor.rowcount


def reset_auto_assignments(
    connection: sqlite3.Connection, person_id: Optional[int] = None
) -> int:
    """自動割り当てを取り消す。**手本と除外には触らない。**

    ``person_id`` を渡すと、その人物の自動割り当てだけを取り消す。**省略は
    「全員」であって「人物で絞らない誰か」ではない**ので、呼び出し側が
    どちらのつもりかをはっきり書けるようにしてある。

    **`family_score` はここでは数え直さない。** 呼び出し側が
    `recompute_family_scores` を呼ぶこと（`match` は付け直したあとに呼ぶので、
    ここで呼ぶと二度手間になる）。
    """
    clauses = ["assign_source = ?"]
    params: List[Any] = [ASSIGN_AUTO]
    if person_id is not None:
        clauses.append("person_id = ?")
        params.append(person_id)
    cursor = connection.cursor()
    cursor.execute(
        "UPDATE Face SET person_id = NULL, assign_source = NULL, assign_score = NULL, assign_rule = NULL,"
        f" assigned_at = NULL WHERE {' AND '.join(clauses)}",
        tuple(params),
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


def family_faces(
    connection: sqlite3.Connection,
    media_ids: Optional[Sequence[int]] = None,
    assign_sources: Sequence[str] = (ASSIGN_MANUAL, ASSIGN_AUTO),
) -> List[Dict[str, Any]]:
    """家族の顔（既定は手本と自動割り当て）の、写真の良さを決める列だけを読む。

    **サムネイルも特徴量も読まない**（数万件になるため）。
    ``media_ids`` を渡すとその写真の顔だけ。``assign_sources`` で種別を絞る
    （`select` の `include_auto_assigned: false` は手本だけ・#89）。
    """
    sources = list(assign_sources)
    query = (
        "SELECT media_id, person_id, assign_source, aligned, yaw, sharpness, smile_score"
        " FROM Face WHERE person_id IS NOT NULL"
        f" AND assign_source IN ({','.join('?' for _ in sources)})"
    )
    params: List[Any] = sources
    if media_ids is None:
        return [dict(row) for row in connection.execute(query, params)]
    found: List[Dict[str, Any]] = []
    ids = list(media_ids)
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        found.extend(
            dict(row)
            for row in connection.execute(
                f"{query} AND media_id IN ({placeholders})", params + chunk
            )
        )
    return found


def recompute_family_scores(connection: sqlite3.Connection) -> None:
    """``family_score`` を、家族の顔だけから計算し直す。

    **式は `scoring.family_photo_score` に1つだけある**（`select` もそれを使う）。
    以前は「家族の顔の確信度の最大（手本は100）」で、**ボケた家族の顔でも満点**に
    なっていた。いまは鮮明さ・正面・笑顔と、はっきり写った家族の人数で決まる
    （利用者の要望。2026-10-09）。
    """
    from . import scoring

    scores = scoring.family_photo_scores(family_faces(connection))
    cursor = connection.cursor()
    cursor.execute("UPDATE AnalysisResult SET family_score = 0.0")
    cursor.executemany(
        "INSERT INTO AnalysisResult (media_id, family_score) VALUES (?, ?)"
        " ON CONFLICT(media_id) DO UPDATE SET family_score = excluded.family_score",
        list(scores.items()),
    )
    connection.commit()


def count_stale_auto_assignments(connection: sqlite3.Connection, rule: str) -> int:
    """``rule`` と違う規則で付いた自動割り当ての数（印の無い古いものも含む）。"""
    row = connection.execute(
        "SELECT COUNT(*) FROM Face WHERE assign_source = ?"
        " AND (assign_rule IS NULL OR assign_rule != ?)",
        (ASSIGN_AUTO, rule),
    ).fetchone()
    return int(row[0])


def get_analysis_result(connection: sqlite3.Connection, media_id: int) -> Optional[Dict[str, Any]]:
    row = connection.execute(
        "SELECT * FROM AnalysisResult WHERE media_id = ?", (media_id,)
    ).fetchone()
    return _row_to_dict(row)
