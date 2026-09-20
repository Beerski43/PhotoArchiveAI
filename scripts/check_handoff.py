#!/usr/bin/env python3
"""セッションを切る前の点検。**次のセッションが迷子にならないか**を見る。

回帰テスト（`tests/test_plan_stays_true.py`）は文書の形だけを見張る。
こちらは **git と GitHub の状態**を見る。文書に書いていない作業が
ブランチの中に浮いていないか、が主眼。

実際に起きたこと（Issue #49）: `Person.birth_date` の実装が
`feature/#48_person-birth-date` にコミット・push されたまま PR が無く、
**どこにも書かれていなかった。** セッションを切っていたら、次の人は
ブランチ一覧を眺めるまで気づけなかった。

使い方:

    python scripts/check_handoff.py

**助言だけで、失敗しても終了コードは 0。** 判断は人がする。
`gh` が無い、または認証が切れている環境では GitHub 側の確認だけを飛ばす。
"""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_BRANCH = "develop"


def run(*args: str) -> tuple[int, str]:
    result = subprocess.run(
        args, cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def check_working_tree() -> list[str]:
    section("コミットしていない変更")
    _, out = run("git", "status", "--porcelain")
    if not out:
        print("  なし")
        return []
    for line in out.splitlines():
        print(f"  {line}")
    return ["コミットしていない変更が残っている"]


def check_unmerged_branches() -> list[str]:
    """`develop` に入っていない作業が、どこかに浮いていないか。"""
    section(f"{INTEGRATION_BRANCH} に入っていない作業")
    run("git", "fetch", "--quiet", "--prune")
    code, out = run(
        "git", "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes/origin"
    )
    if code != 0:
        print("  ブランチを読めなかった")
        return []

    warnings = []
    seen = set()
    for ref in out.splitlines():
        name = ref.removeprefix("origin/")
        if name in {INTEGRATION_BRANCH, "main", "HEAD"} or name in seen:
            continue
        seen.add(name)
        code, commits = run(
            "git", "log", "--oneline", f"origin/{INTEGRATION_BRANCH}..{ref}"
        )
        if code != 0 or not commits:
            continue
        count = len(commits.splitlines())
        print(f"  {ref}  ({count} コミット)")
        for line in commits.splitlines():
            print(f"      {line}")
        warnings.append(f"{ref} が {INTEGRATION_BRANCH} に入っていない")
    if not warnings:
        print("  なし")
    return warnings


def check_pull_requests(unmerged: list[str]) -> list[str]:
    """浮いているブランチに PR があるか。**無ければ、それが申し送りの対象。**"""
    section("未マージのブランチと PR の対応")
    code, out = run("gh", "pr", "list", "--state", "open", "--json", "number,title,headRefName")
    if code != 0:
        print("  gh を使えないので確認を飛ばす（認証切れかも）")
        return []
    try:
        prs = json.loads(out or "[]")
    except json.JSONDecodeError:
        print("  gh の出力を読めなかった")
        return []

    heads = {pr["headRefName"]: pr for pr in prs}
    for pr in prs:
        print(f"  PR #{pr['number']}  {pr['headRefName']}  {pr['title']}")
    if not prs:
        print("  open な PR は無い")

    warnings = []
    for text in unmerged:
        branch = text.split(" ")[0].removeprefix("origin/")
        if branch not in heads:
            warnings.append(
                f"**{branch} は push されているのに PR が無い。**"
                " 申し送り（docs/history/details/）に状態を書くこと"
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


def main() -> int:
    warnings: list[str] = []
    warnings += check_working_tree()
    unmerged = check_unmerged_branches()
    warnings += unmerged
    warnings += check_pull_requests(unmerged)
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
