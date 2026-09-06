import hashlib
from pathlib import Path
from typing import Callable, Optional

from PIL import Image
from pillow_heif import register_heif_opener


HEIC_EXTENSIONS = {".heic", ".heif"}
register_heif_opener()


def _image_fingerprint(path: Path) -> str:
    with Image.open(path) as image:
        image = image.convert("RGB")
        image.thumbnail((256, 256), Image.Resampling.BILINEAR)
        return hashlib.sha256(image.tobytes()).hexdigest()


def _same_image(first: Path, second: Path) -> bool:
    try:
        return _image_fingerprint(first) == _image_fingerprint(second)
    except Exception:
        return False


def _next_output_path(source: Path) -> Optional[Path]:
    candidate = source.with_suffix(".jpg")
    if not candidate.exists():
        return candidate
    if _same_image(source, candidate):
        return None
    number = 1
    while True:
        candidate = source.with_name(f"{source.stem}_{number}.jpg")
        if not candidate.exists():
            return candidate
        if _same_image(source, candidate):
            return None
        number += 1


def convert_heic_files(
    source_dir: str,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    confirm_write_error: Optional[Callable[[Path, Exception], bool]] = None,
) -> tuple[int, int]:
    root = Path(source_dir)
    if not root.is_dir():
        raise ValueError(f"Source directory does not exist: {source_dir}")
    sources = sorted(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in HEIC_EXTENSIONS)
    converted = 0
    skipped = 0
    for index, source in enumerate(sources, start=1):
        output = _next_output_path(source)
        if output is None:
            skipped += 1
        else:
            try:
                with Image.open(source) as image:
                    image.convert("RGB").save(output, "JPEG", quality=95)
                converted += 1
            except (OSError, PermissionError) as error:
                if confirm_write_error is None or not confirm_write_error(source, error):
                    raise
        if progress_callback is not None:
            progress_callback(index, len(sources), source.name)
    return converted, skipped