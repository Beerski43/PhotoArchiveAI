"""写真全体の良し悪しを測る（#106）。

**なぜ要るか。** `select` の並び（`scoring.family_photo_score`）は家族の**顔**しか見ていない。
手ぶれ・ピンぼけ・暗い写真や、構図の整った写真を見分ける材料が無かった。

#105 の試験で、利用者が次の2つを採ると決めた（2026-10-15）。

- **MUSIQ（KonIQ で学習・技術的な画質）**: 下位はほぼ全部が手ぶれ・ピンぼけ・暗い写真
- **NIMA（AVA で学習・美しさ）**: 構図の整った写真を上に付ける

**推論は ONNX Runtime**（利用者の決定。アプリに PyTorch を入れない）。ONNX は
`scripts/export_iqa_onnx.py` が pyiqa から書き出して `models/` に置く（配布元が無いので
`fetch_models.py` では落とせない）。**前処理は pyiqa の PyTorch の処理を numpy で書き直した**
ので、変えたら書き出しスクリプトの比べ合わせを流し直すこと。

同じ1回の読みで、**白飛び・黒つぶれ**（明るさの分布）と、**元写真の幅と高さ**も測る。
幅と高さは顔の見切れ（顔の矩形が写真の縁に接しているか）に使う。**`scan` の顔の矩形は
EXIF の向きを直さない座標**（`face.read_rgb`）なので、幅と高さも向きを直す前のもの。
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Callable, NamedTuple, Optional, Tuple

import numpy as np
from PIL import Image, ImageOps

logger = logging.getLogger("photoarchive.quality")

#: モデルに渡す写真の長辺。#105 の試験と同じ（モデルの入力より大きい分は時間の無駄）。
LONG_SIDE = 1024

#: ONNX のファイル名（`face.find_model_file` で探す）と版。**前処理か書き出し方を変えたら版を上げる。**
#: 版が変わると `assess` が全部を測り直す。
MUSIQ_FILE = "iqa_musiq_koniq.onnx"
NIMA_FILE = "iqa_nima_ava.onnx"
MODEL_VERSION = "musiq-koniq/1+nima-ava/1"

#: 白飛び・黒つぶれと数える明るさ（0〜255）。
HIGHLIGHT_LEVEL = 250
SHADOW_LEVEL = 5

# --- MUSIQ（pyiqa の既定）-------------------------------------------------------
MUSIQ_PATCH = 32
MUSIQ_GRID = 10
MUSIQ_SCALES = (224, 384)
#: 縮小の3次補間の係数（PyTorch の bicubic と同じ）。
CUBIC_A = -0.75

# --- NIMA（pyiqa の既定・Inception-ResNet-v2）----------------------------------------
NIMA_SIZE = 299


class Measured(NamedTuple):
    """写真1枚の測定値。"""

    technical: float
    aesthetic: float
    highlight_clip: float
    shadow_clip: float
    #: 元写真の幅と高さ（EXIF の向きを直す前。`scan` の顔の矩形と同じ座標）。
    width: int
    height: int


# ---------------------------------------------------------------------------
# 読み込み
# ---------------------------------------------------------------------------


def load_photo(path: Path, long_side: int = LONG_SIDE) -> Tuple[Image.Image, Tuple[int, int]]:
    """写真を長辺 ``long_side`` に縮めて読む（#105 の試験と同じ読み方）。

    返すのは（向きを直した RGB の画像, 向きを直す前の元の幅と高さ）。
    """
    with Image.open(path) as image:
        size = image.size
        image.draft("RGB", (long_side, long_side))
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((long_side, long_side))
        return image.copy(), size


def to_array(image: Image.Image) -> np.ndarray:
    """(高さ, 幅, 3) の float32・0〜1。"""
    return np.asarray(image, dtype=np.float32) / 255.0


def clipping(rgb: np.ndarray) -> Tuple[float, float]:
    """白飛びと黒つぶれの画素の割合（0〜1）。``rgb`` は 0〜1。"""
    luma = (rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114) * 255.0
    return float(np.mean(luma >= HIGHLIGHT_LEVEL)), float(np.mean(luma <= SHADOW_LEVEL))


# ---------------------------------------------------------------------------
# MUSIQ の前処理（pyiqa `data/multiscale_trans_util.py` を numpy に写したもの）
# ---------------------------------------------------------------------------


def _cubic(x: np.ndarray) -> np.ndarray:
    x = np.abs(x)
    a = CUBIC_A
    near = ((a + 2) * x - (a + 3)) * x * x + 1
    far = ((a * x - 5 * a) * x + 8 * a) * x - 4 * a
    return np.where(x <= 1, near, np.where(x < 2, far, 0.0))


def cubic_taps(size_in: int, size_out: int) -> Tuple[np.ndarray, np.ndarray]:
    """3次補間で、出力の1画素が使う入力の4画素の位置と重み（どちらも size_out × 4）。

    PyTorch の ``F.interpolate(mode='bicubic', align_corners=False)`` と同じ。
    **縮小でも平滑化しない**（PyTorch の既定。MUSIQ はこれで学習している）。端は複製。
    """
    scale = size_in / size_out
    source = (np.arange(size_out) + 0.5) * scale - 0.5
    base = np.floor(source)
    t = source - base
    offsets = np.arange(-1, 3)
    index = np.clip(base[:, None] + offsets[None, :], 0, size_in - 1).astype(int)
    weight = _cubic(t[:, None] - offsets[None, :])
    return index, weight


def _resize_axis(array: np.ndarray, size_out: int, axis: int) -> np.ndarray:
    index, weight = cubic_taps(array.shape[axis], size_out)
    shape = [1] * array.ndim
    shape[axis] = size_out
    result = np.zeros(array.shape[:axis] + (size_out,) + array.shape[axis + 1:], dtype=np.float64)
    for tap in range(4):
        result += np.take(array, index[:, tap], axis=axis) * weight[:, tap].reshape(shape)
    return result


def resize_cubic(chw: np.ndarray, height: int, width: int) -> np.ndarray:
    """(c, h, w) を3次補間で (c, height, width) に。

    **行列の積を使わない**（1画素に効くのは4画素だけ）。この PC の numpy の BLAS では、
    同じ計算を行列の積にすると 1 段で約 2 秒かかり、MUSIQ の推論（約 1 秒）より遅かった。
    """
    rows = _resize_axis(chw.astype(np.float64), height, axis=1)
    return _resize_axis(rows, width, axis=2).astype(np.float32)


def _nearest_index(grid: int, count: int) -> np.ndarray:
    """``F.interpolate(mode='nearest')`` で 0..grid-1 を count 個に伸ばしたもの。"""
    return np.floor(np.arange(count) * (grid / count)).astype(np.float32)


def hashed_positions(grid: int, count_h: int, count_w: int) -> np.ndarray:
    """パッチごとの位置の番号（grid × grid に畳む）。並びは行ごと。"""
    h = _nearest_index(grid, count_h)
    w = _nearest_index(grid, count_w)
    return (h[:, None] * grid + w[None, :]).reshape(-1)


def _patches(chw: np.ndarray, patch: int) -> Tuple[np.ndarray, int, int]:
    """TF の 'SAME' の余白を足して、patch × patch に切る。(パッチ数, c*patch*patch)。"""
    c, h, w = chw.shape
    count_h, count_w = math.ceil(h / patch), math.ceil(w / patch)
    pad_h, pad_w = count_h * patch - h, count_w * patch - w
    padded = np.pad(chw, ((0, 0), (pad_h // 2, pad_h - pad_h // 2), (pad_w // 2, pad_w - pad_w // 2)))
    blocks = padded.reshape(c, count_h, patch, count_w, patch).transpose(1, 3, 0, 2, 4)
    return blocks.reshape(count_h * count_w, c * patch * patch), count_h, count_w


def _scale_tokens(chw: np.ndarray, scale_id: int, max_len: Optional[int]) -> np.ndarray:
    patches, count_h, count_w = _patches(chw, MUSIQ_PATCH)
    n = patches.shape[0]
    extra = np.stack(
        [
            hashed_positions(MUSIQ_GRID, count_h, count_w),
            np.full(n, scale_id, dtype=np.float32),
            np.ones(n, dtype=np.float32),
        ],
        axis=1,
    )
    tokens = np.concatenate([patches, extra], axis=1)
    if max_len is not None:
        # 足りなければ 0 で埋める（マスクも 0）。多ければ切る。
        filled = np.zeros((max_len, tokens.shape[1]), dtype=np.float32)
        filled[: min(n, max_len)] = tokens[:max_len]
        tokens = filled
    return tokens


def musiq_input(rgb: np.ndarray) -> np.ndarray:
    """MUSIQ の入力 (1, パッチ数, 3075)。``rgb`` は (h, w, 3)・0〜1。

    224・384 の2段の縮小（それぞれ固定の長さに埋める）と、元の解像度の全パッチを並べる。
    """
    chw = (rgb.transpose(2, 0, 1).astype(np.float32) - 0.5) * 2
    _, h, w = chw.shape
    parts = []
    for scale_id, longer in enumerate(sorted(MUSIQ_SCALES)):
        ratio = longer / max(h, w)
        resized = resize_cubic(chw, round(h * ratio), round(w * ratio))
        max_len = int(math.ceil(longer / MUSIQ_PATCH) ** 2)
        parts.append(_scale_tokens(resized, scale_id, max_len))
    parts.append(_scale_tokens(chw, len(MUSIQ_SCALES), None))
    return np.concatenate(parts, axis=0)[None].astype(np.float32)


# ---------------------------------------------------------------------------
# NIMA の前処理（torchvision の resize（短辺・平滑化あり）→ 中央の切り出し）
# ---------------------------------------------------------------------------


def nima_input(rgb: np.ndarray) -> np.ndarray:
    """NIMA の入力 (1, 3, 299, 299)・0〜1（正規化は ONNX の中で行う）。

    短辺を 299 に縮め（双線形・平滑化あり。torchvision の既定は PIL と同じ作り）、中央を切り出す。
    """
    h, w = rgb.shape[:2]
    if h <= w:
        size = (int(NIMA_SIZE * w / h), NIMA_SIZE)
    else:
        size = (NIMA_SIZE, int(NIMA_SIZE * h / w))
    channels = [
        np.asarray(Image.fromarray(rgb[..., c].astype(np.float32), mode="F").resize(size, Image.BILINEAR))
        for c in range(3)
    ]
    resized = np.stack(channels, axis=0)
    _, rh, rw = resized.shape
    top = int(round((rh - NIMA_SIZE) / 2.0))
    left = int(round((rw - NIMA_SIZE) / 2.0))
    return resized[None, :, top : top + NIMA_SIZE, left : left + NIMA_SIZE].astype(np.float32)


# ---------------------------------------------------------------------------
# 推論
# ---------------------------------------------------------------------------


class Models:
    """MUSIQ と NIMA の ONNX のセッション。`load` で作る。"""

    def __init__(self, musiq, nima):
        self.musiq = musiq
        self.nima = nima

    @classmethod
    def load(cls) -> "Models":
        """``models/`` から読む。無ければ置き方を添えて落とす。"""
        from . import face  # noqa: PLC0415 — face は重い（mediapipe）ので使うときだけ

        missing = [name for name in (MUSIQ_FILE, NIMA_FILE) if face.find_model_file(name) is None]
        if missing:
            raise FileNotFoundError(
                f"写真の良し悪しのモデルが見つかりません: {', '.join(missing)}。"
                " PyTorch と pyiqa のある別の仮想環境で `scripts/export_iqa_onnx.py` を流して"
                " models/ に置いてください（手順は README）。"
            )
        import onnxruntime  # noqa: PLC0415

        def session(name):
            return onnxruntime.InferenceSession(
                str(face.find_model_file(name)), providers=["CPUExecutionProvider"]
            )

        return cls(session(MUSIQ_FILE), session(NIMA_FILE))

    def technical(self, rgb: np.ndarray) -> float:
        """MUSIQ の点（KonIQ・おおむね 0〜100。高いほど良い）。"""
        tokens = musiq_input(rgb)
        return float(self.musiq.run(None, {self.musiq.get_inputs()[0].name: tokens})[0].ravel()[0])

    def aesthetic(self, rgb: np.ndarray) -> float:
        """NIMA の点（AVA・1〜10。高いほど良い）。"""
        pixels = nima_input(rgb)
        return float(self.nima.run(None, {self.nima.get_inputs()[0].name: pixels})[0].ravel()[0])


def measure(models: Models, image: Image.Image, size: Tuple[int, int]) -> Measured:
    """読んだ写真1枚を測る（`load_photo` の戻り値を渡す）。"""
    rgb = to_array(image)
    highlight, shadow = clipping(rgb)
    return Measured(
        technical=models.technical(rgb),
        aesthetic=models.aesthetic(rgb),
        highlight_clip=highlight,
        shadow_clip=shadow,
        width=int(size[0]),
        height=int(size[1]),
    )


# ---------------------------------------------------------------------------
# photoarchive assess
# ---------------------------------------------------------------------------

#: 何枚ごとに DB へ書くか。**途中で止めても、書いた分は次から測らない。**
CHUNK_SIZE = 20


def _load(path: Path):
    try:
        return load_photo(path)
    except Exception as error:  # 壊れた写真・NFS の読み損ね。1枚で止めない
        logger.warning("Cannot read photo for assess: %s: %s", path, error)
        return None


def assess(
    connection,
    models: Models,
    assign_sources=None,
    root_paths=(),
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    limit: Optional[int] = None,
) -> dict:
    """家族の写った写真のうち、まだ測っていない写真を測って書く（`photoarchive assess`）。

    **NFS の読み込みと推論を重ねる**（次の写真を別のスレッドで先に読む）。読めなかった写真は
    ``readable=0`` で残し、次からは読まない。``limit`` は試しに数枚だけ測るとき。
    返すのは ``{"total": 対象, "measured": 測れた, "unreadable": 読めなかった}``。
    """
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    from . import db  # noqa: PLC0415

    sources = assign_sources or (db.ASSIGN_MANUAL, db.ASSIGN_AUTO)
    targets = db.photos_to_assess(connection, MODEL_VERSION, sources)
    if limit is not None:
        targets = targets[:limit]
    total = len(targets)
    summary = {"total": total, "measured": 0, "unreadable": 0}
    if not total:
        return summary

    def resolve(path_value: str) -> Path:
        path = Path(path_value)
        if not path.is_absolute() and root_paths:
            path = Path(root_paths[0]) / path
        return path

    if progress_callback is not None:
        progress_callback(0, total, "assessing photos")
    pending = []
    with ThreadPoolExecutor(max_workers=1) as reader:
        upcoming = reader.submit(_load, resolve(targets[0][1]))
        for index, (media_id, _) in enumerate(targets):
            loaded = upcoming.result()
            if index + 1 < total:
                upcoming = reader.submit(_load, resolve(targets[index + 1][1]))
            if loaded is None:
                pending.append((media_id, None))
                summary["unreadable"] += 1
            else:
                pending.append((media_id, measure(models, *loaded)))
                summary["measured"] += 1
            if len(pending) >= CHUNK_SIZE or index + 1 == total:
                db.save_photo_quality(connection, pending, MODEL_VERSION)
                pending = []
            if progress_callback is not None:
                progress_callback(index + 1, total, "assessing photos")
    return summary
