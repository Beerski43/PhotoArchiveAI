# Phase 0 — 基盤整備（Issue #30）

状態: **実施中**

## 何のためにやるか

設計上の決めごとが、一過性の申し送りにしか存在しない状態だった。
コンテキストが切れると再現できず、同じ調査を毎回やり直すことになる。
**「実装を再開して」の一言で続きから進められる状態**を作るのがこのフェーズの目的。

あわせて、作業を始める前に潰さないと先に進めない問題が2つ見つかったので直す。

- **素の `pytest` が収集エラーで落ちていた。** `CLAUDE.md` も
  `docs/testing/TESTING.md` も `README.md` も、この落ちるコマンドを案内していた
- **`src/photoarchive_ai.egg-info/` が git 管理されていた。** 中身は 2026-08-02 で
  凍結しており、廃止済みの `analyzer.py` を列挙し、import してはいけない
  `face_recognition>=1.3` を依存として記載していた

## やること

### 1. ファイルの置き場所

`docs/` 直下を空にし、すべてディレクトリ配下へ移す（`git mv` で履歴を残す）。

| 移動前 | 移動後 |
|---|---|
| `docs/Specification.md` | `docs/spec/Specification.md` |
| `docs/TESTING.md` | `docs/testing/TESTING.md` |
| `docs/handoff/SESSION_HANDOFF_*.md` | `docs/history/details/<日付>-<題名>.md` |

`.gitignore` に `*.egg-info/` `build/` `dist/` `output/` `models/` を追加する。
`models/` は `face.py` がリポジトリ直下をモデルの探索先にするため、
`output/` は `app_settings.json` の `output_root` が相対パスを指すため。

### 2. 回帰テスト環境

- `pyproject.toml` に `pythonpath = ["."]`（収集エラーの修正）と
  `addopts = ["--strict-markers"]`
- `scripts/run_regression.sh`。PR 起票前に必ず実行し、最終行を PR 本文へ貼る
  - 実物の学習済みモデルが要る `-m models` は環境で結果が変わるので合否に含めない
  - entry point の破損はテストで捕まらないので、起動スモークを入れる
- テストが1件も無かった `converter.py` と、薄かった `selection.py` を埋める
- `cli._progress_started` のリセットフィクスチャ（テストの実行順に依存した差を消す）

### 3. 実装プランと作業履歴

- `docs/plan/ROADMAP.md` — フェーズ一覧と Issue 対応表
- `docs/history/WORKLOG.md` — 直近20件。新しいものが上
- `scripts/archive_worklog.py` — 20件を超えたぶんを年ごとに
  `docs/history/archive/WORKLOG-<年>.md` へ移す。冪等。`--check` は書き込まない
- **`CLAUDE.md` から、この3つを必ず参照させる**

### 4. 作業ルールの一本化

`CLAUDE.md` を正本にし、`.github/copilot-instructions.md` はポインタへ縮退させる。
これまで5節ぶんが逐語的に二重管理されており、片方だけ更新される事故が起きやすかった。

CLAUDE.md に足すもの:

- 作業開始時に読むものの先頭を ROADMAP → WORKLOG → 仕様書 に差し替え
- **「実装を再開して」と言われたときの手順**
- **ファイル配置規約の表**（どちらの文書にも無かった）
- フェーズと Issue の運用ルール、プラン改版のルール
- 回帰テストを PR 起票前に実行するルール

### 5. 仕様書 v1.0

[../spec/Specification.md](../spec/Specification.md) を全面改訂する。
各章に実装済み / 一部実装 / 未実装（将来拡張）を明記し、
CLI リファレンス・スキーマバージョンと移行・特徴量の生成規約・非機能要件を新設する。

## やらないこと

- Phase 1 以降の欠陥修正。場所は特定済みなので
  [phase-1-known-defects.md](phase-1-known-defects.md) に記録する
- DBスキーマの変更。実データが 70,297件あるため、`migrate` と
  `PRAGMA user_version` を伴わない変更はしない
- Issue の直接クローズ。解消した Issue（#9 / #22 / #23 / #25）は PR 本文の
  `Closes #N` に任せ、ROADMAP にも根拠を書く
- `data/` の掃除（`photoarchive.db.bak-pre28` 773MB、廃止済み `analyze_*.log` 13件）。
  実データのバックアップなので削除は利用者判断

## 完了の条件

1. `pytest -q` と `./scripts/run_regression.sh` が通る
2. **新しいセッションで `CLAUDE.md` → `ROADMAP.md` → `WORKLOG.md` の順に読むだけで、
   次にやることが特定できる**
3. 仕様書に書かれた機能が、実装済みか未実装かのどちらかに必ず分類されている
