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

1. `config/app_settings.sample.yml` をコピーして `config/app_settings.yml` を作成し、`database_path` / `source_roots` / `output_root` / `rule_path` を設定します。
2. `photoarchive init-db` で SQLite データベースを初期化します。（既存のデータベースがある場合は `photoarchive migrate` を実行します。バックアップは自動で作られます）
3. 必要に応じて `photoarchive convert-heic` でHEIC/HEIFをJPEGへ変換します。
4. `photoarchive scan` で対象ディレクトリをスキャンします。パスの登録と顔の検出をここでまとめて行います。
5. `photoarchive-gui` で人物を登録し、検出された顔を人物へ割り当てます。
6. `photoarchive match` で、割り当てきれなかった顔を自動で紐づけます。結果が
   信用できなければ `photoarchive unassign-auto` で取り消せます。
7. `photoarchive evaluate` で、`match` の取りこぼしと誤りの割合を確かめます（任意）。
8. `config/rule.yml` を編集し、`photoarchive select` でコピー先へ出力します。

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

- `dlib`: 旧い特徴量モデル（2026-10-02 に ArcFace へ替えたため、いまは使っていない）
- `face_recognition_models`: dlib の学習済みモデルデータ。GitHubからインストール（モデルファイルの置き場所としてのみ使い、import はしない）

- `onnxruntime`: ArcFace（512次元の顔特徴量）の推論。**モデル本体は別途取得します**（下記）

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

このパッケージは **モデルファイルの置き場所としてのみ** 使用し、Pythonモジュールとしては読み込みません。`face_recognition_models/__init__.py` が `pkg_resources` に依存しており、setuptools 81 以降では `ModuleNotFoundError` になるためです。モデルを別の場所に置く場合は、環境変数 `PHOTOARCHIVE_DLIB_MODEL_DIR` か `config/app_settings.yml` の `dlib_model_dir` でディレクトリを指定してください。

### 顔特徴量のモデル（ArcFace）の取得 — **必須**

顔の識別には ArcFace（512次元）を使います。**モデル本体は git 管理外**なので、
次のコマンドで `models/` に取得してください。

```bash
cd /home/suu/github/PhotoArchiveAI
source .venv/bin/activate
python scripts/fetch_models.py
```

**sha256 を検証して置きます**（一致しないものは置きません）。すでにあって
一致すれば何もしません。取り直すときは `--force`、検証だけなら `--check`。

- 取得するのは `models/w600k_r50.onnx`（174MB）。InsightFace の `buffalo_l`
  配布物から**認識用の1本だけ**を取り出したものです
- **`insightface` パッケージは入れません。** モデル動物園と GPU 版の
  `onnxruntime` を引き込むため、ONNX 1本と `onnxruntime` だけで動かしています
- `models/` は git 管理外です（dlib の `.dat` を手置きする場合の探索先と同じ扱い）。
  環境変数 `PHOTOARCHIVE_ONNX_MODEL_DIR` で別の場所を指定できます
- **モデルが無いと `scan` と `reembed` は開始前に中断します**（顔は検出されるのに
  特徴量が保存されない状態を避けるため）

**2026-10-02 に dlib ResNet から替えました。** 実データの手本126件で1位正解率
69.8% → 95.2%。測定は
[docs/history/details/2026-10-02-embedding-model-comparison.md](docs/history/details/2026-10-02-embedding-model-comparison.md)。
比べ直すときは `python scripts/measure_embedding_models.py --db data/photoarchive.db`。

SQLite、`argparse`、`json`、`logging`、`pathlib`、`shutil`、`hashlib` などはPython標準ライブラリのため、個別インストールは不要です。

## 使い方

### 1. アプリ設定ファイル作成

```bash
cp config/app_settings.sample.yml config/app_settings.yml
```

必要に応じて `database_path` / `source_roots` / `output_root` / `rule_path` を編集します。

**写真の置き場所（root）が複数あれば、`source_roots` に並べます**（#24）。

```yaml
source_roots:
  - /mnt/nfs/nanoPi-NEO2/suzuki/Photo
  - /mnt/nfs/nanoPi-NEO2/share/photo/natsuTemp/な携帯
```

**共通の親（`/mnt/nfs/nanoPi-NEO2`）を書かないでください。** 関係の無いフォルダまで取り込みます。入れ子になった root は止めます。以前の `source_root:`（1つだけのキー）は読みません。残っていれば警告を出すので、`source_roots:` に書き直してください。

