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
| `test_gui_person.py::test_another_shape_of_broken_exif_is_also_treated_as_missing` | **`0000` で始まるかだけを見ていた。** 別の壊れ方（`TTTT-TT-TTTTT:TT:TT`、実データ Media 67件）が素通りし、撮影日時としてそのまま画面に出ていた |
| `test_gui_assignment.py::test_rebuilding_the_list_does_not_reload_the_preview` | **一覧を作り直すたびにプレビューが再描画され、元写真を NFS から読み直していた。** 200件を選んで割り当てると100回読み直し、1回の操作に17秒かかった（実測。止めると92ms） |
| `test_db.py::test_a_broken_exif_date_does_not_take_over_the_newest_page` | **撮影日時の新しい順にすると、壊れた EXIF が1ページ目をまるごと占領する**（`T` は数字より大きい。実データで顔123件） |
| `test_appearance.py::test_an_unaligned_teacher_still_blocks_a_stranger_as_the_runner_up` | **整列できない手本を丸ごと外すと、誤りが +418 件増えた**（実データ・2026-10-09）。その人物が2位の対抗馬として他人を止めていた役目まで消えた |
| `test_appearance.py::test_a_rejected_young_teacher_does_not_hand_the_face_to_someone_else` | **年齢の差を距離に足し引きすると、勝つ人物が入れ替わった**（大人の手本を締めて +258 件） |
| `test_appearance.py::test_nothing_is_recorded_when_the_landmark_model_is_unavailable` | 目印の検出器が無い環境で「整列できない」と書くと、全部の手本が根拠から外れ二度と測り直されない（実装中にテストのフェイクで踏んだ） |
| `test_selection.py::test_family_only_keeps_a_photo_whose_only_family_face_is_blurred_and_turned_away` | `family_only` を「点 > 0」で絞っていたので、**家族が写っているのにボケて横を向いた写真が落ちた**（実データの複製で 433 枚） |
| `test_appearance.py::test_an_older_teacher_accepts_a_face_even_when_a_baby_teacher_is_nearer` | 年齢の上限を最も近い1件で判定していたので、**手本を足すと割り当てが減った** |
| `test_selection.py::test_a_crisp_stranger_does_not_lift_a_blurred_family_photo` | 写真の笑顔・画質が「写っている顔の最良値」で、**隣の他人がくっきり笑っていればボケた家族の写真が上位に来た** |
| `test_selection.py::test_a_change_made_in_the_gui_reaches_select_without_running_match` | `select` が保存済みの `family_score` を読んでいたので、GUI で直した割り当てが次の `match` まで届かなかった |
| `test_db.py::test_saving_scores_does_not_clear_family_score` | `INSERT OR REPLACE` で `scan` が `match` の書いた値を消していた |
| `test_gui_assignment.py::test_face_age_dialog_keeps_zero_distinct_from_unset` | `value() or None` で0歳が「未設定」に潰れた |
| `test_gui_assignment.py::test_the_age_can_be_typed_straight_from_the_keyboard` | **年齢をキーボードから入力できず、▲を押すしかなかった。** 「未設定」の文字が入った欄に数字を打つと検証に落ちて無反応だった |
| `test_gui_assignment.py::test_an_age_can_be_cleared_back_to_unset` | 一度入れた年齢を未設定へ戻せなかった |
| `test_gui_assignment.py::test_the_person_view_pages_through_every_assigned_face` | 割り当て済み一覧にページャが無く、201件目以降に到達できなかった |
| `test_gui_person.py::test_assigning_several_faces_warns_that_one_age_covers_them_all` | **まとめて割り当てるときに「N件すべてに同じ年齢を入れます」が出ていなかった。** #41 で入れた知らせが、あとから直す画面にしか繋がっていなかった |
| `test_gui_person.py::test_the_suggested_age_is_withheld_when_a_face_has_no_shooting_date` | **撮影日時の無い顔に、別の写真から計算した年齢が黙って保存された。** `shooting_dates_for_faces`（いまの `taken_for_faces`）が日時の無い顔を落とし、`suggested_age` が残った `None` も捨てていたため、10件中9件が EXIF 無しでも残る1件の年齢が全件の初期値になった（実データの 15.8% が該当） |
| `test_db.py::test_updating_a_person_without_a_birth_date_keeps_it` | **`update_person` を省いて呼ぶと誕生日が消えた**（`KEEP_AGE` と同じ罠） |
| `test_gui_person.py::test_the_age_appears_right_after_the_birth_date_is_registered` | 誕生日を登録しても年齢の行がその場で出ず、**機能が効いていないように見えた** |
| `test_db.py::test_bulk_face_ids_can_be_narrowed_by_the_month_range` | **`db.face_ids` が撮影年月の引数を受け取らず、年月で絞った状態で行事の「まとめて…」を押すと `TypeError` で落ちた**（月の絞り込みを足したときの通し忘れ） |
| `test_migration.py::test_the_backup_keeps_writes_that_are_still_in_the_wal` | **控えを `shutil.copy2` で取っていたため、WAL にだけ残っている書き込みが控えから黙って抜けていた**（WAL に全部あるときは表すら無い控えになる） |
| `test_db.py::test_the_age_order_uses_the_calculated_age_across_every_page` | **「年齢の若い順」が確定値（`Face.age`）だけで並べていた。** 実データではひよりの 9,502 件のうち 199 件しか並ばず、残りは id 順のまま2ページ目以降に散っていた（2026-10-09 に利用者が報告） |
| `test_gui_views.py::test_a_small_thumbnail_at_the_top_does_not_shrink_the_whole_page` | **ページの先頭に小さいサムネイルが来ると、枠がそれに合わせて縮み、残りの顔が切り詰められて下の文字も消えた**（`setUniformItemSizes` は先頭の項目から寸法を決める。2026-10-09 に利用者が報告） |
| `test_db.py::test_every_list_filter_also_works_for_counting_and_for_bulk` | 上の落ち方を**種類ごと**に防ぐ。一覧・件数・まとめて処理が同じ絞り込みを受け取ることを、`_face_filter` の引数から数えて確かめる |
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
| `test_gui_event_clusters.py::test_assigning_a_cluster_never_touches_the_teacher_in_it` | **束をまとめて割り当てる操作が、束に混ざった手本を巻き込まないこと。** 1回の操作が数百件に効くので、手本が消えると `match` の土台が崩れる |
| `test_gui_event_clusters.py::test_an_event_without_a_readable_day_is_filtered_by_the_undated_mark` | **`day=None`（日で絞らない）と「日が読めない顔だけ」を同じ値で表さない。** 取り違えると、まとめて除外がフォルダ全体に効く（`KEEP_AGE` と同じ罠） |
| `test_gui_event_clusters.py::test_the_cluster_dialog_bundles_only_undated_faces_of_an_undated_event` | **上の変換が束ねる画面の経路で抜けていた。** 日付不明の行事を束ねると同じフォルダの別の日の顔まで束に入り、まとめて押すとそちらにも効いた（実データで日付つきの未割当 18,000 件が 363 フォルダで巻き込まれる。PR #62 のレビュー指摘1） |
| `test_source_roots.py::test_select_copies_a_photo_outside_every_root_by_its_name` | **設定の root の外のメディアを `select` がコピーしようとすると落ちた**（`str` に `as_posix()`。実データでは `な携帯` の 5,323 件が外側だった。#24 で見つけた） |
| `test_system.py::test_scan_is_incremental_on_second_run` | 上と同じ差分スキャンを、CLI の通し実行で確認する |

---

## ファイル別

### `test_db.py` — スキーマと永続化（38件）

