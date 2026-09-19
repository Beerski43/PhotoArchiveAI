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
