# テスト項目一覧

全 136 件 / 14 ファイル。実行時間は wall clock で 2〜3 秒。
実行方法は [TESTING.md](TESTING.md)。

`models` マーカーの3件だけが実物の学習済みモデルを必要とし、
無い環境では自動的にスキップされる。**回帰テスト
（`./scripts/run_regression.sh`）はこの3件を合否に含めない。**

---

## 過去のバグを閉じ込めているテスト

**消したり緩めたりしないこと。** どれも実際に起きた不具合で、原因と再発条件が
はっきりしている。

| テスト | 何を防いでいるか |
|---|---|
| `test_matcher.py::test_match_assigns_the_correct_person_with_uneven_teacher_counts` | 手本の数で剰余を取って人物を引いていたため、1人に2枚以上割り当てた時点で結果が壊れた |
| `test_matcher.py::test_match_leaves_distant_faces_unassigned` | 閾値判定が無く、どんなに遠い顔も必ず誰かに割り当てられた（Issue #22） |
| `test_matcher.py::test_dry_run_is_not_blinded_by_a_previous_match` | `--dry-run` が2回目以降ほぼ空振りし、閾値を決める目安にならなかった |
| `test_db.py::test_saving_scores_does_not_clear_family_score` | `INSERT OR REPLACE` で `scan` が `match` の書いた値を消していた |
| `test_gui_assignment.py::test_face_age_dialog_keeps_zero_distinct_from_unset` | `value() or None` で0歳が「未設定」に潰れた |
| `test_gui_assignment.py::test_an_age_can_be_cleared_back_to_unset` | 一度入れた年齢を未設定へ戻せなかった |
| `test_gui_assignment.py::test_registered_faces_dialog_pages_through_every_assigned_face` | 割り当て済み一覧にページャが無く、201件目以降に到達できなかった |
| `test_scanner_incremental.py::test_scan_skips_hash_and_faces_on_second_run` | 2回目のスキャンが差分にならなかった（Issue #9） |
| `test_scanner_incremental.py::test_touching_a_file_does_not_make_every_later_scan_read_it_again` | 更新時刻だけ変わったファイルが恒久的に再ハッシュされ、441GB を毎回読み直した |
| `test_scanner_incremental.py::test_scan_stops_when_the_embedding_model_cannot_be_loaded` | モデルが読めないと特徴量が全件 NULL のまま「スキャン済み」になり、無言で全損した |
| `test_scanner_incremental.py::test_an_error_from_one_file_is_not_reported_for_the_next` | 直近のエラーが大域変数に残り、無関係なファイルに付いた |
| `test_cli_progress.py::test_progress_keeps_the_last_error_instead_of_overwriting_it` | `Error: none` が直近のエラーを塗り潰した（Issue #25） |
| `test_cli_commands.py::test_convert_heic_does_not_need_a_database` | DB を使わないコマンドが DB パスを要求して落ちた |
| `test_scanner_incremental.py::test_faces_stored_without_embeddings_are_picked_up_once_the_model_returns` | `--allow-missing-embeddings` で入れた顔が、モデル設置後も回収されなかった |
| `test_pytest_summary.py::test_the_counts_survive_the_colours_pytest_adds_on_a_terminal` | **回帰テストの集計行が、端末で実行すると必ず `0 passed / 0 failed` になっていた**（しかも成功に見えた） |
| `test_pytest_summary.py::test_output_that_cannot_be_read_is_not_reported_as_zero` | 集計できないときに 0 を出して成功に見せた |
| `test_worklog_archive.py::test_a_section_that_is_not_a_dated_entry_survives` | 日付エントリ以外の節を黙って消した（毎コミット実行するスクリプト） |
| `test_face_real.py::test_model_directory_is_resolved_without_pkg_resources` | `face_recognition_models` を import すると setuptools 81 以降で落ちた（Issue #10） |
| `test_system.py::test_scan_is_incremental_on_second_run` | 上と同じ差分スキャンを、CLI の通し実行で確認する |

---

## ファイル別

