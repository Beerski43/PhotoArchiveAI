"""GUI の「写真の選定」タブ（#104）。`select` の重みを動かして、結果をサムネイルで見る。

**なぜ要るか**（#102）。`select` の結果に利用者が満足していない。並びの式と重みが
コードに固定されていて、好みを確かめるたびに Issue・PR・実データの複製での測定が
要っていた。**重みを利用者が動かして、その場で結果を見る**ための画面。

動き方は `selection` の2段に合わせる（#103）。

- 年ごとの表は、タブを開いたときに DB だけから作る（`selection.year_counts`・測らない）。
  **最初から枚数を入れられる**（#113・利用者「[候補を読み込む]ボタンの意味が分からない。
  最初から年毎の選ぶ枚数を入れれるようにしてほしい」）
- 「選ぶ」= `selection.choose`（重みで点を出し、並べ、束ね、年ごとに取る。
  実データで約1秒。重みを変えた直後は、新しく上位に来た写真の見た目を NFS から測る）。
  候補（`selection.prepare`・見え方・表情の未計測分を測る。重い）は、**まだ無いときと、
  割り当てなどが変わったとき（`db.selection_state`）だけ**その前に用意し直す。以前は
  「候補を読み込む」ボタンを利用者が押していた

**値を1つ動かすたびに自動で並べ直さない。** 数秒かかることがあり、続けて動かすと
重なるため。ボタンで並べ直す。

設定の初期値はルールファイル（`config/rule.yml`）から読み、「ルールを保存」で書き戻す。
CLI の `photoarchive select` は同じルールで同じ結果になる。
"""

from __future__ import annotations

import shutil
import calendar
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import yaml
from PIL import Image, ImageOps
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QImage, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStyle,
    QStyleOptionSlider,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import config, db, scoring, selection, similar
from .gui_period import YearMonthRange

#: 結果の1ページの枚数。**ページャの無い一覧を作らない**（CLAUDE.md §8）。
RESULT_PAGE_SIZE = 60
#: 結果のサムネイルの大きさ（長辺）。
RESULT_THUMBNAIL = 180
#: 右のプレビューに読む大きさ（長辺・#113）。表示は枠に合わせて縮める。
PREVIEW_SIZE = 1200
PREVIEW_PROMPT = "写真を選ぶと、ここに大きく出ます"

#: 重みの項目と、画面に出す名前・刻み（`scoring.Weights`・#139）。比重を先頭に、顔・他人・写真全体の順。
#: 顔の項目どうし・写真全体の項目どうしは**比**で効く（和で割る）。
WEIGHT_FIELDS = (
    ("face_ratio", "比重: 顔の側（家族の加点 − 他人の減点）の割合 %。残りが写真全体", 1.0),
    ("extra_person", "2人目以降の効き（1 で全員を同じだけ足す・0 でいちばん良い1人だけ）", 0.05),
    ("sharpness", "顔: 鮮明さ（ボケていない）", 0.05),
    ("frontal", "顔: 正面を向いている", 0.05),
    ("eyes", "顔: 目を開けている", 0.05),
    ("smile", "顔: 笑顔", 0.05),
    ("size", "顔: 大きさ（写真でいちばん大きい顔と比べて）", 0.05),
    ("cutoff", "顔: 見切れていない（写真の縁で切れていない）", 0.05),
    ("strangers", "他人(減点)の顔の点にかける重み（顔認証外にした顔のうち選んだ顔）", 0.1),
    # 写真全体（#106・`photoarchive assess` で測る）。**MUSIQ と NIMA は別々の重み**で、どちらに寄せるかは
    # この2つの比で決まる（#120・利用者の要望）
    ("technical", "写真全体: 画質（ぶれ・ピンぼけ・暗さ・MUSIQ）", 0.05),
    ("aesthetic", "写真全体: 美しさ（構図・NIMA）", 0.05),
    ("highlights", "写真全体: 白飛びしていない", 0.05),
    ("shadows", "写真全体: 黒つぶれしていない", 0.05),
)
#: 横ゲージの右端（#120）。**ゲージは入力欄の写しで、値の上限ではない**（右端より大きい値も入力欄で入れられ、
#: ゲージは右端に止まるだけ）。項目の比（刻み 0.05）は 1、他人の重み（刻み 0.1）は 3、比重（刻み 1）は 100。
WEIGHT_SLIDER_MAXIMUM = {0.05: 1.0, 0.1: 3.0, 1.0: 100.0}
#: 重みの入力の桁と上限。**ルールの値を丸めない・頭打ちにしない**（PR #108 のレビュー指摘3。
#: 画面の値が CLI と黙ってずれ、保存すると丸めた値が残っていた）。`load_rule` は 0 以上なら受け付ける。
WEIGHT_DECIMALS = 4
WEIGHT_MAXIMUM = 1_000_000.0
#: 上限のある重み。
WEIGHT_LIMITS = {
    "face_ratio": selection.FACE_RATIO_MAXIMUM,
    "extra_person": selection.EXTRA_PERSON_MAXIMUM,
}

#: 出力の形（`selection.OUTPUT_LAYOUTS`）と、画面に出す名前。確認の窓の文でも使う（#134）。
LAYOUT_LABELS = {
    selection.OUTPUT_LAYOUT_YEAR: "年ごとのフォルダ",
    selection.OUTPUT_LAYOUT_FLAT: "出力先の直下（平らに）",
}

#: 真偽値のキーと、画面に出す名前。既定は `selection.RULE_FLAGS`。
FLAG_FIELDS = (
    ("family_only", "家族の写った写真だけ"),
    ("include_auto_assigned", "自動割り当ての顔も家族として数える"),
    ("remove_similar", "似た写真・写しを1枚にまとめる"),
    ("remove_duplicate", "同じファイル（ハッシュ）を1枚にまとめる"),
    ("include_video", "動画を含める"),
)

#: 似た写真の判定の数（`selection.RULE_NUMBERS`）と、画面に出す名前・入力の上限・横ゲージの右端。
NUMBER_FIELDS = (
    ("similar_distance", "似た写真の見た目の距離（連写）", 64, 64),
    ("similar_far_distance", "似た写真の見た目の距離（時刻の離れた2枚）", 64, 64),
    ("copy_distance", "写しとみなす見た目の距離", 64, 64),
    ("similar_seconds", "連写とみなす撮影の間隔（秒）", 3600, 120),
)
#: 各行の「既定に戻す」ボタンの文字（#120）。
DEFAULT_LABEL = "既定"

#: 点の内訳の項目と名前（`scoring.photo_breakdown`・#139）。顔の項目は家族と親しい人の顔の和、
#: 比重をかけたあとの点。
BREAKDOWN_LABELS = (
    ("sharpness", "顔: 鮮明さ"),
    ("frontal", "顔: 正面"),
    ("eyes", "顔: 目"),
    ("smile", "顔: 笑顔"),
    ("size", "顔: 大きさ"),
    ("cutoff", "顔: 見切れていない"),
    ("strangers", "他人(減点)の顔"),
    ("technical", "写真全体: 画質"),
    ("aesthetic", "写真全体: 美しさ"),
    ("highlights", "写真全体: 白飛びしていない"),
    ("shadows", "写真全体: 黒つぶれしていない"),
)

YEAR_UNKNOWN = "年不明"
#: 年の表の「年以降」の行の印（`selection.year_limits` の ``2027+``）。
ROW_LATER = "later"
#: ルールの「〜年以降」の枚数を、行の年に関わらず聞くための遠い先の年（4桁の上限）。
FAR_FUTURE_YEAR = 9999
CHOOSE_LABEL = "選ぶ"


def later_row_year(years: Iterable[int], period_end: Optional[int]) -> Optional[int]:
    """「〜年以降」の行の年。**期間の終わりがその年より前なら None（行を出さない）**（#134）。

    始まりは候補のある最後の年の翌年（候補が無ければ今年）。表の行の続きなので、どの年も行から漏れない。
    以前は「今年の翌年」に固定で、期間が 2025 年までなのに「2027 年以降」と出て、今年に候補が無ければ
    今年がどの行にも入らなかった。``period_end`` は期間の終わりの年。
    """
    first = max(years, default=date.today().year - 1) + 1
    return None if period_end is not None and period_end < first else first


