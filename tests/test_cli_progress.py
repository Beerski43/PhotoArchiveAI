"""進捗表示。2行を ANSI で書き換える、CLI 共通の表示。"""

import photoarchive_ai.cli as cli


def test_emit_progress_writes_percentage_to_stdout(capsys):
    cli._emit_progress(1, 4, "demo")
    out = capsys.readouterr().out

    assert "25%" in out
    assert "(1/4)" in out


def test_progress_keeps_the_last_error_instead_of_overwriting_it(capsys):
    """エラーの出なかった回で Error: none に戻さないこと(Issue #25)。

    戻してしまうと、流れていく表示の中でエラーが一瞬しか見えない。
    """
    cli._emit_progress(1, 3, "a")
    assert "Error: none" in capsys.readouterr().out

    cli._emit_progress(2, 3, "b", error="cannot read IMG_0002.HEIC")
    assert "cannot read IMG_0002.HEIC" in capsys.readouterr().out

    cli._emit_progress(3, 3, "c")
    out = capsys.readouterr().out
    assert "cannot read IMG_0002.HEIC" in out
    assert "Error: none" not in out


def test_a_newer_error_replaces_the_previous_one(capsys):
    cli._emit_progress(1, 3, "a", error="first")
    capsys.readouterr()
    cli._emit_progress(2, 3, "b", error="second")
    out = capsys.readouterr().out

    assert "second" in out
    assert "first" not in out


def test_resetting_clears_the_error_for_the_next_command(capsys):
    cli._emit_progress(1, 2, "a", error="boom")
    capsys.readouterr()
    cli._reset_progress_state()

    cli._emit_progress(1, 2, "a")
    out = capsys.readouterr().out

    assert "Error: none" in out
    assert "boom" not in out


def test_long_errors_are_trimmed_to_one_line(capsys):
    cli._emit_progress(1, 2, "a", error="x" * 400)
    out = capsys.readouterr().out

    assert "x" * cli.ERROR_DISPLAY_LIMIT in out
    assert "x" * (cli.ERROR_DISPLAY_LIMIT + 1) not in out


def test_newlines_in_the_error_do_not_break_the_two_line_layout(capsys):
    cli._emit_progress(1, 2, "de\ntail", error="line one\nline two")
    out = capsys.readouterr().out

    # 進捗とエラーで2行。余分な改行が入らないこと。
    assert out.count("\n") == 1
    assert "line one line two" in out


def test_scan_directory_reports_progress(tmp_path, capsys):
    from photoarchive_ai import db
    from photoarchive_ai.scanner import scan_directory
    from tests.helpers import write_image

    source = tmp_path / "media"
    write_image(source / "a.jpg")
    connection = db.ensure_database(str(tmp_path / "progress.db"))
    try:
        scan_directory(
            str(source),
            connection,
            progress_callback=lambda current, total, detail: cli._emit_progress(
                current, total, detail, prefix="Scanning"
            ),
            workers=1,
        )
    finally:
        connection.close()

    assert "Scanning:" in capsys.readouterr().out
