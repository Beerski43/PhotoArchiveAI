"""顔の表情（目閉じ・笑顔）を、保存済みサムネイルから測る（#95）。

**なぜ要るか。** `select` の並び（`scoring.family_photo_score`）には、目を閉じた顔を
見分ける材料が無かった。笑顔（`scoring.estimate_smile_score`・口角の縦横比）は
家族の顔の 8 割以上が 100 に張り付いていて、無表情の顔の 86% にも 100 を付けていた
（2026-10-11・実データの顔 1,972 件。docs/history/details/2026-10-11-eyes-and-smile-measured.md）。

MediaPipe Face Landmarker の表情の値（blendshapes）を使う。

- 目閉じ: ``eyeBlinkLeft/Right`` の**小さいほう**（両目とも閉じている度合い）。
  片目だけのウインクは閉じたことにしない
- 笑顔: ``mouthSmileLeft/Right`` の平均

**元写真は読まない。** サムネイル（検出矩形の生クロップ）だけで測る。顔がきつく
切れているので、そのままでは Face Landmarker の検出器が顔を取りこぼす（実データで
5%）。**周りに黒い余白を足す**と 1.4% まで減った。

測るのは**使う顔だけ**（割り当てのある顔）。1件 約 40ms。

**モデル（`face_landmarker.task`）は pip の mediapipe に入っていない。**
`scripts/fetch_models.py` で `models/` に置く。無い環境では何も測らず、何も書かない
（「測れない」と「顔が取れなかった」を混ぜない。CLAUDE.md §8）。
"""

from __future__ import annotations

import io
import logging
from typing import Callable, NamedTuple, Optional

import numpy as np

logger = logging.getLogger("photoarchive.expression")

#: Face Landmarker のモデル。`face.find_model_file` で探す。
MODEL_FILE = "face_landmarker.task"

#: サムネイルの周りに足す余白（長辺に対する割合）。
PADDING = 0.25

#: 顔の検出の確信度の下限。サムネイルは顔だと分かっている画像なので低めにする。
MIN_DETECTION_CONFIDENCE = 0.3

#: 一度に読むサムネイルの件数。
FILL_CHUNK_SIZE = 500

_landmarker = None
_landmarker_initialized = False


class Expression(NamedTuple):
    """1つの顔の表情。顔が取れなかったときは ``found=False`` で、値は None。"""

    found: bool
    #: 両目の閉じ具合の小さいほう（0〜1）。
    eye_blink: Optional[float]
    #: 笑顔（左右の平均・0〜1）。
    mouth_smile: Optional[float]


NOT_FOUND = Expression(found=False, eye_blink=None, mouth_smile=None)


def _load_landmarker():
    """Face Landmarker を返す。**モデルが無い・読めなければ None。**"""
    global _landmarker, _landmarker_initialized
    if _landmarker_initialized:
        return _landmarker
    _landmarker_initialized = True
    from . import face

    path = face.find_model_file(MODEL_FILE)
    if path is None:
        logger.warning(
            "%s が見つからないので、目閉じと笑顔を測りません。"
            " `python scripts/fetch_models.py` で models/ に置けます。",
            MODEL_FILE,
        )
        return None
    try:
        from mediapipe.tasks.python import BaseOptions, vision  # type: ignore

        options = vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(path)),
            output_face_blendshapes=True,
            num_faces=1,
            min_face_detection_confidence=MIN_DETECTION_CONFIDENCE,
        )
        with face._suppress_mediapipe_output():
            _landmarker = vision.FaceLandmarker.create_from_options(options)
    except Exception as error:
        logger.exception("Face Landmarker initialization failed: %s", error)
        _landmarker = None
    return _landmarker


def reset_model_cache() -> None:
    global _landmarker, _landmarker_initialized
    _landmarker = None
    _landmarker_initialized = False


def pad(rgb: np.ndarray) -> np.ndarray:
    """周りに `PADDING` の黒い余白を足す。"""
    import cv2

    height, width = rgb.shape[:2]
    border = int(PADDING * max(height, width))
    return cv2.copyMakeBorder(
        np.ascontiguousarray(rgb), border, border, border, border, cv2.BORDER_CONSTANT, value=0
    )


def from_blendshapes(scores: dict) -> Expression:
    """表情の値（``{名前: 0〜1}``）から `Expression` を作る。"""
    blink = min(float(scores["eyeBlinkLeft"]), float(scores["eyeBlinkRight"]))
    smile = (float(scores["mouthSmileLeft"]) + float(scores["mouthSmileRight"])) / 2.0
    return Expression(found=True, eye_blink=blink, mouth_smile=smile)


def _blendshapes(rgb: np.ndarray) -> Optional[dict]:
    """余白を足した画像の表情の値。顔が取れなければ None。

    1か所に通すのは、テストのフェイクが差し替える口を1つにするため。
    """
    import mediapipe as mp  # type: ignore

    from . import face

    landmarker = _load_landmarker()
    image = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
    with face._suppress_mediapipe_output():
        result = landmarker.detect(image)
    if not result.face_blendshapes:
        return None
    return {item.category_name: item.score for item in result.face_blendshapes[0]}


def available() -> bool:
    """測れる環境か（モデルが読めるか）。"""
    return _load_landmarker() is not None


def measure(rgb: np.ndarray) -> Optional[Expression]:
    """顔画像（RGB）の表情。**測れない環境では None**、顔が取れなければ `NOT_FOUND`。"""
    if not available():
        return None
    try:
        scores = _blendshapes(pad(rgb))
    except Exception as error:
        logger.warning("Could not measure a face expression: %s", error)
        return None
    return NOT_FOUND if scores is None else from_blendshapes(scores)


def measure_thumbnail(thumbnail: bytes) -> Optional[Expression]:
    """保存済みサムネイル（JPEG）の表情。読めなければ None。"""
    from PIL import Image

    try:
        rgb = np.asarray(Image.open(io.BytesIO(thumbnail)).convert("RGB"))
    except Exception as error:  # 壊れたサムネイル
        logger.warning("Could not read a face thumbnail: %s", error)
        return None
    if rgb.size == 0:
        return None
    return measure(rgb)


def fill_missing(
    connection,
    assign_sources=None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> int:
    """表情が未計測の顔を測って書く。書いた件数を返す。

    ``assign_sources`` で対象を絞る（既定は手本と自動割り当て）。
    **測れない環境では何もしない**（全部を「顔が取れなかった」にすると、モデルを
    置いても二度と測り直されない）。壊れたサムネイルも書かない。
    """
    from . import db

    sources = assign_sources or (db.ASSIGN_MANUAL, db.ASSIGN_AUTO)
    # **サムネイルを全件まとめて読まない**（CLAUDE.md §8）。id だけ先に取る。
    face_ids = db.faces_without_expression(connection, sources)
    total = len(face_ids)
    if not total or not available():
        return 0
    if progress_callback is not None:
        progress_callback(0, total, "measuring expressions")
    written = 0
    done = 0
    for start in range(0, total, FILL_CHUNK_SIZE):
        chunk = face_ids[start : start + FILL_CHUNK_SIZE]
        rows = []
        for face_id, thumbnail in db.face_thumbnails(connection, chunk).items():
            result = measure_thumbnail(thumbnail) if thumbnail is not None else None
            if result is not None:
                rows.append((face_id, result))
        written += db.save_expressions(connection, rows)
        done += len(chunk)
        if progress_callback is not None:
            progress_callback(done, total, "measuring expressions")
    return written