### `test_db.py` — スキーマと永続化（6件）

| テスト | 内容 |
|---|---|
| `test_database_schema_and_media_crud` | メディアの登録と取得。未スキャンは `face_count IS NULL` |
| `test_save_media_resets_scan_state_when_hash_changes` | ハッシュが変われば検出状態をリセットする |
| `test_person_crud_and_face_assignment` | 人物のCRUD。人物を消しても顔は残り未割当に戻る |
| `test_deleting_media_cascades_to_faces_and_results` | 外部キーの CASCADE |
| `test_saving_scores_does_not_clear_family_score` | `scan` と `match` が互いのスコアを潰さない |
| `test_load_manual_embeddings_pairs_vectors_with_person_ids` | 手本に使うのは手動割り当てだけ |

### `test_scanner_incremental.py` — 走査と差分判定（17件）

| テスト | 内容 |
|---|---|
| `test_scan_records_face_count` | NULL / 0 / N の3状態を記録する |
| `test_scan_skips_hash_and_faces_on_second_run` | 2回目はハッシュも顔検出も走らない |
| `test_scan_rescans_when_file_content_changes` | 中身が変われば作り直し、顔行は重複しない |
| `test_scan_removes_media_whose_file_disappeared` | 消えたファイルの行を削除する |
| `test_scan_aborts_when_too_many_files_are_missing` | 20% を超える削除で中断。`--force-prune` で続行 |
| `test_scan_aborts_when_source_has_no_media` | メディア0件でも中断する |
| `test_scan_can_skip_prune` | `--no-prune` |
| `test_touching_a_file_does_not_make_every_later_scan_read_it_again` | 更新時刻だけの変化で再ハッシュが恒久化しない |
| `test_faces_survive_a_scan_that_only_sees_a_new_timestamp` | そのとき割り当て済みの顔が消えない |
| `test_scan_records_the_detector_confidence` | `detection_score` を保存する |
| `test_scan_stops_when_the_embedding_model_cannot_be_loaded` | モデル不在で中断。`--allow-missing-embeddings` で続行 |
| `test_scanning_with_several_workers_gives_the_same_result` | **並列経路**（実運用の既定） |
| `test_a_parallel_rescan_is_still_incremental` | 並列でも差分になり、顔行が重複しない |
| `test_an_error_from_one_file_is_not_reported_for_the_next` | エラーの持ち越しが起きない |
| `test_media_type_is_image_or_video` | `Media.type` の値域 |
| `test_faces_stored_without_embeddings_are_picked_up_once_the_model_returns` | `--allow-missing-embeddings` の顔を、モデル設置後の通常 `scan` が拾い直す |
| `test_a_photo_whose_faces_are_all_too_small_is_still_marked_scanned` | 顔が小さすぎて特徴量が作れないのは正常な結果。毎回読み直さない |

### `test_matcher.py` — 自動割り当て（11件）

| テスト | 内容 |
|---|---|
| `test_match_assigns_the_correct_person_with_uneven_teacher_counts` | 手本の枚数が人物ごとに違っても正しく引く |
| `test_match_leaves_distant_faces_unassigned` | 閾値を超える顔は未割当のまま |
| `test_match_rejects_ambiguous_faces` | 2位との差がマージン未満なら割り当てない |
| `test_match_does_not_learn_from_auto_or_rejected_faces` | 手本は手動割り当てだけ |
| `test_match_is_idempotent_and_reset_keeps_manual` | 何度実行しても同じ結果。手動は消さない |
| `test_match_updates_family_score` | `family_score` を付け直す |
| `test_match_without_teachers_does_nothing` | 手本0件なら何も書かない |
| `test_match_dry_run_does_not_write` | `--dry-run` は書き込まない |
| `test_dry_run_is_not_blinded_by_a_previous_match` | 2回目以降の dry-run が空振りしない |
| `test_dry_run_still_writes_nothing_after_a_real_match` | 上の変更で書き込みが起きていない |
| `test_progress_reaches_the_end_even_when_some_faces_have_no_embedding` | 進捗の分母が実際の候補数と合う |

