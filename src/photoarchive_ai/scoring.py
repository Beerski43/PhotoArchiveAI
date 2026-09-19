"""写真の良し悪しを表すスコアの算出。

顔の特徴量(個人識別)とは別物なので ``face`` から分けている。ここは
MediaPipe の FaceMesh だけを使う。
"""

import logging
from typing import Optional, Tuple

import cv2
import numpy as np

from .face import _suppress_mediapipe_output

logger = logging.getLogger("photoarchive.scoring")

_face_mesh = None
_face_mesh_initialized = False

SIMILARITY_REFERENCE_DISTANCE = 0.6


def _load_mediapipe_face_mesh():
    """Load mediapipe face mesh with fallback."""
    global _face_mesh, _face_mesh_initialized
    if _face_mesh_initialized:
        return _face_mesh
    try:
        import mediapipe as mp  # type: ignore

        with _suppress_mediapipe_output():
            _face_mesh = mp.solutions.face_mesh.FaceMesh(
                static_image_mode=True,
                max_num_faces=1,
                min_detection_confidence=0.5,
            )
        _face_mesh_initialized = True
        return _face_mesh
    except Exception as error:
        _face_mesh_initialized = True
        logger.exception("MediaPipe face mesh initialization failed: %s", error)
        return None


def reset_model_cache() -> None:
    global _face_mesh, _face_mesh_initialized
    _face_mesh = None
    _face_mesh_initialized = False


def distance_to_similarity(distance: float) -> float:
    """顔特徴量の距離を 0-100 の類似度に直す。"""
    normalized = max(0.0, min(1.0, 1.0 - distance / SIMILARITY_REFERENCE_DISTANCE))
    return normalized * 100.0


def estimate_smile_score(rgb: np.ndarray, face_location: Tuple[int, int, int, int]) -> float:
    """口角のランドマークの縦横比から笑顔らしさを推定する。"""
    mesh = _load_mediapipe_face_mesh()
    if mesh is None:
        return 0.0
    try:
        top, right, bottom, left = face_location
        face_rgb = np.ascontiguousarray(rgb[top:bottom, left:right])
        if face_rgb.size == 0:
            return 0.0
        with _suppress_mediapipe_output():
            results = mesh.process(face_rgb)
        if not results.multi_face_landmarks:
            return 0.0
        landmarks = results.multi_face_landmarks[0].landmark
        h, w = rgb.shape[:2]
        mouth_corners = [landmarks[61], landmarks[291]]
        mouth_center_upper = [landmarks[78], landmarks[308]]
        xs = [lm.x * w for lm in mouth_corners + mouth_center_upper]
        ys = [lm.y * h for lm in mouth_corners + mouth_center_upper]
        width = max(xs) - min(xs)
        height = max(ys) - min(ys)
        if height <= 0:
            return 0.0
        ratio = width / height
        return min(100.0, max(0.0, (ratio - 1.4) * 70.0))
    except Exception:
        return 0.0


def estimate_quality(rgb: np.ndarray, face_location: Tuple[int, int, int, int]) -> float:
    """顔領域の明るさと大きさから、素材としての扱いやすさを表すスコアを出す。"""
    top, right, bottom, left = face_location
    face_region = rgb[top:bottom, left:right]
    if face_region.size == 0:
        return 0.0
    gray = cv2.cvtColor(face_region, cv2.COLOR_RGB2GRAY)
    brightness = float(np.mean(gray)) / 255.0
    image_area = float(rgb.shape[0] * rgb.shape[1])
    face_area = float(max(1, (bottom - top) * (right - left)))
    size_ratio = min(1.0, face_area / (image_area * 0.12))
    return min(100.0, brightness * 60.0 + size_ratio * 40.0)


def score_face(
    rgb: np.ndarray,
    face_location: Tuple[int, int, int, int],
) -> Tuple[float, float]:
    """(smile_score, quality_score) をまとめて返す。"""
    return estimate_smile_score(rgb, face_location), estimate_quality(rgb, face_location)


def aggregate_media_scores(
    face_scores: Optional[list],
) -> Tuple[Optional[float], Optional[float]]:
    """メディア単位のスコアは、写っている顔の最良値を採用する。"""
    if not face_scores:
        return 0.0, 0.0
    smile = max(score[0] for score in face_scores)
    quality = max(score[1] for score in face_scores)
    return smile, quality
