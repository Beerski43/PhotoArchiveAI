"""年齢の揃わない顔をまとめて割り当てるときの確認（`FaceAgesDialog`・#18）。"""

import io
import os

import numpy as np
import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QSpinBox

from photoarchive_ai import db
from photoarchive_ai import gui as photoarchive_gui

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    yield QApplication.instance() or QApplication([])


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(np.full((64, 64, 3), 120, dtype=np.uint8)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _unassigned_face(connection, index, shooting_date):
    media = db.save_media(
        connection,
        {
            "path": f"/photos/{index}.jpg",
            "filename": f"{index}.jpg",
            "type": "image",
            "file_hash": f"h{index}",
            "file_size": 100,
            "created_time": "2026-01-01T00:00:00",
            "shooting_date": shooting_date,
        },
    )
    face = db.add_face(
        connection, media_id=media, bbox=(0, 10, 10, 0), embedding=[0.0] * db.EMBEDDING_DIM,
        embed_version=db.embedding_model.ACTIVE.version, thumbnail=_jpeg(),
    )
    connection.commit()
    return face


#: 誕生日 2010-05-03 に対して 6 歳・9 歳・撮影日時なし・誕生前。
DATES = {
    "six": "2017-01-01T10:00:00",
    "nine": "2020-01-01T10:00:00",
    "unknown": None,
    "unborn": "2009-01-01T10:00:00",
}


@pytest.fixture()
def setup(tmp_path, qt_app, monkeypatch):
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)
    path = tmp_path / "ages.db"
    connection = db.ensure_database(str(path))
    person = db.add_person(connection, "${PERSON_1}")
    db.update_person(connection, person, "${PERSON_1}", "", "", birth_date="2010-05-03")
    faces = {name: _unassigned_face(connection, index, when) for index, (name, when) in enumerate(DATES.items())}
    connection.close()
    window = photoarchive_gui.MainWindow(str(path))
    window.show()
    qt_app.processEvents()
    person_record = next(p for p in db.list_persons(window.connection) if p["id"] == person)
    yield window, person_record, faces
    window.close()


def _select(window, face_ids):
    window.face_list.clearSelection()
    for row in range(window.face_list.count()):
        item = window.face_list.item(row)
        if item.data(photoarchive_gui.Qt.UserRole)["id"] in face_ids:
            item.setSelected(True)


def _capture_dialog(monkeypatch, result=QDialog.Accepted, edit=None):
    """本物の窓を開いて、押した結果だけ差し替える。"""
    opened = []

    def run(dialog):
        opened.append(dialog)
        if edit:
            edit(dialog)
        return result

    monkeypatch.setattr(photoarchive_gui.FaceAgesDialog, "exec", run)
    return opened


def test_faces_with_different_ages_are_shown_one_by_one_and_registered(setup, monkeypatch):
    """**写真ごとの年齢を出し、その年齢で登録する**（利用者の指定）。`未設定` の欄1つでは開かない。"""
    window, person, faces = setup
    monkeypatch.setattr(photoarchive_gui, "FaceAgeDialog", lambda *a, **k: pytest.fail("1つの欄で開いた"))
    opened = _capture_dialog(monkeypatch)
    _select(window, [faces["six"], faces["nine"]])

    window._assign_selected(person)

    assert len(opened) == 1
    assert opened[0].ages() == {faces["six"]: 6, faces["nine"]: 9}
    rows = {row["id"]: row for row in db.list_faces(window.connection)}
    assert rows[faces["six"]]["age"] == 6 and rows[faces["six"]]["person_id"] == person["id"]
    assert rows[faces["nine"]]["age"] == 9
    assert rows[faces["six"]]["assign_source"] == db.ASSIGN_MANUAL


def test_each_row_shows_the_picture_the_date_and_the_age(setup, monkeypatch):
    window, person, faces = setup
    opened = _capture_dialog(monkeypatch, QDialog.Rejected)
    _select(window, list(faces.values()))

    window._assign_selected(person)

    table = opened[0].table
    assert table.rowCount() == 4
    assert not table.item(0, 0).icon().isNull(), "サムネイルが出る"
    # 撮影時期の順で、分からない顔は最後
    assert table.item(0, 1).text().startswith("2009-01-01")
    assert table.item(1, 1).text().startswith("2017-01-01")
    assert table.item(2, 1).text().startswith("2020-01-01")
    assert "不明" in table.item(3, 1).text()
    age_text = lambda row: table.cellWidget(row, 2).findChild(QSpinBox).text()
    assert age_text(1) == "6 歳"
    assert age_text(0) == "未設定", "誕生前は未設定"
    assert age_text(3) == "未設定", "撮影日時が無ければ未設定"
    assert "2 件は年齢を出せない" in opened[0].findChild(photoarchive_gui.QLabel).text()


