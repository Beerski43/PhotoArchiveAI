"""GUI の見た目（#18・利用者「全体的な見た目を今どきのモダンなデザインに」）。

**色はここの `COLORS` にだけ持つ。** 画面のコードに色を書かない。枠や薄い文字が要る部品は
`setProperty("look", ...)` で役目を付け、見た目はこの module の QSS で決める。

- Fusion スタイルの上に、パレットと QSS を重ねる（OS の見た目に左右されず、Windows でも同じに見える）
- QSS で枠を指定すると、数値欄と選択欄の標準の矢印が消える。矢印は `assets/` の SVG を使う
- ふだんの字の種類と大きさは OS に任せる（環境ごとに日本語の字が読めるものが違う）
"""

from pathlib import Path

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

ASSETS = Path(__file__).resolve().parent / "assets"

COLORS = {
    "window": "#F3F4F7",
    "surface": "#FFFFFF",
    "surface_alt": "#F8F9FB",
    "border": "#DDE0E7",
    "border_strong": "#C3C8D3",
    "text": "#1E2330",
    "muted": "#646B7A",
    "disabled": "#A3A9B5",
    "disabled_bg": "#EEF0F3",
    "accent": "#4A63E0",
    "accent_hover": "#3D54C8",
    "accent_pressed": "#3346AA",
    "accent_soft": "#E6EAFC",
    "accent_soft_hover": "#F0F2FD",
    "ok_text": "#14663A",
    "ok_bg": "#E3F5EA",
    "ok_border": "#2E9E61",
    "track": "#D9DDE5",
}

#: QSS の雛形。``@名前`` を `COLORS` の値に置き換える（``{}`` と紛れないように ``format`` を使わない）。
_STYLESHEET = """
QMenuBar {
    background: @surface;
    border-bottom: 1px solid @border;
    padding: 2px 6px;
}
QMenuBar::item { background: transparent; padding: 5px 10px; border-radius: 6px; }
QMenuBar::item:selected { background: @accent_soft; color: @accent_pressed; }
QMenuBar::item:disabled { color: @disabled; }
QMenu {
    background: @surface;
    border: 1px solid @border;
    border-radius: 8px;
    padding: 4px;
}
QMenu::item { padding: 6px 22px 6px 12px; border-radius: 6px; }
QMenu::item:selected { background: @accent_soft; color: @accent_pressed; }
QMenu::item:disabled { color: @disabled; }
QMenu::separator { height: 1px; background: @border; margin: 4px 8px; }

QTabWidget::pane { border: none; border-top: 1px solid @border; top: -1px; }
QTabBar { qproperty-drawBase: 0; }
QTabBar::tab {
    background: transparent;
    color: @muted;
    padding: 8px 18px;
    border: none;
    border-bottom: 2px solid transparent;
}
QTabBar::tab:hover { color: @text; }
QTabBar::tab:selected { color: @accent; border-bottom: 2px solid @accent; font-weight: 600; }

QPushButton {
    background: @surface;
    color: @text;
    border: 1px solid @border_strong;
    border-radius: 6px;
    padding: 5px 12px;
}
QPushButton:hover { background: @accent_soft_hover; border-color: @accent; }
QPushButton:pressed { background: @accent_soft; }
QPushButton:focus { border-color: @accent; }
QPushButton:disabled { background: @disabled_bg; color: @disabled; border-color: @border; }
QPushButton[look="primary"] {
    background: @accent;
    color: #FFFFFF;
    border: 1px solid @accent;
    font-weight: 600;
    padding: 7px 14px;
}
QPushButton[look="primary"]:hover { background: @accent_hover; border-color: @accent_hover; }
QPushButton[look="primary"]:pressed { background: @accent_pressed; border-color: @accent_pressed; }
QPushButton[look="primary"]:disabled { background: @disabled_bg; color: @disabled; border-color: @border; }

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background: @surface;
    color: @text;
    border: 1px solid @border_strong;
    border-radius: 6px;
    padding: 3px 6px;
    selection-background-color: @accent;
    selection-color: #FFFFFF;
}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    border: 1px solid @accent;
}
QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover, QLineEdit:hover { border-color: @accent; }
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {
    background: @disabled_bg;
    color: @disabled;
    border-color: @border;
}
QComboBox { padding-right: 20px; }
QComboBox::drop-down { border: none; width: 20px; }
QComboBox::down-arrow { image: url(@asset_down); width: 12px; height: 12px; }
QComboBox QAbstractItemView {
    background: @surface;
    border: 1px solid @border;
    outline: 0;
    selection-background-color: @accent_soft;
    selection-color: @accent_pressed;
}
QSpinBox, QDoubleSpinBox { padding-right: 18px; }
QSpinBox::up-button, QDoubleSpinBox::up-button {
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 18px;
    border: none;
    background: transparent;
}
QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 18px;
    border: none;
    background: transparent;
}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover { background: @accent_soft; }
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow { image: url(@asset_up); width: 10px; height: 10px; }
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow { image: url(@asset_down); width: 10px; height: 10px; }

QSlider::groove:horizontal { height: 4px; background: @track; border-radius: 2px; }
QSlider::sub-page:horizontal { background: @accent; border-radius: 2px; }
QSlider::sub-page:horizontal:disabled { background: @track; }
QSlider::handle:horizontal {
    width: 14px;
    height: 14px;
    margin: -6px 0;
    background: @surface;
    border: 2px solid @accent;
    border-radius: 7px;
}
QSlider::handle:horizontal:hover { background: @accent_soft; }
QSlider::handle:horizontal:disabled { border-color: @border_strong; }

QGroupBox {
    background: @surface;
    border: 1px solid @border;
    border-radius: 10px;
    margin-top: 22px;
    padding: 10px 8px 8px 8px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 4px;
    padding: 0 4px 3px 4px;
    color: @muted;
    font-weight: 600;
}

QListWidget, QTableWidget, QTreeWidget {
    background: @surface;
    border: 1px solid @border;
    border-radius: 8px;
    outline: 0;
    gridline-color: @border;
}
QListWidget::item, QTableWidget::item { border-radius: 6px; color: @text; }
QListWidget::item:hover { background: @accent_soft_hover; }
QListWidget::item:selected, QTableWidget::item:selected { background: @accent_soft; color: @accent_pressed; }
QHeaderView::section {
    background: @surface_alt;
    color: @muted;
    border: none;
    border-bottom: 1px solid @border;
    padding: 5px 6px;
    font-weight: 600;
}

QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: @border_strong; border-radius: 3px; min-height: 28px; }
QScrollBar::handle:horizontal { background: @border_strong; border-radius: 3px; min-width: 28px; }
QScrollBar::handle:hover { background: @muted; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QSplitter::handle { background: transparent; }
QSplitter::handle:horizontal { width: 8px; }
QSplitter::handle:vertical { height: 8px; }

QProgressBar {
    background: @track;
    border: none;
    border-radius: 5px;
    text-align: center;
    color: @text;
    min-height: 10px;
}
QProgressBar::chunk { background: @accent; border-radius: 5px; }

QToolTip {
    background: @text;
    color: #FFFFFF;
    border: none;
    padding: 4px 6px;
}

QLabel[look="muted"] { color: @muted; }
QLabel[look="panel"] {
    background: @surface;
    border: 1px solid @border;
    border-radius: 8px;
    padding: 8px;
}
QLabel[look="toast"] {
    padding: 10px 18px;
    font-size: 16px;
    font-weight: 600;
    color: @ok_text;
    background: @ok_bg;
    border: 1px solid @ok_border;
    border-radius: 8px;
}
"""


