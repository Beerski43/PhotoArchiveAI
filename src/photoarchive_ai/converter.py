from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PIL import Image
from pillow_heif import register_heif_opener


HEIC_EXTENSIONS = {".heic", ".heif"}
register_heif_opener()


# 同じ写真とみなす、縮小画素の差の平均（0〜255）の上限（#79）。
# JPEG は非可逆なので、同じ写真から作っても画素は完全には一致しない。
# 実データでは同じ写真が最大 0.14、連写の別写真が最小 2.37 だった
# （scripts/measure_heic_duplicates.py で測り直せる）。
SAME_IMAGE_MAX_DIFF = 1.0
_COMPARE_SIZE = (64, 64)


def _image_fingerprint(path: Path) -> tuple[tuple[int, int], np.ndarray]:
    with Image.open(path) as image:
        size = image.size
        small = image.convert("RGB").resize(_COMPARE_SIZE, Image.Resampling.BILINEAR)
        return size, np.asarray(small, dtype=np.int16)


def image_difference(first: Path, second: Path) -> Optional[float]:
    """縮小画素の差の平均。寸法が違えば None（別の写真）。"""
    first_size, first_pixels = _image_fingerprint(first)
    second_size, second_pixels = _image_fingerprint(second)
    if first_size != second_size:
        return None
    return float(np.abs(first_pixels - second_pixels).mean())


def _same_image(first: Path, second: Path) -> bool:
    try:
        difference = image_difference(first, second)
    except Exception:
        return False
    return difference is not None and difference <= SAME_IMAGE_MAX_DIFF


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