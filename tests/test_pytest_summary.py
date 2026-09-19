"""回帰テストの集計行（scripts/summarize_pytest.py）。

CLAUDE.md §5 で「最終行を PR 本文に貼る」ことを必須にした行そのものなので、
壊れても気づけないと、誤った数字が記録として残る。

以前はシェルの grep で数えていたため、**端末で実行すると pytest の着色
エスケープに阻まれて常に `0 passed / 0 failed` になっていた。**
しかもそれが成功のように見えていた。
"""

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "summarize_pytest.py"

# 端末で実行したときに pytest が実際に吐く形（着色あり）。
COLOURED = (
    "\x1b[32m.\x1b[0m" * 10 + "\x1b[32m                     [100%]\x1b[0m\n"
    "\x1b[32m\x1b[32m\x1b[1m107 passed\x1b[0m, \x1b[33m3 deselected\x1b[0m\x1b[32m in 2.18s\x1b[0m\x1b[0m\n"
)


@pytest.fixture(scope="module")
def summarize():
    spec = importlib.util.spec_from_file_location("summarize_pytest", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def test_the_counts_survive_the_colours_pytest_adds_on_a_terminal(summarize):
    parsed = summarize.parse_summary(COLOURED)

    assert parsed["passed"] == 107
    assert parsed["failed"] == 0
    assert parsed["elapsed"] == pytest.approx(2.18)


def test_the_line_is_the_same_with_and_without_colours(summarize):
    plain = "107 passed, 3 deselected in 2.18s\n"

    on = date(2026, 9, 19)
    assert summarize.format_summary(
        summarize.parse_summary(COLOURED), on
    ) == summarize.format_summary(summarize.parse_summary(plain), on)


def test_the_formatted_line_reads_as_documented(summarize):
    line = summarize.format_summary(summarize.parse_summary(COLOURED), date(2026, 9, 19))

    assert line == "回帰テスト: 107 passed / 0 failed (2.18s) 実行日: 2026-09-19"


@pytest.mark.parametrize(
    "text, passed, failed",
    [
        ("107 passed, 3 deselected in 2.18s", 107, 0),
        ("1 failed, 36 passed, 3 deselected in 1.20s", 36, 1),
        ("2 failed, 1 error, 34 passed in 2.00s", 34, 3),
        ("40 passed in 1.41s", 40, 0),
        ("107 passed, 1 warning in 2.20s", 107, 0),
        ("3 passed, 37 deselected in 0.47s", 3, 0),
        ("5 passed, 2 skipped, 1 xfailed in 0.90s", 5, 0),
    ],
)
def test_counts_are_read_from_the_shapes_pytest_prints(summarize, text, passed, failed):
    parsed = summarize.parse_summary(text)

    assert (parsed["passed"], parsed["failed"]) == (passed, failed)


def test_a_collection_error_counts_as_a_failure(summarize):
    """テストが1件も走っていない状態を「失敗0件」に見せない。"""
    parsed = summarize.parse_summary(
        "ERROR tests/test_system.py\n!!! Interrupted: 2 errors during collection !!!\n"
        "2 errors in 0.28s\n"
    )

    assert parsed["passed"] == 0
    assert parsed["failed"] == 2


def test_the_last_summary_wins_when_the_log_holds_several(summarize):
    parsed = summarize.parse_summary("1 failed, 2 passed in 0.10s\n40 passed in 1.41s\n")

    assert (parsed["passed"], parsed["failed"]) == (40, 0)


def test_output_that_cannot_be_read_is_not_reported_as_zero(summarize, tmp_path, capsys):
    """集計できないときに 0 passed / 0 failed を出さないこと。

    これが以前の壊れ方そのもの。数字が出てしまうと成功に見える。
    """
    log = tmp_path / "pytest.log"
    log.write_text("何かがおかしくて件数が出ていない\n", encoding="utf-8")

    assert summarize.parse_summary(log.read_text(encoding="utf-8")) is None

    status = summarize.main([str(log)])
    captured = capsys.readouterr()

    assert status == 1
    assert captured.out == ""
    assert "集計できませんでした" in captured.err


def test_main_prints_the_line_for_a_readable_log(summarize, tmp_path, capsys):
    log = tmp_path / "pytest.log"
    log.write_text(COLOURED, encoding="utf-8")

    status = summarize.main([str(log)])

    assert status == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("回帰テスト: 107 passed / 0 failed (2.18s) 実行日: ")


def test_strip_ansi_leaves_plain_text_alone(summarize):
    assert summarize.strip_ansi("107 passed in 1.0s") == "107 passed in 1.0s"
    assert summarize.strip_ansi("\x1b[1m107 passed\x1b[0m") == "107 passed"
