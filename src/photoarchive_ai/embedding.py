"""顔特徴量モデルの記述（次元数・距離尺度・閾値）。

**ここに置いているのは、定数を書き写さないため。** 以前は次元数が `db.py`、
閾値とマージンが `matcher.py`、類似度の基準が `scoring.py` に散っていて、
**モデルを替えるとどれか1つが古いまま残る**形だった。

このモジュールは numpy だけに依存する（`face.py` は MediaPipe と onnxruntime を
読み込む）。**距離の計算もここに置いてある**（下の `pairwise_distances`）。
`db` / `matcher` / `scoring` は次元数と尺度だけが要るので、ここを見る。

実測は [docs/history/details/2026-10-02-embedding-model-comparison.md] にある。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

#: 距離尺度。**モデルごとに違う。** dlib の 0.4 を他のモデルへ持ち込まないこと。
METRIC_EUCLIDEAN = "euclidean"
METRIC_COSINE = "cosine"


@dataclass(frozen=True)
class EmbeddingModel:
    """特徴量の作り方と、その特徴量をどう比べるか。

    ``version`` は `Face.embed_version` に入り、**照合の絞り込みにも使う。**
    別の埋め込み空間の特徴量を混ぜて距離を取るのは常に誤りなので、
    `match` は同じ版の特徴量だけを見る（`db.load_manual_embeddings`）。
    """

    #: `Face.embed_version` に保存する文字列。**作り方を変えたら必ず変える。**
    version: str
    #: 次元数。`db.encode_embedding` がこの形で保存を検査する。
    dimensions: int
    #: `METRIC_EUCLIDEAN` か `METRIC_COSINE`。
    metric: str
    #: `match` の既定の閾値。これより遠い顔は未割当のまま残す。
    threshold: float
    #: 1位と2位の人物の距離差の下限。満たさない顔は未割当のまま残す。
    margin: float
    #: 行事（フォルダ×日）の中で顔を束ねるときの連結の上限
    #: （`clustering.cluster_faces`）。**`threshold` とは役割が違う。**
    #: `threshold` は「手本の人物に紐づけてよいか」、こちらは
    #: 「同じ行事のこの2つを同じ人物として1つにまとめてよいか」。
    #: 束ねすぎると人間が束を割る手間が増え、割れすぎると判断の数が減らない。
    cluster_threshold: float
    #: `scoring.distance_to_similarity` が 0 を返す距離。
    similarity_reference: float


#: 2017年の dlib ResNet。**2026-10-02 まで使っていたもの。**
#:
#: 閾値 0.4 は誤一致率（同一写真に写る別人同士がこれを下回る割合）が
#: 0.5〜1.0% になる点として選ばれた。実データでの1位正解率は 69.8%。
DLIB_RESNET = EmbeddingModel(
    version="dlib_resnet_v1/sp5/pad0.25/full",
    dimensions=128,
    metric=METRIC_EUCLIDEAN,
    threshold=0.4,
    margin=0.05,
    #: **未測定。** dlib は退役したので測り直していない（行事の中の同一人物は
    #: 中央 0.480・別人 0.636 だったので、この値は分けるには足りない）。
    cluster_threshold=0.4,
    similarity_reference=0.6,
)

#: InsightFace の ArcFace（`w600k_r50`）。**5点整列つき。整列が +16.6pt を持つ。**
#:
#: 実データの手本126件での実測（2026-10-02）。
#:
#: - 1位正解率 **95.2%**（dlib は 69.8%）
#: - 行事をまたぐ分離 **+0.418**（dlib は +0.033）
#:
#: **閾値 0.45 は「他人誤認率」で選んだ**（2026-10-03。実データ全件で実測）。
#:
#: 同じ写真に写る顔のペア 37,933 組を「別人」とみなして測った誤認率。
#: dlib の既定 0.4 を選んだときと**同じ数え方**にそろえてある（仕様書 §8.3）。
#:
#: ===== ============ ==================
#:  閾値   他人誤認率   `evaluate` の正解
#: ===== ============ ==================
#:  0.40     0.61%          68.3%
#:  0.45     1.06%          78.6%     ← 既定
#:  0.50     1.92%          84.1%
#:  0.60     5.64%          88.9%
#: ===== ============ ==================
#:
#: **はじめ 0.60 を既定にしていたが、緩すぎた。** 0.60 の他人誤認率 5.64% は
#: dlib の既定（0.5〜1.0%）より**6〜11倍甘い。** そのことに気づけなかったのは、
#: 手本どうしのペアだけで閾値を選んでいたからで、**あれは家族5人の中での
#: 取り違えしか数えていない**（未割当 58,212 件の大半は他人）。
#:
#: 0.45 なら dlib のどの指標も上回る。
#: 正解 26.9% → **78.6%** / `evaluate` の誤り 2.6% → **0.0%** /
#: 他人誤認 0.5〜1.0% → **1.06%**。
#:
#: マージン 0.08 は、1位と2位の距離差の**下位10%**（実測 0.091）に当たる。
#: いちばん迷っている1割を未割当のまま残す。**閾値に対する比率で決めていない**
#: （距離差は埋め込み空間の性質で、閾値を動かしても変わらない）。
#:
#: **どちらも手本126件での暫定値。** 手本が増えたら
#: `photoarchive evaluate` で測り直して確定する（Phase 3 手順5）。
#:
#: ---
#:
#: **⚠️ この版には2つの入力規約が同居している**（2026-10-03 に測って許した）。
#:
#: ===================== ==========================================
#:  経路                   特徴量の入力
#: ===================== ==========================================
#:  `scan`                元写真から `face_rect`（25%パディング・元解像度）
#:  `photoarchive reembed` サムネイル（パディング無し・最大160px）
#: ===================== ==========================================
#:
#: CLAUDE.md §8 は「パディングを変えるなら版を上げる」と言うが、**ArcFace は
#: 5点整列がパディングの違いを吸収する**ので、ここは例外として許している。
#: 実データの手本126件で、同じ顔を両経路で作って比べた結果。
#:
#: - 同じ顔の両経路の距離: 中央 **0.090** / p90 0.270 / 最大 0.893
#: - **閾値 0.45 を超える顔が 10/126 件（8%）**
#: - 1位正解率は**どちらの問い合わせでも 95.2%**（手本は reembed 経路）
#:
#: **精度は落ちていない**ので、作り替えない。**経路ごとに別の版にはしない** —
#: 照合は版の完全一致で絞るので、**新しく scan した顔と reembed した顔が
#: 互いに見えなくなる。**
#:
#: **ただし閾値 0.45 と他人誤認率 1.06% は reembed 経路だけで測った数字。**
#: 新しく scan した顔はこの 8% のぶんだけ、ずれる余地がある。
#:
#: ---
#:
#: **束ねの上限 `cluster_threshold` も 0.45**（2026-10-04 実測。Issue #61）。
#: **照合の閾値と同じ数字になったのは偶然**で、別に測って選んでいる。
#: 行事（フォルダ×日）ごとに平均連結で束ね、**同じ写真に写る顔のペアを
#: 同じ束へ入れた割合**（他人誤認率。閾値 0.45 を選んだときと同じ数え方）を見た。
#:
#: ===== ============ =============== ==============
#:  上限   他人誤認率    決定の数の減り   混ざった束
#: ===== ============ =============== ==============
#:  0.40     0.48%          56.5%          1.68%
#:  0.45     0.66%          62.1%          2.13%     ← 既定
#:  0.50     1.22%          66.4%          2.98%
#:  0.60     3.92%          73.1%          6.39%
#: ===== ============ =============== ==============
#:
#: **0.50 は他人誤認率が照合の 1.06% を越える**ので採らない。0.45 なら
#: 未割当 51,860 件に対する決定が 19,678 回まで落ち、**手本を2件以上含む束の
#: 純度は 100%**（手本126件での実測）。
#:
#: 詳細は
#: [docs/history/details/2026-10-04-event-clustering-measured.md]。
ARCFACE_W600K_R50 = EmbeddingModel(
    version="arcface_w600k_r50/5pt/112",
    dimensions=512,
    metric=METRIC_COSINE,
    threshold=0.45,
    margin=0.08,
    cluster_threshold=0.45,
    similarity_reference=1.0,
)

#: いま使うモデル。**`Face.embed_version` に入る版はここで決まる。**
#:
#: 替えたら `photoarchive reembed` で特徴量を作り直す。**`scan --force-rescan`
#: は使わないこと**（手動割り当てを巻き添えにする）。
ACTIVE = ARCFACE_W600K_R50

#: 版の文字列から記述を引くための対応表。**古い版の顔を読み解くのに使う。**
KNOWN_MODELS = {model.version: model for model in (DLIB_RESNET, ARCFACE_W600K_R50)}


def model_for_version(version: str) -> EmbeddingModel:
    """``Face.embed_version`` から記述を引く。知らない版なら ``KeyError``。"""
    return KNOWN_MODELS[version]


# ---------------------------------------------------------------------------
# 距離の計算
# ---------------------------------------------------------------------------
#
# **尺度の実装をここに1つだけ置く。** 以前は `matcher._distances` と
# `scripts/measure_embedding_models.py` に同じ式が2つあり、`clustering` を
# 足すと3つめになるところだった。**モデルを替えたときに古いまま残るのは、
# いつも書き写したほう。**


def normalize_rows(matrix: np.ndarray) -> np.ndarray:
    """各行を L2 正規化する。長さ0の行はそのまま返す（0除算を避ける）。"""
    matrix = np.asarray(matrix, dtype=np.float64)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return matrix / norms


def pairwise_distances(
    left: np.ndarray, right: np.ndarray, metric: Optional[str] = None
) -> np.ndarray:
    """``(N, K)`` の距離行列。**尺度はモデルが決める**（既定は `ACTIVE`）。

    ブロードキャストで差分をとると ``(N, K, 次元数)`` の巨大な配列になるため、
    内積から展開して計算する。

    - `METRIC_EUCLIDEAN`: ユークリッド距離（dlib）
    - `METRIC_COSINE`: L2 正規化してから ``1 - cos``（ArcFace）。
      **正規化を省くと、ベクトルの長さが距離に混ざる。**
    """
    metric = metric or ACTIVE.metric
    first = np.asarray(left, dtype=np.float64)
    second = np.asarray(right, dtype=np.float64)
    if metric == METRIC_COSINE:
        unit_first = normalize_rows(first)
        unit_second = normalize_rows(second)
        return np.clip(1.0 - unit_first @ unit_second.T, 0.0, 2.0)
    if metric != METRIC_EUCLIDEAN:
        raise ValueError(f"知らない距離尺度: {metric}")
    first_sq = np.sum(first**2, axis=1)[:, None]
    second_sq = np.sum(second**2, axis=1)[None, :]
    squared = np.maximum(first_sq + second_sq - 2.0 * (first @ second.T), 0.0)
    return np.sqrt(squared)
