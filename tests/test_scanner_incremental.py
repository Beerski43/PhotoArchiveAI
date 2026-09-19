import pytest

from photoarchive_ai import db, scanner
from photoarchive_ai.scanner import ScanAborted, scan_directory
from tests.helpers import write_black_image, write_image


@pytest.fixture()
def connection(tmp_path):
    connection = db.ensure_database(str(tmp_path / "test.db"))
    yield connection
    connection.close()


def test_scan_records_face_count(tmp_path, connection):
    source = tmp_path / "media"
    write_image(source / "with_face.jpg")
    write_black_image(source / "no_face.jpg")

    summary = scan_directory(str(source), connection, workers=1)

    assert summary["processed"] == 2
    media = {row["filename"]: row for row in db.list_media(connection)}
    # 顔が見つかれば件数、見つからなければ 0。未スキャンの NULL とは区別する。
    assert media["with_face.jpg"]["face_count"] == 1
    assert media["no_face.jpg"]["face_count"] == 0
    assert media["with_face.jpg"]["detector_version"] is not None

    faces = db.list_faces(connection, unassigned=True)
    assert len(faces) == 1
    # scan は人物への紐づけを一切行わない
    assert faces[0]["person_id"] is None
    assert faces[0]["assign_source"] is None


def test_scan_skips_hash_and_faces_on_second_run(tmp_path, connection, monkeypatch):
    source = tmp_path / "media"
    write_image(source / "a.jpg")
    scan_directory(str(source), connection, workers=1)

    hash_calls = []
    original_hash = scanner.compute_file_hash

    def counting_hash(path):
        hash_calls.append(path)
        return original_hash(path)

    monkeypatch.setattr(scanner, "compute_file_hash", counting_hash)
    detect_calls = []
    original_detect = scanner.face.detect_faces
    monkeypatch.setattr(
        scanner.face,
        "detect_faces",
        lambda rgb: (detect_calls.append(1), original_detect(rgb))[1],
    )

    summary = scan_directory(str(source), connection, workers=1)

    assert summary["processed"] == 0
    assert summary["skipped"] == 1
    assert hash_calls == []
    assert detect_calls == []


def test_scan_rescans_when_file_content_changes(tmp_path, connection):
    source = tmp_path / "media"
    target = source / "a.jpg"
    write_image(target, color=(200, 120, 90))
    scan_directory(str(source), connection, workers=1)
    first = db.list_faces(connection, unassigned=True)
    assert len(first) == 1

    write_image(target, color=(30, 200, 60), size=(240, 240))
    scan_directory(str(source), connection, workers=1)

    second = db.list_faces(connection, unassigned=True)
    # 重複して増えないこと(旧実装は再解析のたびに顔行を追記していた)
    assert len(second) == 1
    assert second[0]["id"] != first[0]["id"]


def test_scan_removes_media_whose_file_disappeared(tmp_path, connection):
    source = tmp_path / "media"
    write_image(source / "a.jpg")
    write_image(source / "b.jpg", color=(40, 180, 220))
    write_image(source / "c.jpg", color=(90, 90, 220))
    write_image(source / "d.jpg", color=(220, 220, 40))
    write_image(source / "e.jpg", color=(120, 40, 200))
    scan_directory(str(source), connection, workers=1)
    assert len(db.list_media(connection)) == 5
    assert len(db.list_faces(connection)) == 5

    (source / "a.jpg").unlink()
    summary = scan_directory(str(source), connection, workers=1)

    assert summary["pruned"] == 1
    paths = {row["filename"] for row in db.list_media(connection)}
    assert paths == {"b.jpg", "c.jpg", "d.jpg", "e.jpg"}
    # 顔と解析結果も外部キーで一緒に消える
    assert len(db.list_faces(connection)) == 4
    assert connection.execute("SELECT COUNT(*) FROM AnalysisResult").fetchone()[0] == 4


