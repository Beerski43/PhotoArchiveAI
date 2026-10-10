"""連写・似た写真の束ね方を実データで測る（#86）。**DB は読み取り専用で開く。**

`select` と同じ手順（`similar.candidate_runs` → `similar.measure` → `similar.scenes`）で、
家族の写った写真を束ね、閾値ごとに何枚減るかを数える。目で確かめるための
見比べ画像（距離の帯ごとに、隣り合う2枚を並べたもの）も書き出す。

    python scripts/measure_similar_photos.py --db data/photoarchive.db --out <リポジトリの外>

見た目の値は ``<out>/looks.json`` に残し、2回目からは読まない（NFS の読み直しを避ける）。
**DB には書かない**（`select` が書く `Media.look_hash` があればそれも使う）。
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from photoarchive_ai import similar  # noqa: E402
from photoarchive_ai.progress import ProgressDisplay  # noqa: E402

THRESHOLDS = (4, 6, 8, 10, 12, 14, 16, 20)
BANDS = ((0, 6), (7, 10), (11, 14), (15, 18), (19, 24))


def _load(db_path: str):
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    family: dict = {}
    for row in connection.execute(
        "SELECT media_id, person_id FROM Face"
        " WHERE person_id IS NOT NULL AND assign_source IN ('manual', 'auto')"
    ):
        family.setdefault(row["media_id"], set()).add(row["person_id"])
    family_of = {key: frozenset(value) for key, value in family.items()}
    # 版7（移行前）の DB にも流せるように、列が無ければ NULL として読む。
    columns = {row[1] for row in connection.execute("PRAGMA table_info(Media)")}
    look = "look_hash" if "look_hash" in columns else "NULL AS look_hash"
    media = [
        dict(row)
        for row in connection.execute(
            f"SELECT id, path, type, shooting_date, {look} FROM Media"
        )
        if row["id"] in family_of
    ]
    connection.close()
    return media, family_of


def _thumbnail(path: str, size: int = 240) -> Image.Image:
    with open(path, "rb") as handle:
        image = similar.exif_thumbnail(handle.read(similar.HEAD_BYTES))
    if image is None:
        image = Image.open(path)
        image.draft("RGB", (size, size))
    image = image.convert("RGB")
    image.thumbnail((size, size))
    return image


def _sheet(pairs, path: Path) -> None:
    """2枚ずつ横に並べ、距離を書いた見比べ画像。"""
    cell = 240
    sheet = Image.new("RGB", (cell * 2 + 10, (cell + 20) * len(pairs)), "white")
    draw = ImageDraw.Draw(sheet)
    for row, (gap, seconds, left, right) in enumerate(pairs):
        top = row * (cell + 20)
        draw.text((4, top + 2), f"distance {gap} / {seconds:.0f}s", fill="black")
        for column, item in enumerate((left, right)):
            try:
                sheet.paste(_thumbnail(item), (column * (cell + 10), top + 18))
            except Exception:
                draw.text((column * (cell + 10) + 4, top + 40), "unreadable", fill="red")
    sheet.save(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True)
    parser.add_argument("--out", required=True, help="結果の置き場（リポジトリの外）")
    parser.add_argument("--seconds", type=float, default=similar.DEFAULT_SECONDS)
    parser.add_argument("--samples", type=int, default=12, help="帯ごとの見比べの組数")
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cache_path = out / "looks.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    media, family_of = _load(args.db)
    runs = similar.candidate_runs(media, family_of, args.seconds)
    members = [item for run in runs for item in run]
    print(f"家族の写った写真 {len(media)} 枚 / 候補の連なり {len(runs)} 個・{len(members)} 枚")

    looks = {}
    todo = []
    for item in members:
        value = item.get("look_hash") or cache.get(str(item["id"]))
        if similar.decode(value) is None:
            todo.append(item)
        else:
            looks[item["id"]] = value
    display = ProgressDisplay() if todo else None
    for done, item in enumerate(todo, start=1):
        value = similar.measure(item["path"])
        if value is not None:
            looks[item["id"]] = value
            cache[str(item["id"])] = value
        if display is not None:
            display.update(done, len(todo), Path(item["path"]).name, prefix="Measuring")
        if done % 500 == 0:
            cache_path.write_text(json.dumps(cache))
    if display is not None:
        display.finish()
    cache_path.write_text(json.dumps(cache))

    sources = {}
    for value in looks.values():
        source = similar.decode(value)[0]
        sources[source] = sources.get(source, 0) + 1
    print(f"測れた {len(looks)} / {len(members)} 枚（取った場所: {sources}）")

    print("閾値ごとに減る枚数（候補の連なりの中だけ）")
    for threshold in THRESHOLDS:
        removed = sum(
            len(scene) - 1 for run in runs for scene in similar.scenes(run, looks, threshold)
        )
        print(f"  距離 <= {threshold:2d}: {removed:6d} 枚")

    pairs_by_band = {band: [] for band in BANDS}
    for run in runs:
        for left, right in zip(run, run[1:]):
            gap = similar.distance(looks.get(left["id"]), looks.get(right["id"]))
            if gap is None:
                continue
            seconds = (
                similar.taken_moment(right["shooting_date"])
                - similar.taken_moment(left["shooting_date"])
            ).total_seconds()
            for band in BANDS:
                if band[0] <= gap <= band[1]:
                    pairs_by_band[band].append((gap, seconds, left["path"], right["path"]))
    random.seed(86)
    for band, pairs in pairs_by_band.items():
        chosen = random.sample(pairs, min(args.samples, len(pairs)))
        if chosen:
            name = out / f"pairs_{band[0]:02d}-{band[1]:02d}.jpg"
            _sheet(sorted(chosen), name)
            print(f"  距離 {band[0]}〜{band[1]}: 隣り合う組 {len(pairs)} → {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
