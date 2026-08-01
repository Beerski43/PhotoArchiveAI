import argparse
import io
from pathlib import Path
from typing import List, Optional

import face_recognition
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
    QFormLayout,
    QTextEdit,
)

from . import db
from .analyzer import detect_faces_in_file


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

        self.details_label = QLabel("人物詳細")
        self.details_label.setWordWrap(True)

        self.add_person_button.clicked.connect(self._add_person)
        self.edit_person_button.clicked.connect(self._edit_person)
        self.delete_person_button.clicked.connect(self._delete_person)
        self.add_face_button.clicked.connect(self._add_face_image)

        button_layout = QHBoxLayout()
        button_layout.addWidget(self.add_person_button)
        button_layout.addWidget(self.edit_person_button)
        button_layout.addWidget(self.delete_person_button)
        button_layout.addWidget(self.add_face_button)

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
        item = self.person_list.currentItem()
        if item is None:
            return
        person = item.data(Qt.UserRole)
        dialog = PersonDialog(self, person['name'], person.get('relation') or '', person.get('memo') or '')
        if dialog.exec() != QDialog.Accepted:
            return
        name, relation, memo = dialog.values()
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
        selected_file, _ = QFileDialog.getOpenFileName(self, "顔画像を選択", "", "Images (*.jpg *.jpeg *.png *.bmp *.heic *.heif)")
        if not selected_file:
            return
        face_locations = detect_faces_in_file(selected_file)
        if not face_locations:
            QMessageBox.information(self, "顔検出なし", "この画像から顔を検出できませんでした。別の画像を試してください。")
            return
        selection_dialog = FaceSelectionDialog(self, selected_file, face_locations)
        if selection_dialog.exec() != QDialog.Accepted or selection_dialog.selected_index is None:
            return
        chosen_location = face_locations[selection_dialog.selected_index]
        self._register_face(selected_file, person['id'], chosen_location)
        self._on_person_selected(item, None)

    def _register_face(self, image_path: str, person_id: int, face_location: tuple):
        image = Image.open(image_path).convert("RGB")
        top, right, bottom, left = face_location
        cropped = image.crop((left, top, right, bottom))
        embedding = face_recognition.face_encodings(np.array(image), [face_location])
        if not embedding:
            QMessageBox.warning(self, "登録失敗", "顔埋め込みの生成に失敗しました。別の画像を試してください。")
            return
        buffer = io.BytesIO()
        cropped.save(buffer, format="JPEG", quality=90)
        db.add_face_embedding(self.connection, person_id, embedding[0].tolist(), face_image=buffer.getvalue())
        QMessageBox.information(self, "登録完了", "顔画像を登録しました。")


def main() -> None:
    import sys

    parser = argparse.ArgumentParser(prog="photoarchive-gui")
    parser.add_argument("--db", required=True, help="SQLite database path.")
    args = parser.parse_args()
    app = QApplication([])
    window = MainWindow(args.db)
    window.show()
    sys.exit(app.exec())
