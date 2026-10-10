import argparse
import logging
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import db
from .config import (
    find_legacy_settings_path,
    get_database_path,
    get_output_root,
    get_rule_path,
    get_source_roots,
    legacy_settings_stop_message,
    load_settings,
)
from .converter import convert_heic_files
from .db import SchemaVersionError, ensure_database
from .evaluation import DEFAULT_THRESHOLDS, evaluate_match, format_report
from .face import get_latest_error
from .logging_setup import setup_logging
from .matcher import DEFAULT_MARGIN, DEFAULT_THRESHOLD, match_faces
from .reembed import format_summary as format_reembed_summary
from .reembed import reembed_faces
from .migration import (
    describe_for_operator,
    migrate_database,
    needs_migration,
    rebuilds_faces,
)
from .progress import ProgressDisplay
from .scanner import ScanAborted, normalize_source_roots, scan_directories
from .selection import (
    link_selected_media,
    load_rule,
    select_media,
    stale_assignment_notice,
)


def _setup_logging(log_file: Optional[Path] = None, log_level: str = "WARNING") -> logging.Logger:
    """このプロセスのログ出力先を決める。

    **ワーカープロセスのぶんはここでは面倒を見ない。** `spawn` の子は
    白紙で始まるので、子の入口（`scanner._start_worker`）が
    `logging_setup.setup_logging` を自分で呼び直す。実体を
    `logging_setup` へ置いているのはそのため。
    """
    return setup_logging(log_file, log_level)


#: いま使っている進捗表示。コマンドの入口（`_reset_progress_state`）で作り直す。
_display = ProgressDisplay()


def _reset_progress_state() -> None:
    """進捗表示を最初から始める。コマンドの入口と、段の切り替わりで呼ぶ。"""
    global _display
    _display.finish()
    _display = ProgressDisplay()


def _emit_progress(
    current: int,
    total: Optional[int],
    detail: str,
    prefix: str = "Progress",
    error: str = "",
) -> None:
    """バーと最新のメッセージの2行を書き直す（#78。中身は `progress.ProgressDisplay`）。

    ``error`` が空でなく直前と違えば、そのエラーをバーの上に1行で残す。
    エラーの無い回に ``Error`` の語は出さない。
    """
    if error:
        _display.error(error)
    _display.update(current, total, detail, prefix=prefix)


def _keep_line(message: str) -> None:
    """進捗の上に1行残す（移行の各段の結果など）。"""
    _display.keep(message)


