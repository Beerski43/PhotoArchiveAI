"""作業履歴の切り出し(scripts/archive_worklog.py)。

WORKLOG.md は新しいセッションが毎回先頭から読む文書なので、
伸び続けると読ませる文書として成り立たなくなる。その安全弁のテスト。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "archive_worklog.py"


@pytest.fixture(scope="module")
def archive_worklog():
    spec = importlib.util.spec_from_file_location("archive_worklog", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    # dataclass が型注釈を解決するときに sys.modules を引くので、
    # exec_module の前に登録しておく。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


PREAMBLE = "# 作業履歴\n\n新しいものが上。\n"


def write_worklog(path: Path, entries) -> Path:
    """entries は (日付, 題名) の並び。先頭が最新。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = [PREAMBLE]
    for date, title in entries:
        body.append(f"\n## {date} — {title}\n{title} の本文。\n")
    path.write_text("".join(body), encoding="utf-8")
    return path


def headings(text: str):
    """エントリの見出しだけを拾う(索引の見出しは含めない)。"""
    import re

    return [line for line in text.splitlines() if re.match(r"^## \d{4}-\d{2}-\d{2}\b", line)]


def test_nothing_moves_while_the_log_is_short(tmp_path: Path, archive_worklog):
    worklog = write_worklog(
        tmp_path / "WORKLOG.md", [("2026-09-19", "b"), ("2026-09-06", "a")]
    )
    before = worklog.read_text(encoding="utf-8")

    assert archive_worklog.main(["--worklog", str(worklog), "--keep", "20"]) == 0
    assert not (tmp_path / "archive").exists()
    assert headings(worklog.read_text(encoding="utf-8")) == headings(before)


def test_the_oldest_entries_move_into_a_file_named_after_their_year(
    tmp_path: Path, archive_worklog
):
    worklog = write_worklog(
        tmp_path / "WORKLOG.md",
        [(f"2026-09-{day:02d}", f"work {day}") for day in range(5, 0, -1)],
    )

    archive_worklog.main(["--worklog", str(worklog), "--keep", "2"])

    remaining = worklog.read_text(encoding="utf-8")
    archived = (tmp_path / "archive" / "WORKLOG-2026.md").read_text(encoding="utf-8")

    assert headings(remaining) == ["## 2026-09-05 — work 5", "## 2026-09-04 — work 4"]
    assert headings(archived) == [
        "## 2026-09-03 — work 3",
        "## 2026-09-02 — work 2",
        "## 2026-09-01 — work 1",
    ]
    assert "work 3 の本文。" in archived
    assert remaining.startswith(PREAMBLE)


def test_entries_are_split_per_year(tmp_path: Path, archive_worklog):
    worklog = write_worklog(
        tmp_path / "WORKLOG.md",
        [
            ("2027-02-01", "new"),
            ("2026-12-01", "older"),
            ("2025-05-01", "oldest"),
        ],
    )

    archive_worklog.main(["--worklog", str(worklog), "--keep", "1"])

    assert headings((tmp_path / "archive" / "WORKLOG-2026.md").read_text(encoding="utf-8")) == [
        "## 2026-12-01 — older"
    ]
    assert headings((tmp_path / "archive" / "WORKLOG-2025.md").read_text(encoding="utf-8")) == [
        "## 2025-05-01 — oldest"
    ]


def test_the_index_of_archives_is_rebuilt_every_time(tmp_path: Path, archive_worklog):
    worklog = write_worklog(
        tmp_path / "WORKLOG.md",
        [("2027-02-01", "new"), ("2026-12-01", "older"), ("2025-05-01", "oldest")],
    )

    archive_worklog.main(["--worklog", str(worklog), "--keep", "1"])
    text = worklog.read_text(encoding="utf-8")

    assert "## 過去の履歴" in text
    assert "- [2026](archive/WORKLOG-2026.md)" in text
    assert "- [2025](archive/WORKLOG-2025.md)" in text
    assert text.index("- [2026]") < text.index("- [2025]")
    assert text.count("## 過去の履歴") == 1


