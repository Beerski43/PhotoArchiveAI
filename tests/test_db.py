from pathlib import Path

from photoarchive_ai import db


def _media_record(path="2025/01/test.jpg", file_hash="dummyhash"):
    return {
        "path": path,
        "filename": Path(path).name,
        "type": "image",
        "file_hash": file_hash,
        "file_size": 12345,
        "created_time": "2025-01-01T00:00:00",
        "shooting_date": "2025-01-01",
    }


def test_database_schema_and_media_crud(tmp_path: Path):
    connection = db.connect(str(tmp_path / "test_photoarchive.db"))
    db.create_tables(connection)
    try:
        record = _media_record()
        media_id = db.save_media(connection, record)
        assert isinstance(media_id, int)

        media_list = db.list_media(connection)
        assert len(media_list) == 1
        assert media_list[0]["path"] == record["path"]
        # 未スキャンは NULL。顔なしの 0 と区別する。
        assert media_list[0]["face_count"] is None

        found = db.get_media_by_path(connection, record["path"])
        assert found["filename"] == record["filename"]
        assert found["type"] == record["type"]

        # 同じハッシュなら再登録しても増えない
        assert db.save_media(connection, record) == media_id
        assert len(db.list_media(connection)) == 1
    finally:
        connection.close()


def test_save_media_resets_scan_state_when_hash_changes(tmp_path: Path):
    connection = db.ensure_database(str(tmp_path / "test.db"))
    try:
        media_id = db.save_media(connection, _media_record())
        db.update_media_scan_state(connection, media_id, 2, "2026-01-01T00:00:00", "v1")
        connection.commit()

        db.save_media(connection, _media_record(file_hash="changed"))
        row = db.get_media_by_id(connection, media_id)
        assert row["face_count"] is None
        assert row["detector_version"] is None
    finally:
        connection.close()


def test_person_crud_and_face_assignment(tmp_path: Path):
    connection = db.ensure_database(str(tmp_path / "test.db"))
    try:
        media_id = db.save_media(connection, _media_record())
        person_id = db.add_person(connection, "父", "father", "memo")
        assert db.list_persons(connection)[0]["name"] == "父"

        face_id = db.add_face(
            connection,
            media_id=media_id,
            bbox=(10, 110, 110, 10),
            embedding=[0.5] * 128,
            embed_version="test",
            thumbnail=b"jpeg",
            quality_score=42.0,
        )
        connection.commit()

        face = db.get_face(connection, face_id)
        assert face["bbox_top"] == 10 and face["bbox_left"] == 10
        assert face["person_id"] is None and face["assign_source"] is None
        assert db.count_faces(connection, unassigned=True) == 1

        db.assign_faces(connection, [face_id], person_id, db.ASSIGN_MANUAL, age=3)
        assert db.count_faces(connection, assign_source=db.ASSIGN_MANUAL) == 1
        assert db.get_face(connection, face_id)["age"] == 3

        db.update_person(connection, person_id, "父さん", "father", "memo2")
        assert db.list_persons(connection)[0]["name"] == "父さん"

        # 人物を消しても顔は残り、未割当に戻る
        db.delete_person(connection, person_id)
        assert db.list_persons(connection) == []
        assert db.count_faces(connection, unassigned=True) == 1
    finally:
        connection.close()


def test_deleting_media_cascades_to_faces_and_results(tmp_path: Path):
    connection = db.ensure_database(str(tmp_path / "test.db"))
    try:
        media_id = db.save_media(connection, _media_record())
        db.add_face(
            connection,
            media_id=media_id,
            bbox=(0, 10, 10, 0),
            embedding=None,
            embed_version="test",
        )
        db.save_media_scores(connection, media_id, 10.0, 20.0)
        connection.commit()

        db.delete_media(connection, [media_id])

        assert db.list_media(connection) == []
        assert db.count_faces(connection) == 0
        assert db.get_analysis_result(connection, media_id) == {}
    finally:
        connection.close()


