from datetime import date
from pathlib import Path

import pytest

from photoarchive_ai import db
from photoarchive_ai.dates import Taken


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
            embedding=[0.5] * db.EMBEDDING_DIM,
            embed_version=db.embedding_model.ACTIVE.version,
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
            embed_version=db.embedding_model.ACTIVE.version,
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
                embedding=[0.1] * db.EMBEDDING_DIM,
                embed_version=db.embedding_model.ACTIVE.version,
                person_id=alice,
                assign_source=db.ASSIGN_MANUAL,
            )
        db.add_face(
            connection,
            media_id=media_id,
            bbox=(0, 10, 10, 0),
            embedding=[0.9] * db.EMBEDDING_DIM,
            embed_version=db.embedding_model.ACTIVE.version,
            person_id=bob,
            assign_source=db.ASSIGN_MANUAL,
        )
        # 自動割当は手本に含めない
        db.add_face(
            connection,
            media_id=media_id,
            bbox=(0, 10, 10, 0),
            embedding=[0.5] * db.EMBEDDING_DIM,
            embed_version=db.embedding_model.ACTIVE.version,
            person_id=bob,
            assign_source=db.ASSIGN_AUTO,
        )
        connection.commit()

        matrix, person_ids = db.load_manual_embeddings(connection)
        assert matrix.shape == (4, db.EMBEDDING_DIM)
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
        person_id = db.add_person(connection, "${PERSON_2}", "daughter", "メモ", birth_date="2011-05-03")

        stored = db.list_persons(connection)[0]
        assert stored["birth_date"] == "2011-05-03"

        db.update_person(connection, person_id, "${PERSON_2}", "daughter", "メモ", birth_date=None)

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
                    embedding=[0.0] * db.EMBEDDING_DIM,
                    embed_version=db.embedding_model.ACTIVE.version,
                )
            )
        connection.commit()

        dates = db.taken_for_faces(connection, face_ids)

        assert len(dates) == len(face_ids)
        # 同じ日時の顔が2件あれば2件とも残る（DISTINCT で潰さない）
        shot = Taken(date(2017, 12, 16), date(2017, 12, 16), inferred=False)
        assert dates.count(shot) == 2
        # 撮影日時の無い顔と壊れた値の顔は None として残る（`/photos/` は
        # 年のフォルダが無いので、フォルダ名からも起こせない）。並びは最後
        assert dates[2:] == [None, None]
        assert db.taken_for_faces(connection, []) == []
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
            connection, "${PERSON_2}", "daughter", "メモ", birth_date="2011-05-03"
        )

        db.update_person(connection, person_id, "${PERSON_2}", "daughter", "メモ2")

        assert db.list_persons(connection)[0]["birth_date"] == "2011-05-03"
        assert db.list_persons(connection)[0]["memo"] == "メモ2"

        # None は消す指示として、これまで通り効く
        db.update_person(connection, person_id, "${PERSON_2}", "daughter", "メモ2", birth_date=None)
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
    person_id = db.add_person(connection, "${PERSON_2}")
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
            embedding=[0.0] * db.EMBEDDING_DIM,
            embed_version=db.embedding_model.ACTIVE.version,
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
                embedding=[0.0] * db.EMBEDDING_DIM,
                embed_version=db.embedding_model.ACTIVE.version,
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
                embedding=[0.0] * db.EMBEDDING_DIM,
                embed_version=db.embedding_model.ACTIVE.version,
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
        where, params = db._face_filter(None, None, True, None, None, prefix="f.")
        query = db._shooting_date_query(list(db.FACE_LIST_COLUMNS), where)
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
        person_id = db.add_person(connection, "${PERSON_2}")
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
                embedding=[0.0] * db.EMBEDDING_DIM,
                embed_version=db.embedding_model.ACTIVE.version,
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


