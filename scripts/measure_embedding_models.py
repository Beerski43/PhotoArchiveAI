#!/usr/bin/env python3
"""特徴量モデルを、保存済みサムネイルだけで比べる（Issue #57）。

**本体のコードを変えない。実データに書かない。NFS を読まない。**

現行は dlib の ResNet（2017年・128次元・ユークリッド距離）で、実データの
1位正解率は 69.8%。家族5人のうち3人が 0〜17歳の子どもで、このモデルには
厳しい対象である。替えるべきかを、**顔画像を `Face.thumbnail` から復元して**
測る。

サムネイルは短辺の中央 160px・全部で 272MB・112px 未満は 7.3% だけなので、
ArcFace の入力（112×112）に足りる。これが成り立てば、モデル差し替えの代償は
**「全件再スキャン約10時間（特徴量 4.9h ＋ NFS 読み直し 5.1h）」から数十分へ落ちる。**

比べる4通り。

===== =============================== ====================================
 #     特徴量 / 画像の出どころ          何が分かるか
===== =============================== ====================================
 (a)   dlib 128次元 / 原寸（DB の値）   現行の基準値
 (b)   dlib 128次元 / サムネイル        **サムネイル化の代償だけ**
 (c)   ArcFace 512次元 / サムネイル     素の効き目（整列なし）
 (d)   ArcFace 512次元 / サムネイル     整列が要るか（5点整列あり）
===== =============================== ====================================

**(a) 対 (b) がこのスクリプトのいちばんの出力。** 差が小さければ
「サムネイルからの再計算でよい」。大きければ代償は NFS 5.1 時間へ戻る。

**距離尺度はモデルの属性にする。** dlib はユークリッド、ArcFace は L2 正規化後の
コサイン距離。0.4 という dlib 用の閾値を持ち込まない。

使い方::

    python scripts/measure_embedding_models.py --db data/photoarchive.db \
        --report docs/history/details/2026-10-02-embedding-model-comparison.md

ArcFace の ONNX は ``models/w600k_r50.onnx`` に手置きする（README 参照）。
無ければ (a)(b) だけを測り、理由を report に書いて終わる。
"""

from __future__ import annotations

import argparse
import io
import sqlite3
import statistics as st
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from photoarchive_ai import db, face  # noqa: E402

# **「読める撮影日時か」の判断を、ここで書き直さない。** 正本は `dates.parse_date`。
# このリポジトリは同じ判断を2か所に持ったせいで2度壊れている
# （`0000-00-00` だけを見ていて `TTTT-TT-TTTTT:TT:TT` が素通りした）。
# `"TTTT-TT-TTTTT:TT:TT"[:10]` は**10文字あるので長さでは弾けない。**
from photoarchive_ai.dates import parse_date  # noqa: E402

# ---------------------------------------------------------------------------
# 距離尺度
# ---------------------------------------------------------------------------

EUCLIDEAN = "euclidean"
COSINE = "cosine"

def normalize_rows(matrix: np.ndarray) -> np.ndarray:
    """各行を L2 正規化する。長さ0の行はそのまま返す（0除算を避ける）。"""
    matrix = np.asarray(matrix, dtype=np.float64)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return matrix / norms


def distance_matrix(embeddings: np.ndarray, metric: str) -> np.ndarray:
    """(N, N) の距離行列。

    ``COSINE`` は L2 正規化してから ``1 - cos`` を返す。**モデルごとに尺度が
    違うので、呼び出し側が決める。** dlib の 0.4 という閾値を他のモデルに
    持ち込まないため。
    """
    matrix = np.asarray(embeddings, dtype=np.float64)
    if metric == COSINE:
        unit = normalize_rows(matrix)
        return np.clip(1.0 - unit @ unit.T, 0.0, 2.0)
    if metric != EUCLIDEAN:
        raise ValueError(f"知らない距離尺度: {metric}")
    square = np.sum(matrix**2, axis=1)
    squared = np.maximum(square[:, None] + square[None, :] - 2.0 * (matrix @ matrix.T), 0.0)
    return np.sqrt(squared)


