#!/usr/bin/env python3
"""セッションを切る前の点検。**次のセッションが迷子にならないか**を見る。

回帰テスト（`tests/test_plan_stays_true.py`）は文書の形だけを見張る。
こちらは **git と GitHub の状態**を見る。文書に書いていない作業が
ブランチの中に浮いていないか、が主眼。

実際に起きたこと（Issue #49）: `Person.birth_date` の実装が
`feature/#48_person-birth-date` にコミット・push されたまま PR が無く、
**どこにも書かれていなかった。**

**いちばん大事な性質: 点検が働かなかったことを、点検が隠さない。**
`gh` を使えなかった、remote に届かなかった、という場合は「異常なし」ではなく
**「確認できなかった」と警告に出す。** 確認して問題が無かったことと、
確認できなかったことを同じ顔で出すと、壊れた番人に守られているつもりになる。

使い方:

    python scripts/check_handoff.py
    python scripts/check_handoff.py --offline   # remote を見ない(速い)

**助言だけで、終了コードは 0。** 判断は人がする。
`./scripts/run_regression.sh` の 5/5 からも呼ばれる。
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_BRANCH = "develop"
DETAILS_DIR = REPO_ROOT / "docs/history/details"
#: remote を触るコマンドの待ち時間。**PR の前に必ず通る場所なので、
#: 繋がらない環境で止まらないことのほうが、正確さより大事。
NETWORK_TIMEOUT_SECONDS = 10.0


def run(*args: str, timeout: float = NETWORK_TIMEOUT_SECONDS) -> tuple[int, str, str]:
    """外部コマンドを実行する。

    **stdout と stderr を混ぜない。** 混ぜると、`gh` が出す新版のお知らせ1行で
    JSON の読み取りが壊れ、「PR を確認できない」に化ける。
    時間切れは終了コード 124 で返す（`timeout(1)` に合わせた）。
    """
    try:
        result = subprocess.run(
            args, cwd=REPO_ROOT, capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"{timeout:.0f} 秒で返らなかった"
    except OSError as error:
        return 127, "", str(error)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def show_working_tree() -> None:
    """作業ツリーの状態。**警告にはしない。**

    `CLAUDE.md` §3 の手順どおりだと、回帰テストはコミット前に回す。
    そのとき汚れているのは正常なので、毎回鳴らすと本物の警告ごと読まれなくなる。
    """
    section("コミットしていない変更")
    _, out, _ = run("git", "status", "--porcelain")
    if not out:
        print("  なし")
        return
    for line in out.splitlines():
        print(f"  {line}")
    print("  （コミット前に回しているなら正常）")


def unmerged_branches(offline: bool) -> tuple[dict[str, list[str]], list[str]]:
    """`develop` に入っていない作業を {ブランチ名: コミット} で返す。"""
    warnings: list[str] = []
    if offline:
        print("  （--offline。手元の origin/* で判定する）")
    else:
        code, _, err = run("git", "fetch", "--quiet", "--prune")
        if code != 0:
            # **黙って古い情報で判定しない。** 取り込み忘れた remote の状態で
            # 「異常なし」と出るのが、いちばん危ない。
            warnings.append(
                f"remote を取りに行けなかったので、**古い情報で判定している**（{err or code}）"
            )

    code, out, _ = run("git", "for-each-ref", "--format=%(refname:short)",
                       "refs/heads", "refs/remotes/origin")
    if code != 0:
        warnings.append("ブランチ一覧を読めなかった。手で確認すること")
        return {}, warnings

    found: dict[str, list[str]] = {}
    for ref in out.splitlines():
        name = ref.removeprefix("origin/")
        if name in {INTEGRATION_BRANCH, "main", "HEAD"} or name in found:
            continue
        code, commits, _ = run("git", "log", "--oneline", f"origin/{INTEGRATION_BRANCH}..{ref}")
        if code == 0 and commits:
            found[name] = commits.splitlines()
    return found, warnings


def branches_mentioned_in_handoff_notes() -> set[str]:
    """申し送りに名前が出ているブランチ。**書いてあるなら浮いていない。**"""
    if not DETAILS_DIR.is_dir():
        return set()
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in DETAILS_DIR.glob("*.md")
    )
    return {word.strip("`*（）()、。 ") for word in text.split() if "feature/" in word}


def check_branches_have_pull_requests(branches: dict[str, list[str]]) -> list[str]:
    """浮いているブランチに PR があるか。

    **PR があれば作業は見える場所にある。** 申し送りに書いてあっても同じ。
    どちらも無いものだけが、誰の目にも触れない。
    """
    section(f"{INTEGRATION_BRANCH} に入っていない作業")
    if not branches:
        print("  なし")
        return []

    code, out, err = run("gh", "pr", "list", "--state", "open", "--json", "number,headRefName")
    heads: dict[str, int] = {}
    gh_available = code == 0
    if gh_available:
        try:
            heads = {pr["headRefName"]: pr["number"] for pr in json.loads(out or "[]")}
        except (json.JSONDecodeError, KeyError, TypeError):
            gh_available = False
            err = err or "gh の出力を読めなかった"

    documented = branches_mentioned_in_handoff_notes()
    warnings = []
    for name, commits in sorted(branches.items()):
        if not gh_available:
            label = "PR は確認できず"
        elif name in heads:
            label = f"PR #{heads[name]} あり"
        elif name in documented:
            label = "PR は無いが、申し送りに記載あり"
        else:
            label = "**PR も申し送りの記載も無い**"
        print(f"  {name}  ({len(commits)} コミット) — {label}")
        for line in commits:
            print(f"      {line}")
        if gh_available and name not in heads and name not in documented:
            warnings.append(
                f"{name} は push されているのに PR が無く、申し送りにも出てこない。"
                " docs/history/details/ に状態を書くこと"
            )

    if not gh_available:
        # **確認できなかったことを、異常なしと同じ顔で出さない。**
        warnings.append(
            f"PR の有無を確認できなかった（{err or 'gh を使えない'}）。手で見ること"
        )
    return warnings


def check_handoff_note() -> list[str]:
    """直近の WORKLOG から、申し送りが辿れるか。"""
    section("申し送り")
    worklog = (REPO_ROOT / "docs/history/WORKLOG.md").read_text(encoding="utf-8")
    head = worklog.split("\n## ")[1] if "\n## " in worklog else ""
    if "details/" in head:
        print("  先頭のエントリから申し送りへのリンクがある")
        return []
    print("  先頭のエントリから details/ へのリンクは無い")
    return ["途中の作業があるなら、申し送りを書いて WORKLOG の先頭から張ること"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="remote を取りに行かない。手元の origin/* だけで判定する。",
    )
    args = parser.parse_args(argv)

    warnings: list[str] = []
    show_working_tree()
    branches, fetch_warnings = unmerged_branches(args.offline)
    warnings += fetch_warnings
    warnings += check_branches_have_pull_requests(branches)
    warnings += check_handoff_note()

    print("\n" + "-" * 60)
    if warnings:
        print("気にすること:")
        for warning in warnings:
            print(f"  - {warning}")
    else:
        print("引き継ぎで気になる点は見つからなかった")
    print("-" * 60)
    # 助言に徹する。ここで落とすと、意図して途中で切る場合に邪魔になる。
    return 0


if __name__ == "__main__":
    sys.exit(main())