def test_saving_scores_does_not_clear_family_score(tmp_path: Path):
    """scan がスコアを書いても、match が書いた family_score を潰さないこと。

    旧実装は INSERT OR REPLACE だったため、再解析のたびに family_score が
    0 に戻っていた。
    """
    connection = db.ensure_database(str(tmp_path / "test.db"))
    try:
        media_id = db.save_media(connection, _media_record())
        db.save_media_scores(connection, media_id, 10.0, 20.0)
        connection.execute(
            "UPDATE AnalysisResult SET family_score = 88.0 WHERE media_id = ?", (media_id,)
        )
        connection.commit()

        db.save_media_scores(connection, media_id, 11.0, 21.0)
        connection.commit()

        result = db.get_analysis_result(connection, media_id)
        assert result["family_score"] == 88.0
        assert result["smile_score"] == 11.0
    finally:
        connection.close()


def test_load_manual_embeddings_pairs_vectors_with_person_ids(tmp_path: Path):
    connection = db.ensure_database(str(tmp_path / "test.db"))
    try:
        media_id = db.save_media(connection, _media_record())
        alice = db.add_person(connection, "Alice")
        bob = db.add_person(connection, "Bob")
        for _ in range(3):
            db.add_face(
                connection,
                media_id=media_id,
                bbox=(0, 10, 10, 0),
                embedding=[0.1] * 128,
                embed_version="test",
                person_id=alice,
                assign_source=db.ASSIGN_MANUAL,
            )
        db.add_face(
            connection,
            media_id=media_id,
            bbox=(0, 10, 10, 0),
            embedding=[0.9] * 128,
            embed_version="test",
            person_id=bob,
            assign_source=db.ASSIGN_MANUAL,
        )
        # 自動割当は手本に含めない
        db.add_face(
            connection,
            media_id=media_id,
            bbox=(0, 10, 10, 0),
            embedding=[0.5] * 128,
            embed_version="test",
            person_id=bob,
            assign_source=db.ASSIGN_AUTO,
        )
        connection.commit()

        matrix, person_ids = db.load_manual_embeddings(connection)
        assert matrix.shape == (4, 128)
        assert list(person_ids) == [alice, alice, alice, bob]
    finally:
        connection.close()


def test_person_birth_date_is_stored_and_can_be_cleared(tmp_path):
    """誕生日は未設定（NULL）と区別して持つ。

    撮影時の年齢を計算するのに使う。**入れ間違えたら未設定へ戻せること**まで
    確かめる。`Face.age` で「一度入れた値を消せない」不具合があったので、
    同じ形の穴を最初から塞いでおく。
    """
    connection = db.ensure_database(str(tmp_path / "test.db"))
    try:
        person_id = db.add_person(connection, "なつ", "daughter", "メモ", birth_date="2011-05-03")

        stored = db.list_persons(connection)[0]
        assert stored["birth_date"] == "2011-05-03"

        db.update_person(connection, person_id, "なつ", "daughter", "メモ", birth_date=None)

        assert db.list_persons(connection)[0]["birth_date"] is None
    finally:
        connection.close()


def test_a_person_without_a_birth_date_is_stored_as_unset(tmp_path):
    """誕生日は任意。渡さなければ未設定になる。"""
    connection = db.ensure_database(str(tmp_path / "test.db"))
    try:
        db.add_person(connection, "父")

        assert db.list_persons(connection)[0]["birth_date"] is None
    finally:
        connection.close()


def test_shooting_dates_come_back_one_per_face(tmp_path: Path):
    """**顔1件につき1件返す。** `DISTINCT` で潰さず、無い顔も落とさない。

    潰すと「撮影日時の分からない顔が混ざっている」ことが呼び出し側から消え、
    その顔にも別の写真から計算した年齢が黙って入る（PR #51 のレビュー指摘1）。
    """
    connection = db.ensure_database(str(tmp_path / "dates.db"))
    try:
        face_ids = []
        for index, shooting_date in enumerate(
            ("2017-12-16T18:46:32", None, "0000-00-00T00:00:00", "2017-12-16T18:46:32")
        ):
            media_id = db.save_media(
                connection,
                {
                    "path": f"/photos/{index}.jpg",
                    "filename": f"{index}.jpg",
                    "type": "image",
                    "file_hash": f"hash{index}",
                    "file_size": 100,
                    "created_time": "2026-01-01T00:00:00",
                    "shooting_date": shooting_date,
                },
            )
            face_ids.append(
                db.add_face(
                    connection,
                    media_id=media_id,
                    bbox=(0, 10, 10, 0),
                    embedding=[0.0] * 128,
                    embed_version="test",
                )
            )
        connection.commit()

        dates = db.shooting_dates_for_faces(connection, face_ids)

        assert len(dates) == len(face_ids)
        # 撮影日時の無い顔は None として残る
        assert None in dates
        # 同じ日時の顔が2件あれば2件とも残る（DISTINCT で潰さない）
        assert dates.count("2017-12-16T18:46:32") == 2
        # 壊れた値もそのまま返す（読めるかの判断は gui.parse_date の1か所）
        assert "0000-00-00T00:00:00" in dates
        assert db.shooting_dates_for_faces(connection, []) == []
    finally:
        connection.close()


