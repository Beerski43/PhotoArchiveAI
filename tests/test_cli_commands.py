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


def test_evaluate_arguments_reach_the_evaluation(tmp_path, monkeypatch):
    """閾値の並びとマージンがそのまま下へ渡ること。

    ここがずれると、表に出る閾値と実際に試した閾値が食い違う。
    """
    database = tmp_path / "photoarchive.db"
    run_cli(["init-db", "--db", str(database)], tmp_path)
    captured = {}

    def fake_evaluate(connection, thresholds, margin, keep_same_media, progress_callback):
        captured.update(
            thresholds=thresholds, margin=margin, keep_same_media=keep_same_media
        )
        return {"teachers": 0, "person_names": {}}

    monkeypatch.setattr(cli, "evaluate_match", fake_evaluate)
    monkeypatch.setattr(cli, "format_report", lambda summary: "報告")
    run_cli(
        ["evaluate", "--db", str(database), "--thresholds", "0.4, 0.45",
         "--margin", "0.1", "--keep-same-media"],
        tmp_path,
    )

    assert captured["thresholds"] == [0.4, 0.45]
    assert captured["margin"] == pytest.approx(0.1)
    assert captured["keep_same_media"] is True


def test_a_threshold_that_cannot_be_read_stops_instead_of_being_dropped(tmp_path):
    """読めない閾値を黙って捨てない。捨てると、頼んだ閾値が表から消える。"""
    database = tmp_path / "photoarchive.db"
    run_cli(["init-db", "--db", str(database)], tmp_path)

    with pytest.raises(SystemExit) as raised:
        run_cli(["evaluate", "--db", str(database), "--thresholds", "0.4,およそ0.5"], tmp_path)

    assert "閾値として読めない値です" in str(raised.value)


def test_evaluate_runs_end_to_end_on_a_database_with_assigned_faces(tmp_path, capsys):
    """CLI から実際に数字が出るところまで通す。"""
    import numpy as np

    database = tmp_path / "photoarchive.db"
    run_cli(["init-db", "--db", str(database)], tmp_path)
    connection = db.ensure_database(str(database))
    person = db.add_person(connection, "Alice")
    for index, offset in enumerate((0.0, 0.01), start=1):
        media = db.save_media(
            connection,
            {
                "path": f"/photos/{index}.jpg",
                "filename": f"{index}.jpg",
                "type": "image",
                "file_hash": f"hash{index}",
                "file_size": 100,
                "created_time": "2026-01-01T00:00:00",
            },
        )
        vector = np.zeros(db.EMBEDDING_DIM, dtype=np.float32)
        vector[0] = offset
        db.add_face(
            connection,
            media_id=media,
            bbox=(0, 10, 10, 0),
            embedding=vector,
            embed_version=db.embedding_model.ACTIVE.version,
            person_id=person,
            assign_source=db.ASSIGN_MANUAL,
        )
    connection.commit()
    connection.close()
    capsys.readouterr()

    run_cli(["evaluate", "--db", str(database), "--thresholds", "0.4"], tmp_path)

    output = capsys.readouterr().out
    assert "Alice" in output
    assert "取りこぼし" in output