def test_scan_aborts_when_too_many_files_are_missing(tmp_path, connection):
    source = tmp_path / "media"
    write_image(source / "a.jpg")
    write_image(source / "b.jpg", color=(40, 180, 220))
    scan_directory(str(source), connection, workers=1)

    (source / "a.jpg").unlink()
    with pytest.raises(ScanAborted):
        scan_directory(str(source), connection, workers=1)

    # --force-prune 相当を渡せば削除できる
    summary = scan_directory(str(source), connection, workers=1, force_prune=True)
    assert summary["pruned"] == 1


def test_scan_aborts_when_source_has_no_media(tmp_path, connection):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ScanAborted):
        scan_directory(str(empty), connection, workers=1)


def test_scan_can_skip_prune(tmp_path, connection):
    source = tmp_path / "media"
    write_image(source / "a.jpg")
    write_image(source / "b.jpg", color=(40, 180, 220))
    scan_directory(str(source), connection, workers=1)

    (source / "a.jpg").unlink()
    summary = scan_directory(str(source), connection, workers=1, prune=False)
    assert summary["pruned"] == 0
    assert len(db.list_media(connection)) == 2


def test_touching_a_file_does_not_make_every_later_scan_read_it_again(
    tmp_path, connection, monkeypatch
):
    """更新時刻だけ変わったファイルが、恒久的に再ハッシュされないこと。

    中身が同じでも更新時刻が変わることがある(コピーや touch)。そのとき
    ファイル属性を書き戻さないと、差分判定が毎回「変わったかもしれない」
    と言い続け、SHA-256 のために毎回全体を読み直すことになる。
    実データは 441GB を NFS 24MB/s で読む環境なので致命的。
    """
    import os

    source = tmp_path / "media"
    image = write_image(source / "a.jpg")
    scan_directory(str(source), connection, workers=1)
    stored = db.get_media_by_path(connection, str(image))

    # 中身はそのままに、更新時刻だけ1時間進める。
    new_mtime = os.stat(image).st_mtime + 3600
    os.utime(image, (new_mtime, new_mtime))

    hash_calls = []
    original_hash = scanner.compute_file_hash
    monkeypatch.setattr(
        scanner,
        "compute_file_hash",
        lambda path: (hash_calls.append(path), original_hash(path))[1],
    )

    # 2回目: 変わったかどうか分からないので、一度だけ読んで確かめる。
    scan_directory(str(source), connection, workers=1)
    assert len(hash_calls) == 1

    refreshed = db.get_media_by_path(connection, str(image))
    assert refreshed["created_time"] != stored["created_time"]
    # 検出済みの状態は消えていないこと。
    assert refreshed["face_count"] == stored["face_count"] == 1
    assert refreshed["detector_version"] == stored["detector_version"]
    assert refreshed["face_scanned_at"] == stored["face_scanned_at"]

    # 3回目: もう読み直さない。
    hash_calls.clear()
    summary = scan_directory(str(source), connection, workers=1)
    assert hash_calls == []
    assert summary["skipped"] == 1


def test_faces_survive_a_scan_that_only_sees_a_new_timestamp(tmp_path, connection):
    """更新時刻だけの変化で、割り当て済みの顔が消えないこと。"""
    import os

    source = tmp_path / "media"
    image = write_image(source / "a.jpg")
    scan_directory(str(source), connection, workers=1)

    person_id = db.add_person(connection, "父")
    face_id = db.list_faces(connection, unassigned=True)[0]["id"]
    db.assign_faces(connection, [face_id], person_id, assign_source="manual")

    new_mtime = os.stat(image).st_mtime + 3600
    os.utime(image, (new_mtime, new_mtime))
    scan_directory(str(source), connection, workers=1)

    faces = db.list_faces(connection, person_id=person_id)
    assert [row["id"] for row in faces] == [face_id]
    assert faces[0]["assign_source"] == "manual"