def _add_log_level(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        type=str.upper,
        default="WARNING",
        help="Log verbosity (default: WARNING).",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="photoarchive")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init-db", help="Initialize SQLite database.")
    init_parser.add_argument("--db", help="SQLite database path.")

    migrate_parser = subparsers.add_parser(
        "migrate", help="Migrate an existing database to the current schema."
    )
    migrate_parser.add_argument("--db", help="SQLite database path.")
    migrate_parser.add_argument("--backup", help="Backup file path (default: <db>.bak-<timestamp>).")
    migrate_parser.add_argument(
        "--no-vacuum",
        action="store_true",
        help="Skip VACUUM after the migration. VACUUM runs only when migrating from v1"
        " (the v2+ migration only adds columns, so nothing is rebuilt).",
    )
    migrate_parser.add_argument(
        "--yes", action="store_true", help="Do not ask for confirmation."
    )

    scan_parser = subparsers.add_parser(
        "scan", help="Scan source media, detect faces and store them in the database."
    )
    scan_parser.add_argument(
        "--source",
        action="append",
        help="走査するディレクトリ。何度でも書ける。書けば設定の source_roots より優先。",
    )
    scan_parser.add_argument("--db", help="SQLite database path.")
    scan_parser.add_argument(
        "--workers",
        type=int,
        default=min(4, max(1, (os.cpu_count() or 2) - 1)),
        help="Number of worker processes for face detection.",
    )
    scan_parser.add_argument(
        "--no-prune", action="store_true", help="Keep database rows whose files are gone."
    )
    scan_parser.add_argument(
        "--force-prune", action="store_true", help="Delete missing files even if many are gone."
    )
    scan_parser.add_argument(
        "--allow-missing-embeddings",
        action="store_true",
        help=(
            "顔特徴量のモデルが読めなくてもスキャンを続ける。顔は検出されるが"
            " match が効かない状態になる。このとき検出器の版は記録しないので、"
            "モデルを設置したあと通常の scan を実行すれば自動で作り直される。"
        ),
    )
    scan_parser.add_argument(
        "--force-rescan", action="store_true", help="Detect faces again for every media file."
    )
    _add_log_level(scan_parser)

    reembed_parser = subparsers.add_parser(
        "reembed",
        help="Rebuild face embeddings from the stored thumbnails (no source files are read).",
    )
    reembed_parser.add_argument("--db", help="SQLite database path.")
    reembed_parser.add_argument(
        "--yes", action="store_true", help="Do not ask for confirmation."
    )
    reembed_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="対象の件数と見積り時間だけを出し、データベースには書かない。",
    )
    reembed_parser.add_argument(
        "--limit", type=int, help="先頭 N 件だけ作り直す（動作確認用）。"
    )
    _add_log_level(reembed_parser)

    convert_parser = subparsers.add_parser("convert-heic", help="Convert HEIC/HEIF files to JPEG.")
    convert_parser.add_argument(
        "--source", action="append", help="変換するディレクトリ（再帰）。何度でも書ける。"
    )

    match_parser = subparsers.add_parser(
        "match", help="Assign remaining faces automatically using the faces assigned in the GUI."
    )
    match_parser.add_argument("--db", help="SQLite database path.")
    match_parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"Maximum face distance to accept (default: {DEFAULT_THRESHOLD}).",
    )
    match_parser.add_argument(
        "--margin",
        type=float,
        default=DEFAULT_MARGIN,
        help=f"Required distance gap to the runner-up person (default: {DEFAULT_MARGIN}).",
    )
    match_parser.add_argument(
        "--no-reset", action="store_true", help="Keep existing automatic assignments."
    )
    match_parser.add_argument(
        "--dry-run", action="store_true", help="Report what would be assigned without writing."
    )
    _add_log_level(match_parser)

    unassign_parser = subparsers.add_parser(
        "unassign-auto",
        help="Remove every automatic assignment, keeping the manual ones and the rejects.",
    )
    unassign_parser.add_argument("--db", help="SQLite database path.")
    unassign_parser.add_argument(
        "--person",
        help=(
            "人物の名前か id。指定すると、その人物の自動割り当てだけを消す"
            "（省略すると全員ぶん）。"
        ),
    )
    unassign_parser.add_argument(
        "--dry-run", action="store_true", help="Report how many would be removed without writing."
    )
    _add_log_level(unassign_parser)

    evaluate_parser = subparsers.add_parser(
        "evaluate",
        help="Measure how often match would miss or mis-assign a face, using assigned faces.",
    )
    evaluate_parser.add_argument("--db", help="SQLite database path.")
    evaluate_parser.add_argument(
        "--thresholds",
        default=",".join(f"{value:g}" for value in DEFAULT_THRESHOLDS),
        help=(
            "試す閾値をカンマ区切りで指定する"
            f" (default: {','.join(f'{value:g}' for value in DEFAULT_THRESHOLDS)})。"
        ),
    )
    evaluate_parser.add_argument(
        "--margin",
        type=float,
        default=DEFAULT_MARGIN,
        help=f"Required distance gap to the runner-up person (default: {DEFAULT_MARGIN}).",
    )
    evaluate_parser.add_argument(
        "--keep-same-media",
        action="store_true",
        help=(
            "同じ写真に写る同一人物の顔も手本に残す。既定では外す"
            "（抜いた顔とほぼ同じ手本が残ると、必ず当たって数字が甘くなるため）。"
        ),
    )
    _add_log_level(evaluate_parser)

    select_parser = subparsers.add_parser("select", help="Select media by rule and symlink them into output.")
    select_parser.add_argument("--db", help="SQLite database path.")
    select_parser.add_argument("--rule", help="YAML rule file path (JSON is not read).")
    select_parser.add_argument("--output", help="Output directory. Symlinks are placed directly under it; previous symlinks there are removed.")
    select_parser.add_argument(
        "--source",
        action="append",
        help="相対パスで登録されたメディアを解く root。何度でも書ける。",
    )

    return parser


