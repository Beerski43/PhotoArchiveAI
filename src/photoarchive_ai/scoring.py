"""写真の良し悪しを表すスコアの算出。

顔の特徴量(個人識別)とは別物なので ``face`` から分けている。ここは
MediaPipe の FaceMesh だけを使う。
"""

import logging
from typing import Optional, Tuple

import cv2
import numpy as np

from . import embedding
from .face import _suppress_mediapipe_output

logger = logging.getLogger("photoarchive.scoring")

_face_mesh = None
_face_mesh_initialized = False

#: `distance_to_similarity` が 0 を返す距離。**モデルの属性。書き写さない。**
#: 尺度がモデルごとに違う（dlib のユークリッドは実質 0〜1.5、ArcFace の
#: コサインは 0〜2.0）ので、固定すると類似度の意味がモデルで変わる。
SIMILARITY_REFERENCE_DISTANCE = embedding.ACTIVE.similarity_reference


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


# ---------------------------------------------------------------------------
# 家族写真としての良さ（`select` の並び。#66 と同じブランチで、利用者の要望）
# ---------------------------------------------------------------------------
#
# 利用者「せっかく本人が映ってる写真を select で選んでくれてても、ぼやけてたら
# 意味ないでしょ？」「select でも家族の写真としてスコアの高いものを選んでほしい」。
#
# **写真のスコアは家族の顔だけから作る。** 以前は写真の笑顔・画質を「写っている
# 顔の最良値」で持っていたので、隣の他人がくっきり笑っていれば、ボケた家族の
# 写真が上位に来た。
#
# 重く見るものは利用者が選んだ（2026-10-09）: **ボケていない・正面・笑顔**。
# 家族が何人も写っている写真は優先する。**ただしボケた家族の顔は人数に数えない。**
# 目閉じを足し、笑顔を表情の値に替えた（#95・2026-10-11）。**目を閉じた家族も
# 人数に数えない**（利用者の決定。ボケと同じ扱い）。
#
# 目盛りは実データの手本 10,701 件を目で見て決めた（鮮明さ・向きの帯ごとに
# サムネイルを並べた。docs/history/details/2026-10-09-age-threshold-measured.md）。

#: 鮮明さ（`appearance.sharpness_of`）がこれ以下なら 0。**はっきりボケている。**
SHARPNESS_BLURRY = 30.0
#: これ以上なら 1。**くっきりしている。** 赤ちゃんの肌はなめらかで、ピントが
#: 合っていても値が低めに出る（75〜110 の帯はピントの合った赤ちゃんが多い）ので、
#: 上限を高くしすぎない。間は対数で結ぶ。
SHARPNESS_CRISP = 150.0
#: 向き（`appearance.yaw_from_points`）がこれ以下なら正面（1）。
YAW_FRONTAL = 0.1
#: これ以上なら横顔（0）。0.45 を超えるとほぼ横顔だった。
YAW_PROFILE = 0.6
#: 目閉じ（`expression` の ``eye_blink``・両目の小さいほう）がこれ以下なら開いている（1）。
#: 実データの顔を帯ごとに並べて決めた（#95・2026-10-11）。0.3 未満はほぼ開いていた。
EYES_OPEN = 0.3
#: これ以上なら閉じている（0）。0.5 以上はほぼ全部が寝顔・伏し目だった。
#: 0.4〜0.5 には大笑いで目が細くなった顔が少し混じるので、間は直線で結ぶ。
EYES_CLOSED = 0.5
#: 笑顔（`expression` の ``mouth_smile``）がこれ以上なら満点。0.25 以上ははっきり
#: 笑っていた。0.02 未満は無表情。
SMILE_FULL = 0.3
#: 重み（利用者の決定・#95）。笑顔は以前いちばん軽かった（0.25）。古い
#: `estimate_smile_score` は無表情の顔の 86% に 100 を付けていて見分ける力が
#: 無かったため。表情の値に替えて、目と並べて 0.20 ずつ。
WEIGHT_SHARPNESS = 0.35
WEIGHT_FRONTAL = 0.25
WEIGHT_EYES = 0.20
WEIGHT_SMILE = 0.20
#: 「はっきり写った家族」と数える鮮明さ（0〜1）の下限。30→0・150→1 の対数目盛りで
#: 約 57。整列できない顔（横顔・見切れ）も数えない。
CLEAR_SHARPNESS = 0.4
#: はっきり写った家族が1人増えるごとの加点。
#:
#: **20（#95・利用者の決定）。** 以前は 10 だった。古い笑顔はほぼ全員 100 で、
#: 写真どうしの差を作っていたのはこの加点だった。表情の値で笑顔が効くように
#: なると（最大 20）、**1人だけ写った笑顔の写真が家族のそろった写真を押しのけた**
#: （実データの複製で出力 638 枚のうち1人の写真が 97 → 240 枚）。20 で 127 枚。
MEMBER_BONUS = 20.0
#: **目立つ他人**の重み 1 あたりに引く点（#100・利用者の決定）。家族に似ていない・家族と
#: 同じくらい大きく写った他人1人で、この点を引く（`strangers.weights`）。家族1人の加点
#: （`MEMBER_BONUS`）より重い。実データの複製で、出力 638 枚のうち重みのある写真が
#: 139 → 37 枚、入れ替わり 104 枚（2026-10-12）。
STRANGER_PENALTY = 30.0
#: 見え方が測れなかった顔（未計測）の値。**良いとも悪いとも言わない。**
UNKNOWN = 0.5


