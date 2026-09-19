import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import get_database_path, get_output_root, get_rule_path, get_source_root, load_settings
from .converter import convert_heic_files
from .db import SchemaVersionError, ensure_database
from .face import get_latest_error
from .matcher import DEFAULT_MARGIN, DEFAULT_THRESHOLD, match_faces
from .migration import describe_migration, migrate_database, needs_migration
from .scanner import ScanAborted, scan_directory
from .selection import copy_selected_media, load_rule, select_media


def _setup_logging(log_file: Optional[Path] = None, log_level: str = "WARNING") -> logging.Logger:
    """Set up file logging for the CLI and all photoarchive children."""
    logger = logging.getLogger("photoarchive")
    level = getattr(logging, log_level.upper(), None)
    if not isinstance(level, int):
        raise ValueError(f"Invalid log level: {log_level}")
    logger.setLevel(level)
    logger.handlers.clear()

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(level)
        file_format = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        file_handler.setFormatter(file_format)
        logger.addHandler(file_handler)

    return logger


_progress_started = False
#: 直近に出たエラー。エラーが出ていない回で "none" に塗り潰さないために持つ。
_last_error = ""
#: 2行目に出せる長さ。これ以上は切る。
ERROR_DISPLAY_LIMIT = 120


def _reset_progress_state() -> None:
    """進捗表示を最初から始める。コマンドの入口で呼ぶ。"""
    global _progress_started, _last_error
    _progress_started = False
    _last_error = ""


def _emit_progress(
    current: int,
    total: int,
    detail: str,
    prefix: str = "Progress",
    error: str = "",
) -> None:
    """2行の進捗を書き換える。1行目が進捗、2行目が直近のエラー。

    2行目は **直近のエラーを保持する**。エラーの出なかった回で
    ``Error: none`` に戻すと、流れていくログの中でエラーが一瞬しか
    見えず、何が起きたのか分からなくなる(Issue #25)。
    """
    global _progress_started, _last_error
    bar_width = 20
    percent = min(100, max(0, int(current * 100 / total))) if total > 0 else 0
    filled = int(bar_width * current / total) if total > 0 else 0
    bar = "#" * filled + "-" * (bar_width - filled)
    progress_detail = str(detail).replace("\r", " ").replace("\n", " ")
    error_detail = str(error).replace("\r", " ").replace("\n", " ")[:ERROR_DISPLAY_LIMIT]
    if error_detail:
        _last_error = error_detail
    progress_line = f"{prefix}: [{bar}] {percent:3d}% ({current}/{total}) {progress_detail}"
    error_line = f"Error: {_last_error}" if _last_error else "Error: none"
    if _progress_started:
        sys.stdout.write(f"\033[2A\r{progress_line}\033[K\n\r{error_line}\033[K")
    else:
        sys.stdout.write(f"{progress_line}\n{error_line}")
        _progress_started = True
    sys.stdout.flush()
    if current >= total:
        sys.stdout.write("\n")
        _progress_started = False


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
        "--no-vacuum", action="store_true", help="Skip VACUUM after the migration."
    )
    migrate_parser.add_argument(
        "--yes", action="store_true", help="Do not ask for confirmation."
    )

    scan_parser = subparsers.add_parser(
        "scan", help="Scan source media, detect faces and store them in the database."
    )
    scan_parser.add_argument("--source", help="Source directory to scan.")
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
            "顔特徴量のモデルが読めなくてもスキャンを続ける。"
            "顔は検出されるが match が効かない状態になる。"
        ),
    )
    scan_parser.add_argument(
        "--force-rescan", action="store_true", help="Detect faces again for every media file."
    )
    _add_log_level(scan_parser)

    convert_parser = subparsers.add_parser("convert-heic", help="Convert HEIC/HEIF files to JPEG.")
    convert_parser.add_argument("--source", help="Directory to convert recursively.")

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

    select_parser = subparsers.add_parser("select", help="Select media by rule and copy to output.")
    select_parser.add_argument("--db", help="SQLite database path.")
    select_parser.add_argument("--rule", help="JSON or YAML rule file path.")
    select_parser.add_argument("--output", help="Output directory for selected media.")
    select_parser.add_argument("--source", help="Source root directory for relative output paths.")

    return parser


