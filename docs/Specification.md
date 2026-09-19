# PhotoArchiveAI Requirements Specification

Version: 0.1  
Status: Draft  
Last Update: 2026-08-01

---

# 1. Project Overview

## 1.1 Project Name

PhotoArchiveAI

## 1.2 Purpose

本ソフトウェアは、長期間保存された大量の写真・動画から、
AIを利用して家族の思い出となるメディアを自動選択するシステムである。

主な利用目的は以下。

- 過去10年以上の写真・動画から記念動画を作成するための素材抽出
- NAS上のフォトフレームで表示する写真セットの作成
- 家族写真アーカイブの整理

## 1.3 Scope

本システムは以下を担当する。

- 写真・動画ファイル検索
- メディア情報解析
- 顔認識
- 家族人物識別
- 写真品質評価
- 類似写真判定
- 抽出条件による写真選択
- 指定フォルダへのコピー

本システムは以下を担当しない。

- 動画編集
- 写真加工
- Web表示機能

---

# 2. Development Environment

## 2.1 Target Environment

- OS: Ubuntu Linux
- Language: Python
- Database: SQLite
- Version Control: Git
- Repository: Private repository

## 2.2 Design Policy

本システムはローカルPC内で完結できることを基本とする。

写真・動画データは外部サービスへ送信しない。

クラウドAIや外部APIは将来的なオプションとして扱う。

## 2.3 Configuration

アプリケーション設定は JSON ファイルで管理する。

- `config/app_settings.json` にデータベースパス、メディアソース、出力先、ルールファイルパスを記載する。
- Git には `config/app_settings.sample.json` を格納し、実際の設定ファイル `config/app_settings.json` は管理対象外とする。
- 抽出ルールは `config/rule.json`（または YAML）として管理できる。
- `config/rule.sample.json` をサンプルとして提供する。

`database_path` および `rule_path` を設定ファイルで指定することで、アプリケーション内に DB パスやルールパスをハードコードしない。

### 設定ファイルの格納場所

- アプリ設定ファイル: `config/app_settings.json`
- サンプル設定: `config/app_settings.sample.json`
- ルール設定: `config/rule.json`
- サンプルルール: `config/rule.sample.json`

### 設定ファイルのパス変更方法

`config/app_settings.json` のパスを変更するには、`src/photoarchive_ai/config.py` の `DEFAULT_CONFIG_PATH` を編集する。
## 2.4 Git 管理対象外ファイル

開発環境固有の生成物や実行環境は Git 管理から除外する。

- `.venv/`
- `data/`（生成される SQLite DB と出力先）
- `config/app_settings.json`（個別設定ファイル）
- `__pycache__/`, `*.pyc` などの Python ビルド成果物

---

# 3. Design Principles

## 3.1 Detection, Assignment and Extraction Separation

顔の検出、人物への紐づけ、抽出の3つを分離する。

検出 (`scan`):

```
写真
 ↓
顔検出
 ↓
顔画像・顔特徴量・スコアを保存
```

紐づけ (GUI → `match`):

```
検出済みの顔
 ↓
GUIで人物を登録し、顔を手動で割り当て
 ↓
match が、割り当て済みの顔を手本に残りを自動で紐づけ
```

抽出 (`select`):

```
解析結果DB
 ↓
抽出条件
 ↓
コピー
```

一度検出した結果は再利用する。

**検出と紐づけを同じ工程にしてはならない。** 人物が登録されていない段階で
自動紐づけを行うと、誰とも分からない顔が誤った人物に結びつく。
紐づけは必ず人物登録より後に行う。

---

## 3.2 Persistent Analysis Data

解析結果は一時データではなく永続データとして管理する。

保存先:

SQLite Database

再検出条件:

- 新規ファイル
- ファイルハッシュ変更
- 検出器・特徴量生成器のバージョン変更 (`Media.detector_version` の不一致)

`Media.face_count` が検出の実施状態を表す。

- `NULL`: 未スキャン
- `0`: スキャン済みで顔が見つからなかった（読み込めなかった場合を含む）
- `N`: 検出した顔の数

ファイルサイズと更新時刻がデータベースの記録と一致する場合は、
ハッシュを再計算しない。

---

## 3.3 No Custom AI Training

