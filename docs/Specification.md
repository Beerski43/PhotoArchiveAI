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

## 3.1 Analysis and Extraction Separation

解析処理と抽出処理は完全に分離する。

解析:

```
写真
 ↓
AI解析
 ↓
解析結果保存
```

抽出:

```
解析結果DB
 ↓
抽出条件
 ↓
コピー
```

一度解析した結果は再利用する。

---

## 3.2 Persistent Analysis Data

解析結果は一時データではなく永続データとして管理する。

保存先:

SQLite Database

再解析条件:

- 新規ファイル
- ファイルハッシュ変更
- AIモデルバージョン変更

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
Media Scanner
        |
        v
AI Analysis Engine
        |
        v
SQLite Database
        |
        v
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
- ハッシュ生成

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
- analyzed_date
- analyzer_version


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


## 6.3 Face Embedding Table

顔特徴量保存。

項目:

- id
- media_id
- person_id
- embedding
- similarity_score


## 6.4 Analysis Result Table

項目:

- media_id
- face_count
- family_score
- smile_score
- quality_score
- duplicate_group
- event_category


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

```
写真
 |
 v
顔検出
 |
 v
顔特徴量生成
 |
 v
登録人物との比較
 |
 v
人物判定
```

---

## 7.3 Age Change Handling

子供など長期間の成長を考慮する。

同一人物について複数年代の顔特徴量を保持可能にする。

例:

```
Child A

0歳 embedding
5歳 embedding
10歳 embedding
15歳 embedding
```

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
- 人物削除
- 顔画像追加
- 顔画像削除
- 登録済み顔一覧表示

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

## 8.4 Face Image Registration

人物ごとに複数枚の顔画像を登録できる。

推奨枚数

5〜20枚

推奨条件

- 正面
- 左右方向
- 笑顔
- 真顔
- 明るい写真
- 年齢の異なる写真

GUIから写真を選択すると、自動的に

- 顔検出
- 顔切り出し
- Face Embedding生成

まで実行する。

登録された顔画像はデータベースへ保存する。

---

## 8.5 Automatic Face Detection

GUIでは元画像を表示する。

画像中に顔が複数ある場合は

```
□□□□
□顔①□

        □顔②□
```

のように矩形表示する。

利用者は登録する顔をクリックして選択する。

---

## 8.6 Face Preview

登録済み人物を選択すると

```
父

[顔]
[顔]
[顔]
[顔]
```

のようにサムネイル表示する。

顔画像をクリックすると

- 削除
- 再登録

が可能。

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

年齢入力は必須ではない。

撮影日時(EXIF)から推定可能な場合は自動入力する。

---

## 8.8 Database

GUIは以下のテーブルのみ更新する。

- Person
- FaceEmbedding

MediaおよびAnalysisResultは更新しない。

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