def test_the_same_threshold_given_twice_is_counted_once(tmp_path, capsys):
    """同じ閾値を2度渡しても、行が2つに割れて率が壊れないこと。

    集計先は閾値の値で引くので、重複すると片方が0件、もう片方が2倍になり
    **正解率が 200% になる。** 表に「0.40 で正解 0.0%」という行が並ぶと、
    読み手は「0.4 では全部取りこぼす」と受け取り、閾値を緩める方向へ倒れる。
    """
    import numpy as np

    database = tmp_path / "photoarchive.db"
    run_cli(["init-db", "--db", str(database)], tmp_path)
    connection = db.ensure_database(str(database))
    person = db.add_person(connection, "Alice")
    for index, offset in enumerate((0.0, 0.01), start=1):
        media = db.save_media(
            connection,
            {
                "path": f"/photos/{index}.jpg",
                "filename": f"{index}.jpg",
                "type": "image",
                "file_hash": f"hash{index}",
                "file_size": 100,
                "created_time": "2026-01-01T00:00:00",
            },
        )
        vector = np.zeros(db.EMBEDDING_DIM, dtype=np.float32)
        vector[0] = offset
        db.add_face(
            connection,
            media_id=media,
            bbox=(0, 10, 10, 0),
            embedding=vector,
            embed_version=db.embedding_model.ACTIVE.version,
            person_id=person,
            assign_source=db.ASSIGN_MANUAL,
        )
    connection.commit()
    connection.close()
    capsys.readouterr()

    run_cli(["evaluate", "--db", str(database), "--thresholds", "0.4,0.40"], tmp_path)

    output = capsys.readouterr().out
    assert output.count("  0.40") == 1
    assert "200.0%" not in output
    # 人物ごとの表が見出しだけにならないこと
    assert "Alice" in output


# "-inf" は argparse がオプション名とみなすのでここでは渡せない。
# 負の無限大は `math.isfinite` で同じ枝に落ちる。
@pytest.mark.parametrize("value", ["nan", "inf", "0", "-1"])
def test_a_threshold_that_is_not_a_positive_finite_number_stops(tmp_path, value):
    """`nan` は float として読めてしまうが、閾値としては通してはいけない。

    `best_distance > nan` は常に False なので、**閾値を掛けていないのと
    同じ判定**になる。表には `nan 100.0%` と出て、まるで取りこぼしが
    無いように見える。
    """
    database = tmp_path / "photoarchive.db"
    run_cli(["init-db", "--db", str(database)], tmp_path)

    with pytest.raises(SystemExit) as raised:
        run_cli(["evaluate", "--db", str(database), "--thresholds", value], tmp_path)

    assert "正の有限の数" in str(raised.value)


def _database_with_mixed_assignments(tmp_path):
    """手本・自動・除外・未割当を1件ずつ持つDB。``family_score`` も入れる。"""
    import numpy as np

    database = tmp_path / "photoarchive.db"
    run_cli(["init-db", "--db", str(database)], tmp_path)
    connection = db.ensure_database(str(database))
    person = db.add_person(connection, "Alice")
    face_ids = {}
    for index, source in enumerate(
        (db.ASSIGN_MANUAL, db.ASSIGN_AUTO, db.ASSIGN_REJECTED, None), start=1
    ):
        media = db.save_media(
            connection,
            {
                "path": f"/photos/{index}.jpg",
                "filename": f"{index}.jpg",
                "type": "image",
                "file_hash": f"hash{index}",
                "file_size": 100,
                "created_time": "2026-01-01T00:00:00",
            },
        )
        vector = np.zeros(db.EMBEDDING_DIM, dtype=np.float32)
        vector[0] = index * 0.01
        face_ids[source] = db.add_face(
            connection,
            media_id=media,
            bbox=(0, 10, 10, 0),
            embedding=vector,
            embed_version=db.embedding_model.ACTIVE.version,
            person_id=person if source in (db.ASSIGN_MANUAL, db.ASSIGN_AUTO) else None,
            assign_source=source,
            assign_score=55.0 if source == db.ASSIGN_AUTO else None,
        )
        db.save_media_scores(connection, media, smile_score=10.0, quality_score=20.0)
    db.set_face_age(connection, face_ids[db.ASSIGN_MANUAL], 7)
    connection.commit()
    db.recompute_family_scores(connection)
    return database, connection, face_ids


