import hashlib
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Generator, List, Optional

from PIL import Image, ExifTags

from .db import save_media

IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "heic", "heif"}
VIDEO_EXTENSIONS = {"mp4", "avi", "mov", "mkv"}
ANALYZER_VERSION = "1.0"


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


def scan_directory(source_dir: str, db_connection, analyzer_version: str = ANALYZER_VERSION) -> List[int]:
    root = Path(source_dir)
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Source directory does not exist: {source_dir}")
    saved_ids = []
    for path in iter_media_files(root):
        file_type = get_media_type(path)
        file_hash = compute_file_hash(path)
        created_time = datetime.fromtimestamp(path.stat().st_mtime).isoformat()
        shooting_date = None
        if file_type == "image":
            shooting_date = extract_exif_datetime(path)
        media = {
            "path": str(path.resolve()),
            "filename": path.name,
            "type": file_type,
            "file_hash": file_hash,
            "file_size": path.stat().st_size,
            "created_time": created_time,
            "shooting_date": shooting_date,
            "analyzed_date": None,
            "analyzer_version": analyzer_version,
        }
        media_id = save_media(db_connection, media)
        saved_ids.append(media_id)
    return saved_ids
