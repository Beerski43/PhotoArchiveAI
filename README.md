# PhotoArchiveAI

PhotoArchiveAI は、長期間保存された家族の写真・動画アーカイブから、AI解析とルールに基づいて良い素材を抽出し、別フォルダへコピーするローカルアプリケーションです。

## 特長

- メディアを再帰的にスキャンして SQLite に登録し、同時に顔を検出
- 写真の EXIF / 撮影日時を取得
- 差分スキャン。顔検出済みのメディアは再検出しない
- PySide6 GUI で人物を登録し、検出済みの顔を人物へ割り当て
- 割り当て済みの顔を手本に、残りの顔を自動で紐づけ
- CLI でのピックアップ/コピー実行
- 元の写真・動画ファイルは変更しない

## 全体のフロー

1. `config/app_settings.sample.json` をコピーして `config/app_settings.json` を作成し、`database_path` / `source_root` / `output_root` / `rule_path` を設定します。
2. `photoarchive init-db` で SQLite データベースを初期化します。（既存のデータベースがある場合は `photoarchive migrate` を実行します。バックアップは自動で作られます）
3. 必要に応じて `photoarchive convert-heic` でHEIC/HEIFをJPEGへ変換します。
4. `photoarchive scan` で対象ディレクトリをスキャンします。パスの登録と顔の検出をここでまとめて行います。
5. `photoarchive-gui` で人物を登録し、検出された顔を人物へ割り当てます。
6. `photoarchive match` で、割り当てきれなかった顔を自動で紐づけます。
7. `config/rule.json` を編集し、`photoarchive select` でコピー先へ出力します。

設計・仕様の詳細は [仕様書](docs/spec/Specification.md)、開発の道筋は [実装プラン](docs/plan/ROADMAP.md)、これまでの経緯は [作業履歴](docs/history/WORKLOG.md) にあります。文書の索引は [docs/README.md](docs/README.md)。

顔の紐づけは **必ず人物を登録したあと** に行います。人物を登録する前に自動で紐づけると、誰とも分からない顔が誤った人物に結びついてしまうためです（Issue #28）。

## インストール

### Ubuntu のシステム依存

Python、仮想環境、`dlib` のビルド、OpenCVの画像/動画処理、PySide6のGUI起動に必要です。

```bash
sudo apt update
sudo apt install -y \
  python3 python3-venv python3-pip python3-dev \
  build-essential cmake libboost-python-dev libopenblas-dev liblapack-dev \
  libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 \
  libx11-dev libxrandr-dev libxkbcommon-x11-0 libxcb-cursor0 \
  libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxcb-xinerama0 \
  libjpeg-dev libpng-dev libtiff-dev libavcodec-dev libavformat-dev libswscale-dev
```

GUIを通常のデスクトップで起動するには、X11またはWaylandの表示セッションも必要です。リモート接続やサーバー環境では `DISPLAY` / Wayland の設定が必要になります。

### Python の依存ライブラリ

`requirements.txt` には、アプリケーションが直接使用する次のライブラリを記載しています。

- `PySide6`: GUI
- `Pillow`: 画像読み込み、変換、EXIF読み込み
- `pillow-heif`: PillowでHEIC/HEIFを読み込むためのデコーダー
- `mediapipe==0.10.21`: 顔検出、顔ランドマーク。旧 `solutions` APIを使用するため、このバージョンを固定
- `opencv-python`: 動画読み込み、画像変換、画質評価
- `numpy`: 画像配列と数値処理
- `PyYAML`: YAML形式のルール読み込み
- `pytest`: テスト実行

加えて、人物の識別に使う次のライブラリも `requirements.txt` と `pyproject.toml` の両方に記載しています。

- `dlib`: 128次元の顔特徴量の生成
- `face_recognition_models`: dlib の学習済みモデルデータ。GitHubからインストール（モデルファイルの置き場所としてのみ使い、import はしない）

通常は以下で全Python依存をインストールできます。

```bash
cd /home/suu/github/PhotoArchiveAI
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
python -m pip install -e .
```

顔の特徴量には `dlib` を使うため、上記の `cmake`、C/C++ビルドツール、Boost、OpenBLAS、LAPACKが必要です。学習済みモデル (`shape_predictor_5_face_landmarks.dat` と `dlib_face_recognition_resnet_model_v1.dat`) は `face_recognition_models` パッケージに含まれており、GitHubリポジトリから取得されます（PyPIには存在しません）。

```bash
python -m pip install git+https://github.com/ageitgey/face_recognition_models
```

このパッケージは **モデルファイルの置き場所としてのみ** 使用し、Pythonモジュールとしては読み込みません。`face_recognition_models/__init__.py` が `pkg_resources` に依存しており、setuptools 81 以降では `ModuleNotFoundError` になるためです。モデルを別の場所に置く場合は、環境変数 `PHOTOARCHIVE_DLIB_MODEL_DIR` か `config/app_settings.json` の `dlib_model_dir` でディレクトリを指定してください。