| テスト | 内容 |
|---|---|
| `test_database_schema_and_media_crud` | メディアの登録と取得。未スキャンは `face_count IS NULL` |
| `test_save_media_resets_scan_state_when_hash_changes` | ハッシュが変われば検出状態をリセットする |
| `test_person_crud_and_face_assignment` | 人物のCRUD。人物を消しても顔は残り未割当に戻る |
| `test_deleting_media_cascades_to_faces_and_results` | 外部キーの CASCADE |
| `test_saving_scores_does_not_clear_family_score` | `scan` と `match` が互いのスコアを潰さない |
| `test_load_manual_embeddings_pairs_vectors_with_person_ids` | 手本に使うのは手動割り当てだけ |
| `test_shooting_dates_come_back_one_per_face` | **顔1件につき1件返す。** `DISTINCT` で潰さず、日時の無い顔も落とさない |
| `test_the_number_of_affected_faces_is_right_even_with_progress` | **進み具合を知らせても戻り値が壊れない**（塊ごとに足す） |
| `test_updating_a_person_without_a_birth_date_keeps_it` | **省いて呼んだら触らない**（`None` は消す指示） |
| `test_person_birth_date_is_stored_and_can_be_cleared` | 誕生日は未設定と区別し、未設定へ戻せる |
| `test_a_person_without_a_birth_date_is_stored_as_unset` | 誕生日は任意 |
| `test_faces_can_be_listed_newest_shot_first` | 撮影日時の新しい順。**日時の無い顔は最後** |
| `test_faces_can_be_listed_youngest_age_first` | 年齢の若い順。**未設定は最後**（SQLite の NULL は最小） |
| `test_the_default_order_is_still_the_quality_score` | **既定を変えない**（`match` と `evaluate` も通る） |
| `test_every_order_honours_the_same_filters` | 絞り込みを2通り書かない。どの並びでも同じ条件が効く |
| `test_pagination_does_not_repeat_or_skip_a_face` | 撮影日時が同じ顔が並んでも、ページをまたいで重複・欠落しない |
| `test_a_broken_exif_date_does_not_take_over_the_newest_page` | **壊れた EXIF を「いちばん新しい」として先頭に出さない** |
| `test_the_shooting_date_order_does_not_fall_back_to_a_full_sort` | **索引を歩くこと。** 全件並べ直しに戻っていないかを問い合わせ計画で見る |
| `test_every_list_filter_also_works_for_counting_and_for_bulk` | **一覧・件数・まとめて処理が、同じ絞り込みを受け取ること**（`_face_filter` の引数が正本） |
| `test_bulk_face_ids_can_be_narrowed_by_the_month_range` | まとめて処理する対象が撮影年月の絞り込みに従う |
| `test_face_counts_are_gathered_in_one_query` | 左の一覧の件数を**1回の問い合わせ**で数える（人数ぶんの問い合わせにしない） |
| `test_the_rejection_list_is_read_through_the_same_filters` | 「この人物ではない」の一覧も**ふつうの絞り込みに乗る**（撮影年月で絞れる・「誰でもない顔」は外す・記録は消さない） |
| `test_faces_can_be_listed_least_confident_first` | 自動割り当ての見直しは**確信度の低い順**（持たない顔は最後） |
| `test_the_age_order_uses_the_calculated_age_across_every_page` | **年齢順は画面に出ている年齢（確定値か計算値）で全件を並べてからページに分ける。** 出せない顔は最後・重複も欠落もしない |
| `test_the_age_order_uses_each_face_s_own_person_when_none_is_selected` | 全員ぶんの表示では、顔ごとの人物の誕生日で年齢を出して並べる |
| `test_every_order_has_its_reverse_and_keeps_missing_values_last` | **並びにはどれも逆向きがあり、値を持たない顔はどちらの向きでも最後**（#69 のコメント） |
| `test_unassigned_faces_shot_before_the_birth_are_left_out` | 人物の未割当から**誕生前の写真を外す。** 撮影日時が読めない顔・誕生日が読めない場合は外さない |
| `test_unassigned_faces_marked_not_this_person_are_left_out` | 「この人物ではない」と記録した顔は**その人物の候補にだけ**出さない |

### `test_db_events.py` — 行事（フォルダ×日）の絞り込みと集計（23件）

| テスト | 内容 |
|---|---|
| `test_folder_expression_and_folder_of_agree` | **SQL の式と Python の関数が同じ答えを返す**（片方だけ直すと絞り込みが黙って外れる） |
| `test_folder_of_handles_paths_without_a_folder` | フォルダを持たないパスは `""` |
| `test_day_expression_rejects_unreadable_dates` | `TTTT-TT-TT…` と `0000-00-00` を日として扱わない（**長さでは弾けない**） |
| `test_day_expression_cuts_the_date_part` | 読める撮影日時から日付だけを取る |
| `test_event_face_counts_splits_the_same_folder_by_day` | **同じフォルダでも日が違えば別の行事**（PR #56 との違い） |
| `test_event_face_counts_separates_unassigned_manual_and_rejected` | 未割当・手本・除外を別々に数える |
| `test_event_face_counts_are_sorted_by_unassigned_desc` | 未割当の多い順 |
| `test_event_face_counts_keeps_faces_without_a_readable_day` | **撮影日時が読めない顔を落とさない**（実データ 5,916 件） |
| `test_event_face_counts_ignores_media_without_faces` | 顔の無いメディアだけの行事は出さない |
| `test_list_faces_filters_by_event_in_every_order` | **並び順の経路が2つある**。どちらでも同じ条件が効く |
| `test_day_none_and_undated_are_different_instructions` | `day=None` と `db.UNDATED` は別の指示 |
| `test_event_filter_does_not_match_subfolders` | 入れ子は別のフォルダ（前方一致にしない） |
| `test_face_ids_returns_every_match_beyond_one_page` | まとめて処理はページをまたぐ |
| `test_face_ids_for_unassigned_leaves_manual_faces_alone` | **手本を巻き込まない** |
| `test_the_event_filter_walks_its_index` | **`idx_media_event` を使う**（式を書き写して綴りがずれると黙って外れる） |
| `test_face_filter_without_an_event_keeps_the_previous_query` | **行事未指定なら問い合わせを変えない**（`match` と `evaluate` も通る） |
| `test_load_faces_for_clustering_requires_a_day` | **束ねる関数の `day` に既定値を置かない**（日で絞らない呼び出しは常に誤り） |
| `test_load_faces_for_clustering_separates_the_undated_faces` | `UNDATED` なら日付の読めない顔だけを読む |

### `test_clustering.py` — 行事の中で顔を束ねる（15件）

| テスト | 内容 |
|---|---|
| `test_far_faces_stay_in_separate_clusters` | 閾値より遠い顔は別の束 |
| `test_average_linkage_does_not_chain` | **単連結にしない**（等間隔の顔が鎖で繋がらない） |
| `test_weights_follow_the_size_of_each_cluster` | 平均連結の重みは件数 |
| `test_bigger_clusters_come_first` | 大きい束から見せる |
| `test_faces_keep_the_order_they_were_given` | 束の中は渡された順（先頭が代表） |
| `test_the_same_input_always_gives_the_same_clusters` | **乱数を使わない**（同じ入力なら同じ束） |
| `test_every_face_lands_in_exactly_one_cluster` | 顔を落とさない |
| `test_a_single_face_is_a_cluster_of_one` / `test_no_faces_means_no_clusters` | 端の場合 |
| `test_the_threshold_comes_from_the_active_model` | **閾値を書き写さない**（`embedding.ACTIVE`） |
| `test_the_metric_comes_from_the_active_model` | 既定の尺度はモデルのもの（コサイン） |
| `test_mismatched_lengths_are_refused` | 特徴量の無い顔を黙って落とさない |
| `test_too_many_faces_is_refused_instead_of_truncated` | **上限超過は黙って切らずに上げる** |
| `test_too_many_faces_is_refused_before_the_distance_matrix` | 断るのは (N, N) を確保する**前** |
| `test_a_non_square_distance_matrix_is_refused` | 距離行列の形を検査する |

### `test_event_clustering_measurement.py` — 行事ごとの束ねを測る道具（6件）

**測定の道具がおかしいと、間違った閾値で実データを束ねる。**

| テスト | 内容 |
|---|---|
| `test_the_measurement_never_writes_to_the_database` | **実データに書かない**（読み取り専用で開く） |
| `test_undated_faces_are_counted_as_folder_events` | **撮影日時が読めない顔を捨てない**（フォルダ単位の行事として数える） |
| `test_rejected_faces_are_left_out` | 除外した顔は入れない（決定は減らない） |
| `test_the_separation_is_measured_on_dated_events_only` | 行事の中／外の分離に、日付不明の顔を混ぜない |
| `test_the_decision_count_is_the_number_of_bundles_with_unassigned_faces` | 決定の数 = 未割当を含む束の数 |
| `test_the_report_keeps_the_two_kinds_of_events_apart` | 報告は日付つきと日付不明を別の表で出す |

### `test_fakes_stay_installed.py` — 回帰テストが実物のモデルを要らないこと（2件）

**作者の手元だけ通って他の環境で落ちる**のを防ぐ。実際に起きた（PR #60 の指摘1）。

| テスト | 内容 |
|---|---|
| `test_no_unmarked_test_file_undoes_the_fakes` | **`monkeypatch.undo()` を `models` マーカーの外で使わない。** conftest のフェイクまで巻き戻り、実物の ONNX を読みに行く |
| `test_the_marked_file_really_carries_the_marker` | 免除した側が本当にマーカーを持っていること（免除リストを抜け道にしない） |

**実行時に捕まえる番人は置けない**（`monkeypatch.undo()` は番人ごと巻き戻す。実測）。
だから静的に見張る。

### `test_reembed.py` — 特徴量の作り直し（15件）

**ここが壊れると、手作業で積み上げた手本と除外が消える。**