**設定ファイルは YAML です（JSON は読みません）。** 以前の `config/app_settings.json` が残っているだけだと、YAML への変換を促すメッセージを出して止まります。JSON の中身は YAML として読めるので、**空白で字下げしていれば** `mv config/app_settings.json config/app_settings.yml` でも移れます（タブで字下げしていると読めません）（ルールファイルも同じ。`rule_path` も `.yml` に向けてください）。

設定ファイルは次の順に探し、最初に見つかったものを使います。**リポジトリ以外のディレクトリから実行しても設定が効きます。**

1. 環境変数 `PHOTOARCHIVE_CONFIG` が指すファイル
2. カレントディレクトリの `config/app_settings.yml`
3. リポジトリ直下の `config/app_settings.yml`（`pip install -e .` のときだけ）

### 2. データベース初期化

```bash
source .venv/bin/activate
photoarchive init-db
```

### 3. HEIC/HEIFのJPEG変換

HEIC/HEIF画像を含む場合は、**スキャン前にJPEGへ変換してください。** `scan` は HEIC/HEIF を読みません（同じ写真が JPEG と二重に登録され、同じ顔に二度割り当てることになるため。#26）。以前の `scan` で登録された HEIC の行は、次の `scan` で消えます。**`scan` は HEIC をファイル名も含めて一切見ない**ので、変換し忘れた HEIC があっても知らせません。新しく写真を足したら、先に `photoarchive convert-heic` を流してください。

```bash
photoarchive convert-heic
```

`source_roots` のディレクトリ以下を再帰的に処理します。別のディレクトリを指定する場合は、次のように `--source` を使用します（何度でも書けます）。

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
- **EXIF に撮影日時が無い写真の撮影時期を、フォルダ名から起こす**（`2012/1210/` → 2012年10月。月まで。EXIF の値とは別の列に持ちます）
- 顔の検出と、顔画像・特徴量・スコアの保存
- 実体が無くなったメディアの行の削除（HEIC など走査の対象外になった拡張子の行も消します）

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

顔特徴量のモデル (ArcFace) を読み込めない場合、`scan` は処理を始める前に中断します。そのまま進むと、顔は検出されるのに特徴量が保存されず、`match` が一切効かない状態のまま「スキャン済み」として記録されてしまうためです。承知のうえで進める場合は `--allow-missing-embeddings` を付けます。

このオプションで取り込んだメディアは検出器の版を記録しないので、**モデルを設置したあとに通常の `photoarchive scan` を実行すれば自動でやり直されます。**`--force-rescan` は不要です（`--force-rescan` は手動で割り当てた顔も作り直してしまいます）。

実体が見つからないメディアが登録数の2割を超えた場合は、ソースの指定間違いやNFSの未マウントを疑って処理を中断します。意図した削除であれば `--force-prune` を付けて再実行してください。

**root が複数あれば root ごとに走査し、消えた行の削除と2割の判定も root ごとに行います。** 片方の NFS が外れていても、もう片方の写真は消えません。`--source` は何度でも書けます（`--source A --source B`）。

**走査し終えた root はデータベースに記録されます。** 設定ファイルを失っても、`--source` も設定も無い `photoarchive scan` は記録された root で走査します（何を使うかを表示します）。年フォルダだけを `--source` で流しても root としては記録しません。**記録した root を内側に含む親フォルダを `--source` に渡すと、走査する前に止めます**（関係の無い写真を取り込まないため）。指定した root が1つでも無ければ、どの root も走査せずに止めます。

ログは `data/logs/scan_*.log` に出力されます。ログレベルは `--log-level` で `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` を指定できます（既定は `WARNING`）。

途中で中断しても、顔検出が終わったメディアは記録済みなので、再実行すれば続きから再開します。

### 補足. 特徴量モデルを替えたとき（`reembed`）

アプリケーションを新しくして**顔特徴量のモデルが替わった**場合、既存の顔の
特徴量を作り直す必要があります。

```bash
photoarchive reembed --db data/photoarchive.db --dry-run   # 件数と見積り
photoarchive reembed --db data/photoarchive.db
```

- **保存済みのサムネイルから作り直すので、元写真を読みません。**
  実データ（顔 58,606 件）で**約158分**です（実測 162ms/件）
