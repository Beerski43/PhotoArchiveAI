# テスト項目一覧

**何がテストで守られているか**の一覧。実行方法は [TESTING.md](TESTING.md)。

いま何件あるかはここには書かない（数えるたびに更新が要るため）。
`pytest -q --collect-only | tail -1` で分かる。

`models` マーカーのテストだけが実物の学習済みモデルを必要とし、
無い環境では自動的にスキップされる。**回帰テスト
（`./scripts/run_regression.sh`）はこれらを合否に含めない。**

---

## 過去のバグを閉じ込めているテスト

**消したり緩めたりしないこと。** どれも実際に起きた不具合で、原因と再発条件が
はっきりしている。

| テスト | 何を防いでいるか |
|---|---|
| `test_matcher.py::test_match_assigns_the_correct_person_with_uneven_teacher_counts` | 手本の数で剰余を取って人物を引いていたため、1人に2枚以上割り当てた時点で結果が壊れた |
| `test_matcher.py::test_match_leaves_distant_faces_unassigned` | 閾値判定が無く、どんなに遠い顔も必ず誰かに割り当てられた（Issue #22） |
| `test_matcher.py::test_dry_run_is_not_blinded_by_a_previous_match` | `--dry-run` が2回目以降ほぼ空振りし、閾値を決める目安にならなかった |
| `test_gui_person.py::test_a_broken_exif_date_is_treated_as_missing` | EXIF が `0000:00:00` の写真で、撮影日時を持っているように見え、ファイル日時のフォールバックまで消えた |
| `test_db.py::test_saving_scores_does_not_clear_family_score` | `INSERT OR REPLACE` で `scan` が `match` の書いた値を消していた |
| `test_gui_assignment.py::test_face_age_dialog_keeps_zero_distinct_from_unset` | `value() or None` で0歳が「未設定」に潰れた |
| `test_gui_assignment.py::test_the_age_can_be_typed_straight_from_the_keyboard` | **年齢をキーボードから入力できず、▲を押すしかなかった。** 「未設定」の文字が入った欄に数字を打つと検証に落ちて無反応だった |
| `test_gui_assignment.py::test_an_age_can_be_cleared_back_to_unset` | 一度入れた年齢を未設定へ戻せなかった |
| `test_gui_assignment.py::test_registered_faces_dialog_pages_through_every_assigned_face` | 割り当て済み一覧にページャが無く、201件目以降に到達できなかった |
| `test_gui_person.py::test_assigning_several_faces_warns_that_one_age_covers_them_all` | **まとめて割り当てるときに「N件すべてに同じ年齢を入れます」が出ていなかった。** #41 で入れた知らせが、あとから直す画面にしか繋がっていなかった |
| `test_scanner_incremental.py::test_scan_skips_hash_and_faces_on_second_run` | 2回目のスキャンが差分にならなかった（Issue #9） |
| `test_scanner_incremental.py::test_touching_a_file_does_not_make_every_later_scan_read_it_again` | 更新時刻だけ変わったファイルが恒久的に再ハッシュされ、441GB を毎回読み直した |
| `test_scanner_incremental.py::test_scan_stops_when_the_embedding_model_cannot_be_loaded` | モデルが読めないと特徴量が全件 NULL のまま「スキャン済み」になり、無言で全損した |
| `test_scanner_incremental.py::test_an_error_from_one_file_is_not_reported_for_the_next` | 直近のエラーが大域変数に残り、無関係なファイルに付いた |
| `test_migration.py::test_a_version_2_database_keeps_every_face_when_migrated` | **v2 のDBを v1 の再構築経路へ流すと、顔 58,606 件と数時間ぶんのスキャンが消える**（Issue #48 で版ごとの分岐を追加） |
| `test_migration.py::test_opening_an_old_database_does_not_stamp_it_as_current` | **移行していないDBにアプリが版の印だけを刻んだ。** `migrate` が「すでに最新です」と答えて何もしなくなり、欠けた列が二度と足されない。実データで発生し、GUI で入れた誕生日が保存されなかった |
| `test_migration.py::test_a_database_whose_version_ran_ahead_is_still_repaired` | 上の状態になったDBを、版ではなく**実際の列**を見て直せること |
| `test_scanner_incremental.py::test_a_worker_writes_its_errors_to_the_log_file` | `fork` をやめた副作用で、並列時にワーカーのログがファイルへ1行も残らなくなった（既定の経路） |
| `test_scanner_incremental.py::test_the_workers_are_not_started_by_forking` | **並列スキャンが実データのDBを壊した。** fork した子が親の SQLite 接続を引き継ぎ、`row N missing from index idx_media_hash`（Issue #35） |
| `test_scanner_incremental.py::test_a_worker_does_not_inherit_what_the_parent_put_in_memory` | 上と同じ原因を、子が親の状態を引き継いでいないかという側から押さえる |
| `test_cli_progress.py::test_progress_keeps_the_last_error_instead_of_overwriting_it` | `Error: none` が直近のエラーを塗り潰した（Issue #25） |
| `test_cli_commands.py::test_convert_heic_does_not_need_a_database` | DB を使わないコマンドが DB パスを要求して落ちた |
| `test_scanner_incremental.py::test_faces_stored_without_embeddings_are_picked_up_once_the_model_returns` | `--allow-missing-embeddings` で入れた顔が、モデル設置後も回収されなかった |
| `test_pytest_summary.py::test_the_counts_survive_the_colours_pytest_adds_on_a_terminal` | **回帰テストの集計行が、端末で実行すると必ず `0 passed / 0 failed` になっていた**（しかも成功に見えた） |
| `test_pytest_summary.py::test_output_that_cannot_be_read_is_not_reported_as_zero` | 集計できないときに 0 を出して成功に見せた |
| `test_worklog_archive.py::test_a_section_that_is_not_a_dated_entry_survives` | 日付エントリ以外の節を黙って消した（毎コミット実行するスクリプト） |
| `test_face_real.py::test_model_directory_is_resolved_without_pkg_resources` | `face_recognition_models` を import すると setuptools 81 以降で落ちた（Issue #10） |
| `test_face_io.py::test_detection_rectangles_come_back_in_the_original_scale` | 長辺1280pxを超える画像は縮小して検出する。戻し倍率を間違えると矩形がずれ、特徴量まで壊れる |
| `test_face_io.py::test_embed_version_records_the_padding` | パディングを変えても `EMBED_VERSION` が据え置かれ、古い特徴量と新しい特徴量が同じ版として混ざる |
| `test_evaluation.py::test_a_teacher_in_the_same_photo_is_not_allowed_to_answer_for_the_face` | 交差検証で、抜いた顔とほぼ同じ手本が残っていると取りこぼし率が0に見え、閾値の判断を誤る |
| `test_scoring.py::test_smile_score_follows_the_mouth_aspect_ratio` | フェイクの都合で笑顔スコアの式が一度も通っておらず、常に 0.0 を返していても気づけなかった |
| `test_system.py::test_scan_is_incremental_on_second_run` | 上と同じ差分スキャンを、CLI の通し実行で確認する |

