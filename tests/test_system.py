import importlib
import importlib.util
import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication, QDialog

# mediapipe may not import cleanly in the test environment.
# conftest.py provides a fake module when import would fail.

from photoarchive_ai import cli as photoarchive_cli
from photoarchive_ai import gui as photoarchive_gui


def _ensure_qt_app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _write_app_settings(config_dir: Path, database_path: str, source_root: str, output_root: str, rule_path: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    settings = {
        "database_path": database_path,
        "source_root": source_root,
        "output_root": output_root,
        "rule_path": rule_path,
    }
    (config_dir / "app_settings.json").write_text(json.dumps(settings), encoding="utf-8")


def _run_cli(args, cwd: Path):
    original_argv = sys.argv
    original_cwd = Path.cwd()
    try:
        sys.argv = ["photoarchive"] + args
        os.chdir(cwd)
        photoarchive_cli.main()
    finally:
        sys.argv = original_argv
        os.chdir(original_cwd)


def _create_sample_image(path: Path) -> None:
    image = Image.new("RGB", (200, 200), color=(200, 200, 200))
    image.save(path, format="JPEG")


@pytest.mark.system
def test_end_to_end_flow(tmp_path: Path, monkeypatch):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    config_dir = tmp_path / "config"
    source_root = tmp_path / "source"
    output_root = tmp_path / "output"
    source_root.mkdir(parents=True)
    output_root.mkdir(parents=True)

    database_path = str(tmp_path / "photoarchive.db")
    rule_path = str(tmp_path / "rule.json")
    _write_app_settings(config_dir, database_path, str(source_root), str(output_root), rule_path)

    sample_image_path = source_root / "person.jpg"
    _create_sample_image(sample_image_path)

    monkeypatch.chdir(tmp_path)

    # 1) データベース初期化
    _run_cli(["init-db"], tmp_path)
    assert Path(database_path).exists()

    # 2) メディアスキャン
    _run_cli(["scan"], tmp_path)

    # 3) GUI 起動と人物登録
    _ensure_qt_app()
    window = photoarchive_gui.MainWindow(database_path)
    monkeypatch.setattr(photoarchive_gui.PersonDialog, "exec", lambda self: QDialog.Accepted)
    monkeypatch.setattr(photoarchive_gui.PersonDialog, "values", lambda self: ("Test Person", "family", "test memo"))
    window._add_person()
    persons = window.connection.execute("SELECT * FROM Person").fetchall()
    assert len(persons) == 1
    person_id = persons[0]["id"]

    # 4) 顔画像登録
    window._register_face(str(sample_image_path), person_id, (10, 110, 110, 10))
    embeddings = window.connection.execute("SELECT * FROM FaceEmbedding WHERE person_id = ?", (person_id,)).fetchall()
    assert len(embeddings) == 1

    # 5) AI 解析実行
    _run_cli(["analyze"], tmp_path)
    analysis = window.connection.execute("SELECT * FROM AnalysisResult").fetchone()
    assert analysis is not None
    assert analysis["face_count"] == 1
    assert analysis["family_score"] > 0.0

    # 6) ルール作成と抽出実行
    rule = {
        "date": {"start": "2000-01-01", "end": "2100-01-01"},
        "family_only": True,
        "count_per_year": 1,
        "include_video": False,
        "remove_duplicate": True,
    }
    Path(rule_path).write_text(json.dumps(rule), encoding="utf-8")
    _run_cli(["select"], tmp_path)

    output_files = list((output_root).rglob("*") )
    assert any(path.is_file() for path in output_files)
    copied_files = [path for path in output_files if path.is_file()]
    assert len(copied_files) == 1
    assert copied_files[0].name == sample_image_path.name