def test_face_paths_come_back_keyed_by_face_id(tmp_path: Path):
    """`evaluate` が誤りになった顔を名指しするのに使う。

    **顔 id だけ出しても人は見に行けない。** パスが要る。
    `faces_by_ids` は `Face` の列しか返さないので、別に用意している。
    """
    connection = db.ensure_database(str(tmp_path / "test.db"))
    try:
        first = db.save_media(connection, _media_record("2025/01/a.jpg", "hash-a"))
        second = db.save_media(connection, _media_record("2025/01/b.jpg", "hash-b"))
        face_ids = [
            db.add_face(
                connection,
                media_id=media_id,
                bbox=(0, 10, 10, 0),
                embedding=None,
                embed_version=db.embedding_model.ACTIVE.version,
            )
            for media_id in (first, second)
        ]
        connection.commit()

        assert db.face_paths(connection, face_ids) == {
            face_ids[0]: "2025/01/a.jpg",
            face_ids[1]: "2025/01/b.jpg",
        }
        # 何も渡さなければ読みに行かない。
        assert db.face_paths(connection, []) == {}
        # 無い id は黙って落ちる。呼び出し側が「見つからない」を書き分けられる。
        assert db.face_paths(connection, [face_ids[0], 999999]) == {
            face_ids[0]: "2025/01/a.jpg"
        }
    finally:
        connection.close()


def test_face_paths_reads_more_faces_than_the_sqlite_variable_limit(tmp_path: Path):
    """`IN (...)` の変数の上限を越えても落ちないこと。

    手本が増えれば名指しする顔も増える。塊に割るのを外すと、ある日突然
    `too many SQL variables` で落ちる。
    """
    connection = db.ensure_database(str(tmp_path / "test.db"))
    try:
        media_id = db.save_media(connection, _media_record())
        face_ids = [
            db.add_face(
                connection,
                media_id=media_id,
                bbox=(0, 10, 10, 0),
                embedding=None,
                embed_version=db.embedding_model.ACTIVE.version,
            )
            for _ in range(1200)
        ]
        connection.commit()

        found = db.face_paths(connection, face_ids)

        assert len(found) == 1200
        assert set(found) == set(face_ids)
    finally:
        connection.close()


def test_shooting_dates_come_back_keyed_by_face_id(tmp_path: Path):
    """一覧の1件ずつに年齢を出すには、**どの顔の撮影日時かが引ける**必要がある。

    `taken_for_faces` は並べた値だけを返すので、まとめて年齢を
    入れるときの範囲表示には足りるが、1件ずつの表示には使えない。
    """
    connection = db.ensure_database(str(tmp_path / "test.db"))
    try:
        dated = db.save_media(connection, _media_record("2025/01/a.jpg", "hash-a"))
        undated = db.save_media(
            connection,
            {**_media_record("2025/01/b.jpg", "hash-b"), "shooting_date": None},
        )
        unknown = db.save_media(
            connection,
            {**_media_record("misc/c.jpg", "hash-c"), "shooting_date": None},
        )
        face_ids = [
            db.add_face(
                connection,
                media_id=media_id,
                bbox=(0, 10, 10, 0),
                embedding=None,
                embed_version=db.embedding_model.ACTIVE.version,
            )
            for media_id in (dated, undated, unknown)
        ]
        connection.commit()

        found = db.taken_by_face(connection, face_ids)

        assert found[face_ids[0]] == Taken(date(2025, 1, 1), date(2025, 1, 1), inferred=False)
        # EXIF が無ければフォルダ名（`2025/01/`）から起こした区間（#65）
        assert found[face_ids[1]] == Taken(date(2025, 1, 1), date(2025, 1, 31), inferred=True)
        # **撮影時期の分からない顔を落とさない。** 落とすと、呼び出し側から
        # 「分からない」が消えて、別の写真の年齢が黙って入る。
        assert face_ids[2] in found
        assert found[face_ids[2]] is None
        assert db.taken_by_face(connection, []) == {}
    finally:
        connection.close()


