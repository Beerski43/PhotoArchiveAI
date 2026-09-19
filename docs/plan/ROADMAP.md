# 実装プラン

完成までの道筋。**この文書は改版される前提**で、レビューや通常の対話で方針が
変わったら、その作業の中で書き換える（書き換えたら
[../history/WORKLOG.md](../history/WORKLOG.md) に1行残す）。

## 使い方

- **新しいフェーズに入るときに Issue を起票し、下の表に番号を書く。**
  フェーズ全部の Issue を先に作らない
- 着手中・着手済みのフェーズだけ `phase-N-*.md` に詳細を書く。
  未着手フェーズの詳細は陳腐化するので書かない
- 1つのフェーズが1つの PR とは限らない。Issue の粒度で分ける

## 現在地

**Phase 0 と Phase 1 を Issue #30 で実施中。** Phase 2 以降は未着手。

| フェーズ | 状態 | 内容 | Issue |
|---|---|---|---|
| [Phase 0 基盤整備](phase-0-foundation.md) | 実施中 | ファイル配置・作業ルール・実装プラン・作業履歴・回帰テスト環境・仕様書 v1.0 | #30 |
| [Phase 1 既知の欠陥修正](phase-1-known-defects.md) | 実施中 | 実データ運用を壊す欠陥を潰す。全件スキャンの前提 | #30（#25 を含む） |
| Phase 2 テストの穴埋め | 未着手 | 並列スキャン・face・scoring・cli・gui の未検証パス | 未起票 |
| Phase 3 実データでの精度確立 | 未着手 | 全件スキャン → 手動割り当て → `match` の取りこぼし率の実測と閾値の確定 | 未起票 |
| Phase 4 GUI の作り込み | 未着手 | 人物ごとの枚数表示、顔候補の推薦ほか | #18 |
| Phase 5 設定と入力の整理 | 未着手 | 設定の YAML 化、ソースディレクトリの複数指定、HEIC の扱いの決着 | #27, #24, #26 |
| Phase 6 選択エンジンの完成 | 未着手 | 画質評価の拡充、類似・連写の重複検出、用途プリセット、出力構造 | 未起票 |
| Phase 7 性能 | 未着手 | GPU 対応 | #8 |
| Phase 8 将来拡張 | 未着手 | 自然言語からのルール生成 | 未起票 |

**フェーズの順序には理由がある。** Phase 1 の欠陥（特に mtime だけ変わった
ファイルの再ハッシュ）を残したまま Phase 3 の全件スキャンに進むと、441GB を
NFS 24MB/s で何度も読み直すことになる。Phase 3 が終わるまで、`match` の精度は
「誤一致率しか測っていない」状態である点にも注意。

## 各フェーズの中身

### Phase 0 — 基盤整備（#30）

詳細は [phase-0-foundation.md](phase-0-foundation.md)。

### Phase 1 — 既知の欠陥修正（#30）

詳細は [phase-1-known-defects.md](phase-1-known-defects.md)。
Phase 0 の調査で場所まで特定済みの欠陥が10件ある。Issue #25 もここで解消する。
Phase 0 と同じ Issue で進めるが、コミットは分ける。

### Phase 2 — テストの穴埋め

Phase 1 までで守られていない主要パスを埋める。全体の実行時間は10秒以内に収める。

- **`scanner.py` の並列経路。**`workers > 1` が一度も実行されていない。
  実運用の既定は `min(4, cpu_count - 1)` なので、既定の経路が無検査という状態
- `face.py`: `read_rgb`（動画の先頭1フレーム、読めないファイル）、
  `detect_faces` のリサイズ、`crop_face`、`make_thumbnail`
- `scoring.py`: フェイクの FaceMesh が常に同じ landmark を返すため、
  `estimate_smile_score` と `estimate_quality` の計算式が実質未検証
- `cli.py`: `migrate` と `convert-heic` サブコマンド、引数の配線
- `gui.py`: `RegisteredFacesDialog`、`_show_preview`、`_edit_person`
- `scanner.get_media_type` が `"image"` を返すのに一部テストが `"photo"` 前提、
  というねじれもここで揃える

### Phase 3 — 実データでの精度確立

