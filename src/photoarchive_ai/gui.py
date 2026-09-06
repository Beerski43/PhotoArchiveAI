import argparse
import io
import os
from pathlib import Path
from typing import List, Optional

import numpy as np
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QFileDialog,
    QMessageBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTextEdit,
)

from . import db
from .analyzer import detect_faces_in_file, compute_face_embedding, _read_image


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
        return self.name_input.text().strip(), self.relation_input.text().strip(), self.memo_input.toPlainText().strip()


class FaceAgeDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("登録顔の年齢")
        self.age_input = QSpinBox()
        self.age_input.setRange(0, 150)
        self.age_input.setSpecialValueText("未設定")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QFormLayout(self)
        layout.addRow("撮影時の年齢", self.age_input)
        layout.addRow(buttons)

    def age(self) -> Optional[int]:
        return self.age_input.value() or None


class FaceSelectionDialog(QDialog):
    def __init__(self, parent, image_path: str, face_locations: List[tuple]):
        super().__init__(parent)
        self.setWindowTitle("登録する顔を選択")
        self.selected_index: Optional[int] = None
        layout = QVBoxLayout()
        self.labels = []
        self.image_path = image_path
        self.face_locations = face_locations

        for index, location in enumerate(face_locations):
            label = QLabel(f"顔 {index + 1}")
            label.setAlignment(Qt.AlignCenter)
            face_image = self._crop_face(image_path, location)
            pixmap = QPixmap.fromImage(face_image)
            label.setPixmap(pixmap.scaled(160, 160, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            button = QPushButton(f"選択 {index + 1}")
            button.clicked.connect(lambda checked, idx=index: self._set_selection(idx))
            layout.addWidget(label)
            layout.addWidget(button)
        self.setLayout(layout)

    def _crop_face(self, image_path: str, face_location: tuple):
        image = Image.open(image_path).convert("RGB")
        top, right, bottom, left = face_location
        crop = image.crop((left, top, right, bottom))
        data = io.BytesIO()
        crop.save(data, format="PNG")
        qimage = QPixmap()
        qimage.loadFromData(data.getvalue(), "PNG")
        return qimage.toImage()

    def _set_selection(self, index: int) -> None:
        self.selected_index = index
        self.accept()


class ImageFileDialog(QFileDialog):
    def __init__(self, parent=None):
        super().__init__(parent, "顔画像を選択")
        self.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        self.setFileMode(QFileDialog.FileMode.ExistingFile)
        self.setNameFilter("Images (*.jpg *.jpeg *.png *.bmp *.heic *.heif)")
        self.setViewMode(QFileDialog.ViewMode.List)

        self.preview_label = QLabel("画像を選択してください")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumSize(240, 180)
        self.preview_label.setWordWrap(True)
        self.preview_label.setStyleSheet("border: 1px solid #999; padding: 8px;")
        self.preview_label.setToolTip("選択中の画像プレビュー")

        layout = self.layout()
        file_view = self.findChild(QSplitter, "splitter")
        if isinstance(layout, QGridLayout) and file_view is not None:
            layout.removeWidget(file_view)
            self.preview_splitter = QSplitter(Qt.Horizontal, self)
            self.preview_splitter.setObjectName("previewSplitter")
            self.preview_splitter.addWidget(file_view)
            self.preview_splitter.addWidget(self.preview_label)
            self.preview_splitter.setStretchFactor(0, 3)
            self.preview_splitter.setStretchFactor(1, 2)
            self.preview_splitter.setSizes([640, 360])
            layout.addWidget(self.preview_splitter, 1, 0, 1, 3)
        else:
            layout.addWidget(self.preview_label)
        self._preview_path = ""
        self.currentChanged.connect(self._update_preview)

    def _update_preview(self, path: str) -> None:
        if not path or not Path(path).is_file():
            self._preview_path = ""
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("画像を選択してください")
            return
        pixmap = QPixmap(path)
        if pixmap.isNull():
            self._preview_path = ""
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("プレビューできない画像です")
            return
        self.preview_label.setText("")
        self._preview_path = path
        self._set_preview_pixmap(pixmap)

    def _set_preview_pixmap(self, pixmap: QPixmap) -> None:
        self.preview_label.setPixmap(
            pixmap.scaled(
                self.preview_label.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._preview_path:
            pixmap = QPixmap(self._preview_path)
            if not pixmap.isNull():
                self._set_preview_pixmap(pixmap)


class RegisteredFacesDialog(QDialog):
    def __init__(self, parent, person_name: str, face_records: List[dict]):
        super().__init__(parent)
        self.setWindowTitle(f"登録済みの顔 - {person_name}")
        self.resize(760, 560)
        self.face_records = face_records
        self.min_age = QSpinBox()
        self.min_age.setRange(0, 150)
        self.min_age.setSpecialValueText("指定なし")
        self.max_age = QSpinBox()
        self.max_age.setRange(0, 150)
        self.max_age.setSpecialValueText("指定なし")
        self.max_age.setValue(150)
        self.preview_label = QLabel("顔画像を選択してください")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumSize(320, 320)
        self.preview_label.setStyleSheet("border: 1px solid #999; padding: 8px;")

        self.thumbnail_layout = QGridLayout()
        thumbnails = QWidget()
        thumbnails.setLayout(self.thumbnail_layout)

        age_filter = QHBoxLayout()
        age_filter.addWidget(QLabel("年齢"))
        age_filter.addWidget(self.min_age)
        age_filter.addWidget(QLabel("歳から"))
        age_filter.addWidget(self.max_age)
        age_filter.addWidget(QLabel("歳"))
        self.min_age.valueChanged.connect(self._refresh_thumbnails)
        self.max_age.valueChanged.connect(self._refresh_thumbnails)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setWidget(thumbnails)
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(scroll_area)
        splitter.addWidget(self.preview_label)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([360, 520])

        layout = QVBoxLayout(self)
        layout.addLayout(age_filter)
        layout.addWidget(splitter)
        self._refresh_thumbnails()

    def _refresh_thumbnails(self) -> None:
        while self.thumbnail_layout.count():
            item = self.thumbnail_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        minimum = self.min_age.value()
        maximum = self.max_age.value() or 150
        for index, record in enumerate(
            record for record in self.face_records
            if record.get("age") is None or minimum <= record["age"] <= maximum
        ):
            image = QPixmap()
            image.loadFromData(record["face_image"] or b"")
            if image.isNull():
                continue
            button = QPushButton()
            button.setIcon(image)
            button.setIconSize(image.scaled(140, 140, Qt.KeepAspectRatio, Qt.SmoothTransformation).size())
            button.setFixedSize(160, 160)
            button.setToolTip(f"登録顔 {index + 1} を拡大表示")
            button.clicked.connect(lambda checked=False, pixmap=image: self._show_preview(pixmap))
            self.thumbnail_layout.addWidget(button, index // 4, index % 4)

    def _show_preview(self, pixmap: QPixmap) -> None:
        self.preview_label.setText("")
        self.preview_label.setPixmap(
            pixmap.scaled(
                self.preview_label.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )


class MainWindow(QWidget):
    def __init__(self, database_path: str):
        super().__init__()
        self.db_path = database_path
        self.connection = db.ensure_database(database_path)
        self.setWindowTitle("PhotoArchiveAI 人物登録")
        self.person_list = QListWidget()
        self.person_list.currentItemChanged.connect(self._on_person_selected)

        self.add_person_button = QPushButton("人物追加")
        self.edit_person_button = QPushButton("編集")
        self.delete_person_button = QPushButton("削除")
        self.add_face_button = QPushButton("顔画像登録")
        self.view_faces_button = QPushButton("登録顔を確認")

        self.details_label = QLabel("人物詳細")
        self.details_label.setWordWrap(True)

        self.add_person_button.clicked.connect(self._add_person)
        self.edit_person_button.clicked.connect(self._edit_person)
        self.delete_person_button.clicked.connect(self._delete_person)
        self.add_face_button.clicked.connect(self._add_face_image)
        self.view_faces_button.clicked.connect(self._view_registered_faces)

        button_layout = QHBoxLayout()
        button_layout.addWidget(self.add_person_button)
        button_layout.addWidget(self.edit_person_button)
        button_layout.addWidget(self.delete_person_button)
        button_layout.addWidget(self.add_face_button)
        button_layout.addWidget(self.view_faces_button)

        main_layout = QVBoxLayout()
        main_layout.addLayout(button_layout)
        main_layout.addWidget(self.person_list)
        main_layout.addWidget(self.details_label)

        self.setLayout(main_layout)
        self.resize(800, 600)
        self._reload_person_list()

    def _reload_person_list(self):
        self.person_list.clear()
        for person in db.list_persons(self.connection):
            item = QListWidgetItem(f"{person['name']} ({person.get('relation') or '-'})")
            item.setData(Qt.UserRole, person)
            self.person_list.addItem(item)
        self.details_label.setText("人物を選択してください。")

    def _on_person_selected(self, current: QListWidgetItem, previous: QListWidgetItem):
        if current is None:
            self.details_label.setText("人物を選択してください。")
            return
        person = current.data(Qt.UserRole)
        embeddings = db.list_face_embeddings(self.connection, person['id'])
        self.details_label.setText(
            f"名前: {person['name']}\n続柄: {person.get('relation') or '-'}\nメモ: {person.get('memo') or '-'}\n登録顔数: {len(embeddings)}"
        )

    def _view_registered_faces(self):
        item = self.person_list.currentItem()
        if item is None:
            QMessageBox.information(self, "選択なし", "先に人物を選択してください。")
            return
        person = item.data(Qt.UserRole)
        face_records = [record for record in db.list_face_embeddings(self.connection, person["id"]) if record.get("face_image")]
        if not face_records:
            QMessageBox.information(self, "登録顔なし", "この人物には登録済みの顔画像がありません。")
            return
        dialog = RegisteredFacesDialog(self, person["name"], face_records)
        dialog.exec()

    def _add_person(self):
        dialog = PersonDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        values = dialog.values()
        name, relation, memo = values[:3]
        if not name:
            QMessageBox.warning(self, "入力エラー", "名前は必須です。")
            return
        db.add_person(self.connection, name, relation, memo)
        self._reload_person_list()

    def _edit_person(self):
        item = self.person_list.currentItem()
        if item is None:
            return
        person = item.data(Qt.UserRole)
        dialog = PersonDialog(self, person['name'], person.get('relation') or '', person.get('memo') or '')
        if dialog.exec() != QDialog.Accepted:
            return
        values = dialog.values()
        name, relation, memo = values[:3]
        if not name:
            QMessageBox.warning(self, "入力エラー", "名前は必須です。")
            return
        db.update_person(self.connection, person['id'], name, relation, memo)
        self._reload_person_list()

    def _delete_person(self):
        item = self.person_list.currentItem()
        if item is None:
            return
        person = item.data(Qt.UserRole)
        if QMessageBox.question(self, "削除確認", f"{person['name']} を削除しますか？") != QMessageBox.Yes:
            return
        db.delete_person(self.connection, person['id'])
        self._reload_person_list()

    def _add_face_image(self):
        item = self.person_list.currentItem()
        if item is None:
            QMessageBox.information(self, "選択なし", "先に人物を選択してください。")
            return
        person = item.data(Qt.UserRole)
        file_dialog = ImageFileDialog(self)
        if file_dialog.exec() != QDialog.Accepted:
            return
        selected_files = file_dialog.selectedFiles()
        if not selected_files:
            return
        selected_file = selected_files[0]
        face_locations = detect_faces_in_file(selected_file)
        if not face_locations:
            QMessageBox.information(self, "顔検出なし", "この画像から顔を検出できませんでした。別の画像を試してください。")
            return
        selection_dialog = FaceSelectionDialog(self, selected_file, face_locations)
        if selection_dialog.exec() != QDialog.Accepted or selection_dialog.selected_index is None:
            return
        chosen_location = face_locations[selection_dialog.selected_index]
        age_dialog = FaceAgeDialog(self)
        if age_dialog.exec() != QDialog.Accepted:
            return
        self._register_face(selected_file, person['id'], chosen_location, age_dialog.age())
        self._on_person_selected(item, None)

    def _register_face(self, image_path: str, person_id: int, face_location: tuple, age: Optional[int] = None):
        rgb = _read_image(Path(image_path))
        if rgb is None:
            QMessageBox.warning(self, "登録失敗", "画像の読み込みに失敗しました。別の画像を試してください。")
            return
        embedding = compute_face_embedding(rgb, face_location)
        if embedding is None:
            QMessageBox.warning(self, "登録失敗", "顔埋め込みの生成に失敗しました。別の画像を試してください。")
            return
        image = Image.open(image_path).convert("RGB")
        top, right, bottom, left = face_location
        cropped = image.crop((left, top, right, bottom))
        buffer = io.BytesIO()
        cropped.save(buffer, format="JPEG", quality=90)
        db.add_face_embedding(self.connection, person_id, embedding, face_image=buffer.getvalue(), age=age)
        QMessageBox.information(self, "登録完了", "顔画像を登録しました。")


def main() -> None:
    import sys

    parser = argparse.ArgumentParser(prog="photoarchive-gui")
    parser.add_argument("--db", required=True, help="SQLite database path.")
    args = parser.parse_args()
    from PySide6.QtCore import QLibraryInfo

    # OpenCV may register its own Qt plugins first; use the PySide6 plugins for this GUI.
    os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = QLibraryInfo.path(
        QLibraryInfo.LibraryPath.PluginsPath
    )
    app = QApplication([])
    window = MainWindow(args.db)
    window.show()
    sys.exit(app.exec())
