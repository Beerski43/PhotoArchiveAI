"""GUI の「写真の選定」タブ（#104）。重みを動かして並べ直し、ルールに書き戻す。"""

import os
from pathlib import Path

import pytest
import yaml
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from photoarchive_ai import config, db, gui, gui_select, scoring, selection
from tests.helpers import scene, write_photo
from tests.test_selection import _add, _face

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def settings_file(tmp_path, monkeypatch):
    """**ルールの読み込みと別名の保存は設定ファイルに書く。** カレントごと tmp_path へ移す。

    移さないと、本体の checkout で流したテストが実データの `config/app_settings.yml` を書き換える。
    返すのは、書かれる先。
    """
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work / "config" / "app_settings.yml"


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
    # タブは2枚。取り込みはメニューのウィザードへ移した（#132）
    tabs = window.tabs
    assert tabs.count() == 2
    assert tabs.tabText(0) == "顔の割り当て" and isinstance(tabs.widget(0), gui.MainWindow)
    assert tabs.tabText(1) == "写真の選定"
    assert isinstance(tabs.widget(1), gui_select.SelectionPanel)


def test_the_settings_start_from_the_rule_file(window):
    panel = window.selection
    assert panel.flag_checks["family_only"].isChecked()
    assert not panel.flag_checks["remove_similar"].isChecked()
    assert panel.weight_spins["smile"].value() == pytest.approx(scoring.WEIGHT_SMILE)


def test_loading_lists_every_year_with_its_candidates(window):
    """**タブを開いた時点で**年ごとの表が出て、枚数を入れられる（#113・「候補を読み込む」は無くした）。"""
    panel = window.selection
    assert not hasattr(panel, "load_button")
    window.tabs.setCurrentWidget(panel)
    window.show()
    assert panel.year_table.rowCount() > 0, "開いただけで表がある"
    rows = {
        panel.year_table.item(row, 0).text(): (
            panel.year_table.item(row, 1).text(), panel.year_table.cellWidget(row, 2).value()
        )
        for row in range(panel.year_table.rowCount())
    }
    # 整数の count_per_year（5）は全部の年に入る
    # 「〜年以降」は候補のある最後の年の翌年から（#134）
    assert rows == {"2018": ("1", 5), "2019": ("2", 5), "年不明": ("0", 5), "2020 年以降": ("", 5)}


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
    panel.refresh_years()
    for row in range(panel.year_table.rowCount()):
        if panel.year_table.item(row, 0).text() == "2018":
            panel.year_table.cellWidget(row, 2).setValue(0)
    panel.choose()
    assert "2018.jpg" not in _names(panel)
    assert panel.current_rule()["count_per_year"] == {
        2018: 0, 2019: 5, "unknown": 5, "2020+": 5
    }


def test_fill_all_years_sets_every_year(window):
    panel = window.selection
    panel.refresh_years()
    panel.fill_spin.setValue(7)
    panel.fill_all_years()
    assert panel.current_rule()["count_per_year"] == {
        2018: 7, 2019: 7, "unknown": 7, "2020+": 7
    }


def test_saving_writes_the_rule_and_keeps_a_backup(window, rule_path, monkeypatch):
    """**保存の前に控えを取る**（ファイルのコメントは消えるため）。書いたルールは CLI でも読める。"""
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
    panel = window.selection
    panel.refresh_years()
    panel.weight_spins["smile"].setValue(0.5)
    panel.save_rule()
    backup = Path(str(rule_path) + ".bak")
    assert "元のコメント" in backup.read_text(encoding="utf-8")
    saved = selection.load_rule(str(rule_path))
    assert saved["weights"]["smile"] == pytest.approx(0.5)
    assert saved["count_per_year"] == {
        2018: 5, 2019: 5, "unknown": 5, "2020+": 5
    }
    assert saved["family_only"] is True


def _yes(monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)


def _pick_file(monkeypatch, path):
    """ファイルを選ぶ窓で ``path`` を選ぶ（空文字は取り消し）。"""
    for name in ("getOpenFileName", "getSaveFileName"):
        monkeypatch.setattr(gui_select.QFileDialog, name, lambda *args, **kwargs: (str(path), ""))


def test_loading_a_rule_file_replaces_the_screen_and_becomes_the_save_target(window, rule_path, tmp_path, monkeypatch):
    """**読み込んだファイルが以後の保存先になる**（#18）。もとのファイルには書かない。年の表も新しい枚数から。"""
    _yes(monkeypatch)
    panel = window.selection
    panel.refresh_years()
    panel.weight_spins["smile"].setValue(0.9)
    other = tmp_path / "other.yml"
    other.write_text("weights:\n  smile: 0.25\ncount_per_year: 3\ndate:\n  start: 2019-01-01\n", encoding="utf-8")
    original = rule_path.read_text(encoding="utf-8")
    _pick_file(monkeypatch, other)

    panel.load_rule_file()

    assert panel.rule_path == str(other)
    assert other.name in panel.rule_file_label.text()
    assert panel.weight_spins["smile"].value() == pytest.approx(0.25)
    assert panel.period_start.text() == "2019-01-01"
    assert panel._year_values()[2019] == 3
    panel.weight_spins["smile"].setValue(0.5)
    panel.save_rule()
    assert selection.load_rule(str(other))["weights"]["smile"] == pytest.approx(0.5)
    assert rule_path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "text",
    ["weights: [\n", "- not-a-mapping\n", "weights:\n  smile: -1\n"],
    ids=["broken-yaml", "not-a-dict", "bad-weight"],
)
def test_an_unreadable_rule_file_leaves_the_screen_as_it_was(window, rule_path, tmp_path, monkeypatch, text):
    """読めないファイルでは知らせるだけ。**動かした値も保存先もそのまま。** 壊れた YAML で落ちない。"""
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: warnings.append(args[2]))
    panel = window.selection
    panel.weight_spins["smile"].setValue(0.9)
    bad = tmp_path / "bad.yml"
    bad.write_text(text, encoding="utf-8")
    _pick_file(monkeypatch, bad)

    panel.load_rule_file()

    assert len(warnings) == 1
    assert panel.rule_path == str(rule_path)
    assert panel.weight_spins["smile"].value() == pytest.approx(0.9)


def test_declining_or_cancelling_a_load_changes_nothing(window, rule_path, tmp_path, monkeypatch):
    panel = window.selection
    panel.weight_spins["smile"].setValue(0.9)
    other = tmp_path / "other.yml"
    other.write_text("weights:\n  smile: 0.25\n", encoding="utf-8")

    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.No)
    _pick_file(monkeypatch, other)
    panel.load_rule_file()
    _pick_file(monkeypatch, "")
    panel.load_rule_file()

    assert panel.rule_path == str(rule_path)
    assert panel.weight_spins["smile"].value() == pytest.approx(0.9)