def top1_accuracy(
    distances: np.ndarray,
    person_ids: Sequence[int],
    media_ids: Sequence[int],
) -> Tuple[float, int]:
    """手本を1件ずつ抜いたときの1位正解率。``(正解率, 評価できた件数)``。

    **同じ写真に写っている顔は候補から外す。** 同じ写真の同一人物は切り出しが
    ほぼ同じで距離が極端に小さく、入れると実力を測れない（2026-09-20 の測定も
    同じ条件なので、数字を並べられるようにそろえてある）。
    """
    person_ids = np.asarray(person_ids)
    media_ids = np.asarray(media_ids)
    correct = 0
    evaluated = 0
    for index in range(len(person_ids)):
        candidates = media_ids != media_ids[index]
        if not candidates.any():
            continue
        row = distances[index][candidates]
        predicted = person_ids[candidates][int(np.argmin(row))]
        evaluated += 1
        correct += int(predicted == person_ids[index])
    if evaluated == 0:
        return 0.0, 0
    return correct / evaluated, evaluated


@dataclass
class Separation:
    """同一人物と別人の距離を、行事の中と行事をまたぐに分けて持つ。"""

    within_same: List[float]
    within_diff: List[float]
    cross_same: List[float]
    cross_diff: List[float]

    def gap(self, within: bool) -> Optional[float]:
        """別人の中央値 − 同一人物の中央値。**大きいほど分けられる。**"""
        same = self.within_same if within else self.cross_same
        diff = self.within_diff if within else self.cross_diff
        if not same or not diff:
            return None
        return st.median(diff) - st.median(same)


def pair_separation(
    distances: np.ndarray,
    person_ids: Sequence[int],
    media_ids: Sequence[int],
    events: Sequence[str],
) -> Separation:
    """顔のペアを4通りに分けて距離を集める。

    **プールした平均を出さない。** 2026-09-20 の測定は行事をまたぐペアで薄まった
    平均を見ていたため、「分布がほぼ重なっている」と読んでしまった。行事の中だけ
    なら差は 4.2 倍あった。**その取り違えを繰り返さないための分け方。**

    ``events`` が空文字の顔（撮影日時が読めない。実データで 10.4%）は、
    **行事が決まらないので「行事をまたぐ」側にも入れない。** 分からないものを
    どちらかに混ぜると、どちらの数字も信じられなくなる。
    """
    result = Separation([], [], [], [])
    count = len(person_ids)
    for i in range(count):
        for j in range(i + 1, count):
            if media_ids[i] == media_ids[j]:
                continue
            value = float(distances[i][j])
            same_person = person_ids[i] == person_ids[j]
            if not events[i] or not events[j]:
                continue
            if events[i] == events[j]:
                (result.within_same if same_person else result.within_diff).append(value)
            else:
                (result.cross_same if same_person else result.cross_diff).append(value)
    return result


def threshold_sweep(
    same: Sequence[float], diff: Sequence[float], steps: int = 400
) -> Optional[Tuple[float, float, float]]:
    """``(閾値, 同一人物を拾う率, 別人を誤る率)`` のうち差が最大のもの。

    **モデルごとに尺度が違うので、閾値そのものは比べない。** 比べるのは
    「どれだけ拾えて、どれだけ誤るか」。
    """
    if not same or not diff:
        return None
    low = min(min(same), min(diff))
    high = max(max(same), max(diff))
    best: Optional[Tuple[float, float, float]] = None
    for threshold in np.linspace(low, high, steps):
        true_positive = sum(1 for value in same if value <= threshold) / len(same)
        false_positive = sum(1 for value in diff if value <= threshold) / len(diff)
        if best is None or (true_positive - false_positive) > (best[1] - best[2]):
            best = (float(threshold), true_positive, false_positive)
    return best


# ---------------------------------------------------------------------------
# 手本の読み出し
# ---------------------------------------------------------------------------


@dataclass
class FaceRecord:
    face_id: int
    person_id: int
    media_id: int
    event: str
    age: Optional[int]
    thumbnail: Optional[bytes]
    stored_embedding: Optional[np.ndarray]

    def rgb(self) -> Optional[np.ndarray]:
        """サムネイルを RGB 配列に戻す。**元写真を読まない。**"""
        if not self.thumbnail:
            return None
        with Image.open(io.BytesIO(self.thumbnail)) as image:
            return np.asarray(image.convert("RGB"))


def folder_of(path: str) -> str:
    """パスを収めているフォルダ。``/a/b/c.jpg`` → ``/a/b``。

    **ここに置いているのは、測定スクリプトが本体を変えない約束のため。**
    行事単位の束ね（Phase 3 手順3）で `db.folder_of` が入ったら、そちらへ寄せる。
    """
    head, separator, _ = path.rpartition("/")
    return head if separator else ""