def _initial_count(key: Any, candidates: Optional[int], limit_of) -> int:
    """年の表の行に初めて入れる枚数。ルールファイルから。ファイルに枚数が無ければ候補の枚数。

    「〜年以降」は、行の年が動いても（候補が増減した）ファイルの枚数を引き継ぐ。
    """
    if limit_of is None:
        return candidates or 0
    year = None if key == selection.COUNT_UNKNOWN else (FAR_FUTURE_YEAR if key == ROW_LATER else key)
    return limit_of(year) or 0


def year_label(year: Optional[int]) -> str:
    return YEAR_UNKNOWN if year is None else str(year)


def photo_thumbnail(path: str, size: int = RESULT_THUMBNAIL) -> Optional[Image.Image]:
    """写真のサムネイル。**EXIF の埋め込みがあれば先頭だけ読む**（NFS・`similar.exif_thumbnail`）。

    無ければ元写真を縮小デコードする。向きは元写真の EXIF に合わせる（埋め込みの
    サムネイルは回っていない）。読めなければ None。
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(similar.HEAD_BYTES)
        image = similar.exif_thumbnail(head)
        if image is not None:
            image = _orient(image, _orientation(head))
        else:
            with Image.open(path) as original:
                original.draft("RGB", (size * 2, size * 2))
                image = ImageOps.exif_transpose(original).convert("RGB")
        image = image.convert("RGB")
        image.thumbnail((size, size))
        return image
    except Exception:  # 壊れた・消えた・NFS が読めない
        return None


def photo_preview(path: str, size: int = PREVIEW_SIZE) -> Optional[Image.Image]:
    """右のプレビュー用に、元写真を縮小デコードする（#113）。読めなければ None。

    **EXIF の埋め込みサムネイルは使わない**（160px 前後で、大きく出すと粗い）。
    `draft` で JPEG を縮小しながら読むので、元の画素数ぶんは展開しない。
    """
    try:
        with Image.open(path) as original:
            original.draft("RGB", (size, size))
            image = ImageOps.exif_transpose(original).convert("RGB")
        image.thumbnail((size, size))
        return image
    except Exception:  # 壊れた・消えた・NFS が読めない
        return None


def _orientation(head: bytes) -> int:
    raw = similar._exif_segment(head)
    if raw is None:
        return 1
    try:
        exif = Image.Exif()
        exif.load(raw)
        return int(exif.get(0x0112, 1))
    except Exception:
        return 1


def _orient(image: Image.Image, orientation: int) -> Image.Image:
    method = {
        2: Image.Transpose.FLIP_LEFT_RIGHT,
        3: Image.Transpose.ROTATE_180,
        4: Image.Transpose.FLIP_TOP_BOTTOM,
        5: Image.Transpose.TRANSPOSE,
        6: Image.Transpose.ROTATE_270,
        7: Image.Transpose.TRANSVERSE,
        8: Image.Transpose.ROTATE_90,
    }.get(orientation)
    return image if method is None else image.transpose(method)


def _pixmap(image: Image.Image) -> QPixmap:
    data = image.tobytes("raw", "RGB")
    qimage = QImage(data, image.width, image.height, image.width * 3, QImage.Format_RGB888)
    return QPixmap.fromImage(qimage.copy())


def format_breakdown(parts: Dict[str, float], measured: Optional[Dict[str, Any]] = None) -> str:
    """点の内訳を1行ずつの文字に。``measured`` は写真の測定値（`db.photo_quality` の1行・#106）。

    重みが 0 でも測った値は見せる（重みを決める手がかりにする）。測っていなければそう書く。
    """
    lines = [f"点 {parts['total']:.1f}"]
    for key, label in BREAKDOWN_LABELS:
        lines.append(f"  {label}: {parts[key] + 0.0:+.1f}")  # -0.0 を出さない
    if measured:
        lines.append(
            f"測定値: 画質 {measured['technical']:.1f}・美しさ {measured['aesthetic']:.2f}"
            f"・白飛び {measured['highlight_clip']:.1%}・黒つぶれ {measured['shadow_clip']:.1%}"
        )
    else:
        lines.append("測定値: まだ測っていません（photoarchive assess）")
    return "\n".join(lines)


class _Progress:
    """`selection` の進み具合（``(済んだ数, 総数か None, 名前)``）を窓に出す。

    **短い作業では何も出さない**（`gui.WorkProgress` と同じ考え）。総数の分からない段
    （写しを比べるために元写真を読む）は件数だけを出す。
    """

    def __init__(self, parent, label: str):
        self.label = label
        self.dialog = QProgressDialog(label, "", 0, 0, parent)
        self.dialog.setWindowTitle("処理中")
        self.dialog.setCancelButton(None)
        self.dialog.setWindowModality(Qt.WindowModal)
        self.dialog.setMinimumDuration(400)

    def __call__(self, done: int, total: Optional[int], detail: str = "") -> None:
        if total:
            self.dialog.setMaximum(total)
            self.dialog.setValue(min(done, total))
            self.dialog.setLabelText(f"{self.label}（{done} / {total}）\n{detail}")
        else:
            self.dialog.setMaximum(0)
            self.dialog.setLabelText(f"{self.label}（{done} 件）\n{detail}")
        QApplication.processEvents()

    def finish(self) -> None:
        self.dialog.close()


#: 結果の一覧の並び（#18）。先頭が既定で、いままでの順（年の古い順・年の中は点の高い順）。
ORDER_YEAR = "year"
ORDER_TAKEN_NEW = "taken_new"
ORDER_TAKEN_OLD = "taken_old"
ORDER_POINTS_HIGH = "points_high"
ORDER_POINTS_LOW = "points_low"
RESULT_ORDERS = (
    ("年の古い順", ORDER_YEAR),
    ("撮影日時の新しい順", ORDER_TAKEN_NEW),
    ("撮影日時の古い順", ORDER_TAKEN_OLD),
    ("点の高い順", ORDER_POINTS_HIGH),
    ("点の低い順", ORDER_POINTS_LOW),
)

#: ルールのファイルを選ぶ窓の絞り込みと、読み書きする拡張子（YAML だけ・#27）。先頭は拡張子を足すときに使う。
RULE_FILE_FILTER = "YAML (*.yml *.yaml)"
RULE_FILE_SUFFIXES = (".yml", ".yaml")


def _number_text(value: float) -> str:
    """既定値の見せ方（``0.35`` / ``20`` / ``0``）。"""
    return f"{value:.4f}".rstrip("0").rstrip(".")


class _WheelOnlyWhenFocused:
    """**フォーカスの無いときはホイールを受けず、親（設定欄のスクロール）へ渡す**（PR #122 のレビュー指摘1）。

    Qt のゲージと入力欄はフォーカスが無くてもホイールで値を動かす。設定欄は `QScrollArea` の中にあり、
    ゲージが行の幅の大半を取るので、ホイールで下へ送るつもりがマウスの下の重みを黙って変えていた
    （「選ぶ」まで結果に出ず、そのまま「ルールを保存」すると残る）。クリックかタブで選んだときだけ動かす。
    """

    def _init_wheel_guard(self) -> None:
        self.setFocusPolicy(Qt.StrongFocus)  # ホイールでフォーカスを取らない

    def wheelEvent(self, event) -> None:  # noqa: N802（Qt の名前）
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class WheelSafeSlider(_WheelOnlyWhenFocused, QSlider):
    def __init__(self, *args):
        super().__init__(*args)
        self._init_wheel_guard()


class WheelSafeSpinBox(_WheelOnlyWhenFocused, QSpinBox):
    def __init__(self, *args):
        super().__init__(*args)
        self._init_wheel_guard()


class _NoWheel(_WheelOnlyWhenFocused):
    """**ホイールを一切受けず、設定欄のスクロールへ渡す**（#18）。**設定欄の入力部品はすべてこれを使う。**

    `_WheelOnlyWhenFocused` だけでは足りない。ゲージや欄をクリックしたあと、そのままスクロールすると
    クリックした欄の値が動く（利用者「スクロールしていると、たまたまポインタが、どこかしらの入力欄に
    重なって、その値がスクロールされたものと認識されて変わってしまう」）。値を変えるのは、
    ドラッグ・打鍵・上下キー・欄の矢印。
    """

    def wheelEvent(self, event) -> None:  # noqa: N802（Qt の名前）
        event.ignore()


class NoWheelSlider(_NoWheel, QSlider):
    def __init__(self, *args):
        super().__init__(*args)
        self._init_wheel_guard()


class NoWheelDoubleSpinBox(_NoWheel, QDoubleSpinBox):
    def __init__(self, *args):
        super().__init__(*args)
        self._init_wheel_guard()


class NoWheelSpinBox(_NoWheel, QSpinBox):
    def __init__(self, *args):
        super().__init__(*args)
        self._init_wheel_guard()


class NoWheelComboBox(_NoWheel, QComboBox):
    def __init__(self, *args):
        super().__init__(*args)
        self._init_wheel_guard()


#: 年月日の欄で「指定なし」を表す値（`--` の値）。
DATE_UNSET = 0
#: 年の欄のいちばん古い年。
DATE_FIRST_YEAR = 1900
#: 年が `--` のときに日の数を決める年（うるう年。2月29日を選べるように）。
_LEAP_YEAR = 2000


class DateBoxes(QWidget):
    """``[年]年 [月]月 [日]日`` のコンボボックス（#18・利用者の指定）。人物の誕生日と撮影時期で共通。

    年は新しい順。**日の欄には、選んだ年月に実在する日だけ**を並べる。無くなった日は `--` に戻す
    （末日へ丸めると違う日になる）。ホイールでは変わらない。

    - ``cascade``: 年が `--` なら月を、月が `--` なら日を押せなくする（撮影時期。年だけ・年月までの指定ができる）
    - ``end``: `text` で足りない部分を埋める向き。False は始め（1月1日）、True は終わり（12月31日・月末）
    - **`setText` で入れた値は、欄を触るまでそのまま `text` で返す**（ルールの時刻付きの値や、読めない値を黙って
      変えない。読めない値は欄が `--` のまま `selection.period_problem` が止める）
    """

    #: 利用者が欄を動かしたとき。`setText` と `set_parts` では出さない。
    changed = Signal()

    def __init__(self, cascade: bool = False, end: bool = False, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.cascade = cascade
        self.end = end
        self._raw: Optional[str] = None
        self.year, self.month, self.day = NoWheelComboBox(), NoWheelComboBox(), NoWheelComboBox()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for box, unit in ((self.year, "年"), (self.month, "月"), (self.day, "日")):
            box.setMaxVisibleItems(15)
            layout.addWidget(box)
            layout.addWidget(QLabel(unit))
        layout.addStretch(1)
        self.set_parts(DATE_UNSET, DATE_UNSET, DATE_UNSET)
        for box in (self.year, self.month, self.day):
            box.currentIndexChanged.connect(self._moved)

    @staticmethod
    def _fill(box: QComboBox, values: Iterable[int], keep: int) -> None:
        """`--` と ``values`` を並べて ``keep`` を選ぶ。``keep`` が無ければ `--`。"""
        blocked = box.blockSignals(True)
        box.clear()
        box.addItem("--", DATE_UNSET)
        for value in values:
            box.addItem(str(value), value)
        box.setCurrentIndex(max(0, box.findData(keep)))
        box.blockSignals(blocked)

    def _fill_day(self, keep: int) -> None:
        year, month = self.year.currentData(), self.month.currentData()
        last = 31 if not month else calendar.monthrange(year or _LEAP_YEAR, month)[1]
        self._fill(self.day, range(1, last + 1), keep)

    def _sync_enabled(self) -> None:
        if self.cascade:
            self.month.setEnabled(bool(self.year.currentData()))
            self.day.setEnabled(bool(self.month.currentData()))

    def set_parts(self, year: int, month: int, day: int) -> None:
        """欄に入れる（`DATE_UNSET` は `--`）。範囲の外の年も選択肢に足す（開いて保存しただけで消えないように）。"""
        self._raw = None
        years = set(range(DATE_FIRST_YEAR, date.today().year + 1)) | ({year} - {DATE_UNSET})
        self._fill(self.year, sorted(years, reverse=True), year)
        self._fill(self.month, range(1, 13), month)
        self._fill_day(day)
        self._sync_enabled()

    def parts(self) -> tuple:
        return self.year.currentData(), self.month.currentData(), self.day.currentData()

    def setText(self, value: Any) -> None:  # noqa: N802（QLineEdit と同じ名前）
        """ルールの値を入れる。読めれば欄に出す。**`YYYY-MM-DD` そのものでなければ、値を持っておく。**"""
        text = "" if value is None else str(value).strip()
        parsed = selection._parse_date(text) if text else None
        if parsed is None:
            self.set_parts(DATE_UNSET, DATE_UNSET, DATE_UNSET)
        else:
            self.set_parts(parsed.year, parsed.month, parsed.day)
        self._raw = text if text and text != self._filled_text() else None

    def text(self) -> str:
        """ルールに書く値（`YYYY-MM-DD`）。年が `--` なら空。足りない部分は ``end`` の向きで埋める。"""
        return self._raw if self._raw is not None else self._filled_text()

    def holds_rule_value(self) -> bool:
        """欄に出せないルールの値を、そのまま持っているか（欄を触るまで）。"""
        return self._raw is not None

    def _filled_text(self) -> str:
        year, month, day = self.parts()
        if not year:
            return ""
        if not month:
            month, day = (12, 31) if self.end else (1, 1)
        elif not day:
            day = calendar.monthrange(year, month)[1] if self.end else 1
        return f"{year:04d}-{month:02d}-{day:02d}"

    def _moved(self, *_args) -> None:
        self._raw = None
        if self.cascade and not self.year.currentData():
            self._fill(self.month, range(1, 13), DATE_UNSET)
        keep = self.day.currentData() if not self.cascade or self.month.currentData() else DATE_UNSET
        self._fill_day(keep)
        self._sync_enabled()
        self.changed.emit()


class DefaultMarkSlider(NoWheelSlider):
    """既定値の位置に縦の印を描くゲージ（#18・利用者の要望）。

    `setTickPosition` の目盛りは等間隔にしか出せないので、自分で描く。印は取っ手の下に描く
    （既定値に合っているときは取っ手に隠れ、上下だけが見える）。
    """

    def __init__(self, orientation, mark: int):
        super().__init__(orientation)
        #: 印を描く位置（ゲージの目盛り）。ゲージの右端より大きい既定値なら描かない。
        self.mark = mark

    def mark_x(self) -> Optional[int]:
        """印の横位置（ピクセル）。範囲の外なら None。"""
        if not self.minimum() <= self.mark <= self.maximum():
            return None
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        style = self.style()
        groove = style.subControlRect(QStyle.CC_Slider, option, QStyle.SC_SliderGroove, self)
        handle = style.subControlRect(QStyle.CC_Slider, option, QStyle.SC_SliderHandle, self)
        offset = QStyle.sliderPositionFromValue(
            self.minimum(), self.maximum(), self.mark, groove.width() - handle.width()
        )
        return groove.x() + offset + handle.width() // 2

    def paintEvent(self, event) -> None:  # noqa: N802（Qt の名前）
        x = self.mark_x()
        if x is not None:
            painter = QPainter(self)
            painter.setPen(QPen(self.palette().color(QPalette.WindowText), 2, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(x, 0, x, self.height() - 1)
            painter.end()
        super().paintEvent(event)


class ParameterRow(QWidget):
    """1つの設定の行: 横ゲージ・入力欄・「既定」ボタン（#120）。

    **値の正は入力欄**（`spin`）。ゲージは入力欄を刻み（``step``）の目盛りで写したもので、
    動かすと入力欄に刻みの倍数を入れる。入力欄から写すときはゲージの信号を止めるので、
    ゲージの目盛りに合わない値（0.33 や右端より大きい値）も**丸めずにそのまま残る**
    （ルールの値を丸めない・頭打ちにしない。PR #108 のレビュー指摘3）。
    「既定」ボタンはこの行だけを既定値に戻し、既定のときは押せない。
    """

    def __init__(self, spin, step: float, slider_maximum: float, default: float):
        super().__init__()
        self.spin = spin
        self.step = step
        self.default = default
        self.slider = DefaultMarkSlider(Qt.Horizontal, int(round(default / step)))
        self.slider.setRange(0, int(round(slider_maximum / step)))
        self.slider.setPageStep(max(1, self.slider.maximum() // 10))
        self.slider.setMinimumWidth(90)
        self.slider.setToolTip(f"縦の印が既定値（{_number_text(default)}）です")
        self.reset_button = QPushButton(DEFAULT_LABEL)
        self.reset_button.setToolTip(f"既定値（{_number_text(default)}）に戻します")
        self.reset_button.clicked.connect(self.reset)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.slider, 1)
        layout.addWidget(spin)
        layout.addWidget(self.reset_button)
        self.slider.valueChanged.connect(self._slider_moved)
        spin.valueChanged.connect(self._spin_changed)
        self._spin_changed(spin.value())

    def _slider_moved(self, position: int) -> None:
        self.spin.setValue(position * self.step)

    def _spin_changed(self, value) -> None:
        position = int(round(value / self.step))
        self.slider.blockSignals(True)
        self.slider.setValue(max(self.slider.minimum(), min(self.slider.maximum(), position)))
        self.slider.blockSignals(False)
        self.reset_button.setEnabled(abs(value - self.default) > 1e-9)

    def reset(self) -> None:
        self.spin.setValue(self.default)


class SelectionPanel(QWidget):
    """「写真の選定」タブ。左に設定、右に結果。"""

    def __init__(
        self,
        connection,
        rule_path: Optional[str] = None,
        output_root: Optional[str] = None,
        source_roots: Optional[Sequence[str]] = None,
    ):
        super().__init__()
        self.connection = connection
        #: いま読み書きするルールのファイル。「ルールを読み込む」と「別のファイルに保存」で変わる（#18）。
        self.rule_path = rule_path
        #: 設定の `rule_path`。読み込みと別名の保存で書き換える（設定に書けなかったときだけ上と違う）。
        self.configured_rule_path = rule_path
        self.output_root = output_root
        self.source_roots = list(source_roots or [])
        self.rule: Dict[str, Any] = self._read_rule()
        self.candidates: Optional[selection.Candidates] = None
        #: 候補を用意したときの `db.selection_state`。変わっていたら「選ぶ」の前に用意し直す。
        self.candidates_state: Any = None
        self._years_filled = False
        #: いま年の表に効いている期間の欄の値（同じ値なら表を作り直さない）。
        self._applied_period = ("", "")
        #: 年の表の「〜年以降」の行の年。行が無ければ None（`later_row_year`）。
        self.later_year: Optional[int] = None
        #: 表に出ていない行の枚数（``{年 / ROW_LATER: 枚数}``）。期間や絞り込みで行が消えても、
        #: 枚数を失わないために持つ（`_remember_hidden_rows`・PR #142 のレビュー指摘1）。
        self._hidden_counts: Dict[Any, int] = {}
        #: 「〜年以降」の行が隠れているときに、その枚数を書く年。
        self._hidden_later_year: Optional[int] = None
        self.selected: List[Dict[str, Any]] = []
        self.by_year: Dict[Optional[int], List[Dict[str, Any]]] = {}
        self.page = 0
        self.thumbnails: Dict[int, QPixmap] = {}
        #: 選んだときのルール（点の内訳を同じ重みで出すため）。
        self.chosen_rule: Dict[str, Any] = {}

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_settings())
        splitter.addWidget(self._build_results())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([380, 820])
        layout = QVBoxLayout(self)
        layout.addWidget(splitter)
        self._show_rule(self.rule)

    # ------------------------------------------------------------------
    # 画面の組み立て
    # ------------------------------------------------------------------

    def _build_settings(self) -> QWidget:
        inner = QWidget()
        layout = QVBoxLayout(inner)

        weights_box = QGroupBox("並びの重み")
        weights_form = QFormLayout(weights_box)
        # 名前を行の上に置き、ゲージに幅を残す（#120。名前の長い行でゲージが潰れないように）
        weights_form.setRowWrapPolicy(QFormLayout.WrapAllRows)
        self.weight_spins: Dict[str, QDoubleSpinBox] = {}
        #: 横ゲージと「既定」ボタンの行（#120）。値は `weight_spins` が持つ。
        self.weight_rows: Dict[str, ParameterRow] = {}
        defaults = scoring.Weights.default()
        for key, label, step in WEIGHT_FIELDS:
            spin = NoWheelDoubleSpinBox()
            # 比重と2人目以降の効きには上限がある（`selection.rule_weights` が超える値を止める）
            spin.setRange(0.0, WEIGHT_LIMITS.get(key, WEIGHT_MAXIMUM))
            spin.setDecimals(WEIGHT_DECIMALS)
            spin.setSingleStep(step)
            self.weight_spins[key] = spin
            row = ParameterRow(spin, step, WEIGHT_SLIDER_MAXIMUM[step], getattr(defaults, key))
            self.weight_rows[key] = row
            # 折り返す。長い項目名が欄の幅を押し広げ、「既定」ボタンが切れて横スクロールが出ていた
            name = QLabel(label)
            name.setWordWrap(True)
            weights_form.addRow(name, row)
        layout.addWidget(weights_box)

        period_box = QGroupBox("撮影時期（-- なら絞り込まない）")
        period_form = QFormLayout(period_box)
        self.period_start = DateBoxes(cascade=True)
        self.period_end = DateBoxes(cascade=True, end=True)
        for boxes in (self.period_start, self.period_end):
            boxes.changed.connect(self._period_changed)
        self.period_start.setToolTip("日が -- ならその月の初日から、月も -- ならその年の1月1日から")
        self.period_end.setToolTip("その日の終わりまで含みます。日が -- ならその月の末日まで、月も -- ならその年の12月31日まで")
        period_form.addRow("始め", self.period_start)
        period_form.addRow("終わり", self.period_end)
        self.period_notice = QLabel("")
        self.period_notice.setWordWrap(True)
        period_form.addRow(self.period_notice)
        layout.addWidget(period_box)

        flags_box = QGroupBox("絞り込み")
        flags_layout = QVBoxLayout(flags_box)
        self.flag_checks: Dict[str, QCheckBox] = {}
        for key, label in FLAG_FIELDS:
            check = QCheckBox(label)
            check.toggled.connect(self._filters_changed)
            self.flag_checks[key] = check
            flags_layout.addWidget(check)
        layout.addWidget(flags_box)

        numbers_box = QGroupBox("似た写真の判定")
        numbers_form = QFormLayout(numbers_box)
        numbers_form.setRowWrapPolicy(QFormLayout.WrapAllRows)
        self.number_spins: Dict[str, QSpinBox] = {}
        self.number_rows: Dict[str, ParameterRow] = {}
        for key, label, maximum, slider_maximum in NUMBER_FIELDS:
            spin = NoWheelSpinBox()
            spin.setRange(0, maximum)
            self.number_spins[key] = spin
            row = ParameterRow(spin, 1, slider_maximum, selection.RULE_NUMBERS[key])
            self.number_rows[key] = row
            numbers_form.addRow(label, row)
        layout.addWidget(numbers_box)

        years_box = QGroupBox("年ごとの枚数（0 の年からは選ばない）")
        years_layout = QVBoxLayout(years_box)
        self.year_table = QTableWidget(0, 3)
        self.year_table.setHorizontalHeaderLabels(["年", "候補", "選ぶ枚数"])
        self.year_table.verticalHeader().setVisible(False)
        self.year_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.year_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.year_table.setMinimumHeight(220)
        years_layout.addWidget(self.year_table)
        fill_row = QHBoxLayout()
        self.fill_spin = NoWheelSpinBox()
        self.fill_spin.setRange(0, 9999)
        self.fill_spin.setValue(30)
        self.fill_button = QPushButton("全部の年に入れる")
        self.fill_button.clicked.connect(self.fill_all_years)
        fill_row.addWidget(self.fill_spin)
        fill_row.addWidget(self.fill_button)
        years_layout.addLayout(fill_row)
        layout.addWidget(years_box)

        output_box = QGroupBox("出力の形")
        output_layout = QVBoxLayout(output_box)
        self.layout_combo = NoWheelComboBox()
        for key in selection.OUTPUT_LAYOUTS:
            self.layout_combo.addItem(LAYOUT_LABELS[key], key)
        self.layout_combo.setToolTip("選ぶ写真は変わりません。「選ぶ」を押し直さなくても「出力する」に効きます")
        output_layout.addWidget(self.layout_combo)
        layout.addWidget(output_box)

        self.choose_button = QPushButton(CHOOSE_LABEL)
        self.choose_button.setToolTip(
            "重み・絞り込み・年ごとの枚数で写真を選びます（何度押してもよい）。\n"
            "顔の割り当てが変わっていれば、先に読み直します（初回は数分かかることがあります）。"
        )
        self.choose_button.clicked.connect(self.choose)
        self.choose_button.setProperty("look", "primary")
        # いま読み書きするルールのファイル。読み込みと別名の保存で変わるので、画面に出す（#18）
        self.rule_file_label = QLabel("")
        self.rule_file_label.setWordWrap(True)
        self._show_rule_file()
        self.export_button = QPushButton("出力する")
        self.export_button.clicked.connect(self.export)
        layout.addWidget(self.rule_file_label)
        layout.addWidget(self.choose_button)
        layout.addWidget(self.export_button)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        # 使わなくなったキーの知らせ（PR #140 の指摘3）。**`status` とは分ける**（「選ぶ」で消えないように）。
        self.retired_notice = QLabel("")
        self.retired_notice.setWordWrap(True)
        layout.addWidget(self.retired_notice)
        self._show_retired_keys()
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        return scroll

    def _build_results(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        top = QHBoxLayout()
        # **撮影時期は年と月の欄で選ぶ**（#113・#134・利用者の指定）。並びは年の古い順で、年不明は最後。
        self.year_range = YearMonthRange()
        self.year_range.changed.connect(self._year_changed)
        self.include_unknown = QCheckBox(f"{YEAR_UNKNOWN}を入れる")
        self.include_unknown.setChecked(True)
        self.include_unknown.setToolTip(
            "年の分からない写真を、範囲と関係なく一覧に入れます（点の順以外では最後に並びます）。"
            "表示だけで、選ぶ写真と出力は変わりません"
        )
        self.include_unknown.toggled.connect(self._year_changed)
        # **並びは見せ方だけ**（#18・利用者の要望）。選ぶ写真と出力の順は変えない
        self.order_box = NoWheelComboBox()
        for label, value in RESULT_ORDERS:
            self.order_box.addItem(label, value)
        self.order_box.setToolTip(
            "一覧の並び順です。「年の古い順」は、年の中を点の高い順に並べます。選ぶ写真と出力は変わりません"
        )
        self.order_box.currentIndexChanged.connect(self._year_changed)
        self.previous_button = QPushButton("前へ")
        self.previous_button.clicked.connect(lambda: self._turn(-1))
        self.next_button = QPushButton("次へ")
        self.next_button.clicked.connect(lambda: self._turn(1))
        self.page_label = QLabel("")
        top.addWidget(QLabel("並び"))
        top.addWidget(self.order_box)
        top.addWidget(QLabel("撮影"))
        top.addWidget(self.year_range)
        top.addWidget(self.include_unknown)
        top.addStretch(1)
        top.addWidget(self.previous_button)
        top.addWidget(self.page_label)
        top.addWidget(self.next_button)
        layout.addLayout(top)

        self.result_list = QListWidget()
        self.result_list.setViewMode(QListWidget.IconMode)
        self.result_list.setIconSize(QSize(RESULT_THUMBNAIL, RESULT_THUMBNAIL))
        self.result_list.setResizeMode(QListWidget.Adjust)
        self.result_list.setMovement(QListWidget.Static)
        self.result_list.setSpacing(6)
        self.result_list.currentItemChanged.connect(self._item_changed)

        # **選んだ写真を右に大きく出す**（#113・利用者の指定）。点の内訳はその下。
        side = QWidget()
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        self.preview = QLabel(PREVIEW_PROMPT)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(320, 240)
        self.preview.setProperty("look", "panel")
        # 画像の大きさで枠が広がらないようにする（枠に合わせて縮めて出す）。
        self.preview.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        side_layout.addWidget(self.preview, 1)
        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        side_layout.addWidget(self.detail)

        body = QSplitter(Qt.Horizontal)
        body.addWidget(self.result_list)
        body.addWidget(side)
        body.setStretchFactor(0, 1)
        body.setStretchFactor(1, 1)
        body.setSizes([480, 480])
        layout.addWidget(body, 1)
        return panel

    # ------------------------------------------------------------------
    # ルールと画面の行き来
    # ------------------------------------------------------------------

    def set_paths(
        self,
        rule_path: Optional[str],
        output_root: Optional[str],
        source_roots: Optional[Sequence[str]],
    ) -> bool:
        """設定の窓で保存した値に差し替える（#131）。ルールを読み直したら True。

        - **ルールのパスが変わったら、新しいファイルを読み直して画面に出す**（重みと年の表。
          年の表は入っている値を引き継がず、新しいファイルの枚数から作り直す）。同じパスなら
          画面で動かした重みはそのまま。**比べる相手は設定の値**で、いま読み書きしているファイルではない
          （設定に書けなかったとき、設定の窓を保存しただけで前のファイルへ戻らないように）
        - root が変わったら、用意した候補と結果を捨てる（候補は root から元写真を引く）
        """
        roots = list(source_roots or [])
        if roots != self.source_roots:
            self.source_roots = roots
            self.candidates = None
            self.selected = []
            self.chosen_rule = {}
            self._group_by_year()
        self.output_root = output_root
        if rule_path == self.configured_rule_path:
            return False
        self.configured_rule_path = rule_path
        self.rule_path = rule_path
        self._use_rule(self._read_rule())
        return True

    def _use_rule(self, rule: Dict[str, Any]) -> None:
        """ルールを画面に出す。年の表は入っている値を引き継がず、このルールの枚数から作り直す。"""
        self.rule = rule
        self._show_rule(rule)
        self._show_retired_keys()
        self._show_rule_file()
        self.year_table.setRowCount(0)
        self._hidden_counts = {}
        if self._years_filled:
            self.refresh_years()

    def _show_rule_file(self) -> None:
        name = Path(self.rule_path).name if self.rule_path else "なし"
        self.rule_file_label.setText(f"ルールのファイル: {name}")
        self.rule_file_label.setToolTip(self.rule_path or "")

    def _read_rule(self) -> Dict[str, Any]:
        if not self.rule_path or not Path(self.rule_path).exists():
            return {}
        try:
            return selection.load_rule(self.rule_path)
        # YAML の構文の誤りは ValueError ではない（受けないと GUI ごと落ちる）
        except (OSError, ValueError, yaml.YAMLError) as error:
            QMessageBox.warning(self, "ルールを読めません", str(error))
            return {}

    def _show_retired_keys(self) -> None:
        """いま読んでいるルールの、使わなくなったキーを画面に出す（GUI は端末を見ずに起動されることがある）。"""
        retired = selection.retired_weight_keys(self.rule)
        self.retired_notice.setText(
            f"ルールの {'・'.join(retired)} は使わなくなりました。点の出し方が変わり、値は読み飛ばしています"
            "（「ルールを保存」で消えます。docs/operation/CONFIG.md）。"
            if retired else ""
        )

    def _show_rule(self, rule: Dict[str, Any]) -> None:
        weights = selection.rule_weights(rule)
        for key, spin in self.weight_spins.items():
            spin.setValue(getattr(weights, key))
        for key, check in self.flag_checks.items():
            check.setChecked(selection._rule_flag(rule, key))
        for key, spin in self.number_spins.items():
            spin.setValue(int(selection._rule_number(rule, key, selection.RULE_NUMBERS[key])))
        period = rule.get("date")
        period = period if isinstance(period, dict) else {}
        self.period_start.setText(_period_text(period.get("start")))
        self.period_end.setText(_period_text(period.get("end")))
        self.period_notice.setText(self._period_problem() or "")
        self.layout_combo.setCurrentIndex(self.layout_combo.findData(selection.output_layout(rule)))

    def _period_problem(self) -> Optional[str]:
        """期間の問題の説明。**読めない値は `--` を選び直しても変わらない**ので、消し方を添える（PR #143 の指摘3）。"""
        problem = selection.period_problem(*self._period_values())
        boxes = (self.period_start, self.period_end)
        if problem and any(box.holds_rule_value() and selection._parse_date(box.text()) is None for box in boxes):
            problem += "\n欄では -- に見えますが、ルールにこの値があります。年を選び直すと置き換わります。"
        return problem

    def _period_values(self) -> tuple:
        return self.period_start.text().strip(), self.period_end.text().strip()

    def _rule_with_period(self, rule: Dict[str, Any]) -> Dict[str, Any]:
        """期間の欄を ``date`` に入れる。**空欄の端はキーごと消す**（両方空なら ``date`` も消す）。"""
        start, end = self._period_values()
        period = {key: value for key, value in (("start", start), ("end", end)) if value}
        rule.pop("date", None)
        if period:
            rule["date"] = period
        return rule

    def _period_changed(self) -> None:
        """期間の欄を変えたら年の表を作り直す（候補の枚数・並ぶ年が変わる）。入っている値は引き継ぐ。"""
        values = self._period_values()
        problem = self._period_problem()
        self.period_notice.setText(problem or "")
        if problem or values == self._applied_period:
            return
        if self._years_filled:
            self.refresh_years()

    def _period_is_readable(self) -> bool:
        """「選ぶ」と「ルールを保存」の前に。読めなければ知らせて止める。読めれば表にも効かせる。"""
        problem = self._period_problem()
        if problem:
            QMessageBox.warning(self, "設定を読めません", problem)
            return False
        self._period_changed()
        return True

    def current_rule(self) -> Dict[str, Any]:
        """画面の設定から作ったルール。画面に出していないキーは元のルールのまま。"""
        rule = self._rule_with_period(dict(self.rule))
        rule["weights"] = {key: spin.value() for key, spin in self.weight_spins.items()}
        for key, check in self.flag_checks.items():
            rule[key] = check.isChecked()
        for key, spin in self.number_spins.items():
            rule[key] = spin.value()
        rule["output_layout"] = self.layout_combo.currentData()
        if self.year_table.rowCount():
            # **表に出ていない行の枚数も書く**（期間を狭めて保存しても、ほかの年の枚数を消さない）
            values = {**self._hidden_counts, **self._year_values()}
            later = self._hidden_later_year if self.later_year is None else self.later_year
            counts: Dict[Any, int] = {year: values[year] for year in sorted(k for k in values if isinstance(k, int))}
            if selection.COUNT_UNKNOWN in values:
                counts[selection.COUNT_UNKNOWN] = values[selection.COUNT_UNKNOWN]
            if ROW_LATER in values:
                counts[f"{later}{selection.LATER_SUFFIX}"] = values[ROW_LATER]
            rule["count_per_year"] = counts
        return rule

    def current_weights(self) -> scoring.Weights:
        """画面の重み。「選ぶ」と同じ値（顔の割り当ての一覧の点もこれで出す・#18）。"""
        return selection.rule_weights({"weights": {key: spin.value() for key, spin in self.weight_spins.items()}})

    def _year_values(self) -> Dict[Any, int]:
        """いま表に入っている枚数（``{年 / "unknown" / "later": 枚数}``）。"""
        values: Dict[Any, int] = {}
        for row in range(self.year_table.rowCount()):
            key = self.year_table.item(row, 0).data(Qt.UserRole)
            values[key] = self.year_table.cellWidget(row, 2).value()
        return values

    def showEvent(self, event) -> None:  # noqa: N802（Qt の名前）
        """タブを初めて開いたときに年の表を作る（起動を遅くしないため・実データで約 1 秒）。"""
        super().showEvent(event)
        if not self._years_filled:
            self.refresh_years()

    def refresh_years(self) -> None:
        """年の表を DB から作り直す（#113）。入っている枚数は引き継ぐ。"""
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self._fill_year_table()
        finally:
            QApplication.restoreOverrideCursor()
        self._years_filled = True

    def _fill_year_table(self) -> None:
        """候補の年ごとの枚数を表に出す。**入っている値は引き継ぐ**（PR #108 のレビュー指摘1）。

        行は、候補のある年・**〜年以降**・**年不明**（利用者の決定・PR #108。これから増える年は
        ここで決める。年は `later_row_year`・期間が届かなければ行が無い・#134）。**年不明は必ず出し、
        常に一番下**（#18・利用者の指定）。新しく現れた行の
        初期値はルールファイルから（年ごとの対応なら ``unknown`` / ``2027+`` も）。ファイルに枚数が
        無ければ候補の枚数。
        """
        previous = {**self._hidden_counts, **self._year_values()}
        self._applied_period = self._period_values()
        # 候補を用意していなくても数えられる（DB だけ・#113）。数は `candidate_counts` と同じ。
        rule = self.current_rule_without_years()
        counts = selection.year_counts(self.connection, rule)
        limit_of = selection.year_limits(self.rule)
        rows = [(year, year_label(year), counts[year]) for year in sorted(y for y in counts if y is not None)]
        end = selection._rule_end((rule.get("date") or {}).get("end"))
        self.later_year = later_row_year((y for y in counts if y is not None), end.year if end else None)
        if self.later_year is not None:
            rows.append((ROW_LATER, f"{self.later_year} 年以降", None))
        rows.append((selection.COUNT_UNKNOWN, YEAR_UNKNOWN, counts.get(None, 0)))
        self.year_table.setRowCount(0)
        for key, label, candidates in rows:
            row = self.year_table.rowCount()
            self.year_table.insertRow(row)
            item = QTableWidgetItem(label)
            item.setData(Qt.UserRole, key)
            self.year_table.setItem(row, 0, item)
            self.year_table.setItem(row, 1, QTableWidgetItem("" if candidates is None else str(candidates)))
            spin = NoWheelSpinBox()
            spin.setRange(0, 99_999)
            spin.setValue(previous[key] if key in previous else _initial_count(key, candidates, limit_of))
            self.year_table.setCellWidget(row, 2, spin)
        self._remember_hidden_rows(rule, {key for key, _, _ in rows}, previous, counts, limit_of)

    def _remember_hidden_rows(self, rule, shown, previous, counts, limit_of) -> None:
        """表に出ていない行の枚数を持っておく（PR #142 のレビュー指摘1）。

        期間で行が消えるのは普段の操作になった。持っておかないと、戻したときにファイルの値へ戻り、
        消したまま保存するとファイルからも消えて、広げ直しても 0 のまま黙ってその年を外す。
        期間の外の年は、期間を外して数える（**期間が入っているときだけ**もう1回数える）。
        """
        hidden = {key: value for key, value in previous.items() if key not in shown}
        every = counts
        if rule.get("date"):
            every = selection.year_counts(self.connection, {k: v for k, v in rule.items() if k != "date"})
        years = [year for year in every if year is not None]
        for year in years:
            if year not in shown and year not in hidden:
                hidden[year] = _initial_count(year, every[year], limit_of)
        if ROW_LATER not in shown:
            hidden.setdefault(ROW_LATER, _initial_count(ROW_LATER, None, limit_of))
            self._hidden_later_year = max(years, default=date.today().year - 1) + 1
        self._hidden_counts = hidden

    def _filters_changed(self, _checked: bool) -> None:
        """絞り込みを変えたら年の表を作り直す（候補の枚数・並ぶ年が変わる）。入っている値は引き継ぐ。"""
        if self._years_filled:
            self.refresh_years()

    def current_rule_without_years(self) -> Dict[str, Any]:
        rule = self._rule_with_period(dict(self.rule))
        for key, check in self.flag_checks.items():
            rule[key] = check.isChecked()
        rule.pop("count_per_year", None)
        return rule

    def fill_all_years(self) -> None:
        """全部の行（年不明・〜年以降も）に同じ数を入れる。"""
        for row in range(self.year_table.rowCount()):
            self.year_table.cellWidget(row, 2).setValue(self.fill_spin.value())

    # ------------------------------------------------------------------
    # 実行
    # ------------------------------------------------------------------

    def _candidates_are_current(self) -> bool:
        """用意した候補が、いまの絞り込みと割り当てで使えるか（#113）。"""
        if self.candidates is None:
            return False
        sources = selection.family_sources(self.current_rule_without_years())
        if set(sources) - set(self.candidates.assign_sources):
            return False
        return self.candidates_state == db.selection_state(self.connection)

    def load_candidates(self) -> None:
        """`selection.prepare`。見え方・表情の未計測分を測る（初回は数分かかることがある）。

        「選ぶ」が要るときだけ呼ぶ（`_candidates_are_current`）。年の表も作り直す。
        """
        rule = self.current_rule_without_years()
        progress = _Progress(self, "候補を読み込んでいます")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.candidates_state = db.selection_state(self.connection)
            self.candidates = selection.prepare(
                self.connection,
                selection.family_sources(rule),
                self.source_roots,
                progress,
            )
            self._fill_year_table()
            self._years_filled = True
        finally:
            QApplication.restoreOverrideCursor()
            progress.finish()
        # **前の結果を捨てる**（PR #108 のレビュー指摘2）。結果は読み込んだ候補に結びついている。
        # 残すと、古い写真の内訳が新しい割り当てで出たり（種別が足りなければ例外）、
        # 「出力する」が古い割り当てで選んだ結果をコピーしたりする。
        self.selected = []
        self.chosen_rule = {}
        self._group_by_year()

    def choose(self) -> None:
        """`selection.choose`。設定が読めない値なら止める。"""
        if not self._period_is_readable():
            return
        if not self._candidates_are_current():
            self.load_candidates()
        rule = self.current_rule()
        try:
            selection.rule_weights(rule)
            selection.year_limits(rule)
        except ValueError as error:
            QMessageBox.warning(self, "設定を読めません", str(error))
            return
        progress = _Progress(self, "並べ直しています")
        self.candidates.progress_callback = progress
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.selected = selection.choose(self.candidates, rule)
        finally:
            QApplication.restoreOverrideCursor()
            progress.finish()
            self.candidates.progress_callback = None
        self.chosen_rule = rule
        self._group_by_year()
        self.status.setText(f"{len(self.selected)} 枚を選びました。")

    def _group_by_year(self) -> None:
        self.by_year = {}
        for media in self.selected:
            self.by_year.setdefault(selection._get_media_year(media), []).append(media)
        # 初めは全部の年。月まで分かる写真の月だけを月の欄に並べる（年までしか分からない写真は年の欄だけ）。
        self.year_range.set_months(
            sorted({
                _month_text(period[0])
                for period in map(selection._media_period, self.selected)
                if period is not None and _month_text(period[0]) == _month_text(period[1])
            }),
            (year for year in self.by_year if year is not None),
        )
        blocked = self.year_range.blockSignals(True)
        self.year_range.set_range(None, None)
        self.year_range.blockSignals(blocked)
        self.page = 0
        self._show_page()

    def _year_changed(self) -> None:
        self.page = 0
        self._show_page()

    def _years_in_order(self) -> List[Optional[int]]:
        """年の古い順。年不明は最後。"""
        return sorted(self.by_year, key=lambda y: (y is None, y or 0))

    def _current_year_media(self) -> List[Dict[str, Any]]:
        """選んだ撮影時期の写真を、`並び` の順に。

        **年不明は範囲と関係なく、`年不明を入れる` で決める**（#18）。点の順では点のとおりに混ぜ、
        それ以外の並びでは最後に置く（撮影時期で比べられないため）。
        月まで分からない写真は、**撮影時期がまるごと範囲に入るときだけ**出す（顔の割り当てタブと同じ）。
        """
        start, end = self.year_range.bounds()
        dated = [media for year in self._years_in_order() if year is not None for media in self.by_year[year]]
        if start is not None or end is not None:
            dated = [media for media in dated if _period_within(selection._media_period(media), start, end)]
        unknown = list(self.by_year.get(None, ())) if self.include_unknown.isChecked() else []
        order = self.order_box.currentData()
        # sorted は同じ値の順を保つ（reverse でも）。同じ点・同じ日時は選んだ順のまま
        if order in (ORDER_POINTS_HIGH, ORDER_POINTS_LOW):
            return sorted(
                dated + unknown,
                key=lambda media: media.get("family_score") or 0.0,
                reverse=order == ORDER_POINTS_HIGH,
            )
        if order in (ORDER_TAKEN_OLD, ORDER_TAKEN_NEW):
            dated = sorted(dated, key=_taken_key, reverse=order == ORDER_TAKEN_NEW)
        return dated + unknown

    def _turn(self, step: int) -> None:
        pages = max(1, -(-len(self._current_year_media()) // RESULT_PAGE_SIZE))
        self.page = min(max(0, self.page + step), pages - 1)
        self._show_page()

    def _show_page(self) -> None:
        media_list = self._current_year_media()
        pages = max(1, -(-len(media_list) // RESULT_PAGE_SIZE))
        start = self.page * RESULT_PAGE_SIZE
        page = media_list[start : start + RESULT_PAGE_SIZE]
        self.page_label.setText(f"{self.page + 1} / {pages}")
        self.previous_button.setEnabled(self.page > 0)
        self.next_button.setEnabled(self.page < pages - 1)
        self.result_list.clear()
        self._clear_preview()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for media in page:
                # 年をまたいで並ぶので、どの年の写真かを添える。
                year = year_label(selection._get_media_year(media))
                text = f"{year} {Path(media['path']).name}"
                if self.order_box.currentData() in (ORDER_POINTS_HIGH, ORDER_POINTS_LOW):
                    text += f" {media.get('family_score') or 0.0:.0f}点"
                item = QListWidgetItem(self._thumbnail_icon(media), text)
                item.setData(Qt.UserRole, media["id"])
                item.setToolTip(media["path"])
                self.result_list.addItem(item)
        finally:
            QApplication.restoreOverrideCursor()

    def _thumbnail_icon(self, media: Dict[str, Any]) -> QIcon:
        pixmap = self.thumbnails.get(media["id"])
        if pixmap is None:
            path = selection._source_path(
                media["path"], [Path(root).resolve() for root in self.source_roots]
            )
            image = photo_thumbnail(str(path))
            pixmap = QPixmap() if image is None else _pixmap(image)
            self.thumbnails[media["id"]] = pixmap
        return QIcon(pixmap)

    def _clear_preview(self) -> None:
        self.detail.setText("")
        self.preview.setPixmap(QPixmap())
        self.preview.setText(PREVIEW_PROMPT)

    def _show_preview(self, media: Dict[str, Any]) -> None:
        """選んだ写真を右に大きく出す。**元写真は NFS 上**なので読むあいだは砂時計にする。"""
        path = selection._source_path(
            media.get("path", ""), [Path(root).resolve() for root in self.source_roots]
        )
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            image = photo_preview(str(path))
        finally:
            QApplication.restoreOverrideCursor()
        if image is None:
            # 前の写真の画像を残さない（下の内訳と別の写真になる）。
            self.preview.setPixmap(QPixmap())
            self.preview.setText(f"写真を開けません:\n{path}")
            return
        self.preview.setText("")
        self.preview.setPixmap(
            _pixmap(image).scaled(self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )

    def _item_changed(self, current, _previous) -> None:
        if current is None or self.candidates is None:
            self._clear_preview()
            return
        media_id = current.data(Qt.UserRole)
        media = next((m for m in self.selected if m["id"] == media_id), None)
        parts = selection.explain(self.candidates, self.chosen_rule, media_id)
        from .gui import format_folder

        media = media or {}
        path = Path(media.get("path", ""))
        place = f"{format_folder(str(path.parent), self.source_roots or None)}/{path.name}"
        taken = media.get("shooting_date") or "撮影日時なし"
        measured = self.candidates.quality.get(media_id)
        self.detail.setText(f"{place}\n{taken}\n{format_breakdown(parts, measured)}")
        self._show_preview(media)

    # ------------------------------------------------------------------
    # 保存と出力
    # ------------------------------------------------------------------

    def _rule_folder(self) -> str:
        """ファイルを選ぶ窓を開く場所。"""
        return str(Path(self.rule_path).parent) if self.rule_path else str(Path.cwd())

    def load_rule_file(self) -> None:
        """ファイルを選んで画面の設定を置き換える（#18）。**読めないファイルでは画面を変えない。**

        以後の「ルールを保存」はこのファイルへ書き、設定の `rule_path` もこのファイルにする
        （次に起動したときと CLI の `select` もこのファイルを読む・利用者の決定）。
        """
        path, _ = QFileDialog.getOpenFileName(self, "ルールを読み込む", self._rule_folder(), RULE_FILE_FILTER)
        if not path:
            return
        try:
            rule = selection.load_rule(path)
        except (OSError, ValueError, yaml.YAMLError) as error:
            QMessageBox.warning(self, "ルールを読めません", str(error))
            return
        answer = QMessageBox.question(
            self,
            "ルールを読み込む",
            f"画面の設定を {path} の内容に置き換えます。保存していない変更は消えます。\n"
            "次に起動したときと CLI の select も、このファイルを読むようになります。",
        )
        if answer != QMessageBox.Yes:
            return
        self.rule_path = path
        self._use_rule(rule)
        self._remember_rule_path(path)
        self.status.setText(f"ルールを読み込みました: {path}。結果に効かせるには「{CHOOSE_LABEL}」を押してください。")

    def _remember_rule_path(self, path: str) -> None:
        """設定の `rule_path` をこのファイルにする。**書けなくても、読み込みや保存は取り消さない**（知らせるだけ）。

        同じファイルなら書かない（設定ファイルのコメントが消え、控えも上書きされるだけなので）。
        """
        known = self.configured_rule_path
        if known and Path(known).expanduser().resolve() == Path(path).expanduser().resolve():
            return
        try:
            config.write_settings(config.settings_write_path(), {"rule_path": path})
        except (OSError, config.SettingsWriteError) as error:
            QMessageBox.warning(
                self,
                "設定に書けません",
                f"次に起動したときと CLI の select は、前のファイルを読みます。\n{error}",
            )
            return
        self.configured_rule_path = path

    def _rule_to_save(self) -> Optional[Dict[str, Any]]:
        """画面の設定から書くルールを作る。読めない値があれば知らせて None。"""
        if not self._period_is_readable():
            return None
        rule = self.current_rule()
        try:
            selection.rule_weights(rule)
            selection.year_limits(rule)
        except ValueError as error:
            QMessageBox.warning(self, "設定を読めません", str(error))
            return None
        return rule

    def _write_rule_to(self, path: str, rule: Dict[str, Any]) -> bool:
        """ルールを書く。書けたら True。"""
        try:
            write_rule(path, rule)
        except OSError as error:
            QMessageBox.warning(self, "保存できません", str(error))
            return False
        self.rule_path = path
        self.rule = rule
        self._show_retired_keys()
        self._show_rule_file()
        self.status.setText(f"ルールを保存しました: {path}")
        return True

    def save_rule(self) -> None:
        """いまのファイルに上書きする。**書く前に控え（``.bak``）を取る。** YAML のコメントは消える。"""
        if not self.rule_path:
            self.save_rule_as()  # 上書きする先が無いので、保存先を聞く
            return
        rule = self._rule_to_save()
        if rule is None:
            return
        answer = QMessageBox.question(
            self,
            "ルールを保存",
            f"{self.rule_path} に上書きします。もとのファイルは {self.rule_path}.bak に控えます。"
            "ファイル内のコメントは消えます。",
        )
        if answer != QMessageBox.Yes:
            return
        self._write_rule_to(self.rule_path, rule)

    def save_rule_as(self) -> None:
        """保存先を選んで書く（#18）。以後はそのファイルを読み書きし、設定の `rule_path` もそのファイルにする。

        既にあるファイルを選んだときの確認は、ファイルを選ぶ窓が出す。そのときも ``.bak`` は取る。
        """
        rule = self._rule_to_save()
        if rule is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "別のファイルに保存", self._rule_folder(), RULE_FILE_FILTER)
        if not path:
            return
        # 拡張子を打たなくても、読み込める名前にする
        if Path(path).suffix.lower() not in RULE_FILE_SUFFIXES:
            path += RULE_FILE_SUFFIXES[0]
        if self._write_rule_to(path, rule):
            self._remember_rule_path(path)

    def export(self) -> None:
        """選んだ写真を出力先へコピーする（CLI の `select` と同じ。前の出力は消える）。"""
        if self.selected and self.candidates_state != db.selection_state(self.connection):
            # 選んだあとに割り当てが変わった。古い割り当てで選んだ結果を出力しない。
            self.selected = []
            self.chosen_rule = {}
            self._group_by_year()
        if not self.selected:
            QMessageBox.information(
                self, "出力", f"先に「{CHOOSE_LABEL}」で写真を選んでください"
                "（顔の割り当てを変えたあとは、選び直しが要ります）。"
            )
            return
        if not self.output_root:
            QMessageBox.warning(self, "出力できません", "設定に output_root がありません。")
            return
        # 出力の形は選ぶ写真を変えないので、選んだときではなく今の欄の値で出す（#134）。
        layout = self.layout_combo.currentData()
        where = LAYOUT_LABELS[layout]
        answer = QMessageBox.question(
            self,
            "出力",
            f"{len(self.selected)} 枚を {self.output_root} の{where}にコピーします。前の出力は消えます。",
        )
        if answer != QMessageBox.Yes:
            return
        progress = _Progress(self, "コピーしています")
        try:
            copied = selection.copy_selected_media(
                self.selected, self.output_root, self.source_roots, progress, layout=layout
            )
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "出力が止まりました", str(error))
            return
        finally:
            progress.finish()
        self.status.setText(f"{copied} 枚を {self.output_root} にコピーしました。")


def _taken_key(media: Dict[str, Any]) -> tuple:
    """撮影時期で並べるキー。月や年までしか分からない写真は、その区間の始まりで比べる。"""
    period = selection._media_period(media)
    return (period is not None, period[0] if period else datetime.min)


def _month_text(moment) -> str:
    return f"{moment.year:04d}-{moment.month:02d}"


def _period_within(period, start: Optional[str], end: Optional[str]) -> bool:
    """撮影時期（`selection._media_period`）が範囲（``YYYY-MM``・両端を含む）にまるごと入るか。"""
    if period is None:
        return False
    return (start is None or _month_text(period[0]) >= start) and (
        end is None or _month_text(period[1]) <= end
    )


def _period_text(value: Any) -> str:
    """ルールの日付を欄の文字に（YAML の引用符の無い日付は `date` で読まれる）。"""
    return "" if value is None else str(value)


def write_rule(rule_path: str, rule: Dict[str, Any]) -> None:
    """ルールを YAML で書く。もとのファイルがあれば ``.bak`` に控える。"""
    path = Path(rule_path)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    text = yaml.safe_dump(rule, allow_unicode=True, sort_keys=False)
    path.write_text(text, encoding="utf-8")