def test_saving_to_another_file_leaves_the_original_and_becomes_the_save_target(window, rule_path, tmp_path, monkeypatch):
    """**別のファイルに保存しても、もとのファイルは変わらない**（#18）。以後の上書きは新しいファイルへ。"""
    _yes(monkeypatch)
    panel = window.selection
    panel.refresh_years()
    panel.weight_spins["smile"].setValue(0.5)
    original = rule_path.read_text(encoding="utf-8")
    target = tmp_path / "movie.yml"
    _pick_file(monkeypatch, target)

    panel.save_rule_as()

    assert selection.load_rule(str(target))["weights"]["smile"] == pytest.approx(0.5)
    assert rule_path.read_text(encoding="utf-8") == original
    assert panel.rule_path == str(target)
    assert target.name in panel.rule_file_label.text()
    assert not Path(str(target) + ".bak").exists(), "新しいファイルに控えは要らない"

    panel.weight_spins["smile"].setValue(0.75)
    panel.save_rule()
    assert selection.load_rule(str(target))["weights"]["smile"] == pytest.approx(0.75)
    assert rule_path.read_text(encoding="utf-8") == original
    assert Path(str(target) + ".bak").exists()


def test_saving_to_another_file_adds_a_readable_suffix_and_backs_up_an_existing_file(window, tmp_path, monkeypatch):
    panel = window.selection
    _pick_file(monkeypatch, tmp_path / "plain")
    panel.save_rule_as()
    assert panel.rule_path == str(tmp_path / "plain.yml")
    selection.load_rule(panel.rule_path)

    existing = tmp_path / "existing.yaml"
    existing.write_text("# 残す\n", encoding="utf-8")
    _pick_file(monkeypatch, existing)
    panel.save_rule_as()
    assert panel.rule_path == str(existing)
    assert "残す" in Path(str(existing) + ".bak").read_text(encoding="utf-8")


def test_cancelling_save_as_or_failing_to_write_keeps_the_save_target(window, rule_path, tmp_path, monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: warnings.append(args[2]))
    panel = window.selection
    _pick_file(monkeypatch, "")
    panel.save_rule_as()
    assert panel.rule_path == str(rule_path) and not warnings

    _pick_file(monkeypatch, tmp_path / "no-such-folder" / "rule.yml")
    panel.save_rule_as()
    assert panel.rule_path == str(rule_path)
    assert len(warnings) == 1


def test_saving_without_a_rule_file_asks_where(database, tmp_path, monkeypatch):
    window = gui.AppWindow(str(database), rule_path=None, output_root=str(tmp_path / "out"))
    target = tmp_path / "new.yml"
    _pick_file(monkeypatch, target)
    window.selection.save_rule()
    assert window.selection.rule_path == str(target) and target.exists()
    window.close()


def _settings(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _existing_settings(path):
    """もとの設定ファイル（ほかのキーが残ることを見る）。"""
    path.parent.mkdir()
    path.write_text("# コメント\noutput_root: /keep\ndlib_model_dir: /models\nrule_path: old.yml\n", encoding="utf-8")


def test_loading_a_rule_file_also_rewrites_the_rule_path(window, tmp_path, monkeypatch, settings_file):
    """**読み込むと設定の `rule_path` も変わる**（#18・利用者の決定）。次の起動と CLI もそのファイルを読む。ほかのキーは残し、控えを取る。"""
    _yes(monkeypatch)
    _existing_settings(settings_file)
    other = tmp_path / "other.yml"
    other.write_text("weights:\n  smile: 0.25\n", encoding="utf-8")
    _pick_file(monkeypatch, other)

    window.selection.load_rule_file()

    assert _settings(settings_file) == {"output_root": "/keep", "dlib_model_dir": "/models", "rule_path": str(other)}
    assert "コメント" in Path(str(settings_file) + ".bak").read_text(encoding="utf-8")
    assert config.get_rule_path(config.load_settings()) == str(other)


def test_saving_to_another_file_also_rewrites_the_rule_path_but_overwriting_does_not(
    window, tmp_path, monkeypatch, settings_file
):
    _yes(monkeypatch)
    _existing_settings(settings_file)
    target = tmp_path / "movie.yml"
    _pick_file(monkeypatch, target)

    window.selection.save_rule_as()
    assert _settings(settings_file)["rule_path"] == str(target)

    written = settings_file.read_text(encoding="utf-8")
    Path(str(settings_file) + ".bak").unlink()
    window.selection.save_rule()
    assert settings_file.read_text(encoding="utf-8") == written
    assert not Path(str(settings_file) + ".bak").exists(), "上書きの保存では設定に書かない"


def test_a_rule_operation_that_did_not_happen_leaves_the_settings_alone(
    window, tmp_path, monkeypatch, settings_file
):
    """断った・取り消した・読めない・書けないときは、設定ファイルに触らない。"""
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: None)
    _existing_settings(settings_file)
    before = settings_file.read_text(encoding="utf-8")
    panel = window.selection
    other = tmp_path / "other.yml"
    other.write_text("weights:\n  smile: 0.25\n", encoding="utf-8")
    bad = tmp_path / "bad.yml"
    bad.write_text("weights: [\n", encoding="utf-8")

    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.No)
    _pick_file(monkeypatch, other)
    panel.load_rule_file()
    _yes(monkeypatch)
    _pick_file(monkeypatch, bad)
    panel.load_rule_file()
    _pick_file(monkeypatch, "")
    panel.load_rule_file()
    panel.save_rule_as()
    _pick_file(monkeypatch, tmp_path / "no-such-folder" / "rule.yml")
    panel.save_rule_as()

    assert settings_file.read_text(encoding="utf-8") == before
    assert not Path(str(settings_file) + ".bak").exists()


def test_choosing_the_file_already_in_the_settings_does_not_rewrite_them(
    database, tmp_path, monkeypatch, settings_file
):
    """同じファイルなら書かない（設定のコメントが消えるだけ）。相対パスで書いてあっても同じファイルと分かる。"""
    _yes(monkeypatch)
    rule = settings_file.parent.parent / "rule.yml"
    rule.write_text("weights:\n  smile: 0.25\n", encoding="utf-8")
    settings_file.parent.mkdir()
    settings_file.write_text("# コメント\nrule_path: rule.yml\n", encoding="utf-8")
    before = settings_file.read_text(encoding="utf-8")
    window = gui.AppWindow(str(database), rule_path="rule.yml", output_root=str(tmp_path / "out"))
    _pick_file(monkeypatch, rule)

    window.selection.load_rule_file()

    assert settings_file.read_text(encoding="utf-8") == before
    window.close()


def test_a_settings_file_that_cannot_be_written_only_warns(window, tmp_path, monkeypatch, settings_file):
    """**設定に書けなくても、読み込みは取り消さない。** 壊れた設定は上書きしない。設定の窓を保存しても前のファイルへ戻らない。"""
    _yes(monkeypatch)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: warnings.append(args[2]))
    settings_file.parent.mkdir()
    settings_file.write_text("rule_path: [\n", encoding="utf-8")
    panel = window.selection
    configured = panel.configured_rule_path
    other = tmp_path / "other.yml"
    other.write_text("weights:\n  smile: 0.25\n", encoding="utf-8")
    _pick_file(monkeypatch, other)

    panel.load_rule_file()

    assert len(warnings) == 1 and "前のファイル" in warnings[0]
    assert settings_file.read_text(encoding="utf-8") == "rule_path: [\n"
    assert panel.rule_path == str(other)
    assert panel.weight_spins["smile"].value() == pytest.approx(0.25)
    assert not panel.set_paths(configured, str(tmp_path / "out"), panel.source_roots)
    assert panel.rule_path == str(other)


