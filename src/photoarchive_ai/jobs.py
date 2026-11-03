"""長い処理を**裏の別プロセス**で流す（#129・Phase 9）。Qt に依存しない。

GUI から `scan` のように何時間もかかる処理を流すための土台。いま載っている仕事は
`run_scan`・`run_measure_faces`（#135）・`run_convert_heic` と `run_assess`（#130）。GUI はこのモジュールの
`Job` を作って始め、タイマーで `poll` して進み具合を受け取り、`request_stop` で止める。

**同じプロセスの別スレッドにしない。** GUI の接続と同じプロセスで書き込みが走り、
`scan` のワーカーまで抱えることになる。別プロセスなら、端末で CLI を流すのと同じ形になる
（子は自分の DB 接続を開く）。**子は `spawn` で起こす**（`fork` だと GUI の SQLite 接続を
複製して持つ。CLAUDE.md §8・Issue #35）。

子から親へはキューで知らせる。1件は ``dict`` で、``kind`` は次のどれか。

=========== ==========================================================================
``listing`` 一覧づくり・変わったかを見る段（``current`` / ``total`` は None のことがある）
``progress``  1件処理した（``current`` / ``total`` / ``detail``）
``error``   ファイルごとのエラー（``message``）
``done``    最後まで終えた（``summary``）
``stopped`` 止める合図で止まった。済んだぶんは commit 済み（``summary``）
``aborted`` 安全のために中断した（``ScanAborted``・``message``）
``failed``  思わぬ失敗（``message``）。子が黙って落ちたときも親がこれを作る
=========== ==========================================================================

``done`` から ``failed`` までが**終わりの知らせ**で、1つの仕事に1つだけ届く。
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import queue as queue_module
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import db

#: 子の起こし方。**fork にしない**（上の説明）。
START_METHOD = "spawn"
#: 終わりの知らせの種類
FINAL_KINDS = ("done", "stopped", "aborted", "failed")
#: 子の DB 接続が、ほかの接続（GUI）の書き込みを待つ秒数
BUSY_TIMEOUT_MS = 30_000
#: ログの置き場所（CLI の `scan` と同じ）
LOG_DIR = Path("data/logs")

Report = Callable[[Dict[str, Any]], None]


def _strip(summary: Dict[str, Any]) -> Dict[str, Any]:
    """親へ送る集計。**`media_ids` は送らない**（数万件の番号は画面に要らない）。"""
    result = {key: value for key, value in summary.items() if key != "media_ids"}
    if "roots" in result:
        result["roots"] = [_strip(part) for part in result["roots"]]
    return result


def run_scan(
    report: Report,
    should_stop: Callable[[], bool],
    database_path: str,
    roots: Sequence[str],
    workers: int = 1,
    prune: bool = True,
    worker_log_file: Optional[str] = None,
    log_level: str = "WARNING",
    worker_initializer: Optional[Callable[[], None]] = None,
) -> Dict[str, Any]:
    """子プロセスの中で `scan` を流す。CLI の `scan` と同じ `scanner.scan_directories` を呼ぶ。

    ``worker_log_file`` は `scan` のワーカーのログ（子自身のログは `Job` の ``log_file``）。

    **1件ごとに commit する**（``commit_interval=1``）。commit の間は書き込みロックを握るので、
    50 件ごとだと GUI の書き込みが NFS の読み出しの間ずっと待たされる。
    """
    from .scanner import ScanStopped, scan_directories

    connection = db.ensure_database(database_path)
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    try:
        try:
            summary = scan_directories(
                list(roots),
                connection,
                progress_callback=lambda current, total, detail: report(
                    {"kind": "progress", "current": current, "total": total, "detail": detail}
                ),
                listing_callback=lambda current, total, detail: report(
                    {"kind": "listing", "current": current, "total": total, "detail": detail}
                ),
                error_callback=lambda message: report({"kind": "error", "message": message}),
                workers=max(1, int(workers)),
                prune=prune,
                log_file=worker_log_file,
                log_level=log_level,
                worker_initializer=worker_initializer,
                should_stop=should_stop,
                commit_interval=1,
            )
        except ScanStopped as stopped:
            connection.commit()
            raise _Stopped(_strip(stopped.summary)) from None
        return _strip(summary)
    finally:
        connection.close()


#: 顔の見え方を測れない環境（FaceMesh が読めない）で返す文
MEASURE_UNAVAILABLE = (
    "顔の見え方を測る部品（MediaPipe の FaceMesh）が読めないため、鮮明さを測れません。"
    "何も書いていません。"
)


def run_measure_faces(
    report: Report,
    should_stop: Callable[[], bool],
    database_path: str,
) -> Dict[str, Any]:
    """子プロセスの中で、**未計測の顔の見え方（鮮明さ・向き・整列）**を測る（#135）。

    対象は未分類・手動・自動の顔（顔認証外は測らない）。**サムネイルだけを読む**
    （元写真＝NFS は読まない）。500 件ずつ書くので、止めても次は残りから。

    **測れない環境では何も書かない**（`appearance.measure` が None を返す）。
    「測れない」と「整列できなかった」を混ぜない（CLAUDE.md §8）。
    """
    from . import appearance, scoring

    if scoring._load_mediapipe_face_mesh() is None:
        raise RuntimeError(MEASURE_UNAVAILABLE)
    connection = db.ensure_database(database_path)
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    try:
        progress = {"done": 0, "total": 0}

        def on_progress(current: int, total: int, _detail: str) -> None:
            progress.update(done=current, total=total)
            report({"kind": "progress", "current": current, "total": total, "detail": "顔"})

        measured = appearance.fill_missing(
            connection,
            unassigned=True,
            progress_callback=on_progress,
            should_stop=should_stop,
        )
        summary = {
            "measured": len(measured),
            "remaining": len(db.faces_without_appearance(
                connection, (db.ASSIGN_MANUAL, db.ASSIGN_AUTO), unassigned=True
            )),
        }
        if should_stop() and summary["remaining"]:
            raise _Stopped(summary)
        return summary
    finally:
        connection.close()


def run_convert_heic(
    report: Report,
    should_stop: Callable[[], bool],
    roots: Sequence[str],
) -> Dict[str, Any]:
    """子プロセスの中で `convert-heic` を流す（#130）。**DB は使わない**（CLI と同じ）。

    裏では問い返せないので、CLI が端末で聞くところを次のように決めてある。

    - **読めない HEIC（壊れている）は飛ばして**、``error`` で知らせ、集計の ``unreadable`` に残す
      （1枚の壊れた写真で、何時間もの変換を止めない）
    - **JPEG が書けなければそこで止める**（``aborted``）。書けないのはたいてい読み取り専用の
      マウントか空き容量の不足で、続けても書けない（CLI の既定の答え N と同じ）。
      書きかけの JPEG は `converter` が消す
    """
    from .converter import ConvertStopped, convert_heic_files
    from .scanner import ScanAborted

    summary: Dict[str, Any] = {"converted": 0, "skipped": 0, "unreadable": [], "roots": len(roots)}

    def on_read_error(path: Path, error: Exception) -> bool:
        summary["unreadable"].append(str(path))
        report({"kind": "error", "message": f"読めない HEIC を飛ばしました（壊れている可能性）: {path}: {error}"})
        return True

    def on_write_error(path: Path, error: Exception) -> bool:
        raise ScanAborted(
            f"JPEG を書けませんでした: {path}: {error}。"
            f"ここまでに {summary['converted']:,} 件を変換してあります（書き込み先の空き容量と書き込み権限を確かめてください）。"
        )

    for root in roots:
        try:
            converted, skipped = convert_heic_files(
                root,
                progress_callback=lambda current, total, detail: report(
                    {"kind": "progress", "current": current, "total": total, "detail": detail}
                ),
                confirm_write_error=on_write_error,
                confirm_read_error=on_read_error,
                should_stop=should_stop,
            )
        except ConvertStopped as stopped:
            summary["converted"] += stopped.converted
            summary["skipped"] += stopped.skipped
            raise _Stopped(summary) from None
        summary["converted"] += converted
        summary["skipped"] += skipped
    return summary


def run_assess(
    report: Report,
    should_stop: Callable[[], bool],
    database_path: str,
    roots: Sequence[str],
    models_factory: Optional[Callable[[], Any]] = None,
) -> Dict[str, Any]:
    """子プロセスの中で `assess`（写真の良し悪し）を流す（#130）。CLI と同じ `quality.assess`。

    **元写真を NFS から読む**ので、初回は一晩かかる。20 枚ごとに書き、止めても次は続きから。
    ``models_factory`` はテストがフェイクのモデルを渡す（pickle できる、モジュール直下の関数）。
    """
    from . import quality

    models = (models_factory or quality.Models.load)()
    connection = db.ensure_database(database_path)
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    try:
        summary = quality.assess(
            connection,
            models,
            root_paths=list(roots),
            progress_callback=lambda current, total, _detail: report(
                {"kind": "progress", "current": current, "total": total, "detail": "写真"}
            ),
            should_stop=should_stop,
        )
    finally:
        connection.close()
    if summary.pop("stopped", False):
        raise _Stopped(summary)
    return summary


class _Stopped(Exception):
    def __init__(self, summary: Dict[str, Any]):
        super().__init__("stopped")
        self.summary = summary


def _child_main(
    target: Callable[..., Dict[str, Any]],
    kwargs: Dict[str, Any],
    events,
    stop_event,
    parent_pid: int,
    initializer: Optional[Callable[[], None]],
    log_file: Optional[str],
    log_level: str,
) -> None:
    """子の入口。**終わりの知らせを必ず1つ送ってから抜ける。**"""
    from .logging_setup import setup_logging
    from .scanner import ScanAborted

    # `spawn` の子は白紙なので、ログもここで張り直す（CLAUDE.md §8）
    setup_logging(Path(log_file) if log_file else None, log_level)
    if initializer is not None:
        initializer()

    def report(event: Dict[str, Any]) -> None:
        events.put(event)

    def should_stop() -> bool:
        # **親（GUI）が落ちたら自分で止まる。** 親が居なくなると親のプロセス番号が変わる。
        return stop_event.is_set() or os.getppid() != parent_pid

    try:
        summary = target(report, should_stop, **kwargs)
        report({"kind": "done", "summary": summary})
    except _Stopped as stopped:
        report({"kind": "stopped", "summary": stopped.summary})
    except ScanAborted as error:
        report({"kind": "aborted", "message": str(error)})
    except BaseException as error:  # noqa: BLE001 — 親へ理由を届けて終わる
        logging.getLogger("photoarchive").exception("job failed")
        report(
            {
                "kind": "failed",
                "message": f"{type(error).__name__}: {error}",
                "detail": traceback.format_exc(),
            }
        )


class Job:
    """裏で流す仕事1つ。**親（GUI）側の窓口。**

    >>> job = Job("scan", run_scan, database_path=..., roots=[...])  # doctest: +SKIP
    >>> job.start(); events = job.poll(); job.request_stop()          # doctest: +SKIP

    ``initializer`` は子の入口で一度だけ呼ばれる（テストがフェイクの検出器を入れる）。
    pickle できるよう、モジュール直下の関数を渡すこと。
    """

    def __init__(
        self,
        name: str,
        target: Callable[..., Dict[str, Any]],
        initializer: Optional[Callable[[], None]] = None,
        log_file: Optional[str] = None,
        log_level: str = "WARNING",
        **kwargs: Any,
    ):
        self.name = name
        self.target = target
        self.kwargs = kwargs
        self.initializer = initializer
        self.log_file = log_file
        self.log_level = log_level
        context = multiprocessing.get_context(START_METHOD)
        self._events = context.Queue()
        self._stop = context.Event()
        self._process = context.Process(
            target=_child_main,
            args=(
                target,
                kwargs,
                self._events,
                self._stop,
                os.getpid(),
                initializer,
                log_file,
                log_level,
            ),
            # **daemon にしない。** daemon の子は自分の子（`scan` のワーカー）を作れない
            daemon=False,
        )
        self.final: Optional[Dict[str, Any]] = None

    def start(self) -> None:
        self._process.start()

    def request_stop(self) -> None:
        """止める合図を送る。**すぐには止まらない**（書きかけを commit し、処理中の数件を待つ）。"""
        self._stop.set()

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    def is_running(self) -> bool:
        """終わりの知らせをまだ受け取っていないか。"""
        return self.final is None

    def poll(self) -> List[Dict[str, Any]]:
        """届いている知らせを全部受け取る（待たない）。

        子が終わりの知らせを送らずに落ちていたら（強制終了など）、``failed`` を作って返す。
        """
        events: List[Dict[str, Any]] = []
        while self.final is None:
            try:
                event = self._events.get_nowait()
            except queue_module.Empty:
                if self._process.is_alive() or self._process.exitcode is None:
                    break
                # 子はもう居ない。キューに残りが流れ込むのを少しだけ待つ
                try:
                    event = self._events.get(timeout=0.5)
                except queue_module.Empty:
                    event = {
                        "kind": "failed",
                        "message": f"処理が途中で終了しました（終了コード {self._process.exitcode}）。",
                    }
            events.append(event)
            if event["kind"] in FINAL_KINDS:
                self.final = event
        if self.final is not None:
            self._process.join(timeout=5)
        return events

    def wait(self, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """終わるまで待つ（テストと、閉じるときの後始末用）。届いた知らせは捨てる。"""
        deadline = None if timeout is None else datetime.now().timestamp() + timeout
        while self.final is None:
            if deadline is not None and datetime.now().timestamp() > deadline:
                return None
            try:
                event = self._events.get(timeout=0.2)
            except queue_module.Empty:
                self.poll()
                continue
            if event["kind"] in FINAL_KINDS:
                self.final = event
        self._process.join(timeout=5)
        return self.final


def log_file_for(name: str) -> str:
    """CLI と同じ名前のログ（``data/logs/<name>_<日時>.log``）。"""
    return str(LOG_DIR / f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")


def scan_log_file() -> str:
    """CLI の `scan` と同じ名前のログ。"""
    return log_file_for("scan")


def describe_summary(summary: Dict[str, Any], name: str = "scan") -> str:
    """集計を1行にする。`scan` と `assess` は CLI の最後の行と同じ中身。"""
    if name == "measure_faces":
        # `run_measure_faces`（#135）
        return (
            f"測った顔 {summary.get('measured', 0):,} 件・"
            f"未計測のまま {summary.get('remaining', 0):,} 件"
        )
    if name == "convert_heic":
        return (
            f"変換 {summary.get('converted', 0):,} 件・"
            f"変換済みで飛ばした {summary.get('skipped', 0):,} 件・"
            f"読めなかった {len(summary.get('unreadable', [])):,} 件"
        )
    if name == "assess":
        if not summary.get("total"):
            return "測る写真はありません（家族の写った写真はすべて測ってあります）"
        line = (
            f"測った写真 {summary.get('measured', 0):,} 枚・"
            f"読めなかった写真 {summary.get('unreadable', 0):,} 枚（対象 {summary.get('total', 0):,} 枚）"
        )
        if summary.get("missing"):
            line += (
                f"・見つからなかった写真 {summary['missing']:,} 枚（記録していません。"
                "マウントを確かめて、もう一度測ってください）"
            )
        return line
    return (
        f"走査 {summary.get('processed', 0):,} 件・飛ばした {summary.get('skipped', 0):,} 件・"
        f"顔 {summary.get('faces', 0):,} 件・削除 {summary.get('pruned', 0):,} 件・"
        f"エラー {summary.get('errors', 0):,} 件"
    )
