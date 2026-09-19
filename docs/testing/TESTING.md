# テストの実行手順

何がテストで守られているかは [TEST_CASES.md](TEST_CASES.md)。

## 1. 準備

```bash
cd /home/suu/github/PhotoArchiveAI
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
pip install -e .        # これを忘れると photoarchive_ai を import できない
```

`pytest` は `requirements.txt` に入っている。

## 2. 回帰テスト（PR を起票する前に必ず実行する）

```bash
./scripts/run_regression.sh
```

これが実行するもの。

1. `pytest -m "not models"` — 実物の学習済みモデルを必要としないテスト全部
2. コマンドの起動スモーク（`photoarchive --help` / `photoarchive-gui --help`）。
   entry point の破損はテストでは捕まらないため
3. `pytest -m models` — **参考。環境で結果が変わるので合否には含めない**
4. 作業履歴の切り出しが必要かの通知

最終行にこの形のまとめが出る。**これを PR 本文へ貼る。**

```
回帰テスト: N passed / M failed (X.XXs) 実行日: YYYY-MM-DD
```

1 と 2 がすべて成功したときだけ終了コードが 0 になる。
**落ちた状態で PR を起票しない。**

## 3. 個別に動かす

```bash
pytest -q                          # 全部
pytest -q tests/test_matcher.py    # ファイル単位
pytest -q tests/test_system.py::test_end_to_end_flow
pytest -q -m system                # 通しテストだけ
pytest -q -m models                # 実物の dlib モデルを使う確認だけ
pytest -q -m "not models"          # 回帰テストと同じ範囲
pytest -q --durations=10           # 遅いテストを探す
```

`models` マーカーのテストは、学習済みモデル
（`shape_predictor_5_face_landmarks.dat` と
`dlib_face_recognition_resnet_model_v1.dat`）が見つからない環境では
自動的にスキップされる。

未登録のマーカーは `--strict-markers` により**失敗**になる。
新しいマーカーを使うときは `pyproject.toml` の `markers` に足すこと。

## 4. VS Code から動かす

1. VS Code でこのワークスペースを開く
2. 左の `Testing` アイコンから `Run All Tests`

`pyproject.toml` の `[tool.pytest.ini_options]` で `tests/` 以下の `test_*.py` を
自動検出する。

## 5. 実行時間

**上限は10秒。** 回帰テストは繰り返し実行するものなので、この範囲を保つ。
新しいテストが目立って遅い場合は `--durations=10` で確認する。

いま何件あるか、どれだけかかるかは、実行すれば分かる。

```bash
./scripts/run_regression.sh          # 最終行に件数と所要時間が出る
pytest -q --collect-only | tail -1   # 件数だけ
pytest -q --durations=10             # 遅い順に10件
```

## 6. 実データでの手動確認

自動テストは外部依存を持たないので、**実データでしか確かめられないことが残る。**
NFS 上の実データを使う確認は手動で行う。

`mediaFiles/` のシンボリックリンク先が存在することを確認し、
`config/app_settings.json` を作って `source_root` を設定する。

既存のデータベースがあれば先に移行する（バックアップは自動で作られる）。

```bash
photoarchive migrate --db data/photoarchive.db
```

**いきなり全件をスキャンせず、年フォルダ1つなど小さい範囲で確かめる。**

```bash
# 小さい範囲でスキャンし、顔が貯まり、人物が紐づいていないことを確認する
photoarchive scan --source /path/to/photo/2019 --workers 4 --log-level INFO
sqlite3 data/photoarchive.db "select face_count, count(*) from Media where face_count is not null group by face_count;"
sqlite3 data/photoarchive.db "select count(*) from Face where person_id is not null;"   # 0 であること
sqlite3 data/photoarchive.db "select count(*) from Face where embedding is null;"       # 大半が0でないなら異常

# 2回目が差分スキャンになっていることを確認する
time photoarchive scan --source /path/to/photo/2019

# 更新時刻だけが変わったファイルを、毎回読み直していないことを確認する
touch /path/to/photo/2019/somefile.jpg
time photoarchive scan --source /path/to/photo/2019   # 1件だけ読む
time photoarchive scan --source /path/to/photo/2019   # 何も読まない

# 設定ファイルの無い場所からでも convert-heic が動くことを確認する
cd /tmp && photoarchive convert-heic --source /path/to/photo/2023 ; cd -

# GUIで顔を割り当てたあと、閾値の目安を見てから自動割り当てを実行する
photoarchive match --dry-run
photoarchive match
photoarchive match --dry-run   # 2回目も同じ件数・分布が出ること
sqlite3 data/photoarchive.db "select assign_source, count(*) from Face group by assign_source;"

# 閾値を決める前に、取りこぼし率と誤一致率を測る(DBには書き込まない)
photoarchive evaluate
photoarchive evaluate --thresholds 0.4,0.45,0.5
```

`evaluate` は **1人につき別の写真から2枚以上** 割り当てていないと測れない。
その顔を抜くと手本が残らない顔は、取りこぼしに数えず評価から外れる
（外さないと、手本の少ない人物のぶんだけ取りこぼし率が悪く出る）。

並列スキャンを変えた場合は、**実データで `--workers` を2以上にして流し、
終わったら `sqlite3 data/photoarchive.db "PRAGMA integrity_check;"` が `ok` を
返すことを確かめる。** ワーカーを `fork` で起こすとDBが壊れるが、
**数万件規模でしか再現しない**ので、テストでは捕まえられない（Issue #39）。

あわせて `data/logs/scan_*.log` に**ワーカーのログが残っているか**を見る。
`spawn` の子は白紙で始まるので、ログ設定を渡し忘れると
「どのファイルがなぜ読めなかったか」だけが並列時に消える。

```bash
grep -c "Cannot read image" data/logs/scan_*.log   # errors の件数と見合うこと
```

顔の位置や大きさの扱いを変えた場合（`face.EMBED_PADDING` など）は、
**必ず `face.EMBED_VERSION` を上げて再スキャンする。** 切り出し方が変わると
顔特徴量の距離が別人判定の閾値と同じ程度に動くため、古い特徴量と新しい特徴量を
混ぜると照合が成立しない。

## 7. テストを書くときの決まり

- **リポジトリの中にファイルを書かない。** `tmp_path` を使う
- **外部依存を持ち込まない。** ネットワーク、NFS、実データベース、実物のモデルに
  触らない（`models` マーカーは例外）
- **修正1件につきテストを1件以上。** テストの無い修正は入れない
- 過去のバグを閉じ込めているテストは、何を防いでいるかを docstring に書く。
  一覧は [TEST_CASES.md](TEST_CASES.md) にまとめる
