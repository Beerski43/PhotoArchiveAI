"""GUI の「写真の選定」タブ（#104）。`select` の重みを動かして、結果をサムネイルで見る。

**なぜ要るか**（#102）。`select` の結果に利用者が満足していない。並びの式と重みが
コードに固定されていて、好みを確かめるたびに Issue・PR・実データの複製での測定が
要っていた。**重みを利用者が動かして、その場で結果を見る**ための画面。

動き方は `selection` の2段に合わせる（#103）。

- 「候補を読み込む」= `selection.prepare`（見え方・表情の未計測分を測る。重い）
- 「並べ直す」= `selection.choose`（重みで点を出し、並べ、束ね、年ごとに取る。
  実データで約1秒。重みを変えた直後は、新しく上位に来た写真の見た目を NFS から測る）

**値を1つ動かすたびに自動で並べ直さない。** 数秒かかることがあり、続けて動かすと
重なるため。ボタンで並べ直す。

設定の初期値はルールファイル（`config/rule.yml`）から読み、「ルールを保存」で書き戻す。
CLI の `photoarchive select` は同じルールで同じ結果になる。
"""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import yaml
from PIL import Image, ImageOps
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
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
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import selection, similar

#: 結果の1ページの枚数。**ページャの無い一覧を作らない**（CLAUDE.md §8）。
RESULT_PAGE_SIZE = 60
#: 結果のサムネイルの大きさ（長辺）。
RESULT_THUMBNAIL = 180

#: 重みの項目と、画面に出す名前・刻み・上限。順は `scoring.Weights` と同じ。
WEIGHT_FIELDS = (
    ("sharpness", "鮮明さ（ボケていない）", 0.05),
    ("frontal", "正面を向いている", 0.05),
    ("eyes", "目を開けている", 0.05),
    ("smile", "笑顔", 0.05),
    ("member_bonus", "家族1人ごとの加点", 1.0),
    ("stranger_penalty", "減点する顔の減点（除外した顔のうち選んだ顔）", 1.0),
    # 写真全体（#106・`photoarchive assess` で測る）
    ("technical", "画質（ぶれ・ピンぼけ・暗さ）", 0.05),
    ("aesthetic", "美しさ（構図）", 0.05),
    ("highlights", "白飛びの減点", 0.05),
    ("shadows", "黒つぶれの減点", 0.05),
    ("cutoff", "顔の見切れの減点", 0.05),
)
#: 重みの入力の桁と上限。**ルールの値を丸めない・頭打ちにしない**（PR #108 のレビュー指摘3。
#: 画面の値が CLI と黙ってずれ、保存すると丸めた値が残っていた）。`load_rule` は 0 以上なら受け付ける。
WEIGHT_DECIMALS = 4
WEIGHT_MAXIMUM = 1_000_000.0

#: 真偽値のキーと、画面に出す名前。既定は `selection.RULE_FLAGS`。
FLAG_FIELDS = (
    ("family_only", "家族の写った写真だけ"),
    ("include_auto_assigned", "自動割り当ての顔も家族として数える"),
    ("remove_similar", "似た写真・写しを1枚にまとめる"),
    ("remove_duplicate", "同じファイル（ハッシュ）を1枚にまとめる"),
    ("include_video", "動画を含める"),
)

#: 似た写真の判定の数（`selection.RULE_NUMBERS`）と、画面に出す名前・上限。
NUMBER_FIELDS = (
    ("similar_distance", "似た写真の見た目の距離（連写）", 64),
    ("similar_far_distance", "似た写真の見た目の距離（時刻の離れた2枚）", 64),
    ("copy_distance", "写しとみなす見た目の距離", 64),
    ("similar_seconds", "連写とみなす撮影の間隔（秒）", 3600),
)

#: 点の内訳の項目と名前（`scoring.photo_breakdown`）。
BREAKDOWN_LABELS = (
    ("sharpness", "鮮明さ"),
    ("frontal", "正面"),
    ("eyes", "目"),
    ("smile", "笑顔"),
    ("members", "家族の人数"),
    ("close", "親しい人"),
    ("strangers", "減点する顔"),
    ("technical", "画質"),
    ("aesthetic", "美しさ"),
    ("highlights", "白飛び"),
    ("shadows", "黒つぶれ"),
    ("cutoff", "顔の見切れ"),
)

