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