本システムでは人物認識モデル自体の再学習は行わない。

既存の顔認識モデルから取得した顔特徴量
(embedding)
を利用して人物識別する。

## 3.4 Git 管理対象外ファイル

開発環境固有の生成物や実行環境は Git 管理から除外する。

- `.venv/`
- `data/`（生成される SQLite DB と出力先）
- `config/app_settings.json`（個別設定ファイル）
- `__pycache__/`, `*.pyc` などの Python ビルド成果物

---

# 4. System Architecture

```
Photo / Video Files
        |
        v
Media Scanner  (scan: 登録 + 顔検出 + 特徴量 + スコア)
        |
        v
SQLite Database  <---- Face Registration GUI (人物登録 + 顔の手動割り当て)
        |                        |
        |                        v
        |                 Face Matcher (match: 残りの顔を自動紐づけ)
        |                        |
        v <----------------------+
Selection Engine
        |
        v
Output Directory
```

---

# 5. Media Scanner

## Requirements

以下を実装する。

- 指定ディレクトリ以下を再帰検索
- 写真ファイル検出
- 動画ファイル検出
- ファイル情報取得
- ハッシュ生成（サイズと更新時刻が一致する場合は省略）
- 顔検出と、顔画像・顔特徴量・スコアの保存
- 既に顔検出済みのメディアは再検出しない
- データベースに存在するが実体が無くなったメディアの行を削除

実体が無いメディアが登録数の一定割合（既定20%）を超えた場合は、
ソース指定の誤りや未マウントを疑って処理を中断する。

スキャンは人物への紐づけを行わない。

対応予定形式:

Images:

- JPEG
- PNG
- HEIC

Videos:

- MP4
- AVI
- その他追加可能な形式

---

# 6. Database Design

## 6.1 Media Table

保存項目:

- id
- path
- filename
- type
- file_hash
- file_size
- created_time
- shooting_date
- face_count (NULL=未スキャン / 0=顔なし / N=検出数)
- face_scanned_at
- detector_version


## 6.2 Person Table

登録人物情報。

例:

```
父
母
子供A
子供B
```

項目:

- id
- name
- relation
- memo


## 6.3 Face Table

写真から検出された顔を保存する。GUIで登録する「手本の顔」も、この中の
`assign_source = 'manual'` の行として表す。手本専用の行は作らない。

項目:

- id
- media_id (必須。必ず写真由来)
- bbox_top / bbox_right / bbox_bottom / bbox_left (元解像度での顔の位置)
- detection_score
- embedding (float32 × 128 のBLOB。顔が小さすぎる場合は NULL)
- embed_version (特徴量の生成規約。変われば再検出の対象)
- thumbnail (一覧表示用の小さな顔画像)
- smile_score / quality_score
- person_id (NULL = 未割当)
- assign_source (NULL=未割当 / 'manual'=手動 / 'auto'=自動 / 'rejected'=除外)
- assign_score (自動割り当て時の類似度 0-100)
- assigned_at
- age (撮影時の年齢。任意)
- created_at

`assign_source` の区別が重要になる理由:

- 自動紐づけの手本に使ってよいのは `'manual'` だけ。`'auto'` を手本に混ぜると、
  誤った紐づけが次の判定の根拠となって増幅する。
- `'auto'` だけをまとめて取り消せるので、`match` を何度実行しても同じ結果になる。
- `'rejected'` を持てるので、誰でもない顔が未割当一覧に残り続けない。


## 6.4 Analysis Result Table

メディア単位のスコア。顔の数は `Media.face_count` が持つ。

項目:

- media_id
- family_score (match が書く)
- smile_score (scan が書く)
- quality_score (scan が書く)

`scan` がスコアを書くときに `family_score` を潰さないこと。
逆に `match` は smile / quality を触らない。


---

# 7. Family Face Recognition

## 7.1 Person Registration

利用者は家族人物を登録する。

例:

```
父
母
子供A
子供B
```

登録には複数写真を利用する。

推奨:

- 正面顔
- 年齢差のある写真
- 明るい写真

---

## 7.2 Recognition Flow

顔検出と人物判定は、別の工程として分ける。

