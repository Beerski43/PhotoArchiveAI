"""長い処理を裏の別プロセスで流す（#129）。子は `spawn` で起こすので、本物の子プロセスで確かめる。"""

import os
import sqlite3
import time

from photoarchive_ai import db, jobs
from tests.fakes import install_fake_backends
from tests.helpers import write_image


def _scan_job(tmp_path, count=3, **options):
    source = tmp_path / "media"
    for index in range(count):
        write_image(source / f"{index}.jpg", color=(20 + index * 10, 100, 150))
    database = tmp_path / "test.db"
    db.ensure_database(str(database)).close()
    job = jobs.Job(
        "scan",
        jobs.run_scan,
        initializer=install_fake_backends,
        database_path=str(database),
        roots=[str(source)],
        worker_initializer=install_fake_backends,
        **options,
    )
    return job, database


def _drain(job, timeout=60):
    final = job.wait(timeout=timeout)
    assert final is not None, "子プロセスが終わらない"
    return final


def test_a_scan_job_runs_in_another_process_and_reports_the_summary(tmp_path):
    job, database = _scan_job(tmp_path)
    job.start()

    final = _drain(job)

    assert final["kind"] == "done"
    assert final["summary"]["processed"] == 3
    assert "media_ids" not in final["summary"]
    assert job._process.pid != os.getpid()
    connection = sqlite3.connect(str(database))
    assert connection.execute("SELECT COUNT(*) FROM Face").fetchone()[0] == 3
    connection.close()


def test_progress_reaches_the_parent(tmp_path):
    job, _ = _scan_job(tmp_path)
    job.start()
    events = []
    deadline = time.time() + 60
    while job.is_running() and time.time() < deadline:
        events.extend(job.poll())
        time.sleep(0.05)

    kinds = [event["kind"] for event in events]
    assert kinds[-1] == "done"
    progress = [event["current"] for event in events if event["kind"] == "progress"]
    assert progress == [1, 2, 3]


def test_a_stop_request_ends_the_job_as_stopped(tmp_path):
    job, database = _scan_job(tmp_path)
    # 始める前に合図しておけば、一覧を作ったところで止まる（時間に頼らない）
    job.request_stop()
    job.start()

    final = _drain(job)

    assert final["kind"] == "stopped"
    assert final["summary"]["processed"] == 0
    connection = sqlite3.connect(str(database))
    assert connection.execute("SELECT COUNT(*) FROM ScanRoot").fetchone()[0] == 0
    connection.close()


def test_a_safety_abort_is_reported_with_its_reason(tmp_path):
    database = tmp_path / "test.db"
    db.ensure_database(str(database)).close()
    empty = tmp_path / "empty"
    empty.mkdir()
    job = jobs.Job(
        "scan", jobs.run_scan, initializer=install_fake_backends, database_path=str(database), roots=[str(empty)]
    )
    job.start()

    final = _drain(job)

    assert final["kind"] == "aborted"
    assert "マウント" in final["message"]


def test_an_unexpected_failure_is_reported(tmp_path):
    job = jobs.Job(
        "scan",
        jobs.run_scan,
        initializer=install_fake_backends,
        database_path=str(tmp_path / "test.db"),
        roots=[str(tmp_path / "does-not-exist")],
    )
    job.start()

    final = _drain(job)

    assert final["kind"] == "failed"
    assert final["message"]


def test_a_child_that_dies_silently_is_reported_as_failed(tmp_path):
    job, _ = _scan_job(tmp_path, count=1)
    job.start()
    job._process.kill()
    job._process.join(timeout=10)

    final = _drain(job)

    assert final["kind"] == "failed"
    assert "終了コード" in final["message"]


def test_the_summary_line_names_every_count():
    line = jobs.describe_summary({"processed": 1200, "skipped": 3, "faces": 4, "pruned": 5, "errors": 6})
    assert "1,200" in line and "飛ばした 3" in line and "削除 5" in line and "エラー 6" in line


# ---------------------------------------------------------------------------
# convert-heic と assess（#130）
# ---------------------------------------------------------------------------