**この製品の価値がまだ実証されていない工程。** 手本が0件のままなので、
`match` が実際に役に立つかは未確認。

1. 全件スキャン（tmux 推奨。中断しても続きから再開できる）
2. GUI で人物ごとに20〜50枚を割り当てる
3. `match --dry-run` で距離の分布を見てから `match`
4. **取りこぼし率を測る。** これまで測ったのは誤一致率だけで、本来紐づくべき顔を
   落とす率は未計測。0.4 で取りこぼしが多すぎるなら 0.45 へ緩める
5. `selection.py` の `family_only` の決着。閾値 0.4 で切ると `assign_score` の下限が
   33.3 になるため、`family_score > 0.0` が「割り当てが1つでもあれば通る」に
   なっている
6. `Person.name` の UNIQUE 制約の要否（入れるなら既存DBの移行が必要）

### Phase 4 — GUI の作り込み（#18）

**#23（顔登録の削除機能）は #28 と #30 で解消済み。** 「割り当て済みを確認」で
解除・除外・年齢変更（未設定へ戻すことも含む）・自動割り当ての確定ができ、
ページャも入った。

残るのは #18（GUI改善）。仕様書 §10.7 の将来拡張（顔候補の自動推薦、
類似人物の統合、人物ごとの写真枚数表示など）から、必要なものを選んで着手する。

### Phase 5 — 設定と入力の整理（#27, #24, #26）

- **#27（json → yml）: ルールファイルは既に YAML 対応済み**（`selection.py` が
  拡張子で分岐する）。残る作業はアプリ設定側（`config.py` が `json.load` 直呼び）だけ
- #24（ソースディレクトリの複数指定）: 配列で指定する
- #26（scan 以降から HEIC を外す）: `face.py` が `register_heif_opener()` を
  呼ぶので HEIC も直接読める。ただし `convert-heic` の後は `.heic` と `.jpg` が
  両方登録され、**同じ顔が二重に検出される**。`IMAGE_EXTENSIONS` から外すか、
  変換後は元を除外するかを決める

### Phase 6 — 選択エンジンの完成

仕様書にあって実装が追いついていないもの。

- 画質評価の拡充。現状は明るさと顔サイズ比の2要素のみで、
  ピント・ブレ・白飛び・目閉じは未実装
- **類似・連写の重複検出。** 現状の `remove_duplicate` は `file_hash` の完全一致
  だけで、仕様書の例（連写から代表1枚）は成立しない
- 用途プリセット（Movie / PhotoFrame）と `purpose` キー。現状コードに存在しない
- 出力構造（`Output/Movie/2016/`）とシンボリックリンク出力

### Phase 7 — 性能（#8）

GPU 優先・CPU フォールバック。顔検出だけ GPU 対応、特徴量は CPU のままという
ハイブリッドから始めるのが現実的。

### Phase 8 — 将来拡張

自然言語からのルール生成。抽出エンジン自体は自然言語に依存させない。

## 解消済みの Issue

PR #31（Issue #30）のマージで、次の Issue がクローズされる。

| Issue | 何で解消したか | 回帰テスト |
|---|---|---|
| #9 scan が差分検索になっていない | #28 と #30 | `test_scanner_incremental.py::test_scan_skips_hash_and_faces_on_second_run`、`::test_touching_a_file_does_not_make_every_later_scan_read_it_again` |
| #22 scan が勝手に person_id を入れる | #28 | `test_matcher.py::test_match_leaves_distant_faces_unassigned` |
| #23 GUI に顔登録削除機能追加 | #28 と #30 | `test_gui_assignment.py::test_an_age_can_be_cleared_back_to_unset`、`::test_registered_faces_dialog_pages_through_every_assigned_face` |
| #25 `Error: none` 表示削除 | #30 | `test_cli_progress.py::test_progress_keeps_the_last_error_instead_of_overwriting_it` |

**#23 は解釈が入っている。** 「削除」を「人物への登録を外す」と読んだ。`Face` の行
そのものを消す機能という意図なら、再オープンして Phase 4 で扱う。

**Issue のクローズは、PR 本文の `Closes #N` に任せる。** エージェントが直接
閉じない（このリポジトリは `develop` が既定ブランチなので、`develop` への
マージで発火する）。
