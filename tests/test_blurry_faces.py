"""ボケた顔を鮮明さで絞り、まとめて顔認証外にする（#135）。

利用者の要望「ピンぼけ、ぶれている写真をフィルターして顔認証外に一気に設定できないか。
ぶれ方の閾値を調整して選定したい」。決めたこと（2026-11-03）: 対象は**未分類だけ**・
鮮明さで絞っていれば**行事を選ばなくても**まとめて処理してよい・未計測の顔は
**GUI から裏で測る**。

ここで見張ること:

- **未計測の顔は絞り込みに出ない**（ボケているか分からない顔をまとめて外さない）
- まとめて顔認証外にするのは**ボケた未分類だけ**。割り当て済み・くっきりした顔・未計測の顔は残る
- 行事を選ばずに押せるのは、未分類を鮮明さで絞っているときだけ（いまの安全弁は残す）
- 測る処理は未分類も測り、止めたら次は残りから。**測れない環境では何も書かない**
"""

import io
import os
import time

import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from photoarchive_ai import appearance, db, jobs, scoring
from photoarchive_ai import gui as photoarchive_gui
from tests.fakes import install_fake_backends

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def _jpeg() -> bytes:
    yy, xx = np.mgrid[0:120, 0:120]
    board = (((yy // 6) + (xx // 6)) % 2) * 255
    buffer = io.BytesIO()
    Image.fromarray(np.stack([board] * 3, axis=-1).astype(np.uint8)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _media(connection, index):
    return db.save_media(
        connection,
        {
            "path": f"/photos/2020/{index}.jpg",
            "filename": f"{index}.jpg",
            "type": "image",
            "file_hash": f"h{index}",
            "file_size": 100,
            "created_time": "2026-01-01T00:00:00",
            "shooting_date": f"2020-05-{index + 1:02d}T10:00:00",
        },
    )


def _face(connection, index, sharpness=None, person_id=None, assign_source=None):
    face_id = db.add_face(
        connection,
        media_id=_media(connection, index),
        bbox=(0, 10, 10, 0),
        embedding=[0.0] * db.EMBEDDING_DIM,
        embed_version=db.embedding_model.ACTIVE.version,
        person_id=person_id,
        assign_source=assign_source,
        thumbnail=_jpeg(),
    )
    if sharpness is not None:
        db.save_appearance(
            connection,
            [(face_id, appearance.Appearance(aligned=True, yaw=0.0, sharpness=sharpness))],
        )
    connection.commit()
    return face_id


@pytest.fixture()
def seeded(tmp_path):
    """未分類のボケた顔・境目の顔・くっきりした顔・未計測の顔と、ボケた手本・自動の顔。"""
    path = tmp_path / "blur.db"
    connection = db.ensure_database(str(path))
    person = db.add_person(connection, "家族A")
    faces = {
        "ボケ": _face(connection, 0, 12.0),
        "境目": _face(connection, 1, 30.0),
        "くっきり": _face(connection, 2, 200.0),
        "未計測": _face(connection, 3),
        "ボケた手本": _face(connection, 4, 10.0, person, db.ASSIGN_MANUAL),
        "ボケた自動": _face(connection, 5, 11.0, person, db.ASSIGN_AUTO),
    }
    yield path, connection, faces
    connection.close()


# ---------------------------------------------------------------------------
# db: 絞り込みと並び
# ---------------------------------------------------------------------------


def test_the_sharpness_filter_includes_the_threshold_and_never_the_unmeasured(seeded):
    _, connection, faces = seeded
    filters = {"unassigned": True, "max_sharpness": 30}

    ids = db.face_ids(connection, **filters)

    assert ids == sorted([faces["ボケ"], faces["境目"]])
    assert db.count_faces(connection, **filters) == 2
    listed = db.list_faces(connection, order=db.ORDER_SHOT_DESC, **filters)
    assert sorted(face["id"] for face in listed) == ids
    assert faces["未計測"] not in db.face_ids(connection, unassigned=True, max_sharpness=10_000)


def test_blurry_first_order_puts_the_unmeasured_last_both_ways(seeded):
    _, connection, faces = seeded

    ascending = [f["id"] for f in db.list_faces(connection, unassigned=True, order=db.ORDER_SHARPNESS_ASC)]
    descending = [f["id"] for f in db.list_faces(connection, unassigned=True, order=db.ORDER_SHARPNESS_DESC)]

    assert ascending == [faces["ボケ"], faces["境目"], faces["くっきり"], faces["未計測"]]
    assert descending == [faces["くっきり"], faces["境目"], faces["ボケ"], faces["未計測"]]
    assert "sharpness" in db.list_faces(connection, unassigned=True)[0]


def test_unassigned_faces_can_be_picked_for_measuring(seeded):
    _, connection, faces = seeded

    assert db.faces_without_appearance(connection, (db.ASSIGN_MANUAL,)) == []
    assert db.faces_without_appearance(connection, (), unassigned=True) == [faces["未計測"]]
    assert db.count_unassigned_without_sharpness(connection) == 1


# ---------------------------------------------------------------------------
# 測る
# ---------------------------------------------------------------------------


def test_filling_can_include_unassigned_faces_and_stops_between_chunks(seeded, monkeypatch):
    _, connection, faces = seeded
    more = [_face(connection, 10 + index) for index in range(2)]
    monkeypatch.setattr(appearance, "FILL_CHUNK_SIZE", 1)
    calls = {"n": 0}

    def stop_after_first_chunk():
        calls["n"] += 1
        return calls["n"] > 1

    first = appearance.fill_missing(connection, unassigned=True, should_stop=stop_after_first_chunk)

    assert len(first) == 1, "1つ目の塊を測ったところで止まる"
    rest = appearance.fill_missing(connection, unassigned=True)
    assert set(first) | set(rest) == {faces["未計測"], *more}
    assert db.count_unassigned_without_sharpness(connection) == 0


def test_the_measure_job_writes_nothing_when_landmarks_are_unavailable(seeded, monkeypatch):
    """**「測れない」と「整列できなかった」を混ぜない**（CLAUDE.md §8）。"""
    path, connection, _ = seeded
    monkeypatch.setattr(scoring, "_load_mediapipe_face_mesh", lambda: None)

    with pytest.raises(RuntimeError, match="測れません"):
        jobs.run_measure_faces(lambda _event: None, lambda: False, str(path))

    assert db.count_unassigned_without_sharpness(connection) == 1


def test_the_measure_job_runs_in_another_process(seeded):
    path, connection, faces = seeded
    job = jobs.Job(
        "measure_faces", jobs.run_measure_faces, initializer=install_fake_backends, database_path=str(path)
    )
    job.start()

    final = job.wait(timeout=60)

    assert final is not None and final["kind"] == "done", final
    assert final["summary"] == {"measured": 1, "remaining": 0}
    assert db.get_face(connection, faces["未計測"])["sharpness"] > 0
    assert "測った顔 1 件" in jobs.describe_summary(final["summary"])


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------


@pytest.fixture()
def window(seeded, monkeypatch):
    path, _, faces = seeded
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: QMessageBox.Ok)
    window = photoarchive_gui.MainWindow(str(path))
    yield window, faces
    window.connection.close()
    window.deleteLater()


def _ids(window):
    return sorted(window.face_list.item(i).data(Qt.UserRole)["id"] for i in range(window.face_list.count()))


def test_without_the_sharpness_filter_bulk_rejecting_still_needs_an_event(window):
    window, _ = window
    assert window._current_scope() == photoarchive_gui.SCOPE_UNASSIGNED
    assert not window.bulk_event_button.isEnabled()


def test_blurry_unassigned_faces_are_rejected_at_once_without_an_event_and_undone(window):
    window, faces = window
    window.blur_spin.setValue(30)
    window.blur_check.setChecked(True)

    assert _ids(window) == sorted([faces["ボケ"], faces["境目"]])
    assert window.bulk_event_button.isEnabled()
    assert "鮮明さ 30 以下" in window.bulk_event_button.text()
    assert all("鮮明さ" in window.face_list.item(i).text() for i in range(window.face_list.count()))

    window._bulk_event_action()

    connection = window.connection
    source = lambda name: db.get_face(connection, faces[name])["assign_source"]  # noqa: E731
    assert source("ボケ") == db.ASSIGN_REJECTED
    assert source("境目") == db.ASSIGN_REJECTED
    assert source("くっきり") is None
    assert source("未計測") is None, "未計測の顔はボケているか分からないので外さない"
    assert source("ボケた手本") == db.ASSIGN_MANUAL, "割り当て済みの顔は触らない"
    assert source("ボケた自動") == db.ASSIGN_AUTO
    assert _ids(window) == []

    window._undo_or_redo(redo=False)

    assert source("ボケ") is None and source("境目") is None


def test_a_lower_threshold_narrows_what_is_rejected(window):
    window, faces = window
    window.blur_check.setChecked(True)
    window.blur_spin.setValue(20)

    assert _ids(window) == [faces["ボケ"]]
    window._bulk_event_action()
    assert db.get_face(window.connection, faces["境目"])["assign_source"] is None


def test_the_sharpness_filter_does_not_unlock_bulk_actions_on_a_person(window):
    """鮮明さで絞っても、**人物の割り当ての解除**は行事を選ばないと押せない（未分類だけの決定）。"""
    window, _ = window
    window.blur_check.setChecked(True)
    for row in range(window.person_list.count()):
        if window.person_list.item(row).data(Qt.UserRole) is not None:
            window.person_list.setCurrentRow(row)
            break
    assert window._current_scope() == photoarchive_gui.SCOPE_PERSON
    assert not window.bulk_event_button.isEnabled()


def test_the_window_shows_how_many_unassigned_faces_are_unmeasured(window):
    window, _ = window
    assert "未計測 1 件" in window.measure_label.text()


def test_the_measure_button_measures_in_the_background(seeded, monkeypatch):
    path, connection, faces = seeded
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: QMessageBox.Ok)
    app = photoarchive_gui.AppWindow(str(path))
    app.measure_initializer = install_fake_backends
    try:
        app.assignment.measure_button.click()
        assert app.jobs.is_running()
        assert not app.assignment.measure_button.isEnabled()
        deadline = time.time() + 60
        while app.jobs.is_running() and time.time() < deadline:
            app.jobs.poll()
            QCoreApplication.processEvents()
            time.sleep(0.05)

        assert app.last_measure is not None and app.last_measure["kind"] == "done"
        assert db.get_face(connection, faces["未計測"])["sharpness"] > 0
        assert app.assignment.measure_button.isEnabled()
        assert "測った顔 1 件" in app.assignment.measure_label.text()
    finally:
        app.jobs.stop_and_wait(timeout=30)
        app.assignment.connection.close()
        app.deleteLater()
