"""メディアの走査と顔検出。

``scan`` の責務:

1. 対象ディレクトリを再帰的に走査してファイルを ``Media`` に登録する
2. 同じ読み込みのついでに顔を検出し、顔画像・特徴量・スコアを ``Face`` に保存する
3. 既に顔検出済みのメディアは再検出しない (差分スキャン)
4. DBにあるのに実体が無くなったメディアの行を削除する

人物への紐づけはここでは一切行わない。それは GUI での手動割り当てと
``match`` の仕事。
"""

import hashlib
import logging
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple

from PIL import ExifTags, Image

from . import db, face, scoring

logger = logging.getLogger("photoarchive.scanner")

IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "heic", "heif"}
VIDEO_EXTENSIONS = {"mp4", "avi", "mov", "mkv"}

COMMIT_INTERVAL = 50
#: 実体が消えたと判断した行がこの割合を超えたら、取り違えを疑って中断する
PRUNE_ABORT_RATIO = 0.2
#: 更新時刻の比較許容差(秒)。NFS やタイムゾーンの丸めで全件が
#: 「変更あり」に倒れると、数時間のハッシュ再計算が走ってしまう。
MTIME_TOLERANCE_SECONDS = 1.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


class ScanAborted(RuntimeError):
    """安全のためにスキャンを中断したときに送出する。"""


def is_media_file(path: Path) -> bool:
    suffix = path.suffix.lower().lstrip(".")
    return suffix in IMAGE_EXTENSIONS or suffix in VIDEO_EXTENSIONS


def get_media_type(path: Path) -> Optional[str]:
    suffix = path.suffix.lower().lstrip(".")
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    return None


def compute_file_hash(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8192), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def extract_exif_datetime(path: Path) -> Optional[str]:
    try:
        image = Image.open(path)
        exif = image._getexif()
        if exif is None:
            return None
        tags = {ExifTags.TAGS.get(key, key): value for key, value in exif.items()}
        for key in ("DateTimeOriginal", "DateTime", "DateTimeDigitized"):
            if key in tags:
                value = tags[key]
                if isinstance(value, str):
                    return value.replace(":", "-", 2).replace(" ", "T")
    except Exception:
        return None
    return None


def iter_media_files(root: Path) -> Generator[Path, None, None]:
    for path in root.rglob("*"):
        if path.is_file() and is_media_file(path):
            yield path


def _mtime_matches(stored_created_time: Optional[str], mtime: float) -> bool:
    if not stored_created_time:
        return False
    try:
        stored = datetime.fromisoformat(stored_created_time).timestamp()
    except (TypeError, ValueError):
        return False
    return abs(stored - mtime) <= MTIME_TOLERANCE_SECONDS


def _needs_face_scan(record: Optional[Dict[str, Any]], force_rescan: bool) -> bool:
    if force_rescan or record is None:
        return True
    if record.get("face_count") is None:
        return True
    return record.get("detector_version") != face.DETECTOR_VERSION


# ---------------------------------------------------------------------------
# 1ファイル分の処理(ワーカープロセスでも実行される)
# ---------------------------------------------------------------------------


def analyze_file(
    path_str: str,
    compute_hash: bool,
    known_hash: Optional[str],
    force_faces: bool,
) -> Dict[str, Any]:
    """1ファイルを読み、ハッシュ・EXIF・顔情報を返す純粋な処理。

    顔検出を行うのは ``force_faces`` が真のとき、またはハッシュが
    ``known_hash`` と食い違ったとき(＝中身が変わったとき)だけ。
    """
    path = Path(path_str)
    result: Dict[str, Any] = {
        "path": str(path),
        "error": None,
        "file_hash": None,
        "faces": [],
        "face_count": None,
    }
    try:
        stat = path.stat()
        result["file_size"] = stat.st_size
        result["created_time"] = datetime.fromtimestamp(stat.st_mtime).isoformat()
        result["type"] = get_media_type(path)
        result["filename"] = path.name

        if compute_hash:
            result["file_hash"] = compute_file_hash(path)

        if result["type"] == "image":
            result["shooting_date"] = extract_exif_datetime(path)
        else:
            result["shooting_date"] = None

        changed = compute_hash and known_hash is not None and result["file_hash"] != known_hash
        if not (force_faces or changed):
            return result

        rgb = face.read_rgb(path)
        if rgb is None:
            # 読めないファイルを未スキャン(NULL)のままにすると毎回再試行になる。
            # 「顔は見つからなかった」として記録し、理由はログに残す。
            result["face_count"] = 0
            result["error"] = face.get_latest_error()
            return result

        locations = face.detect_faces(rgb)
        faces: List[Dict[str, Any]] = []
        for location in locations:
            embedding = face.compute_embedding(rgb, location)
            smile_score, quality_score = scoring.score_face(rgb, location)
            faces.append(
                {
                    "bbox": location,
                    "embedding": face.embedding_to_list(embedding),
                    "thumbnail": face.make_thumbnail(rgb, location),
                    "smile_score": smile_score,
                    "quality_score": quality_score,
                }
            )
        result["faces"] = faces
        result["face_count"] = len(faces)
        if not faces:
            result["error"] = face.get_latest_error()
        return result
    except Exception as error:  # pragma: no cover - 想定外の失敗も全体は止めない
        logger.exception("Failed to process %s: %s", path, error)
        result["error"] = f"{path.name}: {error}"
        return result


def _analyze_file_task(task: Tuple[str, bool, Optional[str], bool]) -> Dict[str, Any]:
    return analyze_file(*task)