| テスト | 内容 |
|---|---|
| `test_too_small_thumbnails_do_not_keep_the_rebuild_unfinished` | **小さすぎる顔が作り直しを永遠に「途中」にしない。** 版を据え置くと毎回「もう一度実行すれば」と誤って案内する |
| `test_a_face_that_could_not_be_embedded_keeps_a_null_embedding` | 作れなかったことを「いまのモデルで作れなかった」として記録する（版を進めて特徴量 NULL） |
| `test_reembed_leaves_every_assignment_alone` | **手本・除外・年齢に触らない。** `scan --force-rescan` との決定的な違い |
| `test_the_original_photo_is_never_read` | **元写真を読まない**（NFS の 441GB を読み直さない） |
| `test_faces_already_on_the_current_version_are_left_out` | 版が一致する顔は対象外 |
| `test_running_twice_does_nothing_the_second_time` | 2回目は空振り（＝再開できることの裏返し） |
| `test_a_limit_stops_early_and_the_rest_is_picked_up_next_time` | **止めた時点までが残る**（塊ごとに確定） |
| `test_thumbnails_that_are_too_small_are_skipped_and_counted` | **小さすぎる顔を引き伸ばさない。** 版が古いまま残り照合の対象外 |
| `test_faces_without_a_thumbnail_are_not_counted_as_targets` | 材料が無いものを分母に入れない |
| `test_the_iterator_does_not_skip_rows_while_they_are_being_rewritten` | **`OFFSET` で送ると顔を飛ばす**（キーセット法で進む） |
| `test_a_dry_run_writes_nothing` | `--dry-run` はDBに書かない |
| `test_the_summary_says_what_was_left_behind` | 残したものを報告する |
| `test_progress_reaches_the_end` | 分母に届く |
| `test_match_only_sees_the_current_version` | **版の違う特徴量が照合に混ざらない** |
| `test_a_face_keeps_matching_itself_after_the_rebuild` | 作り直した手本で紐づけが成り立つ |

### `test_matcher_metrics.py` — 距離尺度がモデルの属性であること（11件）

| テスト | 内容 |
|---|---|
| `test_the_assign_score_floor_matches_the_documented_formula` | **仕様書の式と実装がずれない。** 基準距離を書き写していたため既定を変えたとき式だけ残った |
| `test_the_defaults_come_from_the_active_model` | **閾値もマージンも書き写さない** |
| `test_euclidean_distances_are_the_plain_geometry` | dlib の尺度 |
| `test_cosine_distances_ignore_the_length_of_the_vector` | **L2 正規化してから比べる** |
| `test_cosine_distances_stay_inside_the_expected_range` | 0〜2 に収める（丸め誤差で負にしない） |
| `test_a_zero_vector_does_not_divide_by_zero` | 壊れた特徴量が来ても落ちない |
| `test_an_unknown_metric_is_refused` | **尺度を黙って既定にしない** |
| `test_the_metric_defaults_to_the_active_model` | 省略時はいま使うモデルの尺度 |
| `test_the_histogram_cap_follows_the_metric` | 分布の刻みの上限も尺度で変わる |
| `test_every_known_model_has_a_usable_description` | 記述の取りこぼしを防ぐ |
| `test_an_unknown_version_cannot_be_resolved` | 知らない版は引けない |

### `test_face_alignment.py` — 5点整列（7件）

**整列は1位正解率で +16.6pt を持っている。** 静かに壊れるとモデルを替えた意味が半分消える。

| テスト | 内容 |
|---|---|
| `test_the_transform_puts_a_rotated_face_back_on_the_template` | 相似変換がテンプレートへ重なる |
| `test_the_transform_never_mirrors_the_face` | **鏡像を許さない**（左右の取り違えを隠す） |
| `test_the_eyes_and_the_mouth_corners_are_swapped_together` | **片方だけ入れ替えると対応が崩れる** |
| `test_points_already_in_order_are_left_alone` | 並びが正しいものは触らない |
| `test_landmarks_are_scaled_to_pixels` | FaceMesh の相対座標を画素へ |

### `test_dates.py` — 日付の読み取りと年齢（19件）

| テスト | 内容 |
|---|---|
| `test_readable_dates_are_parsed`（3件） | 撮影日時・誕生日の両方の形 |
| `test_broken_values_are_treated_as_missing`（7件） | `0000-00-00` と `TTTT-TT-TT` の両方 |
| `test_the_length_of_a_broken_value_is_not_a_safe_check` | **長さでは弾けない**ことを数字で固定 |
| `test_age_is_counted_from_the_birthday`（4件） | 誕生日前は上げない。誕生前は負 |
| `test_the_age_is_not_guessed_when_something_is_missing`（4件） | 片方でも欠けたら計算しない |
| `test_the_gui_still_exposes_the_same_functions` | **`gui.parse_date` が同一物であること**（写しではない） |
| `test_the_sql_side_keeps_the_same_judgement` | **SQL 側の写しと答えがそろう**（表示と並び順が食い違わない） |

### `test_folder_dates.py` — フォルダ名から撮影時期を起こす（37件。#65）

| テスト | 内容 |
|---|---|
| `test_the_folder_name_is_read_to_the_month_at_most`（15件） | 読み方の規約。**`YYMM` を `MMDD` と読まない**・`DD=00`・**ファイル名を読まない**（Issue 本文の誤読2例）・親の区間に収まる読み方だけ・日付の無いサブフォルダは年だけ |
| `test_a_range_never_leaves_its_calendar_year` | 区間が1つの暦年に収まる（**年齢の絞り込みの SQL の前提**） |
| `test_the_age_is_known_only_when_both_ends_agree` | 区間の途中に誕生日があれば年齢を出さない。終わりまでに生まれていなければ誕生前 |
| `test_exif_wins_over_the_folder_and_broken_exif_falls_back_to_it` | EXIF が先。壊れた EXIF はフォルダ名へ落ちる |
| `test_save_media_writes_the_folder_range_without_touching_the_exif_column` | **推測を `shooting_date` に混ぜない** |
| `test_refresh_replaces_ranges_left_by_an_older_reading` | 読み方を変えても古い区間が残らない |
| `test_a_version_5_database_gains_the_folder_dates_and_keeps_its_faces` | **v5 → v6 で顔が減らない**。列は移行の中でパスから埋まる |
| `test_the_age_filter_sees_the_same_age_as_the_screen` | **年齢の絞り込み（SQL）と画面（`dates.age_at`）の年齢が一致する**（誕生月・閏日・誕生前・年まで） |
| `test_born_by_drops_only_photos_certainly_taken_before_the_birth` | 誕生前の除外は区間の終わりで見る |
| `test_the_month_filter_takes_a_folder_range_only_when_it_fits_whole` | 撮影年月の絞り込みは区間がまるごと入るときだけ |
| `test_the_teacher_age_ignores_the_folder_range` | **手本の年齢には推測を使わない**（使うと正しい自動割り当てが 125 件外れた。利用者が決定） |
| `test_match_drops_a_person_only_when_the_whole_range_is_before_the_birth` | `match` の誕生前の除外 |
| `test_the_screen_marks_an_age_computed_from_the_folder` ほか2件 | 画面で推測と分かる（`(2歳?)`・`撮影時期: …（フォルダ名から推測）`・推測の件数） |
| `test_select_counts_the_year_from_the_folder_before_the_file_time` ほか1件 | `select` は EXIF → フォルダ名 → ファイル日時（撮影日時が空のときだけ） |
| `test_the_measurement_counts_mismatches_per_folder` | 照合スクリプト（`scripts/measure_folder_dates.py`）の集計 |
| `test_select_date_range_includes_a_folder_range_that_ends_on_the_end_day` ほか1件 | **日付だけの `end` はその日の終わりまで**（0時と読んで 12月31日に終わる区間と写真が落ちた。PR #72 のレビュー指摘1） |
| `test_select_reads_unquoted_yaml_dates` | YAML の引用符の無い日付で `TypeError` にならない（指摘3。develop からの不具合） |
| `test_a_leap_day_birthday_gets_the_same_age_window_as_the_screen` | 2月29日生まれの年齢の窓が画面と一致する（指摘4） |
| `test_refreshing_folder_dates_leaves_the_commit_to_the_caller` | 移行の取引の途中で確定しない（指摘5） |

### `test_fetch_models.py` — モデルの取得（9件）

**ネットワークには触らない。** 取得を差し替えて、sha256 の検証と「置かない」判断を見る。

| テスト | 内容 |
|---|---|
| `test_the_sha256_of_a_file_is_computed_in_blocks` | 174MB を一度にメモリへ載せない |
| `test_a_matching_file_is_left_alone` | 一致すれば取得しない |
| `test_a_file_with_the_wrong_contents_is_reported_not_replaced` | **勝手に取り直さない**（利用者が置いたものを消さない） |
| `test_force_replaces_the_file` | `--force` なら取り直す |
| `test_a_download_that_does_not_match_is_never_placed` | **期待と違うものを置かない。** すり替わると結果だけが静かに変わる |
| `test_a_member_is_taken_out_of_the_archive` | ZIP から認識用の1本だけ取り出す |
| `test_check_only_never_downloads` | `--check` は取得しない |
| `test_main_reports_a_failure_with_a_non_zero_code` | 失敗を終了コードで返す |
| `test_the_arcface_entry_matches_what_face_py_looks_for` | **取得する名前と本体が探す名前をそろえる** |

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
| `test_distance_to_similarity`（5件） | **モデルの基準距離**を使った 0-100 への変換とクリップ |
| `test_media_scores_take_the_best_face` ほか2件 | メディアのスコアは最良の顔で代表する |

