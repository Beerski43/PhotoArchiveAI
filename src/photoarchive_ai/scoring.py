"""写真の良し悪しを表すスコアの算出。

顔の特徴量(個人識別)とは別物なので ``face`` から分けている。ここは
MediaPipe の FaceMesh だけを使う。
"""

import logging
from typing import NamedTuple, Optional, Tuple

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
#: 146 → 37 枚、入れ替わり 112 枚（2026-10-12・#98 を含む develop と比べた値）。
#: **点は負になりうる**ので、`select` は家族の写った写真を先に並べる（`selection.select_media`）。
STRANGER_PENALTY = 30.0
#: 見え方が測れなかった顔（未計測）の値。**良いとも悪いとも言わない。**
UNKNOWN = 0.5

# --- 写真全体の良し悪し（#106・`photoarchive assess` が測る `PhotoQuality`）-----------------
#: 画質（MUSIQ・KonIQ）をこの範囲で 0〜1 に直す。
TECHNICAL_POOR = 40.0
TECHNICAL_GOOD = 75.0
#: 美しさ（NIMA・AVA）をこの範囲で 0〜1 に直す。
AESTHETIC_POOR = 4.0
AESTHETIC_GOOD = 5.5
#: 白飛び・黒つぶれの画素の割合がこれ以上で 1（減点いっぱい）。
CLIP_FULL = 0.25
#: 顔の矩形が写真の縁からこの割合（長辺に対して）以内なら、見切れとみなす。
CUTOFF_MARGIN = 0.005
#: 写真の重みの既定（#106・2026-10-16 に利用者が決めた）。実データの複製で `select` の出力 638 枚のうち
#: 152 枚が入れ替わる（外れた写真の画質の平均 63.6 → 入った写真 74.5）。
#: **白飛び・黒つぶれは 0**: 白背景の写真館の写真・ろうそくのケーキのような、わざとの白や黒を落とした。
#: **見切れも 0**: 落ちるのは自撮りと寄りの写真が多く、切れていない写真と混ざった。どちらも GUI で動かせる。
#: 測定は docs/history/details/2026-10-16-photo-quality-weights-measured.md。
WEIGHT_TECHNICAL = 0.3
WEIGHT_AESTHETIC = 0.1
WEIGHT_HIGHLIGHTS = 0.0
WEIGHT_SHADOWS = 0.0
WEIGHT_CUTOFF = 0.0


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


class Weights(NamedTuple):
    """並びの重み（#103）。**利用者がルールの ``weights:`` で変えられる。**

    既定（`Weights.default`）はこの module の定数。式を変えずに重みだけを動かせるよう、
    `select`（GUI の調整も）はここを通して点を出す。`match` の `family_score` は既定のまま。
    """

    sharpness: float
    frontal: float
    eyes: float
    smile: float
    member_bonus: float
    stranger_penalty: float
    #: 写真全体（#106）。画質・美しさは足し、白飛び・黒つぶれ・顔の見切れは引く（どれも 100 × 重み × 0〜1）。
    technical: float = WEIGHT_TECHNICAL
    aesthetic: float = WEIGHT_AESTHETIC
    highlights: float = WEIGHT_HIGHLIGHTS
    shadows: float = WEIGHT_SHADOWS
    cutoff: float = WEIGHT_CUTOFF

    @classmethod
    def default(cls) -> "Weights":
        """いまの定数から作る（呼んだときに読むので、定数の差し替えも効く）。"""
        return cls(
            WEIGHT_SHARPNESS, WEIGHT_FRONTAL, WEIGHT_EYES, WEIGHT_SMILE,
            MEMBER_BONUS, STRANGER_PENALTY,
            WEIGHT_TECHNICAL, WEIGHT_AESTHETIC, WEIGHT_HIGHLIGHTS, WEIGHT_SHADOWS, WEIGHT_CUTOFF,
        )


def _between(value: float, low: float, high: float) -> float:
    return max(0.0, min(1.0, (value - low) / (high - low)))


def technical_level(technical: Optional[float]) -> float:
    """画質（MUSIQ）を 0〜1 に。未計測は `UNKNOWN`。"""
    return UNKNOWN if technical is None else _between(technical, TECHNICAL_POOR, TECHNICAL_GOOD)


def aesthetic_level(aesthetic: Optional[float]) -> float:
    """美しさ（NIMA）を 0〜1 に。未計測は `UNKNOWN`。"""
    return UNKNOWN if aesthetic is None else _between(aesthetic, AESTHETIC_POOR, AESTHETIC_GOOD)


def clip_level(share: Optional[float]) -> float:
    """白飛び・黒つぶれの割合を 0〜1 に。**未計測は 0**（分からない欠点で引かない）。"""
    return 0.0 if share is None else _between(share, 0.0, CLIP_FULL)


def is_cut_off(face, width: Optional[int], height: Optional[int]) -> bool:
    """顔の矩形が写真の縁に接しているか。``face`` は `db.family_faces` の行。大きさが分からなければ False。"""
    if not width or not height:
        return False
    margin = CUTOFF_MARGIN * max(width, height)
    return (
        face["bbox_left"] <= margin
        or face["bbox_top"] <= margin
        or face["bbox_right"] >= width - margin
        or face["bbox_bottom"] >= height - margin
    )


def cutoff_level(faces, quality) -> float:
    """家族の顔のうち、写真の縁で切れている顔の割合（0〜1）。未計測は 0。"""
    if not faces or not quality:
        return 0.0
    cut = sum(is_cut_off(face, quality.get("width"), quality.get("height")) for face in faces)
    return cut / len(faces)


