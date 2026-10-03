"""行事の中で顔を束ねる（Issue #61）。

**判断の単位を「顔」から「束」へ移すための計算。** 実データの未割当は
51,860 件あり（2026-10-04 時点）、1件ずつ人物を選ぶ作業は終わらない。
同じ行事（フォルダ×日）の中で同じ人物の顔を束ねてしまえば、人間の決定は
束の数まで落ちる。

設計上の約束。

- **DBに書かない。** 束はその場で計算する使い捨ての見方で、保存すると
  手本が増えるたびに古くなる。`Face` に列を足さない（スキーマは変えない）
- **平均連結（average linkage）。** 単連結は1つの誤った近さで鎖のように
  繋がり、完全連結は同じ人物の中の角度差で割れる。平均連結はその間。
- **距離尺度と閾値はモデルの属性**（`embedding.ACTIVE`）。ここに数字を
  書き写さない。dlib はユークリッド、ArcFace はコサインで、**0.45 を
  他のモデルへ持ち込むと意味が変わる**
- **同じ入力から必ず同じ束が出る。** 乱数を使わない（結び方の迷いは
  添字の小さい側に倒す）。同じ写真を2回開いて違う束が出ると、
  何を確認したのかが分からなくなる

実測は
[docs/history/details/2026-10-04-event-clustering-measured.md](../../docs/history/details/2026-10-04-event-clustering-measured.md)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

from . import embedding

logger = logging.getLogger("photoarchive.clustering")

#: 1回に束ねる顔の数の上限。
#:
#: 平均連結は ``(N, N)`` の距離行列を持つので、**費用は顔の数の2乗**。
#: 4,000 件で 128MB・数十秒かかる。実データでいちばん大きい行事は
#: **1,357 件**（2026-10-04。2011-04-16 の結婚式）で、そこは 1.8MB・数秒。
#:
#: **超えたら黙って捨てずに `TooManyFacesError` を上げる。** 先頭だけ束ねて
#: 残りを落とすと、画面には出ていない顔が未割当のまま残り、**束ねたつもりの
#: 行事に未割当が残っていることに気づけない。**
MAX_FACES = 4000


class TooManyFacesError(RuntimeError):
    """``MAX_FACES`` を超える顔をまとめて束ねようとした。"""


@dataclass(frozen=True)
class Cluster:
    """同じ人物とみなした顔の束。

    ``face_ids`` は渡された並びのまま（並べ替えない）。呼び出し側が
    品質スコアの高い順に渡せば、先頭が代表の顔になる。
    """

    face_ids: Tuple[int, ...]

    @property
    def size(self) -> int:
        return len(self.face_ids)


def average_linkage_labels(distances: np.ndarray, threshold: float) -> np.ndarray:
    """距離行列を平均連結で束ね、各行が属する束の番号を返す。

    番号は**その束でいちばん小さい添字**。``threshold`` より近い組が
    無くなったら止める。

    ``distances`` は対称行列で、対角は無視する（自分自身とは結ばない）。
    """
    count = int(distances.shape[0])
    if distances.shape != (count, count):
        raise ValueError(f"距離行列が正方ではない: {distances.shape}")
    if count > MAX_FACES:
        raise TooManyFacesError(
            f"一度に束ねられるのは {MAX_FACES} 件まで（渡されたのは {count} 件）"
        )
    labels = np.arange(count)
    if count < 2:
        return labels

    work = np.array(distances, dtype=np.float64, copy=True)
    np.fill_diagonal(work, np.inf)
    sizes = np.ones(count, dtype=np.float64)

    for _ in range(count - 1):
        flat = int(np.argmin(work))
        # **`argmin` は最初に見つけた位置を返す**ので、同じ距離の組が複数
        # あっても選び方は決まる（添字の小さい側）。乱数を使わない理由と同じ。
        row, column = divmod(flat, count)
        if not np.isfinite(work[row, column]) or work[row, column] > threshold:
            break
        keep, drop = (row, column) if row < column else (column, row)
        # 平均連結の更新式（Lance-Williams）。**件数で重みを付ける。**
        # 付けないと、大きい束と小さい束が同じ重みになって中心がずれる。
        merged = (sizes[keep] * work[keep] + sizes[drop] * work[drop]) / (
            sizes[keep] + sizes[drop]
        )
        work[keep, :] = merged
        work[:, keep] = merged
        work[keep, keep] = np.inf
        work[drop, :] = np.inf
        work[:, drop] = np.inf
        sizes[keep] += sizes[drop]
        sizes[drop] = 0.0
        labels[labels == labels[drop]] = labels[keep]

    return labels


def cluster_faces(
    face_ids: Sequence[int],
    embeddings: Sequence[np.ndarray],
    threshold: Optional[float] = None,
    metric: Optional[str] = None,
) -> List[Cluster]:
    """顔を束ねる。**大きい束が先**（同じ大きさなら渡された順）。

    ``threshold`` と ``metric`` を省略すると、いま使うモデルのもの
    （`embedding.ACTIVE`）。**閾値を測り直すとき以外は省略する。**

    ``embeddings`` に ``None`` は渡せない。**特徴量の無い顔は呼び出し側で
    外す**（実データでは 443 件がサムネイル 60px 未満で特徴量を持たない）。
    ここで黙って落とすと、束の合計が行事の顔の数と合わなくなる。
    """
    if len(face_ids) != len(embeddings):
        raise ValueError(
            f"顔と特徴量の数が合わない: {len(face_ids)} 件 / {len(embeddings)} 件"
        )
    if not face_ids:
        return []
    if threshold is None:
        threshold = embedding.ACTIVE.cluster_threshold
    vectors = np.vstack([np.asarray(vector, dtype=np.float64) for vector in embeddings])
    distances = embedding.pairwise_distances(vectors, vectors, metric)
    labels = average_linkage_labels(distances, threshold)

    grouped: dict = {}
    for index, label in enumerate(labels):
        grouped.setdefault(int(label), []).append(int(face_ids[index]))
    clusters = [Cluster(face_ids=tuple(members)) for _, members in sorted(grouped.items())]
    # 大きい束から見せる。**人間の決定を減らすのが目的**なので、1件しか
    # 入っていない束を先に見せても作業は減らない。
    clusters.sort(key=lambda cluster: (-cluster.size, cluster.face_ids[0]))
    return clusters