def test_resetting_auto_assignments_leaves_the_manual_ones_alone(tmp_path: Path):
    """**自動だけを消す。** 手本と除外は GUI で積み上げた判断なので触らない。"""
    connection = db.ensure_database(str(tmp_path / "test.db"))
    try:
        person = db.add_person(connection, "Alice")
        media_id = db.save_media(connection, _media_record())
        kept = {}
        for source in (db.ASSIGN_MANUAL, db.ASSIGN_AUTO, db.ASSIGN_REJECTED):
            kept[source] = db.add_face(
                connection,
                media_id=media_id,
                bbox=(0, 10, 10, 0),
                embedding=None,
                embed_version=db.embedding_model.ACTIVE.version,
                person_id=person if source != db.ASSIGN_REJECTED else None,
                assign_source=source,
            )
        connection.commit()

        removed = db.reset_auto_assignments(connection)

        assert removed == 1
        rows = {row["id"]: row for row in db.list_faces(connection)}
        assert rows[kept[db.ASSIGN_MANUAL]]["assign_source"] == db.ASSIGN_MANUAL
        assert rows[kept[db.ASSIGN_REJECTED]]["assign_source"] == db.ASSIGN_REJECTED
        assert rows[kept[db.ASSIGN_AUTO]]["assign_source"] is None
    finally:
        connection.close()


def _media_with_date(connection, path, file_hash, shooting_date):
    media_id = db.save_media(connection, _media_record(path, file_hash))
    connection.execute(
        "UPDATE Media SET shooting_date = ? WHERE id = ?", (shooting_date, media_id)
    )
    db.add_face(
        connection,
        media_id=media_id,
        bbox=(0, 10, 10, 0),
        embedding=None,
        embed_version=db.embedding_model.ACTIVE.version,
    )
    connection.commit()
    return media_id


def test_faces_can_be_filtered_by_shooting_month(tmp_path: Path):
    """**撮影年月で絞れること。**

    家族の写っていない行事（結婚式や旅行先の他人）は時期でまとまっているので、
    その時期だけを開いてまとめて除外できる。
    """
    connection = db.ensure_database(str(tmp_path / "test.db"))
    try:
        _media_with_date(connection, "a.jpg", "h-a", "2015-08-14T10:00:00")
        _media_with_date(connection, "b.jpg", "h-b", "2015-08-31T23:59:59")
        _media_with_date(connection, "c.jpg", "h-c", "2015-09-01T00:00:00")

        # 両端を含む。
        assert db.count_faces(connection, month_from="2015-08", month_to="2015-08") == 2
        assert db.count_faces(connection, month_from="2015-09", month_to="2015-09") == 1
        assert db.count_faces(connection, month_from="2015-08", month_to="2015-09") == 3
        # 片方だけでもよい。
        assert db.count_faces(connection, month_from="2015-09") == 1
        assert db.count_faces(connection, month_to="2015-08") == 2
        assert db.count_faces(connection) == 3, "絞らなければ全部"
    finally:
        connection.close()


@pytest.mark.parametrize("broken", ["0000-00-00T00:00:00", "TTTT-TT-TTTTT:TT:TT", None])
def test_a_broken_shooting_date_counts_as_undated_not_as_a_month(tmp_path: Path, broken):
    """**壊れた日付を月として扱わない。**

    `substr(shooting_date, 1, 7)` と書くと「TTTT-TT」という月が一覧に並ぶ。
    判断は `SHOOTING_DATE_SORT_KEY` に1つだけある。
    """
    connection = db.ensure_database(str(tmp_path / "test.db"))
    try:
        _media_with_date(connection, "a.jpg", "h-a", "2015-08-14T10:00:00")
        _media_with_date(connection, "b.jpg", "h-b", broken)

        assert db.available_months(connection) == ["2015-08"]
        assert db.count_undated_faces(connection) == 1
        assert db.count_faces(connection, undated_only=True) == 1
        assert db.count_faces(connection, month_from="2015-08", month_to="2015-08") == 1
        # **読めない日付はどの範囲にも入らない。** 広く取っても混ざらない。
        assert db.count_faces(connection, month_from="1900-01", month_to="2999-12") == 1
    finally:
        connection.close()


