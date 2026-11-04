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
# ---------------------------------------------------------------------------
# 家族写真としての良さ（`select` の並び）
# ---------------------------------------------------------------------------
#
# **#139 で式を作り直した**（利用者「顔個々にスコアを出して、それを加算していく。他人は減算していく。
# その結果と、写真全体の結果を算出し、どちらに重きを置くかを比重で調整したい」2026-11-03）。
#
#     顔の点     = 100 × Σ(項目の重み × 項目の値) / Σ(項目の重み)          … 0〜100
#                  項目: 鮮明さ・正面・目・笑顔・大きさ・見切れていない
#     家族の加点 = 人物ごとに最良の顔の点 × 人物の重み を高い順に並べ、k 人目に extra_person^(k−1) をかけて足す
#     他人の減点 = Σ(他人(減点)の顔の点) × strangers
#     写真全体   = 100 × Σ(項目の重み × 項目の値) / Σ(項目の重み)          … 0〜100
#                  項目: 画質・美しさ・白飛びしていない・黒つぶれしていない
#     総合       = (家族の加点 − 他人の減点) × face_ratio / 100 + 写真全体 × (100 − face_ratio) / 100
#
# 以前（#66〜#110）は**いちばん良い家族の顔1つ**の点に、はっきり写った家族の人数の加点（`member_bonus`）、
# 他人(減点)の顔の**大きさだけ**の減点、写真全体の項目をそのまま足していた。2人目からの顔の良さも、顔の大きさも
# 点に入らず、顔と写真全体のどちらに寄せるかを1つの値で動かせなかった。
#
# **項目の重みは「比」**（和で割る）。顔の点も写真全体の点も 0〜100 にそろうので、`face_ratio` が
# 「顔と写真全体のどちらを見るか」だけを表す。
#
# **家族の加点は人数が多いほど伸びるが、2人目以降は薄める**（`extra_person`。PR #140 のレビュー後の利用者の決定）。
# Σ をそのまま足すと出力 502 枚のうち1人の写真が 186 → 26 枚に減り、`face_ratio` で戻すには 10 前後が要って顔の良さが
# 効かなくなった。人数の平方根で割る形にしなかったのは、ボケた家族が増えると点が下がるため。**順に薄める形なら、
# 家族が増えて点が下がることはない。** **同じ人物の顔が複数あれば、いちばん良い顔だけを足す**（写真の中の写真や
# 割り当ての誤りで点が伸びないように・PR #140 の指摘1・利用者の決定）。
#
# 鮮明さ・向き・目・笑顔の目盛りは実データの手本 10,701 件を目で見て決めた（鮮明さ・向きの帯ごとに
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
#: 顔の大きさ: 顔の一辺（矩形の面積の平方根）を、**その写真でいちばん大きい顔**（検出したすべての顔）と比べる
#: （#139・利用者の決定。写真の縦横は `assess` で測った写真にしか無いため）。この割合以下は 0、`SIZE_LARGE` 以上は 1。
#: 目盛りは #100 の他人の「目立ち具合」と同じ（背景に小さく写っただけの顔は 0）。
SIZE_SMALL = 0.3
SIZE_LARGE = 1.0
#: 顔の矩形が写真の縁からこの割合（長辺に対して）以内なら、見切れとみなす。
CUTOFF_MARGIN = 0.005
#: 見え方・表情・画質が測れなかった（未計測の）ときの値。**良いとも悪いとも言わない。**
UNKNOWN = 0.5

# --- 写真全体の良し悪し（#106・`photoarchive assess` が測る `PhotoQuality`）-----------------
#: 画質（MUSIQ・KonIQ）をこの範囲で 0〜1 に直す。
TECHNICAL_POOR = 40.0
TECHNICAL_GOOD = 75.0
#: 美しさ（NIMA・AVA）をこの範囲で 0〜1 に直す。
AESTHETIC_POOR = 4.0
AESTHETIC_GOOD = 5.5
#: 白飛び・黒つぶれの画素の割合がこれ以上で 1（「していない度」は 0）。
CLIP_FULL = 0.25

