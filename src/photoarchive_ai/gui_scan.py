"""GUI から長い処理を裏で流す（#129・Phase 9）。いまは `scan`。

- `JobController`: 裏で流している仕事（`jobs.Job`）を1つ持ち、タイマーで知らせを受け取る。
  **同時に流すのは1つだけ。**
- `JobStatusBar`: 進み具合と「止める」。**どのタブからも見える位置**（タブの右上）に置く。
  顔の割り当てを続けながら、裏の進み具合を見られるようにする（利用者の決定・2026-11-03）
- `ScanPanel`: 3枚目のタブ「取り込み」。root を出し、`scan` を始める

**止めても、次の `scan` は続きから**（済んだぶんは commit してあり、差分判定で飛ばす）。
消えたファイルの行の削除と root の記録は、最後まで走査したときだけ。

GUI に出さない `scan` の引数: ``--force-rescan``（顔の行を作り直し、**手動割り当てと
顔認証外が消える**・CLAUDE.md §8）・``--force-prune``（2割の安全弁を外す）・
``--allow-missing-embeddings``。どれも CLI で、何が起きるかを分かったうえで使う。
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import db, jobs
from .scanner import normalize_source_roots

#: 知らせを受け取る間隔
POLL_INTERVAL_MS = 200
#: 画面に残すエラーと結果の行数
LOG_MAX_LINES = 2000
#: ワーカー数の既定（CLI の `scan --workers` と同じ）
DEFAULT_WORKERS = min(4, max(1, (os.cpu_count() or 2) - 1))

JOB_NAMES = {"scan": "スキャン"}


def describe_event(name: str, event: Dict[str, Any]) -> Optional[str]:
    """進み具合の知らせを1行にする。進み具合でない知らせは None。"""
    label = JOB_NAMES.get(name, name)
    current, total, detail = event.get("current"), event.get("total"), event.get("detail") or ""
    if event["kind"] == "listing":
        if total:
            return f"{label}: 前回から変わったかを見ています（{current:,} / {total:,} 件）"
        return f"{label}: ファイルを数えています（{current:,} 件）"
    if event["kind"] == "progress":
        if not total:
            return f"{label}: 新しく読むファイルはありません"
        return f"{label}: {current:,} / {total:,} 件（{detail}）"
    return None


def describe_final(name: str, event: Dict[str, Any]) -> str:
    """終わりの知らせを、画面に残す文にする。"""
    label = JOB_NAMES.get(name, name)
    kind = event["kind"]
    if kind == "done":
        return f"{label}が終わりました。{jobs.describe_summary(event['summary'])}"
    if kind == "stopped":
        return (
            f"{label}を止めました。{jobs.describe_summary(event['summary'])}\n"
            "済んだぶんは保存してあります。次に始めると続きからになります"
            "（消えたファイルの行の削除は、最後まで走査したときだけ行います）。"
        )
    if kind == "aborted":
        return f"{label}を中断しました（何も消していません）: {event['message']}"
    return f"{label}が失敗しました: {event['message']}"


class JobController(QObject):
    """裏で流している仕事1つの窓口。**同時に1つだけ。**"""

    #: 仕事を始めた（名前）
    started = Signal(str)
    #: 進み具合の文が変わった
    status_changed = Signal(str)
    #: ファイルごとのエラー
    error_reported = Signal(str)
    #: 終わった（名前・終わりの知らせ）
    finished = Signal(str, dict)

    def __init__(self, parent: Optional[QObject] = None, poll_ms: int = POLL_INTERVAL_MS):
        super().__init__(parent)
        self.job: Optional[jobs.Job] = None
        self.status = ""
        self.timer = QTimer(self)
        self.timer.setInterval(poll_ms)
        self.timer.timeout.connect(self.poll)

    def is_running(self) -> bool:
        return self.job is not None and self.job.is_running()

    def start(self, job: jobs.Job) -> None:
        if self.is_running():
            raise RuntimeError("ほかの処理を流しています。終わってから始めてください。")
        self.job = job
        self.status = f"{JOB_NAMES.get(job.name, job.name)}を始めています"
        job.start()
        self.timer.start()
        self.started.emit(job.name)
        self.status_changed.emit(self.status)

    def request_stop(self) -> None:
        if not self.is_running():
            return
        self.job.request_stop()
        self.status = (
            f"{JOB_NAMES.get(self.job.name, self.job.name)}を止めています"
            "（書きかけを保存し、処理中の数件が終わるのを待っています）"
        )
        self.status_changed.emit(self.status)

    def poll(self) -> None:
        job = self.job
        if job is None:
            self.timer.stop()
            return
        line = None
        for event in job.poll():
            if event["kind"] == "error":
                self.error_reported.emit(event["message"])
                continue
            described = describe_event(job.name, event)
            if described is not None:
                line = described
        if line is not None and not job.stop_requested:
            self.status = line
            self.status_changed.emit(line)
        if job.final is not None:
            self.timer.stop()
            self.status = ""
            self.status_changed.emit("")
            self.finished.emit(job.name, job.final)

    def stop_and_wait(self, on_wait: Optional[Callable[[], None]] = None, timeout: Optional[float] = None) -> bool:
        """止める合図を送り、終わるまで待つ（画面を閉じるとき）。終われば True。

        **子を殺さない。** 書きかけを commit して抜けるのを待つ（DB を壊さない）。
        """
        if not self.is_running():
            return True
        self.request_stop()
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.is_running():
            self.poll()
            if on_wait is not None:
                on_wait()
            if deadline is not None and time.monotonic() > deadline:
                return False
            time.sleep(0.05)
        return True


class JobStatusBar(QWidget):
    """進み具合と「止める」。動いていないときは隠す。"""

    def __init__(self, controller: JobController, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.controller = controller
        self.label = QLabel("")
        self.stop_button = QPushButton("止める")
        self.stop_button.setToolTip("済んだぶんは保存されます。次に始めると続きからになります。")
        self.stop_button.clicked.connect(controller.request_stop)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.addWidget(self.label)
        layout.addWidget(self.stop_button)
        controller.status_changed.connect(self._show)
        self.setVisible(False)

    def _show(self, text: str) -> None:
        self.label.setText(text)
        self.stop_button.setEnabled(not (self.controller.job and self.controller.job.stop_requested))
        self.setVisible(bool(text))


def resolve_scan_roots(settings_roots: Sequence[str], connection) -> List[str]:
    """`scan` する root。**設定 → DB に記録された root** の順（CLI の `scan` と同じ・#24）。"""
    roots = [root for root in settings_roots if root]
    return roots or db.list_scan_roots(connection)