### `test_matcher.py` — 自動割り当て（28件）

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
| `test_progress_is_reported_before_the_first_chunk` | **照合の前の準備（取り消し・手本の読み込み）でも進み具合を知らせる。** GUI の窓が5秒固まって見えていた |

### `test_evaluation.py` — 精度の実測（12件）

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

### `test_gui_migration.py` — 起動時の移行（7件）

| テスト | 内容 |
|---|---|
| `test_a_current_database_opens_without_asking_anything` | 移行が要らなければ何も出さない |
| `test_choosing_to_run_migrates_the_database` | 「移行を実行」でその場で移行し、起動を続けられる |
| `test_the_confirmation_says_what_is_kept` | **何が残るかを見せてから実行する。** 「破棄」と読める案内を出さない |
| `test_choosing_to_quit_leaves_the_database_alone` | 「終了」ならデータベースに触らない |
| `test_a_failed_migration_does_not_let_the_window_open` | **失敗を成功のように見せない。** どこまで進んだかも出す |
| `test_a_database_whose_version_ran_ahead_is_offered_the_migration` | 版だけ進んで列が足りないDBも起動時に拾う |
| `test_the_dialog_does_not_start_on_the_run_button` | Enter の連打で走り出さない（既定は「終了」） |

### `test_gui_assignment.py` — GUI での割り当て（61件）

| テスト | 内容 |
|---|---|
| `test_unassigned_faces_are_listed` | 未割当の顔が並ぶ |
| `test_face_list_is_paged` | ページ単位で読む |
| `test_assign_and_unassign_faces` | 割り当てと解除 |
| `test_reject_faces_removes_them_from_the_unassigned_list` | 除外 |
| `test_deleting_person_returns_faces_to_the_unassigned_list` | 人物削除で顔は未割当に戻る |
| `test_face_age_dialog_keeps_zero_distinct_from_unset` | 0歳と未設定を区別する |
| `test_the_person_view_pages_through_every_assigned_face` | 割り当て済み一覧のページャ |
| `test_the_age_filter_returns_to_the_first_page` | 絞り込みで1ページ目に戻る |
| `test_assigning_without_an_age_keeps_the_one_already_recorded` | 年齢を指定しない割り当ては年齢を触らない |
| `test_an_age_can_be_cleared_back_to_unset` | 年齢を未設定へ戻せる |
| `test_zero_is_stored_as_zero_and_not_as_unset` | 0歳は0歳として保存される |
| `test_changing_an_age_later_also_offers_the_calculated_value` | あとから直すときも計算値が初期値に入る |
| `test_the_unassigned_list_starts_with_the_newest_photo` | 割り当てる画面は撮影日時の新しい順 |
| `test_the_assigned_list_is_ordered_by_age` | 人物の表示は年齢順（未設定は最後） |
| `test_rebuilding_the_list_does_not_reload_the_preview` | **一覧の作り直しで元写真を読み直さない**（200件の割り当てに17秒かかっていた） |
| `test_the_progress_is_reported_for_every_face` | 進み具合が件数で出る |
| `test_the_cursor_is_restored_even_when_the_work_fails` | **砂時計を戻し忘れない**（失敗しても戻す） |
| `test_setting_the_age_of_many_faces_commits_once` | 年齢をまとめて入れるとき、1件ずつコミットしない |
| `test_a_rejected_face_can_be_put_back_to_unassigned` | **除外を取り消せる。** 以前は誰かに割り当てる以外に戻す手段が無かった |
| `test_an_auto_assignment_can_also_be_put_back` | 自動割当も同じボタンで外せる |
| `test_unassigning_is_blocked_while_showing_unassigned_faces` | 戻す先が無いときは、隠さずに押せなくする |
| `test_putting_a_face_back_says_done` | 戻したあとも「完了」を出す |
| `test_putting_faces_back_does_not_reload_the_preview` | 戻すときも元写真を読み直さない |

### `test_gui_views.py` — 左の一覧が「見るもの」になった画面（28件）

**この画面の作りは「1件あたりの手数を減らす」ためにある**（手作業の量が精度の
上限で、他人の顔の 99.1% が家族の写真に混ざっている。2026-10-08 実測）。
ここのテストは、**手数が増える方向に戻っていないか**を見張る。

| テスト | 内容 |
|---|---|
| `test_the_left_list_puts_the_views_above_the_persons` | 表示3つ → 区切り線 → 人物の順。**表示の combo は残さない**（同じことを2か所で選ばせない） |
| `test_the_left_list_shows_how_much_work_is_left` | 残りの件数が出て、操作のたびに数え直す |
| `test_selecting_a_person_shows_the_faces_assigned_to_them` | **人物を選ぶことが、旧「割り当て済みを確認」。** 別ウィンドウは開かない |
| `test_the_person_filters_only_appear_for_a_person` | 種別・年齢は人物のときだけ出す（誕生日が無いと年齢は計算できない） |
| `test_each_view_starts_with_the_order_that_suits_it` | 表示ごとに既定の並びへ戻す。**同じ表示のあいだは選んだ並びを変えない** |
| `test_dragging_a_person_above_the_views_does_not_move_them` | 表示3行はドラッグで動かない。人物の並び順だけを保存する |
| `test_the_menu_only_offers_what_the_view_can_do` | **その表示でできることだけ**をメニューに出す |
| `test_the_menu_lists_every_person_as_an_assign_target` | 割り当て先はメニューが持つ |
| `test_the_assign_menu_shows_the_age_and_warns_about_photos_before_birth` | 人物名に撮影時の年齢を添え、**誕生前の写真が混ざっていたら印を付ける** |
| `test_a_face_can_be_assigned_from_the_menu_without_selecting_the_person` | **人物を選び直さずに割り当てられる**（以前は「先に人物を選択してください」で止まった） |
| `test_the_digit_keys_assign_to_the_persons_in_order` | **1〜9 の打鍵で割り当てられる。** 番号は左の一覧の並び順 |
| `test_right_clicking_an_unselected_face_selects_it_first` | 右クリックした顔を処理する。**すでに選んでいる顔なら選択を崩さない** |
| `test_the_hint_line_names_the_keys_that_work_here` | 右クリックは目に見えないので、効く打鍵と選択件数を1行で出す |
| `test_the_auto_view_names_the_person_on_each_face` | 全員ぶんの表示では、顔に**誰のものか**と年齢を出す |
| `test_confirming_in_the_auto_view_keeps_each_face_with_its_own_person` | まとめて確定しても、**その顔に付いている人物へ**確定する |
| `test_not_this_person_in_the_auto_view_records_it_per_person` | 「この人物ではない」も顔ごとの人物に記録する |
| `test_confirm_is_blocked_unless_an_automatic_face_is_selected` | 確定は自動割り当てだけに効く。押せない理由はツールチップに出す |
| `test_rejecting_from_the_unassigned_view_does_not_ask` | **毎件通る操作に確認を挟まない**（そこが遅さの正体になる） |
| `test_rejecting_an_assigned_face_asks_first` | 割り当て済みに押すときは確認する（手作業の結果が消える） |
| `test_the_month_range_also_narrows_a_person_s_faces` | 共通の絞り込みが人物の表示にも効く（別ウィンドウには無かった） |
| `test_the_rejection_list_can_also_be_narrowed_and_paged` | 「この人物ではない」の一覧もページ単位で読み、年月で絞れる |
| `test_a_broken_shooting_date_is_not_counted_as_before_birth` | **読めない撮影日時を「誕生前」に数えない**（判断は `dates.parse_date` に1つだけ） |
| `test_the_bulk_event_action_follows_the_view` | 行事のまとめ処理が表示に合わせて意味を変える。**「この人物ではない」では押せない** |
| `test_a_small_thumbnail_at_the_top_does_not_shrink_the_whole_page` | **枠はサムネイルの寸法に任せない**（先頭の顔が小さいとページ全体が縮んでいた） |
| `test_the_cell_leaves_room_for_the_thumbnail_and_two_lines_of_text` | 枠にサムネイルと文字2行が入る |
| `test_both_face_lists_are_built_the_same_way` | 一覧の設定を2か所に書かない（`make_face_list`） |
| `test_orders_that_mean_nothing_in_the_view_cannot_be_chosen` | 未割当では年齢・確信度の並びを押せない（理由はツールチップ）。人物ではどれも選べる |
| `test_the_person_view_is_sorted_by_the_shown_age_on_every_page` | 画面に出ている年齢がページをまたいで若い順に並ぶ |