def test_running_twice_changes_nothing(tmp_path: Path, archive_worklog):
    worklog = write_worklog(
        tmp_path / "WORKLOG.md",
        [(f"2026-09-{day:02d}", f"work {day}") for day in range(6, 0, -1)],
    )

    archive_worklog.main(["--worklog", str(worklog), "--keep", "2"])
    first_log = worklog.read_text(encoding="utf-8")
    first_archive = (tmp_path / "archive" / "WORKLOG-2026.md").read_text(encoding="utf-8")

    archive_worklog.main(["--worklog", str(worklog), "--keep", "2"])

    assert worklog.read_text(encoding="utf-8") == first_log
    assert (tmp_path / "archive" / "WORKLOG-2026.md").read_text(encoding="utf-8") == first_archive


def test_a_later_run_appends_to_the_existing_archive(tmp_path: Path, archive_worklog):
    worklog = write_worklog(
        tmp_path / "WORKLOG.md", [("2026-09-03", "c"), ("2026-09-02", "b"), ("2026-09-01", "a")]
    )
    archive_worklog.main(["--worklog", str(worklog), "--keep", "1"])

    text = worklog.read_text(encoding="utf-8")
    worklog.write_text(
        text.replace(PREAMBLE, PREAMBLE + "\n## 2026-09-10 — d\nd の本文。\n"), encoding="utf-8"
    )
    archive_worklog.main(["--worklog", str(worklog), "--keep", "1"])

    assert headings(worklog.read_text(encoding="utf-8")) == ["## 2026-09-10 — d"]
    assert headings((tmp_path / "archive" / "WORKLOG-2026.md").read_text(encoding="utf-8")) == [
        "## 2026-09-03 — c",
        "## 2026-09-02 — b",
        "## 2026-09-01 — a",
    ]


def test_entries_written_out_of_order_are_sorted_newest_first(tmp_path: Path, archive_worklog):
    worklog = write_worklog(
        tmp_path / "WORKLOG.md",
        [("2026-09-01", "old"), ("2026-09-20", "new"), ("2026-09-10", "middle")],
    )

    archive_worklog.main(["--worklog", str(worklog), "--keep", "20"])

    assert headings(worklog.read_text(encoding="utf-8")) == [
        "## 2026-09-20 — new",
        "## 2026-09-10 — middle",
        "## 2026-09-01 — old",
    ]


def test_check_reports_without_writing(tmp_path: Path, archive_worklog, capsys):
    worklog = write_worklog(
        tmp_path / "WORKLOG.md",
        [(f"2026-09-{day:02d}", f"work {day}") for day in range(3, 0, -1)],
    )
    before = worklog.read_text(encoding="utf-8")

    status = archive_worklog.main(["--worklog", str(worklog), "--keep", "1", "--check"])

    assert status == 1
    assert "切り出しが必要" in capsys.readouterr().out
    assert worklog.read_text(encoding="utf-8") == before
    assert not (tmp_path / "archive").exists()


def test_check_is_quiet_when_nothing_needs_moving(tmp_path: Path, archive_worklog, capsys):
    worklog = write_worklog(tmp_path / "WORKLOG.md", [("2026-09-01", "only")])

    status = archive_worklog.main(["--worklog", str(worklog), "--keep", "20", "--check"])

    assert status == 0
    assert "不要" in capsys.readouterr().out


def test_a_missing_worklog_is_not_an_error(tmp_path: Path, archive_worklog):
    assert archive_worklog.main(["--worklog", str(tmp_path / "absent.md")]) == 0


def test_a_section_that_is_not_a_dated_entry_survives(tmp_path: Path, archive_worklog):
    """日付エントリでない節を消さないこと。

    このスクリプトは毎コミット実行する決まりなので、消えると git 管理下の
    文書が静かに欠落する。警告は流れて気づけない。
    """
    worklog = tmp_path / "WORKLOG.md"
    worklog.write_text(
        "# 作業履歴\n\n新しいものが上。\n\n"
        "## 運用メモ\n\nこの節は日付エントリではない。\n\n"
        "## 2026-09-19 — なにか\n\n本文。\n",
        encoding="utf-8",
    )

    archive_worklog.main(["--worklog", str(worklog), "--keep", "20"])
    text = worklog.read_text(encoding="utf-8")

    assert "## 運用メモ" in text
    assert "この節は日付エントリではない。" in text
    # 前書きの一部なので、エントリより前のまま動かない。
    assert text.index("## 運用メモ") < text.index("## 2026-09-19")


