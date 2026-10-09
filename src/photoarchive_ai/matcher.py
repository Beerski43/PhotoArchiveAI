"""GUIで割り当てきれなかった顔を、割り当て済みの顔を手本に自動で紐づける。

設計上の約束:

- 手本は **手動割り当ての顔だけ**。自動割り当ての結果を手本に混ぜると、
  誤りが次の判定の根拠になって増幅する。
- 閾値とマージンを満たさない顔は **未割当のまま残す**。「一番近い人物」を
  無条件に割り当てると、写っていない人物まで紐づいてしまう。
- 既定では実行のたびに自動割り当てを一旦破棄してから付け直す。結果が
  「手本と閾値」だけで決まるので、何度実行しても同じ状態になる。
"""

import logging
from typing import Any, Callable, Dict, Optional

import numpy as np

from . import db, embedding
from .dates import calculate_age
from .scoring import distance_to_similarity

logger = logging.getLogger("photoarchive.matcher")

#: 顔特徴量の距離の上限と、1位と2位の人物の距離差の下限。
#:
#: **どちらもモデルの属性。書き写さない**（`embedding.ACTIVE`）。
#: 尺度がモデルごとに違うので、dlib 用の 0.4 を他のモデルへ持ち込むと
#: 意味が変わる（dlib はユークリッド、ArcFace はコサイン）。
#:
#: 選び方の基準は**モデルを替えても同じ**。「行事をまたぐ別人のペアを
#: 1% 程度しか誤らない」点を採る。誤った紐づけは手作業でのやり直しが
#: 高くつくため、**取りこぼす側に倒す。**
#:
#: 数え方は「同じ写真に写る2つの顔を別人とみなした誤認率」（仕様書 §8.3）。
#:
#: - dlib(0.4): 他人誤認 0.5〜1.0% / `evaluate` の正解 26.9%
#: - ArcFace(0.45): 他人誤認 **1.06%** / `evaluate` の正解 **78.6%**
DEFAULT_THRESHOLD = embedding.ACTIVE.threshold
DEFAULT_MARGIN = embedding.ACTIVE.margin

#: 読み出しの塊の大きさは db 側に持つ。二重定義にすると片方だけずれる。
CHUNK_SIZE = db.MATCH_CHUNK_SIZE

#: 距離の分布（ヒストグラム）の上限。**尺度によって取りうる最大が違う。**
HISTOGRAM_MAX = 2.0 if embedding.ACTIVE.metric == embedding.METRIC_COSINE else 1.5


def _distances(
    candidates: np.ndarray, teachers: np.ndarray, metric: Optional[str] = None
) -> np.ndarray:
    """(N, K) の距離行列。**式は `embedding.pairwise_distances` に1つだけある。**

    ここに写しを置かない。**尺度はモデルの属性**（dlib はユークリッド、
    ArcFace はコサイン）で、写しを持つとモデルを替えたときに片方が古くなる。
    """
    return embedding.pairwise_distances(candidates, teachers, metric)


def _persons_alive_at(
    person_ids: np.ndarray,
    birth_dates: Dict[int, Optional[str]],
    shooting_date: Optional[str],
) -> np.ndarray:
    """その写真の時点で**生まれている**人物だけを残す真偽マスク。

    **「その人が生まれる前の写真には写れない」は動かせない事実**なので、
    候補から外してよい唯一の属性。**上限は設けない**（「老けすぎ」では弾かない）。
    そちらは判断が要る。

    **外した結果はマージンにも効く。** `_best_match` は2位の人物との距離差で
    採否を決めるので、**渡す前に外さないと、ありえない人物が「2位」に居座って
    判断を保留させる。** 実データでは、ここを先に外すことで
    **未割当だった 457 件が正しく${PERSON_3}へ付いた。**

    判定しないのは次の2つ。**分からないものを弾かない。**

    - **撮影日時が読めない**（実データの約16%。壊れた値も含む）
    - **誕生日が未登録**（任意の項目）

    読めるかどうかの判断は `dates.calculate_age` 越しに
    `dates.parse_date` へ預ける。**ここに日付の判定を書かない**
    （`0000-00-00` と `TTTT-TT-TTTTT:TT:TT` でこのリポジトリは2度壊れている。
    CLAUDE.md §8）。
    """
    alive = np.ones(person_ids.shape[0], dtype=bool)
    if shooting_date is None:
        return alive
    for person_id in set(person_ids.tolist()):
        age = calculate_age(birth_dates.get(int(person_id)), shooting_date)
        if age is not None and age < 0:
            alive[person_ids == person_id] = False
    return alive