YEAR_UNKNOWN = "年不明"
#: 年の表の「年以降」の行の印（`selection.year_limits` の ``2027+``）。
ROW_LATER = "later"
STALE_NOTICE = "顔の割り当てを変えたら「候補を読み込む」を押し直してください。"


def later_year() -> int:
    """「〜年以降」の行の年（今年の翌年）。年が変わったら設定し直す運用（利用者の決定・PR #108）。"""
    return date.today().year + 1


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
        self.rule_path = rule_path
        self.output_root = output_root
        self.source_roots = list(source_roots or [])
        self.rule: Dict[str, Any] = self._read_rule()
        self.candidates: Optional[selection.Candidates] = None
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
        self.weight_spins: Dict[str, QDoubleSpinBox] = {}
        for key, label, step in WEIGHT_FIELDS:
            spin = QDoubleSpinBox()
            spin.setRange(0.0, WEIGHT_MAXIMUM)
            spin.setDecimals(WEIGHT_DECIMALS)
            spin.setSingleStep(step)
            self.weight_spins[key] = spin
            weights_form.addRow(label, spin)
        layout.addWidget(weights_box)

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
        self.number_spins: Dict[str, QSpinBox] = {}
        for key, label, maximum in NUMBER_FIELDS:
            spin = QSpinBox()
            spin.setRange(0, maximum)
            self.number_spins[key] = spin
            numbers_form.addRow(label, spin)
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
        self.fill_spin = QSpinBox()
        self.fill_spin.setRange(0, 9999)
        self.fill_spin.setValue(30)
        self.fill_button = QPushButton("全部の年に入れる")
        self.fill_button.clicked.connect(self.fill_all_years)
        fill_row.addWidget(self.fill_spin)
        fill_row.addWidget(self.fill_button)
        years_layout.addLayout(fill_row)
        layout.addWidget(years_box)

        self.load_button = QPushButton("候補を読み込む")
        self.load_button.clicked.connect(self.load_candidates)
        self.choose_button = QPushButton("並べ直す")
        self.choose_button.clicked.connect(self.choose)
        self.save_button = QPushButton("ルールを保存")
        self.save_button.clicked.connect(self.save_rule)
        self.export_button = QPushButton("出力する")
        self.export_button.clicked.connect(self.export)
        for button in (self.load_button, self.choose_button, self.save_button, self.export_button):
            layout.addWidget(button)
        self.status = QLabel(STALE_NOTICE)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        return scroll

    def _build_results(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        top = QHBoxLayout()
        self.year_combo = QComboBox()
        self.year_combo.currentIndexChanged.connect(self._year_changed)
        self.previous_button = QPushButton("前へ")
        self.previous_button.clicked.connect(lambda: self._turn(-1))
        self.next_button = QPushButton("次へ")
        self.next_button.clicked.connect(lambda: self._turn(1))
        self.page_label = QLabel("")
        top.addWidget(QLabel("年"))
        top.addWidget(self.year_combo, 1)
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
        layout.addWidget(self.result_list, 1)

        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.detail)
        return panel

    # ------------------------------------------------------------------
    # ルールと画面の行き来
    # ------------------------------------------------------------------

    def _read_rule(self) -> Dict[str, Any]:
        if not self.rule_path or not Path(self.rule_path).exists():
            return {}
        try:
            return selection.load_rule(self.rule_path)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "ルールを読めません", str(error))
            return {}

    def _show_rule(self, rule: Dict[str, Any]) -> None:
        weights = selection.rule_weights(rule)
        for key, spin in self.weight_spins.items():
            spin.setValue(getattr(weights, key))
        for key, check in self.flag_checks.items():
            check.setChecked(selection._rule_flag(rule, key))
        for key, spin in self.number_spins.items():
            spin.setValue(int(selection._rule_number(rule, key, selection.RULE_NUMBERS[key])))

    def current_rule(self) -> Dict[str, Any]:
        """画面の設定から作ったルール。画面に出していないキー（`date` など）は元のルールのまま。"""
        rule = dict(self.rule)
        rule["weights"] = {key: spin.value() for key, spin in self.weight_spins.items()}
        for key, check in self.flag_checks.items():
            rule[key] = check.isChecked()
        for key, spin in self.number_spins.items():
            rule[key] = spin.value()
        if self.year_table.rowCount():
            counts: Dict[Any, int] = {}
            for key, value in self._year_values().items():
                if key == selection.COUNT_UNKNOWN:
                    counts[selection.COUNT_UNKNOWN] = value
                elif key == ROW_LATER:
                    counts[f"{later_year()}{selection.LATER_SUFFIX}"] = value
                else:
                    counts[int(key)] = value
            rule["count_per_year"] = counts
        return rule

    def _year_values(self) -> Dict[Any, int]:
        """いま表に入っている枚数（``{年 / "unknown" / "later": 枚数}``）。"""
        values: Dict[Any, int] = {}
        for row in range(self.year_table.rowCount()):
            key = self.year_table.item(row, 0).data(Qt.UserRole)
            values[key] = self.year_table.cellWidget(row, 2).value()
        return values

    def _fill_year_table(self) -> None:
        """候補の年ごとの枚数を表に出す。**入っている値は引き継ぐ**（PR #108 のレビュー指摘1）。

        行は、候補のある年・**年不明**・**翌年以降**（利用者の決定・PR #108。これから増える年は
        ここで決め、年が変わったら設定し直す）。新しく現れた行の初期値はルールファイルから
        （年ごとの対応なら ``unknown`` / ``2027+`` も）。ファイルに枚数が無ければ候補の枚数。
        """
        previous = self._year_values()
        counts = selection.candidate_counts(self.candidates, self.current_rule_without_years())
        limit_of = selection.year_limits(self.rule)
        rows = [(year, year_label(year), counts[year]) for year in sorted(y for y in counts if y is not None)]
        rows.append((selection.COUNT_UNKNOWN, YEAR_UNKNOWN, counts.get(None, 0)))
        rows.append((ROW_LATER, f"{later_year()} 年以降", None))
        self.year_table.setRowCount(0)
        for key, label, candidates in rows:
            row = self.year_table.rowCount()
            self.year_table.insertRow(row)
            item = QTableWidgetItem(label)
            item.setData(Qt.UserRole, key)
            self.year_table.setItem(row, 0, item)
            self.year_table.setItem(row, 1, QTableWidgetItem("" if candidates is None else str(candidates)))
            spin = QSpinBox()
            spin.setRange(0, 99_999)
            if key in previous:
                spin.setValue(previous[key])
            elif limit_of is not None:
                year = None if key == selection.COUNT_UNKNOWN else (
                    later_year() if key == ROW_LATER else key
                )
                spin.setValue(limit_of(year) or 0)
            else:
                spin.setValue(candidates or 0)
            self.year_table.setCellWidget(row, 2, spin)

    def _filters_changed(self, _checked: bool) -> None:
        """絞り込みを変えたら年の表を作り直す（候補の枚数・並ぶ年が変わる）。

        読み込んだ候補に無い種別（自動割り当て）を求めるときは何もしない。「並べ直す」が
        読み込み直し、そこで作り直す（入っている値は引き継ぐ）。
        """
        if self.candidates is None:
            return
        sources = selection.family_sources(self.current_rule_without_years())
        if set(sources) <= set(self.candidates.assign_sources):
            self._fill_year_table()

    def current_rule_without_years(self) -> Dict[str, Any]:
        rule = dict(self.rule)
        for key, check in self.flag_checks.items():
            rule[key] = check.isChecked()
        rule.pop("count_per_year", None)
        return rule

    def fill_all_years(self) -> None:
        """全部の行（年不明・翌年以降も）に同じ数を入れる。"""
        for row in range(self.year_table.rowCount()):
            self.year_table.cellWidget(row, 2).setValue(self.fill_spin.value())

    # ------------------------------------------------------------------
    # 実行
    # ------------------------------------------------------------------

    def load_candidates(self) -> None:
        """`selection.prepare`。見え方・表情の未計測分を測る（初回は数分かかることがある）。"""
        rule = self.current_rule_without_years()
        progress = _Progress(self, "候補を読み込んでいます")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.candidates = selection.prepare(
                self.connection,
                selection.family_sources(rule),
                self.source_roots,
                progress,
            )
            self._fill_year_table()
        finally:
            QApplication.restoreOverrideCursor()
            progress.finish()
        # **前の結果を捨てる**（PR #108 のレビュー指摘2）。結果は読み込んだ候補に結びついている。
        # 残すと、古い写真の内訳が新しい割り当てで出たり（種別が足りなければ例外）、
        # 「出力する」が古い割り当てで選んだ結果をコピーしたりする。
        self.selected = []
        self.chosen_rule = {}
        self._group_by_year()
        self.status.setText(f"候補を読み込みました。{STALE_NOTICE}")

    def choose(self) -> None:
        """`selection.choose`。設定が読めない値なら止める。"""
        if self.candidates is None or (
            set(selection.family_sources(self.current_rule_without_years()))
            - set(self.candidates.assign_sources)
        ):
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
        self.status.setText(f"{len(self.selected)} 枚を選びました。{STALE_NOTICE}")

    def _group_by_year(self) -> None:
        self.by_year = {}
        for media in self.selected:
            self.by_year.setdefault(selection._get_media_year(media), []).append(media)
        self.year_combo.blockSignals(True)
        self.year_combo.clear()
        for year in sorted(self.by_year, key=lambda y: (y is None, y or 0)):
            self.year_combo.addItem(f"{year_label(year)}（{len(self.by_year[year])} 枚）", year)
        self.year_combo.blockSignals(False)
        self.page = 0
        self._show_page()

    def _year_changed(self, _index: int) -> None:
        self.page = 0
        self._show_page()

    def _current_year_media(self) -> List[Dict[str, Any]]:
        if self.year_combo.count() == 0:
            return []
        return self.by_year.get(self.year_combo.currentData(), [])

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
        self.detail.setText("")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for media in page:
                item = QListWidgetItem(self._thumbnail_icon(media), Path(media["path"]).name)
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

    def _item_changed(self, current, _previous) -> None:
        if current is None or self.candidates is None:
            self.detail.setText("")
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

    # ------------------------------------------------------------------
    # 保存と出力
    # ------------------------------------------------------------------

    def save_rule(self) -> None:
        """ルールファイルに書く。**書く前に控え（``.bak``）を取る。** YAML のコメントは消える。"""
        if not self.rule_path:
            QMessageBox.warning(self, "保存できません", "設定に rule_path がありません。")
            return
        rule = self.current_rule()
        try:
            selection.rule_weights(rule)
            selection.year_limits(rule)
        except ValueError as error:
            QMessageBox.warning(self, "設定を読めません", str(error))
            return
        answer = QMessageBox.question(
            self,
            "ルールを保存",
            f"{self.rule_path} に書きます。もとのファイルは {self.rule_path}.bak に控えます。"
            "ファイル内のコメントは消えます。",
        )
        if answer != QMessageBox.Yes:
            return
        write_rule(self.rule_path, rule)
        self.rule = rule
        self.status.setText(f"ルールを保存しました: {self.rule_path}")

    def export(self) -> None:
        """選んだ写真を出力先へコピーする（CLI の `select` と同じ。前の出力は消える）。"""
        if not self.selected:
            QMessageBox.information(self, "出力", "先に「並べ直す」で写真を選んでください。")
            return
        if not self.output_root:
            QMessageBox.warning(self, "出力できません", "設定に output_root がありません。")
            return
        answer = QMessageBox.question(
            self,
            "出力",
            f"{len(self.selected)} 枚を {self.output_root} にコピーします。前の出力は消えます。",
        )
        if answer != QMessageBox.Yes:
            return
        progress = _Progress(self, "コピーしています")
        try:
            copied = selection.copy_selected_media(
                self.selected, self.output_root, self.source_roots, progress
            )
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "出力が止まりました", str(error))
            return
        finally:
            progress.finish()
        self.status.setText(f"{copied} 枚を {self.output_root} にコピーしました。")


def write_rule(rule_path: str, rule: Dict[str, Any]) -> None:
    """ルールを YAML で書く。もとのファイルがあれば ``.bak`` に控える。"""
    path = Path(rule_path)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    text = yaml.safe_dump(rule, allow_unicode=True, sort_keys=False)
    path.write_text(text, encoding="utf-8")