# ---------------------------------------------------------------------------
# DB への書き込み
# ---------------------------------------------------------------------------


def _store_result(connection, result: Dict[str, Any], record: Optional[Dict[str, Any]]) -> int:
    """1メディア分の結果を1トランザクションで書く。"""
    media = {
        "path": result["path"],
        "filename": result["filename"],
        "type": result["type"],
        "file_hash": result["file_hash"] or (record or {}).get("file_hash"),
        "file_size": result["file_size"],
        "created_time": result["created_time"],
        "shooting_date": result.get("shooting_date"),
        "face_count": None,
        "face_scanned_at": None,
        "detector_version": None,
    }
    media_id = db.save_media(connection, media)

    if result["face_count"] is None:
        return media_id

    removed_manual = db.count_manual_faces_for_media(connection, media_id)
    if removed_manual:
        logger.warning(
            "Re-scan discards %d manually assigned face(s): %s", removed_manual, result["path"]
        )
    db.delete_faces_for_media(connection, media_id)

    face_scores = []
    for entry in result["faces"]:
        db.add_face(
            connection,
            media_id=media_id,
            bbox=entry["bbox"],
            embedding=entry["embedding"],
            embed_version=face.EMBED_VERSION,
            thumbnail=entry["thumbnail"],
            smile_score=entry["smile_score"],
            quality_score=entry["quality_score"],
        )
        face_scores.append((entry["smile_score"], entry["quality_score"]))

    smile_score, quality_score = scoring.aggregate_media_scores(face_scores)
    db.save_media_scores(connection, media_id, smile_score, quality_score)
    db.update_media_scan_state(
        connection,
        media_id,
        result["face_count"],
        _utc_now(),
        face.DETECTOR_VERSION,
    )
    return media_id


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------


def prune_missing_media(
    connection,
    root: Path,
    present_paths: set,
    force: bool = False,
) -> int:
    """走査範囲にあって実体が無くなったメディアの行を削除する。

    ソースが未マウントだった場合に全消しにならないよう、削除が2割を超えたら
    中断する。
    """
    prefix = str(root) + "/"
    rows = connection.execute("SELECT id, path FROM Media").fetchall()
    in_scope = [row for row in rows if row["path"] == str(root) or row["path"].startswith(prefix)]
    if not in_scope:
        return 0
    missing = [row["id"] for row in in_scope if row["path"] not in present_paths]
    if not missing:
        return 0
    ratio = len(missing) / len(in_scope)
    if ratio > PRUNE_ABORT_RATIO and not force:
        raise ScanAborted(
            f"登録済み {len(in_scope)} 件のうち {len(missing)} 件 "
            f"({ratio:.0%}) が見つかりません。ソースの指定やマウント状態を確認してください。"
            " 意図した削除であれば --force-prune を付けて再実行してください。"
        )
    return db.delete_media(connection, missing)


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------


def scan_directory(
    source_dir: str,
    db_connection,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    workers: int = 1,
    prune: bool = True,
    force_prune: bool = False,
    force_rescan: bool = False,
) -> Dict[str, Any]:
    """ディレクトリを走査し、メディアの登録と顔検出を行う。"""
    root = Path(source_dir).resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Source directory does not exist: {source_dir}")

    media_files = sorted(iter_media_files(root))
    if not media_files:
        raise ScanAborted(
            f"対象ディレクトリにメディアファイルが1件もありません: {root}"
            " (マウントされていない可能性があります)"
        )

    index = db.load_media_index(db_connection)
    present_paths = {str(path) for path in media_files}

    tasks: List[Tuple[str, bool, Optional[str], bool]] = []
    records: Dict[str, Optional[Dict[str, Any]]] = {}
    skipped = 0
    for path in media_files:
        key = str(path)
        record = index.get(key)
        records[key] = record
        try:
            stat = path.stat()
        except OSError:
            continue
        unchanged = (
            record is not None
            and record.get("file_size") == stat.st_size
            and _mtime_matches(record.get("created_time"), stat.st_mtime)
        )
        need_faces = _needs_face_scan(record, force_rescan)
        if unchanged and not need_faces:
            skipped += 1
            continue
        known_hash = record.get("file_hash") if record else None
        tasks.append((key, not unchanged, known_hash, need_faces))

    total = len(tasks)
    summary: Dict[str, Any] = {
        "total_files": len(media_files),
        "processed": 0,
        "skipped": skipped,
        "faces": 0,
        "errors": 0,
        "pruned": 0,
        "media_ids": [],
    }

    def handle(result: Dict[str, Any], position: int) -> None:
        record = records.get(result["path"])
        media_id = _store_result(db_connection, result, record)
        summary["media_ids"].append(media_id)
        summary["processed"] += 1
        summary["faces"] += len(result["faces"])
        if result.get("error"):
            summary["errors"] += 1
        if position % COMMIT_INTERVAL == 0:
            db_connection.commit()
        if progress_callback is not None:
            progress_callback(position, total, Path(result["path"]).name)

    if total:
        if workers > 1:
            with ProcessPoolExecutor(max_workers=workers) as executor:
                for position, result in enumerate(
                    executor.map(_analyze_file_task, tasks, chunksize=4), start=1
                ):
                    handle(result, position)
        else:
            for position, task in enumerate(tasks, start=1):
                handle(_analyze_file_task(task), position)
        db_connection.commit()
    elif progress_callback is not None:
        progress_callback(0, 0, "no media to scan")

    if prune:
        summary["pruned"] = prune_missing_media(
            db_connection, root, present_paths, force=force_prune
        )

    return summary
