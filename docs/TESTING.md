# テスト実行手順

このプロジェクトでは `pytest` を使ってユニットテストを実行します。

## 1. テスト環境の準備

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

`pytest` は `requirements.txt` に追加済みです。

## 2. テストの実行

```bash
pytest
```

特定のテストファイルを実行する場合:

```bash
pytest tests/test_db.py
pytest tests/test_selection.py
```

## 3. VS Code Test Explorerでの実行

1. VS Code でこのワークスペースを開く
2. `tests/` フォルダを右クリックして `Test` を実行
3. あるいは、左の `Testing` アイコンを開き、`pytest` を選択して `Run All Tests` を押す

`pyproject.toml` に `pytest` 設定があり、`tests/` 下の `test_*.py` を自動検出します。

## 4. テスト結果の確認

- 成功: 緑のチェックマーク
- 失敗: 赤のバツ印と失敗したテストのトレースバック
- 失敗時は `tests/` 内のテストファイルとプロジェクト内の対応する実装ファイルを確認してください

## 5. テストデータについて

このテストでは、SQLite の一時ファイルを `tmp_path` に生成し、テスト終了後に自動的に削除します。

## 6. 追加の手動確認

メディアファイルの解析や GUI の動作確認は別途手動で行います。`mediaFiles/` のシンボリックリンク先が存在することを確認し、必要に応じて `config/app_settings.json` を作成して `source_root` を設定してください。
