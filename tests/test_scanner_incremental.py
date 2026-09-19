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
