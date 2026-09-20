"""実装プランと作業履歴が、実態からずれていないことを機械で見張る。

**プランは黙って古くなる。** 誰かが嘘を書くのではなく、実装だけ進んで文書が
置き去りになる。置き去りになった文書はレビューでも気づかれにくい
（読んで矛盾に気づくには、実態を知っている必要がある）。

実際に起きたずれ（2026-09-20 の監査、Issue #49）。

- `phase-3-accuracy.md` が全件スキャン前のままで、**未スキャン 69,347 件**と
  書いてあった。実際は0件。この文書だけ読むと「手順1はこれから」に見える
- `WORKLOG.md` の先頭が最新でなかった。`CLAUDE.md` は「新しいセッションは
  先頭を読んで直近の状況をつかむ」としているので、出だしを間違える
- 申し送りを書いたのに、どこからもリンクされていなかった

ここで見張れるのは**形だけ**（順序・リンク・日付の有無）で、中身が正しいかは
見られない。それでも、**読み手が真に受ける形をしているのに古い**という
いちばん危ない状態は防げる。
"""

import re
import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLAN_DIR = REPO_ROOT / "docs/plan"
WORKLOG = REPO_ROOT / "docs/history/WORKLOG.md"
DETAILS_DIR = REPO_ROOT / "docs/history/details"

#: WORKLOG の日付見出し。`## 2026-09-20 — 題名`
ENTRY_HEADING = re.compile(r"^## (\d{4})-(\d{2})-(\d{2}) —")
#: 実データの件数。`70,297 件` `58,606件`
REAL_DATA_COUNT = re.compile(r"\d{1,3},\d{3}\s*件")
#: いつ数えたか。`2026-09-20`
DATE_IN_TEXT = re.compile(r"\d{4}-\d{2}-\d{2}")
#: 数え直す手段が書いてあるか。
RECOUNT_HINT = re.compile(r"sqlite3|SELECT COUNT")


def _worklog_dates(text: str):
    return [
        (date(int(m.group(1)), int(m.group(2)), int(m.group(3))), line)
        for line in text.splitlines()
        if (m := ENTRY_HEADING.match(line))
    ]


def _undated_counts(text: str, label: str = "") -> list[str]:
    """日付も数え直す手段も無い件数を拾う。

    **免除は節の単位にする。** 文書のどこかに `sqlite3` があれば全体を
    免除する作りにしていたが、それだと**数え直すコマンドを1つ置いた時点で、
    その文書は以後ずっと検査の対象外**になる。仕組みを入れる操作そのものが
    仕組みを無効にしていた（PR #50 のレビュー指摘2）。
    """
    lines = text.splitlines()
    offenders = []
    section_start = 0
    for index, line in enumerate(lines):
        if line.startswith("#"):
            section_start = index
        if not REAL_DATA_COUNT.search(line):
            continue
        window = "\n".join(lines[section_start : index + 1])
        if not (DATE_IN_TEXT.search(window) or RECOUNT_HINT.search(window)):
            prefix = f"{label}:" if label else "行 "
            offenders.append(f"{prefix}{index + 1}  {line.strip()}")
    return offenders


def test_the_worklog_entries_are_newest_first():
    """**日をまたぐ逆転が無いこと。**

    `CLAUDE.md` §1 は「新しいセッションは先頭を読んで直近の状況をつかむ」と
    している。並びが崩れると、**いちばん古い話を最新だと思って再開する。**

    **同じ日の中の順序は見張れない。** 見出しが日付までしか持たないため。
    1日に何本も入る日（実際にある）の並びは、人が見るしかない。
    できないことをできると書かないために、ここに限界を残す。
    """
    entries = _worklog_dates(WORKLOG.read_text(encoding="utf-8"))

    assert entries, "日付の見出しが1つも無い"
    out_of_order = [
        (previous[1], current[1])
        for previous, current in zip(entries, entries[1:])
        if previous[0] < current[0]
    ]
    assert not out_of_order, "新しいものが上になっていない:\n  " + "\n  ".join(
        f"{a}\n  の下に {b}" for a, b in out_of_order
    )


def test_every_phase_document_linked_from_the_roadmap_exists():
    """ROADMAP から張ったフェーズ文書が実在すること。

    リンク切れに気づかないまま「詳細はあちら」と書かれていると、読み手は
    **詳細が無いことにも気づけない。**
    """
    roadmap = (PLAN_DIR / "ROADMAP.md").read_text(encoding="utf-8")
    links = set(re.findall(r"\]\((phase-[\w.-]+\.md)\)", roadmap))

    assert links, "フェーズ文書へのリンクが1つも無い"
    missing = sorted(name for name in links if not (PLAN_DIR / name).is_file())
    assert not missing, f"ROADMAP が存在しない文書を指している: {missing}"


def test_a_phase_document_is_linked_from_the_roadmap():
    """**書いたフェーズ文書が迷子にならないこと。**

    `phase-N-*.md` を足したのに ROADMAP から張り忘れると、誰も読まない。
    """
    roadmap = (PLAN_DIR / "ROADMAP.md").read_text(encoding="utf-8")
    orphans = sorted(
        path.name
        for path in PLAN_DIR.glob("phase-*.md")
        if path.name not in roadmap
    )

    assert not orphans, f"ROADMAP から張られていないフェーズ文書: {orphans}"