# --- 重みの既定 ----------------------------------------------------------------------------
#: 顔の項目の比（#139）。鮮明さ・正面・目・笑顔は #95 の比（0.35・0.25・0.20・0.20）を縮めて、
#: 大きさと見切れに場所を空けた。
WEIGHT_SHARPNESS = 0.30
WEIGHT_FRONTAL = 0.20
WEIGHT_EYES = 0.15
WEIGHT_SMILE = 0.15
WEIGHT_SIZE = 0.15
WEIGHT_CUTOFF = 0.05
#: 写真全体の項目の比（#106 の既定。2026-10-16 に利用者が決めた）。**白飛び・黒つぶれは 0**: 白背景の写真館の
#: 写真・ろうそくのケーキのような、わざとの白や黒を落とした。測定は
#: docs/history/details/2026-10-16-photo-quality-weights-measured.md。
WEIGHT_TECHNICAL = 0.3
WEIGHT_AESTHETIC = 0.1
WEIGHT_HIGHLIGHTS = 0.0
WEIGHT_SHADOWS = 0.0
#: 他人(減点)の顔の点にかける重み（#139）。1 なら、同じ写りの家族（1人目）の加点をちょうど打ち消す。
WEIGHT_STRANGERS = 1.0
#: 比重（0〜100・#139）。顔の側（家族の加点 − 他人の減点）にかける割合。残りが写真全体。
#: 以前の既定で、家族1人の写真の満点に占める写真全体の点（画質 30・美しさ 10）がおよそ 3 割だったので 70。
#: **1人の写真でだけ成り立つ目安**で、人数が増えると顔の側の割合はこれより大きくなる。
FACE_RATIO = 70.0
#: 2人目以降の効き（0〜1）。k 人目の顔の点に、この値の k−1 乗をかけて足す。1 なら全員を同じだけ足し、0 なら最良の1人だけ。
EXTRA_PERSON = 0.5

#: 顔の項目と写真全体の項目（`Weights` のキー・内訳のキー）。
FACE_ITEMS = ("sharpness", "frontal", "eyes", "smile", "size", "cutoff")
PHOTO_ITEMS = ("technical", "aesthetic", "highlights", "shadows")


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


def face_size(face) -> float:
    """検出矩形の面積の平方根（顔の一辺）。``face`` は ``bbox_*`` を持つ行。"""
    height = max(0, face["bbox_bottom"] - face["bbox_top"])
    width = max(0, face["bbox_right"] - face["bbox_left"])
    return float(np.sqrt(height * width))


def size_level(size: float, largest: Optional[float]) -> float:
    """顔の大きさを、写真でいちばん大きい顔と比べて 0〜1 に直す。``largest`` が分からなければ 1（比べる相手がいない）。"""
    if not largest or largest <= 0:
        return 1.0
    ratio = size / largest
    if ratio <= SIZE_SMALL:
        return 0.0
    if ratio >= SIZE_LARGE:
        return 1.0
    return (ratio - SIZE_SMALL) / (SIZE_LARGE - SIZE_SMALL)


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


def _between(value: float, low: float, high: float) -> float:
    return max(0.0, min(1.0, (value - low) / (high - low)))


def technical_level(technical: Optional[float]) -> float:
    """画質（MUSIQ）を 0〜1 に。未計測は `UNKNOWN`。"""
    return UNKNOWN if technical is None else _between(technical, TECHNICAL_POOR, TECHNICAL_GOOD)


def aesthetic_level(aesthetic: Optional[float]) -> float:
    """美しさ（NIMA）を 0〜1 に。未計測は `UNKNOWN`。"""
    return UNKNOWN if aesthetic is None else _between(aesthetic, AESTHETIC_POOR, AESTHETIC_GOOD)


def clip_level(share: Optional[float]) -> float:
    """白飛び・黒つぶれの割合を 0〜1 に（1 がいっぱい）。**未計測は 0**（分からない欠点で引かない）。"""
    return 0.0 if share is None else _between(share, 0.0, CLIP_FULL)