---

## ファイル別

### `test_db.py` — スキーマと永続化（8件）

| テスト | 内容 |
|---|---|
| `test_database_schema_and_media_crud` | メディアの登録と取得。未スキャンは `face_count IS NULL` |
| `test_save_media_resets_scan_state_when_hash_changes` | ハッシュが変われば検出状態をリセットする |
| `test_person_crud_and_face_assignment` | 人物のCRUD。人物を消しても顔は残り未割当に戻る |
| `test_deleting_media_cascades_to_faces_and_results` | 外部キーの CASCADE |
| `test_saving_scores_does_not_clear_family_score` | `scan` と `match` が互いのスコアを潰さない |
| `test_load_manual_embeddings_pairs_vectors_with_person_ids` | 手本に使うのは手動割り当てだけ |
| `test_person_birth_date_is_stored_and_can_be_cleared` | 誕生日は未設定と区別し、未設定へ戻せる |
| `test_a_person_without_a_birth_date_is_stored_as_unset` | 誕生日は任意 |

### `test_scanner_incremental.py` — 走査と差分判定（20件）

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
| `test_the_workers_are_not_started_by_forking` | **ワーカーを `fork` で起こさない**（実データのDBが壊れた。Issue #35） |
| `test_a_worker_does_not_inherit_what_the_parent_put_in_memory` | 子が親のメモリ状態を引き継がないこと（引き継ぐなら fork で起きている） |
| `test_a_worker_writes_its_errors_to_the_log_file` | **ワーカーのログがログファイルに残ること。** `spawn` の子はログ設定も引き継がないので、張り直さないと並列時だけ記録が消える |