### `test_gui_assignment.py` — GUI での割り当て（11件）

| テスト | 内容 |
|---|---|
| `test_unassigned_faces_are_listed` | 未割当の顔が並ぶ |
| `test_face_list_is_paged` | ページ単位で読む |
| `test_assign_and_unassign_faces` | 割り当てと解除 |
| `test_reject_faces_removes_them_from_the_unassigned_list` | 除外 |
| `test_deleting_person_returns_faces_to_the_unassigned_list` | 人物削除で顔は未割当に戻る |
| `test_face_age_dialog_keeps_zero_distinct_from_unset` | 0歳と未設定を区別する |
| `test_registered_faces_dialog_pages_through_every_assigned_face` | 割り当て済み一覧のページャ |
| `test_the_age_filter_returns_to_the_first_page` | 絞り込みで1ページ目に戻る |
| `test_assigning_without_an_age_keeps_the_one_already_recorded` | 年齢を指定しない割り当ては年齢を触らない |
| `test_an_age_can_be_cleared_back_to_unset` | 年齢を未設定へ戻せる |
| `test_zero_is_stored_as_zero_and_not_as_unset` | 0歳は0歳として保存される |

### `test_selection.py` — 抽出とコピー（16件）

| テスト | 内容 |
|---|---|
| `test_select_media_filters_by_rule` | 日付と `include_video` |
| `test_load_rule_reads_json` / `test_load_rule_reads_yaml` | ルールの読み込み（拡張子で分岐） |
| `test_load_rule_raises_for_a_missing_file` | 無いファイル |
| `test_family_only_keeps_media_with_a_family_score` | `family_only` |
| `test_results_are_ordered_by_family_then_quality_then_smile` | 並び順 |
| `test_count_per_year_limits_each_year_independently` | 年ごとの上限 |
| `test_count_per_year_keeps_the_best_of_each_year` | 年内では上位から採る |
| `test_remove_duplicate_keeps_one_row_per_file_hash` | ハッシュ一致の重複除去 |
| `test_duplicate_groups_fall_back_to_the_path_when_there_is_no_hash` | ハッシュが無いときはパスで分ける |
| `test_media_year_prefers_the_shooting_date_and_falls_back_to_created_time` | 年の決め方 |
| `test_date_filter_falls_back_to_created_time_and_drops_unreadable_dates` | 読めない日付は範囲外 |
| `test_copy_keeps_the_layout_below_the_source_root` | 相対パスを再現する |
| `test_copy_flattens_media_that_lives_outside_the_source_root` | 基準の外はファイル名だけにする |
| `test_copy_skips_entries_without_a_path_and_reports_progress` | パスが無い行を飛ばす |
| `test_copy_makes_room_when_the_name_is_taken` | 名前の衝突で連番を付ける |

### `test_converter.py` — HEIC → JPEG（10件）

| テスト | 内容 |
|---|---|
| `test_convert_writes_jpeg_beside_the_source_and_keeps_the_original` | 元ファイルを変更しない |
| `test_convert_finds_sources_recursively_and_ignores_other_extensions` | 再帰探索と対象外の無視 |
| `test_convert_skips_when_an_identical_jpeg_already_exists` | 2回目の実行が無害 |
| `test_convert_numbers_the_output_when_a_different_jpeg_exists` | 別の写真なら連番 |
| `test_convert_reports_progress_for_every_source` | 進捗 |
| `test_convert_raises_for_a_missing_directory` | 無いディレクトリ |
| `test_convert_asks_before_continuing_when_the_output_cannot_be_written` | 書き込み失敗時の確認 |
| `test_fingerprint_matches_the_same_picture_and_differs_for_another` | 同一画像の判定 |
| `test_same_image_is_false_when_a_file_cannot_be_read` | 読めないファイル |
| `test_next_output_path_walks_past_occupied_numbers` | 空いている連番を探す |

### `test_cli_commands.py` — サブコマンドの配線（7件）

