# PhotoArchiveAI 作業ルール

**作業ルールの正本はリポジトリ直下の [`CLAUDE.md`](../CLAUDE.md)。**
このリポジトリでコード・設定・テスト・ドキュメントを変更する場合は、
**必ず `CLAUDE.md` を読むこと。** 内容が食い違った場合は `CLAUDE.md` を優先し、
食い違い自体を申し送りに残す。

`CLAUDE.md` には、以下がまとまっている。

- 作業開始時に読むものと、「実装を再開して」と言われたときの手順
- 進め方（Issue → 方針の起票 → ブランチ → 実装 → 作業履歴 → 回帰テスト → PR）
- 実装の判断基準、ファイルの置き場所
- 処理の流れ、モジュール構成、DBの要点、踏みやすい落とし穴

## 要約（詳細は CLAUDE.md）

1. **作業の単位は Issue。** 実装前に方針を Issue へコメントとして起票する。
2. **ブランチは `develop` から** `feature/#<Issue番号>_<英語のケバブケース>`。
   ブランチ名に日本語を含めない。
3. コミットメッセージの先頭は `#<Issue番号> `。
4. `docs/history/WORKLOG.md` に作業履歴を追記し、
   `python scripts/archive_worklog.py` を実行する。
5. **PR を起票する前に `./scripts/run_regression.sh` を必ず実行し、**
   最終行を PR 本文に貼る。落ちた状態で起票しない。
6. **PR（ベースは `develop`）を起票したら、そこで止まる。**
   レビューはリポジトリの持ち主が行う。マージ、`develop` / `main` への直接 push、
   Issue の直接クローズは行わない。解消した Issue は PR 本文の `Closes #N` に任せる。

## 文書の在り処

索引は [`docs/README.md`](../docs/README.md)。

- `docs/plan/ROADMAP.md`: 実装プラン（今どのフェーズか）
- `docs/history/WORKLOG.md`: 作業履歴（先頭が最新）
- `docs/spec/Specification.md`: 要件・仕様・DB設計・CLIリファレンス
- `docs/testing/`: テストの実行手順とテスト項目一覧
- `docs/operation/`: GUIなどの操作手順
- `README.md`: セットアップ、依存関係、基本操作
