"""人物登録と、検出済みの顔の割り当てを行う GUI。

``scan`` が写真から顔を検出して貯めたあと、ここで人物を作り、顔サムネイルを
選んで人物へ割り当てる。割り当て済みの顔が ``match`` の手本になる。

顔の件数は数万件になりうるので、一覧は必ずページ単位で読む。サムネイルの
BLOB を全件読むと数百MBになり、画面が固まる。
"""

import argparse
import os
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import db, face
from .config import find_settings_path

PAGE_SIZE = 200
THUMBNAIL_SIZE = 120

FILTER_UNASSIGNED = "未割当"
FILTER_AUTO = "自動割当"
FILTER_REJECTED = "除外済み"


def _format_timestamp(value: Optional[str]) -> Optional[str]:
    """DB の ISO 文字列を "YYYY-MM-DD HH:MM:SS" にする。読めなければ ``None``。

    **カメラが `0000:00:00 00:00:00` を書くことがある**（実データで Media 55件・
    顔 33件）。`scanner.extract_exif_datetime` は EXIF を機械的に整形するだけなので、
    これが `0000-00-00T00:00:00` として保存される。日付として読めないものを
    そのまま出すと、**撮影日時を持っているように見えてファイル日時の
    フォールバックも消える**。いちばん手がかりが要る写真で手がかりが減るので、
    持っていないのと同じ扱いにする。
    """
    if not value:
        return None
    text = str(value).replace("T", " ")[:19]
    return None if text.startswith("0000") else text


def resolve_source_root(source_root: Optional[str]) -> Optional[str]:
    """設定に書かれた相対パスを、**設定ファイルの置き場所**を起点に解く。

    カレントディレクトリを起点にすると、リポジトリ直下以外から起動したときに
    相対化が静かに外れ、`GUI_USAGE.md` が約束している「`source_root` からの
    相対」ではなく、読めない NFS の絶対パスに戻る。`config` は設定ファイル
    自体を複数の場所から探しているのに、**その中の値だけ cwd 依存**という
    食い違いだった。

    `database_path` など他の相対値はここでは扱わない。設定の解釈を全体で
    変えるのは、明示的な指示が要る（CLAUDE.md §4）。
    """
    if not source_root or Path(source_root).is_absolute():
        return source_root
    settings_path = find_settings_path()
    if settings_path is None:
        return source_root
    # config/app_settings.json → リポジトリ直下
    return str(settings_path.parent.parent / source_root)


def format_media_info(media: dict, source_root: Optional[str] = None) -> str:
    """プレビューの下に出す、撮影日時とフォルダの説明。

    **年齢を入れるには、その写真がいつ撮られたか分からないといけない。**
    EXIF の撮影日時は実データの 15.8% で欠けているので、日付を持つことが多い
    フォルダ名も併せて出す。

    ``created_time`` は**撮影日時ではない**（コピーで変わる）。取り違えると
    年齢を間違えるので、EXIF が無いときだけ、別の名前で出す。
    """
    lines = []
    shooting_date = _format_timestamp(media.get("shooting_date"))
    if shooting_date:
        lines.append(f"撮影日時: {shooting_date}")
    else:
        lines.append("撮影日時: 不明（EXIFなし）")
        file_time = _format_timestamp(media.get("created_time"))
        if file_time:
            lines.append(f"ファイル日時: {file_time}")

    path = Path(media.get("path", ""))
    folder = path.parent
    if source_root:
        try:
            folder = folder.relative_to(Path(source_root).expanduser().resolve())
        except ValueError:
            # source_root の外にあるメディア。絶対パスのまま出す。
            pass
    if str(folder) == ".":
        # source_root 直下。"." では何のことか読めない。
        folder = "（source_root 直下）"
    lines.append(f"フォルダ: {folder}")
    lines.append(f"ファイル: {path.name}")
    return "\n".join(lines)


def _select_on_focus(spin: QSpinBox) -> QSpinBox:
    """特別な文字（「未設定」など）が入った QSpinBox を、打鍵で置き換えられるようにする。

    `setSpecialValueText` を使うと、入力欄には数字ではなく「未設定」という
    **文字**が入っている。その状態で数字を打つと "未設定5" という文字列になり、
    検証に落ちて**何も起きない。** 利用者からは「▲を押さないと入力できない」
    ように見える。

    開いた時点で全選択しておけば、打った数字がそのまま置き換わる。
    """
    spin.setFocus()
    spin.selectAll()
    return spin