### `test_recommend.py` — 顔を「この人物に似た順」に並べる（13件）

**点はその人物の手本との最小距離**（#69。2026-10-08 の測定と同じ）。

| テスト | 内容 |
|---|---|
| `test_rank_puts_faces_without_a_distance_last_in_both_directions` | 距離の無い顔は、似た順でも似ていない順でも最後 |
| `test_nearest_distance_ignores_the_face_itself` | **自分自身は手本から外す**（確定済みの顔が全部 0 で並ばなくなるのを防ぐ） |
| `test_nearest_distance_is_infinite_when_only_itself_is_a_teacher` | 比べる相手が自分しか居なければ距離は無い |
| `test_nearest_distance_works_in_chunks` | 候補 × 手本の行列を塊に割っても答えが変わらない。進み具合を知らせる |
| `test_similarity_ranks_unassigned_faces_by_the_nearest_teacher` | 平均ではなく**最も近い手本**との距離で並ぶ |
| `test_teachers_that_could_not_be_aligned_are_not_used` | **整列できない手本は根拠にしない**（顔でないものが上位に来るのを防ぐ） |
| `test_faces_of_another_embedding_version_are_not_compared` | **版の違う特徴量を比べない。** 特徴量の無い顔と一緒に最後へ回す |
| `test_similarity_with_no_teachers_says_so` | 手本が0件なら0を返す（画面が「並べられない」と出す） |
| `test_similarity_only_measures_faces_it_has_not_seen` | **ページを送るたびに計算し直さない** |
| `test_adding_a_teacher_updates_the_ranking_without_measuring_everything_again` | 手本が増えたら、**増えた手本とだけ**比べて並びを良くする |
| `test_removing_a_teacher_measures_everything_again` | 手本が減ったら全部測り直す（最小距離が大きくなりうる） |
| `test_teachers_not_yet_measured_are_measured_before_use` | **GUI で割り当てたばかりの手本（見え方が未計測）を、使う前に測る。** 測らずに使うと横倒しの候補が上位に来た（PR #71 レビュー指摘1） |
| `test_a_teacher_that_cannot_be_measured_is_still_used` | サムネイルが読めない手本は未計測のまま根拠に残す（**測れないと整列できないを混ぜない**） |

### `test_gui_recommend.py` — 人物の未割当を似た順に見る画面（12件）

**探す時間を削るための画面**（他人の顔の 99.1% が家族の写真に混ざっていて、まとめて消せない）。

| テスト | 内容 |
|---|---|
| `test_unassigned_faces_of_a_person_are_listed_most_similar_first` | 種別「未割当」を選ぶと、**並びを選び直さなくても**似た順に並ぶ |
| `test_the_reverse_order_lists_the_least_similar_first` | 似ていない順 |
| `test_faces_that_cannot_be_this_person_are_not_offered` | **誕生前の写真と「この人物ではない」と決めた顔は、どれだけ似ていても出さない** |
| `test_a_person_without_teachers_says_the_order_is_not_by_similarity` | **手本が0件の人物では、そうと分かる表示を出す**（黙って id 順にしない） |
| `test_assigning_from_the_list_improves_the_order_right_away` | 割り当てた顔がすぐ手本になり、次の並びに効く |
| `test_the_similarity_is_kept_while_paging` | ページ送りで計算し直さない |
| `test_similarity_orders_need_a_person` | 比べる人物が居ない表示では似た順を押せない。人物の未割当では確信度の並びを押せない |
| `test_a_persons_own_faces_can_be_listed_least_similar_first` | 似ていない順で、割り当ての誤りを探せる（自分自身は手本から外す） |
| `test_the_menu_on_a_persons_unassigned_faces_offers_what_makes_sense` | 右クリックは「この人物ではない」と「誰でもない顔」。戻す先は無い |
| `test_not_this_person_removes_the_face_from_the_candidates` | 「この人物ではない」と押した顔はその人物の候補から消える |
| `test_rejecting_a_persons_unassigned_face_does_not_ask_for_confirmation` | 未割当の除外に確認を挟まない（毎件通る操作） |
| `test_switching_back_from_unassigned_restores_the_persons_order` | 種別を戻すと人物の既定の並び（年齢順）に戻る |

### `test_gui_match.py` — `match` を画面から流す（8件）

| テスト | 内容 |
|---|---|
| `test_match_runs_from_the_window_and_refreshes_the_counts` | 画面から流せて、終わったら左の件数まで出し直す |
| `test_the_backup_is_taken_when_chosen` | 控えを選んだら、**流す前の状態**がそのまま入っている |
| `test_no_backup_is_written_when_declined` | 控えを外したら書かない |
| `test_cancelling_the_dialog_changes_nothing` | 「やめる」なら何も変えない |
| `test_without_teachers_it_says_so_instead_of_running` | 手本が無ければ流さずにそう言う |
| `test_the_progress_bar_reaches_every_candidate` | 進み具合が件数で出て、最後まで届く |
| `test_the_dialog_backs_up_by_default_and_does_not_start_on_enter` | 控えは既定で取る。既定のボタンは「やめる」 |
| `test_the_summary_names_each_person_and_the_backup` | 結果に人物ごとの件数（多い順）と控えの場所を出す |

### `test_gui_event_clusters.py` — 行事で絞って束ねる画面（23件）

**1回の操作が数百件に効く画面**なので、手本を巻き込まないことを中心に固定する。

| テスト | 内容 |
|---|---|
| `test_the_picker_offers_events_not_folders` | 同じフォルダが日ごとに分かれて並ぶ |
| `test_the_picker_filter_matches_what_is_on_screen` | 絞り込みは画面に出ている文字で照合する |
| `test_the_main_window_filters_by_the_chosen_event` | 選んだ行事で一覧が絞られる |
| `test_an_event_without_a_readable_day_is_filtered_by_the_undated_mark` | **日が読めない行事を「日で絞らない」と取り違えない**（一覧の経路） |
| `test_the_cluster_dialog_bundles_only_undated_faces_of_an_undated_event` | **同じことを束ねる画面の経路でも守る**（ここが抜けていた） |
| `test_the_two_paths_turn_an_event_into_the_same_filter` | 両方の経路が `event_filters` の同じ変換を通る |
| `test_the_dialog_bundles_only_the_chosen_day` | 束ねるのはその日の顔だけ |
| `test_near_faces_land_in_one_cluster` | 近い顔が1つの束になる |
| `test_rejected_and_auto_faces_are_left_out_of_the_bundles` | 除外と自動割当は束ねない |
| `test_a_cluster_shows_the_teacher_it_contains` | 束に混ざった手本の名前を出す |
| `test_assigning_a_cluster_never_touches_the_teacher_in_it` | **手本を巻き込まない**（いちばん大事な一線） |
| `test_a_cluster_becomes_done_after_it_is_assigned` | 押した束は「済」になる（二度押さない） |
| `test_rejecting_a_cluster_only_rejects_the_pending_faces` | まとめて除外も未判断の顔だけ |
| `test_only_one_page_of_thumbnails_is_read_at_a_time` | **サムネイルは1ページ分だけ読む** |
| `test_people_born_after_the_event_are_not_offered` | その日に生まれていない人物は選べない |
| `test_people_without_a_birth_date_stay_in_the_list` | 誕生日が未設定の人物は残す |
| `test_too_many_faces_is_reported_instead_of_truncated` | 上限超過を黙って切らない |
| `test_bulk_reject_covers_the_whole_event_but_not_other_days` | 行事まるごとの処理が別の日に漏れない |
| `test_bulk_buttons_stay_disabled_until_an_event_is_chosen` | **行事を選ぶまで押せない**（押し間違いで全件に効くのを防ぐ） |
| `test_the_bulk_button_changes_meaning_with_the_view` | 表示に応じてボタンの意味が変わる |
| `test_splitting_a_cluster_uses_a_tighter_distance` | 大きい束を割れる（実データは 380 件の束を作る） |
| `test_a_cluster_that_cannot_be_split_says_so` | 割れなかったことを黙らない |
| `test_a_single_face_cluster_cannot_be_split` | 1件の束は割れない |

