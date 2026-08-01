import argparse
import json
import os
from pathlib import Path
from typing import Dict

from .analyzer import analyze_database
from .db import ensure_database
from .scanner import scan_directory
from .selection import copy_selected_media, load_rule, select_media


def main() -> None:
    parser = argparse.ArgumentParser(prog="photoarchive")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init-db", help="Initialize SQLite database.")
    init_parser.add_argument("--db", required=True, help="SQLite database path.")

    scan_parser = subparsers.add_parser("scan", help="Scan source media into the database.")
    scan_parser.add_argument("--source", required=True, help="Source directory to scan.")
    scan_parser.add_argument("--db", required=True, help="SQLite database path.")

    analyze_parser = subparsers.add_parser("analyze", help="Run AI analysis on scanned media.")
    analyze_parser.add_argument("--db", required=True, help="SQLite database path.")

    select_parser = subparsers.add_parser("select", help="Select media by rule and copy to output.")
    select_parser.add_argument("--db", required=True, help="SQLite database path.")
    select_parser.add_argument("--rule", required=True, help="JSON or YAML rule file path.")
    select_parser.add_argument("--output", required=True, help="Output directory for selected media.")
    select_parser.add_argument("--source", required=True, help="Source root directory for relative output paths.")

    args = parser.parse_args()

    if args.command == "init-db":
        ensure_database(args.db).close()
        print(f"Database initialized: {args.db}")
        return

    if args.command == "scan":
        with ensure_database(args.db) as connection:
            media_ids = scan_directory(args.source, connection)
        print(f"Scanned {len(media_ids)} media entries.")
        return

    if args.command == "analyze":
        with ensure_database(args.db) as connection:
            analyze_database(connection)
        print("Analysis completed.")
        return

    if args.command == "select":
        with ensure_database(args.db) as connection:
            rule = load_rule(args.rule)
            selected = select_media(connection, rule)
            copied = copy_selected_media(selected, args.output, args.source)
        print(f"Copied {copied} files to {args.output}.")
        return

    parser.print_help()