| テスト | 内容 |
|---|---|
| `test_convert_heic_does_not_need_a_database` | DB を使わないコマンドは DB パスを要求しない |
| `test_commands_that_need_a_database_still_say_so` | 使うコマンドはこれまで通り要求する |
| `test_init_db_creates_a_usable_database` | `init-db` |
| `test_migrate_reports_that_a_fresh_database_is_current` | `migrate` は移行済みDBに何もしない |
| `test_scan_arguments_reach_the_scanner` | `scan` の全オプションが下へ届く |
| `test_match_arguments_reach_the_matcher` | `match` の全オプションが下へ届く |
| `test_the_database_path_falls_back_to_the_settings_file` | 設定ファイルへのフォールバック |

### `test_config.py` — 設定の探索（11件）

環境変数 → カレントディレクトリ → リポジトリ直下 の順に探すこと、優先順位、
壊れたファイルでもコマンドが止まらないこと。リポジトリ直下の候補を
editable install のときだけ出すこと（通常のインストールでは `REPO_ROOT` が
`lib/python3.x` を指すので、**`REPO_ROOT` 自身を見ても判別できない。
モジュールの位置で判断する**）。同じ場所を2度並べないこと。

### `test_cli_progress.py` — 進捗表示（7件）

2行の書き換え、直近のエラーの保持、長いエラーの切り詰め、改行の潰し。

### `test_migration.py` — 旧スキーマからの移行（4件）

Media と Person を温存し Face と AnalysisResult を破棄すること、冪等性、
旧スキーマのまま使おうとしたときのエラー、特徴量 BLOB の往復。

### `test_worklog_archive.py` — 作業履歴の切り出し（16件）

直近20件を残して年ごとに切り出すこと、索引の作り直し、冪等性、`--check`。
**日付エントリ以外の節を消さないこと**（前にあるものは前書きとしてその場に残し、
あとにあるものは末尾へ移す）。切り出し済みの本文を後から書き換えないこと。

### `test_pytest_summary.py` — 回帰テストの集計行（9件）

CLAUDE.md §5 で「最終行を PR 本文に貼る」ことを必須にした行。着色された
pytest 出力から件数と所要時間を読めること、`0 passed / 0 failed` を
成功のように見せないこと。

### `test_system.py` — 通し（2件、`system` マーカー）

`init-db` → `scan` → GUIでの割り当て → `match` → `select` を一通り流す。
2回目のスキャンが差分になることも確認する。

### `test_face_real.py` — 実物のモデル（3件、`models` マーカー）

`face_recognition_models` を import せずにモデルのパスを取り出せること、
実物の dlib が128次元を返すこと、矩形の正方形化とパディング。

---

## テストの作り

**外部依存を持たない。** ネットワーク、NFS、実データベース、実物のモデル
（`models` マーカーを除く）のいずれにも触らない。すべて `tmp_path` の中で完結する。

`tests/conftest.py` が次を差し替える。

| 差し替えるもの | 何になるか |
|---|---|
| `mediapipe` | 顔検出のフェイク。真っ黒な画像は「顔なし」、それ以外は「顔が1つ」。**実装が先に見る `relative_bounding_box` を返す**ので、実際の検出経路をそのまま通る |
| `dlib` | 顔領域の平均色から決まる128次元ベクトル。同じ色の顔は近く、違う色の顔は遠くなるので、`match` の判定を検証できる |
| `config.REPO_ROOT` | 空のディレクトリ。開発機の `config/app_settings.json` をテストから見えなくする |
| `cli._progress_started` | 各テストの前後でリセット。進捗表示の大域状態がテストの順序に依存した差を作らないようにする |
| `QMessageBox` | 何もしない。モーダルで止まらないようにする |

`tests/helpers.py` の `write_image()` に色を指定すると「同一人物」「別人」を
作り分けられる。`write_heic()` は HEIC を書き出す（encoder が無い環境ではスキップ）。

GUI のテストは `QT_QPA_PLATFORM=offscreen` で動くので、画面が無くても実行できる。