class ScanPanel(QWidget):
    """3枚目のタブ「取り込み」。`scan` を裏で流す。"""

    def __init__(
        self,
        controller: JobController,
        database_path: str,
        connection,
        settings_roots: Sequence[str] = (),
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.controller = controller
        self.database_path = database_path
        self.connection = connection
        self.settings_roots = list(settings_roots)
        #: 子プロセスの入口で呼ぶもの（テストがフェイクの検出器を入れる）
        self.initializer: Optional[Callable[[], None]] = None
        #: 子のログ（`None` なら CLI と同じ `data/logs/scan_*.log`）
        self.log_file: Optional[str] = None
        self.last_final: Optional[Dict[str, Any]] = None

        intro = QLabel(
            "写真を走査して、新しい写真・変わった写真の顔を検出します（photoarchive scan と同じ）。"
            "NFS を読むので何時間もかかることがあります。<b>裏で流すので、そのあいだも顔の割り当てを続けられます</b>"
            "（自動割り当て（match）だけは終わるまで押せません）。<br>"
            "途中で止めても、済んだぶんは保存され、次に始めると続きからになります。"
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)

        self.roots_list = QListWidget()
        self.roots_list.setMaximumHeight(110)
        self.roots_note = QLabel("")
        self.roots_note.setWordWrap(True)

        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, max(1, os.cpu_count() or 1))
        self.workers_spin.setValue(min(DEFAULT_WORKERS, self.workers_spin.maximum()))
        self.prune_check = QCheckBox("消えたファイルの行を DB から消す（最後まで走査したときだけ・2割を超えると中断）")
        self.prune_check.setChecked(True)

        self.start_button = QPushButton("スキャンを始める")
        self.start_button.clicked.connect(self.start_scan)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(LOG_MAX_LINES)
        self.log.setPlaceholderText("読めなかったファイルと、終わったときの件数をここに出します。")

        options = QHBoxLayout()
        options.addWidget(QLabel("ワーカー数"))
        options.addWidget(self.workers_spin)
        options.addSpacing(16)
        options.addWidget(self.prune_check)
        options.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(QLabel("走査する root"))
        layout.addWidget(self.roots_list)
        layout.addWidget(self.roots_note)
        layout.addLayout(options)
        layout.addWidget(self.start_button, 0, Qt.AlignLeft)
        layout.addWidget(self.log, 1)

        controller.started.connect(lambda _name: self._set_running(True))
        controller.finished.connect(self._finished)
        controller.error_reported.connect(self.log.appendPlainText)
        self.reload_roots()

    def reload_roots(self) -> None:
        self.roots = resolve_scan_roots(self.settings_roots, self.connection)
        self.roots_list.clear()
        self.roots_list.addItems(self.roots)
        if not self.roots:
            self.roots_note.setText(
                "root がありません。config/app_settings.yml の source_roots に書いてください"
                "（docs/operation/CONFIG.md）。"
            )
        elif self.settings_roots:
            self.roots_note.setText("設定（source_roots）の root です。")
        else:
            self.roots_note.setText("設定に root が無いため、DB に記録された root（前に走査したもの）を使います。")
        self.start_button.setEnabled(bool(self.roots) and not self.controller.is_running())

    def _set_running(self, running: bool) -> None:
        self.start_button.setEnabled(bool(self.roots) and not running)
        self.workers_spin.setEnabled(not running)
        self.prune_check.setEnabled(not running)

    def _finished(self, name: str, event: Dict[str, Any]) -> None:
        self._set_running(False)
        if name != "scan":
            return
        self.last_final = event
        message = describe_final(name, event)
        self.log.appendPlainText(message)
        self.reload_roots()
        if event["kind"] in ("aborted", "failed"):
            QMessageBox.warning(self, "スキャン", message)

    def start_scan(self) -> None:
        if self.controller.is_running() or not self.roots:
            return
        try:
            normalize_source_roots(self.roots)
        except ValueError as error:
            QMessageBox.warning(self, "スキャン", str(error))
            return
        answer = QMessageBox.question(
            self,
            "スキャンを始める",
            "次の root を走査します。\n\n"
            + "\n".join(self.roots)
            + "\n\n何時間もかかることがあります。途中で止めても、次は続きからになります。始めますか？",
        )
        if answer != QMessageBox.Yes:
            return
        log_file = self.log_file or jobs.scan_log_file()
        job = jobs.Job(
            "scan",
            jobs.run_scan,
            initializer=self.initializer,
            log_file=log_file,
            database_path=self.database_path,
            roots=list(self.roots),
            workers=self.workers_spin.value(),
            prune=self.prune_check.isChecked(),
            worker_log_file=log_file,
            worker_initializer=self.initializer,
        )
        self.log.appendPlainText(f"スキャンを始めました（ログ: {log_file}）")
        self.controller.start(job)


def wait_for_stop(controller: JobController, parent: QWidget) -> bool:
    """閉じるときに、止まるのを待つ。待つあいだ「止めています」を出す。"""
    notice = QMessageBox(parent)
    notice.setWindowTitle("止めています")
    notice.setText("書きかけを保存し、処理中の数件が終わるのを待っています。")
    notice.setStandardButtons(QMessageBox.NoButton)
    notice.setWindowModality(Qt.ApplicationModal)
    notice.show()
    try:
        return controller.stop_and_wait(on_wait=QApplication.processEvents)
    finally:
        notice.close()
