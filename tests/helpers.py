"""テスト用の小さなヘルパー。"""

import io
import struct
from pathlib import Path

import numpy as np
from PIL import Image


def write_image(path: Path, color=(200, 120, 90), size=(200, 200)) -> Path:
    """単色の画像を書き出す。

    conftest のフェイク検出器は「真っ黒でなければ顔が1つある」とみなし、
    フェイクの特徴量は領域の平均色から決まる。つまり色を変えれば別人、
    同じ色にすれば同一人物として扱える。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    array = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    array[:, :] = color
    Image.fromarray(array).save(path, format="JPEG", quality=95)
    return path


def write_black_image(path: Path, size=(200, 200)) -> Path:
    """顔が検出されない画像。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    array = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    Image.fromarray(array).save(path, format="JPEG", quality=95)
    return path


def write_video(path: Path, colors=((255, 0, 0), (0, 255, 0)), size=(160, 120)) -> Path:
    """数フレームの動画を書き出す。``colors`` は **BGR** で与える。

    `read_rgb` は動画の先頭1フレームだけを読み、BGR から RGB へ直す。
    先頭と2枚目に別の色を置くことで「先頭を読んでいるか」と
    「色の並びを直しているか」を1つの素材で確かめられる。

    コーデックが無い環境ではテストをスキップする(`write_heic` と同じ流儀)。
    """
    import cv2
    import pytest

    path.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, 10.0, size)
    if not writer.isOpened():  # pragma: no cover - 環境依存
        writer.release()
        pytest.skip("動画を書き出せない環境(コーデック不在)")
    try:
        for color in colors:
            frame = np.zeros((size[1], size[0], 3), dtype=np.uint8)
            frame[:, :] = color
            writer.write(frame)
    finally:
        writer.release()
    if not path.exists() or path.stat().st_size == 0:  # pragma: no cover - 環境依存
        pytest.skip("動画の書き出しに失敗した環境")
    return path


def textured_array(size=(320, 240), seed=0) -> np.ndarray:
    """写真に近い、模様のある画像。

    単色の画像は JPEG にしても画素が変わらないので、非可逆圧縮で
    画素がずれることを前提にした処理を試せない（#79）。
    """
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0 : size[1], 0 : size[0]]
    array = np.stack(
        [
            128 + 100 * np.sin(x / (9 + 7 * rng.random()) + rng.random() * 6),
            128 + 100 * np.cos(y / (11 + 7 * rng.random()) + rng.random() * 6),
            128 + 60 * np.sin((x + y) / (5 + 5 * rng.random())),
        ],
        axis=-1,
    )
    array += rng.normal(0, 12, array.shape)
    return np.clip(array, 0, 255).astype(np.uint8)


def write_heic(path: Path, color=(200, 120, 90), size=(120, 120), texture_seed=None) -> Path:
    """HEIC(HEIF) 画像を書き出す。

    encoder が無い環境ではテストをスキップする。pillow-heif は
    requirements に入っているが、ビルドによっては読み込み専用のため。
    `texture_seed` を渡すと単色ではなく模様のある画像になる。
    """
    import pytest
    from pillow_heif import register_heif_opener

    register_heif_opener()
    path.parent.mkdir(parents=True, exist_ok=True)
    if texture_seed is None:
        array = np.zeros((size[1], size[0], 3), dtype=np.uint8)
        array[:, :] = color
    else:
        array = textured_array(size, texture_seed)
    try:
        Image.fromarray(array).save(path, format="HEIF", quality=90)
    except Exception as error:  # pragma: no cover - 環境依存
        pytest.skip(f"HEIF の書き出しができない環境: {error}")
    return path


def render_terminal(output: str, width: int = 80) -> list:
    """端末に ``output`` を流したあと、画面に残る行（#78 の進捗表示を確かめるため）。

    進捗表示が使うものだけを解釈する: 改行・行頭へ戻る（``\\r``）・カーソルを上げる
    （``ESC[nA``）・行末まで消す（``ESC[K``）と、**幅を超えたときの折り返し**。
    折り返しを写さないと、#78 の不具合（長い行が折り返して古い行が残る）は見えない。
    全角は2桁で数える（`progress.display_width` と同じ規則）。
    """
    import re
    import unicodedata

    def cell_width(char):
        return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1

    rows = [[]]
    row = col = 0
    tokens = re.findall(r"\x1b\[(\d*)([AK])|(\r)|(\n)|(.)", output, flags=re.S)
    for count, command, carriage, newline, char in tokens:
        if command == "A":
            row = max(0, row - int(count or 1))
        elif command == "K":
            rows[row] = rows[row][:col]
        elif carriage:
            col = 0
        elif newline:
            row += 1
            col = 0
        elif char:
            size = cell_width(char)
            if col + size > width:
                row += 1
                col = 0
            while len(rows) <= row:
                rows.append([])
            line = rows[row]
            while len(line) < col:
                line.append(" ")
            line[col : col + 1] = [char] + ([""] if size == 2 else [])
            col += size
            continue
        while len(rows) <= row:
            rows.append([])
    text = ["".join(line).rstrip() for line in rows]
    while text and not text[-1]:
        text.pop()
    return text


# 連写・似た写真（#86）


def scene(seed: int, size=(320, 240)) -> Image.Image:
    """場面の代わり。``seed`` が同じなら同じ絵、違えば別の絵。"""
    rng = np.random.default_rng(seed)
    blocks = rng.integers(0, 256, size=(6, 8, 3), dtype=np.uint8)
    return Image.fromarray(blocks).resize(size, Image.NEAREST)


def burst_of(image: Image.Image, seed: int) -> Image.Image:
    """同じ場面を少しだけ変えたもの（連写の次の1枚）。"""
    rng = np.random.default_rng(seed)
    pixels = np.asarray(image, dtype=np.int16) + rng.integers(-6, 7, size=image.size[::-1] + (3,))
    return Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8))


def exif_with_thumbnail(thumbnail: Image.Image) -> bytes:
    """IFD1 に JPEG のサムネイルを持つ EXIF（カメラが書く形）。"""
    buffer = io.BytesIO()
    thumbnail.save(buffer, "JPEG")
    data = buffer.getvalue()
    # TIFF ヘッダ(8) + 空の IFD0(2+4) + 2項目の IFD1(2+12*2+4) + サムネイル
    ifd0 = 8
    ifd1 = ifd0 + 6
    start = ifd1 + 2 + 12 * 2 + 4
    tiff = b"II*\x00" + struct.pack("<I", ifd0)
    tiff += struct.pack("<H", 0) + struct.pack("<I", ifd1)
    tiff += struct.pack("<H", 2)
    tiff += struct.pack("<HHII", 0x0201, 4, 1, start)
    tiff += struct.pack("<HHII", 0x0202, 4, 1, len(data))
    tiff += struct.pack("<I", 0)
    return b"Exif\x00\x00" + tiff + data


def write_photo(path: Path, image: Image.Image, thumbnail: Image.Image = None) -> str:
    if thumbnail is None:
        image.save(path, "JPEG", quality=95)
    else:
        image.save(path, "JPEG", quality=95, exif=exif_with_thumbnail(thumbnail))
    return str(path)
