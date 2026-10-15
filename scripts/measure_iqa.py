"""写真の良し悪しモデル（IQA）を、家族の写った写真で試す（#105）。**DB は読み取り専用で開く。**

家族の写った写真（手本か自動割り当ての顔がある画像）から無作為に選び、元写真を
長辺 1024px に縮小デコードしてモデルに渡す。モデルごとに、1枚あたりの時間・
同じ写真を2回測ったときの揺れ・写真ごとの点を ``<out>/result.json`` に書き、
上位と下位・2つのモデルが食い違った写真を並べた見比べ画像を ``<out>/sheets/`` に書く。

**PyTorch と pyiqa はアプリの依存に入れない。** リポジトリの外の仮想環境で流す::

    python3 -m venv <外>/venv
    <外>/venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
    <外>/venv/bin/pip install pyiqa
    <外>/venv/bin/python -I scripts/measure_iqa.py --db data/photoarchive.db --out <リポジトリの外か data/ の下>

``--models`` で一部だけ測り直せる（前の ``result.json`` の点は残る）。見比べ画像だけを
作り直すときは ``--sheets-only``（モデルを読まない）。**家族の写真なので、出力は
リポジトリの外か ``data/`` の下（git 管理外）に置く。** 結果は ``docs/history/details/2026-10-13-iqa-trial-measured.md``。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

MODELS = ("nima", "musiq", "topiq_nr", "topiq_iaa", "clipiqa")
LONG_SIDE = 1024  # モデルの入力より大きい分は時間の無駄
DRIFT_SAMPLES = 5
SHEET_COUNT = 15
DISAGREE_COUNT = 10
THUMB = 300


def family_photo_ids(connection: sqlite3.Connection, count: int, seed: int = 0) -> list[int]:
    """家族の写った画像から ``count`` 枚を無作為に選ぶ。同じ DB と seed なら同じ並び。"""
    ids = [
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT F.media_id FROM Face F JOIN Media M ON M.id = F.media_id"
            " WHERE F.assign_source IN ('manual', 'auto') AND M.type = 'image'"
            " ORDER BY F.media_id"
        )
    ]
    random.Random(seed).shuffle(ids)
    return ids[:count]


def ranks(values: list[float]) -> list[float]:
    """順位（1 始まり）。同じ値には平均の順位を付ける。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    result = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start
        while end + 1 < len(order) and values[order[end + 1]] == values[order[start]]:
            end += 1
        for position in range(start, end + 1):
            result[order[position]] = (start + end) / 2 + 1
        start = end + 1
    return result


def spearman(first: dict[str, float], second: dict[str, float]) -> float | None:
    """両方で測れた写真だけの順位の相関。2枚未満か、どちらかが全部同じ値なら None。"""
    common = sorted(set(first) & set(second))
    if len(common) < 2:
        return None
    a = ranks([first[key] for key in common])
    b = ranks([second[key] for key in common])
    mean_a, mean_b = sum(a) / len(a), sum(b) / len(b)
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((y - mean_b) ** 2 for y in b)
    if var_a == 0 or var_b == 0:
        return None
    return cov / (var_a * var_b) ** 0.5


def best_first(scores: dict[str, float], lower_better: bool = False) -> list[str]:
    """良い順に並べた写真の ID。点の低いほうが良いモデルは逆にする。"""
    return sorted(scores, key=lambda key: (scores[key], key), reverse=not lower_better)


def disagreements(first: list[str], second: list[str], count: int) -> tuple[list[str], list[str]]:
    """``first`` では上・``second`` では下の写真と、その逆を、順位の差が大きい順に返す。"""
    pos_first = {key: i for i, key in enumerate(first)}
    pos_second = {key: i for i, key in enumerate(second)}
    common = [key for key in first if key in pos_second]
    gap = sorted(common, key=lambda key: (pos_first[key] - pos_second[key], key))
    return gap[:count], gap[::-1][:count]


def load_photo(path: Path, long_side: int) -> Image.Image:
    with Image.open(path) as image:
        image.draft("RGB", (long_side, long_side))
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((long_side, long_side))
        return image.copy()


def measure(images: dict[int, Image.Image], names: list[str], result: dict, out: Path) -> None:
    import pyiqa  # noqa: PLC0415 — アプリの依存に入れないので、測るときだけ読む
    import torch  # noqa: PLC0415
    import torchvision.transforms.functional as TF  # noqa: PLC0415

    torch.set_num_threads(os.cpu_count() or 1)
    for name in names:
        print(f"{name}: loading", flush=True)
        try:
            started = time.time()
            metric = pyiqa.create_metric(name, device="cpu")
            load_s = time.time() - started
        except Exception as error:  # 1つ読めなくても、残りのモデルは測る
            print(f"{name}: load failed {error}", flush=True)
            result["models"][name] = {"error": str(error)}
            continue
        scores = {}
        started = time.time()
        with torch.no_grad():
            for media_id, image in images.items():
                scores[str(media_id)] = float(metric(TF.to_tensor(image).unsqueeze(0)).item())
            per_photo = (time.time() - started) / len(images)
            sample = list(images)[:DRIFT_SAMPLES]
            drift = max(
                abs(float(metric(TF.to_tensor(images[m]).unsqueeze(0)).item()) - scores[str(m)])
                for m in sample
            )
        result["models"][name] = {
            "load_s": load_s,
            "s_per_photo": per_photo,
            "drift": drift,
            "lower_better": bool(metric.lower_better),
        }
        result["scores"][name] = scores
        print(f"{name}: {per_photo:.2f}s/photo load {load_s:.1f}s drift {drift:.2e}", flush=True)
        # 1つ終わるごとに書く。途中で止まっても測れた分は残る
        (out / "result.json").write_text(json.dumps(result))
        del metric