def _run_migrate(args, db_path: str) -> None:
    if not Path(db_path).exists():
        raise SystemExit(f"Database does not exist: {db_path}")
    if not needs_migration(db_path):
        print("スキーマはすでに最新です。移行は不要です。")
        return
    # 文面は GUI と共有する。2か所に書くと、片方だけ古くなる。
    print(describe_for_operator(db_path))
    # **移行する前に見ておく。** 移行後は版が上がっており、「顔を作り直したか」を
    # 聞いても必ず False になる。
    rebuilt = rebuilds_faces(db_path)
    if not args.yes:
        answer = input("続行しますか? [y/N]: ")
        if answer.strip().lower() not in {"y", "yes"}:
            raise SystemExit("移行を中止しました。")
    _reset_progress_state()
    migrate_database(
        db_path,
        backup_path=args.backup,
        vacuum=not args.no_vacuum,
        log=_keep_line,
        # 段の名前をバーの行に出す（段が終わると2行目は消えるので、名前が残るように）
        progress=lambda current, total, step: _display.update(
            current, total, prefix=step, show_counts=total != 1
        ),
    )
    if rebuilt:
        print("次の手順: photoarchive scan → photoarchive-gui で顔を割り当て → photoarchive match")


def _run_reembed(args, db_path: str) -> None:
    """保存済みサムネイルから特徴量を作り直す。

    **元写真を読まない。** 実データでは全件 約158分（実測 162ms/件。NFS の
    読み直しは0）。見積りは `--dry-run` が実際に作って測るので、ここは目安。
    **割り当てには触らない**ので、手本と除外はそのまま残る。
    """
    log_file = Path("data/logs") / f"reembed_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logger = _setup_logging(log_file, args.log_level)
    _reset_progress_state()
    with ensure_database(db_path) as connection:
        preview = reembed_faces(connection, dry_run=True)
        print(format_reembed_summary(preview))
        if preview["target"] == 0:
            print("作り直す顔はありません（すべて最新の版です）。")
            return
        if args.dry_run:
            return
        if not args.yes:
            answer = input("続行しますか? [y/N]: ")
            if answer.strip().lower() not in {"y", "yes"}:
                raise SystemExit("作り直しを中止しました。")
        logger.info("Reembed started. Log file: %s", log_file)
        summary = reembed_faces(
            connection,
            progress_callback=lambda current, total, detail: _emit_progress(
                current, total, detail, prefix="Reembedding", error=get_latest_error()
            ),
            limit=args.limit,
        )
    print(format_reembed_summary(summary))
    print("次の手順: photoarchive match で自動の紐づけをやり直してください。")


SOURCE_ROOTS_REQUIRED = (
    "検出元のディレクトリが必要です。--source で指定するか、"
    "config/app_settings.yml の source_roots に書いてください。"
)


def _source_roots(args, settings, connection=None) -> List[str]:
    """root を決める。``--source`` → 設定 → **DB に記録された root**（#24）の順。

    最後の段は、設定ファイルを失ったとき（2026-10-02）に DB から戻すためにある。
    記録は実際に走査した root だけなので、推定（共通の親）のような事故は起きない。
    使ったときは何で走査するかを表示する。
    """
    roots = list(getattr(args, "source", None) or []) or get_source_roots(settings)
    if roots or connection is None:
        return roots
    recorded = db.list_scan_roots(connection)
    if recorded:
        print("設定に検出元が無いため、DB に記録された root を使います:")
        for root in recorded:
            print(f"  {root}")
    return recorded