def test_an_age_can_be_corrected_in_the_list(setup, monkeypatch):
    """欄で直した年齢が入る。**直さない顔は計算した年齢のまま。**"""
    window, person, faces = setup
    _capture_dialog(monkeypatch, edit=lambda dialog: dialog.age_inputs[faces["unknown"]].setValue(4))
    _select(window, [faces["six"], faces["unknown"], faces["unborn"]])

    window._assign_selected(person)

    rows = {row["id"]: row for row in db.list_faces(window.connection)}
    assert rows[faces["unknown"]]["age"] == 4
    assert rows[faces["six"]]["age"] == 6
    assert rows[faces["unborn"]]["age"] is None, "未設定のままなら未設定"


def test_declining_assigns_nothing(setup, monkeypatch):
    window, person, faces = setup
    _capture_dialog(monkeypatch, QDialog.Rejected)
    _select(window, [faces["six"], faces["nine"]])

    window._assign_selected(person)

    rows = {row["id"]: row for row in db.list_faces(window.connection)}
    assert rows[faces["six"]]["person_id"] is None and rows[faces["six"]]["age"] is None


def test_undo_takes_back_the_ages_as_well(setup, monkeypatch):
    window, person, faces = setup
    _capture_dialog(monkeypatch)
    _select(window, [faces["six"], faces["nine"]])
    window._assign_selected(person)

    window.action_undo.trigger()

    rows = {row["id"]: row for row in db.list_faces(window.connection)}
    assert rows[faces["six"]]["person_id"] is None and rows[faces["six"]]["age"] is None
    assert rows[faces["nine"]]["age"] is None


@pytest.mark.parametrize("names", [["six"], ["unknown"]], ids=["one-face", "one-unknown"])
def test_one_face_uses_the_usual_dialog(setup, monkeypatch, names):
    window, person, faces = setup
    monkeypatch.setattr(photoarchive_gui.FaceAgesDialog, "exec", lambda self: pytest.fail("一覧で開いた"))
    monkeypatch.setattr(photoarchive_gui.FaceAgeDialog, "exec", lambda self: QDialog.Rejected)
    _select(window, [faces[name] for name in names])
    window._assign_selected(person)


def test_faces_of_the_same_age_use_the_usual_dialog(setup, monkeypatch):
    """揃っていれば、いままでどおり欄1つ（初期値が入る）。"""
    window, person, _faces = setup
    other = _unassigned_face(window.connection, 10, "2017-02-01T10:00:00")
    window.reload_faces()
    opened = []
    monkeypatch.setattr(photoarchive_gui.FaceAgesDialog, "exec", lambda self: pytest.fail("一覧で開いた"))
    monkeypatch.setattr(photoarchive_gui.FaceAgeDialog, "exec", lambda self: opened.append(self.age()) or QDialog.Rejected)
    _select(window, [_faces["six"], other])
    window._assign_selected(person)
    assert opened == [6]


