"""GUI の見た目（`gui_theme`・#18）。"""

import gc
import os
import re
import tomllib
from pathlib import Path

import pytest
import yaml
from PySide6.QtCore import QCoreApplication, QEvent, Qt, qInstallMessageHandler
from PySide6.QtGui import QImageReader, QPalette
from PySide6.QtWidgets import QApplication, QMessageBox, QScrollArea

from photoarchive_ai import config, db, gui, gui_theme

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
SOURCE = Path(gui_theme.__file__).parent
REPOSITORY = SOURCE.parent.parent


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def settings_file(tmp_path, monkeypatch):
    """**表示モードは設定ファイルに書く。** カレントごと tmp_path へ移す（実データの設定に書かない）。"""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work / "config" / "app_settings.yml"


@pytest.fixture()
def themed(qt_app):
    """テーマをかける。**ほかのテストへ持ち越さない**（アプリ全体の設定なので）。"""
    # **先に流れたテストの窓を片付ける。** close だけでは残り、QSS をかけるたびに全部の部品を描き直すので、
    # 回帰テストの順で流すと1回に 90 秒以上かかって止まったように見えた（実物の窓1枚なら一瞬）
    for widget in QApplication.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    gc.collect()
    palette, sheet = qt_app.palette(), qt_app.styleSheet()
    gui_theme.apply(qt_app, "light")
    yield qt_app
    qt_app.setStyleSheet(sheet)
    qt_app.setPalette(palette)


@pytest.fixture()
def window(themed, tmp_path):
    path = tmp_path / "theme.db"
    db.ensure_database(str(path)).close()
    window = gui.AppWindow(str(path), output_root=str(tmp_path / "out"))
    yield window
    window.close()


@pytest.mark.parametrize("name", sorted(gui_theme.THEMES))
def test_every_name_in_the_stylesheet_is_filled_in(name):
    text = gui_theme.stylesheet(name)
    assert "@" not in text, "置き換え忘れの色の名前があると、Qt は黙ってその行を捨てる"


def test_both_themes_name_the_same_colors():
    """片方にだけ足すと、もう片方で `@名前` のまま残る。"""
    assert set(gui_theme.LIGHT) == set(gui_theme.DARK)


def _luminance(color):
    channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first, second):
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("name", sorted(gui_theme.THEMES))
@pytest.mark.parametrize(
    "foreground, background",
    [
        ("text", "window"),
        ("text", "surface"),
        ("muted", "window"),
        ("muted", "surface"),
        ("on_accent", "accent"),
        ("accent_text", "accent_soft"),
        ("ok_text", "ok_bg"),
        ("tooltip_text", "tooltip_bg"),
    ],
)
def test_the_text_is_readable_in_both_themes(name, foreground, background):
    """文字と地のコントラストが 4.5 以上（WCAG AA）。配色を直したときに読めなくしない。"""
    colors = gui_theme.THEMES[name]
    assert _contrast(colors[foreground], colors[background]) >= 4.5


@pytest.mark.parametrize("name", sorted(gui_theme.THEMES))
def test_the_arrow_images_exist_and_can_be_read(name):
    """**QSS で枠を指定すると標準の矢印が消える。** 画像が読めないと、数値欄と選択欄の矢印が無くなる。"""
    assert b"svg" in QImageReader.supportedImageFormats()
    paths = re.findall(r"url\(([^)]+)\)", gui_theme.stylesheet(name))
    assert paths
    for path in paths:
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
    assert themed.palette().color(themed.palette().ColorRole.Highlight).name().upper() == gui_theme.LIGHT["accent"]
    assert themed.styleSheet() == gui_theme.stylesheet("light")


def _text_color(app):
    return app.palette().color(QPalette.WindowText).name().upper()


def test_switching_the_mode_takes_effect_at_once_and_is_remembered(window, themed, settings_file):
    """**再起動なしで切り替わる。** 次に起動したときも同じ（設定ファイルに残す・ほかのキーは残す）。"""
    settings_file.parent.mkdir()
    settings_file.write_text("output_root: /keep\n", encoding="utf-8")
    assert window.theme_actions["auto"].isChecked()

    window.theme_actions["dark"].trigger()

    assert themed.styleSheet() == gui_theme.stylesheet("dark")
    assert _text_color(themed) == gui_theme.DARK["text"]
    assert window.theme_actions["dark"].isChecked() and not window.theme_actions["auto"].isChecked()
    assert yaml.safe_load(settings_file.read_text(encoding="utf-8")) == {"output_root": "/keep", "theme": "dark"}
    assert config.get_theme(config.load_settings()) == "dark"

    window.theme_actions["light"].trigger()
    assert themed.styleSheet() == gui_theme.stylesheet("light")
    assert _text_color(themed) == gui_theme.LIGHT["text"]