def test_saving_the_settings_after_a_load_keeps_the_loaded_file(window, tmp_path, monkeypatch):
    """設定の窓の保存は `set_paths` を呼ぶ。読み込みで `rule_path` も変わっているので、読み直さない。"""
    _yes(monkeypatch)
    panel = window.selection
    other = tmp_path / "other.yml"
    other.write_text("weights:\n  smile: 0.25\n", encoding="utf-8")
    _pick_file(monkeypatch, other)
    panel.load_rule_file()
    panel.weight_spins["smile"].setValue(0.4)

    assert not panel.set_paths(str(other), str(tmp_path / "out"), panel.source_roots)
    assert panel.rule_path == str(other)
    assert panel.weight_spins["smile"].value() == pytest.approx(0.4)

    # 設定ファイルの rule_path が別の値になっていたら、そちらが効く
    changed = tmp_path / "changed.yml"
    changed.write_text("weights:\n  smile: 0.1\n", encoding="utf-8")
    assert panel.set_paths(str(changed), str(tmp_path / "out"), panel.source_roots)
    assert panel.rule_path == str(changed)
    assert panel.weight_spins["smile"].value() == pytest.approx(0.1)


def test_a_broken_yaml_rule_at_startup_warns_instead_of_crashing(database, tmp_path, monkeypatch):
    """本筋の外だが直した。構文の壊れた YAML は ValueError ではないので、起動ごと落ちていた。"""
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: warnings.append(args[2]))
    rule = tmp_path / "broken.yml"
    rule.write_text("weights: [\n", encoding="utf-8")
    window = gui.AppWindow(str(database), rule_path=str(rule), output_root=str(tmp_path / "out"))
    assert len(warnings) == 1
    assert window.selection.rule == {}
    window.close()


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
    panel.year_range.set_range("2019", "2019")
    assert panel.result_list.count() == 1 and panel.page_label.text() == "1 / 2"
    panel._turn(1)
    assert panel.page_label.text() == "2 / 2"
    assert not panel.next_button.isEnabled()


def test_exporting_copies_the_chosen_photos(window, tmp_path, monkeypatch):
    """#121: 既定は年ごとのフォルダ。確認の文言にもそう出す。"""
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: asked.append(args[2]) or QMessageBox.Yes)
    panel = window.selection
    panel.choose()
    panel.export()
    copies = [path for path in (tmp_path / "out").rglob("*") if path.is_file()]
    assert len(copies) == len(panel.selected) == 3
    assert all(copy.parent.parent == tmp_path / "out" for copy in copies)
    assert "年ごとのフォルダ" in asked[0]


def _rows(panel):
    return {
        panel.year_table.item(row, 0).text(): panel.year_table.cellWidget(row, 2).value()
        for row in range(panel.year_table.rowCount())
    }


def _rule_panel(database, tmp_path, text):
    path = tmp_path / "period-rule.yml"
    path.write_text(text, encoding="utf-8")
    window = gui.AppWindow(str(database), rule_path=str(path), output_root=str(tmp_path / "out"))
    window.selection.refresh_years()
    return window, window.selection, path


def _set_period(panel, start, end):
    panel.period_start.setText(start)
    panel.period_end.setText(end)
    panel._period_changed()


def test_exporting_uses_the_layout_on_the_screen_without_choosing_again(window, tmp_path, monkeypatch):
    """出力の形は選ぶ写真を変えないので、**選んだあとに変えても「出力する」に効く**（#134）。"""
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: asked.append(args[2]) or QMessageBox.Yes)
    panel = window.selection
    panel.choose()
    panel.layout_combo.setCurrentIndex(panel.layout_combo.findData(selection.OUTPUT_LAYOUT_FLAT))
    panel.export()
    assert sorted(path.is_file() for path in (tmp_path / "out").iterdir()) == [True, True, True]
    assert "直下" in asked[0]


