"""進捗表示（#78）。バーと最新のメッセージの2行を書き直し、エラーだけを上に残す。

**画面に何が残るか**を、端末の写し（`tests.helpers.render_terminal`）で確かめる。
出力の文字列を見るだけでは、折り返しで古い行が残る不具合（#78）は見えない。
"""

import io

import photoarchive_ai.cli as cli
from photoarchive_ai.progress import ProgressDisplay, display_width, fit
from tests.helpers import render_terminal

LONG_NAME = "/mnt/nfs/photo/2022/20221030穂高ハイキング/とても長いフォルダの名前/IMG_8105.jpg"


def _terminal(width=80):
    stream = io.StringIO()
    return stream, ProgressDisplay(stream=stream, interactive=True, width=width)


def test_long_names_do_not_leave_old_lines_behind():
    """長い日本語のファイル名で何度書き直しても、画面はバーと最新の1行だけ（#78）。

    以前はバーの行が折り返して3行になり、2行しか戻らないので ``Error: none`` が
    書き直すたびに1行ずつ残った。
    """
    stream, display = _terminal(width=80)
    for current, name in enumerate(["a.jpg", LONG_NAME, "c.jpg", LONG_NAME + "x", "e.jpg"], 1):
        display.update(current, 10, name, prefix="Scanning")

    screen = render_terminal(stream.getvalue(), width=80)

    assert screen == ["Scanning: [##########----------]  50% (5/10)", "e.jpg"]


def test_no_error_word_when_nothing_went_wrong():
    """エラーが無いときに ``Error`` の語を出さない（端末が警告の色を付けるため。#78）。"""
    stream, display = _terminal()
    for current in range(1, 4):
        display.update(current, 3, f"IMG_{current}.jpg", prefix="Scanning")

    assert "Error" not in stream.getvalue()


def test_an_error_stays_above_the_bar():
    """エラーはバーの上に1行で残り、そのあとの書き直しで消えない。"""
    stream, display = _terminal()
    display.update(1, 4, "a.jpg", prefix="Scanning")
    display.error("b.jpg: Cannot read image")
    display.update(2, 4, "b.jpg", prefix="Scanning")
    display.update(3, 4, "c.jpg", prefix="Scanning")

    screen = render_terminal(stream.getvalue())

    assert screen == [
        "Error: b.jpg: Cannot read image",
        "Scanning: [###############-----]  75% (3/4)",
        "c.jpg",
    ]


def test_the_same_error_is_kept_once():
    """同じエラーを繰り返し渡されても、残すのは1行だけ（reembed は毎回同じ値を渡す）。"""
    stream, display = _terminal()
    for current in range(1, 4):
        display.error("model missing")
        display.update(current, 4, "x", prefix="Reembedding")

    assert render_terminal(stream.getvalue()).count("Error: model missing") == 1


def test_a_finished_step_leaves_only_its_bar():
    """段が終わったらバーの行を残し、最新のメッセージの行は消す。次の段はその下に出る。"""
    stream, display = _terminal()
    display.update(1, 2, "a.jpg", prefix="Listing")
    display.update(2, 2, "b.jpg", prefix="Listing")
    display.update(1, 3, "c.jpg", prefix="Scanning")

    screen = render_terminal(stream.getvalue())

    assert screen == [
        "Listing: [####################] 100% (2/2)",
        "Scanning: [######--------------]  33% (1/3)",
        "c.jpg",
    ]


def test_counting_without_a_total_shows_how_many_were_found():
    stream, display = _terminal()
    display.update(1200, None, "/mnt/nfs/photo/2022", prefix="Listing")

    assert render_terminal(stream.getvalue()) == ["Listing: 1,200 件", "/mnt/nfs/photo/2022"]


def test_a_step_without_counts_hides_them():
    """始めと終わりしか分からない段（VACUUM など）に ``(0/1)`` を出さない。"""
    stream, display = _terminal()
    display.update(0, 1, prefix="VACUUM を実行しています", show_counts=False)

    assert render_terminal(stream.getvalue())[0] == "VACUUM を実行しています: [--------------------]   0%"


def test_kept_lines_are_not_cut_and_leave_no_old_lines():
    """残す行は切らない。折り返しても、そのあとの書き直しで古い行は残らない。

    控えのパスや「触らなかったファイル」のパスは、ほかに記録が無い（PR #81 のレビュー指摘1）。
    """
    stream, display = _terminal(width=40)
    display.update(1, 4, "a.jpg", prefix="Restoring")
    display.keep(f"  触らない: JPEG がすでに EXIF を持つ: {LONG_NAME}")
    for current in (2, 3):
        display.update(current, 4, LONG_NAME, prefix="Restoring")

    screen = render_terminal(stream.getvalue(), width=40)

    assert LONG_NAME in "".join(screen)
    # 書き直す2行は幅で切られ、末尾に1組だけ残る
    assert screen[-2:] == [
        fit("Restoring: [###############-----]  75% (3/4)", 39),
        fit(LONG_NAME, 39),
    ]
    assert sum(line.startswith("Restoring:") for line in screen) == 1


