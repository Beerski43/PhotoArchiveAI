"""テスト用の小さなヘルパー。"""

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


def write_heic(path: Path, color=(200, 120, 90), size=(120, 120)) -> Path:
    """HEIC(HEIF) 画像を書き出す。

    encoder が無い環境ではテストをスキップする。pillow-heif は
    requirements に入っているが、ビルドによっては読み込み専用のため。
    """
    import pytest
    from pillow_heif import register_heif_opener

    register_heif_opener()
    path.parent.mkdir(parents=True, exist_ok=True)
    array = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    array[:, :] = color
    try:
        Image.fromarray(array).save(path, format="HEIF", quality=90)
    except Exception as error:  # pragma: no cover - 環境依存
        pytest.skip(f"HEIF の書き出しができない環境: {error}")
    return path
