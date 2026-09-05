from photoarchive_ai.cli import _emit_progress
from photoarchive_ai.db import ensure_database
from photoarchive_ai.scanner import scan_directory


def test_emit_progress_writes_percentage_to_stdout(capsys):
    _emit_progress(1, 4, "Scanning")
    captured = capsys.readouterr()
    assert "Scanning" in captured.out
    assert "25%" in captured.out


def test_scan_directory_reports_progress(tmp_path):
    sample_path = tmp_path / "sample.jpg"
    sample_path.write_bytes(b"fake-image-data")

    connection = ensure_database(str(tmp_path / "test.db"))
    calls = []

    scan_directory(
        str(tmp_path),
        connection,
        progress_callback=lambda current, total, detail: calls.append((current, total, detail)),
    )

    assert calls == [(1, 1, sample_path.name)]
