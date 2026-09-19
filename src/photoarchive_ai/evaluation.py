"""手動割り当てを正解とみなして、``match`` の取りこぼし率と誤一致率を実測する。

なぜ要るか:

``matcher.DEFAULT_THRESHOLD`` の根拠は **誤一致率だけ** で決まっている。
同一写真に写る別人同士が閾値を下回る割合は測ったが、**本来紐づくべき顔を
落とす率（取りこぼし率）は測っていない。** 閾値を下げれば誤一致は減り
取りこぼしは増えるので、片側だけでは閾値を決められない。

測り方:

手動割り当ての顔は **正解が分かっている唯一の集合**。ここから1件を抜き、
残りを手本にして ``match`` と同じ判定を通し、抜いた顔が元の人物へ戻るかを見る
（1件抜き交差検証）。結果は「正解 / 取りこぼし / 誤り」の3つに分かれる。

設計上の約束:

- **判定は ``matcher`` の関数をそのまま呼ぶ。** 測るものと実際に動くものが
  ずれたら、実測値そのものが嘘になる。
- **DBには書かない。** 読み出しだけで完結する。
- **同じ写真に写る同一人物の手本は既定で外す。** 抜いた顔とほぼ同じ手本が
  残っていると必ず当たるため。実運用で紐づけたいのは別の写真に写った顔。
- **自分の人物の手本が1つも残らない顔は評価から外す。** 構造上かならず
  取りこぼしになり、混ぜると取りこぼし率が実態より悪く出る。
"""

import unicodedata
from typing import Any, Callable, Dict, List, Optional, Sequence

import numpy as np

from . import db
from .matcher import DEFAULT_MARGIN, DEFAULT_THRESHOLD, _best_match, _distances

#: 既定で試す閾値。0.4 は現在の既定値、0.45 は ROADMAP が緩める候補として挙げる値。
DEFAULT_THRESHOLDS = (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60)

#: 距離行列を一度に作る行数。全手本ぶんを一度に作ると (N, N) になるので刻む。
EVAL_CHUNK_SIZE = 512

#: 評価の3分類。
CORRECT = "correct"
MISSED = "missed"
WRONG = "wrong"


def _empty_threshold_row(threshold: float) -> Dict[str, Any]:
    return {
        "threshold": threshold,
        CORRECT: 0,
        MISSED: 0,
        WRONG: 0,
        "per_person": {},
    }


def _finalize(row: Dict[str, Any], evaluated: int) -> Dict[str, Any]:
    """件数から率を出す。評価対象が0件なら率は None（0.0 と区別する）。"""
    row["accuracy"] = row[CORRECT] / evaluated if evaluated else None
    row["miss_rate"] = row[MISSED] / evaluated if evaluated else None
    row["wrong_rate"] = row[WRONG] / evaluated if evaluated else None
    return row


def evaluate_match(
    connection,
    thresholds: Sequence[float] = DEFAULT_THRESHOLDS,
    margin: float = DEFAULT_MARGIN,
    keep_same_media: bool = False,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, Any]:
    """手本の顔を1件ずつ抜いて、閾値ごとの正解・取りこぼし・誤りを数える。"""
    # 重複を落とす。集計先は閾値の値で引くので、同じ値が2つあると片方が0件、
    # もう片方が2倍になり、正解率が 200% になる。CLI 側でも弾いているが、
    # ここを直に呼ぶ経路(テストや将来の GUI)も守る。
    thresholds = sorted({float(value) for value in thresholds})
    faces = db.load_manual_faces(connection)
    total = int(faces.face_ids.shape[0])

    summary: Dict[str, Any] = {
        "teachers": total,
        "evaluated": 0,
        "skipped": 0,
        "skipped_per_person": {},
        "teachers_per_person": {},
        "person_names": {
            int(person["id"]): person["name"] for person in db.list_persons(connection)
        },
        "margin": margin,
        "keep_same_media": keep_same_media,
        "thresholds": [_empty_threshold_row(value) for value in thresholds],
    }

    for person_id in faces.person_ids.tolist():
        summary["teachers_per_person"][person_id] = (
            summary["teachers_per_person"].get(person_id, 0) + 1
        )

    if total == 0:
        # 進捗は呼ばない。0/0 のバーを出しても伝わるものが無い。
        for row in summary["thresholds"]:
            _finalize(row, 0)
        return summary

    person_ids = faces.person_ids
    media_ids = faces.media_ids
    rows_by_threshold = {row["threshold"]: row for row in summary["thresholds"]}

    for start in range(0, total, EVAL_CHUNK_SIZE):
        stop = min(start + EVAL_CHUNK_SIZE, total)
        distances = _distances(faces.embeddings[start:stop], faces.embeddings)
        for offset in range(stop - start):
            index = start + offset
            truth = int(person_ids[index])
            row = distances[offset].astype(np.float64, copy=True)
            # 自分自身は手本にならない。同じ写真に写る同一人物の顔も、
            # 抜いた顔とほぼ同じなので既定では外す。
            row[index] = np.inf
            if not keep_same_media:
                row[(media_ids == media_ids[index]) & (person_ids == truth)] = np.inf
            if not np.isfinite(row[person_ids == truth]).any():
                # 自分の人物の手本が残っていない。必ず取りこぼすので数えない。
                summary["skipped"] += 1
                summary["skipped_per_person"][truth] = (
                    summary["skipped_per_person"].get(truth, 0) + 1
                )
                continue

            summary["evaluated"] += 1
            for threshold in thresholds:
                best_person, _ = _best_match(row, person_ids, threshold, margin)
                if best_person is None:
                    outcome = MISSED
                elif best_person == truth:
                    outcome = CORRECT
                else:
                    outcome = WRONG
                target = rows_by_threshold[threshold]
                target[outcome] += 1
                per_person = target["per_person"].setdefault(
                    truth, {CORRECT: 0, MISSED: 0, WRONG: 0}
                )
                per_person[outcome] += 1
        if progress_callback is not None:
            progress_callback(stop, total, f"{summary['evaluated']} evaluated")

    for row in summary["thresholds"]:
        _finalize(row, summary["evaluated"])
    return summary


