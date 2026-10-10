"""scan 以降の処理から HEIC を外す（#26）。

実データ（2026-10-10）の HEIC は 1,761 件で、1,758 件に同名の JPEG があった
（`convert-heic` の後に両方が登録されていた）。同じ写真が二重に入り、手動割り当ても
二重に付けていた。守ること:

- HEIC/HEIF は走査しない（`convert-heic` で JPEG にしてから）
- 既存の HEIC の行は消す（利用者の決定・引き継がない）。**2割の安全弁に数えない**
  （HEIC は `${PERSON_2}携帯` の root の 33% を占め、数えると必ず中断する）
- **scan は HEIC をファイル名も含めて一切見ない**（利用者の決定・PR #76 のレビュー）。
  変換は README の手順どおり `convert-heic` を先に流す
"""

import pytest

from photoarchive_ai import db
from photoarchive_ai.scanner import ScanAborted, prune_missing_media, scan_directory
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

    scan_directory(str(root), connection, workers=1)

    assert _paths(connection) == [str(root / "IMG_0001.JPG")]


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


def test_a_folder_of_only_heic_is_treated_as_having_no_media(tmp_path, connection):
    """scan は HEIC を見ないので、HEIC だけのフォルダは「メディアが1件も無い」で止まる。

    PR #76 のレビュー指摘2 は「convert-heic を促す文言にしたい」だったが、利用者は
    scan に HEIC を一切見させないと決めた（変換を促すのは README の手順の役目）。
    """
    root = tmp_path / "2023"
    root.mkdir()
    (root / "IMG_0001.HEIC").write_bytes(b"x")

    with pytest.raises(ScanAborted, match="1件もありません"):
        scan_directory(str(root), connection, workers=1)


def test_heic_rows_are_not_counted_by_the_missing_file_check_on_its_own(tmp_path, connection):
    """`prune_missing_media` 単体でも、HEIC の行を「消えたファイル」の母数と分子に入れない。

    `scan_directory` は先に `prune_excluded_types` が消すので、この絞り込みを外しても
    通しのテストは落ちない（PR #76 のレビュー指摘4）。単体で呼ぶ経路をここで固定する。
    1件の JPEG と1件の HEIC の行で、HEIC を数えると 50% が「消えた」になり中断する。
    """
    root = tmp_path / "phone"
    jpeg = write_image(root / "IMG_0001.JPG")
    scan_directory(str(root), connection, workers=1)
    _register_heic_row(connection, root / "IMG_0002.HEIC")

    removed = prune_missing_media(connection, root, {str(jpeg)})

    assert removed == 0
    assert str(root / "IMG_0002.HEIC") in _paths(connection)
