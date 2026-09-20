"""CLAUDE.md と README.md に、実装で変わる数字を置かない。

テストの件数や所要時間を書くと、**関係のない変更のたびに更新が要る。**
更新し忘れれば、いちばんよく読まれる2つの文書が静かに嘘になる。

件数・所要時間は `docs/testing/` 側に置くか、実行して確かめる
（`./scripts/run_regression.sh` の最終行、`pytest --collect-only`）。
出力例を載せるときは `N passed / M failed (X.XXs)` のように書式だけ示す。
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# ここに挙げた文書は「読み手向けの決まりごと」を書く場所で、
# 実装の状態を写す場所ではない。
STABLE_DOCS = ("CLAUDE.md", "README.md")

FORBIDDEN = (
    # pytest の実行結果そのもの。書式を示すなら N / M を使う。
    (re.compile(r"\d+\s+passed"), "テストの成功件数"),
    (re.compile(r"\d+\s+failed"), "テストの失敗件数"),
    # 「全196件」のような、テストを足すたびにずれる件数。
    (re.compile(r"全\s*\d+\s*件"), "テストの総件数"),
)


@pytest.mark.parametrize("name", STABLE_DOCS)
def test_the_document_holds_no_number_that_changes_with_the_code(name):
    path = REPO_ROOT / name
    offenders = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for pattern, label in FORBIDDEN:
            if pattern.search(line):
                offenders.append(f"{name}:{number}  {label}: {line.strip()}")

    assert not offenders, (
        "実装で変わる数字が書かれている。関係のない変更のたびに更新が要るので、\n"
        "docs/testing/ 側へ移すか、書式だけ示すこと"
        "(例: 回帰テスト: N passed / M failed (X.XXs))。\n  " + "\n  ".join(offenders)
    )


def test_the_check_would_catch_a_number_that_slipped_back_in():
    """この番人自身が働くことを確かめる。"""
    slipped = "全196件がおよそ3秒で終わります。回帰テスト: 194 passed / 0 failed"

    assert sorted(label for pattern, label in FORBIDDEN if pattern.search(slipped)) == [
        "テストの失敗件数",
        "テストの成功件数",
        "テストの総件数",
    ]


def test_the_format_used_in_the_documents_is_allowed():
    """書式だけを示す書き方は通ること。"""
    allowed = "回帰テスト: N passed / M failed (X.XXs) 実行日: YYYY-MM-DD"

    assert not [label for pattern, label in FORBIDDEN if pattern.search(allowed)]


# ---------------------------------------------------------------------------
# スキルが指す CLAUDE.md の節が実在すること
# ---------------------------------------------------------------------------

SECTION_REFERENCE = re.compile(r"`?CLAUDE\.md`?\s*(?:の)?\s*§\s*([0-9]+(?:\.[0-9]+)*)")


def _claude_md_sections() -> set:
    """`CLAUDE.md` の見出しが持つ節番号。"""
    text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    sections = set()
    for line in text.splitlines():
        match = re.match(r"#+\s+([0-9]+(?:\.[0-9]+)*)[.．]?\s", line)
        if match:
            number = match.group(1)
            sections.add(number)
            # 「§3」で §3.3 を含む節全体を指すことがあるので、親も登録する
            while "." in number:
                number = number.rsplit(".", 1)[0]
                sections.add(number)
    return sections


def _documents_that_reference_claude_md():
    for path in sorted(REPO_ROOT.glob(".claude/skills/*/SKILL.md")):
        yield path
    for path in sorted((REPO_ROOT / "docs").rglob("*.md")):
        yield path


def test_a_skill_does_not_point_at_a_section_that_does_not_exist():
    """**「詳細は §N を見よ」が切れていないこと。**

    スキルや文書から `CLAUDE.md` の節を指している箇所がある。節を足したり
    番号を振り直したりすると、**指し先が黙ってずれる。** 読み手は
    「そんな節は無い」ことにも気づけない（`test_plan_stays_true.py` が
    フェーズ文書のリンクを見張っているのと同じ理由）。
    """
    sections = _claude_md_sections()
    broken = []
    for path in _documents_that_reference_claude_md():
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            for referenced in SECTION_REFERENCE.findall(line):
                if referenced not in sections:
                    relative = path.relative_to(REPO_ROOT)
                    broken.append(f"{relative}:{number}  §{referenced}  {line.strip()}")

    assert not broken, (
        "CLAUDE.md に無い節を指している。節を足したか、番号を振り直したはず。\n"
        f"いまある節: {', '.join(sorted(sections))}\n  " + "\n  ".join(broken)
    )


def test_the_section_check_would_catch_a_dangling_pointer():
    """この番人自身が働くことを確かめる。"""
    sections = _claude_md_sections()

    # 実在する節は拾えている（§3.3 はこの検査を足した回に増えた節）
    assert {"3", "3.2", "3.3", "4", "5", "8"} <= sections
    # 指し先の取り出しが効いている
    assert SECTION_REFERENCE.findall("`CLAUDE.md` §3.3 と CLAUDE.md の §99.9") == [
        "3.3",
        "99.9",
    ]
    assert "99.9" not in sections
