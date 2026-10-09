"""`match`（自動割り当て）を GUI から流す口（#67・対話で追加）。

**手本を増やしたら、その場で `match` を流して結果を見たい。** 端末へ移ると
そこで手が止まる。控えを取るかは毎回選べる（既定は取る）。
"""

import os
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication, QDialog

from photoarchive_ai import db
from photoarchive_ai import gui as photoarchive_gui

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def _vector(axis: int) -> list:
    vector = np.zeros(db.EMBEDDING_DIM, dtype=np.float32)
    vector[axis] = 1.0
    return vector.tolist()


def _face(connection, index, axis):
    media_id = db.save_media(
        connection,
        {
            "path": f"/photos/m{index}.jpg",
            "filename": f"m{index}.jpg",
            "type": "image",
            "file_hash": f"h{index}",
            "file_size": 100,
            "created_time": "2026-01-01T00:00:00",
            "shooting_date": "2020-06-01T10:00:00",
        },
    )
    return db.add_face(
        connection,
        media_id=media_id,
        bbox=(0, 10, 10, 0),
        embedding=_vector(axis),
        embed_version=db.embedding_model.ACTIVE.version,
        thumbnail=b"",
    )


@pytest.fixture()
def seeded(tmp_path):
    """手本1件と、同じ顔（同じ特徴量）の未割当1件・別人の未割当1件。"""
    database = tmp_path / "match.db"
    connection = db.ensure_database(str(database))
    person_id = db.add_person(connection, "${PERSON_4}", birth_date="2010-12-01")
    teacher = _face(connection, 0, axis=0)
    same = _face(connection, 1, axis=0)
    other = _face(connection, 2, axis=5)
    db.assign_faces(connection, [teacher], person_id, db.ASSIGN_MANUAL)
    connection.commit()
    connection.close()
    window = photoarchive_gui.MainWindow(str(database))
    yield window, database, person_id, {"手本": teacher, "同じ顔": same, "別人": other}
    window.connection.close()


def _accepting(backup: bool):
    class _Dialog:
        opened = False

        def __init__(self, parent, counts, database_path):
            _Dialog.opened = True

        def exec(self):
            return QDialog.Accepted

        def makes_backup(self):
            return backup

    return _Dialog


@pytest.fixture()
def shown(monkeypatch):
    """結果の知らせを受け取る。**窓を開いたままにしない。**"""
    messages = []
    for kind in ("information", "critical"):
        monkeypatch.setattr(
            photoarchive_gui.QMessageBox,
            kind,
            lambda parent, title, text, *a, _kind=kind, **k: messages.append((_kind, title, text)),
        )
    return messages


def test_match_runs_from_the_window_and_refreshes_the_counts(seeded, monkeypatch, shown):
    """**GUI から流せて、終わったら左の件数まで出し直す。**"""
    window, database, person_id, faces = seeded
    monkeypatch.setattr(photoarchive_gui, "MatchDialog", _accepting(backup=False))

    window._run_match()

    rows = {row["id"]: row for row in db.list_faces(window.connection)}
    assert rows[faces["同じ顔"]]["person_id"] == person_id
    assert rows[faces["同じ顔"]]["assign_source"] == db.ASSIGN_AUTO
    assert rows[faces["別人"]]["person_id"] is None, "似ていない顔は付けない"
    # 左の一覧の件数が、流したあとの値になっている
    assert window.person_list.item(1).text().endswith("1"), "自動割当 1"
    kind, title, text = shown[-1]
    assert kind == "information"
    assert "自動で割り当てた顔: 1 件" in text
    assert "${PERSON_4}: 1 件" in text


def test_the_backup_is_taken_when_chosen(seeded, monkeypatch, shown):
    """**控えを選んだら、流す前の状態がそのまま入っている。**"""
    window, database, person_id, faces = seeded
    monkeypatch.setattr(photoarchive_gui, "MatchDialog", _accepting(backup=True))

    window._run_match()

    backups = list(Path(database).parent.glob("match.db.bak-*"))
    assert len(backups) == 1
    copy = db.connect(str(backups[0]))
    try:
        # **流す前の状態**: 同じ顔はまだ未割当
        assert db.get_face(copy, faces["同じ顔"])["person_id"] is None
    finally:
        copy.close()
    assert str(backups[0]) in shown[-1][2], "控えの場所を知らせる"


