#!/usr/bin/env python3
"""行事（フォルダ×日）で顔を束ねたときの効き目を、実データで測る（Issue #61）。

**本体のコードを変えない。実データに書かない。NFS を読まない。モデルも要らない**
（保存済みの `Face.embedding` だけを読む）。

答えたい問いは3つ。

1. **行事で束ねる意味がまだあるか。** 「行事の中は 4.7 倍分けやすい」は
   **dlib での測定**で、ArcFace では行事をまたぐ分離が 12.7 倍になった。
   **行事で区切る必要が消えている可能性がある**ので、同じ数え方で測り直す
2. **束ねると人間の決定が何回になるか。** 未割当を1件ずつ選ぶ作業が、
   束の数まで落ちるかどうか
3. **束は誤って混ざっていないか。** 手本は 126 件しかないので、
   **同じ写真に2つ入った束**を誤りの代わりに数える（同じ写真に同じ人物が
   2回写ることは稀。仕様書 §8.3 と同じ考え方）

使い方::

    python scripts/measure_event_clustering.py --db data/photoarchive.db \
        --report docs/history/details/2026-10-04-event-clustering-measured.md
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics as st
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from photoarchive_ai import clustering, db, embedding  # noqa: E402

# **「読める撮影日時か」の判断を書き直さない。** 正本は `dates.parse_date`
# （このリポジトリは同じ判断を2か所に持って2度壊れている）。
from photoarchive_ai.dates import parse_date  # noqa: E402

#: 振ってみる連結の上限。**モデルの閾値（照合用）とは別物**なので、
#: `embedding.ACTIVE.threshold` を既定にしない。
THRESHOLDS = (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60)


@dataclass
class FaceRow:
    face_id: int
    media_id: int
    person_id: Optional[int]
    assign_source: Optional[str]
    vector: np.ndarray


@dataclass
class Event:
    """フォルダ×日。**日付が読めない顔は行事を決められない**ので入らない。"""

    folder: str
    day: str
    faces: List[FaceRow] = field(default_factory=list)

    @property
    def unassigned(self) -> List[FaceRow]:
        return [face for face in self.faces if face.assign_source is None]

    @property
    def manual(self) -> List[FaceRow]:
        return [face for face in self.faces if face.assign_source == db.ASSIGN_MANUAL]

    @property
    def photos(self) -> int:
        return len({face.media_id for face in self.faces})

    @property
    def most_faces_in_one_photo(self) -> int:
        """1枚にいちばん多く写っていた顔の数。**その行事に写っている人数の下限。**

        同じ写真の顔はほぼ必ず別人なので、束の数がこれを大きく下回っていたら
        束ね過ぎ、大きく上回っていたら割れ過ぎを疑う足がかりになる。
        """
        counts: Dict[int, int] = defaultdict(int)
        for face in self.faces:
            counts[face.media_id] += 1
        return max(counts.values()) if counts else 0


def load_events(database_path: str) -> Tuple[List[Event], Dict[str, int]]:
    """特徴量を持つ顔を行事ごとに集める。**読み取り専用で開く。**

    除外（`rejected`）の顔は入れない。**もう人が判断した顔**なので、
    束ねても決定は減らない。
    """
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT f.id, f.media_id, f.person_id, f.assign_source, f.embedding,"
            " m.path, m.shooting_date"
            " FROM Face f JOIN Media m ON m.id = f.media_id"
            " WHERE f.embedding IS NOT NULL"
            f" AND f.embed_version = '{embedding.ACTIVE.version}'"
            f" AND (f.assign_source IS NULL OR f.assign_source = '{db.ASSIGN_MANUAL}')"
            " ORDER BY f.quality_score DESC, f.id ASC",
            (),
        ).fetchall()
    finally:
        connection.close()

    events: Dict[Tuple[str, str], Event] = {}
    counts = {"faces": 0, "undated": 0}
    for row in rows:
        counts["faces"] += 1
        taken = parse_date(row["shooting_date"])
        if taken is None:
            counts["undated"] += 1
            continue
        key = (db.folder_of(row["path"]), taken.isoformat())
        event = events.setdefault(key, Event(folder=key[0], day=key[1]))
        event.faces.append(
            FaceRow(
                face_id=row["id"],
                media_id=row["media_id"],
                person_id=row["person_id"],
                assign_source=row["assign_source"],
                vector=db.decode_embedding(row["embedding"]),
            )
        )
    ordered = sorted(events.values(), key=lambda e: (-len(e.faces), e.day, e.folder))
    return ordered, counts


# ---------------------------------------------------------------------------
# 1. 行事の中と外の分離（手本どうしのペアで測る）
# ---------------------------------------------------------------------------


@dataclass
class Separation:
    within_same: List[float] = field(default_factory=list)
    within_other: List[float] = field(default_factory=list)
    across_same: List[float] = field(default_factory=list)
    across_other: List[float] = field(default_factory=list)

    def gap(self, within: bool) -> Optional[float]:
        same = self.within_same if within else self.across_same
        other = self.within_other if within else self.across_other
        if not same or not other:
            return None
        return st.median(other) - st.median(same)


def measure_separation(events: Sequence[Event]) -> Separation:
    """手本どうしの距離を「行事の中／またぐ」×「同一人物／別人」に分ける。

    **同じ写真のペアは外す**（同じ写真の同一人物は必ず近く、別人は必ず
    別人なので、どちらの桶でも実態より良く見せる）。
    `scripts/measure_embedding_models.py` の数え方にそろえてある。
    """
    teachers: List[Tuple[FaceRow, str]] = []
    for event in events:
        for face in event.manual:
            teachers.append((face, f"{event.folder}\t{event.day}"))
    separation = Separation()
    if len(teachers) < 2:
        return separation
    vectors = np.vstack([face.vector for face, _ in teachers])
    distances = embedding.pairwise_distances(vectors, vectors)
    for left in range(len(teachers)):
        for right in range(left + 1, len(teachers)):
            first, first_event = teachers[left]
            second, second_event = teachers[right]
            if first.media_id == second.media_id:
                continue
            value = float(distances[left, right])
            same_person = first.person_id == second.person_id
            same_event = first_event == second_event
            if same_event and same_person:
                separation.within_same.append(value)
            elif same_event:
                separation.within_other.append(value)
            elif same_person:
                separation.across_same.append(value)
            else:
                separation.across_other.append(value)
    return separation


# ---------------------------------------------------------------------------
# 2. 束ねたときの決定の数と、混ざり方
# ---------------------------------------------------------------------------


@dataclass
class ClusterRun:
    threshold: float
    faces: int = 0
    decisions: int = 0
    singletons: int = 0
    biggest: int = 0
    clusters_ge2: int = 0
    mixed_photo_clusters: int = 0
    teacher_clusters: int = 0
    teacher_mixed: int = 0
    teacher_pairs: int = 0
    teacher_pairs_together: int = 0
    photo_pairs: int = 0
    photo_pairs_together: int = 0
    split_ratio: List[float] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def reduction(self) -> float:
        return 1.0 - (self.decisions / self.faces) if self.faces else 0.0

    @property
    def mixed_photo_rate(self) -> float:
        return self.mixed_photo_clusters / self.clusters_ge2 if self.clusters_ge2 else 0.0

    @property
    def photo_pair_rate(self) -> Optional[float]:
        """**他人誤認率**（仕様書 §8.3 と同じ数え方を束ねに当てたもの）。

        同じ写真に写る顔のペアを「別人」とみなし、そのうち何割を同じ束へ
        入れてしまったかを数える。**閾値の選び方を照合とそろえるため**に
        要る（照合は 0.45 で 1.06%）。
        """
        return self.photo_pairs_together / self.photo_pairs if self.photo_pairs else None

    @property
    def teacher_purity(self) -> Optional[float]:
        if not self.teacher_clusters:
            return None
        return 1.0 - self.teacher_mixed / self.teacher_clusters

    @property
    def teacher_recall(self) -> Optional[float]:
        if not self.teacher_pairs:
            return None
        return self.teacher_pairs_together / self.teacher_pairs


def measure_clustering(events: Sequence[Event], threshold: float) -> ClusterRun:
    """行事ごとに束ね、決定の数と混ざり方を数える。

    **決定の数 = 未割当を含む束の数。** 手本だけの束は人が済ませた判断なので
    数えない。
    """
    run = ClusterRun(threshold=threshold)
    started = time.monotonic()
    for event in events:
        if not event.unassigned:
            continue
        faces = event.faces
        clusters = clustering.cluster_faces(
            [face.face_id for face in faces],
            [face.vector for face in faces],
            threshold=threshold,
        )
        by_id = {face.face_id: face in event.unassigned for face in faces}
        rows = {face.face_id: face for face in faces}
        run.faces += len(event.unassigned)
        decisions = 0
        for cluster in clusters:
            members = [rows[face_id] for face_id in cluster.face_ids]
            if any(by_id[face_id] for face_id in cluster.face_ids):
                decisions += 1
                if cluster.size == 1:
                    run.singletons += 1
            run.biggest = max(run.biggest, cluster.size)
            if cluster.size >= 2:
                run.clusters_ge2 += 1
                media = [member.media_id for member in members]
                if len(set(media)) < len(media):
                    # 同じ写真の顔が1つの束に入った。**ほぼ確実に別人**。
                    run.mixed_photo_clusters += 1
            teachers = [member for member in members if member.assign_source == db.ASSIGN_MANUAL]
            if len(teachers) >= 2:
                run.teacher_clusters += 1
                if len({teacher.person_id for teacher in teachers}) > 1:
                    run.teacher_mixed += 1
        run.decisions += decisions
        if event.most_faces_in_one_photo:
            run.split_ratio.append(decisions / event.most_faces_in_one_photo)

        # 他人誤認: 同じ写真に写る顔のペアを、同じ束へ入れてしまった割合。
        # **ほぼ必ず別人**なので、照合の閾値を選んだときと同じ数え方になる。
        by_media: Dict[int, List[FaceRow]] = defaultdict(list)
        for face in faces:
            by_media[face.media_id].append(face)
        label_in_event = {
            face_id: index
            for index, cluster in enumerate(clusters)
            for face_id in cluster.face_ids
        }
        for members in by_media.values():
            for left in range(len(members)):
                for right in range(left + 1, len(members)):
                    run.photo_pairs += 1
                    if (
                        label_in_event[members[left].face_id]
                        == label_in_event[members[right].face_id]
                    ):
                        run.photo_pairs_together += 1

        # 手本の取りこぼし: 同じ行事・同じ人物の手本が、別の写真にあるのに
        # 別の束へ割れていないか。**割れていると、その人物を2回選ぶことになる。**
        label_of = {
            face_id: index
            for index, cluster in enumerate(clusters)
            for face_id in cluster.face_ids
        }
        teachers = event.manual
        for left in range(len(teachers)):
            for right in range(left + 1, len(teachers)):
                first, second = teachers[left], teachers[right]
                if first.person_id != second.person_id:
                    continue
                if first.media_id == second.media_id:
                    continue
                run.teacher_pairs += 1
                if label_of[first.face_id] == label_of[second.face_id]:
                    run.teacher_pairs_together += 1
    run.seconds = time.monotonic() - started
    return run


# ---------------------------------------------------------------------------
# 報告
# ---------------------------------------------------------------------------


def _median(values: Sequence[float]) -> str:
    return f"{st.median(values):.3f}" if values else "—"


def _gap(value: Optional[float]) -> str:
    return f"{value:+.3f}" if value is not None else "—"


def _percent(value: Optional[float]) -> str:
    return f"{value * 100:.1f}%" if value is not None else "—"


def _rate(value: Optional[float]) -> str:
    return f"{value * 100:.2f}%" if value is not None else "—"


def format_report(
    events: Sequence[Event],
    counts: Dict[str, int],
    separation: Separation,
    runs: Sequence[ClusterRun],
) -> str:
    model = embedding.ACTIVE
    sizes = [len(event.unassigned) for event in events if event.unassigned]
    lines = [
        f"# 行事ごとの束ねを実データで測る（Issue #61・{time.strftime('%Y-%m-%d')}）",
        "",
        f"特徴量は **`{model.version}`**（{model.dimensions}次元・{model.metric}）。"
        "**保存済みの `Face.embedding` だけを読んでいる**（モデルも元写真も読まない）。",
        "",
        "再現方法。",
        "",
        "```bash",
        "python scripts/measure_event_clustering.py --db data/photoarchive.db",
        "```",
        "",
        "## 0. 行事の分布",
        "",
        "| 見たもの | 値 |",
        "|---|---|",
        f"| 対象の顔（特徴量あり・未割当か手本） | {counts['faces']:,} 件 |",
        f"| うち**撮影日時が読めず行事を決められない** | {counts['undated']:,} 件 |",
        f"| 行事（フォルダ×日） | {len(events):,} 個 |",
        f"| 未割当を含む行事 | {len(sizes):,} 個 |",
        f"| 1行事あたりの未割当（中央/平均/最大） | "
        f"{st.median(sizes):.0f} / {st.mean(sizes):.1f} / {max(sizes):,} |",
        "",
    ]
    lines.append("| 未割当の多さ | 行事の数 | 未割当の占める割合 |")
    lines.append("|---|---|---|")
    total_unassigned = sum(sizes)
    for bound in (2, 5, 10, 20, 50):
        big = [size for size in sizes if size >= bound]
        share = sum(big) / total_unassigned if total_unassigned else 0.0
        lines.append(f"| {bound} 件以上 | {len(big):,} 個 | {share * 100:.1f}% |")
    lines.extend(
        [
            "",
            "## 1. 行事の中と外の分離（手本どうしのペア・同じ写真は外す）",
            "",
            "| 測ったもの | 同一人物(中央) | 別人(中央) | 差 |",
            "|---|---|---|---|",
            f"| **同じ行事の中** | {_median(separation.within_same)}"
            f" (n={len(separation.within_same)}) | {_median(separation.within_other)}"
            f" (n={len(separation.within_other)}) | "
            f"**{_gap(separation.gap(within=True))}** |",
            f"| 行事をまたぐ | {_median(separation.across_same)}"
            f" (n={len(separation.across_same)}) | {_median(separation.across_other)}"
            f" (n={len(separation.across_other)}) | {_gap(separation.gap(within=False))} |",
            "",
            "## 2. 束ねたときの決定の数と混ざり方",
            "",
            "- **決定の数** = 未割当を含む束の数。これが人間が人物を選ぶ回数",
            "- **他人誤認率** = 同じ写真に写る顔のペア（**ほぼ必ず別人**）を"
            "同じ束へ入れた割合。**照合の閾値を選んだときと同じ数え方**（仕様書 §8.3）",
            "- **混ざった束** = 同じ写真の顔が入ってしまった束の割合"
            "（2件以上の束のうち）。人が画面で見て気づく側の数字",
            "- **手本の純度** = 手本2件以上を含む束のうち、全員同じ人物だった割合",
            "- **手本の再現** = 同じ行事・同じ人物・別の写真の手本ペアが、"
            "同じ束に入った割合",
            "",
            "| 連結の上限 | 決定の数 | 減り方 | 1件だけの束 | 最大の束 |"
            " **他人誤認率** | 混ざった束 | 手本の純度 | 手本の再現 | 所要 |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for run in runs:
        lines.append(
            f"| {run.threshold:.2f} | {run.decisions:,} | **{run.reduction * 100:.1f}%** |"
            f" {run.singletons:,} | {run.biggest:,} |"
            f" **{_rate(run.photo_pair_rate)}** | {run.mixed_photo_rate * 100:.2f}% |"
            f" {_percent(run.teacher_purity)} | {_percent(run.teacher_recall)} |"
            f" {run.seconds:.1f}秒 |"
        )
    if runs:
        lines.extend(
            [
                "",
                f"未割当は **{runs[0].faces:,} 件**。"
                "**減り方 = 1 − 決定の数 ÷ 未割当の数。**",
                "",
                "束の数が「その行事の1枚に写っていた最大の顔数」の何倍かも見ている"
                "（**写っている人数の下限**なので、大きく上回っていたら割れ過ぎ）。",
                "",
                "| 連結の上限 | 束の数 ÷ 1枚の最大顔数（中央） |",
                "|---|---|",
            ]
        )
        for run in runs:
            ratio = st.median(run.split_ratio) if run.split_ratio else 0.0
            lines.append(f"| {run.threshold:.2f} | {ratio:.2f} 倍 |")
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(root / "data" / "photoarchive.db"))
    parser.add_argument("--report", help="結果を書き出す Markdown のパス")
    parser.add_argument(
        "--thresholds",
        help="連結の上限をカンマ区切りで（既定: " + ",".join(f"{t:g}" for t in THRESHOLDS) + "）",
    )
    parser.add_argument(
        "--max-faces",
        type=int,
        help="この件数を超える行事を飛ばす（既定は飛ばさず `clustering.MAX_FACES` まで）",
    )
    args = parser.parse_args(argv)

    database = Path(args.db)
    if not database.exists():
        print(f"データベースが見つかりません: {database}", file=sys.stderr)
        return 1

    thresholds = (
        tuple(float(value) for value in args.thresholds.split(","))
        if args.thresholds
        else THRESHOLDS
    )

    events, counts = load_events(str(database))
    if not events:
        print("行事を作れる顔がありません。", file=sys.stderr)
        return 1
    if args.max_faces:
        skipped = [event for event in events if len(event.faces) > args.max_faces]
        if skipped:
            print(f"顔が {args.max_faces} 件を超える行事を {len(skipped)} 個飛ばします。")
        events = [event for event in events if len(event.faces) <= args.max_faces]

    separation = measure_separation(events)
    print(
        f"行事 {len(events):,} 個 / 手本どうしのペア"
        f" 行事の中 {len(separation.within_same) + len(separation.within_other):,}"
        f" ・またぐ {len(separation.across_same) + len(separation.across_other):,}"
    )
    print(
        f"  行事の中の差 {_gap(separation.gap(within=True))}"
        f" / またぐ差 {_gap(separation.gap(within=False))}"
    )

    runs = []
    for threshold in thresholds:
        run = measure_clustering(events, threshold)
        runs.append(run)
        print(
            f"  {threshold:.2f}: 決定 {run.decisions:,} 件"
            f"（{run.reduction * 100:.1f}% 減）"
            f" / 他人誤認 {_rate(run.photo_pair_rate)}"
            f" / 混ざった束 {run.mixed_photo_rate * 100:.2f}%"
            f" / 手本の純度 {_percent(run.teacher_purity)}"
            f" / 再現 {_percent(run.teacher_recall)}"
            f" / {run.seconds:.1f}秒"
        )

    report = format_report(events, counts, separation, runs)
    if args.report:
        Path(args.report).write_text(report, encoding="utf-8")
        print(f"\n報告を書き出しました: {args.report}")
    else:
        print()
        print(report)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