def test_a_section_after_the_entries_is_moved_to_the_end_not_dropped(
    tmp_path: Path, archive_worklog, capsys
):
    worklog = tmp_path / "WORKLOG.md"
    worklog.write_text(
        "# 作業履歴\n\n"
        "## 2026-09-19 — 新しい\n\n本文A。\n\n"
        "## 参考リンク\n\nhttps://example.com\n\n"
        "## 2026-09-01 — 古い\n\n本文B。\n",
        encoding="utf-8",
    )

    archive_worklog.main(["--worklog", str(worklog), "--keep", "20"])
    text = worklog.read_text(encoding="utf-8")

    assert "## 参考リンク" in text
    assert "https://example.com" in text
    assert "本文A。" in text and "本文B。" in text
    # 日付エントリはすべて参考リンクより前に来る。
    assert text.index("## 2026-09-01") < text.index("## 参考リンク")
    assert "末尾へ移した" in capsys.readouterr().out


def test_sections_that_are_not_entries_stay_put_on_a_second_run(
    tmp_path: Path, archive_worklog
):
    worklog = tmp_path / "WORKLOG.md"
    worklog.write_text(
        "# 作業履歴\n\n## 運用メモ\n\nメモ。\n\n"
        "## 2026-09-19 — 新しい\n\n本文A。\n\n"
        "## 参考リンク\n\nリンク。\n",
        encoding="utf-8",
    )

    archive_worklog.main(["--worklog", str(worklog), "--keep", "20"])
    first = worklog.read_text(encoding="utf-8")
    archive_worklog.main(["--worklog", str(worklog), "--keep", "20"])

    assert worklog.read_text(encoding="utf-8") == first


def test_sections_survive_an_actual_archiving_run(tmp_path: Path, archive_worklog):
    worklog = tmp_path / "WORKLOG.md"
    worklog.write_text(
        "# 作業履歴\n\n## 運用メモ\n\nメモ。\n\n"
        "## 2026-09-03 — c\n\n本文C。\n\n"
        "## 2026-09-02 — b\n\n本文B。\n\n"
        "## 2026-09-01 — a\n\n本文A。\n\n"
        "## 参考リンク\n\nリンク。\n",
        encoding="utf-8",
    )

    archive_worklog.main(["--worklog", str(worklog), "--keep", "1"])
    text = worklog.read_text(encoding="utf-8")

    assert "## 運用メモ" in text and "メモ。" in text
    assert "## 参考リンク" in text and "リンク。" in text
    assert headings(text) == ["## 2026-09-03 — c"]
    assert headings((tmp_path / "archive" / "WORKLOG-2026.md").read_text(encoding="utf-8")) == [
        "## 2026-09-02 — b",
        "## 2026-09-01 — a",
    ]


def test_a_log_without_any_dated_entry_is_left_alone(tmp_path: Path, archive_worklog):
    worklog = tmp_path / "WORKLOG.md"
    original = "# 作業履歴\n\nまだ何も書いていない。\n\n## 運用メモ\n\nメモだけある。\n"
    worklog.write_text(original, encoding="utf-8")

    assert archive_worklog.main(["--worklog", str(worklog), "--keep", "20"]) == 0

    text = worklog.read_text(encoding="utf-8")
    assert "## 運用メモ" in text and "メモだけある。" in text


def test_the_archive_keeps_its_own_text_when_the_heading_is_repeated(
    tmp_path: Path, archive_worklog
):
    """切り出し済みの記録は、あとから書き換えない(意図した優先順位)。"""
    worklog = write_worklog(
        tmp_path / "WORKLOG.md", [("2026-09-02", "b"), ("2026-09-01", "a")]
    )
    archive_worklog.main(["--worklog", str(worklog), "--keep", "1"])

    # 切り出し済みと同じ見出しを、本文を変えて書き戻す。
    worklog.write_text(
        worklog.read_text(encoding="utf-8").replace(
            "## 2026-09-02 — b", "## 2026-09-01 — a\nあとから書いた本文。\n\n## 2026-09-02 — b"
        ),
        encoding="utf-8",
    )
    archive_worklog.main(["--worklog", str(worklog), "--keep", "1"])

    archived = (tmp_path / "archive" / "WORKLOG-2026.md").read_text(encoding="utf-8")
    assert "a の本文。" in archived
    assert "あとから書いた本文。" not in archived