def test_the_layout_on_the_screen_comes_from_the_rule_and_is_saved(database, tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
    window, panel, path = _rule_panel(database, tmp_path, "output_layout: flat\n")
    assert panel.layout_combo.currentData() == selection.OUTPUT_LAYOUT_FLAT
    panel.layout_combo.setCurrentIndex(panel.layout_combo.findData(selection.OUTPUT_LAYOUT_YEAR))
    panel.save_rule()
    assert selection.load_rule(str(path))["output_layout"] == selection.OUTPUT_LAYOUT_YEAR
    window.close()


def _pick(box, value):
    box.setCurrentIndex(box.findData(value))


def test_the_period_is_chosen_with_year_month_and_day_boxes(database, tmp_path):
    """**撮影時期は年・月・日のコンボボックス**（#18・利用者の指定）。ルールの値が欄に割れて出る。"""
    window, panel, _ = _rule_panel(database, tmp_path, "date:\n  start: 2018-03-04\n")
    assert panel.period_start.parts() == (2018, 3, 4)
    assert panel.period_end.parts() == (0, 0, 0)
    assert panel.period_end.year.currentText() == "--"
    _pick(panel.period_end.year, 2019)
    _pick(panel.period_end.month, 2)
    _pick(panel.period_end.day, 28)
    assert panel._period_values() == ("2018-03-04", "2019-02-28")
    assert panel.current_rule()["date"] == {"start": "2018-03-04", "end": "2019-02-28"}
    window.close()


def test_a_partly_chosen_period_covers_the_whole_year_or_month(database, tmp_path):
    """年だけ・年月までも指定できる。**始めは初日から、終わりは末日まで**（うるう年も）。"""
    window, panel, _ = _rule_panel(database, tmp_path, "count_per_year: 5\n")
    _pick(panel.period_start.year, 2018)
    _pick(panel.period_end.year, 2020)
    assert panel._period_values() == ("2018-01-01", "2020-12-31")
    _pick(panel.period_start.month, 3)
    _pick(panel.period_end.month, 2)
    assert panel._period_values() == ("2018-03-01", "2020-02-29")
    assert selection.period_problem(*panel._period_values()) is None
    window.close()


def test_the_month_and_day_wait_for_the_year_and_month(database, tmp_path):
    """年が `--` なら月は押せず、月が `--` なら日は押せない。年を `--` に戻すと下も `--` に戻る。"""
    window, panel, _ = _rule_panel(database, tmp_path, "count_per_year: 5\n")
    boxes = panel.period_start
    assert not boxes.month.isEnabled() and not boxes.day.isEnabled()
    _pick(boxes.year, 2018)
    assert boxes.month.isEnabled() and not boxes.day.isEnabled()
    _pick(boxes.month, 2)
    assert boxes.day.isEnabled()
    assert boxes.day.itemData(boxes.day.count() - 1) == 28, "実在する日だけ"
    _pick(boxes.day, 28)
    _pick(boxes.year, gui_select.DATE_UNSET)
    assert boxes.parts() == (0, 0, 0)
    assert boxes.text() == ""
    assert not boxes.month.isEnabled() and not boxes.day.isEnabled()
    window.close()


def test_moving_a_period_box_rebuilds_the_year_table(database, tmp_path):
    """欄を動かしたら年の表が作り直される（以前の入力欄の「確定したら」と同じ）。"""
    window, panel, _ = _rule_panel(database, tmp_path, "count_per_year: 5\n")
    panel.refresh_years()
    assert "2018" in _rows(panel)
    _pick(panel.period_start.year, 2019)
    assert "2018" not in _rows(panel)
    window.close()


def test_a_rule_value_with_a_time_is_kept_until_the_boxes_are_touched(database, tmp_path):
    """**時刻付きの値を黙って変えない。** 欄には日付を出し、触るまでは元の値のまま。"""
    window, panel, _ = _rule_panel(database, tmp_path, "date:\n  start: '2018-03-04T10:30:00'\n")
    boxes = panel.period_start
    assert boxes.parts() == (2018, 3, 4)
    assert boxes.text() == "2018-03-04T10:30:00"
    _pick(boxes.day, 5)
    assert boxes.text() == "2018-03-05"
    window.close()


def test_an_unreadable_period_says_how_to_clear_it(database, tmp_path):
    """読めない値の欄は `--` に見えるのに、「選ぶ」と保存が止まる。**`--` を選び直しても変わらない**ので、消し方を書く（PR #143 の指摘3）。"""
    window, panel, _ = _rule_panel(database, tmp_path, "date:\n  start: abc\n")
    assert panel.period_start.parts() == (0, 0, 0)
    assert panel.period_start.text() == "abc"
    assert "abc" in panel.period_notice.text()
    assert "年を選び直す" in panel.period_notice.text()

    _pick(panel.period_start.year, 2019)

    assert panel.period_start.text() == "2019-01-01"
    assert panel.period_notice.text() == ""
    window.close()


def test_a_period_in_the_wrong_order_does_not_talk_about_choosing_again(database, tmp_path):
    """始めが終わりより後なのは読めている値。消し方の案内は要らない。"""
    window, panel, _ = _rule_panel(database, tmp_path, "date:\n  start: 2020-01-01\n  end: 2019-01-01\n")
    assert "より後" in panel.period_notice.text()
    assert "選び直す" not in panel.period_notice.text()
    window.close()


def test_the_period_comes_from_the_rule_quoted_or_not(database, tmp_path):
    # 引用符の無い日付は YAML が date で読む
    window, panel, _ = _rule_panel(database, tmp_path, "date:\n  start: 2018-01-01\n  end: '2018-12-31'\n")
    assert panel._period_values() == ("2018-01-01", "2018-12-31")
    window.close()


def test_changing_the_period_rebuilds_the_years_and_keeps_the_counts(database, tmp_path):
    """**期間で外れた年が画面から分かる**（2026 年が表に出ない原因が見えなかった・#134）。入れた枚数は残す。"""
    window, panel, _ = _rule_panel(database, tmp_path, "count_per_year: 5\nremove_similar: false\n")
    panel.year_table.cellWidget(0, 2).setValue(9)  # 2018
    _set_period(panel, "", "2018-12-31")
    assert "2019" not in _rows(panel)
    _set_period(panel, "", "")
    assert _rows(panel)["2018"] == 9 and "2019" in _rows(panel)
    window.close()


def test_the_period_on_the_screen_narrows_the_choice(database, tmp_path):
    window, panel, _ = _rule_panel(database, tmp_path, "count_per_year: 5\nremove_similar: false\n")
    _set_period(panel, "2019-01-01", "")
    panel.choose()
    assert "2018.jpg" not in _names(panel) and len(panel.selected) == 2
    window.close()


def test_saving_writes_the_period_and_a_blank_period_drops_the_key(database, tmp_path, monkeypatch):
    """**空欄は絞り込まない**ので、キーごと消す（端だけ空なら、その端だけ消す）。"""
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
    window, panel, path = _rule_panel(database, tmp_path, "date:\n  start: 2000-01-01\n  end: 2025-12-31\n")
    _set_period(panel, "2018-01-01", "")
    panel.save_rule()
    assert selection.load_rule(str(path))["date"] == {"start": "2018-01-01"}
    _set_period(panel, "", "")
    panel.save_rule()
    assert "date" not in selection.load_rule(str(path))
    window.close()


@pytest.mark.parametrize("start, end", [("2019-13-01", ""), ("", "someday"), ("", "2019-1-1"), ("2020-01-01", "2019-12-31")])
def test_an_unreadable_period_is_shown_and_stops_choosing_and_saving(database, tmp_path, monkeypatch, start, end):
    """読めない値は `select` では指定なしになるので、**打ち間違えたまま黙って絞らずに選ばない**。"""
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: warned.append(args[2]))
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: pytest.fail("保存まで進んだ"))
    window, panel, path = _rule_panel(database, tmp_path, "count_per_year: 5\n")
    _set_period(panel, start, end)
    assert panel.period_notice.text()
    assert panel.period_start.text() == start and panel.period_end.text() == end, "読めない値を黙って消さない"
    panel.choose()
    panel.save_rule()
    assert len(warned) == 2 and not panel.selected
    assert path.read_text(encoding="utf-8") == "count_per_year: 5\n"
    window.close()


def test_the_later_row_is_gone_when_the_period_ends_before_it(database, tmp_path):
    """**利用者の報告**（2026-11-05）: 期間が 2025-12-31 までなのに「2027 年以降」と出ていた。"""
    # 候補は 2019 年まで。実データの「2025 年まで候補があり、期間も 2025 年まで」と同じ形
    window, panel, _ = _rule_panel(database, tmp_path, "date:\n  end: 2019-12-31\ncount_per_year: 5\n")
    assert not [label for label in _rows(panel) if "以降" in label]
    # 行は無くても枚数は持っている（期間を外したときのため・PR #142 の指摘1）
    assert list(panel.current_rule()["count_per_year"]) == [2018, 2019, "unknown", "2020+"]
    _set_period(panel, "", "")
    assert _rows(panel)["2020 年以降"] == 5
    window.close()


def _value(panel, label, count):
    for row in range(panel.year_table.rowCount()):
        if panel.year_table.item(row, 0).text() == label:
            panel.year_table.cellWidget(row, 2).setValue(count)


def test_counts_of_rows_hidden_by_the_period_come_back(database, tmp_path):
    """**期間で消えた行の枚数を失わない**（PR #142 のレビュー指摘1）。ファイルの値に戻っていた。"""
    window, panel, _ = _rule_panel(database, tmp_path, "count_per_year: 5\nremove_similar: false\n")
    _value(panel, "2019", 9)
    _value(panel, "2020 年以降", 7)
    _set_period(panel, "", "2018-12-31")
    assert "2019" not in _rows(panel)
    _set_period(panel, "", "")
    assert _rows(panel) == {"2018": 5, "2019": 9, "年不明": 5, "2020 年以降": 7}
    window.close()


@pytest.mark.parametrize("text, expected", [
    ("count_per_year: {2018: 11, 2019: 22, unknown: 4, 2027+: 8}\n", (11, 22, 4, 8)),
    ("count_per_year: 5\n", (5, 5, 5, 5)),
])
def test_saving_with_a_period_keeps_the_counts_of_the_other_years(database, tmp_path, monkeypatch, text, expected):
    """**狭めたまま保存しても、ほかの年の枚数を消さない**（指摘1）。消えると広げ直しても 0 で、黙って外れた。"""
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
    window, panel, path = _rule_panel(database, tmp_path, text + "remove_similar: false\n")
    _set_period(panel, "", "2018-12-31")
    panel.save_rule()
    first, second, unknown, later = expected
    assert selection.load_rule(str(path))["count_per_year"] == {
        2018: first, 2019: second, "unknown": unknown, "2020+": later
    }
    _set_period(panel, "", "")
    assert _rows(panel) == {"2018": first, "2019": second, "年不明": unknown, "2020 年以降": later}
    panel.choose()
    assert len(panel.selected) == 3, "2019 年も選ばれる"
    window.close()