def test_updating_a_person_without_a_birth_date_keeps_it(tmp_path: Path):
    """**省いて呼んだら触らない。** `None` は「未設定へ戻す」指示。

    既定が `None` だったため、名前だけ直すつもりの呼び出しで登録済みの
    誕生日が消えていた（`assign_faces` の `age` と同じ罠。CLAUDE.md §8）。
    """
    connection = db.ensure_database(str(tmp_path / "person.db"))
    try:
        person_id = db.add_person(
            connection, "なつ", "daughter", "メモ", birth_date="2011-05-03"
        )

        db.update_person(connection, person_id, "なつ", "daughter", "メモ2")

        assert db.list_persons(connection)[0]["birth_date"] == "2011-05-03"
        assert db.list_persons(connection)[0]["memo"] == "メモ2"

        # None は消す指示として、これまで通り効く
        db.update_person(connection, person_id, "なつ", "daughter", "メモ2", birth_date=None)
        assert db.list_persons(connection)[0]["birth_date"] is None
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 一覧の並び順（#53）
# ---------------------------------------------------------------------------


def _seed_for_ordering(connection):
    """撮影日時の違う写真と、年齢の違う顔を用意する。

    **品質スコアと撮影日時と年齢を、わざと逆の順に振る。** 同じ順に振ると、
    どの並び順を指定しても同じ結果になり、テストが何も確かめられない。
    """
    person_id = db.add_person(connection, "なつ")
    faces = {}
    plan = [
        # (キー, 撮影日時, 品質スコア, 年齢)
        ("古い", "2012-01-01T00:00:00", 90.0, 8),
        ("中間", "2017-06-01T00:00:00", 50.0, 2),
        ("新しい", "2021-12-31T00:00:00", 10.0, 5),
        ("日時なし", None, 70.0, None),
    ]
    for index, (key, shooting_date, quality, age) in enumerate(plan):
        media_id = db.save_media(
            connection,
            {
                "path": f"/photos/{index}.jpg",
                "filename": f"{index}.jpg",
                "type": "image",
                "file_hash": f"hash{index}",
                "file_size": 100,
                "created_time": "2026-01-01T00:00:00",
                "shooting_date": shooting_date,
            },
        )
        faces[key] = db.add_face(
            connection,
            media_id=media_id,
            bbox=(0, 10, 10, 0),
            embedding=[0.0] * 128,
            embed_version="test",
            thumbnail=b"",
            quality_score=quality,
        )
    connection.commit()
    return person_id, faces


def test_faces_can_be_listed_newest_shot_first(tmp_path: Path):
    """未割当の一覧は撮影日時の新しい順。**同じ行事の写真が固まる。**

    まとめて選んで一度に割り当てられる。撮影年をまたがないので、
    年齢の初期値も1つに定まる。
    """
    connection = db.ensure_database(str(tmp_path / "order.db"))
    try:
        _, faces = _seed_for_ordering(connection)

        listed = [
            row["id"]
            for row in db.list_faces(connection, unassigned=True, order=db.ORDER_SHOT_DESC)
        ]

        assert listed[:3] == [faces["新しい"], faces["中間"], faces["古い"]]
        # **撮影日時の無い顔は最後。** 先頭に来ると、日付順に見ていく邪魔になる
        assert listed[-1] == faces["日時なし"]
    finally:
        connection.close()