### `test_face_io.py` — 顔の入出力（24件）

モデルを必要としない `face.py` の経路。

| テスト | 内容 |
|---|---|
| `test_read_rgb_returns_the_image_as_rgb` | 画像を (高さ, 幅, 3) の uint8 で読む |
| `test_read_rgb_reads_the_first_frame_of_a_video_and_fixes_the_channel_order` | 動画は先頭1フレームだけを読み、BGR を RGB へ直す |
| `test_read_rgb_returns_none_for_a_missing_file` ほか3件 | 不在・ディレクトリ・画像でないファイル・動画でないファイル |
| `test_the_latest_error_is_cleared_before_the_next_file` | 大域のエラーを消せる |
| `test_detection_rectangles_come_back_in_the_original_scale` | **長辺1280pxを超える画像は縮小して検出し、矩形を原寸へ戻す** |
| `test_small_images_are_not_resized` | 縮小が不要な大きさでは素通し |
| `test_no_faces_are_reported_for_a_black_image` | 顔なし |
| `test_detection_returns_nothing_when_the_detector_is_unavailable` | 検出器が読めないときは空とエラー文言 |
| `test_face_rect_squares_the_rectangle_on_its_longer_side` ほか2件 | 正方形化・パディング率・画像内への収まり |
| `test_embed_version_records_the_padding` | **パディングを変えたら `EMBED_VERSION` も上がる**という約束の見張り |
| `test_crop_face_cuts_exactly_the_given_rectangle` | 切り出す矩形と画素 |
| `test_make_thumbnail_shrinks_the_face_to_the_listing_size` ほか3件 | 一覧用に必ず縮むこと、寸法指定、空矩形は None |
| `test_load_face_image_bytes_recuts_from_the_original_file` ほか1件 | 元写真からの取り直しと、元が無いときの例外 |

### `test_scoring.py` — スコアの計算式（21件）

**フェイクの FaceMesh が全ランドマークを (0.5, 0.5) で返すため、
`estimate_smile_score` は 0.0 で早期 return する枝しか通っていなかった。**
`fake_face_mesh` フィクスチャで座標を与えて式そのものを確かめる。

| テスト | 内容 |
|---|---|
| `test_smile_score_follows_the_mouth_aspect_ratio` | 口の縦横比 2.0 → 42.0（`(比 - 1.4) * 70`） |
| `test_a_round_mouth_scores_zero_instead_of_going_negative` | 負のスコアを出さない |
| `test_an_extremely_wide_mouth_is_capped_at_100` | 100 で頭打ち |
| `test_landmarks_on_a_single_point_score_zero` | 既定のフェイクが通る枝。上の3件が長く未検証だった理由 |
| `test_smile_score_is_zero_when_no_face_mesh_is_found` ほか2件 | 顔なし・モデル不在・空の矩形 |
| `test_quality_combines_brightness_and_face_size` | 明るさ×60 + 顔の面積比×40 |
| `test_a_dark_face_only_earns_the_size_part` ほか3件 | 暗い顔、面積比の頭打ち、大小関係、空の矩形 |
| `test_distance_to_similarity`（5件） | 距離 0.6 を基準にした 0-100 への変換とクリップ |
| `test_media_scores_take_the_best_face` ほか2件 | メディアのスコアは最良の顔で代表する |

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

### `test_evaluation.py` — 精度の実測（11件）

手動割り当てを正解とみなし、手本を1件ずつ抜いて `match` の判定を通す
（1件抜き交差検証）。**この測定が甘く出ると、閾値の判断ごと間違える。**