def _run_scan(args, settings) -> None:
    db_path = getattr(args, "db", None) or get_database_path(settings)
    with ensure_database(db_path) as connection:
        source_roots = _source_roots(args, settings, connection)
    if not source_roots:
        raise SystemExit(SOURCE_ROOTS_REQUIRED)
    try:
        normalize_source_roots(source_roots)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    log_file = Path("data/logs") / f"scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logger = _setup_logging(log_file, args.log_level)
    logger.info("Scan started. Log file: %s", log_file)
    _reset_progress_state()
    try:
        with ensure_database(db_path) as connection:
            summary = scan_directories(
                source_roots,
                connection,
                progress_callback=lambda current, total, detail: _emit_progress(
                    current, total, detail, prefix="Scanning"
                ),
                listing_callback=lambda current, total, detail: _emit_progress(
                    current, total, detail, prefix="Listing"
                ),
                error_callback=lambda message: _display.error(message),
                workers=max(1, args.workers),
                log_file=str(log_file),
                log_level=args.log_level,
                prune=not args.no_prune,
                force_prune=args.force_prune,
                force_rescan=args.force_rescan,
                allow_missing_embeddings=args.allow_missing_embeddings,
            )
    except ScanAborted as error:
        raise SystemExit(f"Scan aborted: {error}") from error
    if len(summary["roots"]) > 1:
        for part in summary["roots"]:
            print(
                f"  {part['root']}: {part['processed']} scanned, {part['skipped']} skipped, "
                f"removed {part['pruned']}"
            )
    print(
        f"Scanned {summary['processed']} media entries "
        f"(skipped {summary['skipped']}, faces {summary['faces']}, "
        f"removed {summary['pruned']}, errors {summary['errors']})."
    )
    if summary.get("excluded"):
        print(
            f"走査の対象外になった拡張子（HEIC など）の行を {summary['excluded']} 件削除しました。"
        )


def _run_match(args, db_path: str) -> None:
    log_file = Path("data/logs") / f"match_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logger = _setup_logging(log_file, args.log_level)
    logger.info("Match started. Log file: %s", log_file)
    _reset_progress_state()
    with ensure_database(db_path) as connection:
        summary = match_faces(
            connection,
            threshold=args.threshold,
            margin=args.margin,
            reset=not args.no_reset,
            dry_run=args.dry_run,
            progress_callback=lambda current, total, detail: _emit_progress(
                current, total, detail, prefix="Matching"
            ),
        )
    if summary["teachers"] == 0:
        print(
            "手本になる顔がありません。photoarchive-gui で顔を人物に割り当ててから"
            " 再実行してください。"
        )
        return
    label = "(dry-run) " if summary["dry_run"] else ""
    print(
        f"{label}Matched {summary['assigned']} faces from {summary['teachers']} assigned faces; "
        f"{summary['unassigned']} left unassigned."
    )
    if summary.get("unusable_teachers"):
        print(
            f"5点整列ができなかった手本 {summary['unusable_teachers']} 件は、"
            "割り当ての根拠にしていません（2位の対抗馬としては使います）。"
        )
    if summary["histogram"]:
        print("距離の分布:")
        for bucket in sorted(summary["histogram"]):
            print(f"  {bucket:.1f}-{bucket + 0.1:.1f}: {summary['histogram'][bucket]}")