- **GUI で割り当てた顔・除外した顔・入力した年齢は残ります。**
  書き換えるのは特徴量とその版だけです
- **途中で止めても、もう一度実行すれば続きから再開します**
- 終わったら `photoarchive match` をやり直してください（手本の特徴量も
  作り直されているため）

**`photoarchive scan --force-rescan` は使わないでください。** あちらは顔の行を
消して作り直すので、**手作業で割り当てた顔が消えます。**

### 5. GUI で人物登録と顔の割り当て

```bash
photoarchive-gui
```

`--db` を省くと `config/app_settings.yml` の `database_path` を使います。別のデータベースを開く場合は `photoarchive-gui --db data/photoarchive.db` のように指定します。

人物を登録し、`scan` が検出した顔のサムネイル一覧から、その人物の顔を選んで割り当てます。ここで割り当てた顔が次の `match` の手本になります。

**画面は1枚です。** 左の一覧で「いま何を見るか」（`未割当` / `自動割当` / `除外済み` / 人物ごと）を選び、**顔を右クリック**して操作します。**`1`〜`9` の打鍵でその番号の人物に割り当てられます**（番号は左の一覧の並び順。ドラッグで変えられます）。

**行事（フォルダ×日）ごとにまとめて片付けられます。** `行事を選ぶ` で絞り、`この行事の顔を束ねる` を押すと、似た顔を束ねて束ごとに割り当て・除外できます。別人が混ざっていたら `この束を割る` で割り直せます。手動で割り当てた顔は、まとめて操作しても動きません。

操作手順は [GUI利用手順](docs/operation/GUI_USAGE.md) を参照してください。

### 6. 残りの顔の自動紐づけ

```bash
photoarchive match
```

手動で割り当てた顔を手本に、未割当の顔を自動で紐づけます。十分な枚数を手作業で割り当ててあるなら、実行しなくても構いません。

**GUI からも流せます**（左下の `自動割り当て（match）を実行…`。進み具合が出て、流す前に控えを取るかを選べます）。

- 手本に使うのは **手動で割り当てた顔だけ** です。自動割り当ての結果を手本に混ぜると、誤りが次の判定の根拠になって増幅するためです。
- **GUI で「この人物ではない」と記録した人物は、その顔の候補から外します。** 「割り当てを解除」しただけでは判断が残らず、流すたびに同じ顔が戻ります。
- **その写真の時点で生まれていない人物は、候補から外します。** 人物に誕生日を登録しておくと効きます。EXIF の無い写真は、フォルダ名から起こした撮影時期の終わりまでに生まれていない人物だけを外します（撮影時期が分からない写真や、誕生日が未登録の人物は絞り込みません）。
- 似ている度合いが基準に届かない顔は **未割当のまま残します**。
- **顔がうまく写っていない手本（横顔・見切れなどで目鼻の位置が取れないもの）は、割り当ての根拠にしません。** 誤って他人を引き寄せていたためです。割り当てそのものは消しません。初回は手本の写り方を測るので数分かかります（2回目からは新しい手本の分だけ）。
- **8歳以下の手本はより厳しく（0.35）、年齢が分からない手本も少し厳しく（0.40）判定します。** 赤ちゃんの顔は誰でも互いに似ているためです。年上の子どもの顔は、その年頃の手本を割り当てておくと付きやすくなります。
- 実行のたびに自動割り当てを付け直すので、何度実行しても同じ結果になります。

```bash
# どれくらい割り当てられそうかを、書き込まずに確認する
photoarchive match --dry-run

# 判定を厳しく／緩くする（既定は 0.45、小さいほど厳しい）
photoarchive match --threshold 0.5

# 2位の人物との距離差の下限（既定は 0.08）
photoarchive match --margin 0.1
```

`--dry-run` は顔の距離の分布を表示するので、`--threshold` を決める目安になります。**`match` を実行したあとでも同じ結果が出ます**（自動割り当てを取り消したあとの状態を再現して数えるため）。ログは `data/logs/match_*.log` に出力されます。

#### 自動紐づけの結果を取り消す

GUI で見直して結果が信用できなかったときは、**自動割り当てだけを取り消せます。**