```
[ scan ]
写真
 |
 v
顔検出
 |
 v
顔特徴量生成
 |
 v
未割当の顔として保存        ← ここでは人物と比較しない

[ GUI ]
 |
 v
人物を登録し、顔を手動で割り当て (assign_source = 'manual')

[ match ]
 |
 v
手動割り当ての顔とだけ比較
 |
 v
閾値とマージンを満たせば人物判定 (assign_source = 'auto')
満たさなければ未割当のまま
```

---

## 7.4 Automatic Assignment (match)

手本:

- `assign_source = 'manual'` かつ `embedding` を持つ顔だけ
- 自動割り当ての結果は手本に含めない

判定:

- 顔特徴量のユークリッド距離が最小の人物を候補とする
- 距離が閾値（既定 0.5）以下であること
- かつ、次に近い別人物との距離差がマージン（既定 0.05）以上であること
- どちらかを満たさない顔は未割当のまま残す

冪等性:

- 実行のたびに `assign_source = 'auto'` の割り当てを一旦取り消してから付け直す
- 結果は「手本の集合」と「閾値・マージン」だけで決まる

`family_score` は、人物が紐づいた顔の類似度の最大値として算出し直す。
手動割り当ての顔は確信度 100 とみなす。

---

## 7.3 Age Change Handling

子供など長期間の成長を考慮する。

同一人物について複数年代の顔特徴量を保持可能にする。
年代の異なる顔を手動で割り当てておくことで、`match` の手本が厚くなる。

例:

```
Child A

0歳 embedding
5歳 embedding
10歳 embedding
15歳 embedding
```

年齢は `Face.age` に持つ。未設定と0歳は区別する。

---

# 8. Face Registration GUI

## 8.1 Purpose

顔登録作業を簡単にするため、専用GUIを提供する。

本GUIは解析エンジンとは独立した補助ツールとして実装する。

GUIは人物登録および顔画像管理のみを担当し、写真解析や抽出処理は行わない。

---

## 8.2 Requirements

以下の機能を提供する。

- 人物一覧表示
- 人物追加
- 人物編集
- 人物削除（顔は消さず、未割当に戻す）
- 未割当の顔の一覧表示と、人物への割り当て
- 割り当ての解除
- 誰でもない顔の除外
- 割り当て済み顔の一覧表示と、自動割り当ての確定

---

## 8.3 Person Registration

登録可能な情報

- 名前
- 続柄（任意）
- メモ（任意）

例

```
父
母
長男
長女
祖父
祖母
```

---

## 8.4 Face Assignment

GUIは、`scan` が検出して貯めた顔を人物へ割り当てる。
利用者が画像ファイルを選んで顔を切り出す操作は行わない。
検出と特徴量生成の規約を `scan` と GUI で一致させるためである。

推奨枚数

人物あたり 20〜50枚

推奨条件

- 正面
- 左右方向
- 笑顔
- 真顔
- 明るい写真
- 年齢の異なる写真

操作:

- 未割当の顔をサムネイルで一覧表示する
- 複数選択して、選択中の人物へまとめて割り当てる
- 割り当てた顔は `assign_source = 'manual'` になる

---

## 8.5 Unassigned Face List

顔は数万件になりうるため、一覧は必ずページ単位で読み出す。
サムネイルを全件読み込む実装にしてはならない。

```
[顔][顔][顔][顔][顔][顔]
[顔][顔][顔][顔][顔][顔]

 «  ‹   3 / 310 ページ（全 61,842 件）   ›  »
```

- 1ページあたりの件数は 200 件
- 並び順は品質スコアの高い順。大きく明るい顔から出るため、割り当て作業が
  進めやすい
- 表示の切り替えで「未割当」「自動割当」「除外済み」を選べる
- 顔を選ぶと、元写真から切り出し直して拡大表示する

---

## 8.6 Face Preview and Correction

人物を選んで「割り当て済みを確認」を開くと、サムネイル一覧を表示する。

```
父

[顔 手動][顔 自動 82][顔 手動]
```

顔を選ぶと、

- 確定（`'auto'` を `'manual'` に昇格し、次回の `match` の手本にする）
- 割り当て解除（未割当に戻す）
- 年齢の設定

が可能。誰でもない顔は「除外」して一覧から外せる。

---

## 8.7 Age Management

