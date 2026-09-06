# PhotoArchiveAI

PhotoArchiveAI は、長期間保存された家族の写真・動画アーカイブから、AI解析とルールに基づいて良い素材を抽出し、別フォルダへコピーするローカルアプリケーションです。

## 特長

- メディアを再帰的にスキャンして SQLite に登録
- 写真の EXIF / 撮影日時を取得
- 顔検出と埋め込みを使った人物判定
- PySide6 GUI での人物登録と顔画像登録
- CLI でのピックアップ/コピー実行
- 元の写真・動画ファイルは変更しない

## 全体のフロー

1. `config/app_settings.sample.json` をコピーして `config/app_settings.json` を作成し、`database_path` / `source_root` / `output_root` / `rule_path` を設定します。
2. `photoarchive init-db` で SQLite データベースを初期化します。
3. 必要に応じて `photoarchive convert-heic` でHEIC/HEIFをJPEGへ変換します。
4. `photoarchive scan` で対象ディレクトリをスキャンします。
5. `photoarchive analyze` で AI 解析を実行します。
6. `photoarchive-gui` で人物登録 GUI を起動し、登録を行います。
7. `config/rule.json` を編集し、`photoarchive select` でコピー先へ出力します。

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

`pyproject.toml` には、上記に加えて人物登録・顔照合で使用する次のライブラリも記載しています。

- `face_recognition`: 顔エンコーディングと顔照合
- `face_recognition_models`: `face_recognition` 用モデル。GitHubからインストール

通常は以下で全Python依存をインストールできます。

```bash
cd /home/suu/github/PhotoArchiveAI
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
python -m pip install -e .
```

`face_recognition` は内部で `dlib` を使用するため、上記の `cmake`、C/C++ビルドツール、Boost、OpenBLAS、LAPACKが必要です。`face_recognition_models` はGitHubリポジトリから取得されます。

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

### 4. メディアのスキャン

```bash
photoarchive scan
```

スキャン中は、処理済み件数と進捗バーがターミナルに表示されます。

### 5. AI 解析の実行

```bash
photoarchive analyze
```

解析中も進捗バーが表示されます。ログは `data/logs/` に出力され、通常は警告以上だけを記録します。

ログレベルは `--log-level` で指定できます。

```bash
# 通常運用（デフォルト）
photoarchive analyze --log-level WARNING

# 顔未検出などの情報も確認
photoarchive analyze --log-level INFO

# ファイル確認などの詳細情報も確認
photoarchive analyze --log-level DEBUG
```

指定できるレベルは `DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL` です。ログレベルを下げるほど出力が増えるため、通常はデフォルトの `WARNING` を使用してください。

### 6. GUI で人物登録

```bash
photoarchive-gui --db data/photoarchive.db
```

GUIの起動方法、人物情報の追加・編集・削除、顔画像の登録方法は [GUI利用手順](docs/operation/GUI_USAGE.md) を参照してください。

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

`tests/conftest.py` では、テスト収集時に `face_recognition` / `face_recognition_models` のフェイクモジュールを挿入し、依存関係が揃わない環境でも `tests/test_system.py` の実行を安定させます。

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
      scanner.py
      analyzer.py
      selection.py
      cli.py
      gui.py
```

## 主要ライブラリ

- PySide6: GUI
- Pillow: 画像処理
- pillow-heif: HEIC/HEIF画像の読み込み
- face_recognition: 顔検出と埋め込み
- opencv-python: 画像/動画読み込みと品質評価
- numpy: 数値処理
- PyYAML: ルールの YAML 読み込み

## 注意事項

- 元の写真・動画を変更しません。
- 出力先フォルダへファイルをコピーします。
- GUI は人物登録と顔画像登録をサポートし、解析は CLI で実行します。