def test_unassign_auto_removes_only_the_automatic_assignments(tmp_path, capsys):
    """**手本と除外には触らない。** 触ると GUI で積み上げた判断が消える。"""
    database, connection, face_ids = _database_with_mixed_assignments(tmp_path)
    connection.close()
    capsys.readouterr()

    run_cli(["unassign-auto", "--db", str(database)], tmp_path)

    connection = db.ensure_database(str(database))
    try:
        rows = {row["id"]: row for row in db.list_faces(connection)}
        manual = rows[face_ids[db.ASSIGN_MANUAL]]
        assert manual["assign_source"] == db.ASSIGN_MANUAL
        assert manual["person_id"] is not None
        assert manual["age"] == 7, "年齢も残る"
        assert rows[face_ids[db.ASSIGN_REJECTED]]["assign_source"] == db.ASSIGN_REJECTED
        automatic = rows[face_ids[db.ASSIGN_AUTO]]
        assert automatic["assign_source"] is None
        assert automatic["person_id"] is None
        assert automatic["assign_score"] is None
    finally:
        connection.close()
    assert "Removed 1 automatic assignments" in capsys.readouterr().out


def test_unassign_auto_recomputes_the_family_score(tmp_path, capsys):
    """**消しただけだと family_score が古いまま残る。**

    `AnalysisResult.family_score` は「その写真に家族が写っている確からしさ」で、
    自動割り当てから計算される。消したのに数え直さないと、**もう存在しない
    割り当てのスコアが残り、`select` の family_only がその古い値で写真を選ぶ。**
    """
    database, connection, face_ids = _database_with_mixed_assignments(tmp_path)
    auto_media = db.get_face(connection, face_ids[db.ASSIGN_AUTO])["media_id"]
    manual_media = db.get_face(connection, face_ids[db.ASSIGN_MANUAL])["media_id"]
    # 家族の顔が写っているので点がある（式は `scoring.family_photo_score`）
    assert db.get_analysis_result(connection, auto_media)["family_score"] > 0.0
    manual_before = db.get_analysis_result(connection, manual_media)["family_score"]
    connection.close()
    capsys.readouterr()

    run_cli(["unassign-auto", "--db", str(database)], tmp_path)

    connection = db.ensure_database(str(database))
    try:
        assert db.get_analysis_result(connection, auto_media)["family_score"] == 0.0
        # 手本の写真の点は変わらない（手本は消していない）。
        assert db.get_analysis_result(connection, manual_media)["family_score"] == manual_before
        # scan が書いたスコアは潰れていない。
        assert db.get_analysis_result(connection, auto_media)["smile_score"] == 10.0
    finally:
        connection.close()


def test_unassign_auto_can_report_without_writing(tmp_path, capsys):
    """`--dry-run` は数えるだけ。**消す前に件数を確かめられる。**"""
    database, connection, _ = _database_with_mixed_assignments(tmp_path)
    connection.close()
    capsys.readouterr()

    run_cli(["unassign-auto", "--db", str(database), "--dry-run"], tmp_path)

    assert "(dry-run) Would remove 1" in capsys.readouterr().out
    connection = db.ensure_database(str(database))
    try:
        assert db.count_faces(connection, assign_source=db.ASSIGN_AUTO) == 1
    finally:
        connection.close()


def test_match_can_put_the_automatic_assignments_back(tmp_path, capsys):
    """**解除は取り返しがつく。** 手本と閾値だけで決まるので、流し直せば戻る。"""
    database, connection, _ = _database_with_mixed_assignments(tmp_path)
    connection.close()
    capsys.readouterr()
    run_cli(["unassign-auto", "--db", str(database)], tmp_path)
    capsys.readouterr()

    run_cli(["match", "--db", str(database), "--threshold", "0.5"], tmp_path)

    connection = db.ensure_database(str(database))
    try:
        assert db.count_faces(connection, assign_source=db.ASSIGN_AUTO) >= 1
    finally:
        connection.close()