def photo_parts(faces, quality, weights: "Weights") -> dict:
    """写真全体の点（#106）。``quality`` は `db.photo_quality` の1行（未計測は None）。"""
    quality = quality or {}
    return {
        "technical": 100.0 * weights.technical * technical_level(quality.get("technical")),
        "aesthetic": 100.0 * weights.aesthetic * aesthetic_level(quality.get("aesthetic")),
        "highlights": -100.0 * weights.highlights * clip_level(quality.get("highlight_clip")),
        "shadows": -100.0 * weights.shadows * clip_level(quality.get("shadow_clip")),
        "cutoff": -100.0 * weights.cutoff * cutoff_level(faces, quality),
    }


def family_face_score(
    aligned: Optional[int],
    yaw: Optional[float],
    sharpness: Optional[float],
    eye_blink: Optional[float],
    mouth_smile: Optional[float],
    weights: Optional[Weights] = None,
) -> float:
    """家族の顔1つの良さ（既定の重みで 0〜100）。"""
    weights = weights or Weights.default()
    return 100.0 * (
        weights.sharpness * sharpness_level(sharpness)
        + weights.frontal * frontal_level(aligned, yaw)
        + weights.eyes * eyes_level(eye_blink)
        + weights.smile * smile_level(mouth_smile)
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


def family_photo_score(faces, weights: Optional[Weights] = None) -> float:
    """写真1枚の家族写真としての良さ。``faces`` はその写真の**家族の顔だけ**。

    いちばん良い家族の顔の点に、はっきり写った家族の人数ぶん（2人目から）
    `MEMBER_BONUS` を足す。家族の顔が無ければ 0。

    各要素は ``person_id`` / ``aligned`` / ``yaw`` / ``sharpness`` / ``eye_blink`` /
    ``mouth_smile`` を持つ辞書（`db.family_faces` の行）。
    """
    if not faces:
        return 0.0
    weights = weights or Weights.default()
    best = max(
        family_face_score(
            face["aligned"], face["yaw"], face["sharpness"], face["eye_blink"],
            face["mouth_smile"], weights,
        )
        for face in faces
    )
    members = {
        face["person_id"]
        for face in faces
        if is_clear_face(face["aligned"], face["sharpness"], face["eye_blink"])
    }
    return best + weights.member_bonus * max(0, len(members) - 1)


#: 内訳の項目（`photo_breakdown`）。顔・人数・他人のあとに写真全体（#106）。
BREAKDOWN_KEYS = (
    "sharpness", "frontal", "eyes", "smile", "members", "strangers",
    "technical", "aesthetic", "highlights", "shadows", "cutoff",
)


def photo_breakdown(
    faces, stranger_weight: float = 0.0, weights: Optional[Weights] = None, quality=None
) -> dict:
    """写真1枚の点の**内訳**（#104。GUI で「なぜこの順位か」を見せる）。

    ``family_photo_score(faces, weights) - weights.stranger_penalty * stranger_weight`` に
    写真全体の点（`photo_parts`・#106）を足した式を項目に分けたもの。``total`` は各項目の和で、
    `family_photo_scores` の点と一致する。家族の顔が無ければ全部 0。
    """
    weights = weights or Weights.default()
    parts = {key: 0.0 for key in BREAKDOWN_KEYS}
    if faces:
        best = max(
            faces,
            key=lambda face: family_face_score(
                face["aligned"], face["yaw"], face["sharpness"], face["eye_blink"],
                face["mouth_smile"], weights,
            ),
        )
        parts["sharpness"] = 100.0 * weights.sharpness * sharpness_level(best["sharpness"])
        parts["frontal"] = 100.0 * weights.frontal * frontal_level(best["aligned"], best["yaw"])
        parts["eyes"] = 100.0 * weights.eyes * eyes_level(best["eye_blink"])
        parts["smile"] = 100.0 * weights.smile * smile_level(best["mouth_smile"])
        members = {
            face["person_id"]
            for face in faces
            if is_clear_face(face["aligned"], face["sharpness"], face["eye_blink"])
        }
        parts["members"] = weights.member_bonus * max(0, len(members) - 1)
        parts["strangers"] = -weights.stranger_penalty * stranger_weight
        parts.update(photo_parts(faces, quality, weights))
    parts["total"] = sum(parts.values())
    return parts


def family_photo_scores(
    rows, strangers=None, weights: Optional[Weights] = None, quality=None
) -> dict:
    """``{media_id: 点}``。``rows`` は `db.family_faces` の結果。

    ``strangers`` は写真ごとの目立つ他人の重み（`strangers.weights`。1人で最大 1）。
    重み 1 につき `STRANGER_PENALTY` を引く（#100。利用者の要望「家族以外の顔がある場合は、
    select の対象になりにくいように」）。``quality`` は写真ごとの良し悪し（`db.photo_quality`・#106）。
    写真全体の重みがすべて 0 なら、#106 の前と同じ点になる。
    """
    strangers = strangers or {}
    quality = quality or {}
    weights = weights or Weights.default()
    by_media: dict = {}
    for row in rows:
        by_media.setdefault(row["media_id"], []).append(row)
    scores = {}
    for media_id, faces in by_media.items():
        score = family_photo_score(faces, weights) - weights.stranger_penalty * strangers.get(media_id, 0)
        score += sum(photo_parts(faces, quality.get(media_id), weights).values())
        scores[media_id] = score
    return scores