def test_saving_a_rule_without_counts_under_a_period_does_not_write_zero(database, tmp_path, monkeypatch):
    """ルールに枚数が無ければ隠れた年も候補の枚数。0 と書くと、期間を外したときにその年が消える。"""
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
    window, panel, path = _rule_panel(database, tmp_path, "remove_similar: false\n")
    _set_period(panel, "", "2018-12-31")
    panel.save_rule()
    assert selection.load_rule(str(path))["count_per_year"][2019] == 2
    window.close()


def test_counts_of_rows_hidden_by_a_filter_come_back(mixed, tmp_path):
    """絞り込みで消えた行も同じ（指摘1 と同じ原因）。"""
    window, panel = _panel(mixed, tmp_path, "family_only: false\ncount_per_year: 5\n")
    panel.refresh_years()
    _value(panel, "2016", 3)
    panel.flag_checks["family_only"].setChecked(True)
    assert "2016" not in _rows(panel)
    panel.flag_checks["family_only"].setChecked(False)
    assert _rows(panel)["2016"] == 3
    window.close()


def test_opening_another_rule_forgets_the_hidden_counts(database, tmp_path):
    window, panel, _ = _rule_panel(database, tmp_path, "count_per_year: 5\n")
    _value(panel, "2019", 9)
    _set_period(panel, "", "2018-12-31")
    other = tmp_path / "other.yml"
    other.write_text("count_per_year: 2\n", encoding="utf-8")
    panel.set_paths(str(other), panel.output_root, panel.source_roots)
    assert _rows(panel)["2019"] == 2
    assert panel.current_rule()["count_per_year"][2019] == 2
    window.close()


def test_the_year_table_follows_the_period_after_an_unreadable_value(database, tmp_path):
    """読めない値の間に絞り込みを動かしても、直したら表が追いつく（PR #142 のレビュー指摘4）。"""
    window, panel, _ = _rule_panel(database, tmp_path, "count_per_year: 5\n")
    _set_period(panel, "", "2018-12-31")
    _set_period(panel, "", "bad")
    check = panel.flag_checks["include_video"]
    check.setChecked(not check.isChecked())
    assert "2019" in _rows(panel), "読めない値は絞らない扱い"
    _set_period(panel, "", "2018-12-31")
    assert "2019" not in _rows(panel), "欄の値と表が一致する"
    window.close()


def test_a_year_missing_from_the_rule_shows_zero_and_is_not_chosen(tmp_path):
    """**利用者の決定**（PR #142 の (a)）: 書いていない年は選ばない（#102）。2027+ の手前の年は 0 で見える。"""
    path = tmp_path / "gap.db"
    connection = db.ensure_database(str(path))
    for seed, year in enumerate((2025, 2026), 1):
        photo = write_photo(tmp_path / f"{year}.jpg", scene(seed))
        _add(connection, photo, shooting_date=f"{year}-05-03T10:00:00", faces=[_face("${PERSON_4}")])
    connection.close()
    window, panel, _ = _rule_panel(
        path, tmp_path, "date: {end: 2025-12-31}\ncount_per_year: {2025: 30, unknown: 5, 2027+: 30}\nremove_similar: false\n"
    )
    assert _rows(panel) == {"2025": 30, "年不明": 5}
    _set_period(panel, "", "")
    assert _rows(panel) == {"2025": 30, "2026": 0, "年不明": 5, "2027 年以降": 30}
    panel.choose()
    assert _names(panel) == ["2025.jpg"], "CLI と同じ"
    _value(panel, "2026", 4)
    panel.choose()
    assert "2026.jpg" in _names(panel)
    window.close()


def test_the_unknown_row_stays_under_a_period(database, tmp_path):
    """**利用者の決定**（PR #142 の (b)）: 候補 0 と見えれば、年不明が期間で外れたと画面から分かる。"""
    window, panel, _ = _rule_panel(database, tmp_path, "count_per_year: 5\n")
    _set_period(panel, "", "2018-12-31")
    assert _rows(panel)["年不明"] == 5
    row = next(r for r in range(panel.year_table.rowCount()) if panel.year_table.item(r, 0).text() == "年不明")
    assert panel.year_table.item(row, 1).text() == "0"
    window.close()


def test_a_period_with_a_time_zone_is_refused():
    """写真の撮影日時と比べられず `TypeError` になっていた（PR #142 のレビュー指摘5）。"""
    assert selection.period_problem("", "2025-12-31T00:00:00+09:00")
    assert selection.period_problem("2025-01-01T00:00:00+09:00", "2025-12-31")
    assert selection.period_problem("2025-01-01", "2025-12-31T23:59:59") is None


def test_the_later_row_starts_right_after_the_last_candidate_year():
    """どの年も行から漏れない。以前は「今年の翌年」固定で、今年に候補が無いと今年がどの行にも入らなかった。"""
    assert gui_select.later_row_year([2018, 2025], None) == 2026
    assert gui_select.later_row_year([2018, 2026], None) == 2027
    assert gui_select.later_row_year([2018, 2025], 2025) is None
    assert gui_select.later_row_year([2018, 2025], 2026) == 2026
    assert gui_select.later_row_year([], None) == gui_select.date.today().year


def test_the_later_count_in_the_rule_follows_the_row_when_its_year_moves(database, tmp_path):
    """ファイルが ``2027+`` でも行が「2020 年以降」なら同じ枚数を引き継ぐ（0 に落とすと保存で消える）。"""
    window, panel, _ = _rule_panel(database, tmp_path, "count_per_year:\n  2018: 1\n  2019: 1\n  2027+: 8\n")
    assert _rows(panel)["2020 年以降"] == 8
    assert panel.current_rule()["count_per_year"]["2020+"] == 8
    window.close()


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


def test_selecting_a_photo_shows_it_large_on_the_right(window, tmp_path):
    """サムネイルを選ぶと、右に写真を大きく出す（#113・利用者の指定）。ページを替えたら消す。"""
    panel = window.selection
    panel.choose()
    assert panel.preview.pixmap().isNull()

    panel.result_list.setCurrentRow(0)
    assert not panel.preview.pixmap().isNull()
    assert panel.preview.text() == ""

    panel._show_page()
    assert panel.preview.pixmap().isNull()
    assert panel.preview.text() == gui_select.PREVIEW_PROMPT


def test_the_preview_is_read_from_the_photo_not_the_small_exif_thumbnail(tmp_path):
    """プレビューは元写真から読む（埋め込みのサムネイルは粗い）。読めなければ None。"""
    photo = write_photo(tmp_path / "big.jpg", scene(1))
    image = gui_select.photo_preview(str(photo), size=100)
    assert image is not None and max(image.size) == 100
    assert gui_select.photo_preview(str(tmp_path / "missing.jpg")) is None


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
    panel.refresh_years()
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
    panel.refresh_years()
    panel.fill_spin.setValue(1)
    panel.fill_all_years()
    before = panel.current_rule()["count_per_year"]
    panel.flag_checks["include_auto_assigned"].setChecked(True)
    panel.choose()
    after = panel.current_rule()["count_per_year"]
    assert all(after[year] == 1 for year in before)
    window.close()