def _persons_not_rejected(
    person_ids: np.ndarray, rejected_person_ids: Optional[set]
) -> Optional[np.ndarray]:
    """「**この顔はこの人物ではない**」と人が記録した人物を外すマスク。

    `Face.assign_source='rejected'`（＝**誰でもない顔**）とは別のもの。
    あちらは候補から顔ごと外れるが、こちらは**その人物だけ**を外す。
    **兄弟の赤ん坊の顔は互いによく似ており**、未割当に戻すだけでは
    `match` を流すたびに同じ誤りが戻る（実データで${PERSON_4}の 1,785 件で起きた）。

    否定が無ければ ``None`` を返す（絞る必要が無い、の意味）。
    """
    if not rejected_person_ids:
        return None
    keep = np.ones(person_ids.shape[0], dtype=bool)
    for person_id in rejected_person_ids:
        keep[person_ids == person_id] = False
    return keep


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
    metric: Optional[str] = None,
) -> Dict[str, Any]:
    """未割当の顔を自動で人物に紐づける。

    ``metric`` は距離尺度。省略するといま使うモデルのもの（`embedding.ACTIVE`）。
    **明示できるようにしているのは、尺度を前提にした判断を読み手に見せるため。**
    閾値とマージンは尺度と一体なので、`threshold` を変えるときは
    どの尺度の話かがはっきりしている必要がある。
    """
    summary: Dict[str, Any] = {
        "teachers": 0,
        "candidates": 0,
        "assigned": 0,
        "unassigned": 0,
        # 誕生日で候補が1人も残らなかった顔。**未割当の内訳**で、
        # 「似た顔が無い」のではなく「その写真にいられる人が居ない」。
        "no_candidate": 0,
        "reset": 0,
        "per_person": {},
        "histogram": {},
        "dry_run": dry_run,
    }

    if reset and not dry_run:
        summary["reset"] = db.reset_auto_assignments(connection)
        # **工程の境目でも知らせる。** 照合の前の準備（取り消しと手本の読み込み）は
        # 実データで約5秒かかり、そのあいだ何も知らせないと GUI の窓が固まって
        # 見える（GNOME は5秒で「応答なし」と出す）。#67 で GUI から流す口を作って測った。
        if progress_callback is not None:
            progress_callback(0, 0, f"{summary['reset']} auto assignments cleared")

    teachers, person_ids = db.load_manual_embeddings(connection)
    summary["teachers"] = int(teachers.shape[0])
    # **誕生日は候補を絞る材料。** 登録されていない人物は絞られない。
    birth_dates = {
        int(person["id"]): person["birth_date"] for person in db.list_persons(connection)
    }
    # **人が「この人物ではない」と押した記録。** 誕生日の絞り込みと同じく、
    # 距離を比べる前に候補から外す。
    rejections = db.load_person_rejections(connection)
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
    if progress_callback is not None:
        progress_callback(0, total, f"{summary['teachers']} teachers loaded")
    updates = []
    processed = 0
    # **マスクは撮影日時で使い回す。** 同じ写真に何件も顔があり、人物ごとの
    # 年齢計算を顔の数だけ繰り返すと、日付の解析が候補数×人物数になる。
    alive_cache: Dict[Any, np.ndarray] = {}
    for ids, candidates in db.iter_unassigned_embeddings(
        connection, CHUNK_SIZE, include_auto=include_auto
    ):
        shooting_dates = db.shooting_dates_by_face(connection, ids.tolist())
        distances = _distances(candidates, teachers, metric)
        for row_index in range(distances.shape[0]):
            shooting_date = shooting_dates.get(int(ids[row_index]))
            if shooting_date not in alive_cache:
                alive_cache[shooting_date] = _persons_alive_at(
                    person_ids, birth_dates, shooting_date
                )
            alive = alive_cache[shooting_date]
            denied = _persons_not_rejected(
                person_ids, rejections.get(int(ids[row_index]))
            )
            if denied is not None:
                alive = alive & denied
            # **絞るのは `_best_match` に渡す前。** あとから捨てると、ありえない
            # 人物が2位に居座ってマージンを潰し、判断が保留のままになる。
            if alive.all():
                person_id, distance = _best_match(
                    distances[row_index], person_ids, threshold, margin
                )
            elif not alive.any():
                # 手本のある人物が全員、この写真の時点でまだ生まれていない。
                person_id, distance = None, float("inf")
            else:
                person_id, distance = _best_match(
                    distances[row_index][alive], person_ids[alive], threshold, margin
                )
            # **刻みの上限は尺度に合わせる。** ユークリッド(dlib)は実質 1.5 まで、
            # コサインは 2.0 まで。固定すると遠い顔が1つの桶に潰れて分布が読めない。
            # 候補が1人も残らなかった顔は、距離の分布に混ぜない（距離が無い）。
            if not np.isfinite(distance):
                summary["unassigned"] += 1
                summary["no_candidate"] += 1
                continue
            capped = min(distance, HISTOGRAM_MAX)
            bucket = round(capped - (capped % 0.1), 1)
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