def write_sheet(path: Path, title: str, keys: list[str], thumbs: dict[str, Image.Image],
                scores: dict[str, dict[str, float]]) -> None:
    columns = 5
    rows = max(1, (len(keys) + columns - 1) // columns)
    sheet = Image.new("RGB", (columns * (THUMB + 10), rows * (THUMB + 40) + 40), "white")
    draw = ImageDraw.Draw(sheet)
    draw.text((10, 8), title, fill="black", font=ImageFont.load_default(size=22))
    small = ImageFont.load_default(size=13)
    for i, key in enumerate(keys):
        x, y = (i % columns) * (THUMB + 10) + 5, (i // columns) * (THUMB + 40) + 40
        if key in thumbs:
            sheet.paste(thumbs[key], (x, y))
        caption = f"m{key} " + " ".join(
            f"{name[:5]}={values[key]:.2f}" for name, values in scores.items() if key in values
        )
        draw.text((x, y + THUMB + 4), caption, fill="black", font=small)
    sheet.save(path, quality=85)


def write_sheets(result: dict, paths: dict[str, Path], out: Path) -> None:
    scores = result["scores"]
    order = {
        name: best_first(values, result["models"][name].get("lower_better", False))
        for name, values in scores.items()
    }
    plan = []
    for name, keys in order.items():
        plan.append((f"{name}_top", f"{name}: top {SHEET_COUNT} (best first)", keys[:SHEET_COUNT]))
        plan.append((f"{name}_bottom", f"{name}: bottom {SHEET_COUNT} (worst first)",
                     keys[-SHEET_COUNT:][::-1]))
    names = list(order)
    for i, first in enumerate(names):
        for second in names[i + 1:]:
            high_low, low_high = disagreements(order[first], order[second], DISAGREE_COUNT)
            plan.append((f"{first}_high_{second}_low", f"{first} ranks high, {second} ranks low", high_low))
            plan.append((f"{second}_high_{first}_low", f"{second} ranks high, {first} ranks low", low_high))
    thumbs = {}
    for _, _, keys in plan:
        for key in keys:
            if key not in thumbs and key in paths:
                try:
                    thumbs[key] = load_photo(paths[key], THUMB)
                except OSError as error:
                    print(f"sheet: skip m{key} {error}", file=sys.stderr)
    (out / "sheets").mkdir(exist_ok=True)
    for file_name, title, keys in plan:
        write_sheet(out / "sheets" / f"{file_name}.jpg", title, keys, thumbs, scores)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True)
    parser.add_argument("--out", required=True, help="リポジトリの外か data/ の下（家族の写真が出る。git に入れない）")
    parser.add_argument("--n", type=int, default=200)
    parser.add_argument("--models", nargs="*", default=list(MODELS), choices=MODELS)
    parser.add_argument("--sheets-only", action="store_true", help="モデルを読まず、見比べ画像だけ作る")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    ids = family_photo_ids(connection, args.n)
    result_path = out / "result.json"
    result = json.loads(result_path.read_text()) if result_path.exists() else {"models": {}, "scores": {}}
    # 前の結果で測った写真も見比べ画像に出せるように、点のある写真のパスも引く
    keys = {str(media_id) for media_id in ids}.union(*result["scores"].values())
    paths = {}
    for key in keys:
        row = connection.execute("SELECT path FROM Media WHERE id = ?", (int(key),)).fetchone()
        if row:
            paths[key] = Path(row[0])

    if not args.sheets_only:
        images, skipped, read_s = {}, [], 0.0
        for media_id in ids:
            started = time.time()
            try:
                images[media_id] = load_photo(paths[str(media_id)], LONG_SIDE)
            except OSError as error:
                skipped.append(media_id)
                print(f"skip m{media_id} {error}", file=sys.stderr)
            read_s += time.time() - started
        if not images:
            print("読めた写真がありません", file=sys.stderr)
            return 1
        # 読めなかった写真も数に残す（黙って母数を減らさない）
        result.update(read_s_per_photo=read_s / len(ids), sampled=len(ids), skipped=skipped)
        print(f"read {len(images)}/{len(ids)} photos: {read_s / len(ids):.3f}s/photo", flush=True)
        measure(images, args.models, result, out)

    names = list(result["scores"])
    result["spearman"] = {
        f"{a}/{b}": spearman(result["scores"][a], result["scores"][b])
        for i, a in enumerate(names) for b in names[i + 1:]
    }
    result_path.write_text(json.dumps(result))
    for pair, value in result["spearman"].items():
        print(f"spearman {pair}: {value:.2f}" if value is not None else f"spearman {pair}: -")
    write_sheets(result, paths, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