### `test_gui_person.py` — 人物編集とプレビュー（59件）

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
| `test_the_folder_is_shown_relative_to_the_root` | フォルダは root からの相対。日付の手がかりになる |
| `test_a_photo_outside_every_root_keeps_its_full_path` | root の外は絶対パスのまま |
| `test_the_folder_is_shown_without_a_root` | root が無くても動く |
| `test_selecting_a_face_fills_the_information_under_the_preview` | 顔を選ぶと情報欄が埋まる |
| `test_the_information_is_still_shown_when_the_original_is_gone` | **元写真が開けないときこそ出す**（出どころはDB） |
| `test_a_broken_exif_date_is_treated_as_missing` | `0000:00:00` を書くカメラがある（実データ55件）。持っていない扱いにしてファイル日時へ落とす |
| `test_another_shape_of_broken_exif_is_also_treated_as_missing` | **先頭の文字だけを見て弾かない。** `TTTT-TT-TTTTT:TT:TT` が素通りして画面に出ていた（実データ67件） |
| `test_a_photo_directly_under_a_root_says_so` | `フォルダ: .` では読めない |
| `test_a_relative_root_is_anchored_to_the_settings_file` | **起動した場所で表示が変わらない**（相対の起点は設定ファイル） |
| `test_an_absolute_root_is_left_alone` | 絶対パスと未設定は触らない |
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
| `test_the_suggested_age_is_withheld_when_a_face_has_no_shooting_date` | **「分からない」を「反対しない」にしない。** 日時の無い顔が1件でもあれば初期値を出さない |
| `test_the_suggested_age_is_withheld_when_a_shooting_date_is_broken` | 壊れた EXIF も「分からない」として扱う |
| `test_the_selection_notice_says_how_many_dates_are_unknown` | 初期値が入らない理由（不明な件数）を出す |
| `test_a_broken_exif_date_does_not_appear_in_the_selection_notice` | `0000:00:00` を撮影日時として画面に出さない |
| `test_the_age_appears_right_after_the_birth_date_is_registered` | **誕生日を登録したら、その場で年齢の行が出る** |
| `test_dropping_the_person_selection_also_drops_the_age_line` | 前の人物の年齢を残さない |
| `test_a_new_person_is_selected_so_the_age_shows_immediately` | 追加した人物も選ばれた状態になる |
| `test_the_person_order_can_be_changed_and_is_remembered` | **並べ替えた順が開き直しても残る**（`Person.display_order`） |
| `test_the_person_list_accepts_a_drag` | ドラッグで動かせる設定になっている |
| `test_a_new_person_goes_to_the_end_of_the_order` | 追加した人物を先頭に割り込ませない |
| `test_a_person_who_was_never_reordered_keeps_the_name_order` | 並べ替えたことのない人物は名前順のまま |
| `test_a_new_person_goes_to_the_end_even_before_anyone_was_reordered` | **移行直後（全員 NULL）でも末尾に来る。** 0 を振ると先頭に割り込む |
| `test_persons_added_to_a_new_database_keep_their_registration_order` | 新しいDBでは登録順 |
| `test_the_preview_says_done_and_fades_after_an_assignment` | **割り当てた顔が濃いまま残らない。** 「完了」を出して薄くする |
| `test_the_done_label_sits_on_top_of_the_photo` | 札は顔写真に重ねて中央 |
| `test_choosing_another_face_clears_the_done_label` | 次の顔を選んだら消す |
| `test_rejecting_a_face_also_says_done` | 除外でも同じ扱い |
| `test_dimming_leaves_the_original_alone` | 薄くするのは複製。元の画像を書き換えない |

### `test_selection.py` — 抽出とコピー（29件）

| テスト | 内容 |
|---|---|
| `test_select_media_filters_by_rule` | 日付と `include_video` |
| `test_load_rule_reads_yaml` / `test_load_rule_reads_an_empty_yaml_as_no_conditions` | ルールは YAML。空なら条件なし |
| `test_load_rule_refuses_json_and_names_the_yaml_to_write` | **JSON のルールは止める**（#27。古いファイルを指したままの設定に気づけるように） |
| `test_load_rule_refuses_a_yaml_that_is_not_a_mapping` | 辞書でない YAML は止める |
| `test_the_sample_rule_is_yaml_and_readable` | 管理しているサンプルがそのまま読める |
| `test_load_rule_raises_for_a_missing_file` | 無いファイル |
| `test_family_only_keeps_media_where_a_family_member_is_assigned` | `family_only` は家族の顔が写っているか |
| `test_family_only_keeps_a_photo_whose_only_family_face_is_blurred_and_turned_away` | **点 0 の家族の写真も `family_only` で残る**（PR #70 のレビュー指摘1） |
| `test_a_blurred_family_photo_comes_after_a_crisp_one` | **ボケた家族の写真は後ろ**（利用者の要望の核心） |
| `test_a_profile_comes_after_a_frontal_face` | 横顔・整列できない顔は正面の後ろ |
| `test_a_smile_ranks_above_a_straight_face` | 笑顔が上 |
| `test_a_crisp_stranger_does_not_lift_a_blurred_family_photo` | **点は家族の顔だけから作る** |
| `test_more_clear_family_members_rank_higher_but_blurred_ones_do_not_count` | はっきり写った家族が多いほど上。ボケた家族は数えない |
| `test_a_change_made_in_the_gui_reaches_select_without_running_match` | 保存済みの `family_score` を読まない |
| `test_match_writes_the_same_score_that_select_uses` | `match` が書く点と `select` の点は同じ式 |
| `test_select_measures_family_faces_that_were_never_measured` | 未計測の家族の顔は `select` が測る |
| `test_select_warns_when_auto_assignments_came_from_an_older_rule` | 古い規則の自動割り当てを知らせる（`Face.assign_rule`） |
| `test_media_without_family_are_ordered_by_quality_then_smile` | 家族のいない写真は画質 → 笑顔の順 |
| `test_count_per_year_limits_each_year_independently` | 年ごとの上限 |
| `test_count_per_year_keeps_the_best_of_each_year` | 年内では上位から採る |
| `test_remove_duplicate_keeps_one_row_per_file_hash` | ハッシュ一致の重複除去 |
| `test_duplicate_groups_fall_back_to_the_path_when_there_is_no_hash` | ハッシュが無いときはパスで分ける |
| `test_media_year_prefers_the_shooting_date_and_falls_back_to_created_time` | 年の決め方 |
| `test_date_filter_falls_back_to_created_time_and_drops_unreadable_dates` | 読めない日付は範囲外 |
| `test_copy_keeps_the_layout_below_the_root` | 相対パスを再現する |
| `test_copy_flattens_media_that_lives_outside_the_root` | 基準の外はファイル名だけにする |
| `test_copy_skips_entries_without_a_path_and_reports_progress` | パスが無い行を飛ばす |
| `test_copy_makes_room_when_the_name_is_taken` | 名前の衝突で連番を付ける |

### `test_appearance.py` — 顔の見え方・手本の選別・年齢の上限（21件。#66）

| テスト | 内容 |
|---|---|
| `test_a_frontal_face_has_no_yaw_and_a_turned_face_has_some` | 向き（鼻のずれ ÷ 両目の間隔） |
| `test_a_blurred_face_is_less_sharp_than_a_crisp_one` | 鮮明さ（ラプラシアン分散） |
| `test_a_face_without_landmarks_is_recorded_as_not_aligned` | 目印が取れない顔は「整列できない」 |
| `test_nothing_is_recorded_when_the_landmark_model_is_unavailable` | **測れないときは書かない** |
| `test_filling_measures_only_assigned_faces_and_can_skip_writing` | 割り当てのある顔だけ測る。`write=False` は書かない |
| `test_a_face_close_only_to_an_unaligned_teacher_is_left_unassigned` | 整列できない手本は根拠にしない |
| `test_an_unaligned_teacher_still_blocks_a_stranger_as_the_runner_up` | **対抗馬としては残す** |
| `test_the_score_comes_from_the_aligned_teacher_that_decided` | `assign_score` は受け入れを決めた手本から |
| `test_unmeasured_teachers_are_measured_before_matching` | 未計測の手本は `match` が測る |
| `test_auto_assignments_record_the_rule_and_manual_ones_clear_it` | `assign_rule` は auto の行だけ |
| `test_evaluate_applies_the_same_rule` / `test_evaluate_does_not_write_what_it_measures` | `evaluate` も同じ規則・書かない |
| `test_a_version_4_database_gains_the_appearance_columns_and_keeps_its_faces` | **v4 → v5 で顔が減らない** |
| `test_the_age_limits_only_tighten` | 年齢の上限は締めるだけ |
| `test_a_baby_teacher_needs_a_closer_face_than_an_adult_teacher` | 8歳以下の手本は 0.35 |
| `test_a_teacher_of_unknown_age_is_limited_to_0_40` | 年齢不明は 0.40 |
| `test_the_confirmed_age_wins_over_the_calculated_one` | 年齢は確定値が優先 |
| `test_a_rejected_young_teacher_does_not_hand_the_face_to_someone_else` | **上限は勝者とマージンに効かせない** |
| `test_evaluate_applies_the_age_limits_too` | `evaluate` も年齢の上限を効かせる |
| `test_an_older_teacher_accepts_a_face_even_when_a_baby_teacher_is_nearer` | **手本を足して割り当てが減らない**（どれか1件が上限以内なら受け入れる。PR #70 のレビュー指摘2） |
| `test_a_face_beyond_every_teachers_own_limit_is_still_left_unassigned` | 上限以内の手本が無ければ受け入れない |

