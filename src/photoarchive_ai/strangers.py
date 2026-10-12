"""家族の写真に**目立って写った他人**の重みを出す（#100）。`select` の並びで点を引くため。

**なぜ要るか。** 並びの点（`scoring.family_photo_score`）は家族の顔だけから作るので、
家族の写りが良ければ、隣に他人が大きく写っていても上位に来る（利用者の要望・2026-10-12）。

**重みは「家族らしくなさ」×「目立ち具合」**（利用者の決定）。家族でない顔（未割当・除外）
1つずつについて、

- **家族らしくなさ**（0〜1・`unlikeness`）: 家族の顔（手本と自動割り当て）のうち、
  **その写真にまだ写っていない人物**の顔までの最短距離から出す（同じ人は1枚に1人なので、
  写っている人物には似ていても家族ではない）。照合の閾値（`embedding.ACTIVE.threshold`）
  以内なら 0、`FAR` 以上なら 1、間は直線。除外（`rejected`）の顔は 1。
  **特徴量の無い顔は 0**（分からないものを外さない）
- **目立ち具合**（0〜1・`prominence`）: 写真の中でいちばん大きい家族の顔と比べた大きさ
  （検出矩形の面積の平方根の比）。`SMALL` 以下は 0（背景の人。案2）、`LARGE` 以上は 1、間は直線

写真の重みは、その写真の他人の重みの和。

**未割当の顔は他人とは限らない。** 実データでは、家族の写った写真の未割当の顔 7,785 個の
うち 1,876 個が、いちばん近い手本まで照合の閾値以内で、その人物がまだ写真にいなかった。
並べて見ると手本と同じ子に見えるものが多い（`match` がマージン・年齢の上限で受け入れ
なかった顔）。閾値以内を 0 にするのは、これを引かないため。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np

from . import embedding

#: いちばん大きい家族の顔と比べて、この割合以下の顔は目立たない（0）。
SMALL = 0.3
#: この割合以上は目立つ（1）。家族と同じ大きさ。
LARGE = 1.0

#: 家族の顔までの最短距離がこれ以上なら、家族らしくなさ 1。
#: 実データで、手本から 0.6 以上の未割当の顔は大人や家族でない子だった（2026-10-12）。
FAR = 0.65

#: 一度に比べる顔の数（家族の顔との距離行列を小さく保つ）。
CHUNK_SIZE = 500


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


def unlikeness(distance: Optional[float]) -> float:
    """家族の顔までの最短距離を、家族らしくなさ（0〜1）に直す。分からなければ 0。"""
    if distance is None:
        return 0.0
    threshold = embedding.ACTIVE.threshold
    if distance <= threshold:
        return 0.0
    if distance >= FAR:
        return 1.0
    return (distance - threshold) / (FAR - threshold)


def _nearest_absent(
    connection, faces: Sequence[Dict[str, Any]], present: Dict[int, set]
) -> List[Optional[float]]:
    """``faces`` のそれぞれに、**その写真にいない人物**の家族の顔までの最短距離。

    家族の顔が1つも無ければ None（分からない）。**家族の全員がすでにその写真にいれば
    無限大**（残りの顔は家族ではありえない）。
    """
    from . import db

    person_ids, references = db.family_embeddings(connection)
    if not len(person_ids):
        return [None] * len(faces)
    nearest: List[Optional[float]] = []
    for start in range(0, len(faces), CHUNK_SIZE):
        chunk = faces[start : start + CHUNK_SIZE]
        distances = embedding.pairwise_distances(
            np.vstack([face["embedding"] for face in chunk]), references
        )
        for row, face in enumerate(chunk):
            here = present.get(face["media_id"], set())
            allowed = ~np.isin(person_ids, list(here)) if here else slice(None)
            candidates = distances[row][allowed]
            nearest.append(float(candidates.min()) if candidates.size else float("inf"))
    return nearest


def weights(connection, family_rows: Iterable[Dict[str, Any]]) -> Dict[int, float]:
    """写真ごとの目立つ他人の重み（``{media_id: 重み}``・0 の写真は入らない）。

    ``family_rows`` は `db.family_faces` の行（家族と数える顔だけ）。家族の写っていない
    写真は見ない（点はもともと 0）。
    """
    from . import db

    largest: Dict[int, float] = {}
    present: Dict[int, set] = defaultdict(set)
    for row in family_rows:
        media_id = row["media_id"]
        largest[media_id] = max(largest.get(media_id, 0.0), face_size(row))
        present[media_id].add(row["person_id"])
    if not largest:
        return {}

    found: Dict[int, float] = defaultdict(float)
    unknown = []
    for face in db.other_faces(connection, largest):
        top = largest[face["media_id"]]
        weight = prominence(face_size(face) / top) if top > 0 else 0.0
        if weight <= 0:
            continue
        face["prominence"] = weight
        if face["assign_source"] == db.ASSIGN_REJECTED:
            found[face["media_id"]] += weight
        elif face["embedding"] is not None:
            unknown.append(face)
    for face, distance in zip(unknown, _nearest_absent(connection, unknown, present)):
        found[face["media_id"]] += face["prominence"] * unlikeness(distance)
    return {media_id: value for media_id, value in found.items() if value > 0}