def _run_migrate(args, db_path: str) -> None:
    if not Path(db_path).exists():
        raise SystemExit(f"Database does not exist: {db_path}")
    if not needs_migration(db_path):
        print("スキーマはすでに最新です。移行は不要です。")
        return
    info = describe_migration(db_path)
    print(f"移行対象: {db_path}")
    print(f"  Media {info['media']} 件 / Person {info['persons']} 件 は保持します。")
    print(
        f"  顔データ {info['faces_to_drop']} 件と解析結果 {info['analysis_to_drop']} 件は破棄し、"
        " 顔検出をやり直します。"
    )
    if not args.yes:
        answer = input("続行しますか? [y/N]: ")
        if answer.strip().lower() not in {"y", "yes"}:
            raise SystemExit("移行を中止しました。")
    migrate_database(
        db_path,
        backup_path=args.backup,
        vacuum=not args.no_vacuum,
        log=print,
    )
    print("次の手順: photoarchive scan → photoarchive-gui で顔を割り当て → photoarchive match")


def _run_scan(args, settings) -> None:
    source_root = getattr(args, "source", None) or get_source_root(settings)
    if not source_root:
        raise SystemExit("Source root is required either via --source or application settings.")
    log_file = Path("data/logs") / f"scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logger = _setup_logging(log_file, args.log_level)
    logger.info("Scan started. Log file: %s", log_file)
    _reset_progress_state()
    db_path = getattr(args, "db", None) or get_database_path(settings)
    try:
        with ensure_database(db_path) as connection:
            summary = scan_directory(
                source_root,
                connection,
                progress_callback=lambda current, total, detail: _emit_progress(
                    current, total, detail, prefix="Scanning", error=get_latest_error()
                ),
                workers=max(1, args.workers),
                prune=not args.no_prune,
                force_prune=args.force_prune,
                force_rescan=args.force_rescan,
                allow_missing_embeddings=args.allow_missing_embeddings,
            )
    except ScanAborted as error:
        raise SystemExit(f"Scan aborted: {error}") from error
    print(
        f"Scanned {summary['processed']} media entries "
        f"(skipped {summary['skipped']}, faces {summary['faces']}, "
        f"removed {summary['pruned']}, errors {summary['errors']})."
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
    if summary["histogram"]:
        print("距離の分布:")
        for bucket in sorted(summary["histogram"]):
            print(f"  {bucket:.1f}-{bucket + 0.1:.1f}: {summary['histogram'][bucket]}")


# データベースを使わないサブコマンド。DBパスの解決を要求しない。
_COMMANDS_WITHOUT_DATABASE = {"convert-heic"}


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    settings = load_settings()
    db_path = getattr(args, "db", None) or get_database_path(settings)
    if not db_path and args.command not in _COMMANDS_WITHOUT_DATABASE:
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

        if args.command == "convert-heic":
            _reset_progress_state()
            source_root = getattr(args, "source", None) or get_source_root(settings)
            if not source_root:
                raise SystemExit("Source root is required via --source or application settings.")

            def confirm_write_error(path: Path, error: Exception) -> bool:
                answer = input(
                    f"Write failed for {path}: {error}\nContinue with the next file? [y/N]: "
                )
                return answer.strip().lower() in {"y", "yes"}

            try:
                converted, skipped = convert_heic_files(
                    source_root,
                    progress_callback=lambda current, total, detail: _emit_progress(
                        current, total, detail, prefix="Converting"
                    ),
                    confirm_write_error=confirm_write_error,
                )
            except (OSError, PermissionError) as error:
                raise SystemExit(f"Conversion stopped: {error}") from error
            print(f"Converted {converted} files; skipped {skipped} existing files.")
            return

        if args.command == "match":
            _run_match(args, db_path)
            return

        if args.command == "select":
            _reset_progress_state()
            source_root = getattr(args, "source", None) or get_source_root(settings)
            output_root = getattr(args, "output", None) or get_output_root(settings)
            rule_path = getattr(args, "rule", None) or get_rule_path(settings)
            if not source_root:
                raise SystemExit("Source root is required via application settings or --source.")
            if not output_root:
                raise SystemExit("Output path is required via application settings or --output.")
            if not rule_path:
                raise SystemExit("Rule file path is required via application settings or --rule.")
            with ensure_database(db_path) as connection:
                rule = load_rule(rule_path)
                selected = select_media(connection, rule)
                copied = copy_selected_media(
                    selected,
                    output_root,
                    source_root,
                    progress_callback=lambda current, total, detail: _emit_progress(
                        current, total, detail, prefix="Copying"
                    ),
                )
            print(f"Copied {copied} files to {output_root}.")
            return
    except SchemaVersionError as error:
        raise SystemExit(str(error)) from error

    parser.print_help()
