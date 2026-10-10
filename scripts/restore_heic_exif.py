#!/usr/bin/env python3
"""`convert-heic` が撮影日時を落とした JPEG に、隣の HEIC の EXIF を戻す（PR #80）。

#80 より前の `convert-heic` は EXIF を引き継がず、変換した JPEG は `scan` で
撮影日時なしとして登録された。**JPEG を書き直すと `scan` がハッシュの違いから
顔を検出し直し、手動・自動の割り当てが消える。** そこで、

1. JPEG の**画素データのバイト列には触れず**、EXIF の APP1 だけを差し込む
   （`converter.insert_exif`。差し込んだ APP1 を除けば元のバイト列に戻る）
2. DB の ``file_hash`` / ``file_size`` / ``created_time`` / ``shooting_date`` を、
   書き換えたファイルに合わせる。``scan`` は大きさと更新時刻が一致すれば
   読まないので、顔は検出し直されない

という2段で、ファイルと DB を食い違わせずに撮影日時を戻す（利用者の決定・
PR #80 のレビュー判断 (a) の案4）。

**既定は数えるだけ。** ``--apply`` を付けたときだけ書く。書く前に DB の控えを
取る（`migration.backup_database`。``--no-backup`` で省ける）。

対象は、DB で撮影日時が NULL の ``.jpg`` のうち、同じフォルダに同名の HEIC/HEIF が
あるもの。次のどれかに当たれば触らず、理由ごとに数える。

- JPEG がすでに EXIF を持つ / HEIC に撮影日時が無い / HEIC の EXIF が回転を求める
- ファイルの大きさか更新時刻が DB と違う（`scan` の後に変わった。`scan` に任せる）
- HEIC と JPEG が同じ写真に見えない（`converter._same_image`）

使い方::

    python scripts/restore_heic_exif.py --db data/photoarchive.db
    python scripts/restore_heic_exif.py --db data/photoarchive.db --apply
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image  # noqa: E402

from photoarchive_ai.converter import (  # noqa: E402
    HEIC_EXTENSIONS,
    _same_image,
    has_exif,
    insert_exif,
)
from photoarchive_ai.migration import backup_database  # noqa: E402
from photoarchive_ai.scanner import _mtime_matches, extract_exif_datetime  # noqa: E402

_ORIENTATION = 0x0112


def sibling_heic(jpeg: Path) -> Optional[Path]:
    """同じフォルダにある同名の HEIC/HEIF。無ければ None。"""
    try:
        names = os.listdir(jpeg.parent)
    except OSError:
        return None
    for name in sorted(names):
        candidate = Path(name)
        if candidate.stem == jpeg.stem and candidate.suffix.lower() in HEIC_EXTENSIONS:
            return jpeg.parent / name
    return None


def candidates(connection: sqlite3.Connection) -> Iterator[Tuple[int, Path, sqlite3.Row]]:
    rows = connection.execute(
        "SELECT id, path, file_size, created_time FROM Media"
        " WHERE shooting_date IS NULL AND lower(path) LIKE '%.jpg' ORDER BY path"
    ).fetchall()
    for row in rows:
        path = Path(row["path"])
        if sibling_heic(path) is not None:
            yield row["id"], path, row


def heic_exif(heic: Path) -> Tuple[Optional[bytes], Optional[str]]:
    """差し込んでよい EXIF と、だめなときの理由。"""
    with Image.open(heic) as image:
        exif = image.info.get("exif")
        if not exif:
            return None, "HEIC に EXIF が無い"
        parsed = image.getexif()
        if parsed.get(_ORIENTATION) not in (None, 1):
            # 変換済みの JPEG は回転済み。回転を求める EXIF を足すと二重に回る
            return None, "HEIC の EXIF が回転を求める"
        return exif, None


def plan(row: sqlite3.Row, jpeg: Path, heic: Path) -> Tuple[Optional[bytes], str]:
    """書き込む新しいバイト列と、書かないときの理由。"""
    stat = jpeg.stat()
    if row["file_size"] != stat.st_size or not _mtime_matches(row["created_time"], stat.st_mtime):
        return None, "DB と大きさか更新時刻が違う"
    data = jpeg.read_bytes()
    if has_exif(data):
        return None, "JPEG がすでに EXIF を持つ"
    exif, reason = heic_exif(heic)
    if exif is None:
        return None, reason
    if not _same_image(heic, jpeg):
        return None, "HEIC と同じ写真に見えない"
    updated = insert_exif(data, exif)
    # 足したのは APP1 だけで、残りは1バイトも変わっていないこと
    added = len(updated) - len(data)
    at = len(os.path.commonprefix([data, updated]))
    if updated[:at] + updated[at + added :] != data:
        return None, "差し込みで画素データが変わった"
    return updated, "戻せる"


def write_and_record(connection: sqlite3.Connection, media_id: int, jpeg: Path, updated: bytes) -> str:
    """ファイルを書き換え、DB をそれに合わせる。撮影日時を返す。"""
    temporary = jpeg.with_name(f".{jpeg.name}.exif-tmp")
    temporary.write_bytes(updated)
    os.chmod(temporary, jpeg.stat().st_mode & 0o777)
    os.replace(temporary, jpeg)
    stat = jpeg.stat()
    shooting_date = extract_exif_datetime(jpeg)
    if shooting_date is None:
        raise RuntimeError(f"書き込んだ JPEG から撮影日時が読めない: {jpeg}")
    # scan の analyze_file と同じ値を書く（大きさ・更新時刻が一致すれば scan は読まない）
    connection.execute(
        "UPDATE Media SET file_hash = ?, file_size = ?, created_time = ?, shooting_date = ?"
        " WHERE id = ?",
        (
            hashlib.sha256(updated).hexdigest(),
            stat.st_size,
            datetime.fromtimestamp(stat.st_mtime).isoformat(),
            shooting_date,
            media_id,
        ),
    )
    connection.commit()
    return shooting_date


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="データベースのパス")
    parser.add_argument("--apply", action="store_true", help="実際に書く（既定は数えるだけ）")
    parser.add_argument("--no-backup", action="store_true", help="--apply の前に DB の控えを取らない")
    args = parser.parse_args(argv)

    if args.apply and not args.no_backup:
        print(f"DB の控え: {backup_database(args.db)}")

    connection = sqlite3.connect(args.db)
    connection.row_factory = sqlite3.Row
    reasons: Counter = Counter()
    restored = 0
    try:
        for media_id, jpeg, row in candidates(connection):
            try:
                updated, reason = plan(row, jpeg, sibling_heic(jpeg))
            except Exception as error:
                updated, reason = None, f"読めない（{type(error).__name__}）"
            reasons[reason] += 1
            if updated is None:
                if reason != "戻せる":
                    print(f"  触らない: {reason}: {jpeg}")
                continue
            if args.apply:
                write_and_record(connection, media_id, jpeg, updated)
                restored += 1
    finally:
        connection.close()

    for reason, count in reasons.most_common():
        print(f"{reason}: {count} 件")
    if args.apply:
        print(f"撮影日時を戻した: {restored} 件")
    else:
        print("数えただけです。書くには --apply を付けてください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