def test_a_phase_that_has_started_names_its_issue():
    """着手したフェーズは Issue 番号を持つこと。

    `CLAUDE.md` §3 は「新しいフェーズに入るときに親 Issue を起票し、ROADMAP に
    番号を書く」としている。番号が無いまま進むと、**作業とブランチの紐づけ先が
    無くなる**（1 Issue - 1 ブランチ - 1 PR が成り立たない）。
    """
    roadmap = (PLAN_DIR / "ROADMAP.md").read_text(encoding="utf-8")
    offenders = []
    for line in roadmap.splitlines():
        if not line.startswith("| ") or "Phase" not in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        state, issues = cells[1], cells[-1]
        if "未着手" in state or "状態" in state:
            continue
        if not re.search(r"#\d+", issues):
            offenders.append(line.strip())

    assert not offenders, "着手済みなのに Issue 番号が無い行:\n  " + "\n  ".join(offenders)


@pytest.mark.parametrize("path", sorted(PLAN_DIR.glob("*.md")), ids=lambda p: p.name)
def test_a_real_data_count_in_a_plan_document_is_dated_or_recountable(path):
    """**実データの件数を、いつのものか分からない形で書かない。**

    プラン文書の数字は黙って古くなる。`phase-3-accuracy.md` は
    「未スキャン 69,347 件」と書いたまま全件スキャンが終わり、**この文書だけ
    読むと手順1が未着手に見える**状態になっていた。

    日付を添えるか、数え直すコマンドを同じ文書に置くかのどちらかを求める。
    どちらも無い数字は、読み手が現在の値だと思い込む。

    おおよその規模を語りたいだけなら「7万件規模」のように丸めて書く
    （この検査に引っかからず、古くもならない）。
    """
    offenders = _undated_counts(path.read_text(encoding="utf-8"), path.name)

    assert not offenders, (
        "いつ数えたのか分からない実データの件数がある。日付を添えるか、\n"
        "数え直すコマンドを同じ文書に置くか、丸めた表現にすること。\n  "
        + "\n  ".join(offenders)
    )


def test_every_handoff_note_is_linked_from_the_worklog():
    """申し送りが迷子にならないこと。

    `CLAUDE.md` §5 は「未解決が残るなら `details/` に書いて WORKLOG から
    リンクする」としている。**リンクを忘れた申し送りは、置いた本人以外
    誰も見つけられない。**
    """
    if not DETAILS_DIR.is_dir():
        pytest.skip("details/ がまだ無い")
    worklog = WORKLOG.read_text(encoding="utf-8")
    archive = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPO_ROOT / "docs/history/archive").glob("*.md")
    ) if (REPO_ROOT / "docs/history/archive").is_dir() else ""

    orphans = sorted(
        path.name
        for path in DETAILS_DIR.glob("*.md")
        if path.name not in worklog and path.name not in archive
    )

    assert not orphans, (
        "WORKLOG からも archive からもリンクされていない申し送り: " f"{orphans}"
    )


def test_the_checks_would_catch_a_plan_that_drifted(tmp_path, monkeypatch):
    """**番人自身が働くことを確かめる。** 素通りする検査は無いのと同じ。

    検査と同じ判定をここに書き写すと、**検査を壊しても気づけない**
    （実際そうなっていた。PR #50 のレビュー指摘5）。
    本物の検査を、崩した文書に向けて呼ぶ。
    """
    drifted = tmp_path / "WORKLOG.md"
    drifted.write_text(
        "## 2026-09-01 — 古い\n\n## 2026-09-20 — 新しい\n", encoding="utf-8"
    )
    monkeypatch.setattr(sys.modules[__name__], "WORKLOG", drifted)

    with pytest.raises(AssertionError):
        test_the_worklog_entries_are_newest_first()


def test_entries_on_the_same_day_are_not_ordered(tmp_path, monkeypatch):
    """同じ日の中の順序は見張らない。**これは意図した限界。**

    見出しが日付までしか持たないので比べようがない。時刻を足せば見張れるが、
    書式が変わり `archive_worklog.py` にも波及する。
    """
    same_day = tmp_path / "WORKLOG.md"
    same_day.write_text(
        "## 2026-09-20 — 古い作業\n\n## 2026-09-20 — 新しい作業\n", encoding="utf-8"
    )
    monkeypatch.setattr(sys.modules[__name__], "WORKLOG", same_day)

    test_the_worklog_entries_are_newest_first()  # 落ちない


def test_a_dated_count_in_another_section_does_not_excuse_this_one(tmp_path):
    """**節をまたいだ免除をしない。**

    日付や数え直すコマンドが文書のどこかにあれば全体を免除する作りだと、
    あとから足した節の古い数字を見逃す。守りたい文書ほど、先に免除される。
    """
    document = (
        "## 数えた節\n\n2026-09-20 に数えた。Media は 70,297 件。\n\n"
        "## あとから足した節\n\n未スキャンは 69,347 件（日付なし）。\n"
    )

    offenders = _undated_counts(document)

    assert len(offenders) == 1
    assert "69,347" in offenders[0]
    assert "70,297" not in offenders[0]


def test_a_recount_command_excuses_only_its_own_section(tmp_path):
    """数え直すコマンドも、同じ節の中だけを免除する。"""
    document = (
        "## 数え直せる節\n\n```\nsqlite3 data/x.db \"SELECT COUNT(*)\"\n```\n"
        "Media は 70,297 件。\n\n## 別の節\n\n顔は 58,606 件。\n"
    )

    offenders = _undated_counts(document)

    assert len(offenders) == 1
    assert "58,606" in offenders[0]


def test_a_rounded_number_is_not_treated_as_a_count():
    """丸めた表現は引っかからない。**逃げ道を残す**ための確認。

    規模を語りたいだけの数字まで日付を強いると、書き手が検査を嫌う。
    丸めれば古くならないので、そちらへ誘導する。
    """
    assert REAL_DATA_COUNT.search("未スキャン 69,347 件")
    assert REAL_DATA_COUNT.search("顔 58,606件")
    assert not REAL_DATA_COUNT.search("実データは7万件規模")
    assert not REAL_DATA_COUNT.search("200 件ずつ読む")