def sharpness_level(sharpness: Optional[float]) -> float:
    """鮮明さを 0〜1 に直す（対数目盛り）。未計測は `UNKNOWN`。"""
    if sharpness is None:
        return UNKNOWN
    if sharpness <= SHARPNESS_BLURRY:
        return 0.0
    if sharpness >= SHARPNESS_CRISP:
        return 1.0
    return float(
        np.log(sharpness / SHARPNESS_BLURRY) / np.log(SHARPNESS_CRISP / SHARPNESS_BLURRY)
    )


def frontal_level(aligned: Optional[int], yaw: Optional[float]) -> float:
    """正面らしさを 0〜1 に直す。**整列できない顔は 0**（横顔・見切れ）。未計測は `UNKNOWN`。"""
    if aligned is None:
        return UNKNOWN
    if not aligned or yaw is None:
        return 0.0
    if yaw <= YAW_FRONTAL:
        return 1.0
    if yaw >= YAW_PROFILE:
        return 0.0
    return 1.0 - (yaw - YAW_FRONTAL) / (YAW_PROFILE - YAW_FRONTAL)


def eyes_level(eye_blink: Optional[float]) -> float:
    """目の開き具合を 0〜1 に直す（開いていて 1）。未計測は `UNKNOWN`。"""
    if eye_blink is None:
        return UNKNOWN
    if eye_blink <= EYES_OPEN:
        return 1.0
    if eye_blink >= EYES_CLOSED:
        return 0.0
    return 1.0 - (eye_blink - EYES_OPEN) / (EYES_CLOSED - EYES_OPEN)


def smile_level(mouth_smile: Optional[float]) -> float:
    """笑顔を 0〜1 に直す。未計測は `UNKNOWN`。"""
    if mouth_smile is None:
        return UNKNOWN
    return max(0.0, min(1.0, mouth_smile / SMILE_FULL))


def family_face_score(
    aligned: Optional[int],
    yaw: Optional[float],
    sharpness: Optional[float],
    eye_blink: Optional[float],
    mouth_smile: Optional[float],
) -> float:
    """家族の顔1つの良さ（0〜100）。"""
    return 100.0 * (
        WEIGHT_SHARPNESS * sharpness_level(sharpness)
        + WEIGHT_FRONTAL * frontal_level(aligned, yaw)
        + WEIGHT_EYES * eyes_level(eye_blink)
        + WEIGHT_SMILE * smile_level(mouth_smile)
    )


def is_clear_face(
    aligned: Optional[int], sharpness: Optional[float], eye_blink: Optional[float] = None
) -> bool:
    """「はっきり写った家族」として人数に数えるか。

    **鮮明さが未計測なら数えない。** 目閉じは**測って閉じていたときだけ**外す
    （モデルの無い環境で全員を外さないため。分からないものを外さない）。
    """
    if eye_blink is not None and eye_blink >= EYES_CLOSED:
        return False
    return bool(aligned) and sharpness is not None and (
        sharpness_level(sharpness) >= CLEAR_SHARPNESS
    )


def family_photo_score(faces) -> float:
    """写真1枚の家族写真としての良さ。``faces`` はその写真の**家族の顔だけ**。

    いちばん良い家族の顔の点に、はっきり写った家族の人数ぶん（2人目から）
    `MEMBER_BONUS` を足す。家族の顔が無ければ 0。

    各要素は ``person_id`` / ``aligned`` / ``yaw`` / ``sharpness`` / ``eye_blink`` /
    ``mouth_smile`` を持つ辞書（`db.family_faces` の行）。
    """
    if not faces:
        return 0.0
    best = max(
        family_face_score(
            face["aligned"], face["yaw"], face["sharpness"], face["eye_blink"], face["mouth_smile"]
        )
        for face in faces
    )
    members = {
        face["person_id"]
        for face in faces
        if is_clear_face(face["aligned"], face["sharpness"], face["eye_blink"])
    }
    return best + MEMBER_BONUS * max(0, len(members) - 1)


def family_photo_scores(rows, strangers=None) -> dict:
    """``{media_id: 点}``。``rows`` は `db.family_faces` の結果。

    ``strangers`` は写真ごとの目立つ他人の重み（`strangers.weights`。1人で最大 1）。
    重み 1 につき `STRANGER_PENALTY` を引く（#100。利用者の要望「家族以外の顔がある場合は、
    select の対象になりにくいように」）。
    """
    strangers = strangers or {}
    by_media: dict = {}
    for row in rows:
        by_media.setdefault(row["media_id"], []).append(row)
    return {
        media_id: family_photo_score(faces) - STRANGER_PENALTY * strangers.get(media_id, 0)
        for media_id, faces in by_media.items()
    }
