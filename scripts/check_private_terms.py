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

一覧は**本体の checkout** の ``config/private_terms.yml`` から引く。git のワークツリー
（``.claude/worktrees/`` など）には git に入らないファイルが無いので、ワークツリーの直下を
見ると一覧が見つからず、検査が丸ごと飛んでいた（PR #82 のレビュー指摘1）。

一覧が無いとき:
- **hook から呼ばれたとき（``--staged`` / ``--message``）は止める**（1 で終わる）。hook を
  有効にした人は検査を望んでいるので、一覧が見えないのは設定の不備（利用者の決定・PR #82）
- 回帰テストと手での実行は、知らせて 0 で終わる（一覧を持たない人の手元を止めない）
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
TERMS_IN_CHECKOUT = Path("config") / "private_terms.yml"
#: `git commit -v` が差分の前に置く線。git はこの線から下をメッセージに含めない。
SCISSORS = "------------------------ >8 ------------------------"
#: エディタで書くときに git が入れる案内。これがあればコメント行は git が捨てる。
EDITOR_TEMPLATE = "Please enter the commit message for your changes."


def default_terms(cwd: Path) -> Path:
    """一覧の既定の場所。**本体の checkout**（``--git-common-dir`` の親）の下。

    ワークツリーでも本体でも同じ場所になる。git が使えなければ ``cwd`` の下。
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=cwd, check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return cwd / TERMS_IN_CHECKOUT
    return Path(out).parent / TERMS_IN_CHECKOUT


@dataclass(frozen=True)
class Term:
    text: str
    replace: str
    ignore_case: bool = False

    @property
    def pattern(self) -> "re.Pattern[str]":
        return re.compile(re.escape(self.text), re.IGNORECASE if self.ignore_case else 0)


class TermsError(Exception):
    """一覧が読めない。**メッセージに語を入れない**（位置だけ）。"""


def load_terms(path: Path) -> List[Term]:
    """一覧を読む。**長い語から順に並べる**（短い語が長い語の一部を先に置き換えないように）。

    壊れた YAML のエラーは問題の行を抜粋して出すので、そのまま投げると**語が端末に出る**
    （PR #82 のレビュー指摘3）。位置だけを出す。
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        where = f" の {mark.line + 1} 行目" if mark is not None else ""
        raise TermsError(f"{path.name}{where}が YAML として読めません") from None
    terms = []
    for number, entry in enumerate((data.get("terms") or []) if isinstance(data, dict) else [], start=1):
        try:
            text = str(entry["text"])
            value = str(entry["replace"])
        except (KeyError, TypeError):
            raise TermsError(f"{path.name} の {number} 件目に text と replace がありません") from None
        if not text:
            raise TermsError(f"{path.name} の {number} 件目の語が空です")
        terms.append(Term(text, value, bool(entry.get("ignore_case", False))))
    return sorted(terms, key=lambda term: len(term.text), reverse=True)


def message_body(text: str, comment: str = "#") -> str:
    """commit-msg が受け取ったファイルのうち、**コミットに残る部分**（PR #82 のレビュー指摘2）。

    - はさみ線（`git commit -v`）から下は差分なので落とす。差分の削除行に語があると、
      語を消すためのコミットが止まっていた
    - エディタで書いたとき（git の案内がある）は、git が捨てるコメント行も落とす。
      ``-m`` のときは落とさない（``#77 ...`` で始まる1行目を見逃さないため）
    """
    lines = []
    for line in text.splitlines():
        if line.lstrip(comment).strip() == SCISSORS:
            break
        lines.append(line)
    if any(line.startswith(comment) and EDITOR_TEMPLATE in line for line in lines):
        lines = [line for line in lines if not line.startswith(comment)]
    return "\n".join(lines)


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


def _comment_char() -> str:
    try:
        value = _git("config", "core.commentChar").decode("utf-8").strip()
    except subprocess.CalledProcessError:
        return "#"
    return value if value and value != "auto" else "#"


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
    parser.add_argument("--terms", type=Path, help="語の一覧（既定は本体の checkout の config/private_terms.yml）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="コミットしようとしている中身を見る")
    mode.add_argument("--message", type=Path, help="コミットメッセージのファイルを見る")
    mode.add_argument("--fix", action="store_true", help="追跡中のファイルの中身を置き換える")
    mode.add_argument("--filter-repo-expressions", type=Path, help="git filter-repo --replace-text の式を書き出す")
    args = parser.parse_args(argv)

    terms_path = args.terms or default_terms(REPO_ROOT)
    from_hook = bool(args.staged or args.message)
    if not terms_path.exists():
        if from_hook:
            print(
                f"公開しない語の一覧が見つかりません: {terms_path}\n"
                "hook が有効なのに検査できないので、コミットを止めます。本体の checkout の"
                " config/private_terms.yml に一覧を置いてください（形は config/private_terms.sample.yml）。"
            )
            return 1
        print(f"公開しない語の一覧がありません（{terms_path.name}）。検査を飛ばします。")
        return 0
    try:
        terms = load_terms(terms_path)
    except TermsError as error:
        print(f"公開しない語の一覧が読めません: {error}")
        return 1

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
        text = message_body(args.message.read_text(encoding="utf-8"), _comment_char())
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