def test_reloading_clears_the_previous_results(mixed, tmp_path, monkeypatch):
    """**割り当てが変わったら前の結果を捨てる**（指摘2）。古い結果をクリックすると例外、出力は古いまま
    コピーしていた。#113 で「候補を読み込む」を無くしたので、出力の前に割り当ての変化を見る。"""
    window, panel = _panel(mixed, tmp_path, "remove_similar: false\n")
    panel.choose()
    assert panel.result_list.count() > 0 or panel.year_range.old_year.count() > 0
    # 選んだあとに割り当てを変えた（1枚目のタブでの操作と同じ）。
    face_id = panel.connection.execute("SELECT id FROM Face WHERE person_id IS NOT NULL").fetchone()[0]
    db.unassign_faces(panel.connection, [face_id])
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args: shown.append(args[2]))
    panel.export()
    assert panel.result_list.count() == 0 and panel.selected == []
    assert shown and gui_select.CHOOSE_LABEL in shown[0]
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
    """「〜年以降」の行は、表に無い新しい年の枚数。"""
    later = 2027
    limit = selection.year_limits({"count_per_year": {2019: 1, f"{later}+": 3, "unknown": 2}})
    assert (limit(2019), limit(2020), limit(later), limit(later + 5), limit(None)) == (1, 0, 3, 3, 2)


def test_every_weight_has_a_field_on_the_screen(window):
    """重みを足したら画面にも出す（#106 の5つ・#139 の大きさ・他人・比重）。"""
    assert sorted(key for key, _, _ in gui_select.WEIGHT_FIELDS) == sorted(scoring.Weights._fields)
    assert set(window.selection.weight_spins) == set(scoring.Weights._fields)
    assert [key for key, _ in gui_select.BREAKDOWN_LABELS] == list(scoring.BREAKDOWN_KEYS)


def test_the_breakdown_shows_the_measured_values_or_says_they_are_missing():
    """重みが 0 でも測った値は見せる（重みを決める手がかり）。測っていなければそう書く。"""
    parts = {key: 0.0 for key, _ in gui_select.BREAKDOWN_LABELS}
    parts["total"] = 0.0
    measured = {"technical": 72.34, "aesthetic": 5.123, "highlight_clip": 0.031, "shadow_clip": 0.0}
    text = gui_select.format_breakdown(parts, measured)
    assert "画質 72.3" in text and "美しさ 5.12" in text and "白飛び 3.1%" in text
    assert "photoarchive assess" in gui_select.format_breakdown(parts)


def test_choosing_reloads_the_candidates_only_when_the_assignments_changed(window, monkeypatch):
    """「選ぶ」は、割り当てが変わったときだけ候補を用意し直す（#113）。重みを動かすだけなら読み直さない。"""
    panel = window.selection
    loads = []
    original = selection.prepare
    monkeypatch.setattr(selection, "prepare", lambda *a, **k: loads.append(1) or original(*a, **k))

    panel.choose()
    panel.weight_spins["smile"].setValue(2.0)
    panel.choose()
    assert len(loads) == 1, "重みだけなら読み直さない"

    face_id = panel.connection.execute("SELECT id FROM Face WHERE person_id IS NOT NULL").fetchone()[0]
    db.reject_faces(panel.connection, [face_id])
    panel.choose()
    assert len(loads) == 2, "割り当てが変わったら読み直す"


def test_the_year_counts_from_the_database_match_the_prepared_candidates(mixed):
    """DB だけで数えた年ごとの枚数が、候補を用意して数えたものと同じ（#113）。"""
    connection = db.ensure_database(str(mixed))
    for rule in ({}, {"family_only": False}, {"include_auto_assigned": False}):
        candidates = selection.prepare(connection, selection.family_sources(rule))
        assert selection.year_counts(connection, rule) == selection.candidate_counts(candidates, rule)
    connection.close()


def test_the_results_can_be_narrowed_to_a_range_of_years(mixed, tmp_path):
    """結果は年の範囲で絞る（#113・#134）。**年不明は範囲と関係なくチェックで決める**（#18）。既定は入れて最後に並べる。"""
    window, panel = _panel(mixed, tmp_path, "family_only: false\nremove_similar: false\n")
    panel.choose()
    shown = lambda: [panel.result_list.item(i).text() for i in range(panel.result_list.count())]
    box = panel.year_range.old_year
    assert [box.itemData(i) for i in range(box.count())] == [None, 2016, 2017, 2019]
    assert box.itemText(1) == "2016", "欄は数字だけ（利用者の要望）"
    assert len(shown()) == 4, "初めは全部の年"
    assert shown()[-1] == "年不明 undated.jpg"

    panel.year_range.set_range("2016", "2017")
    assert shown() == ["2016 nofamily2016.jpg", "2017 auto2017.jpg", "年不明 undated.jpg"], "範囲を指定しても入る"
    panel.year_range.set_range("2019", None)
    assert shown() == ["2019 dated.jpg", "年不明 undated.jpg"]

    panel.include_unknown.setChecked(False)
    assert shown() == ["2019 dated.jpg"]
    assert panel.year_range.isEnabled(), "範囲と同時に使える"
    panel.year_range.set_range(None, None)
    assert len(shown()) == 3 and "年不明 undated.jpg" not in shown(), "範囲が無くても外せる"

    selected = len(panel.selected)
    panel.choose()
    assert not panel.include_unknown.isChecked(), "選び直しても外したまま"
    assert len(panel.selected) == selected, "見せ方だけで、選ぶ写真は変わらない"
    window.close()


@pytest.mark.parametrize(
    "date",
    ["", "date:\n  end: 2017-12-31\n"],
    ids=["with-later-row", "period-without-later-row"],
)
def test_the_unknown_row_is_always_the_last_row(mixed, tmp_path, date):
    """**年の表の「年不明」は必ず出て、常に一番下**（#18・利用者の指定）。「〇年以降」より下。"""
    window, panel = _panel(mixed, tmp_path, f"family_only: false\n{date}")
    panel.refresh_years()
    labels = [panel.year_table.item(r, 0).text() for r in range(panel.year_table.rowCount())]
    assert labels[-1] == "年不明"
    assert labels.count("年不明") == 1
    assert any("年以降" in label for label in labels) == (date == "")
    window.close()


def _set_result_order(panel, value):
    panel.order_box.setCurrentIndex(panel.order_box.findData(value))


def _shown_results(panel):
    return [panel.result_list.item(i).text() for i in range(panel.result_list.count())]