def test_available_months_come_back_newest_first_and_only_where_faces_are(tmp_path: Path):
    """**顔のある写真の月だけ。** 選んでも1件も出ない月を並べない。"""
    connection = db.ensure_database(str(tmp_path / "test.db"))
    try:
        _media_with_date(connection, "a.jpg", "h-a", "2015-08-14T10:00:00")
        _media_with_date(connection, "b.jpg", "h-b", "2020-01-02T10:00:00")
        # 顔の無い写真。月の一覧に出てはいけない。
        faceless = db.save_media(connection, _media_record("c.jpg", "h-c"))
        connection.execute(
            "UPDATE Media SET shooting_date = '2018-06-01T10:00:00' WHERE id = ?",
            (faceless,),
        )
        connection.commit()

        assert db.available_months(connection) == ["2020-01", "2015-08"]
    finally:
        connection.close()


def test_the_month_filter_combines_with_the_other_filters(tmp_path: Path):
    """ほかの絞り込みと併用できること。"""
    connection = db.ensure_database(str(tmp_path / "test.db"))
    try:
        person = db.add_person(connection, "Alice")
        _media_with_date(connection, "a.jpg", "h-a", "2015-08-14T10:00:00")
        _media_with_date(connection, "b.jpg", "h-b", "2015-08-20T10:00:00")
        first = db.list_faces(connection, month_from="2015-08", month_to="2015-08")[0]["id"]
        db.assign_faces(connection, [first], person, db.ASSIGN_MANUAL)
        connection.commit()

        window = {"month_from": "2015-08", "month_to": "2015-08"}
        assert db.count_faces(connection, unassigned=True, **window) == 1
        assert (
            db.count_faces(connection, assign_source=db.ASSIGN_MANUAL, **window) == 1
        )
        assert (
            db.count_faces(
                connection, unassigned=True, month_from="2015-09", month_to="2015-09"
            )
            == 0
        )
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 絞り込みの引数の揃い / 件数 / 「この人物ではない」の一覧 / 確信度の並び（#67）
# ---------------------------------------------------------------------------


def test_every_list_filter_also_works_for_counting_and_for_bulk():
    """**一覧・件数・まとめて処理が、同じ絞り込みを受け取ること。**

    画面は同じ辞書を3つに渡す（`list_faces` / `count_faces` / `face_ids`）。
    **1つだけ受け取れないと、見えている顔と処理の対象がずれる。**

    実際に `face_ids` だけ撮影年月を受け取れておらず、**年月で絞った状態で
    行事の「まとめて…」を押すと `TypeError` で落ちていた**（#67）。
    絞り込みを足すのは `_face_filter` なので、そこを正本に数える。
    """
    import inspect

    expected = set(inspect.signature(db._face_filter).parameters) - {"prefix"}
    for function in (db.list_faces, db.count_faces, db.face_ids):
        missing = expected - set(inspect.signature(function).parameters)
        assert not missing, f"{function.__name__} が受け取れない絞り込み: {missing}"


def test_bulk_face_ids_can_be_narrowed_by_the_month_range(tmp_path: Path):
    """**まとめて処理する対象が、撮影年月の絞り込みに従うこと。**

    画面に3件しか出ていないのに、まとめて処理が全件に効いては困る。
    """
    connection = db.ensure_database(str(tmp_path / "bulk.db"))
    try:
        _seed_for_ordering(connection)

        every = db.face_ids(connection, unassigned=True)
        narrowed = db.face_ids(
            connection, unassigned=True, month_from="2017-01", month_to="2017-12"
        )
        undated = db.face_ids(connection, unassigned=True, undated_only=True)

        assert len(every) == 4
        assert len(narrowed) == 1, "2017年に撮った1件だけ"
        assert len(undated) == 1, "撮影日時が読めない1件だけ"
    finally:
        connection.close()