| テスト | 内容 |
|---|---|
| `test_a_face_returns_to_its_own_person_when_the_teachers_are_close` | 近い手本があれば正解になる |
| `test_lowering_the_threshold_turns_correct_answers_into_missed_ones` | **閾値を絞るほど取りこぼしが増える**（測定として成立しているか） |
| `test_a_face_that_lands_on_another_person_is_counted_as_wrong_not_missed` | 誤りと取りこぼしを混ぜない（直し方が逆） |
| `test_a_teacher_in_the_same_photo_is_not_allowed_to_answer_for_the_face` | **同じ写真の同一人物を手本から外す。** 残すと必ず当たって取りこぼしが0に見える |
| `test_a_person_with_a_single_teacher_is_left_out_instead_of_counted_as_missed` | 手本1件の人物は構造上かならず取りこぼすので、率に混ぜない |
| `test_the_margin_leaves_a_face_between_two_people_unassigned` | マージンの判定が `match` と同じ |
| `test_automatic_assignments_are_not_used_as_the_answer_key` | 自動の結果を正解として数えない |
| `test_a_database_without_any_assigned_face_says_what_to_do` | 手本0件なら率ではなく次の手順を出す |
| `test_the_report_shows_each_threshold_and_each_person` | 閾値ごと・人物ごとの内訳が出る |
| `test_the_report_tells_the_user_when_nothing_could_be_evaluated` | 評価対象0件を 0.0%（＝取りこぼし無し）と出さない |
| `test_the_person_column_lines_up_when_names_mix_japanese_and_ascii` | 人物名の列を見た目の幅で揃える |

### `test_gui_assignment.py` — GUI での割り当て（18件）

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
| `test_changing_an_age_later_also_offers_the_calculated_value` | あとから直すときも計算値が初期値に入る |

### `test_gui_person.py` — 人物編集とプレビュー（40件）

| テスト | 内容 |
|---|---|
| `test_editing_a_person_saves_the_new_values` | 今の値でダイアログを開き、保存で一覧まで入れ替わる |
| `test_cancelling_the_edit_changes_nothing` | 取り消しで何も変えない |
| `test_an_empty_name_is_rejected` | 名前は必須。空で上書きしない |
| `test_editing_without_a_selection_does_not_open_a_dialog` | 人物未選択ならダイアログを開かない |
| `test_selecting_a_face_previews_it_from_the_original_photo` | サムネイルではなく元写真から取り直して表示する |
| `test_the_preview_names_the_file_when_the_original_is_gone` | **元写真が消えていたらパスを出す**（NFS 未マウント時に起きる） |
| `test_the_preview_does_nothing_without_a_selection` | 未選択なら何もしない |
| `test_the_preview_uses_the_last_selected_face` | 複数選択では最後の1件 |
| `test_the_shooting_date_is_shown_when_the_photo_has_one` | 撮影日時を出す（**年齢はこれを見て入れる**） |
| `test_a_photo_without_exif_says_so_and_falls_back_to_the_file_time` | **ファイルの日時を撮影日時として出さない**（コピーで変わる） |
| `test_the_folder_is_shown_relative_to_the_source_root` | フォルダは `source_root` からの相対。日付の手がかりになる |
| `test_a_photo_outside_the_source_root_keeps_its_full_path` | `source_root` の外は絶対パスのまま |
| `test_the_folder_is_shown_without_a_source_root` | `source_root` が無くても動く |
| `test_selecting_a_face_fills_the_information_under_the_preview` | 顔を選ぶと情報欄が埋まる |
| `test_the_information_is_still_shown_when_the_original_is_gone` | **元写真が開けないときこそ出す**（出どころはDB） |
| `test_a_broken_exif_date_is_treated_as_missing` | `0000:00:00` を書くカメラがある（実データ55件）。持っていない扱いにしてファイル日時へ落とす |
| `test_a_photo_directly_under_the_source_root_says_so` | `フォルダ: .` では読めない |
| `test_a_relative_source_root_is_anchored_to_the_settings_file` | **起動した場所で表示が変わらない**（相対の起点は設定ファイル） |
| `test_an_absolute_source_root_is_left_alone` | 絶対パスと未設定は触らない |
| `test_the_preview_does_not_keep_the_previous_photo_when_the_image_cannot_be_decoded` | デコード失敗で上下が別の写真にならない |
| `test_the_age_dialog_says_how_many_faces_get_the_same_age` | **1回の入力が全件に入る**ことと、撮影日時のまたがりを知らせる |
| `test_the_summary_reaches_the_age_dialog` | 要約がダイアログに載る |
| `test_the_age_is_counted_from_the_birthday_not_the_year` | **誕生日を迎える前なら1引く**（年の引き算だけだと1歳ずれる） |
| `test_the_age_is_not_calculated_when_either_side_is_missing` | 誕生日か撮影日時が欠けたら計算しない |
| `test_a_broken_exif_date_does_not_produce_an_age` | **「撮影日時: 不明」と出ている写真に年齢だけ出さない**（`0000:00:00`） |
| `test_a_photo_taken_before_the_birthday_says_so` | 誕生前は行を消さず「誕生前」と出す（選び間違いに気づける） |
| `test_the_preview_shows_the_age_of_the_selected_person` | 情報欄の最後に「誰が何歳か」を出す |
| `test_the_preview_leaves_the_age_line_out_when_it_cannot_be_calculated` | 計算できないときは行そのものを出さない |
| `test_the_age_line_follows_the_person_selection` | 人物を選び直すと年齢の行が変わる。**元写真は読み直さない**（NFS 律速） |
| `test_a_birth_date_can_be_registered_and_cleared` | 誕生日の登録と、空欄での未設定へ戻し |
| `test_a_partly_filled_birth_date_is_rejected` | **年月日まで必須。** 一部だけの入力と、暦に無い日とで言うことを変える |
| `test_the_birth_date_is_built_from_three_numbers` | 年・月・日の3つから組み立て、保存済みの値を3つに割る |
| `test_typing_a_birth_date_straight_from_the_keyboard` | `--` の入った欄でも打鍵で置き換わる |
| `test_the_edit_dialog_opens_with_the_stored_birth_date` | 編集ダイアログが今の誕生日で開く |
| `test_the_person_dialog_round_trips_a_birth_date` | 実物のダイアログが誕生日を持ち帰る |
| `test_the_person_details_show_the_birth_date` | 人物詳細に誕生日が出る（年齢が出ない理由が分かる） |
| `test_the_suggested_age_needs_every_selected_face_to_agree` | **食い違うなら初期値を出さない**（1回の入力が全件に入る） |
| `test_the_age_dialog_opens_with_the_calculated_age` | 計算値を初期値に入れ、計算値だと画面に書く |
| `test_assigning_faces_offers_the_calculated_age_without_saving_it` | **自動保存はしない。** 取り消せば何も入らない |
| `test_assigning_several_faces_warns_that_one_age_covers_them_all` | まとめて割り当てるときにも、全件に入る旨とまたがりを知らせる |

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

