"""顔を「この人物に似た順」に並べる（顔候補の自動推薦・#69）。

**並べるだけで、閾値では切らない。** 確定するのは人で、`match` とは役目が違う。

点は**その人物の手本との最小距離**。2026-10-08 に、手本を1件抜いて未割当に
混ぜ、この点で並べたときの順位を測った（中央 1 番目・1ページ目に 94.0%。
`docs/history/details/2026-10-08-face-recommendation-remeasured.md`）。

守っていること:

- **手本は `db.load_manual_faces` から取る。** いまのモデルの版
  （`embed_version`）だけを読み、5点整列ができなかった手本（`usable` が偽）は
  **使わない。** 整列できない顔の特徴量は中身に関係なく互いに近くなるので、
  使うと横倒しの顔や顔でないものが上位に来る（2026-10-10 の取り下げの経緯）
- **距離の式は `embedding.pairwise_distances` に1つだけ**（尺度を書き写さない）
- **自分自身は手本から外して測る。** 確定済みの顔を「似ていない順」に並べる
  ときに、自分との距離 0 で全件が並ばなくなるのを防ぐ
- **距離を出せない顔**（特徴量が無い・版が違う）は、どちらの向きでも最後
"""

from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import numpy as np

from . import db, embedding

#: 並びの名前。**db の並び（`db.ORDER_*`）とは別に持つ。** あちらは SQL だけで
#: 並べられるもので、こちらは手本との距離が要る。
ORDER_SIMILAR = "similar"
ORDER_DISSIMILAR = "dissimilar"
ORDERS = (ORDER_SIMILAR, ORDER_DISSIMILAR)

#: 距離行列を作る塊の大きさ。**候補 × 手本の行列を一度に作らない**
#: （実データの未割当 36,018 × ひよりの手本 3,362 を float64 で持つと 970MB）。
CHUNK_SIZE = 4000

Progress = Callable[[int, int], None]


def person_teachers(connection, person_id: int) -> Tuple[np.ndarray, np.ndarray]:
    """その人物の、**並びの根拠にしてよい**手本を (id配列, 行列) で返す。"""
    faces = db.load_manual_faces(connection)
    keep = (faces.person_ids == int(person_id)) & np.asarray(faces.usable, dtype=bool)
    return faces.face_ids[keep], faces.embeddings[keep]


def nearest_distances(
    candidate_ids: np.ndarray,
    candidates: np.ndarray,
    teacher_ids: np.ndarray,
    teachers: np.ndarray,
    progress: Optional[Progress] = None,
) -> np.ndarray:
    """候補ごとの、手本との最小距離。**自分自身は手本から外す。**

    手本が自分しか無い候補は ``inf``（比べる相手が居ない）。
    """
    count = int(candidates.shape[0])
    best = np.full(count, np.inf, dtype=np.float64)
    if count == 0 or teachers.shape[0] == 0:
        return best
    teacher_ids = np.asarray(teacher_ids, dtype=np.int64)
    for start in range(0, count, CHUNK_SIZE):
        stop = min(start + CHUNK_SIZE, count)
        distances = embedding.pairwise_distances(candidates[start:stop], teachers)
        own = np.asarray(candidate_ids[start:stop], dtype=np.int64)[:, None] == teacher_ids[None, :]
        distances[own] = np.inf
        best[start:stop] = distances.min(axis=1)
        if progress is not None:
            progress(stop, count)
    return best


def rank(
    face_ids: Iterable[int], distances: Dict[int, float], descending: bool = False
) -> List[int]:
    """顔の id を距離の近い順（``descending`` なら遠い順）に並べる。

    **距離の無い顔はどちらの向きでも最後**（id 順）。同じ距離の中は id 順で、
    **ページをまたいで重複・欠落させない。**
    """
    sign = -1.0 if descending else 1.0

    def key(face_id: int) -> tuple:
        value = distances.get(face_id)
        if value is None or not np.isfinite(value):
            return (True, 0.0, face_id)
        return (False, sign * value, face_id)

    return sorted((int(face_id) for face_id in face_ids), key=key)


class PersonSimilarity:
    """1人ぶんの「顔 → 手本との最小距離」。**ページ送りのあいだ持ち続ける。**

    ページを送るたび・絞り込みを変えるたびに未割当 3万件ぶんを計算し直すと、
    1回に約3秒かかる（2026-10-10 実データ・ひより）。そこで:

    - **求められた顔のうち、まだ測っていないものだけ**を測る
    - **手本が増えただけ**なら、測った顔を**増えた手本とだけ**比べて更新する
      （最小距離は手本を足しても増えない）。割り当てるたびに並びが良くなり、
      待ちは短い
    - **手本が減った**（解除・除外）ときだけ、全部を測り直す
    """

    def __init__(self, person_id: int):
        self.person_id = int(person_id)
        self.teacher_ids: Set[int] = set()
        self.distances: Dict[int, float] = {}
        #: 距離を出せない顔（特徴量が無い・版が違う）。**測り直さない。**
        self.unmeasurable: Set[int] = set()

    def update(
        self, connection, face_ids: Sequence[int], progress: Optional[Progress] = None
    ) -> int:
        """``face_ids`` の距離をそろえる。**並びの根拠にした手本の件数**を返す。

        手本が0件なら何も測らない（呼び出し側がそうと分かる表示を出す）。
        """
        teacher_ids, teachers = person_teachers(connection, self.person_id)
        current = set(int(face_id) for face_id in teacher_ids.tolist())
        if not current:
            self._forget()
            return 0
        if not self.teacher_ids <= current:
            self._forget()
        added = current - self.teacher_ids
        work: List[Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = []
        if added and self.distances:
            mask = np.isin(teacher_ids, np.fromiter(added, dtype=np.int64))
            known_ids, known = db.embeddings_for_faces(connection, list(self.distances))
            work.append((known_ids, known, teacher_ids[mask], teachers[mask]))
        missing = [
            int(face_id)
            for face_id in face_ids
            if int(face_id) not in self.distances and int(face_id) not in self.unmeasurable
        ]
        if missing:
            new_ids, new = db.embeddings_for_faces(connection, missing)
            self.unmeasurable.update(set(missing) - set(new_ids.tolist()))
            work.append((new_ids, new, teacher_ids, teachers))
        self.teacher_ids = current

        total = sum(int(item[0].shape[0]) for item in work)
        done = 0
        for ids, matrix, part_ids, part in work:
            offset = done

            def report(step: int, _count: int, offset=offset) -> None:
                if progress is not None:
                    progress(offset + step, total)

            best = nearest_distances(ids, matrix, part_ids, part, report)
            for face_id, value in zip(ids.tolist(), best.tolist()):
                previous = self.distances.get(int(face_id), np.inf)
                self.distances[int(face_id)] = min(previous, value)
            done += int(ids.shape[0])
        return len(current)

    def _forget(self) -> None:
        self.teacher_ids = set()
        self.distances = {}
