# docs

| 知りたいこと | 読むもの |
|---|---|
| **今どこまで進んでいて、次に何をやるか** | [plan/ROADMAP.md](plan/ROADMAP.md) |
| **直近に何をやったか** | [history/WORKLOG.md](history/WORKLOG.md)（先頭が最新） |
| 何を作ろうとしているか（要件・仕様・DB設計・CLI） | [spec/Specification.md](spec/Specification.md) |
| テストの動かし方 | [testing/TESTING.md](testing/TESTING.md) |
| 何がテストで守られているか | [testing/TEST_CASES.md](testing/TEST_CASES.md) |
| GUI の使い方 | [operation/GUI_USAGE.md](operation/GUI_USAGE.md) |
| 過去のセッションの詳しい記録 | [history/details/](history/details/) |
| セットアップと基本操作 | [../README.md](../README.md) |
| 作業のルール（AIエージェント向け） | [../CLAUDE.md](../CLAUDE.md) |

## 置き場所の決まり

| ディレクトリ | 何を置くか |
|---|---|
| `spec/` | 要件・仕様・DB設計。**実装と食い違ったら、どちらかを直して食い違いを残さない** |
| `plan/` | 実装プラン。`ROADMAP.md` が全体、`phase-N-*.md` は着手済みフェーズの詳細 |
| `history/` | 作業履歴。`WORKLOG.md` は直近20件、溢れたぶんは `archive/` へ年ごとに移る |
| `history/details/` | 1セッション1ファイルの詳しい記録。`<日付>-<英語のケバブケース>.md` |
| `testing/` | テスト環境・実行手順・テスト項目一覧 |
| `operation/` | 利用者向けの操作手順 |

`history/details/` が50件を超えたら、年ごとのディレクトリに分ける。

`WORKLOG.md` へ追記したら `python scripts/archive_worklog.py` を実行すること。
切り出しが必要かどうかは `python scripts/archive_worklog.py --check` で分かる
（`scripts/run_regression.sh` の最後でも通知する）。