def test_scan_records_the_detector_confidence(tmp_path, connection):
    """detection_score を保存すること(以前は全行 NULL だった)。"""
    source = tmp_path / "media"
    write_image(source / "a.jpg")

    scan_directory(str(source), connection, workers=1)

    faces = db.list_faces(connection, unassigned=True)
    assert len(faces) == 1
    assert db.get_face(connection, faces[0]["id"])["detection_score"] == pytest.approx(0.93)


def test_scan_stops_when_the_embedding_model_cannot_be_loaded(tmp_path, connection, monkeypatch):
    """特徴量が作れないまま「スキャン済み」にしないこと。

    そのまま進むと face_count だけが記録され、embedding が全件 NULL の
    まま二度と再検出されない。警告も出ないので気づけない。
    """
    source = tmp_path / "media"
    write_image(source / "a.jpg")
    monkeypatch.setattr(scanner.face, "embedding_available", lambda: False)

    with pytest.raises(ScanAborted) as raised:
        scan_directory(str(source), connection, workers=1)
    assert "--allow-missing-embeddings" in str(raised.value)
    assert db.list_media(connection) == []

    # 明示的に許可したときは進む。
    summary = scan_directory(
        str(source), connection, workers=1, allow_missing_embeddings=True
    )
    assert summary["processed"] == 1


def test_scanning_with_several_workers_gives_the_same_result(tmp_path, connection):
    """並列経路を通すこと。

    実運用の既定は --workers = min(4, CPU数 - 1) なので、既定の経路が
    一度も検査されていない状態だった。
    """
    source = tmp_path / "media"
    for index in range(6):
        write_image(source / f"{index}.jpg", color=(20 + index * 10, 100, 150))
    write_black_image(source / "dark.jpg")

    summary = scan_directory(str(source), connection, workers=2)

    assert summary["processed"] == 7
    assert summary["errors"] == 0
    media = {row["filename"]: row for row in db.list_media(connection)}
    assert media["dark.jpg"]["face_count"] == 0
    assert all(media[f"{index}.jpg"]["face_count"] == 1 for index in range(6))
    assert len(db.list_faces(connection, unassigned=True)) == 6


def test_a_parallel_rescan_is_still_incremental(tmp_path, connection):
    source = tmp_path / "media"
    for index in range(4):
        write_image(source / f"{index}.jpg", color=(20 + index * 10, 100, 150))
    scan_directory(str(source), connection, workers=2)

    summary = scan_directory(str(source), connection, workers=2)

    assert summary["processed"] == 0
    assert summary["skipped"] == 4
    # 顔行が二重に増えていないこと。
    assert len(db.list_faces(connection, unassigned=True)) == 4


def test_an_error_from_one_file_is_not_reported_for_the_next(tmp_path, connection):
    """直近のエラーが次のファイルに付かないこと。

    エラーはモジュール変数で持つため、消さないと前のファイルの
    メッセージが次のファイルの結果として報告される。並列実行では
    fork 時の値が全ワーカーへ複製されるので、特に紛れ込みやすい。
    """
    source = tmp_path / "media"
    write_black_image(source / "dark.jpg")
    scanner.face._set_latest_error("前のファイルで起きた何か")

    summary = scan_directory(str(source), connection, workers=1)

    assert summary["errors"] == 0


def test_media_type_is_image_or_video(tmp_path):
    """type の値域は "image" / "video" / None。

    テストの作り物が "photo" を使っていて実装とずれていた。
    selection が `!= "video"` で判定しているため実害は出ていなかったが、
    作り物が実装と違う値を使っていると、後から読む人が誤解する。
    """
    assert scanner.get_media_type(tmp_path / "a.JPG") == "image"
    assert scanner.get_media_type(tmp_path / "a.heic") == "image"
    assert scanner.get_media_type(tmp_path / "a.mp4") == "video"
    assert scanner.get_media_type(tmp_path / "a.txt") is None