def _parse_thresholds(raw: str) -> List[float]:
    """カンマ区切りの閾値を読む。読めない値は握り潰さずに止める。

    **同じ閾値を2度数えない。** 集計先は閾値の値で引くので、重複すると
    片方が0件、もう片方が2倍になり、正解率が 200% になる。表に
    「0.40 で正解 0.0%」という行が並ぶと、閾値を緩める方向に判断が傾く。

    **`nan` や `inf` も弾く。** `float("nan")` は読めてしまうが、
    `best_distance > nan` が常に False なので**閾値を掛けていないのと同じ**
    判定になる。0 以下も、何も割り当たらない表が出るだけで意味がない。
    """
    values: List[float] = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = float(part)
        except ValueError:
            raise SystemExit(f"閾値として読めない値です: {part}") from None
        if not math.isfinite(value) or value <= 0:
            raise SystemExit(f"閾値は正の有限の数で指定してください: {part}")
        if value in values:
            continue
        values.append(value)
    if not values:
        raise SystemExit("閾値が1つも指定されていません。")
    return values


def _resolve_person(connection, wanted: str) -> dict:
    """``--person`` に渡された名前か id から人物を1人に決める。

    **同じ名前の人物が複数いたら決めない。** `Person.name` に UNIQUE 制約は
    まだ無いので（仕様書 Phase 3 手順7）、勝手に1人目を選ぶと**別人の
    割り当てを消す。**
    """
    persons = db.list_persons(connection)
    if wanted.isdigit():
        matched = [person for person in persons if int(person["id"]) == int(wanted)]
    else:
        matched = [person for person in persons if person["name"] == wanted]
    if not matched:
        known = "、".join(f"{person['id']}:{person['name']}" for person in persons) or "（登録なし）"
        raise SystemExit(f"人物が見つかりません: {wanted}\n登録されている人物: {known}")
    if len(matched) > 1:
        ids = "、".join(str(person["id"]) for person in matched)
        raise SystemExit(
            f"同じ名前の人物が複数います: {wanted}（id {ids}）。--person に id を渡してください。"
        )
    return matched[0]


def _run_unassign_auto(args, db_path: str) -> None:
    """自動割り当てだけを消す。**手本と除外には触らない。**

    `match` は流すたびに自動割り当てを付け直すので、これは「`match` の結果が
    信用できないので、いったん無かったことにする」ための口。
    手本を直してから `photoarchive match` を流せば付け直せる。
    """
    with ensure_database(db_path) as connection:
        person = _resolve_person(connection, args.person) if args.person else None
        person_id = int(person["id"]) if person else None
        scope = f"{person['name']} の" if person else "全員の"
        before = db.count_faces(
            connection, assign_source=db.ASSIGN_AUTO, person_id=person_id
        )
        if args.dry_run:
            print(f"(dry-run) Would remove {before} automatic assignments ({scope}).")
            return
        removed = db.reset_auto_assignments(connection, person_id=person_id)
        # **family_score を数え直す。** 消しただけだと、もう存在しない
        # 自動割り当てから計算されたスコアが AnalysisResult に残り、
        # `select` の family_only がその古い値で写真を選ぶ。
        db.recompute_family_scores(connection)
    print(
        f"Removed {removed} automatic assignments ({scope}) "
        "(manual assignments and rejects are untouched); family_score recomputed."
    )


def _run_evaluate(args, db_path: str) -> None:
    log_file = Path("data/logs") / f"evaluate_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logger = _setup_logging(log_file, args.log_level)
    logger.info("Evaluate started. Log file: %s", log_file)
    _reset_progress_state()
    thresholds = _parse_thresholds(args.thresholds)
    with ensure_database(db_path) as connection:
        summary = evaluate_match(
            connection,
            thresholds=thresholds,
            margin=args.margin,
            keep_same_media=args.keep_same_media,
            progress_callback=lambda current, total, detail: _emit_progress(
                current, total, detail, prefix="Evaluating"
            ),
        )
    print(format_report(summary))


