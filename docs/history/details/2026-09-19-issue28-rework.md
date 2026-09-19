# 申し送り 2026-09-19

対象 Issue: #28（scanとanalyzeの処理を変更）。#9 と #22 も併せて解消。
ブランチ: `feature/#28_rework-scan-and-match`（`develop` から分岐）

設計方針は [Issue #28 のコメント](https://github.com/Beerski43/PhotoArchiveAI/issues/28#issuecomment-5740379872) に起票済み。

---

## 1. 何を変えたか

### 処理順の転換

```
旧: scan(登録) → analyze(顔検出 + 人物紐づけ) → GUI(人物登録)
新: scan(登録 + 顔検出) → GUI(人物登録 + 顔の割り当て) → match(残りを自動紐づけ)
```

旧 `analyze` は、**人物が1人も登録されていない段階で人物への紐づけを行っていた**。
順序が逆であることが #28 の指摘そのもの。`analyze` は廃止した。

### 修正した3つの欠陥

| # | 内容 | 旧コードの位置 |
|---|---|---|
| 1 | 閾値判定が無く、どんなに遠い顔も必ず誰かに紐づいていた (#22) | `analyzer.py:342-352` |
| 2 | 全人物の特徴量を1本に連結しながら人数で剰余を取って人物を引いていた。1人に2枚以上登録した時点で紐づく人物が壊れる | `analyzer.py:350` |
| 3 | 特徴量が FaceMesh のx座標を128個並べただけで、個人識別能力が無かった | `analyzer.py:195` |

3 は dlib ResNet の128次元特徴量に置き換えた。

### モジュール構成

- `analyzer.py` → `face.py`（顔検出と特徴量）に改名し、責務を分割
- `scoring.py`（笑顔・画質のスコア）を新設
- `matcher.py`（自動紐づけ）を新設
- `migration.py`（旧スキーマからの移行）を新設

### DBスキーマ (`user_version = 2`)

- `Media.face_count`: **NULL=未スキャン / 0=顔なし / N=検出数**
- `Media.analyzed_date` / `analyzer_version` を廃止し、`face_scanned_at` / `detector_version` に
- `FaceEmbedding` → `Face` に改名。`media_id` 必須。`media_id IS NULL` の「手本専用の行」という暗黙ルールを廃止
- `Face.assign_source`: `NULL` / `'manual'` / `'auto'` / `'rejected'`
- `Face.embedding`: JSON文字列 → float32×128 のBLOB（1件 1,560B → 512B）
- `Face.thumbnail`: 160px / q85（原寸クロップだと 62,000顔で 3.3GB になる）
- `Face.bbox_*`: 元解像度での顔の位置。拡大プレビューの再切り出しに使う
- `Person.age`、`AnalysisResult.face_count` / `duplicate_group` / `event_category` を廃止（死に列）
- 外部キーを有効化 (`PRAGMA foreign_keys = ON`) し、`ON DELETE CASCADE` を付けた
- `journal_mode = WAL`（GUIとCLIの同時利用のため）

`selection.py` は無改修。`get_media_with_analysis` が返す辞書のキー `face_count` が変わらないため。

---

## 2. 実測値（このマシンで計測、2026-09-19）

| 項目 | 実測 | 全体換算(70,297件) |
|---|---|---|
| NFS 読み出し | 24 MB/s | 画像 219GB → 2.5h / 動画 222GB → 2.6h |
| JPEGフルデコード | 0.094〜0.205 s/枚 | 1.7〜4.0h |
| MediaPipe 顔検出 | 0.020〜0.030 s/枚 | 0.4〜0.6h |
| dlib 特徴量 | 0.130 s/顔（約62,000顔） | 2.2h |
| SHA-256 | 0.012 s/枚 | 0.2h |

- 素朴な実装で 9〜10時間。1回読み + 差分判定 + `--workers 4` で **2.5〜3.5時間**（IOが律速）
- 2回目以降のスキャンは数十秒

### 採らなかった最適化: `Image.draft()`

DCTスケーリングデコードは2.9倍速く、**検出結果は full と完全一致**（実写真40枚 / 顔31個で差分ゼロ）。
しかし同じ顔の full 版と draft 版で特徴量の距離が **平均 0.13・最大 0.38** 出た。閾値 0.4 に対して無視できず、
IOが律速でデコード時間は並列化で吸収できる以上、リスクを取る利益がないと判断した。

---

## 3. 実データでの確認結果（2026-09-19 実施）

### migrate

```
移行前: 773MB / Media 70,297 / Person 5 / FaceEmbedding 17,716 / AnalysisResult 24,550
移行後:  29MB / Media 70,297 / Person 5 / Face 0 / AnalysisResult 0
         全 Media が face_count IS NULL（未スキャン）、file_hash は全件温存
所要時間: 17秒（VACUUM 込み）
バックアップ: data/photoarchive.db.bak-pre28
```

### scan

| 対象 | 件数 | 容量 | 時間(--workers 4) | 検出顔 | エラー |
|---|---|---|---|---|---|
| Photo/2005（古いデジカメ） | 345 | 108MB | 29.9秒 | 570 | 0 |
| な携帯/2023/ポケモン展（HEIC 56件含む） | 113 | 311MB | 19.7秒 | 7 | 0 |
| Photo/2026（最近のスマホ） | 492 | 7.9GB | 49.7秒 | 260 | 0 |

確認できたこと:

- `face_count` が NULL / 0 / N の3状態で正しく記録される
- **`Face.person_id` が全件 NULL。** scan は人物への紐づけを一切行わない（#22 の修正）
- 2回目の scan は 29.9秒 → **0.87秒**（345件すべてスキップ、顔の重複増殖なし、#9 の修正）
- **HEIC 56件がすべて読めた。** 読み込み失敗は0件。旧実装のログにあった
  `cannot identify image file ... .HEIC` は解消している
- サムネイルの実測平均は 3.3KB（見積り 4.8KB より小さい）。顔1件あたりDB増分は約5KB
- GUI が実データで起動し、Person 5件と顔577件を200件ずつページングして表示できた
- 手本が0件の状態で `match` を実行すると、何も書かずに案内を出して終了する

所要時間の目安: 2026年フォルダは 7.9GB を 49.7秒（実効160MB/s）。NFSのキャッシュが
効いた可能性があるため、全件スキャンの見積もりはこの数字をそのまま当てにしないこと。
2005年フォルダの 0.087秒/件 から換算すると全70,297件で約1.7時間。

### まだ確認していないこと

- **GUIでの手動割り当てと `match` の実効精度。** 手本が0件のため未検証。
  人物ごとに20〜50枚を割り当てたうえで `match --dry-run` から確認すること
- 消えたファイルの行削除（prune）と、その安全弁の実データでの動作
- 全件スキャン

以下の順で続けること。

```bash
photoarchive-gui --db data/photoarchive.db    # 人物5名に各20〜50枚を割り当て
photoarchive match --dry-run                  # 距離の分布を確認
photoarchive match
sqlite3 data/photoarchive.db "select assign_source, count(*) from Face group by assign_source;"

# 問題なければ全件スキャン(tmux 推奨。中断しても続きから再開できる)
photoarchive scan --workers 4 --log-level INFO
```

### `EMBED_PADDING` のキャリブレーション（実施済み・2026-09-19）

実データ 950件の顔で、padding を 0 / 0.1 / 0.25 / 0.4 / 0.6 と振って比較した。
評価には「同一写真に写る2つの顔は別人」という前提を使った（矩形が重なるペアは
除外済み。重複検出は0件だったため前提は妥当）。

| padding | 予測器 | 別人ペアの中央値 | 5%点 | 閾値0.5での誤一致率 |
|---|---|---|---|---|
| 0.00 | sp5 | 0.614 | 0.457 | 11.9% |
| 0.10 | sp5 | 0.611 | 0.463 | 11.3% |
| **0.25** | **sp5** | **0.620** | **0.475** | **9.6%** |
| 0.40 | sp5 | 0.622 | 0.436 | 15.9% |
| 0.60 | sp5 | 0.506 | 0.377 | **47.3%** |
| 0.25 | sp68 | 0.632 | 0.454 | 10.2% |
| 0.60 | sp68 | 0.476 | 0.351 | **59.2%** |

- **0.0〜0.25 はほぼ同等。0.4 以上で急激に悪化する。** 現在の 0.25 を維持する
- 5点予測器と68点予測器に有意な差は無かった。軽い sp5 のままでよい
- padding を安易に大きくしてはならない。0.6 では半数近くが別人同士で誤一致する

変更したら `face.EMBED_VERSION` を必ず上げる。上げないと古い特徴量と混ざって照合が壊れる。

### `--threshold` を 0.5 から 0.4 に変更（2026-09-19）

同じ前提で、閾値ごとの誤一致率を実測した。

| 閾値 | 2005年（顔幅の中央値128px） | 2026年（同233px） |
|---|---|---|
| 0.40 | **1.0%** | **0.5%** |
| 0.45 | 2.8% | 5.7% |
| 0.50 | 9.0% | 22.7% |
| 0.55 | 21.6% | 43.3% |
| 0.60（dlib標準） | 40.4% | 63.4% |

当初の既定 0.5 は緩すぎたため **0.4 に下げた**。dlib 標準の 0.6 はこのデータでは
論外で、半数以上が誤一致する。

注意点:

- **新しい写真の方が誤一致率が高い。** 顔は大きく写っているのに悪化している。
  家族（特に兄弟姉妹や同年代の子ども）が同じ写真に写る割合が高く、
  顔が本来似ているためと考えられる
- ここで測ったのは誤一致率だけで、**本来紐づくべき顔を取りこぼす率は測っていない。**
  0.4 で取りこぼしが多すぎるようなら、GUIでの手作業を増やすか 0.45 へ緩める
- 取りこぼした顔は未割当のまま残るだけなので、誤って紐づくより害が小さい

---

## 4. 決めた前提（異論があれば指摘してほしい）

- **既存の `AnalysisResult` 24,550行は破棄した。** 方針は「Media と Person を温存」であり
  `AnalysisResult` は明示されていなかったが、smile/quality は再スキャンで計算し直され、
  family_score は壊れたマッチャの産物のため。
- **手動登録済みの顔2件も破棄した。** 特徴量の生成方式が変わり互換性が無いため。
  実DB上、`media_id IS NULL AND person_id IS NOT NULL` は2件しか無かった。
- **ハッシュが変わったメディアは、手動割り当て済みの顔も含めて顔を作り直す。**
  中身が別物になった以上、座標も特徴量も無効なため。削除時は WARNING ログを出す。
- **読み込めない画像（HEICなど）は `face_count = 0` として記録する。**
  NULL のままにすると毎回スキャンし直すことになるため。理由はログに残る。

---

## 5. 未解決・別Issue

- **#26（scan以降からHEICを外す）**: `face.py` の import 時に
  `pillow_heif.register_heif_opener()` を呼ぶようにしたので、HEIC も読めるようになった。
  実DBには HEIC が 1,761件ある。`IMAGE_EXTENSIONS` から外すかどうかは #26 で判断する。
- **#25（`Error: none` を上書きしない）**: `cli.py:_emit_progress` の2行目の挙動。
  `scan` と `match` でも同じ表示が出るが、今回は手を入れていない。
- **#23（GUIに顔登録削除機能）**: 「割り当て済みを確認」に解除・年齢変更・自動割当の確定を
  実装したため、要求の大部分は満たしている。#23 を閉じてよいか確認が必要。
- **`selection.py` の `family_only` 判定**: 閾値 0.4 で切るため `assign_score` は最低でも 33.3 になり、
  `family_score > 0.0` が実質「割り当てが1つでもあれば通る」になった。
  selection の仕様変更にあたるので今回は据え置いた。
- **`scanner.get_media_type` は `"image"` を返すが、一部のテストは `"photo"` 前提**で書かれている。
  `selection.py` が `!= "video"` で判定しているため実害は出ていない。
- **`Person.name` に UNIQUE 制約が無い**ので同名人物を作れる。GUIの割り当て画面で
  紛らわしくなるようなら別Issue化する。

---

## 6. テスト

`pytest -q` で **40件 passed**（変更前は6件）。

| ファイル | 内容 |
|---|---|
| `tests/test_migration.py` | 旧スキーマからの移行、冪等性、BLOBのラウンドトリップ |
| `tests/test_scanner_incremental.py` | 差分スキャン、顔行の重複防止、消えた行の削除、安全弁 |
| `tests/test_matcher.py` | **剰余バグの回帰テスト**、閾値・マージン、手本の選び方、冪等性 |
| `tests/test_gui_assignment.py` | ページング、割り当て・解除・除外、0歳と未設定の区別 |
| `tests/test_db.py` | CRUD、CASCADE、family_score を潰さないUPSERT |
| `tests/test_system.py` | 通しテスト（scan → GUI割り当て → match → select）と差分スキャン |
| `tests/test_face_real.py` | 実物の dlib モデルでの確認（`-m models`） |

`tests/conftest.py` のフェイクを直した。旧フェイクは `location_data.bounding_box` しか
持たず、実装が先に見る `relative_bounding_box` を返していなかったため、
**検出経路が一度も検証されていなかった**。

---

## 7. Issue #10（face_recognition エラー）の真因

`face_recognition_models/__init__.py:7` の `from pkg_resources import resource_filename` が、
setuptools 81 以降（この環境は 83）で `ModuleNotFoundError` になるだけだった。

`importlib.util.find_spec("face_recognition_models")` は `__init__.py` を実行しないので、
この問題を踏まずにモデルのパスだけ取り出せる。`face.py` の `_resolve_model_dir()` がこれを行う。
**`face_recognition` パッケージ自体は不要**なので、依存から外した。
