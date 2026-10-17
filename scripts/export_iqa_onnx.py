"""写真の良し悪しモデル（MUSIQ・NIMA）を pyiqa から ONNX に書き出す（#106）。

**アプリには PyTorch を入れない**（利用者の決定）。書き出しは一度だけ、PyTorch と pyiqa のある
別の仮想環境で流す::

    python3 -m venv <外>/venv
    <外>/venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
    <外>/venv/bin/pip install pyiqa onnx onnxruntime
    <外>/venv/bin/python -I scripts/export_iqa_onnx.py --out models

学習済みの重みは、初回に pyiqa が Hugging Face から落とす（`~/.cache/torch/hub/pyiqa/`）。

書き出したあと、**アプリの前処理（`quality.py`・numpy）+ ONNX が pyiqa と同じ点を出すか**を確かめる。

- 乱数の画像で、MUSIQ と NIMA の前処理を PyTorch の処理と直接比べる
- ``--db`` を渡すと、家族の写った写真 ``--n`` 枚で点を比べる（DB は読み取り専用で開く）

**`quality.py` の前処理を変えたら、これを流し直す。**
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from photoarchive_ai import quality  # noqa: E402


def _wrappers(torch, musiq, nima):
    class Musiq(torch.nn.Module):
        """パッチに切ったあとの MUSIQ（pyiqa の forward の後半）。出力は点。"""

        def __init__(self, net):
            super().__init__()
            self.net = net

        def forward(self, x):
            net = self.net
            b, seq_len, _ = x.shape
            spatial = x[:, :, -3]
            scale = x[:, :, -2]
            mask = x[:, :, -1].bool()
            x = x[:, :, :-3].reshape(-1, 3, net.patch_size, net.patch_size)
            x = net.block1(net.root_pool(net.gn_root(net.conv_root(x))))
            x = x.permute(0, 2, 3, 1).reshape(b, seq_len, -1)
            x = net.transformer_encoder(net.embedding(x), spatial, scale, mask)
            return net.head(x[:, 0])

    class Nima(torch.nn.Module):
        """縮めて切り出したあとの NIMA。入力は 0〜1、出力は点（1〜10）。"""

        def __init__(self, net):
            super().__init__()
            self.net = net

        def forward(self, x):
            net = self.net
            x = (x - net.default_mean) / net.default_std
            dist = net.classifier(net.global_pool(net.base_model(x)[-1]))
            return (dist * torch.arange(1, 11, dtype=dist.dtype)).sum(dim=1, keepdim=True)

    return Musiq(musiq).eval(), Nima(nima).eval()


def export(out: Path):
    import pyiqa
    import torch

    musiq = pyiqa.create_metric("musiq", device="cpu").net.eval()
    nima = pyiqa.create_metric("nima", device="cpu").net.eval()
    musiq_wrap, nima_wrap = _wrappers(torch, musiq, nima)
    tokens = torch.from_numpy(quality.musiq_input(np.random.default_rng(0).random((768, 1024, 3))))
    out.mkdir(parents=True, exist_ok=True)
    with torch.no_grad():
        torch.onnx.export(
            musiq_wrap, tokens, str(out / quality.MUSIQ_FILE), input_names=["tokens"],
            output_names=["score"], dynamic_axes={"tokens": {1: "count"}}, opset_version=17, dynamo=False,
        )
        torch.onnx.export(
            nima_wrap, torch.rand(1, 3, quality.NIMA_SIZE, quality.NIMA_SIZE), str(out / quality.NIMA_FILE),
            input_names=["pixels"], output_names=["score"], opset_version=17, dynamo=False,
        )
    print(f"書き出した: {out / quality.MUSIQ_FILE}・{out / quality.NIMA_FILE}")
    return musiq, nima


def compare_preprocessing(musiq, nima) -> bool:
    """乱数の画像で、numpy の前処理と PyTorch の前処理を直接比べる。"""
    import torch
    from pyiqa.data.multiscale_trans_util import get_multiscale_patches

    ok = True
    rng = np.random.default_rng(1)
    for h, w in ((768, 1024), (1024, 683), (576, 1024), (100, 37)):
        rgb = rng.random((h, w, 3)).astype(np.float32)
        tensor = torch.from_numpy(rgb.transpose(2, 0, 1))[None]
        expected = get_multiscale_patches((tensor - 0.5) * 2, **musiq.data_preprocess_opts).numpy()
        actual = quality.musiq_input(rgb)
        musiq_gap = float(np.abs(expected - actual).max()) if expected.shape == actual.shape else float("inf")
        nima_expected = nima.preprocess(tensor) * nima.default_std + nima.default_mean
        nima_gap = float(np.abs(nima_expected.numpy() - quality.nima_input(rgb)).max())
        print(f"前処理 {h}x{w}: MUSIQ 最大差 {musiq_gap:.2e}（形 {actual.shape}）・NIMA 最大差 {nima_gap:.2e}")
        ok = ok and musiq_gap < 1e-3 and nima_gap < 2e-2
    return ok


def compare_photos(db_path: str, count: int) -> None:
    """家族の写った写真で、pyiqa と アプリ（numpy + ONNX）の点を比べる。"""
    import pyiqa
    import torch
    import torchvision.transforms.functional as TF

    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    ids = [
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT F.media_id FROM Face F JOIN Media M ON M.id = F.media_id"
            " WHERE F.assign_source IN ('manual', 'auto') AND M.type = 'image' ORDER BY F.media_id"
        )
    ]
    random.Random(0).shuffle(ids)
    metrics = {"musiq": pyiqa.create_metric("musiq", device="cpu"), "nima": pyiqa.create_metric("nima", device="cpu")}
    models = quality.Models.load()
    gaps = {"musiq": [], "nima": []}
    pairs = {"musiq": [], "nima": []}
    started = time.time()
    for media_id in ids[:count]:
        path = connection.execute("SELECT path FROM Media WHERE id = ?", (media_id,)).fetchone()[0]
        try:
            image, _ = quality.load_photo(Path(path))
        except OSError as error:
            print(f"skip m{media_id} {error}", file=sys.stderr)
            continue
        rgb = quality.to_array(image)
        tensor = TF.to_tensor(image).unsqueeze(0)
        with torch.no_grad():
            reference = {name: float(metric(tensor).item()) for name, metric in metrics.items()}
        ours = {"musiq": models.technical(rgb), "nima": models.aesthetic(rgb)}
        for name in gaps:
            gaps[name].append(abs(reference[name] - ours[name]))
            pairs[name].append((reference[name], ours[name]))
    print(f"写真 {len(gaps['musiq'])} 枚（{time.time() - started:.0f} 秒）")
    for name in gaps:
        values = np.array(gaps[name])
        a, b = np.array(pairs[name]).T
        rank = np.corrcoef(np.argsort(np.argsort(a)), np.argsort(np.argsort(b)))[0, 1]
        print(f"{name}: 差の最大 {values.max():.4f}・中央 {np.median(values):.4f}・順位の相関 {rank:.4f}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="models", help="書き出し先（既定: models/）")
    parser.add_argument("--db", help="写真で比べるときの DB（読み取り専用で開く）")
    parser.add_argument("--n", type=int, default=50, help="写真で比べる枚数")
    args = parser.parse_args()
    musiq, nima = export(Path(args.out))
    if not compare_preprocessing(musiq, nima):
        print("前処理が pyiqa と一致しません。quality.py を確かめてください。", file=sys.stderr)
        return 1
    if args.db:
        import os

        os.environ.setdefault("PHOTOARCHIVE_ONNX_MODEL_DIR", str(Path(args.out).resolve()))
        compare_photos(args.db, args.n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