# データベースを使わないサブコマンド。DBパスの解決を要求しない。
_COMMANDS_WITHOUT_DATABASE = {"convert-heic"}


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    settings = load_settings()
    db_path = getattr(args, "db", None) or get_database_path(settings)
    if not db_path and args.command not in _COMMANDS_WITHOUT_DATABASE:
        legacy = find_legacy_settings_path()
        if legacy is not None:
            raise SystemExit(legacy_settings_stop_message(legacy))
        raise SystemExit("Database path is required via application settings or --db.")

    try:
        if args.command == "migrate":
            _run_migrate(args, db_path)
            return

        if args.command == "init-db":
            ensure_database(db_path).close()
            print(f"Database initialized: {db_path}")
            return

        if args.command == "scan":
            _run_scan(args, settings)
            return

        if args.command == "reembed":
            _run_reembed(args, db_path)
            return

        if args.command == "convert-heic":
            _reset_progress_state()
            source_roots = _source_roots(args, settings)
            if not source_roots:
                raise SystemExit(SOURCE_ROOTS_REQUIRED)

            unreadable: List[Path] = []

            def ask_to_continue(message: str) -> bool:
                # 描いている進捗の2行を消してから聞く。消さないと、答えたあとの
                # 書き直しが問いの行を巻き込み、古いバーが残る
                _display.clear()
                answer = input(f"{message}\nContinue with the next file? [y/N]: ")
                return answer.strip().lower() in {"y", "yes"}

            def confirm_write_error(path: Path, error: Exception) -> bool:
                return ask_to_continue(f"Write failed for {path}: {error}")

            def confirm_read_error(path: Path, error: Exception) -> bool:
                # 書き込み先ではなく、元の HEIC が読めない（壊れている）
                if ask_to_continue(f"Cannot read {path} (the file may be damaged): {error}"):
                    unreadable.append(path)
                    return True
                return False

            converted = skipped = 0
            try:
                for root in source_roots:
                    _reset_progress_state()
                    done, already = convert_heic_files(
                        root,
                        progress_callback=lambda current, total, detail: _emit_progress(
                            current, total, detail, prefix="Converting"
                        ),
                        confirm_write_error=confirm_write_error,
                        confirm_read_error=confirm_read_error,
                    )
                    converted += done
                    skipped += already
            except (OSError, PermissionError, ValueError) as error:
                raise SystemExit(f"Conversion stopped: {error}") from error
            print(f"Converted {converted} files; skipped {skipped} existing files.")
            if unreadable:
                print(f"Could not read {len(unreadable)} files (left as they are):")
                for path in unreadable:
                    print(f"  {path}")
            return

        if args.command == "match":
            _run_match(args, db_path)
            return

        if args.command == "unassign-auto":
            _run_unassign_auto(args, db_path)
            return

        if args.command == "evaluate":
            _run_evaluate(args, db_path)
            return

        if args.command == "select":
            _reset_progress_state()
            output_root = getattr(args, "output", None) or get_output_root(settings)
            rule_path = getattr(args, "rule", None) or get_rule_path(settings)
            if not output_root:
                raise SystemExit("Output path is required via application settings or --output.")
            if not rule_path:
                raise SystemExit("Rule file path is required via application settings or --rule.")
            try:
                rule = load_rule(rule_path)
            except (OSError, ValueError) as error:
                raise SystemExit(str(error)) from error
            with ensure_database(db_path) as connection:
                source_roots = _source_roots(args, settings, connection)
                if not source_roots:
                    raise SystemExit(SOURCE_ROOTS_REQUIRED)
                notice = stale_assignment_notice(connection)
                if notice:
                    print(f"注意: {notice}")
                selected = select_media(
                    connection,
                    rule,
                    progress_callback=lambda current, total, detail: _emit_progress(
                        current, total, detail, prefix="Measuring"
                    ),
                )
                _reset_progress_state()
                linked = link_selected_media(
                    selected,
                    output_root,
                    source_roots,
                    progress_callback=lambda current, total, detail: _emit_progress(
                        current, total, detail, prefix="Linking"
                    ),
                )
            print(f"Linked {linked} files in {output_root}.")
            return
    except SchemaVersionError as error:
        raise SystemExit(str(error)) from error

    parser.print_help()