def _two_people_with_automatic_faces(tmp_path):
    """2人に自動割り当てがあるDB。片方だけ消せるかを見るため。"""
    import numpy as np

    database = tmp_path / "photoarchive.db"
    run_cli(["init-db", "--db", str(database)], tmp_path)
    connection = db.ensure_database(str(database))
    people = {name: db.add_person(connection, name) for name in ("Alice", "Bob")}
    index = 0
    for name, person in people.items():
        for _ in range(2):
            index += 1
            media = db.save_media(
                connection,
                {
                    "path": f"/photos/{index}.jpg",
                    "filename": f"{index}.jpg",
                    "type": "image",
                    "file_hash": f"hash{index}",
                    "file_size": 100,
                    "created_time": "2026-01-01T00:00:00",
                },
            )
            db.add_face(
                connection,
                media_id=media,
                bbox=(0, 10, 10, 0),
                embedding=np.zeros(db.EMBEDDING_DIM, dtype=np.float32),
                embed_version=db.embedding_model.ACTIVE.version,
                person_id=person,
                assign_source=db.ASSIGN_AUTO,
                assign_score=55.0,
            )
    connection.commit()
    connection.close()
    return database, people


def test_unassign_auto_can_be_limited_to_one_person(tmp_path, capsys):
    """**人物を指定して消せる。** 1人ぶんだけ見直したいときに、全員を巻き込まない。"""
    database, people = _two_people_with_automatic_faces(tmp_path)
    capsys.readouterr()

    run_cli(["unassign-auto", "--db", str(database), "--person", "Alice"], tmp_path)

    assert "Removed 2 automatic assignments (Alice の)" in capsys.readouterr().out
    connection = db.ensure_database(str(database))
    try:
        assert db.count_faces(connection, assign_source=db.ASSIGN_AUTO) == 2
        assert (
            db.count_faces(
                connection, assign_source=db.ASSIGN_AUTO, person_id=people["Bob"]
            )
            == 2
        ), "Bob の自動割り当ては残る"
        assert (
            db.count_faces(
                connection, assign_source=db.ASSIGN_AUTO, person_id=people["Alice"]
            )
            == 0
        )
    finally:
        connection.close()


def test_unassign_auto_accepts_a_person_id_as_well_as_a_name(tmp_path, capsys):
    """id でも指せること。同名の人物がいるときの逃げ道になる。"""
    database, people = _two_people_with_automatic_faces(tmp_path)
    capsys.readouterr()

    run_cli(
        ["unassign-auto", "--db", str(database), "--person", str(people["Bob"])],
        tmp_path,
    )

    connection = db.ensure_database(str(database))
    try:
        assert (
            db.count_faces(
                connection, assign_source=db.ASSIGN_AUTO, person_id=people["Bob"]
            )
            == 0
        )
    finally:
        connection.close()


def test_an_unknown_person_stops_instead_of_removing_everything(tmp_path):
    """**名前を打ち間違えたときに全員ぶんを消さない。**

    絞り込みが黙って無視されると、1人ぶんのつもりで全部消える。
    """
    database, _ = _two_people_with_automatic_faces(tmp_path)

    with pytest.raises(SystemExit) as error:
        run_cli(["unassign-auto", "--db", str(database), "--person", "Carol"], tmp_path)

    assert "人物が見つかりません" in str(error.value)
    connection = db.ensure_database(str(database))
    try:
        assert db.count_faces(connection, assign_source=db.ASSIGN_AUTO) == 4
    finally:
        connection.close()


def test_a_duplicated_person_name_stops_instead_of_guessing(tmp_path):
    """**同じ名前が2人いたら決めない。** `Person.name` に UNIQUE 制約がまだ無い。

    勝手に1人目を選ぶと、**別人の割り当てを消す。**
    """
    database, _ = _two_people_with_automatic_faces(tmp_path)
    connection = db.ensure_database(str(database))
    db.add_person(connection, "Alice")
    connection.commit()
    connection.close()

    with pytest.raises(SystemExit) as error:
        run_cli(["unassign-auto", "--db", str(database), "--person", "Alice"], tmp_path)

    assert "同じ名前の人物が複数います" in str(error.value)
    connection = db.ensure_database(str(database))
    try:
        assert db.count_faces(connection, assign_source=db.ASSIGN_AUTO) == 4
    finally:
        connection.close()
