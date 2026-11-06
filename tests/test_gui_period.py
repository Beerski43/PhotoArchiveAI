"""撮影時期の範囲を年と月の欄で選ぶ部品（#134）。両方のタブで使う。"""

import os

import pytest
from PySide6.QtWidgets import QApplication, QLabel

from photoarchive_ai import gui_period

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def picker():
    picker = gui_period.YearMonthRange()
    picker.set_months(["2015-08", "2015-11", "2018-02", "2018-07", "2020-01"])
    return picker


def _items(box):
    return [box.itemText(i) for i in range(box.count())]


def _pick(box, value):
    index = box.findData(value)
    assert index >= 0, value
    box.setCurrentIndex(index)


def test_the_years_and_months_are_separate_fields_listed_oldest_first(picker):
    """**利用者の要望**: [年]と[月]で入力欄を分ける。"""
    assert _items(picker.old_year) == [gui_period.ANY, "2015", "2018", "2020"]
    assert not picker.old_month.isEnabled(), "年を選ぶまで月は選べない"
    _pick(picker.old_year, 2018)
    assert picker.old_month.isEnabled()
    assert _items(picker.old_month) == [gui_period.ANY, "2", "7"], "その年にある月だけ"


def test_an_arrow_points_from_the_old_side_to_the_new_side(picker):
    """**利用者の要望**: 「～ではなく→にするだけで良い」。"""
    texts = [widget.text() for widget in picker.children() if isinstance(widget, QLabel)]
    assert texts == ["年", "月", "→", "年", "月"]


def test_the_fields_hold_only_digits_and_dashes_mean_no_limit(picker):
    """**利用者の要望**: 「数字だけで良い」。指定なしは `--`。長い文字はその幅まで欄を広げる。"""
    picker.set_range("2018-07", "2020-01")
    for box in (picker.old_year, picker.old_month, picker.new_year, picker.new_month):
        assert _items(box)[0] == "--"
        assert all(text.isdigit() for text in _items(box)[1:])


def test_a_blank_month_means_the_whole_year_and_a_blank_year_means_no_limit(picker):
    assert picker.bounds() == (None, None)
    _pick(picker.old_year, 2015)
    assert picker.bounds() == ("2015-01", None), "1月から"
    _pick(picker.new_year, 2018)
    assert picker.bounds() == ("2015-01", "2018-12"), "12月まで"
    _pick(picker.old_month, 8)
    _pick(picker.new_month, 7)
    assert picker.bounds() == ("2015-08", "2018-07")


def test_the_new_side_offers_only_what_comes_after_the_old_side(picker):
    """**利用者の要望**（2026-11-05）: 左の年月を入れたら、右の年月入力はそれ以降になるように。"""
    _pick(picker.old_year, 2018)
    assert _items(picker.new_year) == [gui_period.ANY, "2018", "2020"]
    _pick(picker.new_year, 2018)
    _pick(picker.old_month, 7)
    assert _items(picker.new_month) == [gui_period.ANY, "7"], "同じ年なら古い方の月以降"
    _pick(picker.new_year, 2020)
    assert _items(picker.new_month) == [gui_period.ANY, "1"], "後の年なら月は絞らない"
    picker.old_year.setCurrentIndex(0)
    assert _items(picker.new_year) == [gui_period.ANY, "2015", "2018", "2020"], "古い方を外せば全部に戻る"


def test_moving_the_old_side_past_the_new_side_pushes_the_new_side_up(picker):
    """逆転したまま0件を見せない。指定なしにも戻さない（狭めた範囲が黙って広がる）。"""
    picker.set_range("2015-08", "2015-11")
    _pick(picker.old_year, 2018)
    assert picker.new_year.currentData() == 2018
    picker.set_range("2018-02", "2018-02")
    _pick(picker.old_month, 7)
    assert picker.bounds() == ("2018-07", "2018-07")


def test_a_blank_new_side_stays_blank_when_the_old_side_moves(picker):
    _pick(picker.old_year, 2018)
    assert picker.bounds() == ("2018-01", None)


def test_the_choice_survives_new_months_and_goes_blank_when_its_year_is_gone(picker):
    """`scan` のあとに月が増えても選び直しを強いない。"""
    picker.set_range("2018-07", "2020-01")
    picker.set_months(["2018-07", "2020-01", "2021-03"])
    assert picker.bounds() == ("2018-07", "2020-01")
    picker.set_months(["2020-01"])
    assert picker.bounds() == (None, "2020-01")


def test_a_range_that_is_not_on_offer_is_refused(picker):
    with pytest.raises(ValueError):
        picker.set_range("1999", None)
    with pytest.raises(ValueError):
        picker.set_range("2018-03", None)


def test_changed_fires_once_per_move_and_not_on_a_refill(picker):
    fired = []
    picker.changed.connect(lambda: fired.append(1))
    picker.set_months(["2015-08", "2018-02"])
    assert not fired
    _pick(picker.old_year, 2018)
    assert len(fired) == 1
    picker.set_range(None, None)
    assert len(fired) == 2


def test_a_year_without_a_known_month_is_still_listed(picker):
    """写真の選定タブ: 年までしか分からない写真の年は、月が無くても年の欄に並ぶ。"""
    picker.set_months(["2018-07"], [2012, 2018])
    assert _items(picker.old_year) == [gui_period.ANY, "2012", "2018"]
    _pick(picker.old_year, 2012)
    assert _items(picker.old_month) == [gui_period.ANY]
