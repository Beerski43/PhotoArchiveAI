#!/usr/bin/env python3
"""作業履歴が伸び続けないように、古いエントリを年ごとに切り出す。

`docs/history/WORKLOG.md` は新しいセッションが毎回先頭から読む文書なので、
放っておくと読ませる文書として成り立たなくなる。直近 20 件だけを残し、
それより古いものを `docs/history/archive/WORKLOG-<年>.md` へ移す。

    python scripts/archive_worklog.py           # 切り出しを実行する
    python scripts/archive_worklog.py --check   # 要否だけ調べる(書き込まない)
    python scripts/archive_worklog.py --keep 30 # 残す件数を変える

エントリの区切りは `## YYYY-MM-DD — 題名` の見出し行。
何度実行しても結果が変わらない(冪等)。
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

DEFAULT_KEEP = 20
INDEX_HEADING = "## 過去の履歴"
ENTRY_PATTERN = re.compile(r"^## (\d{4})-(\d{2})-(\d{2})\b")


@dataclass
class Entry:
    """WORKLOG の1エントリ。見出し行と、次の見出しまでの本文。"""

    date: str
    heading: str
    body: str

    @property
    def year(self) -> str:
        return self.date[:4]

    def render(self) -> str:
        return f"{self.heading}\n{self.body}".rstrip() + "\n"


def split_document(text: str) -> Tuple[str, List[Entry], List[str]]:
    """前書き・エントリ・エントリ以外の節に分ける。

    「過去の履歴」の索引は毎回作り直すので、ここでは捨てる。
    """
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("## ")]
    if not starts:
        return text.rstrip() + "\n", [], []

    preamble = "\n".join(lines[: starts[0]]).rstrip()
    bounds = starts + [len(lines)]
    entries: List[Entry] = []
    strays: List[str] = []
    for index, start in enumerate(starts):
        heading = lines[start]
        body = "\n".join(lines[start + 1 : bounds[index + 1]]).rstrip()
        match = ENTRY_PATTERN.match(heading)
        if match:
            entries.append(Entry(date="-".join(match.groups()), heading=heading, body=body))
        elif heading.strip() != INDEX_HEADING:
            strays.append(heading)
    return preamble + "\n", entries, strays


def sort_newest_first(entries: List[Entry]) -> List[Entry]:
    """日付の新しい順。同じ日付は元の並びを保つ。"""
    return sorted(entries, key=lambda entry: entry.date, reverse=True)


def render_worklog(preamble: str, entries: List[Entry], archives: List[str]) -> str:
    parts = [preamble.rstrip() + "\n"]
    for entry in entries:
        parts.append("\n" + entry.render())
    if archives:
        parts.append("\n" + INDEX_HEADING + "\n\n")
        for name in archives:
            year = name.replace("WORKLOG-", "").replace(".md", "")
            parts.append(f"- [{year}]({Path('archive') / name})\n")
    return "".join(parts)


def render_archive(year: str, entries: List[Entry]) -> str:
    header = (
        f"# 作業履歴 {year}\n\n"
        "`WORKLOG.md` から切り出したもの。新しいものが上。\n"
    )
    parts = [header]
    for entry in entries:
        parts.append("\n" + entry.render())
    return "".join(parts)


def load_archive(path: Path) -> List[Entry]:
    if not path.exists():
        return []
    _, entries, _ = split_document(path.read_text(encoding="utf-8"))
    return entries


def merge(existing: List[Entry], incoming: List[Entry]) -> List[Entry]:
    """既にあるエントリは残し、無いものだけ足す。見出し行をキーにする。"""
    by_heading: Dict[str, Entry] = {entry.heading: entry for entry in existing}
    for entry in incoming:
        by_heading.setdefault(entry.heading, entry)
    return sort_newest_first(list(by_heading.values()))


def archive(worklog_path: Path, keep: int, check_only: bool) -> int:
    if not worklog_path.exists():
        print(f"  {worklog_path} が無い")
        return 0

    preamble, entries, strays = split_document(worklog_path.read_text(encoding="utf-8"))
    for heading in strays:
        print(f"  警告: エントリとして読めない見出しがある: {heading}")

    ordered = sort_newest_first(entries)
    kept, overflow = ordered[:keep], ordered[keep:]

    archive_dir = worklog_path.parent / "archive"
    if check_only:
        if overflow:
            years = sorted({entry.year for entry in overflow}, reverse=True)
            print(
                f"  履歴の切り出しが必要です: {len(overflow)} 件 ({', '.join(years)})"
                "  ->  python scripts/archive_worklog.py"
            )
            return 1
        print(f"  作業履歴は {len(kept)} 件。切り出しは不要")
        return 0

    by_year: Dict[str, List[Entry]] = {}
    for entry in overflow:
        by_year.setdefault(entry.year, []).append(entry)

    if by_year:
        archive_dir.mkdir(parents=True, exist_ok=True)
    for year, moved in sorted(by_year.items()):
        path = archive_dir / f"WORKLOG-{year}.md"
        merged = merge(load_archive(path), moved)
        path.write_text(render_archive(year, merged), encoding="utf-8")
        print(f"  {path} へ {len(moved)} 件を移した(計 {len(merged)} 件)")

    names = sorted(
        (path.name for path in archive_dir.glob("WORKLOG-*.md")), reverse=True
    ) if archive_dir.exists() else []
    worklog_path.write_text(render_worklog(preamble, kept, names), encoding="utf-8")
    if by_year:
        print(f"  {worklog_path} に直近 {len(kept)} 件を残した")
    else:
        print(f"  作業履歴は {len(kept)} 件。切り出しは不要")
    return 0


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--worklog",
        default=str(Path(__file__).resolve().parents[1] / "docs/history/WORKLOG.md"),
        help="WORKLOG.md のパス",
    )
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="残す件数")
    parser.add_argument(
        "--check", action="store_true", help="書き込まず、切り出しの要否だけ返す"
    )
    args = parser.parse_args(argv)
    return archive(Path(args.worklog), args.keep, args.check)


if __name__ == "__main__":
    sys.exit(main())