def _make_select_all_on_focus(spin: QSpinBox):
    """フォーカスが入ったら全選択する `focusInEvent` を作る。

    常時フォーカスしてよい入力（ダイアログの主役）と違い、画面に並ぶ入力欄は
    **触られたときだけ**選択したい。
    """
    original = spin.__class__.focusInEvent

    def focus_in(event):
        original(spin, event)
        spin.selectAll()

    return focus_in



class PersonDialog(QDialog):
    def __init__(self, parent=None, name="", relation="", memo=""):
        super().__init__(parent)
        self.setWindowTitle("人物情報")
        self.name_input = QLineEdit(name)
        self.relation_input = QLineEdit(relation)
        self.memo_input = QTextEdit(memo)
        self.ok_button = QPushButton("OK")
        self.ok_button.clicked.connect(self.accept)

        form = QFormLayout()
        form.addRow("名前", self.name_input)
        form.addRow("続柄", self.relation_input)
        form.addRow("メモ", self.memo_input)
        form.addWidget(self.ok_button)
        self.setLayout(form)

    def values(self):
        return (
            self.name_input.text().strip(),
            self.relation_input.text().strip(),
            self.memo_input.toPlainText().strip(),
        )


def summarize_selection(face_count: int, shooting_dates: List[str]) -> str:
    """年齢ダイアログに出す「何に入れるのか」の1行。

    **1回の入力が選択中の全件に入る**のに、プレビューに出ているのは最後に
    選んだ1枚の撮影日時だけ。撮影年をまたいで選ぶと、画面の日時を見て入れた
    年齢が別の年の顔にも入る。件数と、撮影日時の範囲を見せて気づけるようにする。

    1件だけの選択なら、プレビューと食い違わないので出さない。
    """
    if face_count <= 1:
        return ""
    if not shooting_dates:
        return f"{face_count} 件すべてに同じ年齢を入れます（撮影日時は不明）。"
    first, last = shooting_dates[0][:10], shooting_dates[-1][:10]
    if first == last:
        return f"{face_count} 件すべてに同じ年齢を入れます（撮影日時 {first}）。"
    # QLabel は Markdown を解釈しないので、装飾記号を書かない（そのまま出る）。
    return (
        f"{face_count} 件すべてに同じ年齢を入れます。"
        f"\n撮影日時が {first} 〜 {last} にまたがっています。"
    )


class FaceAgeDialog(QDialog):
    """撮影時の年齢を任意で入力する。未設定と0歳は区別する。"""

    def __init__(self, parent=None, summary: str = ""):
        super().__init__(parent)
        self.setWindowTitle("撮影時の年齢")
        self.summary = summary
        self.age_input = QSpinBox()
        # 最小値を -1 にして「未設定」に割り当てる。0 を特別扱いにすると
        # 0歳の顔を登録できなくなる。
        self.age_input.setRange(-1, 150)
        self.age_input.setSpecialValueText("未設定")
        self.age_input.setValue(-1)
        # 「未設定」の文字が入ったままなので、全選択しておかないと
        # キーボードから数字を入れられない（▲を押すしかなくなる）。
        _select_on_focus(self.age_input)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QFormLayout(self)
        if summary:
            summary_label = QLabel(summary)
            summary_label.setWordWrap(True)
            layout.addRow(summary_label)
        layout.addRow("撮影時の年齢", self.age_input)
        layout.addRow(buttons)

    def showEvent(self, event):
        """開くたびに入力欄を選択しておく。

        `__init__` での選択は、ダイアログが表示されるときに解除されることが
        ある。**開いた直後に数字を打てる**ことが大事なので、ここでもやり直す。
        """
        super().showEvent(event)
        _select_on_focus(self.age_input)

    def age(self) -> Optional[int]:
        value = self.age_input.value()
        return None if value < 0 else value