def _percent(value: Optional[float]) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def _display_width(text: str) -> int:
    """端末での見た目の幅。日本語は2桁ぶんを占める。

    ``str.ljust`` は文字数で揃えるので、人物名に日本語が混ざると列がずれる。
    """
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def _pad(text: str, width: int, align: str = ">") -> str:
    space = " " * max(0, width - _display_width(text))
    return text + space if align == "<" else space + text


def _table(header: Sequence[str], rows: Sequence[Sequence[str]], align: str) -> List[str]:
    """見た目の幅で揃えた表。``align`` は列ごとの ``<`` / ``>``。"""
    widths = [
        max(_display_width(str(cell)) for cell in column)
        for column in zip(header, *rows)
    ]
    lines = ["  " + "  ".join(_pad(str(cell), width, ">") for cell, width in zip(header, widths))]
    for row in rows:
        lines.append(
            "  "
            + "  ".join(
                _pad(str(cell), width, side)
                for cell, width, side in zip(row, widths, align)
            )
        )
    return lines


def _detail_threshold(summary: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """人物ごとの内訳を出す閾値。既定の閾値があればそれ、無ければ先頭。"""
    if not summary["thresholds"]:
        return None
    for row in summary["thresholds"]:
        if abs(row["threshold"] - DEFAULT_THRESHOLD) < 1e-9:
            return row
    return summary["thresholds"][0]


def format_report(summary: Dict[str, Any]) -> str:
    """実測の結果を人が読む形にする。CLI から切り離してテストできるようにする。"""
    if summary["teachers"] == 0:
        return (
            "手本になる顔がありません。photoarchive-gui で顔を人物に割り当ててから"
            "再実行してください。"
        )

    names = summary["person_names"]
    lines: List[str] = []
    same_media = "含める" if summary["keep_same_media"] else "除く"
    lines.append(
        f"手本 {summary['teachers']} 件 / 人物 {len(summary['teachers_per_person'])} 名"
        f"（評価対象 {summary['evaluated']} 件、対象外 {summary['skipped']} 件）"
    )
    lines.append(
        f"マージン {summary['margin']}、同じ写真に写る同一人物の手本は{same_media}"
    )
    if summary["skipped"]:
        detail = "、".join(
            f"{names.get(person_id, f'#{person_id}')} {count}件"
            for person_id, count in sorted(summary["skipped_per_person"].items())
        )
        lines.append(
            f"対象外は、その顔を抜くと自分の人物の手本が残らないもの: {detail}"
        )

    if summary["evaluated"] == 0:
        lines.append("")
        lines.append(
            "評価できる顔がありません。**1人につき、別の写真から2枚以上**"
            " 割り当ててから再実行してください。"
        )
        return "\n".join(lines)

    lines.append("")
    lines.append("閾値ごとの実測（1件抜き交差検証）")
    lines.extend(
        _table(
            ("閾値", "正解", "取りこぼし", "誤り", "正解/取りこぼし/誤り"),
            [
                (
                    f"{row['threshold']:.2f}",
                    _percent(row["accuracy"]),
                    _percent(row["miss_rate"]),
                    _percent(row["wrong_rate"]),
                    f"{row[CORRECT]} / {row[MISSED]} / {row[WRONG]} 件",
                )
                for row in summary["thresholds"]
            ],
            align=">>>>>",
        )
    )

    detail = _detail_threshold(summary)
    if detail is not None:
        lines.append("")
        lines.append(f"人物ごと（閾値 {detail['threshold']:.2f}）")
        lines.extend(
            _table(
                ("人物", "手本", "正解", "取りこぼし", "誤り"),
                [
                    (
                        names.get(person_id, f"#{person_id}"),
                        str(summary["teachers_per_person"].get(person_id, 0)),
                        str(detail["per_person"][person_id][CORRECT]),
                        str(detail["per_person"][person_id][MISSED]),
                        str(detail["per_person"][person_id][WRONG]),
                    )
                    for person_id in sorted(detail["per_person"])
                ],
                align="<>>>>",
            )
        )

    lines.append("")
    lines.append("取りこぼし＝未割当のまま残った顔、誤り＝別の人物に割り当てられた顔。")
    lines.append(
        "連写のように似た手本が別のファイルにあると、正解は実運用より甘く出る。"
    )
    return "\n".join(lines)
