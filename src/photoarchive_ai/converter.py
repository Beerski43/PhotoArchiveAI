from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PIL import Image
from pillow_heif import register_heif_opener


HEIC_EXTENSIONS = {".heic", ".heif"}
register_heif_opener()

_EXIF_HEADER = b"Exif\x00\x00"
_SOI = b"\xff\xd8"
_APP0 = 0xE0
_APP1 = 0xE1
_SOS = 0xDA


def _jpeg_segments(data: bytes):
    """SOS の手前までの (マーカー, 開始位置, 終了位置)。壊れていれば ValueError。"""
    if not data.startswith(_SOI):
        raise ValueError("JPEG ではない（SOI が無い）")
    position = 2
    while position + 4 <= len(data):
        if data[position] != 0xFF:
            raise ValueError(f"マーカーが読めない: {position}")
        marker = data[position + 1]
        if marker == _SOS:
            return
        length = int.from_bytes(data[position + 2 : position + 4], "big")
        end = position + 2 + length
        if length < 2 or end > len(data):
            raise ValueError(f"セグメントの長さが壊れている: {position}")
        yield marker, position, end
        position = end
    raise ValueError("SOS が見つからない")


def has_exif(data: bytes) -> bool:
    """JPEG のバイト列が EXIF（APP1）を持つか。"""
    return any(
        marker == _APP1 and data[start + 4 : start + 10] == _EXIF_HEADER
        for marker, start, _ in _jpeg_segments(data)
    )


def insert_exif(data: bytes, exif: bytes) -> bytes:
    """JPEG のバイト列に EXIF を差し込む。**画素データのバイト列には触れない。**

    再エンコードすると画素が変わり、`scan` が別の写真として顔を検出し直す。
    ここでは APP1 を1つ足すだけなので、足した APP1 を取り除けば元のバイト列に戻る
    （PR #80 のレビュー判断 (a)・案4。`scripts/restore_heic_exif.py` が使う）。
    APP0（JFIF）があればその直後、無ければ SOI の直後に置く。
    """
    if not exif.startswith(_EXIF_HEADER):
        raise ValueError("EXIF の先頭が Exif\\0\\0 ではない")
    if len(exif) + 2 > 0xFFFF:
        raise ValueError("EXIF が大きすぎて1つの APP1 に収まらない")
    if has_exif(data):
        raise ValueError("すでに EXIF がある")
    at = 2
    for marker, _, end in _jpeg_segments(data):
        if marker == _APP0:
            at = end
        break
    segment = b"\xff\xe1" + (len(exif) + 2).to_bytes(2, "big") + exif
    return data[:at] + segment + data[at:]


def strip_exif(data: bytes) -> bytes:
    """`insert_exif` の逆。最初の EXIF（APP1）を取り除いたバイト列。無ければそのまま。"""
    for marker, start, end in _jpeg_segments(data):
        if marker == _APP1 and data[start + 4 : start + 10] == _EXIF_HEADER:
            return data[:start] + data[end:]
    return data


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
    confirm_read_error: Optional[Callable[[Path, Exception], bool]] = None,
) -> tuple[int, int]:
    """HEIC/HEIF を同じフォルダの JPEG にする。``(変換した件数, スキップした件数)`` を返す。

    **読めない HEIC と、書けない JPEG を分けて聞く**（``confirm_read_error`` /
    ``confirm_write_error``。真を返せば次のファイルへ進む）。以前は両方を「Write failed」と
    聞いており、壊れた HEIC を書き込み先の問題と読ませていた（PR #81 で利用者の依頼）。
    どちらも渡さなければ例外をそのまま投げる。
    """
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
                    # 撮影日時を引き継ぐ（PR #80 のレビュー指摘1）。scan は撮影日時を
                    # EXIF からしか読まない。pillow-heif は回転を済ませて Orientation を
                    # 1 にした EXIF を返すので、そのまま渡しても二重に回らない。
                    exif = image.info.get("exif")
                    rgb = image.convert("RGB")
            except OSError as error:
                # 壊れた HEIC など（UnidentifiedImageError も OSError）
                rgb = None
                if confirm_read_error is None or not confirm_read_error(source, error):
                    raise
            if rgb is not None:
                try:
                    extra = {"exif": exif} if exif else {}
                    rgb.save(output, "JPEG", quality=95, **extra)
                    converted += 1
                except OSError as error:
                    if confirm_write_error is None or not confirm_write_error(source, error):
                        raise
        if progress_callback is not None:
            progress_callback(index, len(sources), source.name)
    return converted, skipped