import json
import os
import sys
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QDialog

# mediapipe と dlib は conftest.py がフェイクに差し替える。

from photoarchive_ai import cli as photoarchive_cli
from photoarchive_ai import db
from photoarchive_ai import gui as photoarchive_gui
from tests.helpers import write_image


def _ensure_qt_app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _write_app_settings(
    config_dir: Path, database_path: str, source_root: str, output_root: str, rule_path: str
) -> None:
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


@pytest.mark.system
def test_end_to_end_flow(tmp_path: Path, monkeypatch):
    """init-db → scan → GUIで顔を割り当て → match → select を一通り流す。"""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    config_dir = tmp_path / "config"
    source_root = tmp_path / "source"
    output_root = tmp_path / "output"
    source_root.mkdir(parents=True)
    output_root.mkdir(parents=True)

    database_path = str(tmp_path / "photoarchive.db")
    rule_path = str(tmp_path / "rule.json")
    _write_app_settings(config_dir, database_path, str(source_root), str(output_root), rule_path)

    # 同じ人物に見える2枚(同じ色)。1枚目を手で割り当て、2枚目を match に任せる。
    first_image = write_image(source_root / "2026" / "person_a.jpg", color=(200, 120, 90))
    write_image(source_root / "2026" / "person_b.jpg", color=(201, 121, 91))

    monkeypatch.chdir(tmp_path)

    # 1) データベース初期化
    _run_cli(["init-db"], tmp_path)
    assert Path(database_path).exists()

    # 2) スキャン(パス登録と顔検出)
    _run_cli(["scan", "--workers", "1"], tmp_path)

    connection = db.ensure_database(database_path)
    media = db.list_media(connection)
    assert len(media) == 2
    assert all(row["face_count"] == 1 for row in media)
    faces = db.list_faces(connection, unassigned=True)
    assert len(faces) == 2
    # scan の時点では人物には一切紐づいていない
    assert all(row["person_id"] is None for row in faces)
    connection.close()

    # 3) GUI で人物登録
    _ensure_qt_app()
    window = photoarchive_gui.MainWindow(database_path)
    monkeypatch.setattr(photoarchive_gui.PersonDialog, "exec", lambda self: QDialog.Accepted)
    monkeypatch.setattr(
        photoarchive_gui.PersonDialog,
        "values",
        lambda self: ("Test Person", "family", "test memo", "2011-05-03"),
    )
    window._add_person()
    persons = db.list_persons(window.connection)
    assert len(persons) == 1
    # 誕生日は年齢の計算にしか使わないが、通しで入ることは確かめておく
    assert persons[0]["birth_date"] == "2011-05-03"
    person_id = persons[0]["id"]

    # 4) GUI で検出済みの顔を人物に割り当て
    unassigned = db.list_faces(window.connection, unassigned=True)
    assert len(unassigned) == 2
    first_face = next(
        row
        for row in unassigned
        if db.get_media_by_id(window.connection, row["media_id"])["path"] == str(first_image)
    )
    window.assign_faces([first_face["id"]], person_id, age=5)
    assigned = db.list_faces(window.connection, assign_source=db.ASSIGN_MANUAL)
    assert len(assigned) == 1
    assert assigned[0]["age"] == 5

    # 5) 残りを自動で紐づけ
    _run_cli(["match"], tmp_path)
    window.reload_faces()
    auto = db.list_faces(window.connection, assign_source=db.ASSIGN_AUTO)
    assert len(auto) == 1
    assert auto[0]["person_id"] == person_id
    assert db.count_faces(window.connection, unassigned=True) == 0

    results = window.connection.execute(
        "SELECT M.path, AR.family_score FROM Media M JOIN AnalysisResult AR ON M.id = AR.media_id"
    ).fetchall()
    assert len(results) == 2
    assert all(row["family_score"] > 0.0 for row in results)

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

    copied_files = [path for path in output_root.rglob("*") if path.is_file()]
    assert len(copied_files) == 1

    window.connection.close()


@pytest.mark.system
def test_scan_is_incremental_on_second_run(tmp_path: Path, monkeypatch, capsys):
    """2回目の scan がスキップだけで終わること (#9)。"""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    config_dir = tmp_path / "config"
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True)
    database_path = str(tmp_path / "photoarchive.db")
    _write_app_settings(
        config_dir, database_path, str(source_root), str(tmp_path / "out"), str(tmp_path / "r.json")
    )
    write_image(source_root / "a.jpg")
    monkeypatch.chdir(tmp_path)

    _run_cli(["init-db"], tmp_path)
    _run_cli(["scan", "--workers", "1"], tmp_path)
    capsys.readouterr()

    _run_cli(["scan", "--workers", "1"], tmp_path)
    output = capsys.readouterr().out
    assert "Scanned 0 media entries" in output
    assert "skipped 1" in output
