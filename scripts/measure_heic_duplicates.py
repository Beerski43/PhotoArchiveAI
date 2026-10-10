#!/usr/bin/env python3
"""`convert-heic` の「同じ写真か」の閾値を実データで測る（Issue #79）。

**本体のコードを変えない。実データに書かない。** NFS 上の写真を読むので時間がかかる。

HEIC と同名の JPEG（同じ写真）と、HEIC と連番が1つ先の JPEG（別の写真。連写だと
いちばん似る）の画素差を `converter.image_difference` で測り、分布を出す。
``converter.SAME_IMAGE_MAX_DIFF`` が両者のあいだに収まっているかを確かめる道具で、
比べ方や閾値を変えたらこれを流し直すこと。

使い方::

    python scripts/measure_heic_duplicates.py /path/to/root --sample 200
"""

from __future__ import annotations

import argparse
import os
import random
import re
import sys
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from photoarchive_ai.converter import (  # noqa: E402
    HEIC_EXTENSIONS,
    SAME_IMAGE_MAX_DIFF,
    image_difference,
)


def neighbour_name(stem: str) -> Optional[str]:
    """連番が1つ先のファイル名（``IMG_0099`` → ``IMG_0100.jpg``）。番号が無ければ None。"""
    found = re.match(r"^(.*?)(\d+)$", stem)
    if found is None:
        return None
    prefix, number = found.groups()
    return f"{prefix}{int(number) + 1:0{len(number)}d}.jpg"


def heic_with_jpeg(root: Path) -> Iterator[Tuple[Path, Path]]:
    """同名の JPEG がある HEIC と、その JPEG の組。"""
    for directory, _, files in os.walk(root):
        names = set(files)
        for name in files:
            stem, extension = os.path.splitext(name)
            if extension.lower() in HEIC_EXTENSIONS and f"{stem}.jpg" in names:
                yield Path(directory, name), Path(directory, f"{stem}.jpg")


def summarize(label: str, values: List[float]) -> str:
    if not values:
        return f"{label}: 0 件"
    ordered = sorted(values)
    median = ordered[len(ordered) // 2]
    return f"{label}: {len(ordered)} 件  最小 {ordered[0]:.2f}  中央 {median:.2f}  最大 {ordered[-1]:.2f}"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", type=Path)
    parser.add_argument("--sample", type=int, default=200, help="測る HEIC の件数")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)

    pairs = list(heic_with_jpeg(args.root))
    random.Random(args.seed).shuffle(pairs)
    same: List[float] = []
    other: List[float] = []
    resized = 0
    for heic, jpeg in pairs[: args.sample]:
        difference = image_difference(heic, jpeg)
        if difference is None:
            resized += 1
        else:
            same.append(difference)
        neighbour = neighbour_name(heic.stem)
        if neighbour is not None and (heic.parent / neighbour).exists():
            difference = image_difference(heic, heic.parent / neighbour)
            if difference is not None:
                other.append(difference)

    print(summarize("同じ写真（同名 JPEG）", same))
    print(f"  寸法が違った組: {resized} 件")
    print(summarize("別の写真（連番が1つ先）", other))
    print(f"閾値 SAME_IMAGE_MAX_DIFF = {SAME_IMAGE_MAX_DIFF}")
    print(f"  閾値を超えた同じ写真: {sum(value > SAME_IMAGE_MAX_DIFF for value in same)} 件")
    print(f"  閾値以下の別の写真: {sum(value <= SAME_IMAGE_MAX_DIFF for value in other)} 件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