SQLite、`argparse`、`json`、`logging`、`pathlib`、`shutil`、`hashlib` などはPython標準ライブラリのため、個別インストールは不要です。

## 使い方

### 1. アプリ設定ファイル作成

```bash
cp config/app_settings.sample.json config/app_settings.json
```

必要に応じて `database_path` / `source_root` / `output_root` / `rule_path` を編集します。

設定ファイルは次の順に探し、最初に見つかったものを使います。**リポジトリ以外のディレクトリから実行しても設定が効きます。**

1. 環境変数 `PHOTOARCHIVE_CONFIG` が指すファイル
2. カレントディレクトリの `config/app_settings.json`
3. リポジトリ直下の `config/app_settings.json`（`pip install -e .` のときだけ）

### 2. データベース初期化

```bash
source .venv/bin/activate
photoarchive init-db
```

### 3. HEIC/HEIFのJPEG変換

HEIC/HEIF画像を含む場合は、スキャン前にJPEGへ変換できます。

```bash
photoarchive convert-heic
```

`source_root` 設定のディレクトリ以下を再帰的に処理します。別のディレクトリを指定する場合は、次のように `--source` を使用します。

```bash
photoarchive convert-heic --source /path/to/photo
```

変換先は元ファイルと同じディレクトリで、拡張子だけを `.jpg` にした同名ファイルです。変換先に同名JPEGがあり、内容が同じ写真の場合は変換をスキップします。異なる写真の場合は `photo_1.jpg`、`photo_2.jpg` のように空いている連番を付けます。元のHEIC/HEIFファイルは変更しません。

変換先へ書き込めない場合は、続行するか確認を求めます。`y` または `yes` を入力すると次のファイルへ進み、それ以外を入力すると処理を停止します。

### 4. メディアのスキャンと顔検出

```bash
photoarchive scan
```

`scan` は次をまとめて行います。

- メディアファイルの登録（パス、ハッシュ、サイズ、EXIF撮影日時）
- 顔の検出と、顔画像・特徴量・スコアの保存
- 実体が無くなったメディアの行の削除

**この時点では人物への紐づけは一切行いません。** 顔は「未割当」として貯まります。

2回目以降はファイルサイズと更新時刻が一致するものを読み飛ばすため、短時間で終わります。顔検出済みのメディアも再検出しません。

```bash
# 並列数を指定（既定は CPU コア数 - 1、最大 4）
photoarchive scan --workers 4

# 実体が無いメディアの行を消さない
photoarchive scan --no-prune

# 検出器を変えたなどの理由で、全件の顔を検出し直す
photoarchive scan --force-rescan
```

顔特徴量のモデル (dlib) を読み込めない場合、`scan` は処理を始める前に中断します。そのまま進むと、顔は検出されるのに特徴量が保存されず、`match` が一切効かない状態のまま「スキャン済み」として記録されてしまうためです。承知のうえで進める場合は `--allow-missing-embeddings` を付けます。

このオプションで取り込んだメディアは検出器の版を記録しないので、**モデルを設置したあとに通常の `photoarchive scan` を実行すれば自動でやり直されます。**`--force-rescan` は不要です（`--force-rescan` は手動で割り当てた顔も作り直してしまいます）。

実体が見つからないメディアが登録数の2割を超えた場合は、ソースの指定間違いやNFSの未マウントを疑って処理を中断します。意図した削除であれば `--force-prune` を付けて再実行してください。

ログは `data/logs/scan_*.log` に出力されます。ログレベルは `--log-level` で `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` を指定できます（既定は `WARNING`）。

途中で中断しても、顔検出が終わったメディアは記録済みなので、再実行すれば続きから再開します。

### 5. GUI で人物登録と顔の割り当て

```bash
photoarchive-gui
```

`--db` を省くと `config/app_settings.json` の `database_path` を使います。別のデータベースを開く場合は `photoarchive-gui --db data/photoarchive.db` のように指定します。

人物を登録し、`scan` が検出した顔のサムネイル一覧から、その人物の顔を選んで割り当てます。ここで割り当てた顔が次の `match` の手本になります。

操作手順は [GUI利用手順](docs/operation/GUI_USAGE.md) を参照してください。

### 6. 残りの顔の自動紐づけ

```bash
photoarchive match
```

手動で割り当てた顔を手本に、未割当の顔を自動で紐づけます。十分な枚数を手作業で割り当ててあるなら、実行しなくても構いません。

- 手本に使うのは **手動で割り当てた顔だけ** です。自動割り当ての結果を手本に混ぜると、誤りが次の判定の根拠になって増幅するためです。
- 似ている度合いが基準に届かない顔は **未割当のまま残します**。
- 実行のたびに自動割り当てを付け直すので、何度実行しても同じ結果になります。

```bash
# どれくらい割り当てられそうかを、書き込まずに確認する
photoarchive match --dry-run

# 判定を厳しく／緩くする（既定は 0.4、小さいほど厳しい）
photoarchive match --threshold 0.45

# 2位の人物との距離差の下限（既定は 0.05）
photoarchive match --margin 0.1
```