def test_faces_can_be_listed_youngest_age_first(tmp_path: Path):
    """割り当て済みの一覧は年齢順。**成長の順に並ぶ。**

    年齢の入れ間違いや、別人が混ざっているのに気づきやすい。
    """
    connection = db.ensure_database(str(tmp_path / "order.db"))
    try:
        person_id, faces = _seed_for_ordering(connection)
        for key, age in (("古い", 8), ("中間", 2), ("新しい", 5), ("日時なし", None)):
            db.assign_faces(connection, [faces[key]], person_id, db.ASSIGN_MANUAL, age=age)

        listed = [
            row["id"]
            for row in db.list_faces(connection, person_id=person_id, order=db.ORDER_AGE)
        ]

        assert listed[:3] == [faces["中間"], faces["新しい"], faces["古い"]]
        # **年齢が未設定の顔は最後。** SQLite の NULL は最小なので、
        # そのまま昇順にすると先頭を埋めてしまう
        assert listed[-1] == faces["日時なし"]
    finally:
        connection.close()


def test_the_default_order_is_still_the_quality_score(tmp_path: Path):
    """**既定を変えない。** `match` と `evaluate` も `list_faces` を通る。"""
    connection = db.ensure_database(str(tmp_path / "order.db"))
    try:
        _, faces = _seed_for_ordering(connection)

        listed = [row["id"] for row in db.list_faces(connection, unassigned=True)]

        assert listed == [
            faces["古い"],      # 90.0
            faces["日時なし"],   # 70.0
            faces["中間"],      # 50.0
            faces["新しい"],    # 10.0
        ]
    finally:
        connection.close()


def test_every_order_honours_the_same_filters(tmp_path: Path):
    """**絞り込みを2通り書かない。** 書き分けると片方にだけ条件が足される。"""
    connection = db.ensure_database(str(tmp_path / "order.db"))
    try:
        person_id, faces = _seed_for_ordering(connection)
        db.assign_faces(connection, [faces["新しい"]], person_id, db.ASSIGN_MANUAL, age=5)
        db.assign_faces(connection, [faces["日時なし"]], person_id, db.ASSIGN_MANUAL, age=None)

        for order in (db.ORDER_QUALITY, db.ORDER_SHOT_DESC, db.ORDER_AGE):
            unassigned = db.list_faces(connection, unassigned=True, order=order)
            assert {row["id"] for row in unassigned} == {faces["古い"], faces["中間"]}, order
            # 件数は並び順に左右されない
            assert db.count_faces(connection, unassigned=True) == len(unassigned)

        # 年齢の絞り込みも同じように効く。**未設定は範囲から外れても残る**
        for order in (db.ORDER_QUALITY, db.ORDER_SHOT_DESC, db.ORDER_AGE):
            assigned = db.list_faces(
                connection, person_id=person_id, min_age=10, order=order
            )
            assert [row["id"] for row in assigned] == [faces["日時なし"]], order
    finally:
        connection.close()


def test_pagination_does_not_repeat_or_skip_a_face(tmp_path: Path):
    """ページをまたいでも、同じ顔が二度出たり抜けたりしないこと。

    並びが一意でないと（撮影日時が同じ顔が並ぶと）起きる。`id` を
    第2キーに入れてある。
    """
    connection = db.ensure_database(str(tmp_path / "order.db"))
    try:
        media_id = db.save_media(
            connection,
            {
                "path": "/photos/same.jpg",
                "filename": "same.jpg",
                "type": "image",
                "file_hash": "same",
                "file_size": 100,
                "created_time": "2026-01-01T00:00:00",
                # 同じ写真に写った顔は、撮影日時が完全に同じ
                "shooting_date": "2019-08-15T12:00:00",
            },
        )
        expected = [
            db.add_face(
                connection,
                media_id=media_id,
                bbox=(0, 10, 10, 0),
                embedding=[0.0] * 128,
                embed_version="test",
                quality_score=1.0,
            )
            for _ in range(5)
        ]
        connection.commit()

        pages = []
        for offset in (0, 2, 4):
            pages += [
                row["id"]
                for row in db.list_faces(
                    connection,
                    unassigned=True,
                    order=db.ORDER_SHOT_DESC,
                    limit=2,
                    offset=offset,
                )
            ]

        assert pages == expected
    finally:
        connection.close()


