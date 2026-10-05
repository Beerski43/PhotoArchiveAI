"""行事ごとの束ねを測るスクリプト（Issue #61）。

**測定の道具がおかしいと、間違った閾値で実データを束ねることになる。**
いちばん大事なのは**数え落とさないこと** — 撮影日時が読めない顔を黙って
外していたため、「未割当 51,860 件」が全体の数のように文書へ出ていた
（PR #62 のレビュー指摘2）。
"""

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from photoarchive_ai import db, embedding

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "measure_event_clustering.py"


@pytest.fixture(scope="module")
def measure_module():
    spec = importlib.util.spec_from_file_location("measure_event_clustering", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def _vector(value: float) -> list:
    values = [0.0] * embedding.ACTIVE.dimensions
    values[0] = value
    values[1] = 1.0 - abs(value)
    return values


def _seed_database(path: Path) -> None:
    """1つの行事に近い顔2件、別の日に1件、日付不明のフォルダに2件。"""
    connection = db.ensure_database(str(path))
    try:
        person = db.add_person(connection, "${PERSON_2}")
        rows = [
            ("/photos/undoukai/a.jpg", "2012-10-06T10:00:00", 1.0, db.ASSIGN_MANUAL),
            ("/photos/undoukai/b.jpg", "2012-10-06T11:00:00", 0.99, None),
            ("/photos/undoukai/c.jpg", "2012-10-07T11:00:00", 0.98, None),
            # 壊れた EXIF。日が決まらない（**捨てずにフォルダ単位で束ねる**）。
            ("/photos/broken/d.jpg", "TTTT-TT-TTTTT:TT:TT", 1.0, None),
            ("/photos/broken/e.jpg", "0000-00-00 00:00:00", 0.99, None),
        ]
        for path_text, shot, value, source in rows:
            media_id = db.save_media(
                connection,
                {
                    "path": path_text,
                    "filename": Path(path_text).name,
                    "type": "image",
                    "file_hash": path_text,
                    "file_size": 10,
                    "created_time": "2012-10-06T10:00:00",
                    "shooting_date": shot,
                },
            )
            db.add_face(
                connection,
                media_id=media_id,
                bbox=(0, 24, 24, 0),
                embedding=_vector(value),
                embed_version=embedding.ACTIVE.version,
                thumbnail=b"",
                quality_score=value,
                person_id=person if source == db.ASSIGN_MANUAL else None,
                assign_source=source,
            )
        connection.commit()
    finally:
        connection.close()


def test_the_measurement_never_writes_to_the_database(measure_module, tmp_path):
    """**実データに書かない。** 読み取り専用で開いていること。"""
    database = tmp_path / "events.db"
    _seed_database(database)
    before = database.stat().st_mtime_ns

    measure_module.load_events(str(database))

    assert database.stat().st_mtime_ns == before
    with pytest.raises(sqlite3.OperationalError):
        read_only = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            read_only.execute("UPDATE Face SET age = 9")
        finally:
            read_only.close()


def test_undated_faces_are_counted_as_folder_events(measure_module, tmp_path):
    """**撮影日時が読めない顔を捨てない。** フォルダ単位の行事として数える。

    以前は `continue` で外していたため、「未割当 51,860 件 → 決定 19,678 回」が
    **日付つきだけの数**だったのに、文書は全体の数のように書いていた
    （実データで 5,910 件が測定の外。PR #62 のレビュー指摘2）。
    """
    database = tmp_path / "events.db"
    _seed_database(database)

    dated, undated, counts = measure_module.load_events(str(database))

    assert [(event.folder, event.day) for event in dated] == [
        ("/photos/undoukai", "2012-10-06"),
        ("/photos/undoukai", "2012-10-07"),
    ]
    # **フォルダ単位にまとまる**（日は決められないので `None`）。
    assert [(event.folder, event.day) for event in undated] == [("/photos/broken", None)]
    assert len(undated[0].faces) == 2
    assert counts["undated"] == 2
    # 数え落としが無いこと。
    assert counts["faces"] == sum(len(event.faces) for event in dated + undated)


def test_rejected_faces_are_left_out(measure_module, tmp_path):
    """除外した顔は入れない。**もう人が判断した顔**なので決定は減らない。"""
    database = tmp_path / "events.db"
    _seed_database(database)
    connection = db.connect(str(database))
    try:
        target = db.face_ids(connection, unassigned=True, folder="/photos/broken")[0]
        db.reject_faces(connection, [target])
        connection.commit()
    finally:
        connection.close()

    _dated, undated, counts = measure_module.load_events(str(database))
    assert len(undated[0].faces) == 1
    assert counts["undated"] == 1


def test_the_separation_is_measured_on_dated_events_only(measure_module, tmp_path):
    """**行事の中と外の分離に、日付不明の顔を混ぜない。**

    混ぜると、フォルダ単位（日をまたぐ）のペアが「行事の中」に紛れ、
    どちらの数字も信じられなくなる。
    """
    database = tmp_path / "events.db"
    _seed_database(database)
    dated, undated, _counts = measure_module.load_events(str(database))

    separation = measure_module.measure_separation(dated)
    # 手本が1件しか無いので、同一人物のペアは作れない（0件でよい）。
    assert separation.within_same == []
    assert measure_module.measure_separation(undated).within_same == []


def test_the_decision_count_is_the_number_of_bundles_with_unassigned_faces(
    measure_module, tmp_path
):
    """**決定の数 = 未割当を含む束の数。** 手本だけの束は数えない。"""
    database = tmp_path / "events.db"
    _seed_database(database)
    dated, _undated, _counts = measure_module.load_events(str(database))

    run = measure_module.measure_clustering(dated, threshold=0.45)

    # 10-06 の行事は手本1件＋未割当1件で、近いので1束（決定1回）。
    # 10-07 の行事は未割当1件だけ（決定1回）。
    assert run.faces == 2
    assert run.decisions == 2


def test_the_report_keeps_the_two_kinds_of_events_apart(measure_module, tmp_path):
    """報告が、日付つきと日付不明を**別の表**で出すこと。

    1つの表に混ぜると、条件の違う数字が同じ列に並ぶ。
    """
    database = tmp_path / "events.db"
    _seed_database(database)
    dated, undated, counts = measure_module.load_events(str(database))
    separation = measure_module.measure_separation(dated)
    runs = [measure_module.measure_clustering(dated, 0.45)]
    undated_runs = [measure_module.measure_clustering(undated, 0.45)]

    report = measure_module.format_report(dated, undated, counts, separation, runs, undated_runs)

    assert "## 2. 束ねたときの決定の数と混ざり方（**日付つきの行事**）" in report
    assert "## 3. 撮影日時が読めない顔（**フォルダ単位**）" in report
    assert embedding.ACTIVE.version in report
