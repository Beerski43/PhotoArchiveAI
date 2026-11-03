"""`scan` を途中で止めて、次は続きから（#129・GUI が裏で流すとき）。"""

import sqlite3

import pytest

from photoarchive_ai import db
from photoarchive_ai.scanner import ScanStopped, scan_directories, scan_directory
from tests.fakes import install_fake_backends
from tests.helpers import write_image


@pytest.fixture()
def connection(tmp_path):
    connection = db.ensure_database(str(tmp_path / "test.db"))
    yield connection
    connection.close()


def _photos(source, count):
    for index in range(count):
        write_image(source / f"{index:02d}.jpg", color=(20 + index * 7, 100, 150))


def _stop_after(count):
    """``count`` 回目に見たときから止める合図を返す。"""
    seen = {"n": 0}

    def should_stop():
        seen["n"] += 1
        return seen["n"] >= count

    return should_stop


def test_a_stopped_scan_keeps_what_it_did_and_the_next_scan_continues(tmp_path, connection):
    source = tmp_path / "media"
    # 前回あって今は無いファイル。**途中で止めたら消さない**（一覧が途中かもしれない）
    gone = write_image(source / "gone.jpg", color=(250, 250, 250))
    scan_directory(str(source), connection, workers=1)
    gone.unlink()
    _photos(source, 6)

    progress = []
    # 一覧づくりの段で 2 回見る（打ち切り・stat の段の最後）。3 回目は 1 件目を書いたあと
    with pytest.raises(ScanStopped) as stopped:
        scan_directory(
            str(source),
            connection,
            workers=1,
            should_stop=_stop_after(4),
            progress_callback=lambda current, total, detail: progress.append(current),
        )

    assert stopped.value.summary["processed"] == 2
    assert progress == [1, 2]
    other = sqlite3.connect(str(tmp_path / "test.db"))
    # **済んだぶんは commit してある**（ほかの接続から見える）
    scanned = other.execute("SELECT COUNT(*) FROM Media WHERE face_count IS NOT NULL").fetchone()[0]
    assert scanned == 3  # 前回の gone.jpg と、今回の 2 件
    assert other.execute("SELECT COUNT(*) FROM Media WHERE filename = 'gone.jpg'").fetchone()[0] == 1
    other.close()

    summary = scan_directory(str(source), connection, workers=1)

    assert summary["processed"] == 4
    assert summary["skipped"] == 2
    assert summary["pruned"] == 1
    assert len(db.list_media(connection)) == 6


def test_a_parallel_scan_stops_without_finishing_every_file(tmp_path, connection):
    """``with ProcessPoolExecutor`` のまま抜けると、残りを全件処理し終えるまで待つ。"""
    source = tmp_path / "media"
    _photos(source, 30)

    with pytest.raises(ScanStopped) as stopped:
        scan_directory(
            str(source),
            connection,
            workers=2,
            worker_initializer=install_fake_backends,
            should_stop=_stop_after(3),
        )

    assert stopped.value.summary["processed"] == 1
    scanned = connection.execute("SELECT COUNT(*) FROM Media").fetchone()[0]
    assert scanned == 1


def test_a_stop_while_listing_writes_nothing(tmp_path, connection):
    source = tmp_path / "media"
    _photos(source, 3)

    with pytest.raises(ScanStopped) as stopped:
        scan_directory(str(source), connection, workers=1, should_stop=lambda: True)

    assert stopped.value.summary["processed"] == 0
    assert connection.execute("SELECT COUNT(*) FROM Media").fetchone()[0] == 0


def test_a_stop_skips_the_remaining_roots(tmp_path, connection):
    first, second = tmp_path / "a", tmp_path / "b"
    _photos(first, 2)
    _photos(second, 2)
    calls = {"n": 0}

    def stop_in_second_root():
        calls["n"] += 1
        # 1つ目の root を終えた（一覧 2 回 + 書く 2 回）あと、2つ目の一覧の打ち切りで止める
        return calls["n"] > 4

    with pytest.raises(ScanStopped) as stopped:
        scan_directories([str(first), str(second)], connection, workers=1, should_stop=stop_in_second_root)

    summary = stopped.value.summary
    assert summary["processed"] == 2
    assert [part["root"] for part in summary["roots"]] == [str(first.resolve()), str(second.resolve())]
    # 終えた root だけを記録する
    assert db.list_scan_roots(connection) == [str(first.resolve())]


def test_the_commit_interval_bounds_how_long_the_write_lock_is_held(tmp_path, connection):
    """GUI と同時に流すときは 1 件ごとに commit する。間にほかの接続が書ける。"""
    source = tmp_path / "media"
    _photos(source, 3)
    other = sqlite3.connect(str(tmp_path / "test.db"), timeout=0)
    written = []

    def write_from_another_connection(current, total, detail):
        other.execute("INSERT INTO Person (name) VALUES (?)", (f"p{current}",))
        other.commit()
        written.append(current)

    scan_directory(
        str(source), connection, workers=1, commit_interval=1, progress_callback=write_from_another_connection
    )

    assert written == [1, 2, 3]
    other.close()


def test_the_default_commit_interval_holds_the_lock_between_files(tmp_path, connection):
    """既定（CLI）は今までどおりまとめて commit する。上のテストが何を見ているかの対照。"""
    source = tmp_path / "media"
    _photos(source, 2)
    other = sqlite3.connect(str(tmp_path / "test.db"), timeout=0)
    failures = []

    def write_from_another_connection(current, total, detail):
        try:
            other.execute("INSERT INTO Person (name) VALUES (?)", (f"p{current}",))
            other.commit()
        except sqlite3.OperationalError as error:
            failures.append(str(error))

    scan_directory(str(source), connection, workers=1, progress_callback=write_from_another_connection)

    assert failures and "locked" in failures[0]
    other.close()