def stylesheet() -> str:
    """色と画像を埋めた QSS。"""
    text = _STYLESHEET
    text = text.replace("@asset_down", (ASSETS / "chevron-down.svg").as_posix())
    text = text.replace("@asset_up", (ASSETS / "chevron-up.svg").as_posix())
    # 長い名前から置き換える（@accent が @accent_soft の先頭を食わないように）
    for name in sorted(COLORS, key=len, reverse=True):
        text = text.replace(f"@{name}", COLORS[name])
    return text


def palette() -> QPalette:
    """QSS の届かないところ（窓の地・チェックボックス・文字の既定の色）。"""
    colors = QPalette()
    for role, name in (
        (QPalette.Window, "window"),
        (QPalette.WindowText, "text"),
        (QPalette.Base, "surface"),
        (QPalette.AlternateBase, "surface_alt"),
        (QPalette.Text, "text"),
        (QPalette.Button, "surface"),
        (QPalette.ButtonText, "text"),
        (QPalette.ToolTipBase, "text"),
        (QPalette.Highlight, "accent"),
        (QPalette.PlaceholderText, "disabled"),
        (QPalette.Mid, "border_strong"),
    ):
        colors.setColor(role, QColor(COLORS[name]))
    colors.setColor(QPalette.ToolTipText, QColor("#FFFFFF"))
    colors.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        colors.setColor(QPalette.Disabled, role, QColor(COLORS["disabled"]))
    return colors


def apply(app: QApplication) -> None:
    """アプリ全体に見た目をかける。窓を作る前に呼ぶ。"""
    app.setStyle("Fusion")
    app.setPalette(palette())
    app.setStyleSheet(stylesheet())