def test_face_counts_are_gathered_in_one_query(tmp_path: Path):
    """左の一覧に出す件数が、**1回の問い合わせ**でそろうこと。

    人物ごとに数えると人数ぶんの問い合わせになり、選び直すたびに増える。
    """
    connection = db.ensure_database(str(tmp_path / "counts.db"))
    try:
        person_id, faces = _seed_for_ordering(connection)
        other = db.add_person(connection, "とら")
        db.assign_faces(connection, [faces["古い"]], person_id, db.ASSIGN_MANUAL)
        db.assign_faces(connection, [faces["中間"]], other, db.ASSIGN_AUTO)
        db.reject_faces(connection, [faces["新しい"]])

        statements = []
        connection.set_trace_callback(statements.append)
        try:
            counts = db.face_counts(connection)
        finally:
            connection.set_trace_callback(None)

        assert counts["unassigned"] == 1
        assert counts["manual"] == 1
        assert counts["auto"] == 1
        assert counts["rejected"] == 1
        assert counts["by_person"][person_id] == {"manual": 1, "auto": 0}
        assert counts["by_person"][other] == {"manual": 0, "auto": 1}
        assert sum("SELECT" in text.upper() for text in statements) == 1, statements
    finally:
        connection.close()


def test_the_rejection_list_is_read_through_the_same_filters(tmp_path: Path):
    """「この人物ではない」の一覧も、**ふつうの絞り込みに乗ること。**

    以前は専用の読み出し（`rejected_face_ids_for_person`）だったため、
    **撮影年月・行事・年齢の絞り込みとページャが効かなかった。**
    """
    connection = db.ensure_database(str(tmp_path / "rejections.db"))
    try:
        person_id, faces = _seed_for_ordering(connection)
        other = db.add_person(connection, "とら")
        db.reject_faces_for_person(
            connection, [faces["古い"], faces["中間"]], person_id
        )
        db.reject_faces_for_person(connection, [faces["新しい"]], other)

        listed = db.face_ids(connection, rejected_for_person=person_id)
        assert listed == sorted([faces["古い"], faces["中間"]])
        assert db.count_faces(connection, rejected_for_person=person_id) == 2
        # **撮影年月でも絞れる。**
        assert db.face_ids(
            connection, rejected_for_person=person_id, month_from="2017-01"
        ) == [faces["中間"]]

        # **「誰でもない顔」にした顔は出さない。** `match` の候補から顔ごと
        # 外れるので、否定の記録はもう何の仕事もしていない。
        db.reject_faces(connection, [faces["古い"]])
        assert db.face_ids(connection, rejected_for_person=person_id) == [faces["中間"]]
        # **記録そのものは消さない**（除外を取り消せば一覧に戻る）。
        assert db.count_person_rejections(connection, person_id) == 2
        db.unassign_faces(connection, [faces["古い"]])
        assert db.face_ids(connection, rejected_for_person=person_id) == sorted(
            [faces["古い"], faces["中間"]]
        )
    finally:
        connection.close()


def test_faces_can_be_listed_least_confident_first(tmp_path: Path):
    """自動割り当ての見直しは、**確信度の低い順**に見る。

    低いほど「似ていないのに割り当てた」顔なので、誤りに早く当たる。
    確信度を持たない顔は最後。
    """
    connection = db.ensure_database(str(tmp_path / "score.db"))
    try:
        person_id, faces = _seed_for_ordering(connection)
        db.apply_auto_assignments(
            connection,
            [
                (faces["古い"], person_id, 80.0),
                (faces["中間"], person_id, 20.0),
                (faces["新しい"], person_id, 50.0),
            ],
        )

        listed = [
            row["id"]
            for row in db.list_faces(connection, order=db.ORDER_SCORE_ASC)
        ]

        assert listed[:3] == [faces["中間"], faces["新しい"], faces["古い"]]
        # 確信度を持たない顔（未割当）は最後
        assert listed[-1] == faces["日時なし"]
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 年齢の若い順は、画面に出ている年齢で全件に効く（2026-10-09 利用者の報告）
# ---------------------------------------------------------------------------