def test_many_faces_scroll_and_the_wheel_does_not_change_an_age(setup, qt_app):
    """**多くてもスクロールで全部見える**（利用者の指定）。欄の上のホイールで年齢が変わらない。"""
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent

    window, _person, _faces = setup
    records = [{"id": index, "thumbnail": _jpeg()} for index in range(100)]
    dialog = photoarchive_gui.FaceAgesDialog(
        window, "${PERSON_1}", records, {index: None for index in range(100)}, {index: index % 20 for index in range(100)},
    )
    dialog.show()
    qt_app.processEvents()
    assert dialog.table.rowCount() == 100
    assert dialog.table.verticalScrollBar().maximum() > 0
    assert dialog.height() < 800, "窓は画面に収まり、表の中がスクロールする"

    spin = dialog.age_inputs[3]
    spin.setFocus()
    before = spin.value()
    centre = QPointF(spin.width() / 2, spin.height() / 2)
    event = QWheelEvent(centre, QPointF(spin.mapToGlobal(centre.toPoint())), QPoint(0, 0), QPoint(0, -120),
                        Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    QApplication.sendEvent(spin, event)
    assert spin.value() == before
    assert not event.isAccepted()
    dialog.close()


def test_the_database_takes_an_age_for_each_face(setup):
    window, person, faces = setup
    db.assign_faces(window.connection, [faces["six"], faces["nine"]], person["id"], age={faces["six"]: 6})
    rows = {row["id"]: row for row in db.list_faces(window.connection)}
    assert rows[faces["six"]]["age"] == 6
    assert rows[faces["nine"]]["age"] is None, "辞書に無い顔は未設定"

    db.set_faces_age(window.connection, [faces["six"], faces["nine"]], {faces["nine"]: 8})
    rows = {row["id"]: row for row in db.list_faces(window.connection)}
    assert rows[faces["nine"]]["age"] == 8
    assert rows[faces["six"]]["age"] is None


def _assigned(window, person, faces, stored):
    """顔を人物へ割り当て、``stored``（名前 → 年齢）の年齢を入れて、人物の一覧を見せる。"""
    db.assign_faces(window.connection, list(faces.values()), person["id"], db.ASSIGN_MANUAL)
    for name, age in stored.items():
        db.set_face_age(window.connection, faces[name], age)
    for row in range(window.person_list.count()):
        data = window.person_list.item(row).data(photoarchive_gui.Qt.UserRole)
        if data is not None and data["id"] == person["id"]:
            window.person_list.setCurrentRow(row)
    window.reload_faces()


def test_setting_ages_opens_the_same_list_with_stored_ages_first(setup, monkeypatch):
    """**`年齢を設定…` も同じ窓。初期値は登録済みの年齢、無ければ計算した年齢**（#18・利用者の指定）。"""
    window, person, faces = setup
    _assigned(window, person, faces, {"six": 40})
    monkeypatch.setattr(photoarchive_gui, "FaceAgeDialog", lambda *a, **k: pytest.fail("1つの欄で開いた"))
    opened = _capture_dialog(monkeypatch, QDialog.Rejected)
    _select(window, [faces["six"], faces["nine"], faces["unknown"]])

    window._set_age_selected()

    assert opened[0].ages() == {faces["six"]: 40, faces["nine"]: 9, faces["unknown"]: None}
    assert opened[0].register_button.text() == "この年齢で設定"
    assert "登録済みの年齢" in opened[0].findChild(photoarchive_gui.QLabel).text()


def test_setting_ages_can_be_corrected_and_undone(setup, monkeypatch):
    window, person, faces = setup
    _assigned(window, person, faces, {"six": 40})
    _capture_dialog(monkeypatch, edit=lambda dialog: dialog.age_inputs[faces["nine"]].setValue(10))
    _select(window, [faces["six"], faces["nine"]])

    window._set_age_selected()

    rows = {row["id"]: row for row in db.list_faces(window.connection)}
    assert rows[faces["six"]]["age"] == 40, "直さなければ登録済みのまま"
    assert rows[faces["nine"]]["age"] == 10

    window.action_undo.trigger()
    rows = {row["id"]: row for row in db.list_faces(window.connection)}
    assert rows[faces["six"]]["age"] == 40
    assert rows[faces["nine"]]["age"] is None


def _capture_single_dialog(monkeypatch):
    """1つの欄の窓が開いた初期値と文を控えて、閉じる。"""
    opened = []

    def run(dialog):
        opened.append((dialog.age(), dialog.summary))
        return QDialog.Rejected

    monkeypatch.setattr(photoarchive_gui.FaceAgeDialog, "exec", run)
    return opened


def test_setting_the_age_of_one_face_starts_from_its_stored_age(setup, monkeypatch):
    window, person, faces = setup
    _assigned(window, person, faces, {"six": 40})
    opened = _capture_single_dialog(monkeypatch)
    _select(window, [faces["six"]])

    window._set_age_selected()

    assert opened[0][0] == 40, "計算すれば 6 歳だが、登録済みの値を出す"
    assert "登録済みの年齢" in opened[0][1]


def test_setting_the_age_of_one_face_without_a_stored_age_starts_from_the_computed_age(setup, monkeypatch):
    window, person, faces = setup
    _assigned(window, person, faces, {})
    opened = _capture_single_dialog(monkeypatch)
    _select(window, [faces["six"]])

    window._set_age_selected()

    assert opened[0][0] == 6
    assert "計算した年齢" in opened[0][1]


def test_same_stored_ages_use_the_usual_dialog(setup, monkeypatch):
    """登録済みの年齢が揃っていれば1つの欄（計算すれば別の年齢でも）。"""
    window, person, faces = setup
    _assigned(window, person, faces, {"six": 40, "nine": 40})
    opened = _capture_single_dialog(monkeypatch)
    _select(window, [faces["six"], faces["nine"]])

    window._set_age_selected()

    assert opened[0][0] == 40


def test_assigning_keeps_the_computed_age_as_the_initial_value(setup, monkeypatch):
    """割り当ての初期値は計算した年齢のまま。**別の人物へ移すときに前の人物の年齢を出さない。**"""
    window, person, faces = setup
    _assigned(window, person, faces, {"six": 40})
    opened = _capture_single_dialog(monkeypatch)
    _select(window, [faces["six"]])

    window._assign_selected(person)

    assert opened[0][0] == 6
