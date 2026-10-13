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
    later = f"{gui_select.later_year()} 年以降"
    assert rows == {"2018": ("1", 5), "2019": ("2", 5), "年不明": ("0", 5), later: ("", 5)}


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
    assert panel.current_rule()["count_per_year"] == {
        2018: 0, 2019: 5, "unknown": 5, f"{gui_select.later_year()}+": 5
    }


def test_fill_all_years_sets_every_year(window):
    panel = window.selection
    panel.load_candidates()
    panel.fill_spin.setValue(7)
    panel.fill_all_years()
    assert panel.current_rule()["count_per_year"] == {
        2018: 7, 2019: 7, "unknown": 7, f"{gui_select.later_year()}+": 7
    }


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
    assert saved["count_per_year"] == {
        2018: 5, 2019: 5, "unknown": 5, f"{gui_select.later_year()}+": 5
    }
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


# ---------------------------------------------------------------------------
# PR #108 のレビュー対応
# ---------------------------------------------------------------------------


@pytest.fixture()
def mixed(tmp_path):
    """2019（手本）・2017（自動割り当て）・2016（他人だけ）・年不明（手本・EXIF 日時が壊れている）。"""
    path = tmp_path / "mixed.db"
    connection = db.ensure_database(str(path))
    folder = tmp_path / "photos"
    folder.mkdir()
    for name, when, face, seed in (
        ("dated.jpg", "2019-05-03T10:00:00", _face("${PERSON_4}"), 1),
        ("auto2017.jpg", "2017-05-03T10:00:00", _face("${PERSON_4}", source=db.ASSIGN_AUTO), 2),
        ("nofamily2016.jpg", "2016-05-03T10:00:00", _face(None), 3),
        ("undated.jpg", "0000-00-00T00:00:00", _face("${PERSON_4}"), 4),
    ):
        _add(connection, write_photo(folder / name, scene(seed)), shooting_date=when, faces=[face])
    connection.close()
    return path


def _panel(mixed, tmp_path, rule_text):
    rule = tmp_path / "mixed-rule.yml"
    rule.write_text(rule_text, encoding="utf-8")
    window = gui.AppWindow(str(mixed), rule_path=str(rule), output_root=str(tmp_path / "out"))
    return window, window.selection


def test_unchecking_family_only_adds_the_years_without_family(mixed, tmp_path):
    """**絞り込みを変えたら年の表を作り直す**（PR #108 のレビュー指摘1）。

    作り直さないと、家族の写っていない写真しかない年が表に無く、選ばれなかった。
    """
    window, panel = _panel(mixed, tmp_path, "family_only: true\ncount_per_year: 5\nremove_similar: false\n")
    panel.load_candidates()
    panel.fill_spin.setValue(5)
    panel.fill_all_years()
    panel.flag_checks["family_only"].setChecked(False)
    years = [panel.year_table.item(r, 0).text() for r in range(panel.year_table.rowCount())]
    assert "2016" in years
    panel.choose()
    assert "nofamily2016.jpg" in _names(panel)
    window.close()


def test_reloading_for_auto_assignments_keeps_the_year_counts(mixed, tmp_path):
    """**読み込み直しても、入れた年ごとの枚数を引き継ぐ**（指摘1）。ルールファイルの値に戻さない。"""
    window, panel = _panel(
        mixed, tmp_path,
        "include_auto_assigned: false\ncount_per_year: 5\nremove_similar: false\n",
    )
    panel.load_candidates()
    panel.fill_spin.setValue(1)
    panel.fill_all_years()
    before = panel.current_rule()["count_per_year"]
    panel.flag_checks["include_auto_assigned"].setChecked(True)
    panel.choose()
    after = panel.current_rule()["count_per_year"]
    assert all(after[year] == 1 for year in before)
    window.close()


def test_reloading_clears_the_previous_results(mixed, tmp_path, monkeypatch):
    """**読み込み直したら前の結果を捨てる**（指摘2）。古い結果をクリックすると例外、出力は古いまま
    コピーしていた。"""
    window, panel = _panel(mixed, tmp_path, "remove_similar: false\n")
    panel.choose()
    assert panel.result_list.count() > 0 or panel.year_combo.count() > 0
    panel.flag_checks["include_auto_assigned"].setChecked(False)
    panel.load_candidates()
    assert panel.result_list.count() == 0 and panel.selected == []
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args: shown.append(args[2]))
    panel.export()
    assert shown and "並べ直す" in shown[0]
    window.close()


def test_the_weights_from_the_rule_are_shown_as_they_are(mixed, tmp_path):
    """**ルールの重みを丸めない・頭打ちにしない**（指摘3）。画面と CLI の結果がずれる。"""
    window, panel = _panel(mixed, tmp_path, "weights:\n  smile: 0.125\n  sharpness: 20\n")
    weights = panel.current_rule()["weights"]
    assert weights["smile"] == pytest.approx(0.125)
    assert weights["sharpness"] == pytest.approx(20.0)
    window.close()



def test_reading_an_int_rule_and_choosing_gives_the_cli_result(mixed, tmp_path):
    """**年の表に「年不明」と「翌年以降」の行がある**ので、整数のルールを読んで並べ直しても
    CLI と同じ結果になる（PR #108 のレビュー指摘4。利用者の決定）。以前は年不明の写真が落ちた。
    """
    window, panel = _panel(mixed, tmp_path, "count_per_year: 5\nremove_similar: false\n")
    panel.choose()
    cli = selection.select_media(
        panel.connection, selection.load_rule(str(tmp_path / "mixed-rule.yml"))
    )
    assert sorted(_names(panel)) == sorted(Path(m["path"]).name for m in cli)
    assert "undated.jpg" in _names(panel)
    window.close()


def test_the_later_row_covers_years_after_this_one(tmp_path):
    """翌年以降の行は、表に無い新しい年の枚数（年が変わったら設定し直す運用）。"""
    later = gui_select.later_year()
    limit = selection.year_limits({"count_per_year": {2019: 1, f"{later}+": 3, "unknown": 2}})
    assert (limit(2019), limit(2020), limit(later), limit(later + 5), limit(None)) == (1, 0, 3, 3, 2)