def _face_on(connection, index, shooting_date, person_id, age=None):
    media_id = db.save_media(
        connection,
        {
            "path": f"/photos/age{index}.jpg",
            "filename": f"age{index}.jpg",
            "type": "image",
            "file_hash": f"age-hash{index}",
            "file_size": 100,
            "created_time": "2026-01-01T00:00:00",
            "shooting_date": shooting_date,
        },
    )
    face_id = db.add_face(
        connection,
        media_id=media_id,
        bbox=(0, 10, 10, 0),
        embedding=[0.0] * db.EMBEDDING_DIM,
        embed_version=db.embedding_model.ACTIVE.version,
        thumbnail=b"",
    )
    db.assign_faces(connection, [face_id], person_id, db.ASSIGN_AUTO, age=age)
    return face_id


def test_the_age_order_uses_the_calculated_age_across_every_page(tmp_path: Path):
    """**年齢の若い順は、画面に出ている年齢で全件を並べてからページに分ける。**

    以前は `Face.age`（人が入れた確定値）だけで並べていた。実データでは
    ${PERSON_4}の 9,502 件のうち確定値は 199 件だけで、**残り 9,303 件は id 順のまま
    2ページ目以降に並んでいた。** 画面には括弧つきの計算年齢が出ているので、
    利用者には「ページの中しか並んでいない」ように見えた（2026-10-09 に報告）。

    **id の順と年齢の順をわざと逆にしてある。** id 順のままでも通る並びだと、
    この不具合を捕まえられない。
    """
    connection = db.ensure_database(str(tmp_path / "age.db"))
    try:
        person_id = db.add_person(connection, "${PERSON_4}", birth_date="2010-12-01")
        plan = [
            ("13歳", "2024-06-01T10:00:00", None),
            ("9歳", "2020-01-01T10:00:00", None),
            ("壊れた日付", "TTTT-TT-TTTTT:TT:TT", None),
            ("1歳", "2012-01-01T10:00:00", None),
            # 確定値が計算値（8歳）と食い違っていても、**画面と同じく確定値**で並べる
            ("確定 3歳", "2019-01-01T10:00:00", 3),
            ("日付なし", None, None),
            ("0歳", "2011-06-01T10:00:00", None),
        ]
        faces = {
            name: _face_on(connection, index, shooting_date, person_id, age)
            for index, (name, shooting_date, age) in enumerate(plan)
        }
        connection.commit()

        listed = []
        for page in range(4):
            listed += [
                row["id"]
                for row in db.list_faces(
                    connection,
                    person_id=person_id,
                    order=db.ORDER_AGE,
                    birth_date="2010-12-01",
                    limit=2,
                    offset=page * 2,
                )
            ]

        expected = ["0歳", "1歳", "確定 3歳", "9歳", "13歳"]
        assert listed[:5] == [faces[name] for name in expected]
        # **年齢を出せない顔は最後。** 壊れた日付も「無い」として扱う
        assert set(listed[5:]) == {faces["壊れた日付"], faces["日付なし"]}
        assert len(listed) == len(set(listed)) == len(plan), "重複・欠落しない"
    finally:
        connection.close()


def test_the_age_order_uses_each_face_s_own_person_when_none_is_selected(tmp_path: Path):
    """**全員ぶんの表示では、顔ごとに付いている人物の誕生日で年齢を出す。**

    画面（自動割当の表示）がそう表示しているので、並びもそれに合わせる。
    """
    connection = db.ensure_database(str(tmp_path / "owners.db"))
    try:
        older = db.add_person(connection, "兄", birth_date="2005-01-01")
        younger = db.add_person(connection, "妹", birth_date="2015-01-01")
        # 同じ日の写真でも、兄は 15歳・妹は 5歳
        brother = _face_on(connection, 0, "2020-06-01T10:00:00", older)
        sister = _face_on(connection, 1, "2020-06-01T10:00:00", younger)
        connection.commit()

        listed = [
            row["id"]
            for row in db.list_faces(
                connection, assign_source=db.ASSIGN_AUTO, order=db.ORDER_AGE
            )
        ]

        assert listed == [sister, brother]
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 並びの逆向き（#69 のコメント「高い順があるなら低い順もあるべき」）
# ---------------------------------------------------------------------------


