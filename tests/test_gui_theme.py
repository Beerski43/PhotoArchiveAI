"""GUI の見た目（`gui_theme`・#18）。"""

import os
import re
import tomllib
from pathlib import Path

import pytest
from PySide6.QtCore import qInstallMessageHandler
from PySide6.QtGui import QImageReader
from PySide6.QtWidgets import QApplication, QScrollArea

from photoarchive_ai import db, gui, gui_theme

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
SOURCE = Path(gui_theme.__file__).parent
REPOSITORY = SOURCE.parent.parent


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture()
def themed(qt_app):
    """テーマをかける。**ほかのテストへ持ち越さない**（アプリ全体の設定なので）。"""
    style, palette, sheet = qt_app.style().objectName(), qt_app.palette(), qt_app.styleSheet()
    gui_theme.apply(qt_app)
    yield qt_app
    qt_app.setStyleSheet(sheet)
    qt_app.setPalette(palette)
    if style:
        qt_app.setStyle(style)


@pytest.fixture()
def window(themed, tmp_path):
    path = tmp_path / "theme.db"
    db.ensure_database(str(path)).close()
    window = gui.AppWindow(str(path), output_root=str(tmp_path / "out"))
    yield window
    window.close()


def test_every_name_in_the_stylesheet_is_filled_in():
    text = gui_theme.stylesheet()
    assert "@" not in text, "置き換え忘れの色の名前があると、Qt は黙ってその行を捨てる"


def test_the_arrow_images_exist_and_can_be_read():
    """**QSS で枠を指定すると標準の矢印が消える。** 画像が読めないと、数値欄と選択欄の矢印が無くなる。"""
    assert b"svg" in QImageReader.supportedImageFormats()
    for path in re.findall(r"url\(([^)]+)\)", gui_theme.stylesheet()):
        assert Path(path).is_file(), path
        assert not QImageReader(path).read().isNull(), path


def test_the_arrow_images_are_packaged():
    """入れ忘れると、`pip install -e .` では動くのに、インストールした先で矢印が消える。"""
    with open(REPOSITORY / "pyproject.toml", "rb") as file:
        data = tomllib.load(file)["tool"]["setuptools"]["package-data"]["photoarchive_ai"]
    assert "assets/*.svg" in data
    assert list((SOURCE / "assets").glob("*.svg"))


def test_applying_the_theme_gives_no_stylesheet_warnings(themed, tmp_path):
    """Qt は QSS の誤りを警告だけで読み飛ばす。全部の画面を出して警告が無いことを見る。"""
    messages = []
    previous = qInstallMessageHandler(lambda _kind, _context, text: messages.append(text))
    try:
        path = tmp_path / "theme.db"
        db.ensure_database(str(path)).close()
        window = gui.AppWindow(str(path), output_root=str(tmp_path / "out"))
        window.show()
        window.tabs.setCurrentWidget(window.selection)
        themed.processEvents()
        window.close()
    finally:
        qInstallMessageHandler(previous)
    assert not [text for text in messages if "style sheet" in text.lower() or "parse" in text.lower()]


def test_the_theme_applies_the_palette_and_the_stylesheet(themed):
    assert themed.palette().color(themed.palette().ColorRole.Highlight).name().upper() == gui_theme.COLORS["accent"]
    assert themed.styleSheet() == gui_theme.stylesheet()


def test_the_settings_column_still_fits_under_the_theme(window, themed):
    """テーマで余白が増えても、設定欄に横スクロールが出ない。"""
    window.resize(1240, 820)
    window.tabs.setCurrentWidget(window.selection)
    window.show()
    themed.processEvents()
    assert window.selection.findChild(QScrollArea).horizontalScrollBar().maximum() == 0


def test_the_default_mark_stays_in_place_under_the_theme(window, themed):
    """QSS をかけるとゲージの取っ手の大きさが変わる。印の位置は取っ手の中心の動く範囲に合う。"""
    window.tabs.setCurrentWidget(window.selection)
    window.show()
    themed.processEvents()
    slider = window.selection.weight_rows["technical"].slider
    slider.setValue(slider.maximum())
    themed.processEvents()
    slider.mark = 0
    left = slider.mark_x()
    slider.mark = slider.maximum()
    right = slider.mark_x()
    assert 0 <= left < right < slider.width()
    slider.mark = slider.maximum() // 2
    middle = slider.mark_x()
    drawn = slider.grab().toImage()
    slider.mark = slider.maximum() + 1
    bare = slider.grab().toImage()
    assert drawn.pixelColor(middle, 1) != bare.pixelColor(middle, 1)


@pytest.mark.parametrize("name", sorted(path.name for path in SOURCE.glob("gui*.py") if path.name != "gui_theme.py"))
def test_screens_do_not_carry_their_own_colors(name):
    """**色は `gui_theme` にだけ持つ。** 画面のコードに書くと、テーマを変えたときにそこだけ古い色で残る。"""
    text = (SOURCE / name).read_text(encoding="utf-8")
    # `#110` のような Issue 番号と紛れないよう、3桁は英字を含むものだけ。数字だけの色は下の setStyleSheet の検査が見る
    assert not re.findall(r"#[0-9a-fA-F]{6}\b|#(?=\d*[a-fA-F])[0-9a-fA-F]{3}\b|rgba?\(", text)
    sheets = re.findall(r"setStyleSheet\(([^)]*)\)", text)
    assert not [sheet for sheet in sheets if re.search(r"color|background|border", sheet)], sheets


def test_every_look_used_by_the_screens_is_defined():
    """`setProperty("look", ...)` の名前を QSS が知らないと、黙って飾りが付かない。"""
    used = set()
    for path in SOURCE.glob("gui*.py"):
        used |= set(re.findall(r'setProperty\("look", "(\w+)"\)', path.read_text(encoding="utf-8")))
    assert used
    for look in used:
        assert f'[look="{look}"]' in gui_theme.stylesheet(), look
