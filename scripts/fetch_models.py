#!/usr/bin/env python3
"""学習済みモデルを `models/` へ取得する。

**アプリからは呼ばない。** `scan` が黙って外部へ通信するのは挙動が読みにくく、
どこから来たファイルで動いているのかが分からなくなる。dlib の `.dat` と同じで、
**設置は明示的な操作のまま**にしてある。

いま取得するのは ArcFace の認識モデル1本だけ。**`insightface` パッケージは
入れない**（モデル動物園と GPU 版 onnxruntime を引き込む）。`buffalo_l` の
配布物から必要な1本を取り出して置く。

**sha256 を必ず検証する。** 特徴量モデルが別物にすり替わると、照合の結果が
静かに変わる（版の文字列は同じままなので、`reembed` も作り直さない）。

使い方::

    python scripts/fetch_models.py           # 無ければ取得、あれば検証だけ
    python scripts/fetch_models.py --force   # 取り直す
    python scripts/fetch_models.py --check    # 取得せず、いまあるものを検証
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

#: 読み出しの塊。大きなファイルを一度にメモリへ載せない。
CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ModelFile:
    """取得するモデル1つ。

    ``archive_member`` があれば ZIP の中から取り出す。無ければ本体を直接落とす。
    """

    name: str
    sha256: str
    url: str
    archive_member: Optional[str] = None
    note: str = ""


ARCFACE = ModelFile(
    name="w600k_r50.onnx",
    sha256="4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43",
    url="https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip",
    archive_member="w600k_r50.onnx",
    note="ArcFace の顔特徴量(512次元)。buffalo_l から認識用の1本だけを取り出す。",
)

MODELS: Sequence[ModelFile] = (ARCFACE,)


def sha256_of(path: Path) -> str:
    """ファイルの sha256。**一度にメモリへ載せない**（174MB ある）。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(path: Path, expected: str) -> bool:
    """置かれているファイルが期待した中身かどうか。"""
    return path.is_file() and sha256_of(path) == expected


def _download(url: str, destination: Path, log=print) -> None:
    log(f"  取得中: {url}")
    with urllib.request.urlopen(url) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle, CHUNK_BYTES)


def _extract(archive: Path, member: str, destination: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        with bundle.open(member) as source, destination.open("wb") as handle:
            shutil.copyfileobj(source, handle, CHUNK_BYTES)


def fetch(
    model: ModelFile,
    models_dir: Path,
    force: bool = False,
    check_only: bool = False,
    log=print,
) -> bool:
    """1つのモデルを用意する。**用意できたら True。**

    すでにあって sha256 が一致すれば何もしない。``check_only`` なら取得しない。
    """
    target = models_dir / model.name
    if verify(target, model.sha256) and not force:
        log(f"OK   {model.name}（sha256 一致）")
        return True
    if target.is_file() and not force:
        log(f"NG   {model.name} が期待した中身ではありません（--force で取り直す）")
        return False
    if check_only:
        log(f"NG   {model.name} がありません（{models_dir}）")
        return False

    models_dir.mkdir(parents=True, exist_ok=True)
    log(f"     {model.note}")
    with tempfile.TemporaryDirectory() as workspace:
        staging = Path(workspace)
        if model.archive_member:
            archive = staging / "bundle.zip"
            _download(model.url, archive, log)
            log(f"  取り出し中: {model.archive_member}")
            _extract(archive, model.archive_member, staging / model.name)
        else:
            _download(model.url, staging / model.name, log)
        fetched = staging / model.name
        actual = sha256_of(fetched)
        if actual != model.sha256:
            # **期待と違うものを置かない。** 置いてしまうと、別物のモデルで
            # 照合が走る（版の文字列は変わらないので reembed も作り直さない）。
            log(f"NG   sha256 が一致しません\n     期待 {model.sha256}\n     実際 {actual}")
            return False
        shutil.move(str(fetched), str(target))
    log(f"OK   {model.name} を置きました: {target}")
    return True


def main(argv: Optional[Sequence[str]] = None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--models-dir", default=str(root / "models"), help="置き場所（既定: models/）"
    )
    parser.add_argument("--force", action="store_true", help="すでにあっても取り直す。")
    parser.add_argument(
        "--check", action="store_true", help="取得せず、いまあるものを検証するだけ。"
    )
    args = parser.parse_args(argv)

    models_dir = Path(args.models_dir)
    ok = True
    for model in MODELS:
        ok = fetch(model, models_dir, force=args.force, check_only=args.check) and ok
    if not ok:
        print("\n用意できていないモデルがあります。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
