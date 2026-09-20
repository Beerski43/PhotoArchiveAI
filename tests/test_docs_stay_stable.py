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


# ---------------------------------------------------------------------------
# テスト項目一覧の、見出しと表の対応
# ---------------------------------------------------------------------------

TEST_CASES = REPO_ROOT / "docs/testing/TEST_CASES.md"
FILE_SECTION = re.compile(r"^### `(test_\w+\.py)`")


def _sections_of_test_cases(path=None):
    """`### \\`test_*.py\\`` ごとに、見出し行と、そのあとに続く行を返す。"""
    lines = (path or TEST_CASES).read_text(encoding="utf-8").splitlines()
    sections = []
    for number, line in enumerate(lines):
        match = FILE_SECTION.match(line)
        if match:
            sections.append({"name": match.group(1), "line": number + 1, "body": []})
        elif sections:
            if line.startswith("### ") or line.startswith("## "):
                sections.append(None)  # 別の節に入った印
            elif sections[-1] is not None:
                sections[-1]["body"].append(line)
    return [section for section in sections if section]


def test_a_test_file_section_owns_the_table_under_it(path=None):
    """**見出しと表がずれていないこと。**

    見出しが自分の表の**下**に落ちると、その表は前の節にぶら下がる。
    実際に起きた（`test_gui_migration.py` の7行が `test_evaluation.py` の
    節に紛れ、移行の節は空になった）。読み手は、どの節の表なのかを
    取り違える。
    """
    offenders = []
    for section in _sections_of_test_cases(path):
        filled = [line for line in section["body"] if line.strip()]
        place = f"{section['name']}（{section['line']} 行目）"
        if not filled:
            # 見出しだけが残っている＝自分の表を前の節に持っていかれている
            offenders.append(f"{place}: 表も説明も無い")
            continue
        if not filled[0].startswith("|"):
            continue  # 説明文から始まるのは正しい
        # 表から始まるなら、1行目はヘッダで、2行目は区切り行のはず。
        # 途中の行から始まっているのは、ヘッダを前の節に置いてきた印。
        if len(filled) < 2 or not filled[1].startswith("|--"):
            offenders.append(f"{place}: 表のヘッダが無く、途中の行から始まっている")
    assert not offenders, (
        "見出しが自分の表の下に落ちている。表は見出しの**あと**に置くこと。\n  "
        + "\n  ".join(offenders)
    )


def test_a_table_is_not_split_by_a_blank_line(path=None):
    """**表の途中に空行を入れないこと。**

    GitHub の描画器は、空行のあとの行を表として扱わない
    （`| z | w |` がそのまま文字として出る）。実際に11行が表から外れていた。
    """
    offenders = []
    for section in _sections_of_test_cases(path):
        body = section["body"]
        for index, line in enumerate(body):
            if line.strip():
                continue
            previous = body[index - 1] if index else ""
            following = next((l for l in body[index + 1 :] if l.strip()), "")
            if previous.startswith("|") and following.startswith("|"):
                offenders.append(f"{section['name']}: 表の途中に空行がある（{following[:40]}…）")
    assert not offenders, (
        "表の途中に空行がある。GitHub はそのあとの行を表にしない。\n  "
        + "\n  ".join(offenders)
    )


def test_the_table_checks_would_catch_a_broken_document(tmp_path):
    """**番人自身が働くことを確かめる。** 素通りする検査は無いのと同じ。

    実際に起きた2つの壊れ方を、そのまま書いた文書で落ちることを見る。
    """
    split_table = tmp_path / "split.md"
    split_table.write_text(
        "### `test_a.py` — あ（1件）\n\n"
        "| テスト | 内容 |\n|---|---|\n| `test_x` | x |\n"
        "\n"                      # ← 表の途中の空行
        "| `test_y` | y |\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="表の途中に空行"):
        test_a_table_is_not_split_by_a_blank_line(split_table)

    stray_heading = tmp_path / "stray.md"
    stray_heading.write_text(
        "### `test_a.py` — あ（1件）\n\n"
        "| テスト | 内容 |\n|---|---|\n| `test_x` | x |\n\n"
        "### `test_b.py` — い（1件）\n"   # ← 見出しが表の下に落ちている
        "| `test_y` | y |\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="表の下に落ちている"):
        test_a_test_file_section_owns_the_table_under_it(stray_heading)

    # いまの文書は両方とも通る
    test_a_table_is_not_split_by_a_blank_line()
    test_a_test_file_section_owns_the_table_under_it()