def test_every_order_has_its_reverse_and_keeps_missing_values_last(tmp_path: Path):
    """**逆向きでも、値を持たない顔は最後。** 逆にしたとたん撮影日時の無い顔や
    確信度の無い顔が先頭に並ぶと、見たい顔が2ページ目以降に押し出される。"""
    connection = db.ensure_database(str(tmp_path / "reverse.db"))
    try:
        person_id, faces = _seed_for_ordering(connection)
        no_quality = db.add_face(
            connection,
            media_id=db.get_face(connection, faces["日時なし"])["media_id"],
            bbox=(0, 10, 10, 0),
            embedding=None,
            embed_version=db.embedding_model.ACTIVE.version,
            thumbnail=b"",
        )
        db.apply_auto_assignments(
            connection,
            [
                (faces["古い"], person_id, 80.0),
                (faces["中間"], person_id, 20.0),
                (faces["新しい"], person_id, 50.0),
            ],
        )
        connection.commit()

        def listed(order, **filters):
            return [row["id"] for row in db.list_faces(connection, order=order, **filters)]

        assert listed(db.ORDER_SHOT_ASC)[:3] == [faces["古い"], faces["中間"], faces["新しい"]]
        assert listed(db.ORDER_SHOT_ASC)[3:] == [faces["日時なし"], no_quality]
        assert listed(db.ORDER_SCORE_DESC)[:3] == [faces["古い"], faces["新しい"], faces["中間"]]
        assert set(listed(db.ORDER_SCORE_DESC)[3:]) == {faces["日時なし"], no_quality}
        assert listed(db.ORDER_QUALITY_ASC) == [
            faces["新しい"], faces["中間"], faces["日時なし"], faces["古い"], no_quality,
        ]
        assert listed(db.ORDER_QUALITY)[-1] == no_quality

        # 年齢は誕生日から計算した値。2010-01-01 生まれ → 古い 2歳・中間 7歳・新しい 11歳。
        assert listed(
            db.ORDER_AGE_DESC, person_id=person_id, birth_date="2010-01-01"
        ) == [faces["新しい"], faces["中間"], faces["古い"]]
        assert listed(
            db.ORDER_AGE, person_id=person_id, birth_date="2010-01-01"
        ) == [faces["古い"], faces["中間"], faces["新しい"]]
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 人物の画面で未割当を見るとき、候補になりえない顔を外す（#69）
# ---------------------------------------------------------------------------


def test_unassigned_faces_shot_before_the_birth_are_left_out(tmp_path: Path):
    """**生まれる前の写真には写れない。** 撮影日時が読めない顔は外さない
    （分からないものを弾かない）。誕生日の当日は残す。"""
    connection = db.ensure_database(str(tmp_path / "born.db"))
    try:
        _person_id, faces = _seed_for_ordering(connection)
        born_on_the_day = _face_on(connection, 99, "2017-06-01T09:00:00", _person_id)
        db.unassign_faces(connection, [born_on_the_day])

        kept = db.face_ids(connection, unassigned=True, born_by="2017-06-01")

        assert kept == sorted(
            [faces["中間"], faces["新しい"], faces["日時なし"], born_on_the_day]
        )
        assert db.count_faces(connection, unassigned=True, born_by="2017-06-01") == 4
        # 誕生日が読めなければ、何も外さない。
        assert len(db.face_ids(connection, unassigned=True, born_by="0000-00-00")) == 5
    finally:
        connection.close()


def test_unassigned_faces_marked_not_this_person_are_left_out(tmp_path: Path):
    """**「この人物ではない」と人が決めた顔は、その人物の候補に出さない。**
    ほかの人物の候補には出る。"""
    connection = db.ensure_database(str(tmp_path / "not-this.db"))
    try:
        person_id, faces = _seed_for_ordering(connection)
        other = db.add_person(connection, "${PERSON_4}")
        db.reject_faces_for_person(connection, [faces["古い"]], person_id)
        connection.commit()

        listed = db.face_ids(connection, unassigned=True, not_rejected_for_person=person_id)

        assert faces["古い"] not in listed and len(listed) == 3
        assert faces["古い"] in db.face_ids(
            connection, unassigned=True, not_rejected_for_person=other
        )
    finally:
        connection.close()
