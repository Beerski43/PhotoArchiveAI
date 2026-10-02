"""顔特徴量モデルの記述（次元数・距離尺度・閾値）。

**ここに置いているのは、定数を書き写さないため。** 以前は次元数が `db.py`、
閾値とマージンが `matcher.py`、類似度の基準が `scoring.py` に散っていて、
**モデルを替えるとどれか1つが古いまま残る**形だった。

このモジュールは重い依存を持たない（`face.py` は MediaPipe と onnxruntime を
読み込む）。
`db` / `matcher` / `scoring` は次元数と尺度だけが要るので、ここを見る。

実測は [docs/history/details/2026-10-02-embedding-model-comparison.md] にある。
"""

from __future__ import annotations

from dataclasses import dataclass

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
    similarity_reference=0.6,
)

#: InsightFace の ArcFace（`w600k_r50`）。**5点整列つき。整列が +16.6pt を持つ。**
#:
#: 実データの手本126件での実測（2026-10-02）。
#:
#: - 1位正解率 **95.2%**（dlib は 69.8%）
#: - 行事をまたぐ分離 **+0.418**（dlib は +0.033）
#:
#: 閾値 0.60 は、**dlib の 0.4 と同じ誤り率の基準**で選んだ。行事をまたぐ
#: 別人のペアの誤りが 1% 以下になる最大の閾値が 0.603 で、そのとき同一人物を
#: **63.5%** 拾う（dlib は同じ誤り率で約30%しか拾えていなかった）。
#:
#: マージン 0.08 は、1位と2位の差の**下位10%**（0.091）に当たる。いちばん
#: 迷っている1割を未割当のまま残す。dlib の 0.05 は閾値の 12.5% で、
#: 0.60 の 12.5% は 0.075 なので、比としてもほぼ同じ。
#:
#: **どちらも手本126件での暫定値。** 手本が増えたら
#: `photoarchive evaluate` で測り直して確定する（Phase 3 手順5）。
ARCFACE_W600K_R50 = EmbeddingModel(
    version="arcface_w600k_r50/5pt/112",
    dimensions=512,
    metric=METRIC_COSINE,
    threshold=0.60,
    margin=0.08,
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
