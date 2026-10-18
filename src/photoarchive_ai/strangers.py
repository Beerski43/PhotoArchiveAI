"""家族の写真に写った**減点する顔**の重みを出す（#100・#110）。`select` の並びで点を引くため。

**なぜ要るか。** 並びの点（`scoring.family_photo_score`）は家族の顔だけから作るので、
家族の写りが良ければ、隣に写したくない人が大きく写っていても上位に来る（利用者の要望・2026-10-12）。

**減点するのは、利用者が選んだ顔だけ**（#110・2026-10-17 の利用者の決定）。除外（`rejected`）した顔のうち
「減点する」の印（`Face.penalize`）を付けた顔だけを数え、**未割当の顔と、印の無い除外の顔は加点にも減点にも
入れない。**

以前（#100）は未割当・除外の顔をすべて対象にし、家族の顔までの距離から「家族らしくなさ」を出して掛けていた。
未割当の顔には家族の取りこぼしが混ざる（実データで家族の写った写真の未割当 7,785 個のうち 1,876 個が手本の
閾値以内）ので距離で外していたが、写してよい他人（友だち・親戚）も減点していた。

重みは顔ごとの**目立ち具合**（0〜1・`prominence`）の和。写真の中でいちばん大きい家族の顔と比べた大きさ
（検出矩形の面積の平方根の比）で、`SMALL` 以下は 0（背景に小さく写っただけ）、`LARGE` 以上は 1、間は直線。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable

import numpy as np

#: いちばん大きい家族の顔と比べて、この割合以下の顔は目立たない（0）。
SMALL = 0.3
#: この割合以上は目立つ（1）。家族と同じ大きさ。
LARGE = 1.0


def face_size(row: Dict[str, Any]) -> float:
    """検出矩形の面積の平方根。"""
    height = max(0, row["bbox_bottom"] - row["bbox_top"])
    width = max(0, row["bbox_right"] - row["bbox_left"])
    return float(np.sqrt(height * width))


def prominence(ratio: float) -> float:
    """いちばん大きい家族の顔と比べた大きさを、目立ち具合（0〜1）に直す。"""
    if ratio <= SMALL:
        return 0.0
    if ratio >= LARGE:
        return 1.0
    return (ratio - SMALL) / (LARGE - SMALL)


def weights(connection, family_rows: Iterable[Dict[str, Any]]) -> Dict[int, float]:
    """写真ごとの減点する顔の重み（``{media_id: 重み}``・0 の写真は入らない）。

    ``family_rows`` は `db.family_faces` の行。**家族の顔だけで大きさを比べる**（親しい人の顔は物差しにしない）。
    家族の写っていない写真は見ない（点はもともと 0）。
    """
    from . import db

    largest: Dict[int, float] = {}
    for row in family_rows:
        if not db.is_family(row):
            continue
        media_id = row["media_id"]
        largest[media_id] = max(largest.get(media_id, 0.0), face_size(row))
    if not largest:
        return {}

    found: Dict[int, float] = defaultdict(float)
    for face in db.penalized_faces(connection, largest):
        top = largest[face["media_id"]]
        found[face["media_id"]] += prominence(face_size(face) / top) if top > 0 else 0.0
    return {media_id: value for media_id, value in found.items() if value > 0}
