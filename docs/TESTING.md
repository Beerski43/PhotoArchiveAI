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
pytest tests/test_scanner_incremental.py   # 差分スキャンと削除行の掃除
pytest tests/test_matcher.py               # 自動紐づけの判定
pytest tests/test_gui_assignment.py        # GUIでの顔の割り当て
pytest tests/test_migration.py             # 旧スキーマからの移行
```

システムテストを実行する場合:

```bash
pytest -q tests/test_system.py
```

個別のエンドツーエンドテストのみを実行する場合:

```bash
pytest -q tests/test_system.py::test_end_to_end_flow
```

`system` マーカー付きテストを全て実行する場合:

```bash
pytest -q -m system
```

実物の dlib モデルを使う確認だけを実行する場合:

```bash
pytest -q -m models
```

`models` マーカーのテストは、学習済みモデル (`shape_predictor_5_face_landmarks.dat` と `dlib_face_recognition_resnet_model_v1.dat`) が見つからない環境では自動的にスキップされます。

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

`tests/test_system.py` は `init-db` → `scan` → GUIでの顔の割り当て → `match` → `select` の流れを通しで確認します。

`tests/conftest.py` が次の2つを差し替えるため、依存関係やモデルファイルが揃わない環境でもテストが安定して動きます。

- **mediapipe**: 顔検出のフェイク。真っ黒な画像は「顔なし」、それ以外は「顔が1つ」を返します。実装が先に参照する `relative_bounding_box` を返すので、実際の検出経路をそのまま通ります
- **dlib**: `face._load_dlib_models()` を差し替え、顔領域の平均色から決まる128次元ベクトルを返します。同じ色の顔は近く、違う色の顔は遠くなるため、`match` の判定（閾値・マージン・手本の選び方）を検証できます

`tests/helpers.py` の `write_image()` に色を指定すると、テスト内で「同一人物」「別人」を作り分けられます。

GUIのテストは `QT_QPA_PLATFORM=offscreen` で動くため、画面が無い環境でも実行できます。

## 6. 追加の手動確認

実データでの動作確認は別途手動で行います。`mediaFiles/` のシンボリックリンク先が存在することを確認し、`config/app_settings.json` を作成して `source_root` を設定してください。

既存のデータベースがある場合は、先に移行します。バックアップは自動で作成されます。

```bash
photoarchive migrate --db data/photoarchive.db
```

いきなり全件をスキャンせず、年フォルダ1つなど小さい範囲で所要時間と結果を確かめてください。

```bash
# 小さい範囲でスキャンし、顔が貯まることと、人物が紐づいていないことを確認する
photoarchive scan --source /path/to/photo/2019 --workers 4 --log-level INFO
sqlite3 data/photoarchive.db "select face_count, count(*) from Media where face_count is not null group by face_count;"
sqlite3 data/photoarchive.db "select count(*) from Face where person_id is not null;"   # 0 であること

# 2回目が差分スキャンになっていることを確認する
time photoarchive scan --source /path/to/photo/2019

# GUIで顔を割り当てたあと、閾値の目安を確認してから自動紐づけを実行する
photoarchive match --dry-run
photoarchive match
sqlite3 data/photoarchive.db "select assign_source, count(*) from Face group by assign_source;"
```

顔の位置や大きさの扱いを変えた場合（`face.EMBED_PADDING` など）は、**必ず `face.EMBED_VERSION` を上げて再スキャンしてください。** 切り出し方が変わると顔特徴量の距離が別人判定の閾値と同じ程度に動くため、古い特徴量と新しい特徴量を混ぜると照合が成立しません。