### `test_cli_commands.py` — サブコマンドの配線（10件）

| テスト | 内容 |
|---|---|
| `test_convert_heic_does_not_need_a_database` | DB を使わないコマンドは DB パスを要求しない |
| `test_commands_that_need_a_database_still_say_so` | 使うコマンドはこれまで通り要求する |
| `test_init_db_creates_a_usable_database` | `init-db` |
| `test_migrate_reports_that_a_fresh_database_is_current` | `migrate` は移行済みDBに何もしない |
| `test_scan_arguments_reach_the_scanner` | `scan` の全オプションが下へ届く |
| `test_match_arguments_reach_the_matcher` | `match` の全オプションが下へ届く |
| `test_the_database_path_falls_back_to_the_settings_file` | 設定ファイルへのフォールバック |
| `test_evaluate_arguments_reach_the_evaluation` | `evaluate` の全オプションが下へ届く |
| `test_a_threshold_that_cannot_be_read_stops_instead_of_being_dropped` | 読めない閾値を黙って捨てない |
| `test_evaluate_runs_end_to_end_on_a_database_with_assigned_faces` | CLI から実際に数字が出るところまで通す |

### `test_config.py` — 設定の探索（11件）

環境変数 → カレントディレクトリ → リポジトリ直下 の順に探すこと、優先順位、
壊れたファイルでもコマンドが止まらないこと。リポジトリ直下の候補を
editable install のときだけ出すこと（通常のインストールでは `REPO_ROOT` が
`lib/python3.x` を指すので、**`REPO_ROOT` 自身を見ても判別できない。
モジュールの位置で判断する**）。同じ場所を2度並べないこと。

### `test_cli_progress.py` — 進捗表示（7件）

2行の書き換え、直近のエラーの保持、長いエラーの切り詰め、改行の潰し。