def test_no_backup_is_written_when_declined(seeded, monkeypatch, shown):
    window, database, _person_id, _faces = seeded
    monkeypatch.setattr(photoarchive_gui, "MatchDialog", _accepting(backup=False))

    window._run_match()

    assert list(Path(database).parent.glob("match.db.bak-*")) == []
    assert "控え: 取っていません" in shown[-1][2]


def test_cancelling_the_dialog_changes_nothing(seeded, monkeypatch, shown):
    window, database, _person_id, faces = seeded

    class _Declining(_accepting(backup=True)):
        def exec(self):
            return QDialog.Rejected

    monkeypatch.setattr(photoarchive_gui, "MatchDialog", _Declining)

    window._run_match()

    assert db.get_face(window.connection, faces["同じ顔"])["person_id"] is None
    assert list(Path(database).parent.glob("match.db.bak-*")) == []
    assert shown == []


def test_without_teachers_it_says_so_instead_of_running(tmp_path, monkeypatch, shown):
    """**手本が無ければ流さない。** 流しても何も付かず、なぜかが分からない。"""
    database = tmp_path / "empty.db"
    connection = db.ensure_database(str(database))
    _face(connection, 0, axis=0)
    connection.commit()
    connection.close()
    window = photoarchive_gui.MainWindow(str(database))
    dialog = _accepting(backup=True)
    monkeypatch.setattr(photoarchive_gui, "MatchDialog", dialog)
    try:
        window._run_match()
    finally:
        window.connection.close()

    assert dialog.opened is False
    assert shown[-1][1] == "手本がありません"


def test_the_progress_bar_reaches_every_candidate(seeded, monkeypatch, shown):
    """**進み具合が件数で出て、最後まで届くこと。** 実データでは約 26 秒かかる。"""
    window, _database, _person_id, _faces = seeded
    reported = []

    class _Spy(photoarchive_gui.WorkProgress):
        def __call__(self, done, total):
            reported.append((done, total))
            super().__call__(done, total)

    monkeypatch.setattr(photoarchive_gui, "WorkProgress", _Spy)
    monkeypatch.setattr(photoarchive_gui, "MatchDialog", _accepting(backup=False))

    window._run_match()

    assert reported, "進み具合が1度も出ていない"
    done, total = reported[-1]
    assert done == total == 2, "未割当2件を照合し終えたところまで出る"


def test_the_dialog_backs_up_by_default_and_does_not_start_on_enter(seeded):
    """控えは**既定で取る**。既定のボタンは「やめる」（Enter の連打で走らない）。"""
    window, database, _person_id, _faces = seeded

    dialog = photoarchive_gui.MatchDialog(window, db.face_counts(window.connection), str(database))

    assert dialog.makes_backup() is True
    assert dialog.cancel_button.isDefault() is True
    assert dialog.run_button.isDefault() is False
    dialog.backup_box.setChecked(False)
    assert dialog.makes_backup() is False


def test_the_summary_names_each_person_and_the_backup():
    text = photoarchive_gui.format_match_summary(
        {
            "teachers": 10577,
            "candidates": 46742,
            "assigned": 15467,
            "unassigned": 31275,
            "no_candidate": 12,
            "per_person": {3: 6208, 4: 6245},
        },
        [{"id": 3, "name": "${PERSON_4}"}, {"id": 4, "name": "${PERSON_3}"}],
        Path("data/photoarchive.db.bak-20261009"),
    )

    assert "自動で割り当てた顔: 15,467 件" in text
    assert "誕生日で候補が1人も残らなかった顔: 12 件" in text
    # 多い順に並ぶ
    assert text.index("${PERSON_3}: 6,245 件") < text.index("${PERSON_4}: 6,208 件")
    assert "控え: data/photoarchive.db.bak-20261009" in text
