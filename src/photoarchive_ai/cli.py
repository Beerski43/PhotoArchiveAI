import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from .analyzer import analyze_database, get_latest_error
from .config import get_database_path, get_output_root, get_rule_path, get_source_root, load_settings
from .db import ensure_database
from .scanner import scan_directory
from .selection import copy_selected_media, load_rule, select_media


def _setup_logging(log_file: Optional[Path] = None, log_level: str = "WARNING") -> logging.Logger:
    """Set up file logging for the CLI and all analyzer children."""
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


def _emit_progress(
    current: int,
    total: int,
    detail: str,
    prefix: str = "Progress",
    error: str = "",
) -> None:
    global _progress_started
    bar_width = 20
    percent = min(100, max(0, int(current * 100 / total))) if total > 0 else 0
    filled = int(bar_width * current / total) if total > 0 else 0
    bar = "#" * filled + "-" * (bar_width - filled)
    progress_detail = str(detail).replace("\r", " ").replace("\n", " ")
    error_detail = str(error).replace("\r", " ").replace("\n", " ")[:120]
    progress_line = f"{prefix}: [{bar}] {percent:3d}% ({current}/{total}) {progress_detail}"
    error_line = f"Error: {error_detail}" if error_detail else "Error: none"
    if _progress_started:
        sys.stdout.write(f"\033[2A\r{progress_line}\033[K\n\r{error_line}\033[K")
    else:
        sys.stdout.write(f"{progress_line}\n{error_line}")
        _progress_started = True
    sys.stdout.flush()
    if current >= total:
        sys.stdout.write("\n")
        _progress_started = False


def main() -> None:
    global _progress_started
    parser = argparse.ArgumentParser(prog="photoarchive")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init-db", help="Initialize SQLite database.")
    init_parser.add_argument("--db", help="SQLite database path.")

    scan_parser = subparsers.add_parser("scan", help="Scan source media into the database.")
    scan_parser.add_argument("--source", help="Source directory to scan.")
    scan_parser.add_argument("--db", help="SQLite database path.")

    analyze_parser = subparsers.add_parser("analyze", help="Run AI analysis on scanned media.")
    analyze_parser.add_argument("--db", help="SQLite database path.")
    analyze_parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        type=str.upper,
        default="WARNING",
        help="Log verbosity (default: WARNING).",
    )

    select_parser = subparsers.add_parser("select", help="Select media by rule and copy to output.")
    select_parser.add_argument("--db", help="SQLite database path.")
    select_parser.add_argument("--rule", help="JSON or YAML rule file path.")
    select_parser.add_argument("--output", help="Output directory for selected media.")
    select_parser.add_argument("--source", help="Source root directory for relative output paths.")

    args = parser.parse_args()
    settings = load_settings()
    db_path = getattr(args, "db", None) or get_database_path(settings)
    if not db_path:
        raise SystemExit("Database path is required via application settings or --db.")

    if args.command == "init-db":
        ensure_database(db_path).close()
        print(f"Database initialized: {db_path}")
        return

    if args.command == "scan":
        source_root = getattr(args, "source", None) or get_source_root(settings)
        if not source_root:
            raise SystemExit("Source root is required either via --source or application settings.")
        with ensure_database(db_path) as connection:
            media_ids = scan_directory(
                source_root,
                connection,
                progress_callback=lambda current, total, detail: _emit_progress(current, total, detail, prefix="Scanning"),
            )
        print(f"Scanned {len(media_ids)} media entries.")
        return

    if args.command == "analyze":
        log_file = Path("data/logs") / f"analyze_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        logger = _setup_logging(log_file, args.log_level)
        logger.info(f"Analysis started. Log file: {log_file}")
        _progress_started = False
        try:
            with ensure_database(db_path) as connection:
                analyze_database(
                    connection,
                    progress_callback=lambda current, total, detail: _emit_progress(
                        current,
                        total,
                        detail,
                        prefix="Analyzing",
                        error=get_latest_error(),
                    ),
                )
            logger.info("Analysis completed successfully.")
        except Exception as e:
            logger.error(f"Analysis failed: {e}", exc_info=True)
            _emit_progress(0, 1, "aborted", prefix="Analyzing", error=str(e))
            raise SystemExit(1) from e
        return

    if args.command == "select":
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
                progress_callback=lambda current, total, detail: _emit_progress(current, total, detail, prefix="Copying"),
            )
        print(f"Copied {copied} files to {output_root}.")
        return

    parser.print_help()