### `test_migration.py` — スキーマの移行（20件）

**v1 → v2**: Media と Person を温存し Face と AnalysisResult を破棄すること、
冪等性、旧スキーマのまま使おうとしたときのエラー、特徴量 BLOB の往復。

**v2 → v3**: `Person.birth_date` を足すだけで、**顔・解析結果・検出済みの状態が
1件も減らないこと**。v1 の再構築経路へ流すと落ちることを確認済み。
案内の文面が「破棄します」にならないこと（消えると読めると実行をためらう）。

**版の印だけが進むのを防ぐ**: 移行していないDBをアプリが開いても版を刻まないこと、
すでに刻まれてしまったDBを**実際の列**を見て直せること、列の一覧が `SCHEMA` から
導かれていること。

移行前に何件消えるかを数える `describe_migration`、バックアップの保存先の
指定（親ディレクトリが無くても作る）と既定の日時付きの名前、
`vacuum=False` / `make_backup=False`、進捗メッセージ、空のファイルへの
スキーマ作成、存在しないDBを指したときのエラー。

### `test_worklog_archive.py` — 作業履歴の切り出し（16件）

直近20件を残して年ごとに切り出すこと、索引の作り直し、冪等性、`--check`。
**日付エントリ以外の節を消さないこと**（前にあるものは前書きとしてその場に残し、
あとにあるものは末尾へ移す）。切り出し済みの本文を後から書き換えないこと。

### `test_pytest_summary.py` — 回帰テストの集計行（9件）

CLAUDE.md §5 で「最終行を PR 本文に貼る」ことを必須にした行。着色された
pytest 出力から件数と所要時間を読めること、`0 passed / 0 failed` を
成功のように見せないこと。

### `test_check_handoff.py` — 引き継ぎの点検が自分の故障を隠さない（10件）

**点検が働かなかったことを「異常なし」と報告すると、壊れた番人に守られている
つもりになる。** 最初の版は `gh` が標準エラーに1行出すだけで壊れ、PR の無い
ブランチの警告が黙って消えていた（PR #50 のレビュー指摘1）。

| テスト | 内容 |
|---|---|
| `test_a_noisy_gh_does_not_turn_the_check_into_an_all_clear` | **stderr の雑音で判定が消えない** |
| `test_a_gh_that_cannot_run_is_reported_instead_of_being_ignored` | 確認できなかったことを警告に出す |
| `test_a_branch_with_an_open_pull_request_is_not_a_warning` | PR があれば鳴らさない |
| `test_a_documented_branch_without_a_pull_request_is_not_a_warning` | 申し送りに書いてあれば鳴らさない |
| `test_an_undocumented_branch_without_a_pull_request_is_a_warning` | PR も申し送りも無いものだけ鳴らす（#48 の形） |
| `test_a_slow_command_does_not_block_the_check` | **繋がらない環境で止まらない**（10秒で打ち切る） |
| `test_the_output_streams_are_not_mixed` | stdout と stderr を分ける |
| `test_offline_does_not_touch_the_remote` | `--offline` は `git fetch` を呼ばない |
| `test_a_failed_fetch_says_the_judgement_used_stale_information` | **黙って古い情報で判定しない** |
| `test_the_working_tree_is_shown_but_not_warned_about` | コミット前の汚れは鳴らさない |

### `test_plan_stays_true.py` — 実装プランが実態からずれない（プラン文書の本数ぶんを含む）

**プランは黙って古くなる。** 誰かが嘘を書くのではなく、実装だけ進んで文書が
置き去りになる。読んで矛盾に気づくには実態を知っている必要があるので、
レビューでも落ちる。形だけでも機械で見張る。