class Weights(NamedTuple):
    """並びの重み（#103・#139）。**利用者がルールの ``weights:`` で変えられる。**

    既定（`Weights.default`）はこの module の定数。`select`（GUI の調整も）と `match` の `family_score` は
    ここを通して点を出す。顔の項目どうし・写真全体の項目どうしは**比**（和で割る）。
    """

    sharpness: float = WEIGHT_SHARPNESS
    frontal: float = WEIGHT_FRONTAL
    eyes: float = WEIGHT_EYES
    smile: float = WEIGHT_SMILE
    size: float = WEIGHT_SIZE
    cutoff: float = WEIGHT_CUTOFF
    strangers: float = WEIGHT_STRANGERS
    technical: float = WEIGHT_TECHNICAL
    aesthetic: float = WEIGHT_AESTHETIC
    highlights: float = WEIGHT_HIGHLIGHTS
    shadows: float = WEIGHT_SHADOWS
    face_ratio: float = FACE_RATIO
    extra_person: float = EXTRA_PERSON

    @classmethod
    def default(cls) -> "Weights":
        """いまの定数から作る（呼んだときに読むので、定数の差し替えも効く）。"""
        return cls(
            WEIGHT_SHARPNESS, WEIGHT_FRONTAL, WEIGHT_EYES, WEIGHT_SMILE, WEIGHT_SIZE, WEIGHT_CUTOFF,
            WEIGHT_STRANGERS,
            WEIGHT_TECHNICAL, WEIGHT_AESTHETIC, WEIGHT_HIGHLIGHTS, WEIGHT_SHADOWS,
            FACE_RATIO, EXTRA_PERSON,
        )

    def ratio(self) -> float:
        """顔の側の割合（0〜1）。"""
        return max(0.0, min(100.0, self.face_ratio)) / 100.0


def _share(levels: dict, weights: Weights, items) -> dict:
    """項目ごとの点（0〜100 の点を重みの比で分けたもの）。重みの和が 0 なら全部 0。"""
    total = sum(getattr(weights, key) for key in items)
    if total <= 0:
        return {key: 0.0 for key in items}
    return {key: 100.0 * getattr(weights, key) * levels[key] / total for key in items}


def face_levels(face, largest: Optional[float] = None, quality=None) -> dict:
    """顔1つの項目の値（0〜1）。``face`` は見え方・表情・矩形を持つ行（`db.family_faces` など）。

    ``largest`` はその写真でいちばん大きい顔の一辺、``quality`` は写真の良し悪しの行（縦横を見る）。
    """
    quality = quality or {}
    return {
        "sharpness": sharpness_level(face.get("sharpness")),
        "frontal": frontal_level(face.get("aligned"), face.get("yaw")),
        "eyes": eyes_level(face.get("eye_blink")),
        "smile": smile_level(face.get("mouth_smile")),
        "size": size_level(face_size(face), largest),
        "cutoff": 0.0 if is_cut_off(face, quality.get("width"), quality.get("height")) else 1.0,
    }


def face_parts(face, weights: Optional[Weights] = None, largest=None, quality=None) -> dict:
    """顔1つの点の項目ごとの内訳（和が顔の点・0〜100）。"""
    return _share(face_levels(face, largest, quality), weights or Weights.default(), FACE_ITEMS)


def face_score(face, weights: Optional[Weights] = None, largest=None, quality=None) -> float:
    """顔1つの点（0〜100）。人物の重みはかけない。"""
    return sum(face_parts(face, weights, largest, quality).values())


def photo_levels(quality) -> dict:
    """写真全体の項目の値（0〜1）。``quality`` は `db.photo_quality` の1行（未計測は None）。"""
    quality = quality or {}
    return {
        "technical": technical_level(quality.get("technical")),
        "aesthetic": aesthetic_level(quality.get("aesthetic")),
        "highlights": 1.0 - clip_level(quality.get("highlight_clip")),
        "shadows": 1.0 - clip_level(quality.get("shadow_clip")),
    }


def photo_parts(quality, weights: Optional[Weights] = None) -> dict:
    """写真全体の点の項目ごとの内訳（和が写真全体の点・0〜100）。"""
    return _share(photo_levels(quality), weights or Weights.default(), PHOTO_ITEMS)


def _person_weight(face) -> float:
    """顔の人物の重み（`Person.weight`・#110）。行に無ければ 1.0（重みを付けていない人物）。"""
    value = face.get("person_weight")
    return 1.0 if value is None else float(value)


def _has_family(faces) -> bool:
    """家族の顔があるか（`db.family_faces` の ``close`` が偽の顔・#110）。親しい人だけの写真は家族の写真ではない。"""
    return any(not face.get("close") for face in faces)


def _best_face_per_person(faces, weights: Weights, largest, quality) -> list:
    """同じ人物の顔が複数あれば、いちばん点の高い顔だけを残す。"""
    best: dict = {}
    for face in faces:
        person = face.get("person_id")
        if person not in best or face_score(face, weights, largest, quality) > face_score(
            best[person], weights, largest, quality
        ):
            best[person] = face
    return list(best.values())


