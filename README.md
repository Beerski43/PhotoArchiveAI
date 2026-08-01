# PhotoArchiveAI

PhotoArchiveAI は、長期間保存された家族の写真・動画アーカイブから、AI解析とルールに基づいて良い素材を抽出し、別フォルダへコピーするローカルアプリケーションです。

## 特長

- メディアを再帰的にスキャンして SQLite に登録
- 写真の EXIF / 撮影日時を取得
- 顔検出と埋め込みを使った人物判定
- PySide6 GUI での人物登録と顔画像登録
- CLI でのピックアップ/コピー実行
- 元の写真・動画ファイルは変更しない

## インストール

Ubuntu 環境での例:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip build-essential cmake libopenblas-dev liblapack-dev libx11-dev libxrandr-dev libxkbcommon-x11-0 libjpeg-dev libpng-dev libtiff-dev libavcodec-dev libavformat-dev libswscale-dev

cd /home/suu/github/PhotoArchiveAI
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
pip install -e .
```

`face_recognition` は `dlib` を必要とするため、依存ライブラリが不足している場合はビルドに失敗する可能性があります。

## 使い方

### 1. データベース初期化

```bash
photoarchive init-db --db photoarchive.db
```

### 2. メディアのスキャン

```bash
photoarchive scan --source /path/to/media --db photoarchive.db
```

### 3. AI 解析の実行

```bash
photoarchive analyze --db photoarchive.db
```

### 4. GUI で人物登録

```bash
photoarchive-gui --db photoarchive.db
```

### 5. 抽出ルールに基づく選択とコピー

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
photoarchive select --db photoarchive.db --rule rule.json --output /path/to/output --source /path/to/media
```

## ディレクトリ構成

```
PhotoArchiveAI/
  pyproject.toml
  requirements.txt
  README.md
  docs/
  src/
    photoarchive_ai/
      __init__.py
      __main__.py
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
- face_recognition: 顔検出と埋め込み
- opencv-python: 画像/動画読み込みと品質評価
- numpy: 数値処理
- PyYAML: ルールの YAML 読み込み

## 注意事項

- 元の写真・動画を変更しません。
- 出力先フォルダへファイルをコピーします。
- GUI は人物登録と顔画像登録をサポートし、解析は CLI で実行します。