同一人物について複数年代の顔を保持できる。

例

```
長男

0歳
3歳
7歳
12歳
18歳
```

年齢入力は必須ではない。未設定と0歳は別の状態として扱う。

撮影日時(EXIF)から推定可能な場合は自動入力する。

---

## 8.8 Database

GUIは以下のテーブルのみ更新する。

- Person
- Face（`person_id` / `assign_source` / `assign_score` / `assigned_at` / `age` の各列）

MediaおよびAnalysisResultは更新しない。
顔の検出結果そのもの（座標・特徴量・サムネイル）もGUIからは書き換えない。

---

## 8.9 GUI Framework

GUIはPythonで実装する。

推奨ライブラリ

PySide6(Qt)

CLIとは独立した実行ファイルとして提供する。

例

```
photoarchive-gui
```

または

```
python face_registration_gui.py
```

---

## 8.10 Future Extensions

将来的に以下を追加できる構成とする。

- 顔候補の自動推薦
- 「この人は○○ですか？」確認画面
- 類似人物の統合
- 家族関係ツリー表示
- 人物検索
- 人物ごとの写真枚数表示

# 9. Image Quality Analysis

写真評価項目:

- ピント
- ブレ
- 明るさ
- 白飛び
- 顔サイズ
- 笑顔
- 目閉じ

評価結果をスコア化する。

例:

```
quality_score: 85
smile_score: 90
```

---

# 10. Duplicate Detection

連写写真や類似写真を検出する。

目的:

同じ場面の大量写真から代表1枚を選択する。

例:

```
IMG001
IMG002
IMG003

↓

IMG002 selected
```

---

# 11. Selection Engine

抽出処理はRuleによって制御する。

内部形式はJSON/YAMLを基本とする。

---

## Example Rule

```json
{
  "purpose": "movie",

  "date": {
    "start": "2014-01-01",
    "end": "2023-12-31"
  },

  "family_only": true,
  "count_per_year": 40,
  "include_video": true,
  "remove_duplicate": true
}
```

---

# 12. Output

## 12.1 Source

入力データは変更しない。

参照元写真はシンボリックリンクによる管理を可能とする。

---

## 12.2 Output

選択されたメディアは指定ディレクトリへコピーする。

例:

```
Output/

 Movie/
   2016/
   2017/

 PhotoFrame/
   2016/
   2017/
```

---

# 13. Usage Preset

用途別プリセットを提供する。

## Movie

目的:

記念動画素材

特徴:

- 年代均等
- 家族優先
- 笑顔優先
- 動画含む


## PhotoFrame

目的:

NASフォトフレーム表示

特徴:

- バリエーション重視
- 季節分散
- 重複削除
- 長時間表示向け

---

# 14. Natural Language Extension

将来的に自然言語入力をサポートする。

ただし抽出エンジンは自然言語に依存しない。

構成:

```
Natural Language

       ↓

Rule Generator

       ↓

JSON Rule

       ↓

Selection Engine
```

例:

入力:

```
過去10年の家族旅行写真を中心に
記念動画用の写真を選択して
```

変換:

```json
{
 "purpose":"movie",
 "family_only":true,
 "event":"travel"
}
```

---

# 15. Development Phases

## Phase 1

File Scanner

- ファイル検索
- SQLite登録
- ハッシュ管理


## Phase 2

Basic Metadata

- EXIF取得
- 撮影日時管理


## Phase 3

AI Analysis

- 顔検出
- 顔特徴量
- 品質評価


## Phase 4

Selection Engine

- Rule処理
- スコアリング
- 重複除外


## Phase 5

Output

- コピー処理
- ログ出力


## Phase 6

Natural Language Support

- Rule生成AI


---

# 16. Development Management

## Git Policy

仕様変更:

1. requirements.md変更
2. Git commit
3. 実装変更
4. Git commit


## Copilot Agent Development Policy

実装はPhase単位で行う。

一度に全機能を生成しない。

各Phase終了時に動作確認可能な状態を維持する。

---

# 17. Future Extensions

予定:

- 解析済み写真から顔候補を選ぶ機能
- イベント分類
- 人物別アルバム生成
- 自然言語検索
- 自動スライドショー生成(別プロジェクトで作成して連携)