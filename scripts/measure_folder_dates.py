#!/usr/bin/env python3
"""フォルダ名から起こした撮影時期を、EXIF の撮影日時と突き合わせる（Issue #65）。

**本体のコードを変えない。実データに書かない。NFS を読まない**（パスだけを見る）。

EXIF が読める写真を正解として、`dates.folder_date_range` の区間に EXIF の日付が
入るかを数える。**読み方の規約を推測で決めないための道具**で、規約を変えたら
これを流し直して、不一致のフォルダを1つずつ見ること。

不一致は規則では消せないものが残る（EXIF の誤り・境目をまたぐ・別時期の写真の混入）。
分類は ``docs/history/details/2026-10-10-folder-dates-measured.md``。

使い方::

    python scripts/measure_folder_dates.py --db data/photoarchive.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from photoarchive_ai.dates import folder_date_range, parse_date  # noqa: E402


def precision(found: Optional[Tuple]) -> str:
    """区間の粗さ。``month`` / ``months`` / ``year`` / ``none``。"""
    if found is None:
        return "none"
    start, end = found
    if (start.month, end.month) == (1, 12):
        return "year"
    return "month" if start.month == end.month else "months"


def measure(rows: Iterable[Tuple[str, Optional[str], Optional[int]]]) -> Dict:
    """``(path, shooting_date, face_count)`` を突き合わせた集計を返す。

    - ``checked``: EXIF が読める写真の、区間の粗さごとの ``{一致: 件数}``
    - ``mismatched``: 不一致のフォルダ → EXIF の日付の一覧
    - ``target``: EXIF の読めない**顔のある**写真の、区間の粗さごとの件数
    """
    checked: Dict[str, Counter] = defaultdict(Counter)
    mismatched: Dict[str, List[str]] = defaultdict(list)
    target: Counter = Counter()
    for path, shooting_date, face_count in rows:
        found = folder_date_range(path)
        exif = parse_date(shooting_date)
        if exif is None:
            if face_count:
                target[precision(found)] += 1
            continue
        if found is None:
            checked["none"][None] += 1
            continue
        ok = found[0] <= exif <= found[1]
        checked[precision(found)][ok] += 1
        if not ok:
            mismatched[path.rsplit("/", 1)[0]].append(exif.isoformat())
    return {"checked": checked, "mismatched": mismatched, "target": target}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="data/photoarchive.db")
    args = parser.parse_args(argv)
    connection = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = connection.execute("SELECT path, shooting_date, face_count FROM Media").fetchall()
    result = measure(rows)

    print("## EXIF が読める写真での突き合わせ")
    total = Counter()
    for kind, counts in sorted(result["checked"].items()):
        print(f"- {kind}: 一致 {counts[True]} / 不一致 {counts[False]} / 区間なし {counts[None]}")
        total.update(counts)
    print(f"- 計: 一致 {total[True]} / 不一致 {total[False]} / 区間なし {total[None]}")
    print()
    print("## EXIF の読めない、顔のある写真（推測で埋まる対象）")
    for kind, count in sorted(result["target"].items()):
        print(f"- {kind}: {count}")
    print()
    print(f"## 不一致のフォルダ（{len(result['mismatched'])} 件・多い順）")
    for folder, dates in sorted(result["mismatched"].items(), key=lambda kv: (-len(kv[1]), kv[0])):
        print(f"- {len(dates)}\t{min(dates)}〜{max(dates)}\t{folder}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
