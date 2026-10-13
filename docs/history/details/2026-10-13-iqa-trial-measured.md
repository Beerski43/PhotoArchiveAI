# 写真の良し悪しモデル（IQA）の試験 — 途中結果（2026-10-13・#105）

**途中で止まった試験の記録。** NIMA・MUSIQ・TOPIQ（NR）の3つは 200 枚を測り終えた。TOPIQ（IAA）を読み込むところでプロセスがエラーを残さず止まり（2026-10-13 21:43 ごろ。原因は未調査）、CLIP-IQA は測っていない。
**まだ上位と下位の写真を見比べておらず、採るモデルも決まっていない。**

## 環境

- **この PC（i7-3770K・AVX2 なし・GPU は使えない）で PyTorch 2.14（CPU 版）は動く。** `torch.backends.cpu.get_cpu_capability()` は `DEFAULT`。pyiqa は 0.1.16
- 作業用の仮想環境はリポジトリの外に作った（2.4GB）。作り直し方:
  ```bash
  python3 -m venv <外>/venv
  <外>/venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
  <外>/venv/bin/pip install pyiqa
  ```
- 学習済みの重みは初回に Hugging Face（`chaofengc/IQA-PyTorch-Weights`）から `~/.cache/torch/hub/pyiqa/` に落ちる。NIMA（208MB）・MUSIQ・TOPIQ（NR）の重みはここに残っている
- 「NNPACK を初期化できない（Unsupported hardware）」という警告が大量に出るが、測定は進む

## 測り方

- 家族の写った写真（手本か自動割り当ての顔がある画像）から 200 枚を無作為に選んだ（`random.Random(0)`）。実データの DB は読み取り専用
- 元写真は NFS から読み、**長辺 1024px に縮小デコード**してからモデルに渡した（`draft` → `exif_transpose` → `thumbnail`）。読み込みは 1 枚 0.17 秒
- モデルごとに、200 枚の点・1 枚あたりの時間・同じ写真を2回測ったときの揺れを出した
- スクリプト・結果（写真の ID ごとの点）・ログは **`data/iqa-trial-2026-10-13/`**（git 管理外・この PC の中だけ）。`measure_iqa.py` は上の仮想環境の Python で `-I` を付けて流す（`--db data/photoarchive.db --roots <source_roots> --out <外> --n 200`）

## 結果（200 枚）

| モデル | 1 枚あたり（CPU 8 スレッド） | 1.6 万枚の見込み | 点の範囲（最小 / 中央 / 最大） | 揺れ |
|---|---|---|---|---|
| NIMA（美しさ・AVA で学習） | 0.50 秒 | 約 2.2 時間 | 3.56 / 4.70 / 5.86 | 0 |
| MUSIQ（技術的な画質・KonIQ） | 1.53 秒 | 約 6.8 時間 | 18.8 / 69.3 / 78.3 | 0 |
| TOPIQ NR（技術的な画質・KonIQ） | 2.88 秒 | 約 12.8 時間（**一晩に収まらない**） | 0.14 / 0.55 / 0.79 | 0 |
| TOPIQ IAA（美しさ） | — | — | 読み込みで止まった | — |
| CLIP-IQA | — | — | 未測定 | — |

見込みはモデルの時間だけ。NFS の読み込み（1.6 万枚 × 0.17 秒 ≒ 45 分）が別にかかる。

**モデルどうしの一致**（順位の相関・Spearman）:

| | NIMA | MUSIQ | TOPIQ NR |
|---|---|---|---|
| NIMA | — | 0.64 | 0.66 |
| MUSIQ | | — | **0.90** |

上位 20 枚の重なり: MUSIQ と TOPIQ NR は 13 枚、NIMA と MUSIQ は 6 枚、NIMA と TOPIQ NR は 7 枚。
**MUSIQ と TOPIQ NR（どちらも技術的な画質・同じデータで学習）はほぼ同じ順位**を付け、**NIMA（美しさ）は別のものを見ている。** 一晩の上限（利用者の決定）では TOPIQ NR は外れ、技術的な画質は MUSIQ で足りる見込み。

## 次にやること（#105）

1. モデルごとに上位と下位の写真を並べた画像を作り（リポジトリの外。家族の写真なので置かない）、**利用者に見せて採るモデルを決めてもらう**。候補は NIMA（美しさ・速い）と MUSIQ（画質）。両方を採って重みを別々に持たせる手もある
2. 必要なら TOPIQ IAA と CLIP-IQA を測り直す（止まった原因をまず確かめる。メモリ不足を疑う）
3. 決まったら #106 で組み込む（スキーマ版 11・`photoarchive assess`・GUI の重み）
