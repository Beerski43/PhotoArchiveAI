"""メニューの「精度を測る（evaluate）…」（#132・Phase 9）。

`photoarchive evaluate` と同じ `evaluation.evaluate_match` を裏の別プロセスで流し（`jobs.run_evaluate`）、
同じ文（`evaluation.format_report`）を窓に出す。**DB には書かない。**

別の窓なので、結果を見ながら顔の割り当てを続けられる。**閉じても裏の処理は止まらず**、
結果は開き直すと見える。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import evaluation, jobs
from .gui_scan import JOB_NAMES, JobController, JobStatusBar, describe_final
from .matcher import DEFAULT_MARGIN

#: 試す閾値の欄の既定（CLI の `--thresholds` と同じ）
DEFAULT_THRESHOLD_TEXT = ",".join(f"{value:g}" for value in evaluation.DEFAULT_THRESHOLDS)


class EvaluateWindow(QDialog):
    """`evaluate` の条件を入れて裏で流し、結果の表を出す窓。"""

    def __init__(self, controller: JobController, database_path: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.controller = controller
        self.database_path = database_path
        #: 子プロセスの入口で呼ぶもの（テスト用）
        self.initializer: Optional[Callable[[], None]] = None
        #: 子のログ（`None` なら CLI と同じ `data/logs/evaluate_*.log`）
        self.log_file: Optional[str] = None
        self.last_final: Optional[Dict[str, Any]] = None
        self.setWindowTitle("精度を測る（evaluate）")
        self.resize(820, 620)

        intro = QLabel(
            "手本（手動で割り当てた顔）を1件ずつ抜き、残りの手本で元の人物へ戻るかを見て、"
            "自動割り当ての取りこぼしと誤りの割合を閾値ごとに測ります（photoarchive evaluate と同じ）。"
            "DB には書きません。この窓を閉じても処理は止まりません。"
        )
        intro.setWordWrap(True)

        self.thresholds_edit = QLineEdit(DEFAULT_THRESHOLD_TEXT)
        self.thresholds_edit.setToolTip("カンマで区切ります。")
        self.margin_spin = QDoubleSpinBox()
        self.margin_spin.setRange(0.0, 1.0)
        self.margin_spin.setDecimals(3)
        self.margin_spin.setSingleStep(0.01)
        self.margin_spin.setValue(DEFAULT_MARGIN)
        self.margin_spin.setToolTip("2位の人物との距離の差の下限。自動割り当てと同じ意味です。")
        self.keep_same_media_check = QCheckBox(
            "同じ写真に写る同一人物の顔も手本に残す（既定は外す。抜いた顔とほぼ同じ手本が残ると数字が甘くなるため）"
        )
        form = QFormLayout()
        form.addRow("試す閾値", self.thresholds_edit)
        form.addRow("マージン", self.margin_spin)
        form.addRow(self.keep_same_media_check)

        self.start_button = QPushButton("測る")
        self.start_button.clicked.connect(self.start)
        self.status = JobStatusBar(controller, in_window=True)
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.report.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.report.setPlaceholderText("結果をここに出します。")

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(self.start_button)
        layout.addWidget(self.status)
        layout.addWidget(self.report, 1)

        controller.started.connect(lambda _name: self._set_running(True))
        controller.finished.connect(self._finished)
        self._set_running(controller.is_running())

    def open_window(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)

    def start(self) -> None:
        title = JOB_NAMES["evaluate"]
        if self.controller.is_running():
            return
        try:
            thresholds = evaluation.parse_thresholds(self.thresholds_edit.text())
        except ValueError as error:
            QMessageBox.warning(self, title, str(error))
            return
        job = jobs.Job(
            "evaluate",
            jobs.run_evaluate,
            initializer=self.initializer,
            log_file=self.log_file or jobs.log_file_for("evaluate"),
            database_path=self.database_path,
            thresholds=thresholds,
            margin=self.margin_spin.value(),
            keep_same_media=self.keep_same_media_check.isChecked(),
        )
        self.report.clear()
        self.controller.start(job)

    def _finished(self, name: str, event: Dict[str, Any]) -> None:
        self._set_running(False)
        if name != "evaluate":
            return
        self.last_final = event
        if event["kind"] == "done":
            self.report.setPlainText(evaluation.format_report(event["summary"]))
            return
        message = describe_final(name, event)
        self.report.setPlainText(message)
        if event["kind"] == "failed":
            QMessageBox.warning(self, JOB_NAMES[name], message)
