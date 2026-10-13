"""GUI の「写真の選定」タブ（#104）。重みを動かして並べ直し、ルールに書き戻す。"""

import os
from pathlib import Path

import pytest
import yaml
from PySide6.QtWidgets import QApplication, QMessageBox

from photoarchive_ai import db, gui, gui_select, scoring, selection
from tests.helpers import scene, write_photo
from tests.test_selection import _add, _face

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def database(tmp_path):
    path = tmp_path / "test.db"
    connection = db.ensure_database(str(path))
    folder = tmp_path / "2019"
    folder.mkdir()
    for name, sharpness, smile, seed in (("blurred-smile.jpg", 40.0, 100.0, 1),
                                         ("sharp-straight.jpg", 200.0, 0.0, 2)):
        photo = write_photo(folder / name, scene(seed))
        _add(connection, photo, shooting_date="2019-05-03T10:00:00",
             faces=[_face("${PERSON_4}", sharpness=sharpness, smile=smile)])
    old = write_photo(tmp_path / "2018.jpg", scene(3))
    _add(connection, old, shooting_date="2018-01-02T10:00:00", faces=[_face("${PERSON_4}")])
    connection.close()
    return path


@pytest.fixture()
def rule_path(tmp_path):
    path = tmp_path / "rule.yml"
    path.write_text("# 元のコメント\nfamily_only: true\ncount_per_year: 5\nremove_similar: false\n",
                    encoding="utf-8")
    return path


@pytest.fixture()
def window(database, rule_path, tmp_path):
    window = gui.AppWindow(str(database), rule_path=str(rule_path),
                           output_root=str(tmp_path / "out"))
    yield window
    window.close()


def _names(panel):
    return [Path(media["path"]).name for media in panel.selected]


def test_the_window_has_the_assignment_tab_first_and_the_selection_tab_second(window):
    assert window.count() == 2
    assert window.tabText(0) == "顔の割り当て" and isinstance(window.widget(0), gui.MainWindow)
    assert window.tabText(1) == "写真の選定"
    assert isinstance(window.widget(1), gui_select.SelectionPanel)


def test_the_settings_start_from_the_rule_file(window):
    panel = window.selection
    assert panel.flag_checks["family_only"].isChecked()
    assert not panel.flag_checks["remove_similar"].isChecked()
    assert panel.weight_spins["smile"].value() == pytest.approx(scoring.WEIGHT_SMILE)


def test_loading_lists_every_year_with_its_candidates(window):
    panel = window.selection
    panel.load_candidates()
    rows = {
        panel.year_table.item(row, 0).text(): (
            panel.year_table.item(row, 1).text(), panel.year_table.cellWidget(row, 2).value()
        )
        for row in range(panel.year_table.rowCount())
    }
    # 整数の count_per_year（5）は全部の年に入る
    assert rows == {"2018": ("1", 5), "2019": ("2", 5)}


def test_choosing_again_with_other_weights_changes_the_order(window):
    """**重みを動かして「並べ直す」と、その場で並びが変わる**（#102 の要望）。"""
    panel = window.selection
    panel.choose()
    in_2019 = [n for n in _names(panel) if n != "2018.jpg"]
    assert in_2019 == ["sharp-straight.jpg", "blurred-smile.jpg"]
    panel.weight_spins["smile"].setValue(3.0)
    panel.choose()
    assert [n for n in _names(panel) if n != "2018.jpg"] == ["blurred-smile.jpg", "sharp-straight.jpg"]


def test_a_year_set_to_zero_is_not_chosen(window):
    panel = window.selection
    panel.load_candidates()
    for row in range(panel.year_table.rowCount()):
        if panel.year_table.item(row, 0).text() == "2018":
            panel.year_table.cellWidget(row, 2).setValue(0)
    panel.choose()
    assert "2018.jpg" not in _names(panel)
    assert panel.current_rule()["count_per_year"] == {2018: 0, 2019: 5}


def test_fill_all_years_sets_every_year(window):
    panel = window.selection
    panel.load_candidates()
    panel.fill_spin.setValue(7)
    panel.fill_all_years()
    assert panel.current_rule()["count_per_year"] == {2018: 7, 2019: 7}


def test_saving_writes_the_rule_and_keeps_a_backup(window, rule_path, monkeypatch):
    """**保存の前に控えを取る**（ファイルのコメントは消えるため）。書いたルールは CLI でも読める。"""
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
    panel = window.selection
    panel.load_candidates()
    panel.weight_spins["smile"].setValue(0.5)
    panel.save_rule()
    backup = Path(str(rule_path) + ".bak")
    assert "元のコメント" in backup.read_text(encoding="utf-8")
    saved = selection.load_rule(str(rule_path))
    assert saved["weights"]["smile"] == pytest.approx(0.5)
    assert saved["count_per_year"] == {2018: 5, 2019: 5}
    assert saved["family_only"] is True


def test_the_breakdown_adds_up_to_the_score(window):
    """点の内訳の合計が、並びに使った点と一致する（なぜその順位かを見せる）。"""
    panel = window.selection
    panel.choose()
    for media in panel.selected:
        parts = selection.explain(panel.candidates, panel.chosen_rule, media["id"])
        assert parts["total"] == pytest.approx(media["family_score"])
        assert "点" in gui_select.format_breakdown(parts)


def test_the_results_are_paged(window, monkeypatch):
    monkeypatch.setattr(gui_select, "RESULT_PAGE_SIZE", 1)
    panel = window.selection
    panel.choose()
    index = [panel.year_combo.itemData(i) for i in range(panel.year_combo.count())].index(2019)
    panel.year_combo.setCurrentIndex(index)
    assert panel.result_list.count() == 1 and panel.page_label.text() == "1 / 2"
    panel._turn(1)
    assert panel.page_label.text() == "2 / 2"
    assert not panel.next_button.isEnabled()


def test_exporting_copies_the_chosen_photos(window, tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
    panel = window.selection
    panel.choose()
    panel.export()
    assert len(list((tmp_path / "out").iterdir())) == len(panel.selected) == 3


def test_a_thumbnail_comes_from_the_exif_and_follows_the_orientation(tmp_path):
    from PIL import Image

    from tests.helpers import exif_with_thumbnail

    photo = tmp_path / "a.jpg"
    body = Image.new("RGB", (400, 200), (10, 20, 30))
    raw = exif_with_thumbnail(Image.new("RGB", (160, 80), (200, 0, 0)))
    body.save(photo, "JPEG", exif=raw)
    thumb = gui_select.photo_thumbnail(str(photo))
    assert thumb is not None and thumb.getpixel((5, 5))[0] > 150  # 埋め込みの赤
    assert gui_select.photo_thumbnail(str(tmp_path / "missing.jpg")) is None
    assert gui_select._orient(Image.new("RGB", (40, 20)), 6).size == (20, 40)


def test_the_breakdown_never_shows_minus_zero():
    parts = {key: 0.0 for key, _ in gui_select.BREAKDOWN_LABELS}
    parts["strangers"] = -0.0
    parts["total"] = 0.0
    assert "-0.0" not in gui_select.format_breakdown(parts)


def test_selecting_a_photo_shows_where_it_is_and_why(window):
    panel = window.selection
    panel.choose()
    panel.result_list.setCurrentRow(0)
    text = panel.detail.text()
    assert "点 " in text and "鮮明さ" in text