### `test_converter.py` — HEIC → JPEG（14件）

| テスト | 内容 |
|---|---|
| `test_convert_writes_jpeg_beside_the_source_and_keeps_the_original` | 元ファイルを変更しない |
| `test_convert_finds_sources_recursively_and_ignores_other_extensions` | 再帰探索と対象外の無視 |
| `test_convert_skips_when_an_identical_jpeg_already_exists` | 2回目の実行が無害 |
| `test_convert_numbers_the_output_when_a_different_jpeg_exists` | 別の写真なら連番 |
| `test_convert_reports_progress_for_every_source` | 進捗 |
| `test_convert_raises_for_a_missing_directory` | 無いディレクトリ |
| `test_convert_asks_before_continuing_when_the_output_cannot_be_written` | 書き込み失敗時の確認 |
| `test_difference_is_small_for_the_same_picture_and_large_for_another` | 同一画像の判定 |
| `test_same_image_requires_the_same_dimensions` | 寸法が違えば別の写真（#79） |
| `test_convert_skips_a_textured_photo_on_the_second_run` | 模様のある写真で2回目以降に `_1.jpg` を作らない。単色では JPEG の画素ずれが起きず見逃していた（#79） |
| `test_convert_numbers_the_output_when_a_similar_but_different_photo_exists` | 模様の違う写真なら連番（#79） |
| `test_the_measurement_separates_the_same_photo_from_the_next_one` | 閾値を測るスクリプト（`scripts/measure_heic_duplicates.py`）の集計 |
| `test_same_image_is_false_when_a_file_cannot_be_read` | 読めないファイル |
| `test_next_output_path_walks_past_occupied_numbers` | 空いている連番を探す |

### `test_cli_commands.py` — サブコマンドの配線（28件）

| テスト | 内容 |
|---|---|
| `test_convert_heic_does_not_need_a_database` | DB を使わないコマンドは DB パスを要求しない |
| `test_commands_that_need_a_database_still_say_so` | 使うコマンドはこれまで通り要求する |
| `test_init_db_creates_a_usable_database` | `init-db` |
| `test_migrate_reports_that_a_fresh_database_is_current` | `migrate` は移行済みDBに何もしない |
| `test_scan_arguments_reach_the_scanner` | `scan` の全オプションが下へ届く |
| `test_scan_takes_several_sources_in_the_given_order` | **`--source` は何度でも書け、書いた順に root ごとに走査する**（#24） |
| `test_scan_reads_the_source_roots_from_the_settings` | 設定の `source_roots`（配列） |
| `test_scan_refuses_nested_sources_before_touching_anything` | 入れ子の root は何も走査せずに止める |
| `test_scan_falls_back_to_the_roots_recorded_in_the_database` | **設定を失っても DB に記録された root で走査する**（#24 の動機） |
| `test_scan_without_any_source_names_the_setting_to_write` | root がどこにも無ければ `source_roots` を書くよう言う |
| `test_match_arguments_reach_the_matcher` | `match` の全オプションが下へ届く |
| `test_the_database_path_falls_back_to_the_settings_file` | 設定ファイルへのフォールバック |
| `test_evaluate_arguments_reach_the_evaluation` | `evaluate` の全オプションが下へ届く |
| `test_a_threshold_that_cannot_be_read_stops_instead_of_being_dropped` | 読めない閾値を黙って捨てない |
| `test_evaluate_runs_end_to_end_on_a_database_with_assigned_faces` | CLI から実際に数字が出るところまで通す |

### `test_config.py` — 設定の探索（25件）

**設定は YAML だけを読む**（#27）。古い `app_settings.json` は読まずに WARNING で変換を
促し、CLI は「DB のパスが要る」ではなくそのことを言う。環境変数が JSON を指していても
読まない。YAML と JSON が両方あれば YAML を読んで黙る。注釈と日本語のパスが読めること。
GUI も古い JSON のことを言い、止める文は WARNING の案内を繰り返さない。タブ字下げの JSON を
改名しただけならタブを名指しする。`select --help` が JSON を受け付けると言わない（PR #74 のレビュー）。
環境変数 → カレントディレクトリ → リポジトリ直下 の順に探すこと、優先順位、
壊れたファイルでもコマンドが止まらないこと。リポジトリ直下の候補を
editable install のときだけ出すこと（通常のインストールでは `REPO_ROOT` が
`lib/python3.x` を指すので、**`REPO_ROOT` 自身を見ても判別できない。
モジュールの位置で判断する**）。同じ場所を2度並べないこと。
検出元は `source_roots`（配列。1つなら文字列でもよい。**古い `source_root` は読まずに WARNING**（PR #75 で利用者が決めた）。#24）。

### `test_source_roots.py` — 検出元の root を複数持つ（19件。#24）

| テスト | 内容 |
|---|---|
| `test_two_roots_are_both_scanned_and_recorded` | 2つの root を走査し、両方を `ScanRoot` に記録する |
| `test_a_missing_root_does_not_make_the_other_roots_media_prunable` | **片方の root だけ走査しても、もう片方のメディアは消えない** |
| `test_the_safety_valve_still_works_per_root` | 2割の安全弁は root ごとに効く |
| `test_nested_roots_are_refused` / `test_the_same_root_written_twice_is_scanned_once` | 入れ子は止める・重複は1回 |
| `test_a_root_given_through_a_symlink_is_recorded_by_its_real_path` | 記録は実体のパス（`Media.path` とそろえる） |
| `test_scanning_a_year_folder_inside_a_root_does_not_record_a_new_root` | 年フォルダだけの走査は root にしない |
| `test_scanning_a_parent_of_a_recorded_root_stops_before_reading_anything` | **記録済みの root を含む親は走査する前に止める**（PR #75 のレビュー (a)・利用者の決定。以前は記録が親だけに置き換わって戻らなかった） |
| `test_a_year_folder_inside_a_recorded_root_is_still_scanned` | 止めるのは親だけ。年フォルダは通す |
| `test_recording_an_outer_root_never_drops_the_inner_records` / `test_a_sibling_with_a_common_prefix_is_not_taken_for_an_inner_root` | 記録は消さない（両方残れば入れ子の検査で止まる）。`/mnt/Photo2` は `/mnt/Photo` の内側ではない |
| `test_a_root_that_does_not_exist_stops_before_any_root_is_scanned` | **無い root があれば、どの root も走査せずに止める**（PR #75 のレビュー指摘1） |
| `test_an_aborted_scan_does_not_record_its_root` | 中断した走査は root を書かない |
| `test_a_version_6_database_gains_the_root_table_and_keeps_its_faces` | **v6 → v7 で顔が減らない。root は推定しない** |
| `test_with_several_roots_the_folder_is_prefixed_with_the_root_name` / `test_with_one_root_the_folder_is_shown_as_before` | GUI の表示。root が複数なら root の名前を付ける |
| `test_the_window_falls_back_to_the_roots_recorded_in_the_database` | 設定に root が無ければ GUI も記録を使う |
| `test_select_copies_relative_to_the_root_that_holds_each_photo` | `select` は含む root からの相対。同じ名前は連番 |
| `test_select_copies_a_photo_outside_every_root_by_its_name` | **root の外のメディアで `select` が落ちていた**（`str` に `as_posix()`。本筋の外で直した） |

### `test_heic_excluded.py` — scan 以降から HEIC を外す（6件。#26）

| テスト | 内容 |
|---|---|
| `test_a_heic_next_to_its_jpeg_is_not_registered` | HEIC は登録しない（JPEG だけ） |
| `test_existing_heic_rows_are_removed_without_tripping_the_safety_valve` | **既存の HEIC の行は消え、root の大半でも2割の安全弁で止まらない**（実データの `な携帯` は 33%） |
| `test_the_safety_valve_still_counts_real_files_that_vanished` | 消えた JPEG は今までどおり安全弁で止まる |
| `test_no_prune_keeps_the_heic_rows` | `--no-prune` なら消さない |
| `test_a_folder_of_only_heic_is_treated_as_having_no_media` | **scan は HEIC を一切見ない。** HEIC だけのフォルダは「メディアが1件も無い」で止まる（PR #76 のレビューで利用者が決めた） |
| `test_heic_rows_are_not_counted_by_the_missing_file_check_on_its_own` | `prune_missing_media` 単体でも HEIC の行を安全弁に数えない（PR #76 のレビュー指摘4） |

### `test_cli_progress.py` — 進捗表示（7件）

2行の書き換え、直近のエラーの保持、長いエラーの切り詰め、改行の潰し。

### `test_migration.py` — スキーマの移行（25件）

**v1 → v2**: Media と Person を温存し Face と AnalysisResult を破棄すること、
冪等性、旧スキーマのまま使おうとしたときのエラー、特徴量 BLOB の往復。

**v2 → v3**: `Person.birth_date` を足すだけで、**顔・解析結果・検出済みの状態が
1件も減らないこと**。v1 の再構築経路へ流すと落ちることを確認済み。
案内の文面が「破棄します」にならないこと（消えると読めると実行をためらう）。