def test_the_results_can_be_ordered_by_date_and_points(mixed, tmp_path):
    """**結果にも `並び` がある**（#18・利用者の要望）。既定はいままでの順。年不明は日時の順では最後、点の順では点のとおり。"""
    window, panel = _panel(mixed, tmp_path, "family_only: false\nremove_similar: false\n")
    panel.choose()
    assert panel.order_box.currentData() == gui_select.ORDER_YEAR
    assert _shown_results(panel) == [
        "2016 nofamily2016.jpg", "2017 auto2017.jpg", "2019 dated.jpg", "年不明 undated.jpg"
    ]

    _set_result_order(panel, gui_select.ORDER_TAKEN_NEW)
    assert _shown_results(panel) == [
        "2019 dated.jpg", "2017 auto2017.jpg", "2016 nofamily2016.jpg", "年不明 undated.jpg"
    ]
    _set_result_order(panel, gui_select.ORDER_TAKEN_OLD)
    assert _shown_results(panel)[0] == "2016 nofamily2016.jpg"
    assert _shown_results(panel)[-1] == "年不明 undated.jpg"

    scores = {"dated.jpg": 10.0, "auto2017.jpg": 40.0, "nofamily2016.jpg": 20.0, "undated.jpg": 30.0}
    for media in panel.selected:
        media["family_score"] = scores[Path(media["path"]).name]
    _set_result_order(panel, gui_select.ORDER_POINTS_HIGH)
    assert _shown_results(panel) == [
        "2017 auto2017.jpg 40点", "年不明 undated.jpg 30点", "2016 nofamily2016.jpg 20点", "2019 dated.jpg 10点"
    ]
    _set_result_order(panel, gui_select.ORDER_POINTS_LOW)
    assert _shown_results(panel)[0] == "2019 dated.jpg 10点"
    window.close()


def test_the_result_order_changes_only_the_view(mixed, tmp_path):
    """**見せ方だけ。** 選んだ写真の順（出力の順位）は変えない。絞り込みと併せて効き、選び直しても保つ。"""
    window, panel = _panel(mixed, tmp_path, "family_only: false\nremove_similar: false\n")
    panel.choose()
    chosen = [media["id"] for media in panel.selected]
    panel.page = 0

    _set_result_order(panel, gui_select.ORDER_TAKEN_NEW)
    assert [media["id"] for media in panel.selected] == chosen
    panel.include_unknown.setChecked(False)
    panel.year_range.set_range("2016", "2017")
    assert _shown_results(panel) == ["2017 auto2017.jpg", "2016 nofamily2016.jpg"]

    panel.choose()
    assert panel.order_box.currentData() == gui_select.ORDER_TAKEN_NEW
    window.close()


def test_a_new_result_order_goes_back_to_the_first_page(mixed, tmp_path, monkeypatch):
    monkeypatch.setattr(gui_select, "RESULT_PAGE_SIZE", 2)
    window, panel = _panel(mixed, tmp_path, "family_only: false\nremove_similar: false\n")
    panel.choose()
    panel._turn(1)
    assert panel.page == 1
    _set_result_order(panel, gui_select.ORDER_TAKEN_NEW)
    assert panel.page == 0
    window.close()


def test_the_results_can_be_narrowed_by_month(mixed, tmp_path):
    """**年と月を別の欄で選ぶ**（#134・利用者の要望）。どの写真も 5 月なので、4 月までなら 2019 年は外れる。"""
    window, panel = _panel(mixed, tmp_path, "family_only: false\nremove_similar: false\n")
    panel.choose()
    shown = lambda: [panel.result_list.item(i).text() for i in range(panel.result_list.count())]
    panel.include_unknown.setChecked(False)
    panel.year_range.set_range("2017-05", "2019")
    assert shown() == ["2017 auto2017.jpg", "2019 dated.jpg"]
    panel.year_range.old_year.setCurrentIndex(panel.year_range.old_year.findData(2019))
    assert shown() == ["2019 dated.jpg"], "欄を動かしても一覧が追いつく"
    window.close()


def test_a_photo_known_only_by_year_stays_out_of_a_narrower_month_range():
    """月まで分からない写真は、撮影時期がまるごと入るときだけ（顔の割り当てタブと同じ）。"""
    from datetime import datetime

    whole_year = (datetime(2012, 1, 1), datetime(2012, 12, 31))
    assert gui_select._period_within(whole_year, "2012-01", "2012-12")
    assert not gui_select._period_within(whole_year, "2012-05", "2012-12")
    assert not gui_select._period_within(None, "2012-01", None)


def test_each_weight_has_a_gauge_that_moves_its_value_by_the_step(window):
    """**横ゲージで重みを動かせる**（#120・利用者の要望）。ゲージは刻みの目盛りで入力欄に写る。"""
    panel = window.selection
    assert set(panel.weight_rows) == set(scoring.Weights._fields)
    assert set(panel.number_rows) == set(selection.RULE_NUMBERS)
    technical = panel.weight_rows["technical"]
    technical.slider.setValue(14)
    assert panel.weight_spins["technical"].value() == pytest.approx(0.70)
    assert panel.current_rule()["weights"]["technical"] == pytest.approx(0.70)
    panel.weight_rows["face_ratio"].slider.setValue(35)
    assert panel.current_rule()["weights"]["face_ratio"] == pytest.approx(35.0)
    panel.weight_rows["strangers"].slider.setValue(15)
    assert panel.current_rule()["weights"]["strangers"] == pytest.approx(1.5)
    panel.number_rows["copy_distance"].slider.setValue(12)
    assert panel.current_rule()["copy_distance"] == 12


def test_a_value_off_the_gauge_is_kept_as_typed(window):
    """**ゲージは値の上限でも目盛りでもない。** 入力欄の 3.0 や 0.33 を丸めずにルールへ（PR #108 の指摘3）。"""
    panel = window.selection
    row = panel.weight_rows["smile"]
    panel.weight_spins["smile"].setValue(3.0)
    assert row.slider.value() == row.slider.maximum(), "ゲージは右端に止まる"
    assert panel.current_rule()["weights"]["smile"] == pytest.approx(3.0)
    panel.weight_spins["smile"].setValue(0.33)
    assert panel.current_rule()["weights"]["smile"] == pytest.approx(0.33), "目盛りに丸めない"
    panel.number_spins["similar_seconds"].setValue(600)
    assert panel.current_rule()["similar_seconds"] == 600


def test_the_settings_column_fits_without_a_sideways_scroll(window):
    """長い項目名は折り返す。**押し広げると「既定」ボタンが切れて、横スクロールが出ていた**（#18 で見つけた）。"""
    from PySide6.QtWidgets import QScrollArea

    panel = window.selection
    window.resize(1240, 820)
    window.tabs.setCurrentWidget(panel)
    window.show()
    QApplication.processEvents()
    assert panel.findChild(QScrollArea).horizontalScrollBar().maximum() == 0


def test_each_gauge_marks_its_default_value(window):
    """**ゲージの既定値の位置に印が出る**（#18）。値を動かしても印は動かない。"""
    panel = window.selection
    window.tabs.setCurrentWidget(panel)
    window.show()
    QApplication.processEvents()
    rows = list(panel.weight_rows.values()) + list(panel.number_rows.values())
    assert rows
    for row in rows:
        slider = row.slider
        assert slider.mark == round(row.default / row.step)
        if slider.mark > slider.maximum():
            assert slider.mark_x() is None
            continue
        x = slider.mark_x()
        assert 0 <= x < slider.width()
        slider.setValue(slider.maximum() if slider.mark < slider.maximum() / 2 else 0)
        QApplication.processEvents()
        assert slider.mark_x() == x, "取っ手を動かしても印は動かない"