class RegisteredFacesDialog(QDialog):
    """人物に割り当て済みの顔を確認し、確定・解除する。"""

    def __init__(self, parent, connection, person: dict):
        super().__init__(parent)
        self.connection = connection
        self.person = person
        self.setWindowTitle(f"割り当て済みの顔 - {person['name']}")
        self.resize(820, 600)

        self.min_age = QSpinBox()
        self.min_age.setRange(0, 150)
        self.min_age.setSpecialValueText("指定なし")
        self.max_age = QSpinBox()
        self.max_age.setRange(0, 150)
        self.max_age.setSpecialValueText("指定なし")
        self.max_age.setValue(150)
        # 年齢の絞り込みも「指定なし」の文字が入っている。年齢の入力と
        # 同じ理由で、触ったときに打鍵で置き換えられるようにする。
        for spin in (self.min_age, self.max_age):
            spin.focusInEvent = _make_select_all_on_focus(spin)

        self.face_list = QListWidget()
        self.face_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.face_list.setIconSize(QSize(THUMBNAIL_SIZE, THUMBNAIL_SIZE))
        self.face_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.face_list.setUniformItemSizes(True)
        self.face_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)

        self.confirm_button = QPushButton("選択した顔を確定")
        self.unassign_button = QPushButton("割り当てを解除")
        self.age_button = QPushButton("年齢を設定")
        self.confirm_button.setToolTip("自動割当の顔を手動割当に昇格し、match の手本にする")
        self.confirm_button.clicked.connect(self._confirm_selected)
        self.unassign_button.clicked.connect(self._unassign_selected)
        self.age_button.clicked.connect(self._set_age_selected)

        # 割り当て済みの顔も数百件になりうる。ページ単位で読まないと、
        # 1ページ目より後ろの顔に手が届かなくなる。
        self.page = 0
        self.total = 0
        self.prev_button = QPushButton("< 前")
        self.next_button = QPushButton("次 >")
        self.prev_button.clicked.connect(self._previous_page)
        self.next_button.clicked.connect(self._next_page)
        self.page_label = QLabel("-")

        pager = QHBoxLayout()
        pager.addStretch(1)
        pager.addWidget(self.prev_button)
        pager.addWidget(self.page_label)
        pager.addWidget(self.next_button)

        age_filter = QHBoxLayout()
        age_filter.addWidget(QLabel("年齢"))
        age_filter.addWidget(self.min_age)
        age_filter.addWidget(QLabel("歳から"))
        age_filter.addWidget(self.max_age)
        age_filter.addWidget(QLabel("歳"))
        age_filter.addStretch(1)
        self.min_age.valueChanged.connect(self._reset_page)
        self.max_age.valueChanged.connect(self._reset_page)

        actions = QHBoxLayout()
        actions.addWidget(self.confirm_button)
        actions.addWidget(self.unassign_button)
        actions.addWidget(self.age_button)
        actions.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(age_filter)
        layout.addLayout(pager)
        layout.addWidget(self.face_list)
        layout.addLayout(actions)
        self.reload()

    def _age_range(self):
        return (self.min_age.value() or None, self.max_age.value() or None)

    def _reset_page(self) -> None:
        self.page = 0
        self.reload()

    def _previous_page(self) -> None:
        if self.page > 0:
            self.page -= 1
            self.reload()

    def _next_page(self) -> None:
        if (self.page + 1) * PAGE_SIZE < self.total:
            self.page += 1
            self.reload()

    def reload(self) -> None:
        minimum, maximum = self._age_range()
        self.total = db.count_faces(
            self.connection,
            person_id=self.person["id"],
            min_age=minimum,
            max_age=maximum,
        )
        pages = max(1, (self.total + PAGE_SIZE - 1) // PAGE_SIZE)
        self.page = min(self.page, pages - 1)
        records = db.list_faces(
            self.connection,
            person_id=self.person["id"],
            with_thumbnail=True,
            limit=PAGE_SIZE,
            offset=self.page * PAGE_SIZE,
            min_age=minimum,
            max_age=maximum,
        )
        _fill_face_list(self.face_list, records)
        self.page_label.setText(f"{self.page + 1} / {pages} ページ（全 {self.total} 件）")
        self.prev_button.setEnabled(self.page > 0)
        self.next_button.setEnabled(self.page + 1 < pages)

    def _selected_ids(self) -> List[int]:
        return [item.data(Qt.UserRole)["id"] for item in self.face_list.selectedItems()]

    def _confirm_selected(self) -> None:
        face_ids = self._selected_ids()
        if not face_ids:
            return
        db.assign_faces(self.connection, face_ids, self.person["id"], db.ASSIGN_MANUAL)
        self.reload()

    def _unassign_selected(self) -> None:
        face_ids = self._selected_ids()
        if not face_ids:
            return
        db.unassign_faces(self.connection, face_ids)
        self.reload()

    def _set_age_selected(self) -> None:
        face_ids = self._selected_ids()
        if not face_ids:
            return
        dialog = FaceAgeDialog(
            self,
            summary=summarize_selection(
                len(face_ids), db.shooting_dates_for_faces(self.connection, face_ids)
            ),
        )
        if dialog.exec() != QDialog.Accepted:
            return
        age = dialog.age()
        for face_id in face_ids:
            db.set_face_age(self.connection, face_id, age)
        self.reload()


def _fill_face_list(widget: QListWidget, records: List[dict]) -> None:
    widget.clear()
    for record in records:
        pixmap = QPixmap()
        pixmap.loadFromData(record.get("thumbnail") or b"")
        item = QListWidgetItem()
        if not pixmap.isNull():
            item.setIcon(pixmap)
        label = str(record["id"])
        if record.get("assign_source") == db.ASSIGN_AUTO:
            label = f"{label} (自動 {record.get('assign_score') or 0:.0f})"
        if record.get("age") is not None:
            label = f"{label} {record['age']}歳"
        item.setText(label)
        item.setData(Qt.UserRole, record)
        widget.addItem(item)


class MainWindow(QWidget):
    def __init__(self, database_path: str, source_root: Optional[str] = None):
        super().__init__()
        self.db_path = database_path
        #: プレビューのフォルダ表示を相対パスにするためだけに使う。
        #: 無ければ絶対パスで出すので、渡さなくても動く。
        self.source_root = source_root
        self.connection = db.ensure_database(database_path)
        self.setWindowTitle("PhotoArchiveAI 人物登録と顔の割り当て")
        self.page = 0

        # --- 左: 人物 ---------------------------------------------------
        self.person_list = QListWidget()
        self.person_list.currentItemChanged.connect(self._on_person_selected)
        self.add_person_button = QPushButton("人物追加")
        self.edit_person_button = QPushButton("編集")
        self.delete_person_button = QPushButton("削除")
        self.view_faces_button = QPushButton("割り当て済みを確認")
        self.details_label = QLabel("人物を選択してください。")
        self.details_label.setWordWrap(True)

        self.add_person_button.clicked.connect(self._add_person)
        self.edit_person_button.clicked.connect(self._edit_person)
        self.delete_person_button.clicked.connect(self._delete_person)
        self.view_faces_button.clicked.connect(self._view_assigned_faces)

        person_buttons = QHBoxLayout()
        person_buttons.addWidget(self.add_person_button)
        person_buttons.addWidget(self.edit_person_button)
        person_buttons.addWidget(self.delete_person_button)

        person_panel = QWidget()
        person_layout = QVBoxLayout(person_panel)
        person_layout.addLayout(person_buttons)
        person_layout.addWidget(self.person_list)
        person_layout.addWidget(self.view_faces_button)
        person_layout.addWidget(self.details_label)

        # --- 右: 顔 -----------------------------------------------------
        self.filter_box = QComboBox()
        self.filter_box.addItems([FILTER_UNASSIGNED, FILTER_AUTO, FILTER_REJECTED])
        self.filter_box.currentIndexChanged.connect(self._reset_page)

        self.face_list = QListWidget()
        self.face_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.face_list.setIconSize(QSize(THUMBNAIL_SIZE, THUMBNAIL_SIZE))
        self.face_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.face_list.setUniformItemSizes(True)
        self.face_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)

        self.assign_button = QPushButton("選択した顔を割り当て")
        self.reject_button = QPushButton("この顔を除外")
        self.assign_button.clicked.connect(self._assign_selected)
        self.reject_button.clicked.connect(self._reject_selected)

        self.prev_button = QPushButton("< 前")
        self.next_button = QPushButton("次 >")
        self.prev_button.clicked.connect(self._previous_page)
        self.next_button.clicked.connect(self._next_page)
        self.page_label = QLabel("-")

        pager = QHBoxLayout()
        pager.addWidget(self.filter_box)
        pager.addStretch(1)
        pager.addWidget(self.prev_button)
        pager.addWidget(self.page_label)
        pager.addWidget(self.next_button)

        face_actions = QHBoxLayout()
        face_actions.addWidget(self.assign_button)
        face_actions.addWidget(self.reject_button)
        face_actions.addStretch(1)

        self.preview_label = QLabel("顔を選ぶと元写真から切り出して表示します。")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumWidth(320)
        self.preview_label.setWordWrap(True)
        self.preview_label.setStyleSheet("border: 1px solid #999; padding: 8px;")

        # 撮影日時とフォルダ。**年齢を入れるのに要る。**
        # QLabel は画像か文字のどちらかしか持てないので、もう1枚に分ける。
        self.preview_info = QLabel("")
        self.preview_info.setWordWrap(True)
        self.preview_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.preview_info.setStyleSheet("padding: 4px;")

        preview_panel = QWidget()
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.addWidget(self.preview_label, 1)
        preview_layout.addWidget(self.preview_info, 0)
        self.face_list.itemSelectionChanged.connect(self._show_preview)

        face_area = QSplitter(Qt.Horizontal)
        face_area.addWidget(self.face_list)
        face_area.addWidget(preview_panel)
        face_area.setStretchFactor(0, 3)
        face_area.setStretchFactor(1, 2)

        face_panel = QWidget()
        face_layout = QVBoxLayout(face_panel)
        face_layout.addLayout(pager)
        face_layout.addWidget(face_area)
        face_layout.addLayout(face_actions)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(person_panel)
        splitter.addWidget(face_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([280, 780])

        main_layout = QVBoxLayout(self)
        main_layout.addWidget(splitter)
        self.resize(1100, 720)

        self._reload_person_list()
        self.reload_faces()

    # ------------------------------------------------------------------
    # 人物
    # ------------------------------------------------------------------

    def _reload_person_list(self):
        self.person_list.clear()
        for person in db.list_persons(self.connection):
            item = QListWidgetItem(f"{person['name']} ({person.get('relation') or '-'})")
            item.setData(Qt.UserRole, person)
            self.person_list.addItem(item)
        self.details_label.setText("人物を選択してください。")

    def _current_person(self) -> Optional[dict]:
        item = self.person_list.currentItem()
        return None if item is None else item.data(Qt.UserRole)

    def _on_person_selected(self, current: QListWidgetItem, previous: QListWidgetItem = None):
        if current is None:
            self.details_label.setText("人物を選択してください。")
            return
        person = current.data(Qt.UserRole)
        manual = db.count_faces(
            self.connection, assign_source=db.ASSIGN_MANUAL, person_id=person["id"]
        )
        auto = db.count_faces(self.connection, assign_source=db.ASSIGN_AUTO, person_id=person["id"])
        self.details_label.setText(
            f"名前: {person['name']}\n"
            f"続柄: {person.get('relation') or '-'}\n"
            f"メモ: {person.get('memo') or '-'}\n"
            f"手動割当: {manual} 件 / 自動割当: {auto} 件"
        )

    def _add_person(self):
        dialog = PersonDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        name, relation, memo = dialog.values()
        if not name:
            QMessageBox.warning(self, "入力エラー", "名前は必須です。")
            return
        db.add_person(self.connection, name, relation, memo)
        self._reload_person_list()

    def _edit_person(self):
        person = self._current_person()
        if person is None:
            return
        dialog = PersonDialog(
            self, person["name"], person.get("relation") or "", person.get("memo") or ""
        )
        if dialog.exec() != QDialog.Accepted:
            return
        name, relation, memo = dialog.values()
        if not name:
            QMessageBox.warning(self, "入力エラー", "名前は必須です。")
            return
        db.update_person(self.connection, person["id"], name, relation, memo)
        self._reload_person_list()

    def _delete_person(self):
        person = self._current_person()
        if person is None:
            return
        if QMessageBox.question(
            self, "削除確認", f"{person['name']} を削除しますか？\n割り当て済みの顔は未割当に戻ります。"
        ) != QMessageBox.Yes:
            return
        db.delete_person(self.connection, person["id"])
        self._reload_person_list()
        self.reload_faces()

    def _view_assigned_faces(self):
        person = self._current_person()
        if person is None:
            QMessageBox.information(self, "選択なし", "先に人物を選択してください。")
            return
        if db.count_faces(self.connection, person_id=person["id"]) == 0:
            QMessageBox.information(self, "割当なし", "この人物に割り当てられた顔はありません。")
            return
        dialog = RegisteredFacesDialog(self, self.connection, person)
        dialog.exec()
        self._on_person_selected(self.person_list.currentItem())
        self.reload_faces()

    # ------------------------------------------------------------------
    # 顔一覧
    # ------------------------------------------------------------------

    def _filter_arguments(self) -> dict:
        selected = self.filter_box.currentText()
        if selected == FILTER_AUTO:
            return {"assign_source": db.ASSIGN_AUTO}
        if selected == FILTER_REJECTED:
            return {"assign_source": db.ASSIGN_REJECTED}
        return {"unassigned": True}

    def _reset_page(self):
        self.page = 0
        self.reload_faces()

    def reload_faces(self):
        filters = self._filter_arguments()
        total = db.count_faces(self.connection, **filters)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        self.page = max(0, min(self.page, pages - 1))
        records = db.list_faces(
            self.connection,
            with_thumbnail=True,
            limit=PAGE_SIZE,
            offset=self.page * PAGE_SIZE,
            **filters,
        )
        _fill_face_list(self.face_list, records)
        self.page_label.setText(f"{self.page + 1} / {pages} ページ（全 {total} 件）")
        self.prev_button.setEnabled(self.page > 0)
        self.next_button.setEnabled(self.page < pages - 1)

    def _previous_page(self):
        self.page = max(0, self.page - 1)
        self.reload_faces()

    def _next_page(self):
        self.page += 1
        self.reload_faces()

    def _selected_face_ids(self) -> List[int]:
        return [item.data(Qt.UserRole)["id"] for item in self.face_list.selectedItems()]

    def _show_preview(self) -> None:
        """選択中の顔を元写真から切り出して大きく表示する。

        サムネイルは160pxまで縮めてあるので、確認には元画像から取り直す。
        """
        items = self.face_list.selectedItems()
        if not items:
            return
        record = db.get_face(self.connection, items[-1].data(Qt.UserRole)["id"])
        if not record:
            return
        media = db.get_media_by_id(self.connection, record["media_id"])
        if not media:
            return
        # 情報は画像より先に出す。**元写真が開けないときこそ、
        # どのフォルダのどのファイルなのかが要る。**
        self.preview_info.setText(format_media_info(media, self.source_root))
        bbox = (
            record["bbox_top"],
            record["bbox_right"],
            record["bbox_bottom"],
            record["bbox_left"],
        )
        try:
            data = face.load_face_image_bytes(Path(media["path"]), bbox)
        except Exception as error:
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText(f"元写真を開けません:\n{media['path']}\n{error}")
            return
        pixmap = QPixmap()
        pixmap.loadFromData(data)
        if pixmap.isNull():
            # **前の写真の画像を残さない。** 情報欄は先に新しい写真で
            # 上書きしているので、残すと上下で別の写真になる。
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText(f"画像を表示できません:\n{media['path']}")
            return
        self.preview_label.setText("")
        self.preview_label.setPixmap(
            pixmap.scaled(
                self.preview_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        )

    def assign_faces(self, face_ids: List[int], person_id: int, age=db.KEEP_AGE) -> int:
        """選択した顔を人物へ手動で割り当てる。テストからも直接呼ぶ。

        ``age`` を省くと年齢は触らない。``None`` は「未設定」の指示。
        """
        return db.assign_faces(self.connection, face_ids, person_id, db.ASSIGN_MANUAL, age=age)

    def _assign_selected(self):
        person = self._current_person()
        if person is None:
            QMessageBox.information(self, "選択なし", "先に人物を選択してください。")
            return
        face_ids = self._selected_face_ids()
        if not face_ids:
            QMessageBox.information(self, "選択なし", "割り当てる顔を選択してください。")
            return
        dialog = FaceAgeDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        self.assign_faces(face_ids, person["id"], dialog.age())
        self.reload_faces()
        self._on_person_selected(self.person_list.currentItem())

    def _reject_selected(self):
        face_ids = self._selected_face_ids()
        if not face_ids:
            return
        db.reject_faces(self.connection, face_ids)
        self.reload_faces()


def main() -> None:
    import sys

    parser = argparse.ArgumentParser(prog="photoarchive-gui")
    parser.add_argument(
        "--db",
        help="SQLite database path. 省略すると config/app_settings.json の database_path を使う。",
    )
    args = parser.parse_args()

    # CLI と同じ解決順にする。GUI だけ設定ファイルを読まないと、
    # 「アプリケーション内にDBパスをハードコードしない」という方針から外れる。
    from .config import get_database_path, get_source_root, load_settings

    settings = load_settings()
    db_path = args.db or get_database_path(settings)
    # プレビューのフォルダを相対パスで出すためだけに使う。無くても動く。
    source_root = resolve_source_root(get_source_root(settings))
    if not db_path:
        raise SystemExit(
            "データベースのパスが必要です。--db で指定するか、"
            "config/app_settings.json の database_path を設定してください。"
        )

    from PySide6.QtCore import QLibraryInfo

    # OpenCV may register its own Qt plugins first; use the PySide6 plugins for this GUI.
    os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = QLibraryInfo.path(
        QLibraryInfo.LibraryPath.PluginsPath
    )
    app = QApplication([])
    window = MainWindow(db_path, source_root=source_root)
    window.show()
    sys.exit(app.exec())
