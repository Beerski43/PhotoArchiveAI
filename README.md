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
2. `photoarchive init-db` で SQLite データベースを初期化します。（既存のデータベースがある場合は `photoarchive migrate` を実行します）
3. 必要に応じて `photoarchive convert-heic` でHEIC/HEIFをJPEGへ変換します。
4. `photoarchive scan` で対象ディレクトリをスキャンします。パスの登録と顔の検出をここでまとめて行います。
5. `photoarchive-gui` で人物を登録し、検出された顔を人物へ割り当てます。
6. `photoarchive match` で、割り当てきれなかった顔を自動で紐づけます。
7. `config/rule.json` を編集し、`photoarchive select` でコピー先へ出力します。

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

必要に応じて `database_path` / `source_root` / `output_root` を編集します。

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

実体が見つからないメディアが登録数の2割を超えた場合は、ソースの指定間違いやNFSの未マウントを疑って処理を中断します。意図した削除であれば `--force-prune` を付けて再実行してください。

ログは `data/logs/scan_*.log` に出力されます。ログレベルは `--log-level` で `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` を指定できます（既定は `WARNING`）。

途中で中断しても、顔検出が終わったメディアは記録済みなので、再実行すれば続きから再開します。

### 5. GUI で人物登録と顔の割り当て

```bash
photoarchive-gui --db data/photoarchive.db
```

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
```

`--dry-run` は顔の距離の分布を表示するので、`--threshold` を決める目安になります。ログは `data/logs/match_*.log` に出力されます。

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

このプロジェクトでは `pytest` を使って単体テストとシステムテストを実行します。

`tests/conftest.py` では、テスト収集時に `mediapipe` のフェイクモジュールを挿入し、`dlib` のモデル読み込みも差し替えます。依存関係やモデルファイルが揃わない環境でもテストが安定して動きます。

システムテストを実行するには:

```bash
pytest -q tests/test_system.py
```

個別のエンドツーエンドテストだけを実行する場合:

```bash
pytest -q tests/test_system.py::test_end_to_end_flow
```

`system` マーカー付きテストを指定する場合:

```bash
pytest -q -m system
```

## ディレクトリ構成

```
PhotoArchiveAI/
  pyproject.toml
  requirements.txt
  README.md
  .gitignore
  config/
    app_settings.sample.json
    rule.sample.json
  docs/
  src/
    photoarchive_ai/
      __init__.py
      __main__.py
      config.py
      db.py
      migration.py
      scanner.py
      face.py
      scoring.py
      matcher.py
      converter.py
      selection.py
      cli.py
      gui.py
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