def largest_face(faces) -> float:
    """``faces`` のうち、いちばん大きい顔の一辺。顔が無ければ 0。"""
    return max((face_size(face) for face in faces), default=0.0)


#: 内訳の項目（`photo_breakdown`）。顔の項目（家族と親しい人の顔の和）・他人(減点)・写真全体の項目。
BREAKDOWN_KEYS = FACE_ITEMS + ("strangers",) + PHOTO_ITEMS


def photo_breakdown(
    faces,
    stranger_faces=(),
    weights: Optional[Weights] = None,
    quality=None,
    largest: Optional[float] = None,
) -> dict:
    """写真1枚の点の**内訳**（#104・#139。GUI で「なぜこの順位か」を見せる）。``total`` は各項目の和。

    - ``faces``: その写真の割り当てのある顔（家族と親しい人・`db.family_faces` の行）
    - ``stranger_faces``: その写真の他人(減点)の顔（`strangers.faces` の行）
    - ``quality``: 写真の良し悪し（`db.photo_quality` の1行）
    - ``largest``: その写真でいちばん大きい顔の一辺（`db.largest_face_sizes`）。渡さなければ ``faces`` と
      ``stranger_faces`` の中から取る

    顔の項目（人物ごとの最良の顔を高い順に `extra_person` で薄めた和）と他人(減点)には `face_ratio`、
    写真全体の項目には残りをかけてある。**家族の顔が無ければ全部 0**
    （親しい人だけの写真は家族の写真ではない・#110）。
    """
    weights = weights or Weights.default()
    parts = {key: 0.0 for key in BREAKDOWN_KEYS}
    if _has_family(faces):
        if largest is None:
            largest = largest_face(list(faces) + list(stranger_faces))
        ratio = weights.ratio()
        # 高い順に足すので、家族が増えても点は下がらない（重み 0 の人物は最後に回って何も足さない）
        people = sorted(
            _best_face_per_person(faces, weights, largest, quality),
            key=lambda face: _person_weight(face) * face_score(face, weights, largest, quality),
            reverse=True,
        )
        for rank, face in enumerate(people):
            scale = ratio * _person_weight(face) * weights.extra_person ** rank
            for key, value in face_parts(face, weights, largest, quality).items():
                parts[key] += scale * value
        parts["strangers"] = -ratio * weights.strangers * sum(
            face_score(face, weights, largest, quality) for face in stranger_faces
        )
        for key, value in photo_parts(quality, weights).items():
            parts[key] = (1.0 - ratio) * value
    parts["total"] = sum(parts[key] for key in BREAKDOWN_KEYS)
    return parts


def family_photo_score(
    faces, weights: Optional[Weights] = None, stranger_faces=(), quality=None, largest=None
) -> float:
    """写真1枚の家族写真としての良さ（`photo_breakdown` の ``total``）。家族の顔が無ければ 0。

    各要素は ``person_id`` / ``aligned`` / ``yaw`` / ``sharpness`` / ``eye_blink`` / ``mouth_smile`` /
    ``bbox_*`` を持つ辞書（`db.family_faces` の行）。``person_weight`` と ``close`` は無ければ 1.0・家族とみなす。
    """
    return photo_breakdown(faces, stranger_faces, weights, quality, largest)["total"]


def family_photo_scores(
    rows, strangers=None, weights: Optional[Weights] = None, quality=None, largest=None
) -> dict:
    """``{media_id: 点}``。``rows`` は `db.family_faces` の結果（家族と親しい人の顔）。

    **家族の顔のある写真だけ**に点を付ける（親しい人だけの写真は入らない・#110）。
    ``strangers`` は写真ごとの他人(減点)の顔（``{media_id: [行]}``・`strangers.faces`）、
    ``quality`` は写真ごとの良し悪し（`db.photo_quality`・#106）、``largest`` は写真ごとのいちばん大きい顔の
    一辺（`db.largest_face_sizes`。無い写真は顔の行から取る）。
    """
    strangers = strangers or {}
    quality = quality or {}
    largest = largest or {}
    weights = weights or Weights.default()
    by_media: dict = {}
    for row in rows:
        by_media.setdefault(row["media_id"], []).append(row)
    return {
        media_id: family_photo_score(
            faces, weights, strangers.get(media_id, ()), quality.get(media_id), largest.get(media_id)
        )
        for media_id, faces in by_media.items()
        if _has_family(faces)
    }