def test_the_mark_is_at_the_right_place_and_is_drawn(window):
    panel = window.selection
    window.tabs.setCurrentWidget(panel)
    window.show()
    QApplication.processEvents()
    slider = panel.weight_rows["technical"].slider
    slider.setValue(slider.maximum())  # 取っ手を印から離す
    QApplication.processEvents()

    slider.mark = 0
    left = slider.mark_x()
    slider.mark = slider.maximum()
    right = slider.mark_x()
    slider.mark = slider.maximum() // 2
    middle = slider.mark_x()
    assert left < middle < right
    assert abs(middle - (left + right) / 2) <= 1

    drawn = slider.grab().toImage()
    slider.mark = slider.maximum() + 1  # 印を消す
    bare = slider.grab().toImage()
    assert slider.mark_x() is None
    assert drawn.pixelColor(middle, 1) != bare.pixelColor(middle, 1), "描いた位置の色が変わる"


def test_the_default_button_puts_back_only_its_own_value(window):
    """**項目ごとに1クリックで既定へ戻す**（#120）。既定のときは押せない。ほかの項目には触らない。"""
    panel = window.selection
    defaults = scoring.Weights.default()
    technical, aesthetic = panel.weight_rows["technical"], panel.weight_rows["aesthetic"]
    assert not technical.reset_button.isEnabled(), "既定のままなら押せない"
    panel.weight_spins["technical"].setValue(0.9)
    panel.weight_spins["aesthetic"].setValue(0.6)
    assert technical.reset_button.isEnabled()
    technical.reset_button.click()
    assert panel.weight_spins["technical"].value() == pytest.approx(defaults.technical)
    assert technical.slider.value() == round(defaults.technical / 0.05)
    assert not technical.reset_button.isEnabled()
    assert panel.weight_spins["aesthetic"].value() == pytest.approx(0.6), "ほかの項目はそのまま"

    distance = panel.number_rows["similar_distance"]
    panel.number_spins["similar_distance"].setValue(30)
    distance.reset_button.click()
    assert panel.number_spins["similar_distance"].value() == selection.RULE_NUMBERS["similar_distance"]


def test_the_quality_weights_name_their_models():
    """画質と美しさがどちらのモデルかを画面で分かるように（#120。MUSIQ と NIMA は別々の重み）。"""
    labels = {key: label for key, label, _ in gui_select.WEIGHT_FIELDS}
    assert "MUSIQ" in labels["technical"] and "NIMA" in labels["aesthetic"]


def _wheel(widget, notches=-1):
    """フォーカスに関係なく、ウィジェットの真ん中へホイールを送る（下向きが負）。"""
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent

    centre = QPointF(widget.width() / 2, widget.height() / 2)
    event = QWheelEvent(centre, QPointF(widget.mapToGlobal(centre.toPoint())), QPoint(0, 0),
                        QPoint(0, 120 * notches), Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase,
                        False)
    QApplication.sendEvent(widget, event)
    return event


def _settings_inputs(panel):
    """設定欄にある、ホイールで値の動く部品すべて（年の表の欄も）。"""
    from PySide6.QtWidgets import QAbstractSlider, QAbstractSpinBox, QComboBox, QScrollArea

    inner = panel.findChild(QScrollArea).widget()
    found = []
    for kind in (QAbstractSpinBox, QAbstractSlider, QComboBox):
        # 表やスクロール領域が持つスクロールバーも QAbstractSlider だが、値の入力欄ではない
        found += [w for w in inner.findChildren(kind) if type(w).__name__ != "QScrollBar"]
    return found


def _input_value(widget):
    return widget.currentIndex() if hasattr(widget, "currentIndex") else widget.value()


@pytest.mark.parametrize("focused", [False, True], ids=["pointer-only", "after-clicking"])
def test_the_wheel_never_changes_a_value_in_the_settings_column(window, focused):
    """**設定欄をスクロールしても、マウスの下の値が変わらない**（#18・PR #122 のレビュー指摘1）。

    利用者「スクロールしていると、たまたまポインタが、どこかしらの入力欄に重なって、その値が
    スクロールされたものと認識されて変わってしまう」。年の表の欄は素の `QSpinBox` のままで、
    それ以外もクリックしたあとはホイールで動いていた。**全部の部品を見るので、足した欄も見張られる。**
    """
    panel = window.selection
    window.tabs.setCurrentWidget(panel)
    window.show()
    panel.refresh_years()
    QApplication.processEvents()
    inputs = _settings_inputs(panel)
    assert panel.year_table.rowCount() > 0
    assert panel.year_table.cellWidget(0, 2) in inputs
    assert panel.fill_spin in inputs and panel.layout_combo in inputs

    for widget in inputs:
        # 真ん中の値に置く（端の値だと、向きによっては元から動かず、テストが素通りする）
        if hasattr(widget, "currentIndex"):
            widget.setCurrentIndex(0)
        else:
            widget.setValue(type(widget.value())((widget.minimum() + widget.maximum()) / 2))
    for widget in inputs:
        if focused:
            widget.setFocus()
            QApplication.processEvents()
        before = _input_value(widget)
        event = _wheel(widget)
        assert _input_value(widget) == before, f"{type(widget).__name__} の値がホイールで動いた"
        # 受けなければ Qt が親へ送り、スクロールになる（テストから送るイベントは親へ伝わらないので、受けないことを見る）
        # 押せない欄（年が -- のときの月など）には Qt がホイールを渡さず、受けた印も変わらない
        assert not event.isAccepted() or not widget.isEnabled(), f"{type(widget).__name__} がホイールを受けた"


def test_the_sharpness_gauge_still_takes_the_wheel_after_clicking(window):
    """顔の割り当てタブの鮮明さは**スクロール領域の外**で、絞り込みをホイールで調整できる（#135）。設定欄とは扱いが違う。"""
    faces = window.assignment
    window.tabs.setCurrentWidget(faces)
    window.show()
    faces.blur_check.setChecked(True)
    faces.blur_spin.setValue(30)
    faces.blur_check.setFocus()
    QApplication.processEvents()

    _wheel(faces.blur_slider)
    assert faces.blur_spin.value() == 30, "上を通っただけでは動かない"

    faces.blur_slider.setFocus()
    QApplication.processEvents()
    assert faces.blur_slider.hasFocus()
    _wheel(faces.blur_slider)
    assert faces.blur_spin.value() != 30, "選んだゲージは動く"


def test_the_panel_says_which_old_weight_keys_were_skipped(database, tmp_path, monkeypatch):
    """**GUI は端末を見ずに起動されることがある**（PR #140 の指摘3）。読み飛ばしたキーを画面に出し、保存したら消す。"""
    rule = tmp_path / "old.yml"
    rule.write_text("weights:\n  stranger_penalty: 6\n  smile: 0.4\n", encoding="utf-8")
    window = gui.AppWindow(str(database), rule_path=str(rule), output_root=str(tmp_path / "out"))
    panel = window.selection
    assert "stranger_penalty" in panel.retired_notice.text()
    assert panel.weight_spins["strangers"].value() == scoring.WEIGHT_STRANGERS

    panel.choose()  # 「選ぶ」で `status` が書き換わっても消えない
    assert "stranger_penalty" in panel.retired_notice.text()

    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
    panel.save_rule()
    assert panel.retired_notice.text() == ""
    assert selection.retired_weight_keys(selection.load_rule(str(rule))) == []
    window.close()
