"""GUIで割り当てきれなかった顔を、割り当て済みの顔を手本に自動で紐づける。

設計上の約束:

- 教師データは **手動割り当ての顔だけ**。自動割り当ての結果を手本に混ぜると、
  誤りが次の判定の根拠になって増幅する。
- 閾値とマージンを満たさない顔は **未割当のまま残す**。「一番近い人物」を
  無条件に割り当てると、写っていない人物まで紐づいてしまう。
- 既定では実行のたびに自動割り当てを一旦破棄してから付け直す。結果が
  「教師データと閾値」だけで決まるので、何度実行しても同じ状態になる。
"""

import logging
from typing import Any, Callable, Dict, Optional

import numpy as np

from . import db
from .scoring import distance_to_similarity

logger = logging.getLogger("photoarchive.matcher")

#: 顔特徴量の距離の上限。実データでの誤一致率(同一写真に写る別人同士が
#: この距離を下回る割合)は 0.4 で 0.5〜1.0%、0.45 で 2.8〜5.7%、
#: 0.5 で 9.0〜22.7%、dlib 標準の 0.6 では 40〜63% だった。
#: 誤った紐づけは手作業でのやり直しが高くつくため、取りこぼす側に倒す。
DEFAULT_THRESHOLD = 0.4
DEFAULT_MARGIN = 0.05
#: 読み出しの塊の大きさは db 側に持つ。二重定義にすると片方だけずれる。
CHUNK_SIZE = db.MATCH_CHUNK_SIZE


def _distances(candidates: np.ndarray, teachers: np.ndarray) -> np.ndarray:
    """(N, K) のユークリッド距離行列。

    ブロードキャストで差分をとると (N, K, 128) の巨大な配列になるため、
    内積から展開して計算する。
    """
    candidate_sq = np.sum(candidates.astype(np.float64) ** 2, axis=1)[:, None]
    teacher_sq = np.sum(teachers.astype(np.float64) ** 2, axis=1)[None, :]
    cross = candidates.astype(np.float64) @ teachers.astype(np.float64).T
    squared = np.maximum(candidate_sq + teacher_sq - 2.0 * cross, 0.0)
    return np.sqrt(squared)


def _best_match(
    distance_row: np.ndarray,
    person_ids: np.ndarray,
    threshold: float,
    margin: float,
):
    """最も近い人物と、2位の人物との距離差を見て採否を決める。"""
    best_index = int(np.argmin(distance_row))
    best_person = int(person_ids[best_index])
    best_distance = float(distance_row[best_index])
    if best_distance > threshold:
        return None, best_distance
    others = distance_row[person_ids != best_person]
    if others.size:
        second = float(np.min(others))
        if second - best_distance < margin:
            return None, best_distance
    return best_person, best_distance


def match_faces(
    connection,
    threshold: float = DEFAULT_THRESHOLD,
    margin: float = DEFAULT_MARGIN,
    reset: bool = True,
    dry_run: bool = False,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, Any]:
    """未割当の顔を自動で人物に紐づける。"""
    summary: Dict[str, Any] = {
        "teachers": 0,
        "candidates": 0,
        "assigned": 0,
        "unassigned": 0,
        "reset": 0,
        "per_person": {},
        "histogram": {},
        "dry_run": dry_run,
    }

    if reset and not dry_run:
        summary["reset"] = db.reset_auto_assignments(connection)

    teachers, person_ids = db.load_manual_embeddings(connection)
    summary["teachers"] = int(teachers.shape[0])
    if summary["teachers"] == 0:
        logger.warning("No manually assigned faces; nothing to match against.")
        if progress_callback is not None:
            progress_callback(0, 0, "no assigned faces to learn from")
        return summary

    # dry-run は自動割り当てを取り消さないので、取り消したあとに何が起きるかを
    # 見せるには auto の顔も候補に入れる必要がある。入れないと、2回目以降の
    # dry-run が「もう割り当て済みの顔」を数え落として空振りに見える。
    include_auto = dry_run and reset
    total = db.count_match_candidates(connection, include_auto=include_auto)
    updates = []
    processed = 0
    for ids, candidates in db.iter_unassigned_embeddings(
        connection, CHUNK_SIZE, include_auto=include_auto
    ):
        distances = _distances(candidates, teachers)
        for row_index in range(distances.shape[0]):
            person_id, distance = _best_match(
                distances[row_index], person_ids, threshold, margin
            )
            bucket = round(min(distance, 1.5) - (min(distance, 1.5) % 0.1), 1)
            summary["histogram"][bucket] = summary["histogram"].get(bucket, 0) + 1
            if person_id is None:
                summary["unassigned"] += 1
                continue
            updates.append((int(ids[row_index]), person_id, distance_to_similarity(distance)))
            summary["per_person"][person_id] = summary["per_person"].get(person_id, 0) + 1
            summary["assigned"] += 1
        processed += len(ids)
        summary["candidates"] = processed
        if progress_callback is not None:
            progress_callback(processed, max(total, processed), f"{summary['assigned']} assigned")

    if updates and not dry_run:
        db.apply_auto_assignments(connection, updates)

    if not dry_run:
        db.recompute_family_scores(connection)

    return summary