`--dry-run` は顔の距離の分布を表示するので、`--threshold` を決める目安になります。**`match` を実行したあとでも同じ結果が出ます**（自動割り当てを取り消したあとの状態を再現して数えるため）。ログは `data/logs/match_*.log` に出力されます。

### 7. 抽出ルールに基づく選択とコピー

`config/rule.sample.json` をコピーして `config/rule.json` とし、必要に応じて編集します。

```bash
photoarchive select
```

`rule.json` の例:

```json
{
  "date": {
    "start": "2000-01-01",
    "end": "2025-12-31"
  },
  "family_only": true,
  "count_per_year": 30,
  "include_video": true,
  "remove_duplicate": true
}
```

実行:

```bash
photoarchive select
```

コピー処理でも進捗バーが表示され、出力先へどこまでコピー済みか確認できます。

直接引数を使う場合は `--db`, `--source`, `--output`, `--rule` で設定を上書きできます。

例:

```bash
photoarchive select --output /path/to/output --source /path/to/media --rule config/rule.json
```

## テスト

変更を加えたら、回帰テストを実行します。

```bash
source .venv/bin/activate
./scripts/run_regression.sh
```

全196件がおよそ3秒で終わります。最終行に次の形のまとめが出ます。

```
回帰テスト: 194 passed / 0 failed (3.15s) 実行日: 2026-09-19
```

個別に動かす場合:

```bash
pytest -q                          # 全部
pytest -q tests/test_matcher.py    # ファイル単位
pytest -q -m system                # 通しテストだけ
pytest -q -m models                # 実物の dlib モデルを使う確認だけ(環境依存)
```

`tests/conftest.py` が `mediapipe` と `dlib` をフェイクに差し替えるので、依存関係やモデルファイルが揃わない環境でもテストが安定して動きます。テストはネットワーク・NFS・実データベースに一切触りません。

詳しい手順は [テストの実行手順](docs/testing/TESTING.md)、何がテストで守られているかは [テスト項目一覧](docs/testing/TEST_CASES.md) を参照してください。

## ディレクトリ構成

```
PhotoArchiveAI/
  README.md
  CLAUDE.md                    AIエージェント向けの作業ルール
  pyproject.toml
  requirements.txt
  scripts/
    run_regression.sh          回帰テスト
    summarize_pytest.py        回帰テストの集計行を作る
    archive_worklog.py         作業履歴の切り出し
  config/
    app_settings.sample.json   アプリ設定のサンプル
    rule.sample.json           抽出ルールのサンプル
  docs/
    README.md                  文書の索引
    spec/Specification.md      要件・仕様・DB設計・CLIリファレンス
    plan/ROADMAP.md            実装プラン
    history/WORKLOG.md         作業履歴
    testing/                   テストの手順とテスト項目一覧
    operation/GUI_USAGE.md     GUIの操作手順
  src/
    photoarchive_ai/
      __init__.py
      __main__.py
      config.py                設定ファイルの探索と読み込み
      db.py                    スキーマと永続化
      migration.py             旧スキーマからの移行
      scanner.py               走査・差分判定・顔検出の呼び出し
      face.py                  顔検出(MediaPipe)と顔特徴量(dlib)
      scoring.py               笑顔・画質のスコア
      matcher.py               自動紐づけ
      converter.py             HEIC/HEIF → JPEG 変換
      selection.py             ルールに基づく抽出とコピー
      cli.py                   サブコマンド定義
      gui.py                   人物登録と顔の割り当て画面
  tests/                       テスト(リポジトリ内には書き込まない)
  data/                        DB・ログ・バックアップ(git管理外)
  mediaFiles/                  実データへのsymlink置き場(git管理外)
```

## 主要ライブラリ

- PySide6: GUI
- Pillow: 画像処理
- pillow-heif: HEIC/HEIF画像の読み込み
- mediapipe: 顔検出と表情のランドマーク
- dlib: 128次元の顔特徴量（人物の識別）
- face_recognition_models: dlib の学習済みモデルデータ（import はしない）
- opencv-python: 画像/動画読み込みと品質評価
- numpy: 数値処理
- PyYAML: ルールの YAML 読み込み

## 注意事項

- 元の写真・動画を変更しません。
- 出力先フォルダへファイルをコピーします。
- GUI は人物登録と顔の割り当てを担当し、顔検出と自動紐づけは CLI で実行します。
- `photoarchive analyze` は廃止しました。顔検出は `photoarchive scan` に、人物への紐づけは GUI と `photoarchive match` に分かれています。
- `photoarchive convert-heic` はデータベースを使わないので、`--db` も設定ファイルも必要ありません。
- 顔の切り出し方（`face.EMBED_PADDING` など）を変えた場合は、`face.EMBED_VERSION` を必ず上げて再スキャンしてください。古い特徴量と新しい特徴量が混ざると照合が成立しません。
