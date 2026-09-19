"""サブコマンドの配線。

DB を使うコマンドと使わないコマンドの区別、引数がそのまま下へ渡ること、
移行コマンドが動くことを見る。
"""

import os
import sys
from pathlib import Path

import pytest

from photoarchive_ai import cli, db
from tests.helpers import write_heic, write_image


def run_cli(args, cwd: Path):
    original_argv, original_cwd = sys.argv, Path.cwd()
    try:
        sys.argv = ["photoarchive"] + args
        os.chdir(cwd)
        cli.main()
    finally:
        sys.argv = original_argv
        os.chdir(original_cwd)


def test_convert_heic_does_not_need_a_database(tmp_path, capsys):
    """DB を触らないコマンドが DB パスを要求しないこと。

    README は「scan の前に convert-heic」と案内しているので、
    init-db より先に実行されうる。
    """
    source = tmp_path / "photos"
    write_heic(source / "a.heic")
    workdir = tmp_path / "work"
    workdir.mkdir()

    run_cli(["convert-heic", "--source", str(source)], workdir)

    assert (source / "a.jpg").exists()
    assert "Converted 1 files" in capsys.readouterr().out


def test_commands_that_need_a_database_still_say_so(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()

    with pytest.raises(SystemExit) as raised:
        run_cli(["init-db"], workdir)

    assert "Database path is required" in str(raised.value)


def test_init_db_creates_a_usable_database(tmp_path, capsys):
    database = tmp_path / "photoarchive.db"

    run_cli(["init-db", "--db", str(database)], tmp_path)

    assert database.exists()
    assert "Database initialized" in capsys.readouterr().out
    connection = db.ensure_database(str(database))
    connection.close()


def test_migrate_reports_that_a_fresh_database_is_current(tmp_path, capsys):
    database = tmp_path / "photoarchive.db"
    run_cli(["init-db", "--db", str(database)], tmp_path)
    capsys.readouterr()

    run_cli(["migrate", "--db", str(database), "--yes"], tmp_path)

    assert "photoarchive.db.bak" not in capsys.readouterr().err


def test_scan_arguments_reach_the_scanner(tmp_path, monkeypatch):
    source = tmp_path / "media"
    write_image(source / "a.jpg")
    database = tmp_path / "photoarchive.db"
    run_cli(["init-db", "--db", str(database)], tmp_path)

    captured = {}

    def fake_scan(source_dir, connection, **kwargs):
        captured["source"] = source_dir
        captured.update(kwargs)
        return {"processed": 0, "skipped": 0, "faces": 0, "pruned": 0, "errors": 0}

    monkeypatch.setattr(cli, "scan_directory", fake_scan)
    run_cli(
        [
            "scan",
            "--db", str(database),
            "--source", str(source),
            "--workers", "3",
            "--no-prune",
            "--force-prune",
            "--force-rescan",
            "--allow-missing-embeddings",
        ],
        tmp_path,
    )

    assert captured["source"] == str(source)
    assert captured["workers"] == 3
    assert captured["prune"] is False
    assert captured["force_prune"] is True
    assert captured["force_rescan"] is True
    assert captured["allow_missing_embeddings"] is True


def test_match_arguments_reach_the_matcher(tmp_path, monkeypatch):
    database = tmp_path / "photoarchive.db"
    run_cli(["init-db", "--db", str(database)], tmp_path)
    captured = {}

    def fake_match(connection, **kwargs):
        captured.update(kwargs)
        return {
            "teachers": 0, "candidates": 0, "assigned": 0, "unassigned": 0,
            "reset": 0, "per_person": {}, "histogram": {}, "dry_run": True,
        }

    monkeypatch.setattr(cli, "match_faces", fake_match)
    run_cli(
        ["match", "--db", str(database), "--threshold", "0.45",
         "--margin", "0.1", "--no-reset", "--dry-run"],
        tmp_path,
    )

    assert captured["threshold"] == pytest.approx(0.45)
    assert captured["margin"] == pytest.approx(0.1)
    assert captured["reset"] is False
    assert captured["dry_run"] is True


def test_the_database_path_falls_back_to_the_settings_file(tmp_path, monkeypatch):
    import json

    database = tmp_path / "from-settings.db"
    (tmp_path / "config").mkdir()
    (tmp_path / "config/app_settings.json").write_text(
        json.dumps({"database_path": str(database)}), encoding="utf-8"
    )

    run_cli(["init-db"], tmp_path)

    assert database.exists()
