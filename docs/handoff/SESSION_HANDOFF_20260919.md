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
しかし同じ顔の full 版と draft 版で特徴量の距離が **平均 0.13・最大 0.38** 出た。閾値 0.5 に対して無視できず、
IOが律速でデコード時間は並列化で吸収できる以上、リスクを取る利益がないと判断した。

---

## 3. 次にやること（実データでの確認）

まだ実データでは動かしていない。以下の順で確認すること。

```bash
cp data/photoarchive.db data/photoarchive.db.bak      # 念のため(migrate も自動でバックアップする)
photoarchive migrate --db data/photoarchive.db
#   → Media 70,297 / Person 5 が残り、Face 0 件、全 face_count が NULL になる
#   → VACUUM で 773MB → 十数MB に縮む

photoarchive scan --source /mnt/nfs/nanoPi-NEO2/suzuki/Photo/2019 --workers 4 --log-level INFO
#   → 所要時間を測って全体の見積もりを更新する
sqlite3 data/photoarchive.db "select count(*) from Face where person_id is not null;"   # 0 であること

time photoarchive scan --source /mnt/nfs/nanoPi-NEO2/suzuki/Photo/2019   # 差分スキャンの確認

photoarchive-gui --db data/photoarchive.db    # 人物5名に各20〜50枚を割り当て
photoarchive match --dry-run                  # 距離の分布を見て閾値を決める
photoarchive match
```

### `EMBED_PADDING` のキャリブレーション（未実施・重要）

`face.EMBED_PADDING`（現在 0.25）は**まだ実データで最適化していない**。
同じ顔でもパディングを 0% → 25% に変えると特徴量の距離が **0.03〜0.57** 動く（実測）。
これは別人判定の閾値 0.6 と同じオーダーで、**この定数が自動紐づけの精度を支配する**。

手動割り当てを各人10枚ほど作ったうえで、padding を 0 / 0.15 / 0.25 / 0.4 / 0.6 と振り、
「同一人物内距離の95パーセンタイル」と「別人物間距離の5パーセンタイル」の差が
最大になる値を選ぶこと。決めた値と根拠データをこのファイルに追記する。

変更したら `face.EMBED_VERSION` を必ず上げる。上げないと古い特徴量と混ざって照合が壊れる。

`--threshold`（既定 0.5）と `--margin`（既定 0.05）も同じ分布から決め直すこと。

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
- **`selection.py` の `family_only` 判定**: 閾値 0.5 で切るため `assign_score` は最低でも 16.7 になり、
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
