"""scan 以降の処理から HEIC を外す（#26）。

実データ（2026-10-10）の HEIC は 1,761 件で、1,758 件に同名の JPEG があった
（`convert-heic` の後に両方が登録されていた）。同じ写真が二重に入り、手動割り当ても
二重に付けていた。守ること:

- HEIC/HEIF は走査しない（`convert-heic` で JPEG にしてから）
- 既存の HEIC の行は消す（利用者の決定・引き継がない）。**2割の安全弁に数えない**
  （HEIC は `な携帯` の root の 33% を占め、数えると必ず中断する）
- JPEG の無い HEIC は写真ごと入らなくなるので、知らせる
"""

import pytest

from photoarchive_ai import db
from photoarchive_ai.scanner import scan_directories, scan_directory, unconverted_heic
from tests.helpers import write_image


@pytest.fixture()
def connection(tmp_path):
    connection = db.ensure_database(str(tmp_path / "test.db"))
    yield connection
    connection.close()


def _paths(connection):
    return sorted(row["path"] for row in db.list_media(connection))


def _register_heic_row(connection, path, person_id=None):
    """#26 より前の scan が入れた HEIC の行（手動割り当ての顔つき）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not decoded")
    media_id = db.save_media(
        connection,
        {
            "path": str(path),
            "filename": path.name,
            "type": "image",
            "file_hash": f"hash-{path.name}",
            "file_size": path.stat().st_size,
            "created_time": "2026-01-01T00:00:00",
            "face_count": 1,
        },
    )
    db.add_face(
        connection,
        media_id=media_id,
        bbox=(0, 10, 10, 0),
        embedding=[0.0] * db.EMBEDDING_DIM,
        embed_version=db.embedding_model.ACTIVE.version,
        person_id=person_id,
        assign_source=db.ASSIGN_MANUAL if person_id else None,
    )
    connection.commit()
    return media_id


def test_a_heic_next_to_its_jpeg_is_not_registered(tmp_path, connection):
    root = tmp_path / "phone"
    write_image(root / "IMG_0001.JPG")
    (root / "IMG_0001.HEIC").write_bytes(b"not decoded")

    summary = scan_directory(str(root), connection, workers=1)

    assert _paths(connection) == [str(root / "IMG_0001.JPG")]
    assert summary["unconverted_heic"] == []


def test_existing_heic_rows_are_removed_without_tripping_the_safety_valve(tmp_path, connection):
    """**HEIC が root の大半を占めても中断しない。** 対象外の拡張子は未マウントの兆候ではない。

    ここで安全弁に数えると、`--force-prune`（安全弁ごと外す）を付けるしかなくなる。
    """
    root = tmp_path / "phone"
    jpeg = write_image(root / "IMG_0001.JPG")
    person_id = db.add_person(connection, "母", "mother", "")
    for index in range(1, 4):
        _register_heic_row(connection, root / f"IMG_000{index}.HEIC", person_id=person_id)
    assert len(_paths(connection)) == 3

    summary = scan_directory(str(root), connection, workers=1)

    assert summary["excluded"] == 3
    assert summary["pruned"] == 0
    assert _paths(connection) == [str(jpeg)]
    # 利用者の決定: 引き継がない。HEIC の顔ごと消える
    assert connection.execute(
        "SELECT COUNT(*) FROM Face WHERE assign_source = 'manual'"
    ).fetchone()[0] == 0


def test_the_safety_valve_still_counts_real_files_that_vanished(tmp_path, connection):
    """HEIC を分けて数えても、消えた JPEG は今までどおり2割で止まる。"""
    root = tmp_path / "phone"
    keep = write_image(root / "keep.jpg")
    gone = [write_image(root / f"gone{index}.jpg", color=(index, 9, 9)) for index in range(3)]
    scan_directory(str(root), connection, workers=1)
    _register_heic_row(connection, root / "old.HEIC")
    for path in gone:
        path.unlink()

    from photoarchive_ai.scanner import ScanAborted

    with pytest.raises(ScanAborted):
        scan_directory(str(root), connection, workers=1)
    assert str(keep) in _paths(connection)


def test_no_prune_keeps_the_heic_rows(tmp_path, connection):
    root = tmp_path / "phone"
    write_image(root / "IMG_0001.JPG")
    _register_heic_row(connection, root / "IMG_0001.HEIC")

    summary = scan_directory(str(root), connection, workers=1, prune=False)

    assert summary["excluded"] == 0
    assert str(root / "IMG_0001.HEIC") in _paths(connection)


def test_a_heic_without_a_jpeg_is_reported(tmp_path, connection):
    root = tmp_path / "phone"
    write_image(root / "IMG_0001.JPG")
    (root / "IMG_0001.heic").write_bytes(b"x")
    (root / "sub" / "IMG_6464.HEIC").parent.mkdir()
    (root / "sub" / "IMG_6464.HEIC").write_bytes(b"x")

    summary = scan_directories([str(root)], connection, workers=1)

    assert summary["unconverted_heic"] == [str(root / "sub" / "IMG_6464.HEIC")]


def test_a_jpeg_in_another_folder_does_not_count_as_converted(tmp_path):
    heic = tmp_path / "a" / "IMG_1.HEIC"
    jpeg = tmp_path / "b" / "IMG_1.jpg"

    assert unconverted_heic([heic], [jpeg]) == [heic]
    assert unconverted_heic([heic], [tmp_path / "a" / "img_1.JPEG"]) == []


def test_the_scan_command_tells_how_to_convert_the_remaining_heic(tmp_path, capsys):
    import os
    import sys

    from photoarchive_ai import cli

    root = tmp_path / "phone"
    write_image(root / "IMG_0001.JPG")
    (root / "IMG_6464.HEIC").write_bytes(b"x")
    database = tmp_path / "photoarchive.db"
    original_argv, original_cwd = sys.argv, os.getcwd()
    try:
        os.chdir(tmp_path)
        sys.argv = ["photoarchive", "scan", "--db", str(database), "--source", str(root), "--workers", "1"]
        cli.main()
    finally:
        sys.argv = original_argv
        os.chdir(original_cwd)

    out = capsys.readouterr().out
    assert "convert-heic" in out
    assert "IMG_6464.HEIC" in out