def test_switching_back_and_forth_with_the_windows_open_does_not_crash(window, themed):
    """**QSS をかけたあとに窓を開いたまま setStyle を呼ぶと Qt ごと落ちる。** `apply` は最初の1回しか呼ばない。"""
    window.resize(1240, 820)
    window.show()
    window.tabs.setCurrentWidget(window.selection)
    themed.processEvents()
    for mode in ("dark", "light", "dark", "auto", "light"):
        window.set_theme(mode)
        themed.processEvents()
        assert not window.grab().toImage().isNull()


def test_the_window_opens_with_the_mode_from_the_settings(themed, tmp_path):
    path = tmp_path / "theme.db"
    db.ensure_database(str(path)).close()
    window = gui.AppWindow(str(path), output_root=str(tmp_path / "out"), theme_mode="dark")
    assert window.theme_actions["dark"].isChecked()
    window.close()


def test_choosing_the_same_mode_does_not_rewrite_the_settings(window, settings_file):
    window.set_theme("auto")
    assert not settings_file.exists()


def test_a_settings_file_that_cannot_be_written_still_switches_the_mode(window, themed, settings_file, monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: warnings.append(args[2]))
    settings_file.parent.mkdir()
    settings_file.write_text("theme: [\n", encoding="utf-8")

    window.set_theme("dark")

    assert themed.styleSheet() == gui_theme.stylesheet("dark")
    assert len(warnings) == 1
    assert settings_file.read_text(encoding="utf-8") == "theme: [\n", "壊れた設定は上書きしない"


def test_auto_follows_the_operating_system(qt_app, monkeypatch):
    """`auto` は OS の明暗に合わせる。OS が教えないときは明るい方。`light` と `dark` は OS を見ない。"""

    class Hints:
        scheme = Qt.ColorScheme.Dark

        def colorScheme(self):  # noqa: N802（Qt の名前）
            return self.scheme

    class App:
        hints = Hints()

        def styleHints(self):  # noqa: N802（Qt の名前）
            return self.hints

    app = App()
    assert gui_theme.resolve("auto", app) == "dark"
    assert gui_theme.resolve("light", app) == "light"
    Hints.scheme = Qt.ColorScheme.Light
    assert gui_theme.resolve("auto", app) == "light"
    assert gui_theme.resolve("dark", app) == "dark"
    Hints.scheme = Qt.ColorScheme.Unknown
    assert gui_theme.resolve("auto", app) == "light"


def test_when_the_os_changes_only_auto_follows(window, themed, monkeypatch):
    calls = []
    monkeypatch.setattr(gui_theme, "apply", lambda _app, mode="auto": calls.append(mode))
    window._color_scheme_changed()
    assert calls == ["auto"]
    window.theme_mode = "dark"
    window._color_scheme_changed()
    assert calls == ["auto"], "選んだモードは OS で変えない"


@pytest.mark.parametrize("value, expected", [(None, "auto"), ("dark", "dark"), ("light", "light"), ("auto", "auto")])
def test_the_theme_setting_is_read(value, expected):
    settings = {} if value is None else {"theme": value}
    assert config.get_theme(settings) == expected


def test_an_unreadable_theme_setting_warns_and_uses_auto(caplog):
    """見た目のことで起動を止めない。"""
    assert config.get_theme({"theme": "blue"}) == "auto"
    assert "theme" in caplog.text


@pytest.mark.parametrize("name", sorted(gui_theme.THEMES))
def test_each_theme_renders_with_its_own_window_color(themed, tmp_path, name):
    """実際に描いて、窓の地が各モードの色になる（パレットが画面に届いている）。"""
    gui_theme.apply(themed, name)
    path = tmp_path / "theme.db"
    db.ensure_database(str(path)).close()
    window = gui.AppWindow(str(path), output_root=str(tmp_path / "out"))
    window.resize(1240, 820)
    window.show()
    themed.processEvents()
    image = window.grab().toImage()
    window.close()
    assert image.pixelColor(image.width() - 3, image.height() - 3).name().upper() == gui_theme.THEMES[name]["window"]


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