def load_manual_faces(database_path: str) -> List[FaceRecord]:
    """手動割り当て（手本）の顔を読む。**読み取り専用で開く。**"""
    uri = f"file:{database_path}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT f.id, f.person_id, f.media_id, f.age, f.thumbnail, f.embedding,"
            " m.path, m.shooting_date"
            " FROM Face f JOIN Media m ON m.id = f.media_id"
            f" WHERE f.assign_source = '{db.ASSIGN_MANUAL}'"
            " ORDER BY f.id"
        ).fetchall()
    finally:
        connection.close()
    records = []
    for row in rows:
        # 行事 = フォルダ × 日。**日付として読めない顔は行事を決めない**（空文字）。
        # 長さで判定しないこと（`TTTT-TT-TT` は10文字ある）。
        taken = parse_date(row["shooting_date"])
        event = f"{folder_of(row['path'])}\t{taken.isoformat()}" if taken else ""
        records.append(
            FaceRecord(
                face_id=row["id"],
                person_id=row["person_id"],
                media_id=row["media_id"],
                event=event,
                age=row["age"],
                thumbnail=row["thumbnail"],
                stored_embedding=db.decode_embedding(row["embedding"]),
            )
        )
    return records


# ---------------------------------------------------------------------------
# 特徴量の作り方（4通り）
# ---------------------------------------------------------------------------


@dataclass
class Variant:
    key: str
    label: str
    metric: str
    embed: Callable[[FaceRecord], Optional[np.ndarray]]
    note: str = ""


def stored_embedding(record: FaceRecord) -> Optional[np.ndarray]:
    """(a) いま DB にある dlib 特徴量。原寸から作られたもの。"""
    return record.stored_embedding


def dlib_from_thumbnail(record: FaceRecord) -> Optional[np.ndarray]:
    """(b) dlib をサムネイルから作り直す。**サムネイル化の代償を切り出す。**

    サムネイルは検出矩形の生クロップなので、画像全体を顔の位置として渡す。
    `face.compute_embedding` が `face_rect` でさらに正方形化とパディングを
    かけるため、原寸経路と同じ規約を通る。
    """
    rgb = record.rgb()
    if rgb is None:
        return None
    height, width = rgb.shape[:2]
    return face.compute_embedding(rgb, (0, width, height, 0))


class ArcFaceEmbedder:
    """(c)(d) InsightFace の ArcFace（512次元）を onnxruntime で動かす。

    **`insightface` パッケージは入れない。** モデル動物園と GPU 版 onnxruntime を
    引き込むので、認識用の ONNX 1本だけを読む。

    前処理は InsightFace の `ArcFaceONNX` と同じ。入力は **RGB**、
    ``(x - 127.5) / 127.5``、NCHW。
    """


    def __init__(self, model_path: Path, align: bool):
        import onnxruntime

        self.align = align
        options = onnxruntime.SessionOptions()
        options.log_severity_level = 3
        self.session = onnxruntime.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self.input_name = self.session.get_inputs()[0].name
        self.fallbacks = 0

    def _prepare(self, rgb: np.ndarray) -> np.ndarray:
        import cv2

        size = face.ARCFACE_INPUT_SIZE
        if self.align:
            points = face.detect_five_points(rgb)
            if points is None:
                # 整列できない顔。**捨てずに縮小で通し、件数を数える。**
                # 捨てると (c) と (d) で母集団が変わり、比べられなくなる。
                self.fallbacks += 1
            else:
                matrix = face.similarity_transform(points, face.ARCFACE_TEMPLATE)
                return cv2.warpAffine(rgb, matrix, (size, size), flags=cv2.INTER_LINEAR)
        return cv2.resize(rgb, (size, size), interpolation=cv2.INTER_LINEAR)

    def __call__(self, record: FaceRecord) -> Optional[np.ndarray]:
        rgb = record.rgb()
        if rgb is None:
            return None
        prepared = self._prepare(rgb).astype(np.float32)
        blob = (prepared - face.ARCFACE_INPUT_MEAN) / face.ARCFACE_INPUT_STD
        blob = np.transpose(blob, (2, 0, 1))[None, ...]
        output = self.session.run(None, {self.input_name: blob})[0]
        return np.asarray(output[0], dtype=np.float32)