| テスト | 内容 |
|---|---|
| `test_the_worklog_entries_are_newest_first` | **日をまたぐ逆転が無い**（`CLAUDE.md` §1 の前提）。同じ日の中の順序は見張れない |
| `test_every_phase_document_linked_from_the_roadmap_exists` | ROADMAP のリンク切れ |
| `test_a_phase_document_is_linked_from_the_roadmap` | 書いたのに張り忘れた文書 |
| `test_a_phase_that_has_started_names_its_issue` | 着手済みのフェーズに Issue 番号がある |
| `test_a_real_data_count_in_a_plan_document_is_dated_or_recountable` | **実データの件数に日付か数え直す手段がある**（4件、文書ごと） |
| `test_every_handoff_note_is_linked_from_the_worklog` | 迷子の申し送りを作らない |
| `test_the_checks_would_catch_a_plan_that_drifted` | **番人自身が働く**（本物の検査を、崩した文書に向けて呼ぶ） |
| `test_entries_on_the_same_day_are_not_ordered` | 同日内は見張らない（**意図した限界**） |
| `test_a_dated_count_in_another_section_does_not_excuse_this_one` | **節をまたいだ免除をしない**（守りたい文書ほど先に免除される） |
| `test_a_recount_command_excuses_only_its_own_section` | 数え直すコマンドも同じ節の中だけ |
| `test_a_rounded_number_is_not_treated_as_a_count` | 丸めた表現は引っかからない（逃げ道） |

git と GitHub の状態（PR の無いブランチなど）は `scripts/check_handoff.py`。
`run_regression.sh` の 5/5 で**実行する**が、**合否には含めない**
（ネットワークが要るため）。**そのスクリプト自体のテストは
`test_check_handoff.py`。**

### `test_docs_stay_stable.py` — 文書に実装の数字を置かない（4件）

`CLAUDE.md` と `README.md` に、テストの件数や成功件数が書かれていないことを
見る。書いてしまうと**関係のない変更のたびに更新が要り**、忘れれば
いちばんよく読まれる2つの文書が静かに嘘になる。番人自身が働くことも
確かめている（数字を戻すと落ちること、`N passed / M failed` の書式は通ること）。

### `test_system.py` — 通し（2件、`system` マーカー）

`init-db` → `scan` → GUIでの割り当て → `match` → `select` を一通り流す。
2回目のスキャンが差分になることも確認する。

### `test_face_real.py` — 実物のモデル（2件、`models` マーカー）

`face_recognition_models` を import せずにモデルのパスを取り出せること、
実物の dlib が128次元を返すこと。

**矩形の正方形化とパディングの確認はここから `test_face_io.py` へ移した。**
`face_rect` はモデルを必要としないのに `models` マーカーの下にあったため、
回帰テスト（`-m "not models"`）では常に除外されていた。

---

## テストの作り

**外部依存を持たない。** ネットワーク、NFS、実データベース、実物のモデル
（`models` マーカーを除く）のいずれにも触らない。すべて `tmp_path` の中で完結する。

`tests/conftest.py` が次を差し替える。**フェイクの中身は `tests/fakes.py`。**
`scan` のワーカーは `spawn` で起こすため子は白紙で始まり、`conftest` を
import できない。子へは `worker_initializer=install_fake_backends` で入れる。

| 差し替えるもの | 何になるか |
|---|---|
| `mediapipe` | 顔検出のフェイク。真っ黒な画像は「顔なし」、それ以外は「顔が1つ」。**実装が先に見る `relative_bounding_box` を返す**ので、実際の検出経路をそのまま通る |
| FaceMesh | 既定は全ランドマークが (0.5, 0.5)。`fake_face_mesh` フィクスチャで座標を上書きすると、笑顔スコアの式を検証できる。**既定のままでは式が 0.0 の枝しか通らない** |
| `dlib` | 顔領域の平均色から決まる128次元ベクトル。同じ色の顔は近く、違う色の顔は遠くなるので、`match` の判定を検証できる |
| `config.REPO_ROOT` | 空のディレクトリ。開発機の `config/app_settings.json` をテストから見えなくする |
| `cli._progress_started` | 各テストの前後でリセット。進捗表示の大域状態がテストの順序に依存した差を作らないようにする |
| `QMessageBox` | 何もしない。モーダルで止まらないようにする |

`tests/helpers.py` の `write_image()` に色を指定すると「同一人物」「別人」を
作り分けられる。`write_heic()` は HEIC を書き出す（encoder が無い環境ではスキップ）。
`write_video()` は数フレームの mp4 を書き出す。**色は BGR で与える**ので、
先頭フレームの色を見れば `read_rgb` が並びを直しているか分かる
（コーデックが無い環境ではスキップ）。

GUI のテストは `QT_QPA_PLATFORM=offscreen` で動くので、画面が無くても実行できる。