def test_a_convert_job_converts_in_another_process(tmp_path):
    from tests.helpers import write_heic

    source = tmp_path / "media"
    write_heic(source / "a.heic")
    write_heic(source / "sub" / "b.heic", color=(30, 160, 90))
    job = jobs.Job("convert_heic", jobs.run_convert_heic, roots=[str(source)])
    job.start()
    final = _drain(job)

    assert final["kind"] == "done", final
    assert final["summary"]["converted"] == 2
    assert (source / "a.jpg").exists() and (source / "sub" / "b.jpg").exists()
    assert "変換 2 件" in jobs.describe_summary(final["summary"], "convert_heic")


def test_a_convert_job_skips_an_unreadable_heic_and_lists_it(tmp_path):
    from tests.helpers import write_heic

    source = tmp_path / "media"
    write_heic(source / "good.heic")
    (source / "broken.heic").write_bytes(b"not a heic at all")
    job = jobs.Job("convert_heic", jobs.run_convert_heic, roots=[str(source)])
    job.start()
    events = []
    while job.is_running():
        events.extend(job.poll())
        time.sleep(0.05)

    assert job.final["kind"] == "done", job.final
    assert job.final["summary"]["converted"] == 1
    assert job.final["summary"]["unreadable"] == [str(source / "broken.heic")]
    assert any(event["kind"] == "error" and "broken.heic" in event["message"] for event in events)


def test_a_convert_job_stops_when_a_jpeg_cannot_be_written(tmp_path):
    """書けなければ続けても書けない（読み取り専用・空き容量）。止めて理由を出す。"""
    from tests.helpers import write_heic

    source = tmp_path / "media"
    write_heic(source / "a.heic")
    os.chmod(source, 0o555)
    try:
        if os.access(source, os.W_OK):  # root で流しているとき
            return
        job = jobs.Job("convert_heic", jobs.run_convert_heic, roots=[str(source)])
        job.start()
        final = _drain(job)
    finally:
        os.chmod(source, 0o755)

    assert final["kind"] == "aborted", final
    assert "JPEG を書けませんでした" in final["message"]


def _assess_job(tmp_path, count=3):
    from tests.fakes import fake_quality_models
    from tests.helpers import add_family_photo

    database = tmp_path / "test.db"
    connection = db.ensure_database(str(database))
    ids = [add_family_photo(connection, tmp_path / "media" / f"{i}.jpg", width=40 + i) for i in range(count)]
    connection.close()
    job = jobs.Job(
        "assess",
        jobs.run_assess,
        database_path=str(database),
        roots=[str(tmp_path / "media")],
        models_factory=fake_quality_models,
    )
    return job, database, ids


def test_an_assess_job_measures_family_photos_in_another_process(tmp_path):
    from photoarchive_ai import quality

    job, database, ids = _assess_job(tmp_path)
    job.start()
    final = _drain(job)

    assert final["kind"] == "done", final
    assert final["summary"] == {"total": 3, "measured": 3, "unreadable": 0, "missing": 0}
    connection = db.ensure_database(str(database))
    try:
        assert sorted(db.photo_quality(connection, quality.MODEL_VERSION)) == ids
    finally:
        connection.close()
    assert "測った写真 3 枚" in jobs.describe_summary(final["summary"], "assess")


def test_an_assess_job_stopped_at_once_measures_at_most_one_photo(tmp_path):
    job, _database, _ids = _assess_job(tmp_path)
    job.request_stop()
    job.start()
    final = _drain(job)

    assert final["kind"] == "stopped", final
    assert final["summary"]["measured"] <= 1


def test_an_assess_job_without_models_fails_with_how_to_make_them(tmp_path, monkeypatch):
    """モデルが無いときは子で落ちても、置き方が届く（GUI は始める前にも確かめる）。"""
    from photoarchive_ai import quality

    monkeypatch.setattr("photoarchive_ai.face.find_model_file", lambda name: None)
    report = []
    try:
        jobs.run_assess(report.append, lambda: False, str(tmp_path / "x.db"), [])
    except FileNotFoundError as error:
        assert "export_iqa_onnx.py" in str(error)
    else:  # pragma: no cover
        raise AssertionError("モデルが無いのに始まった")
    assert quality.missing_model_files()


def test_nothing_to_assess_reads_naturally():
    assert jobs.describe_summary({"total": 0, "measured": 0, "unreadable": 0, "missing": 0}, "assess") == (
        "測る写真はありません（家族の写った写真はすべて測ってあります）"
    )
