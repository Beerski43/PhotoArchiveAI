#!/usr/bin/env python3
"""公開しない語（家族の名前・パス・誕生日など）がリポジトリに入っていないかを見る（#77）。

語の一覧は ``config/private_terms.yml`` にあり、**git に入れない**（``config/*`` は管理外）。
一覧そのものが公開しない情報なので、このスクリプトにも文書にも語を書かない。
形は ``config/private_terms.sample.yml`` を見ること。

**見つけたときは語を出さず、置き換える変数だけを出す。** この出力は回帰テストの結果として
PR 本文に貼られることがある。

使い方::

    python scripts/check_private_terms.py               # 追跡中のファイル（中身とファイル名）
    python scripts/check_private_terms.py --staged      # コミットしようとしている中身（pre-commit）
    python scripts/check_private_terms.py --message F   # コミットメッセージ（commit-msg）
    python scripts/check_private_terms.py --fix         # 追跡中のファイルの中身を一覧どおりに置き換える
    python scripts/check_private_terms.py --filter-repo-expressions OUT  # git filter-repo の式を書き出す

一覧が無い環境では、検査できないことを知らせて 0 で終わる（ほかの人の手元や CI で止めないため）。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TERMS = REPO_ROOT / "config" / "private_terms.yml"


@dataclass(frozen=True)
class Term:
    text: str
    replace: str
    ignore_case: bool = False

    @property
    def pattern(self) -> "re.Pattern[str]":
        return re.compile(re.escape(self.text), re.IGNORECASE if self.ignore_case else 0)


def load_terms(path: Path) -> List[Term]:
    """一覧を読む。**長い語から順に並べる**（短い語が長い語の一部を先に置き換えないように）。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    terms = []
    for entry in data.get("terms") or []:
        text = str(entry["text"])
        if not text:
            raise ValueError("空の語は書けません")
        terms.append(Term(text, str(entry["replace"]), bool(entry.get("ignore_case", False))))
    return sorted(terms, key=lambda term: len(term.text), reverse=True)


def find(text: str, terms: Sequence[Term]) -> Iterator[Tuple[int, Term]]:
    """``(行番号, 語)`` を返す。同じ行で同じ語は1回だけ。"""
    for number, line in enumerate(text.splitlines(), start=1):
        for term in terms:
            if term.pattern.search(line):
                yield number, term


def replace(text: str, terms: Sequence[Term]) -> str:
    """長い語から順に置き換える。置き換えた変数が、短い語に当たり直すことは無い（変数は ASCII）。"""
    for term in terms:
        text = term.pattern.sub(lambda _match, value=term.replace: value, text)
    return text


# -- git ---------------------------------------------------------------------


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, check=True, capture_output=True).stdout


def tracked_files() -> List[str]:
    return [name for name in _git("ls-files", "-z").decode("utf-8").split("\0") if name]


def staged_files() -> List[str]:
    out = _git("diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR")
    return [name for name in out.decode("utf-8").split("\0") if name]


def _decode(data: bytes) -> Optional[str]:
    """テキストなら文字列、バイナリ（NUL を含む・UTF-8 でない）なら None。"""
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


# -- 検査 --------------------------------------------------------------------


def check(names: Iterable[str], read, terms: Sequence[Term]) -> List[str]:
    """見つかった箇所を ``path:line: 公開しない語（→ 変数）`` の形で返す。語は出さない。"""
    found = []
    for name in names:
        for _, term in find(name, terms):
            found.append(f"{_safe(name, terms)}: ファイル名に公開しない語（→ {term.replace}）")
        text = _decode(read(name))
        if text is None:
            continue
        for number, term in find(text, terms):
            found.append(f"{_safe(name, terms)}:{number}: 公開しない語（→ {term.replace}）")
    return found


def _safe(name: str, terms: Sequence[Term]) -> str:
    """出力に出すファイル名からも語を消す。"""
    return replace(name, terms)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--terms", type=Path, default=DEFAULT_TERMS, help="語の一覧（既定 config/private_terms.yml）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="コミットしようとしている中身を見る")
    mode.add_argument("--message", type=Path, help="コミットメッセージのファイルを見る")
    mode.add_argument("--fix", action="store_true", help="追跡中のファイルの中身を置き換える")
    mode.add_argument("--filter-repo-expressions", type=Path, help="git filter-repo --replace-text の式を書き出す")
    args = parser.parse_args(argv)

    if not args.terms.exists():
        print(f"公開しない語の一覧がありません（{args.terms.name}）。検査を飛ばします。")
        return 0
    terms = load_terms(args.terms)

    if args.filter_repo_expressions:
        lines = []
        for term in terms:
            if term.ignore_case:
                lines.append(f"regex:(?i){re.escape(term.text)}==>{term.replace}")
            else:
                lines.append(f"literal:{term.text}==>{term.replace}")
        args.filter_repo_expressions.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"{len(lines)} 件の式を書き出しました: {args.filter_repo_expressions}")
        return 0

    if args.fix:
        changed = 0
        for name in tracked_files():
            path = REPO_ROOT / name
            if not path.is_file():
                continue
            text = _decode(path.read_bytes())
            if text is None:
                continue
            fixed = replace(text, terms)
            if fixed != text:
                path.write_text(fixed, encoding="utf-8")
                changed += 1
        print(f"{changed} ファイルを置き換えました。ファイル名は手で直してください（git mv）。")
        renames = [name for name in tracked_files() if any(True for _ in find(name, terms))]
        for name in renames:
            print(f"  ファイル名に公開しない語: {_safe(name, terms)}")
        return 0

    if args.message:
        text = args.message.read_text(encoding="utf-8")
        found = [f"コミットメッセージ:{number}: 公開しない語（→ {term.replace}）" for number, term in find(text, terms)]
    elif args.staged:
        found = check(staged_files(), lambda name: _git("show", f":{name}"), terms)
    else:
        found = check(tracked_files(), lambda name: (REPO_ROOT / name).read_bytes() if (REPO_ROOT / name).is_file() else b"", terms)

    for line in found:
        print(line)
    if found:
        print(f"公開しない語が {len(found)} か所にあります。`--fix` で置き換えられます（#77）。")
        return 1
    print("公開しない語はありません。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
