# #30 作業ルール・実装プラン・回帰テスト環境の整備（2026-09-19）

対象 Issue: #30。ブランチ `feature/#30_rework-project-rules-and-plan`（`develop` から分岐）。
方針は [Issue #30 のコメント](https://github.com/Beerski43/PhotoArchiveAI/issues/30) に起票済み。

---

## 1. 調査で見つかったこと

### 素の `pytest` が落ちていた

```
tests/test_scanner_incremental.py:5: ModuleNotFoundError: No module named 'tests'
Interrupted: 2 errors during collection
```

`tests/helpers.py` を `from tests.helpers import ...` で読むのに、リポジトリルートが
`sys.path` に入っていなかった。`python -m pytest` は CWD を `sys.path` 先頭に入れる
ので偶然通っていただけで、**`CLAUDE.md` / `docs/TESTING.md` / `README.md` の3つが
そろって、この落ちるコマンドを案内していた。**

`pyproject.toml` に `pythonpath = ["."]` を足して解決。

### `src/photoarchive_ai.egg-info/` が git 管理されていた

setuptools のビルド生成物。中身は 2026-08-02 で凍結しており、

- `SOURCES.txt` が**廃止済みの `analyzer.py`** を列挙
- `requires.txt` が**import してはいけない `face_recognition>=1.3`** を要求

`.venv` 側は `__editable__.photoarchive_ai-0.1.0.pth` が import を担っているので、
git 管理から外しても動作に影響しない。

### 作業ルールが二重管理されていた

`CLAUDE.md`（156行）と `.github/copilot-instructions.md`（60行）が5節ぶん逐語的に
同じ内容を持ち、互いを参照し合っていた。**どちらの文書構成表にも `config/` `data/`
`tests/` `src/` の配置規約が無く**、Issue #30 が求める「ファイルの置き場所」は
どこにも書かれていなかった。

### 仕様書に CLI の章が無かった

`init-db` / `migrate` / `convert-heic` とすべてのオプションが一行も書かれておらず、
README が事実上の唯一の情報源だった。ほかに、§2.4 と §3.4 の完全重複、
§7.3 と §7.4 の順序逆転、存在しない `face_registration_gui.py` の案内があった。

---

## 2. 修正した欠陥

| # | 症状 | 直し方 |
|---|---|---|
| A1 | **更新時刻だけ変わったファイルが恒久的に再ハッシュされる** | `save_media` はハッシュが変わったときしか行を更新しないため、touch しただけのファイルは `created_time` が書き戻されず、毎回「変わったかもしれない」と判断されていた。ハッシュが同じでもファイル属性は書き戻し、検出状態には触らないようにした |
| A2 | `convert-heic` が DB パスを要求して落ちる | DB を使わないサブコマンドでは DB パスの解決を要求しない |
| A3 | **dlib モデルが読めないと顔が無言で全損する** | 特徴量が全件 NULL のまま「スキャン済み」になり、`--force-rescan` なしでは二度と回収されなかった。`scan` の開始時に確かめて中断する |
| A4 | GUI だけ設定ファイルを読まず `--db` が必須 | CLI と同じ解決順にした |
| A5 | 設定の探索がカレントディレクトリ依存 | 環境変数 → CWD → リポジトリ直下 の順に探す |
| A6 | `match --dry-run` が2回目以降ほぼ空振り | 自動割り当てを取り消さないのに候補を未割当だけに絞っていた。dry-run では `auto` の顔も候補に含める |
| A7 | `detection_score` が全行 NULL | MediaPipe の `score` を拾って保存する |
| A8 | `Error: none` が直近のエラーを塗り潰す（#25） | 直近のエラーを保持する |
| A9 | 割り当て済み一覧にページャが無く201件目以降に到達できない | `offset` ページングを入れた |
| A10 | 一度入れた年齢を「未設定」へ戻せない | `assign_faces` の `age` 既定値を `KEEP_AGE` 番兵にして、「触らない」と「未設定にする」を区別した |

### 並列テストで表面化した、もう1件

`face.get_latest_error()` は大域変数で、1ファイルごとに消していなかった。
**前のファイルのエラーが次のファイルの結果として報告される。** 並列実行では
fork 時の値が全ワーカーに複製されるため、無関係なファイルに付く。

`workers=2` のテストが単体では通るのに全体実行だと落ちる、という形で見つかった。
`analyze_file` の先頭で `clear_latest_error()` を呼ぶようにした。

---

## 3. 決めた前提

- **DBスキーマは変更していない。** 実データ 70,297件があるため、`migrate` と
  `PRAGMA user_version` を伴わない変更は避けた。A1〜A10 はすべてスキーマ非破壊
- **`Person.name` の UNIQUE 制約は入れていない。** 既存DBに同名人物がいると
  移行が必要になり、スキーマ版の話になる。ROADMAP の Phase 3 で判断する
- **`data/` の掃除はしていない。** `photoarchive.db.bak-pre28`（773MB）と
  廃止済みの `analyze_*.log` 13件（8.5MB）が残っているが、実データの
  バックアップなので削除は利用者判断
- **`-m models` は回帰テストの合否に含めない。** 実物の学習済みモデルが要り、
  無い環境では丸ごとスキップされる。環境で結果が変わるものを必須にすると、
  「PR 前に必ず実行する」というルールが守れなくなる

---

## 4. テスト

40件 → 110件。wall clock 2〜3秒。

| ファイル | 増えたもの |
|---|---|
| `test_converter.py` | **新規10件。**それまでテストが1件も無かった |
| `test_config.py` | **新規8件。**設定の探索順 |
| `test_cli_commands.py` | **新規7件。**サブコマンドの配線 |
| `test_worklog_archive.py` | **新規10件。**作業履歴の切り出し |
| `test_selection.py` | 2件 → 16件。`count_per_year`、`remove_duplicate`、コピーの挙動 |
| `test_scanner_incremental.py` | 7件 → 15件。A1・A3・A7、**並列経路**、エラーの持ち越し |
| `test_gui_assignment.py` | 6件 → 11件。A9・A10 |
| `test_matcher.py` | 8件 → 11件。A6、進捗の分母 |
| `test_cli_progress.py` | 2件 → 7件。A8 |

`tests/conftest.py` に2つのフィクスチャを足した。

- `isolate_app_settings`: 開発機の `config/app_settings.json` をテストから
  見えなくする。A5 でリポジトリ直下を探索先に足したため、素のテストが実機の設定
  （NFS 上の `source_root`）を読みうる状態になっていた
- `reset_cli_progress_state`: 進捗表示の大域状態を戻す。テストの実行順に依存した
  差が出ていた

---

## 5. まだ確認していないこと

- **実データでの動作確認。** A1・A2・A3・A6 は実データでしか効果を確かめられない。
  手順は [../../testing/TESTING.md](../../testing/TESTING.md) の §6 に書いた
- **全件スキャン**（未実施のまま）
- **`match` の取りこぼし率。** これまで測れているのは誤一致率だけ。
  ROADMAP の Phase 3