**v5 → v6** は `test_folder_dates.py` にある（列を足してパスから埋める。顔は減らない）。

**v3 → v4**: `Person.display_order` を足すだけ。**実データが通る唯一の経路**なので、
顔・手本・誕生日が減らないことと、移行直後の並び（全員未設定＝名前順、追加は末尾）を
固定している。

**版の印だけが進むのを防ぐ**: 移行していないDBをアプリが開いても版を刻まないこと、
すでに刻まれてしまったDBを**実際の列**を見て直せること、列の一覧が `SCHEMA` から
導かれていること。**足す列は `db.ADDABLE_COLUMNS` の1行で決まり**、移行コードに
書き足さない（書き忘れがその事故を生む）。v1 の再構築経路も最後に同じ処理を通す。

移行前の案内が **VACUUM するかどうかを言うこと**（`--no-vacuum` は v1 からの
移行でしか効かない。黙って効かない引数を作らない）。

**控えは SQLite の backup で取る**（`test_the_backup_keeps_writes_that_are_still_in_the_wal`）。
ファイルを複写すると WAL にだけある書き込みが抜ける。控えの比較は**中身**
（`iterdump`）で行う（backup はヘッダの変更カウンタが変わるので、バイト列は揃わない）。

移行前に何件消えるかを数える `describe_migration`、バックアップの保存先の
指定（親ディレクトリが無くても作る）と既定の日時付きの名前、
`vacuum=False` / `make_backup=False`、進捗メッセージ、空のファイルへの
スキーマ作成、存在しないDBを指したときのエラー。

### `test_embedding_measurement.py` — 特徴量モデルの比較と他人誤認率（29件＋`models` 1件）

**測定の道具がおかしいと、間違った結論で全件再計算に進む。**

| テスト | 内容 |
|---|---|
| `test_euclidean_and_cosine_are_both_supported` | dlib はユークリッド、ArcFace はコサイン |
| `test_cosine_ignores_the_length_of_the_vector` | **L2 正規化してから比べる**。長さに左右されない |
| `test_an_unknown_metric_is_refused` | **尺度を黙って既定にしない**（dlib の尺度を他モデルに当てる事故を防ぐ） |
| `test_top1_accuracy_excludes_faces_from_the_same_photo` | **同じ写真の顔を候補から外す。** 外さないと正解率が水増しされる（0% が 40% に見える） |
| `test_top1_accuracy_reports_nothing_when_every_face_shares_one_photo` | 評価できる顔が無ければ件数0を返す |
| `test_pairs_are_split_into_within_event_and_across_events` | **プールした平均を出さない。** ここを混ぜたのが 9/20 の読み違いの原因 |
| `test_faces_without_a_readable_date_are_left_out_of_both_sides` | **分からないものをどちらかに混ぜない**（未割当の 10.4% が該当） |
| `test_pairs_from_the_same_photo_are_skipped` | ペアの集計でも同じ写真を外す |
| `test_the_gap_is_how_far_strangers_sit_from_the_same_person`（2件） | 差の定義と、片側が空のとき |
| `test_the_sweep_finds_a_threshold_that_separates`（2件） | 閾値の振り方と、振れないとき |
| `test_the_transform_puts_a_rotated_face_back_on_the_template` | 5点整列の相似変換。**整列が +16.6pt を持っている** |
| `test_the_transform_never_mirrors_the_face` | **鏡像を許さない**（左右の取り違えを整列が隠してしまう） |
| `test_the_eyes_and_the_mouth_corners_are_swapped_together` | **片方だけ入れ替えると対応が崩れる** |
| `test_points_already_in_order_are_left_alone` | 並びが正しいものは触らない |
| `test_landmarks_are_scaled_to_pixels` | FaceMesh の相対座標を画素へ直す |
| `test_manual_faces_are_read_without_writing_to_the_database` | **実データに書かない**（`mode=ro` で開く） |
| `test_an_event_is_a_folder_and_a_day` | 行事＝フォルダ×日。**`TTTT-TT-TT` は10文字あるので長さで弾けない** |
| `test_folder_of_strips_the_file_name` | フォルダの取り出し |
| `test_the_thumbnail_is_the_only_image_source` | **元写真を読まない**（NFS 再読み込み 5.1時間を避ける） |
| `test_measure_counts_what_it_could_not_embed` | **作れなかった顔を黙って落とさない**（母集団が変わると比較が成り立たない） |
| `test_measure_gives_up_when_fewer_than_two_faces_remain` | 2件未満では測らない |
| `test_the_stored_embedding_variant_uses_the_value_in_the_database` | (a) の経路 |
| `test_dlib_can_be_recomputed_from_the_thumbnail` | (b) の経路。サムネイルだけで作り直せる |
| `test_arcface_is_skipped_when_the_model_is_absent` | **モデルが無いときに黙って (a)(b) だけ出さない。** 理由を残す |
| `test_the_report_lists_every_variant_and_both_kinds_of_gap` | 報告に両方の差と、距離を直接比べない断り書きが載る |
| `test_the_report_survives_an_empty_gap` | 手本が少なく片側が0件でも落ちない |
| `test_main_refuses_a_database_that_is_not_there` | DB が無ければ理由を言って終了 |
| `test_main_writes_a_report_and_notes_the_missing_model` | モデル不在を報告に書き残す |
| `test_same_photo_pairs_separate_labelled_from_assumed` | **「同じ写真なら別人」は仮定。** 手本どうしのペアと分けて数える。**同一人物のペアを誤りに数えない** |
| `test_photos_with_a_single_face_are_ignored` | 1枚1顔の写真はペアにならない |
| `test_faces_on_another_version_are_left_out` | **版の違う特徴量を混ぜない** |
| `test_the_report_keeps_the_assumption_visible` | 報告に「高めに出る」断り書きを残す |
| `test_the_real_arcface_returns_512_dimensions`（`models`） | 実物の ONNX が 512次元を返す（環境依存・合否に含めない） |

### `test_worklog_archive.py` — 作業履歴の切り出し（16件）

直近20件を残して年ごとに切り出すこと、索引の作り直し、冪等性、`--check`。
**日付エントリ以外の節を消さないこと**（前にあるものは前書きとしてその場に残し、
あとにあるものは末尾へ移す）。切り出し済みの本文を後から書き換えないこと。

### `test_pytest_summary.py` — 回帰テストの集計行（15件）

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
| `test_a_branch_name_at_the_end_of_an_english_sentence` | **末尾の記号を名前に含めない**（`feature/#59_x.` では一致しない） |
| `test_a_branch_name_next_to_japanese_punctuation_is_still_found` | **端を削る方式では取りこぼす。** 全角括弧が直後に付くと一致せず、書いてあるのに「浮いている」と鳴った |
| `test_the_branch_name_does_not_swallow_the_text_after_it` | 逆に後ろの文を名前に巻き込まないこと |
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

### `test_docs_stay_stable.py` — 文書に実装の数字を置かない（9件）

`TEST_CASES.md` の**見出しと表がずれていないこと**も見る。見出しが自分の表の
下に落ちると表が前の節にぶら下がり、**表の途中の空行はそれ以降を表でなくする**
（GitHub の描画器の仕様。実際に11行が表から外れた）。

`CLAUDE.md` と `README.md` に、テストの件数や成功件数が書かれていないことを
見る。書いてしまうと**関係のない変更のたびに更新が要り**、忘れれば
いちばんよく読まれる2つの文書が静かに嘘になる。番人自身が働くことも
確かめている（数字を戻すと落ちること、`N passed / M failed` の書式は通ること）。

**スキルや文書が指す `CLAUDE.md` の節が実在すること**も見る
（`test_a_skill_does_not_point_at_a_section_that_does_not_exist`）。節を足したり
番号を振り直したりすると指し先が黙ってずれ、読み手は**そんな節が無いことにも
気づけない**。フェーズ文書のリンクを見張っているのと同じ理由
（`test_plan_stays_true.py`）。

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
| `config.REPO_ROOT` | 空のディレクトリ。開発機の `config/app_settings.yml` をテストから見えなくする |
| `cli._progress_started` | 各テストの前後でリセット。進捗表示の大域状態がテストの順序に依存した差を作らないようにする |
| `QMessageBox` | 何もしない。モーダルで止まらないようにする |

`tests/helpers.py` の `write_image()` に色を指定すると「同一人物」「別人」を
作り分けられる。`write_heic()` は HEIC を書き出す（encoder が無い環境ではスキップ）。
`write_video()` は数フレームの mp4 を書き出す。**色は BGR で与える**ので、
先頭フレームの色を見れば `read_rgb` が並びを直しているか分かる
（コーデックが無い環境ではスキップ）。

GUI のテストは `QT_QPA_PLATFORM=offscreen` で動くので、画面が無くても実行できる。
