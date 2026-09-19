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