def test_a_broken_exif_date_does_not_take_over_the_newest_page(tmp_path: Path):
    """**壊れた EXIF を「いちばん新しい」として先頭に出さない。**

    カメラが `TTTT-TT-TTTTT:TT:TT` を書くことがある（実データで Media 67件・
    顔 123件）。文字の大小で並べると `T` は数字より大きいので、そのままだと
    **新しい順の1ページ目をまるごと占領する。**
    """
    connection = db.ensure_database(str(tmp_path / "order.db"))
    try:
        _, faces = _seed_for_ordering(connection)
        broken = {}
        for index, value in enumerate(("TTTT-TT-TTTTT:TT:TT", "0000-00-00T00:00:00", "いつか")):
            media_id = db.save_media(
                connection,
                {
                    "path": f"/photos/broken{index}.jpg",
                    "filename": f"broken{index}.jpg",
                    "type": "image",
                    "file_hash": f"broken{index}",
                    "file_size": 100,
                    "created_time": "2026-01-01T00:00:00",
                    "shooting_date": value,
                },
            )
            broken[value] = db.add_face(
                connection,
                media_id=media_id,
                bbox=(0, 10, 10, 0),
                embedding=[0.0] * 128,
                embed_version="test",
                quality_score=1.0,
            )
        connection.commit()

        listed = [
            row["id"]
            for row in db.list_faces(connection, unassigned=True, order=db.ORDER_SHOT_DESC)
        ]

        # 読める日付が先。壊れた値は「日時なし」と同じ扱いで最後
        assert listed[:3] == [faces["新しい"], faces["中間"], faces["古い"]]
        assert set(listed[3:]) == {faces["日時なし"]} | set(broken.values())
    finally:
        connection.close()


def test_the_shooting_date_order_does_not_fall_back_to_a_full_sort(tmp_path: Path):
    """**索引を歩くこと。** 全件並べ直しに戻っていないかを問い合わせ計画で見る。

    実データ（未割当 58,547 件）では、全件並べ直すと 1ページの読み出しが
    220〜435ms かかる。索引を順に歩けば 1ページ目は 0.6ms。
    `CROSS JOIN` を外すと、この検査が落ちる。
    """
    connection = db.ensure_database(str(tmp_path / "order.db"))
    try:
        _seed_for_ordering(connection)
        query, params = db._shooting_date_query(
            list(db.FACE_LIST_COLUMNS), None, None, True, None, None
        )
        plan = "\n".join(
            row[-1] for row in connection.execute(f"EXPLAIN QUERY PLAN {query}", params)
        )

        assert "idx_media_shooting" in plan
        # 並べ直しが残っていたら、索引を歩けていない
        assert "USE TEMP B-TREE FOR ORDER BY" not in plan
    finally:
        connection.close()


def test_the_number_of_affected_faces_is_right_even_with_progress(tmp_path: Path):
    """**進み具合を知らせても、戻り値が壊れないこと。**

    `executemany` を塊に分けたので、`cursor.rowcount` は**最後の塊のぶん**しか
    持たない。それを返していたため、120件を割り当てても 20 が返っていた。
    """
    connection = db.ensure_database(str(tmp_path / "count.db"))
    try:
        person_id = db.add_person(connection, "なつ")
        media_id = db.save_media(
            connection,
            {
                "path": "/photos/a.jpg",
                "filename": "a.jpg",
                "type": "image",
                "file_hash": "hash",
                "file_size": 100,
                "created_time": "2026-01-01T00:00:00",
            },
        )
        # **塊の境目をまたぐ件数**にする。ちょうど割り切れると穴に気づけない
        count = db.PROGRESS_CHUNK * 2 + 20
        face_ids = [
            db.add_face(
                connection,
                media_id=media_id,
                bbox=(0, 10, 10, 0),
                embedding=[0.0] * 128,
                embed_version="test",
            )
            for _ in range(count)
        ]
        connection.commit()
        noop = lambda done, total: None  # noqa: E731

        assert db.assign_faces(
            connection, face_ids, person_id, db.ASSIGN_MANUAL, progress=noop
        ) == count
        assert db.set_faces_age(connection, face_ids, 5, progress=noop) == count
        assert db.unassign_faces(connection, face_ids, progress=noop) == count
        assert db.reject_faces(connection, face_ids, progress=noop) == count
        # 知らせない場合も同じ
        assert db.unassign_faces(connection, face_ids) == count
    finally:
        connection.close()
