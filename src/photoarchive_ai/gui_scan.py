"""GUI から長い処理を裏で流す（#129・Phase 9）。`convert-heic`・`scan`・`assess`（#130）。

- `JobController`: 裏で流している仕事（`jobs.Job`）を1つ持ち、タイマーで知らせを受け取る。
  **同時に流すのは1つだけ。**
- `JobStatusBar`: 進み具合と「止める」。**どのタブからも見える位置**（タブの右上）に置く。
  顔の割り当てを続けながら、裏の進み具合を見られるようにする（利用者の決定・2026-11-03）
- `ScanPanel`: 3枚目のタブ「取り込み」。root を出し、上から **HEIC を JPEG に変換 → スキャン →
  写真の良し悪しを測る**（一連の流れの順・#130）。3つとも同じ root の一覧を使う

**止めても、次の `scan` は続きから**（済んだぶんは commit してあり、差分判定で飛ばす）。
消えたファイルの行の削除と root の記録は、最後まで走査したときだけ。

GUI に出さない `scan` の引数: ``--force-rescan``（顔の行を作り直し、**手動割り当てと
顔認証外が消える**・CLAUDE.md §8）・``--force-prune``（2割の安全弁を外す）・
``--allow-missing-embeddings``。どれも CLI で、何が起きるかを分かったうえで使う。
`assess` の ``--limit`` も出さない（動作確認用）。

`convert-heic` は裏では問い返せないので、**読めない HEIC は飛ばして一覧に残し、JPEG が
書けなければそこで止める**（`jobs.run_convert_heic`）。
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

JOB_NAMES = {
    "scan": "スキャン",
    "measure_faces": "鮮明さの計測",
    "convert_heic": "HEIC の変換",
    "assess": "写真の良し悪しの計測",
}
#: 対象が無いときの文（``progress`` の ``total`` が 0）
NOTHING_TO_DO = {
    "scan": "新しく読むファイルはありません",
    "measure_faces": "未計測の顔はありません",
    "convert_heic": "HEIC/HEIF のファイルはありません",
    "assess": "測る写真はありません",
}


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
            return f"{label}: {NOTHING_TO_DO.get(name, '対象はありません')}"
        return f"{label}: {current:,} / {total:,} 件（{detail}）"
    return None


def describe_final(name: str, event: Dict[str, Any]) -> str:
    """終わりの知らせを、画面に残す文にする。"""
    label = JOB_NAMES.get(name, name)
    kind = event["kind"]
    if kind == "done":
        message = f"{label}が終わりました。{jobs.describe_summary(event['summary'], name)}"
        if name == "convert_heic" and event["summary"].get("converted"):
            message += "\n変換した JPEG は、続けてスキャンすると取り込まれます。"
        return message
    if kind == "stopped" and name != "scan":
        return (
            f"{label}を止めました。{jobs.describe_summary(event['summary'], name)}\n"
            "済んだぶんは保存してあります。次に始めると続きからになります。"
        )
    if kind == "stopped":
        return (
            f"{label}を止めました。{jobs.describe_summary(event['summary'], name)}\n"
            "済んだぶんは保存してあります。次に始めると続きからになります"
            "（消えたファイルの行の削除は、最後まで走査したときだけ行います）。"
        )
    if kind == "aborted" and name == "convert_heic":
        return f"{label}を止めました: {event['message']}"
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


def _section_title(text: str) -> QLabel:
    label = QLabel(f"<b>{text}</b>")
    label.setTextFormat(Qt.RichText)
    return label


def _note(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.RichText)
    return label


class ScanPanel(QWidget):
    """3枚目のタブ「取り込み」。`convert-heic`・`scan`・`assess` を裏で流す（#129・#130）。

    上から一連の流れの順に並べる。**3つとも同じ root の一覧**（設定 → DB に記録された root）を使う。
    """

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
        #: `assess` のモデルを作るもの（`None` なら `quality.Models.load`。テストがフェイクを入れる）
        self.models_factory: Optional[Callable[[], Any]] = None
        #: このタブから流した仕事の、最後の終わりの知らせ
        self.last_final: Optional[Dict[str, Any]] = None
        self.last_name: Optional[str] = None

        intro = _note(
            "写真の取り込みと、写真の良し悪しの計測を、<b>裏で流します</b>。そのあいだも顔の割り当てを続けられます"
            "（自動割り当て（match）と、ほかの裏の処理は、終わるまで押せません）。"
            "途中で止めても、済んだぶんは保存され、次に始めると続きからになります。"
        )

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

        self.convert_button = QPushButton("HEIC を JPEG に変換する")
        self.convert_button.clicked.connect(self.start_convert)
        self.assess_button = QPushButton("写真の良し悪しを測る")
        self.assess_button.clicked.connect(self.start_assess)
        self.assess_count = QLabel("")

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

        assess_row = QHBoxLayout()
        assess_row.addWidget(self.assess_button)
        assess_row.addWidget(self.assess_count, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(QLabel("root（写真の置き場）"))
        layout.addWidget(self.roots_list)
        layout.addWidget(self.roots_note)
        layout.addSpacing(6)
        layout.addWidget(_section_title("1. HEIC を JPEG に変換（photoarchive convert-heic と同じ）"))
        layout.addWidget(_note(
            "iPhone の HEIC/HEIF を、同じフォルダに JPEG で書きます。元のファイルは残します。"
            "変換済みの写真は飛ばします。読めない HEIC は飛ばして下に出し、JPEG が書けなければそこで止めます。"
        ))
        layout.addWidget(self.convert_button, 0, Qt.AlignLeft)
        layout.addSpacing(6)
        layout.addWidget(_section_title("2. スキャン（photoarchive scan と同じ）"))
        layout.addWidget(_note(
            "新しい写真・変わった写真の顔を検出します。NFS を読むので何時間もかかることがあります。"
        ))
        layout.addLayout(options)
        layout.addWidget(self.start_button, 0, Qt.AlignLeft)
        layout.addSpacing(6)
        layout.addWidget(_section_title("3. 写真の良し悪しを測る（photoarchive assess と同じ）"))
        layout.addWidget(_note(
            "家族の写った写真の画質（MUSIQ）・美しさ（NIMA）・白飛び・黒つぶれを測り、写真の選定の点に使います。"
            "まだ測っていない写真だけを測ります。元写真を NFS から読むので、初回は一晩かかることがあります。"
            "顔を割り当てたあと（match のあと）に流します。"
        ))
        layout.addLayout(assess_row)
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
        self.refresh_assess_count()
        self._set_running(self.controller.is_running())

    def refresh_assess_count(self) -> None:
        """まだ測っていない家族の写真の枚数（`assess` の対象）。"""
        from . import quality

        self.unassessed = len(db.photos_to_assess(self.connection, quality.MODEL_VERSION))
        if self.unassessed:
            self.assess_count.setText(f"まだ測っていない家族の写真: {self.unassessed:,} 枚")
        else:
            self.assess_count.setText("家族の写った写真はすべて測ってあります。")

    def showEvent(self, event) -> None:
        # 顔の割り当てタブで割り当てると、測る写真が増える
        self.refresh_assess_count()
        self._set_running(self.controller.is_running())
        super().showEvent(event)

    def _set_running(self, running: bool) -> None:
        idle = bool(self.roots) and not running
        self.start_button.setEnabled(idle)
        self.convert_button.setEnabled(idle)
        self.assess_button.setEnabled(idle and bool(getattr(self, "unassessed", 0)))
        self.workers_spin.setEnabled(not running)
        self.prune_check.setEnabled(not running)

    def _finished(self, name: str, event: Dict[str, Any]) -> None:
        if name not in ("scan", "convert_heic", "assess"):
            # 鮮明さの計測（#135）は顔の割り当て画面が知らせる
            self._set_running(False)
            return
        self.last_final = event
        self.last_name = name
        message = describe_final(name, event)
        self.log.appendPlainText(message)
        self.reload_roots()
        if event["kind"] in ("aborted", "failed"):
            QMessageBox.warning(self, JOB_NAMES[name], message)

    def start_scan(self) -> None:
        if not self._roots_ready("スキャン"):
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

    def _roots_ready(self, title: str) -> bool:
        if self.controller.is_running() or not self.roots:
            return False
        try:
            normalize_source_roots(self.roots)
        except ValueError as error:
            QMessageBox.warning(self, title, str(error))
            return False
        return True

    def start_convert(self) -> None:
        """`convert-heic` を裏で流す（#130）。"""
        title = JOB_NAMES["convert_heic"]
        if not self._roots_ready(title):
            return
        answer = QMessageBox.question(
            self,
            "HEIC を JPEG に変換する",
            "次の root の中の HEIC/HEIF を、同じフォルダに JPEG で書きます（元のファイルは残します）。\n\n"
            + "\n".join(self.roots)
            + "\n\n読めない HEIC は飛ばし、JPEG が書けなければそこで止めます。始めますか？",
        )
        if answer != QMessageBox.Yes:
            return
        log_file = self.log_file or jobs.log_file_for("convert_heic")
        job = jobs.Job(
            "convert_heic",
            jobs.run_convert_heic,
            initializer=self.initializer,
            log_file=log_file,
            roots=list(self.roots),
        )
        self.log.appendPlainText(f"HEIC の変換を始めました（ログ: {log_file}）")
        self.controller.start(job)

    def start_assess(self) -> None:
        """`assess`（写真の良し悪し）を裏で流す（#130）。**モデルが無ければ始めない。**"""
        from . import quality

        title = JOB_NAMES["assess"]
        if not self._roots_ready(title):
            return
        if self.models_factory is None:
            missing = quality.missing_model_files()
            if missing:
                QMessageBox.warning(self, title, quality.missing_models_message(missing))
                return
        self.refresh_assess_count()
        if not self.unassessed:
            QMessageBox.information(self, title, "家族の写った写真はすべて測ってあります。")
            self._set_running(False)
            return
        answer = QMessageBox.question(
            self,
            "写真の良し悪しを測る",
            f"まだ測っていない家族の写真 {self.unassessed:,} 枚を測ります。\n"
            "元写真を NFS から読むので、何時間もかかることがあります。"
            "途中で止めても、次は続きからになります。始めますか？",
        )
        if answer != QMessageBox.Yes:
            return
        log_file = self.log_file or jobs.log_file_for("assess")
        job = jobs.Job(
            "assess",
            jobs.run_assess,
            initializer=self.initializer,
            log_file=log_file,
            database_path=self.database_path,
            roots=list(self.roots),
            models_factory=self.models_factory,
        )
        self.log.appendPlainText(f"写真の良し悪しの計測を始めました（ログ: {log_file}）")
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