def build_variants(arcface_path: Optional[Path]) -> List[Variant]:
    variants = [
        Variant("a", "dlib / 原寸（DB の値・現行）", EUCLIDEAN, stored_embedding),
        Variant("b", "dlib / サムネイル", EUCLIDEAN, dlib_from_thumbnail),
    ]
    if arcface_path is not None and arcface_path.exists():
        variants.append(
            Variant(
                "c",
                "ArcFace / サムネイル・整列なし",
                COSINE,
                ArcFaceEmbedder(arcface_path, align=False),
            )
        )
        aligned = ArcFaceEmbedder(arcface_path, align=True)
        variants.append(
            Variant("d", "ArcFace / サムネイル・5点整列", COSINE, aligned)
        )
    return variants


# ---------------------------------------------------------------------------
# 測定
# ---------------------------------------------------------------------------


@dataclass
class Result:
    variant: Variant
    used: int
    skipped: int
    seconds: float
    top1: float
    evaluated: int
    separation: Separation

    @property
    def seconds_per_face(self) -> float:
        return self.seconds / self.used if self.used else 0.0


def measure(variant: Variant, records: Sequence[FaceRecord]) -> Optional[Result]:
    started = time.monotonic()
    vectors: List[np.ndarray] = []
    kept: List[FaceRecord] = []
    skipped = 0
    for record in records:
        vector = variant.embed(record)
        if vector is None:
            skipped += 1
            continue
        vectors.append(np.asarray(vector, dtype=np.float64))
        kept.append(record)
    elapsed = time.monotonic() - started
    if len(kept) < 2:
        return None
    embeddings = np.vstack(vectors)
    distances = distance_matrix(embeddings, variant.metric)
    person_ids = [record.person_id for record in kept]
    media_ids = [record.media_id for record in kept]
    events = [record.event for record in kept]
    accuracy, evaluated = top1_accuracy(distances, person_ids, media_ids)
    return Result(
        variant=variant,
        used=len(kept),
        skipped=skipped,
        seconds=elapsed,
        top1=accuracy,
        evaluated=evaluated,
        separation=pair_separation(distances, person_ids, media_ids, events),
    )


def _median(values: Sequence[float]) -> str:
    return f"{st.median(values):.3f}" if values else "—"


def _gap(value: Optional[float]) -> str:
    return f"{value:+.3f}" if value is not None else "—"


