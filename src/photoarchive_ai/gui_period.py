"""撮影時期の範囲を「年」と「月」の欄で選ぶ部品（#134）。顔の割り当てと写真の選定の両方のタブで使う。

**利用者の要望**（2026-11-05）: 「年フィルタだが、[年]と[月]で入力欄を分けてほしい。また、どっちが
古くて、どっちが新しいのか見た目的にわからない」。そこで次の形にしてある。

- 欄の前に「古い方」「新しい方」と書く。年は両方の欄で古い順に並べる
- 月を `指定なし` にすると、古い方はその年の1月から、新しい方はその年の12月まで
- 年を `指定なし` にすると、その側の限りが無くなる（月は選べない）
- 並ぶのは渡された年と月だけ（`set_months`）。**選んでも1件も出ない月を並べない**
- **古い方を入れたら、新しい方にはそれ以降の年月だけが並ぶ**（利用者の要望・2026-11-05）。新しい方が
  古い方より前になっていれば押し上げる。黙って0件にすると、絞り込みが壊れているように見える

範囲は ``YYYY-MM`` の文字列で返す（`db` の ``month_from`` / ``month_to`` と同じ形・両端を含む）。
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QWidget

ANY = "指定なし"
OLD_LABEL = "古い方"
NEW_LABEL = "新しい方"


class YearMonthRange(QWidget):
    """``古い方 [年]年 [月]月 〜 新しい方 [年]年 [月]月``。"""

    #: 利用者が欄を動かしたとき・`set_range` のあとに1回。`set_months` では出さない。
    changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._months: Dict[int, List[int]] = {}
        self._year_labels: Dict[int, str] = {}
        self.old_year, self.old_month = QComboBox(), QComboBox()
        self.new_year, self.new_month = QComboBox(), QComboBox()
        self.old_year.setToolTip("この年から（含む）。指定なしなら古い側の限りを決めない")
        self.old_month.setToolTip("この月から（含む）。指定なしならその年の1月から")
        self.new_year.setToolTip("この年まで（含む）。指定なしなら新しい側の限りを決めない")
        self.new_month.setToolTip("この月まで（含む）。指定なしならその年の12月まで")

        # 既定は最初に見せたときの中身で幅が決まり、あとから入れた年（枚数つき）の文字が切れる
        for box in (self.old_year, self.old_month, self.new_year, self.new_month):
            box.setSizeAdjustPolicy(QComboBox.AdjustToContents)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        for widget in (
            QLabel(OLD_LABEL), self.old_year, QLabel("年"), self.old_month, QLabel("月"),
            QLabel("〜"),
            QLabel(NEW_LABEL), self.new_year, QLabel("年"), self.new_month, QLabel("月"),
        ):
            layout.addWidget(widget)

        self._fill_all({})
        self.old_year.currentIndexChanged.connect(self._old_year_moved)
        self.old_month.currentIndexChanged.connect(self._old_month_moved)
        self.new_year.currentIndexChanged.connect(self._new_year_moved)
        self.new_month.currentIndexChanged.connect(self.changed)

    # ------------------------------------------------------------------
    # 値の出し入れ
    # ------------------------------------------------------------------

    def set_months(
        self, months: Iterable[str], year_labels: Optional[Dict[int, str]] = None
    ) -> None:
        """選べる年月（``YYYY-MM``）を入れ替える。**いま選んでいる値は、残っていれば保つ。**

        ``year_labels`` は年の欄に出す文字（``{2019: "2019（30 枚）"}``）。ここにある年は、月が1つも
        無くても年の欄に並ぶ（年までしか分からない写真の年）。
        """
        found: Dict[int, set] = {year: set() for year in (year_labels or {})}
        for text in months:
            found.setdefault(int(text[:4]), set()).add(int(text[5:7]))
        self._months = {year: sorted(values) for year, values in found.items()}
        self._year_labels = dict(year_labels or {})
        self._fill_all({
            self.old_year: (self.old_year.currentData(), self.old_month.currentData()),
            self.new_year: (self.new_year.currentData(), self.new_month.currentData()),
        })

    def set_range(self, start: Optional[str], end: Optional[str]) -> None:
        """範囲を選ぶ（``YYYY`` か ``YYYY-MM``・``None`` は指定なし）。無い値は `ValueError`。

        新しい方が古い方より前なら、画面の操作と同じく古い方まで押し上げる。
        """
        wanted: Dict[QComboBox, Tuple[Optional[int], Optional[int]]] = {}
        for year_box, text in ((self.old_year, start), (self.new_year, end)):
            year = None if text is None else int(text[:4])
            month = int(text[5:7]) if text and len(text) > 4 else None
            if year is not None and (
                year not in self._months or (month is not None and month not in self._months[year])
            ):
                raise ValueError(f"選べない年月です: {text}")
            wanted[year_box] = (year, month)
        self._fill_all(wanted)
        self.changed.emit()

    def bounds(self) -> Tuple[Optional[str], Optional[str]]:
        """``(古い方, 新しい方)``（``YYYY-MM``・両端を含む）。指定なしの側は ``None``。"""
        old_year, new_year = self.old_year.currentData(), self.new_year.currentData()
        return (
            None if old_year is None else f"{old_year:04d}-{self.old_month.currentData() or 1:02d}",
            None if new_year is None else f"{new_year:04d}-{self.new_month.currentData() or 12:02d}",
        )

    # ------------------------------------------------------------------
    # 欄の中身
    # ------------------------------------------------------------------

    def _fill_all(self, wanted: Dict[QComboBox, Tuple[Optional[int], Optional[int]]]) -> None:
        """4つの欄を作り直す。信号は出さない。"""
        year, month = wanted.get(self.old_year, (None, None))
        self._fill_box(self.old_year, sorted(self._months), year, self._year_label)
        self._fill_old_months(month)
        self._fill_new(*wanted.get(self.new_year, (None, None)))

    def _fill_box(self, box: QComboBox, values: Iterable[int], keep: Optional[int], label=str) -> None:
        blocked = box.blockSignals(True)
        box.clear()
        box.addItem(ANY, None)
        for value in values:
            box.addItem(label(value), value)
        box.setCurrentIndex(max(0, box.findData(keep)))
        box.blockSignals(blocked)

    def _year_label(self, year: int) -> str:
        return self._year_labels.get(year, str(year))

    def _fill_old_months(self, keep: Optional[int]) -> None:
        year = self.old_year.currentData()
        self._fill_box(self.old_month, self._months.get(year, ()), keep)
        self.old_month.setEnabled(year is not None)

    def _fill_new(self, year: Optional[int], month: Optional[int]) -> None:
        """新しい方の欄を、**古い方以降の年月だけ**で作り直す（利用者の要望・2026-11-05）。

        古い方より前の値は古い方まで押し上げる。指定なしのままにすると、狭めたつもりの範囲が黙って
        広がる。月の指定なし（その年の12月まで）は古い方より前にならないので、そのまま。
        """
        old_year, old_month = self.old_year.currentData(), self.old_month.currentData()
        if old_year is not None and year is not None and year < old_year:
            year, month = old_year, old_month
        same_year = old_year is not None and year == old_year
        if same_year and old_month is not None and month is not None and month < old_month:
            month = old_month
        self._fill_box(
            self.new_year,
            [value for value in sorted(self._months) if old_year is None or value >= old_year],
            year,
            self._year_label,
        )
        first = old_month if same_year and old_month is not None else 1
        self._fill_box(
            self.new_month, [value for value in self._months.get(year, ()) if value >= first], month
        )
        self.new_month.setEnabled(year is not None)

    # ------------------------------------------------------------------
    # 利用者が動かしたとき
    # ------------------------------------------------------------------

    def _old_year_moved(self) -> None:
        self._fill_old_months(self.old_month.currentData())
        self._old_month_moved()

    def _old_month_moved(self) -> None:
        self._fill_new(self.new_year.currentData(), self.new_month.currentData())
        self.changed.emit()

    def _new_year_moved(self) -> None:
        self._fill_new(self.new_year.currentData(), self.new_month.currentData())
        self.changed.emit()