```bash
# 全員ぶん
photoarchive unassign-auto

# 1人ぶんだけ（名前か id）
photoarchive unassign-auto --person ひより

# 消す前に件数だけ確かめる
photoarchive unassign-auto --person ひより --dry-run
```

**手動で割り当てた顔（手本）・除外した顔・設定した年齢には触りません。**
`match` は手本と閾値だけで結果が決まるので、**手本を直してから `photoarchive match` を
流し直せば付け直せます。**

### 7. 紐づけの精度を測る（任意）

```bash
photoarchive evaluate
```

手動で割り当てた顔を正解とみなし、**閾値ごとの取りこぼし率と誤り（誤一致）率**を表にします。データベースには書き込みません。

- **取りこぼし**: 未割当のまま残った顔。閾値を緩めれば拾えます
- **誤り**: 別の人物に割り当てられた顔。閾値を緩めるほど増えます

```bash
# 試す閾値を指定する
photoarchive evaluate --thresholds 0.4,0.45,0.5

# 同じ写真に写る同一人物の顔も手本に残す(既定は外す)
photoarchive evaluate --keep-same-media
```

手本の顔を1件ずつ抜き、残りを手本にして元の人物へ戻るかを見ています（1件抜き交差検証）。**同じ写真に写る同一人物の顔は既定で手本から外します。** 抜いた顔とほぼ同じ手本が残っていると必ず当たり、取りこぼしが0に見えてしまうためです。

**1人につき、別の写真から2枚以上**割り当てていないと測れません（その顔を抜くと手本が残らないため、評価から外れます）。測り方の詳細は [仕様書 §8.5](docs/spec/Specification.md)。

### 8. 抽出ルールに基づく選択とコピー

`config/rule.sample.yml` をコピーして `config/rule.yml` とし、必要に応じて編集します。

```bash
photoarchive select
```

`rule.yml` の例:

```yaml
date:
  start: 2000-01-01
  end: 2025-12-31   # その日の終わりまで含む
family_only: true
count_per_year: 30
include_video: true
remove_duplicate: true
```

実行:

```bash
photoarchive select
```

**家族の写真としての良さで並べます。** 家族の顔がボケていない・正面を向いている・笑顔であるほど上に来て、はっきり写った家族が多いほど優先します（他人の顔は点に入れません）。点は実行のたびにいまの割り当てから計算するので、GUI で直した割り当てもすぐ効きます。家族の顔の写り方をまだ測っていなければ、先に測ります（初回は数分）。

`match` の規則が変わったあとに `match` を流し直していないと、古い判定が残っている件数を知らせます。

コピー処理でも進捗バーが表示され、出力先へどこまでコピー済みか確認できます。

直接引数を使う場合は `--db`, `--source`, `--output`, `--rule` で設定を上書きできます。

例:

```bash
photoarchive select --output /path/to/output --source /path/to/media --rule config/rule.yml
```

## テスト

変更を加えたら、回帰テストを実行します。

```bash
source .venv/bin/activate
./scripts/run_regression.sh
```

数秒で終わります。最終行に次の形のまとめが出ます。

```
回帰テスト: N passed / M failed (X.XXs) 実行日: YYYY-MM-DD
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
    app_settings.sample.yml    アプリ設定のサンプル
    rule.sample.yml            抽出ルールのサンプル
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
      embedding.py             特徴量モデルの記述(次元数・尺度・閾値・版)と距離計算
      scanner.py               走査・差分判定・顔検出の呼び出し
      face.py                  顔検出(MediaPipe)と顔特徴量(ArcFace)
      reembed.py               保存済みサムネイルから特徴量を作り直す
      dates.py                 日付の読み取りと年齢の計算
      scoring.py               笑顔・画質のスコアと、家族写真としての良さ
      appearance.py            顔の写り方(整列できるか・向き・鮮明さ)をサムネイルから測る
      matcher.py               自動紐づけ
      recommend.py             顔を「この人物に似た順」に並べる(GUI の顔候補の推薦)
      clustering.py            行事の中で顔を束ねる(平均連結)
      evaluation.py            自動紐づけの精度の実測
      converter.py             HEIC/HEIF → JPEG 変換
      selection.py             ルールに基づく抽出とコピー
      logging_setup.py         ログの設定
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
- onnxruntime + ArcFace: 512次元の顔特徴量（人物の識別）
- dlib: 旧い特徴量モデル（2026-10-02 まで使用）
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
