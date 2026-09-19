#!/usr/bin/env python3
"""pytest の出力から、PR 本文へ貼る1行を作る。

    python scripts/summarize_pytest.py <ログファイル>
    回帰テスト: 107 passed / 0 failed (2.22s) 実行日: 2026-09-19

`scripts/run_regression.sh` から呼ぶ。**集計できなかったときは
黙って 0 を出さず、非ゼロで終了する。** シェルの grep でやっていたときは、
端末で実行すると pytest の着色エスケープに阻まれて常に
`0 passed / 0 failed` になり、しかもそれが成功のように見えていた。
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path
from typing import Dict, Optional

# 着色の SGR シーケンス。pytest は出力先が端末だと色を付ける。
ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# 「12 passed」「1 failed」「2 errors」。前後は何が来てもよい
# (着色していると数字の直前・直後にエスケープが挟まる)。
COUNT_PATTERN = re.compile(r"(\d+)\s+(passed|failed|error|errors)\b")
ELAPSED_PATTERN = re.compile(r"\bin\s+([0-9.]+)s\b")


def strip_ansi(text: str) -> str:
    return ANSI_PATTERN.sub("", text)


def find_summary_line(text: str) -> Optional[str]:
    """pytest の最終行(件数と所要時間が並ぶ行)を探す。"""
    for line in reversed(strip_ansi(text).splitlines()):
        line = line.strip()
        if not line:
            continue
        if COUNT_PATTERN.search(line) and ELAPSED_PATTERN.search(line):
            return line
    return None


def parse_summary(text: str) -> Optional[Dict[str, object]]:
    """件数と所要時間を取り出す。読み取れなければ None。

    ``failed`` にはエラー(収集時の失敗など)も足す。テストが1件も
    実行されていない状態を「失敗0件」として見せないため。
    """
    line = find_summary_line(text)
    if line is None:
        return None
    counts = {"passed": 0, "failed": 0}
    for number, label in COUNT_PATTERN.findall(line):
        if label == "passed":
            counts["passed"] += int(number)
        else:  # failed / error / errors
            counts["failed"] += int(number)
    elapsed = ELAPSED_PATTERN.search(line)
    return {
        "passed": counts["passed"],
        "failed": counts["failed"],
        "elapsed": float(elapsed.group(1)) if elapsed else None,
        "line": line,
    }


def format_summary(summary: Dict[str, object], on: Optional[date] = None) -> str:
    elapsed = summary["elapsed"]
    elapsed_text = f"{elapsed:.2f}s" if isinstance(elapsed, float) else "?s"
    stamp = (on or date.today()).isoformat()
    return (
        f"回帰テスト: {summary['passed']} passed / {summary['failed']} failed"
        f" ({elapsed_text}) 実行日: {stamp}"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("log", help="pytest の出力を落としたファイル")
    args = parser.parse_args(argv)

    text = Path(args.log).read_text(encoding="utf-8", errors="replace")
    summary = parse_summary(text)
    if summary is None:
        print(
            "回帰テスト: 集計できませんでした。pytest の出力を直接確認してください。",
            file=sys.stderr,
        )
        return 1
    print(format_summary(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