def test_fit_counts_wide_characters_as_two_columns():
    assert display_width("穂高") == 4
    # 曖昧幅は全角で描く端末があるので2桁（PR #81 のレビュー指摘2）。ASCII は1桁のまま
    assert display_width("①※×…") == 8
    assert display_width("IMG_0001.jpg") == 12
    assert fit("穂高ハイキング", 9) == "穂高ハ..."
    assert display_width(fit("穂高ハイキング", 9)) <= 9
    assert fit("de\ntail", 20) == "de tail"


def test_without_a_terminal_no_escape_codes_are_written():
    """リダイレクト先にはエスケープを書かず、残す行と段の終わりのバーだけを出す。"""
    stream = io.StringIO()
    display = ProgressDisplay(stream=stream, interactive=False)
    display.update(1, 2, "a.jpg", prefix="Scanning")
    display.error("a.jpg: broken")
    display.update(2, 2, "b.jpg", prefix="Scanning")

    out = stream.getvalue()
    assert "\033" not in out
    assert out.splitlines() == ["Error: a.jpg: broken", "Scanning: [####################] 100% (2/2)"]


def test_emit_progress_keeps_the_error_it_is_given(capsys):
    """CLI の呼び出し口は、渡されたエラーを残し、エラーの無い回に何も足さない。"""
    cli._emit_progress(1, 3, "a")
    cli._emit_progress(2, 3, "b", error="cannot read IMG_0002.jpg")
    cli._emit_progress(3, 3, "c")
    out = capsys.readouterr().out

    assert out.count("cannot read IMG_0002.jpg") == 1
    assert "Error: none" not in out
    assert "100% (3/3)" in out


def test_resetting_forgets_the_previous_error(capsys):
    """次のコマンドで同じエラーが起きたら、もう一度残す。"""
    cli._emit_progress(1, 2, "a", error="boom")
    cli._reset_progress_state()
    cli._emit_progress(1, 2, "a", error="boom")

    assert capsys.readouterr().out.count("Error: boom") == 2


def test_scan_reports_listing_progress_and_file_errors(tmp_path, monkeypatch):
    """scan は一覧づくりの進み具合と、ファイルごとのエラーを知らせる（#78）。"""
    from photoarchive_ai import db, scanner
    from photoarchive_ai.scanner import scan_directory
    from tests.helpers import write_image

    source = tmp_path / "media"
    write_image(source / "a.jpg")
    (source / "broken.jpg").write_bytes(b"not an image")
    monkeypatch.setattr(scanner, "LISTING_REPORT_INTERVAL", 1)
    listing, errors, scanned = [], [], []
    connection = db.ensure_database(str(tmp_path / "progress.db"))
    try:
        scan_directory(
            str(source),
            connection,
            progress_callback=lambda *args: scanned.append(args),
            listing_callback=lambda *args: listing.append(args),
            error_callback=errors.append,
            workers=1,
        )
    finally:
        connection.close()

    assert (1, None, str(source)) in listing
    assert listing[-1] == (2, 2, "broken.jpg")
    assert [args[:2] for args in scanned] == [(1, 2), (2, 2)]
    assert len(errors) == 1 and errors[0].startswith("broken.jpg: ")


def test_clearing_before_a_question_leaves_no_old_bar():
    """問いを出す前に2行を消す。消さないと、答えたあとの書き直しで古いバーが残る。"""
    stream, display = _terminal()
    display.update(1, 3, "a.heic", prefix="Converting")
    display.clear()
    stream.write("Cannot read a.heic\nContinue with the next file? [y/N]: y\n")
    display.update(2, 3, "b.heic", prefix="Converting")

    assert render_terminal(stream.getvalue()) == [
        "Cannot read a.heic",
        "Continue with the next file? [y/N]: y",
        "Converting: [#############-------]  66% (2/3)",
        "b.heic",
    ]


def test_convert_heic_says_which_files_could_not_be_read(tmp_path, monkeypatch, capsys):
    """convert-heic は壊れた HEIC を「読めない」と聞き、最後に一覧で知らせる。"""
    import sys

    (tmp_path / "IMG_6463.HEIC").write_bytes(b"\x00\x00\x00\x15infe" + b"\x00" * 16)
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or "y")
    monkeypatch.setattr(sys, "argv", ["photoarchive", "convert-heic", "--source", str(tmp_path)])

    cli.main()

    out = capsys.readouterr().out
    assert len(asked) == 1 and asked[0].startswith(f"Cannot read {tmp_path / 'IMG_6463.HEIC'}")
    assert "Write failed" not in asked[0]
    assert f"Could not read 1 files (left as they are):\n  {tmp_path / 'IMG_6463.HEIC'}" in out