def format_report(
    results: Sequence[Result], records: Sequence[FaceRecord], total_faces: int, notes: Sequence[str]
) -> str:
    lines: List[str] = []
    lines.append("# 特徴量モデルの比較（Issue #57）")
    lines.append("")
    lines.append(
        f"手本 **{len(records)} 件**で測った。顔画像は `Face.thumbnail` から復元しており、"
        "**元写真（NFS）を1枚も読んでいない。**"
    )
    lines.append("")
    lines.append("## 1位正解率と、分けられるかどうか")
    lines.append("")
    lines.append(
        "| # | 特徴量 / 画像の出どころ | 尺度 | 1位正解率 | 行事の中の差 | 行事をまたぐ差 | 使えた件数 |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for result in results:
        lines.append(
            f"| ({result.variant.key}) | {result.variant.label} |"
            f" {'ユークリッド' if result.variant.metric == EUCLIDEAN else 'コサイン'} |"
            f" **{result.top1 * 100:.1f}%** |"
            f" {_gap(result.separation.gap(within=True))} |"
            f" {_gap(result.separation.gap(within=False))} |"
            f" {result.used}（除外 {result.skipped}） |"
        )
    lines.append("")
    lines.append("**差は「別人の距離の中央値 − 同一人物の距離の中央値」。大きいほど分けられる。**")
    lines.append("**尺度が違うモデルの距離そのものは比べられないので、差と正解率で比べる。**")
    lines.append("")
    lines.append("## 距離の内訳")
    lines.append("")
    lines.append("| # | 行事の中・同一 | 行事の中・別人 | またぐ・同一 | またぐ・別人 |")
    lines.append("|---|---|---|---|---|")
    for result in results:
        sep = result.separation
        lines.append(
            f"| ({result.variant.key}) | {_median(sep.within_same)} (n={len(sep.within_same)}) |"
            f" {_median(sep.within_diff)} (n={len(sep.within_diff)}) |"
            f" {_median(sep.cross_same)} (n={len(sep.cross_same)}) |"
            f" {_median(sep.cross_diff)} (n={len(sep.cross_diff)}) |"
        )
    lines.append("")
    lines.append("## 閾値を振ったときの拾い方と誤り方")
    lines.append("")
    lines.append("| # | 行事の中（閾値 → 拾う/誤る） | 行事をまたぐ（閾値 → 拾う/誤る） |")
    lines.append("|---|---|---|")
    for result in results:
        sep = result.separation
        cells = []
        for same, diff in ((sep.within_same, sep.within_diff), (sep.cross_same, sep.cross_diff)):
            best = threshold_sweep(same, diff)
            cells.append(
                "—"
                if best is None
                else f"{best[0]:.3f} → 同一 {best[1] * 100:.0f}% / 誤り {best[2] * 100:.0f}%"
            )
        lines.append(f"| ({result.variant.key}) | {cells[0]} | {cells[1]} |")
    lines.append("")
    lines.append("## 全件に広げたときの所要時間")
    lines.append("")
    lines.append(f"顔 {total_faces:,} 件への外挿。**サムネイル由来なら NFS を読まない。**")
    lines.append("")
    lines.append("| # | 1件あたり | 全件 |")
    lines.append("|---|---|---|")
    for result in results:
        per = result.seconds_per_face
        lines.append(
            f"| ({result.variant.key}) | {per * 1000:.1f}ms |"
            f" {per * total_faces / 60:.0f}分 |"
        )
    lines.append("")
    if notes:
        lines.append("## 備考")
        lines.append("")
        for note in notes:
            lines.append(f"- {note}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 同じ写真に写る顔のペアでの他人誤認率
# ---------------------------------------------------------------------------

#: 他人誤認率を測るときに振る閾値。
CONFUSION_THRESHOLDS = (0.40, 0.50, 0.55, 0.60, 0.65, 0.70, 0.80)

#: ペアの分類。
LABELLED_DIFFERENT = "手本どうし・別人"
LABELLED_SAME = "手本どうし・同一人物"
ASSUMED_DIFFERENT = "片方以上が未割当（別人と仮定）"


def same_photo_pairs(database_path: str, version: str, metric: str) -> Dict[str, List[float]]:
    """**同じ写真に写る2つの顔**のペアの距離を、分類ごとに集める。

    仕様書 §8.3 の dlib の表と**同じ数え方**。本番の `match` が解くのは
    「未割当（大半が他人）を手本と照合する」問題なので、**手本どうしの測定では
    実害が出る誤りを測れない。** ここが他人誤認率のいちばん近い代理。

    **「同じ写真なら別人」は仮定で、外れることがある**（鏡・ポスター・壁の
    写真立て・顔でないものの誤検出）。外れたぶんは誤りに数えられるので、
    **この率は実際より高めに出る。**

    ただし dlib の測定より1つ改善してある。**両方が手本で同じ人物のペアは、
    本当に同一人物なので誤りに数えない**（分類を分けて返す）。

    版が一致する特徴量だけを読む（`reembed` の途中は混在する）。
    """
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT f.id, f.media_id, f.person_id, f.assign_source, f.embedding"
            " FROM Face f"
            " WHERE f.embedding IS NOT NULL AND f.embed_version = ?"
            " AND f.media_id IN (SELECT media_id FROM Face WHERE embedding IS NOT NULL"
            "                    AND embed_version = ?"
            "                    GROUP BY media_id HAVING COUNT(*) >= 2)"
            " ORDER BY f.media_id, f.id",
            (version, version),
        ).fetchall()
    finally:
        connection.close()

    by_media: Dict[int, List[sqlite3.Row]] = {}
    for row in rows:
        by_media.setdefault(row["media_id"], []).append(row)

    buckets: Dict[str, List[float]] = {
        LABELLED_DIFFERENT: [],
        LABELLED_SAME: [],
        ASSUMED_DIFFERENT: [],
    }
    for faces in by_media.values():
        if len(faces) < 2:
            continue
        matrix = np.vstack([db.decode_embedding(row["embedding"]) for row in faces])
        distances = distance_matrix(matrix, metric)
        for i in range(len(faces)):
            for j in range(i + 1, len(faces)):
                left, right = faces[i], faces[j]
                both_manual = (
                    left["assign_source"] == db.ASSIGN_MANUAL
                    and right["assign_source"] == db.ASSIGN_MANUAL
                )
                if both_manual and left["person_id"] == right["person_id"]:
                    key = LABELLED_SAME
                elif both_manual:
                    key = LABELLED_DIFFERENT
                else:
                    key = ASSUMED_DIFFERENT
                buckets[key].append(float(distances[i][j]))
    return buckets


def format_same_photo_report(buckets: Dict[str, List[float]], version: str, metric: str) -> str:
    lines = [
        "# 同じ写真に写る顔のペアでの他人誤認率",
        "",
        f"版 `{version}` / 尺度 {'コサイン' if metric == COSINE else 'ユークリッド'}。",
        "",
        "**仕様書 §8.3 の dlib の表と同じ数え方。** 「同じ写真なら別人」は仮定で、",
        "鏡・ポスター・誤検出で外れる。外れたぶんは誤りに数えられるので、",
        "**この率は実際より高めに出る。**",
        "",
        "| ペアの種類 | 件数 | " + " | ".join(f"≤{t:.2f}" for t in CONFUSION_THRESHOLDS) + " |",
        "|---|---|" + "---|" * len(CONFUSION_THRESHOLDS),
    ]
    for key in (ASSUMED_DIFFERENT, LABELLED_DIFFERENT, LABELLED_SAME):
        values = buckets[key]
        if not values:
            lines.append(f"| {key} | 0 | " + " | ".join("—" for _ in CONFUSION_THRESHOLDS) + " |")
            continue
        cells = [
            f"{sum(1 for v in values if v <= t) / len(values) * 100:.2f}%"
            for t in CONFUSION_THRESHOLDS
        ]
        lines.append(f"| {key} | {len(values):,} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append(
        f"**`{LABELLED_SAME}` の行は誤りではなく「拾えた」ほうの数字**"
        "（同じ写真に同じ人が2回写っている、本当に同一人物のペア）。"
    )
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(root / "data" / "photoarchive.db"))
    parser.add_argument("--report", help="結果を書き出す Markdown のパス")
    parser.add_argument(
        "--arcface",
        default=str(root / "models" / "w600k_r50.onnx"),
        help="ArcFace の ONNX。無ければ (a)(b) だけ測る",
    )
    parser.add_argument("--limit", type=int, help="先頭 N 件だけ測る（動作確認用）")
    parser.add_argument(
        "--same-photo-pairs",
        action="store_true",
        help="**保存済みの特徴量**で、同じ写真に写る顔のペアの他人誤認率を測る"
        "（モデルは要らない。仕様書 §8.3 の dlib の表と同じ数え方）。",
    )
    args = parser.parse_args(argv)

    database = Path(args.db)
    if not database.exists():
        print(f"データベースが見つかりません: {database}", file=sys.stderr)
        return 1

    if args.same_photo_pairs:
        from photoarchive_ai import embedding as embedding_model

        version, metric = embedding_model.ACTIVE.version, embedding_model.ACTIVE.metric
        report = format_same_photo_report(
            same_photo_pairs(str(database), version, metric), version, metric
        )
        if args.report:
            Path(args.report).write_text(report, encoding="utf-8")
            print(f"報告を書き出しました: {args.report}")
        else:
            print(report)
        return 0

    records = load_manual_faces(str(database))
    if args.limit:
        records = records[: args.limit]
    if len(records) < 2:
        print("手本が足りません（2件以上必要）。", file=sys.stderr)
        return 1

    notes: List[str] = []
    arcface = Path(args.arcface)
    if not arcface.exists():
        notes.append(
            f"**ArcFace を測れていない。** `{arcface}` が無いため (a)(b) だけの結果。"
            " README の手順でモデルを設置して測り直すこと。"
        )
        arcface_path = None
    else:
        arcface_path = arcface

    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        total_faces = connection.execute("SELECT COUNT(*) FROM Face").fetchone()[0]
    finally:
        connection.close()

    missing_thumbnails = sum(1 for record in records if not record.thumbnail)
    if missing_thumbnails:
        notes.append(f"サムネイルを持たない手本が {missing_thumbnails} 件あった。")

    results = []
    for variant in build_variants(arcface_path):
        result = measure(variant, records)
        if result is None:
            notes.append(f"({variant.key}) {variant.label} は測れなかった。")
            continue
        results.append(result)
        print(
            f"({variant.key}) {variant.label}: 1位正解 {result.top1 * 100:.1f}%"
            f" / 行事の中 {_gap(result.separation.gap(within=True))}"
            f" / またぐ {_gap(result.separation.gap(within=False))}"
            f" / {result.used}件 {result.seconds_per_face * 1000:.0f}ms/件"
        )
        embedder = variant.embed
        if isinstance(embedder, ArcFaceEmbedder) and embedder.fallbacks:
            notes.append(
                f"({variant.key}) 5点が取れず縮小で通した顔が {embedder.fallbacks} 件あった。"
            )

    report = format_report(results, records, total_faces, notes)
    if args.report:
        Path(args.report).write_text(report, encoding="utf-8")
        print(f"\n報告を書き出しました: {args.report}")
    else:
        print()
        print(report)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
