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
