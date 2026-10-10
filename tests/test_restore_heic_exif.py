"""変換した JPEG の撮影日時（PR #80 のレビュー指摘1・判断 (a) の案4）。

`convert-heic` は以前 EXIF を落としており、変換した JPEG は撮影日時なしで
`scan` された。今後の変換は EXIF を引き継ぎ、既に `scan` 済みのものは
`scripts/restore_heic_exif.py` が**画素データに触れずに** EXIF を差し込み、
DB をファイルに合わせる。**割り当てが消えないこと**がこの修復の要。
"""

import importlib.util
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from photoarchive_ai import db
from photoarchive_ai.converter import convert_heic_files, has_exif, insert_exif
from photoarchive_ai.scanner import extract_exif_datetime, scan_directory
from tests.helpers import textured_array

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "restore_heic_exif.py"
_TAKEN = "2020:01:02 03:04:05"


def _restore_module():
    spec = importlib.util.spec_from_file_location("restore_heic_exif", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_heic_with_date(path: Path, orientation=None, seed=3) -> Path:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    path.parent.mkdir(parents=True, exist_ok=True)
    exif = Image.Exif()
    if orientation is not None:
        exif[0x0112] = orientation
    exif.get_ifd(0x8769)[0x9003] = _TAKEN
    try:
        Image.fromarray(textured_array((320, 240), seed)).save(
            path, format="HEIF", quality=90, exif=exif.tobytes()
        )
    except Exception as error:  # pragma: no cover - 環境依存
        pytest.skip(f"HEIF の書き出しができない環境: {error}")
    return path


def _write_old_conversion(heic: Path) -> Path:
    """#80 より前の `convert-heic` と同じ書き方（EXIF を渡さない）。"""
    output = heic.with_suffix(".jpg")
    with Image.open(heic) as image:
        image.convert("RGB").save(output, "JPEG", quality=95)
    return output


def _pixels(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB"))


def test_convert_keeps_the_shooting_date(tmp_path: Path):
    """変換した JPEG から、scan が撮影日時を読めること（指摘1）。"""
    _write_heic_with_date(tmp_path / "photo.heic")

    convert_heic_files(str(tmp_path))

    assert extract_exif_datetime(tmp_path / "photo.jpg") == "2020-01-02T03:04:05"


def test_convert_does_not_rotate_twice(tmp_path: Path):
    """回転を求める HEIC でも、JPEG は回転済みで Orientation を求めない。"""
    _write_heic_with_date(tmp_path / "photo.heic", orientation=6)

    convert_heic_files(str(tmp_path))

    with Image.open(tmp_path / "photo.jpg") as image:
        assert image.size == (240, 320)
        assert image.getexif().get(0x0112) in (None, 1)


def test_insert_exif_leaves_every_other_byte_alone(tmp_path: Path):
    """差し込んだ APP1 を除けば元のバイト列で、画素も同じ。"""
    heic = _write_heic_with_date(tmp_path / "photo.heic")
    jpeg = _write_old_conversion(heic)
    data = jpeg.read_bytes()
    with Image.open(heic) as image:
        exif = image.info["exif"]

    updated = insert_exif(data, exif)
    segment = len(exif) + 4
    at = updated.index(b"\xff\xe1")

    assert updated[:at] + updated[at + segment :] == data
    assert has_exif(updated) and not has_exif(data)
    restored = tmp_path / "restored.jpg"
    restored.write_bytes(updated)
    assert extract_exif_datetime(restored) == "2020-01-02T03:04:05"
    assert np.array_equal(_pixels(restored), _pixels(jpeg))


def test_insert_exif_refuses_a_second_exif_and_a_bad_payload(tmp_path: Path):
    heic = _write_heic_with_date(tmp_path / "photo.heic")
    with Image.open(heic) as image:
        exif = image.info["exif"]
    data = insert_exif(_write_old_conversion(heic).read_bytes(), exif)

    with pytest.raises(ValueError):
        insert_exif(data, exif)
    with pytest.raises(ValueError):
        insert_exif(_write_old_conversion(heic).read_bytes(), b"not exif")


@pytest.fixture()
def scanned(tmp_path: Path):
    """古い変換で作った JPEG を scan し、顔を手動で割り当てた状態。"""
    source = tmp_path / "media"
    heic = _write_heic_with_date(source / "IMG_0001.HEIC")
    jpeg = _write_old_conversion(heic)
    database = tmp_path / "test.db"
    connection = db.ensure_database(str(database))
    scan_directory(str(source), connection, workers=1)
    person_id = db.add_person(connection, "父")
    face_id = db.list_faces(connection, unassigned=True)[0]["id"]
    db.assign_faces(connection, [face_id], person_id, assign_source="manual")
    connection.commit()
    yield source, jpeg, database, connection, person_id, face_id
    connection.close()


def _media(connection, jpeg: Path):
    return connection.execute("SELECT * FROM Media WHERE path = ?", (str(jpeg),)).fetchone()


def test_restore_counts_without_writing_by_default(scanned, capsys):
    source, jpeg, database, connection, _, _ = scanned
    before = jpeg.read_bytes()

    assert _restore_module().main(["--db", str(database)]) == 0

    assert "戻せる: 1 件" in capsys.readouterr().out
    assert jpeg.read_bytes() == before
    assert _media(connection, jpeg)["shooting_date"] is None


def test_restore_keeps_the_assignment_through_the_next_scan(scanned):
    """戻したあと scan しても、顔は検出し直されず手動の割り当てが残る。"""
    source, jpeg, database, connection, person_id, face_id = scanned
    pixels = _pixels(jpeg)

    assert _restore_module().main(["--db", str(database), "--apply", "--no-backup"]) == 0
    summary = scan_directory(str(source), connection, workers=1)

    assert summary["skipped"] == 1
    assert _media(connection, jpeg)["shooting_date"] == "2020-01-02T03:04:05"
    faces = db.list_faces(connection, person_id=person_id)
    assert [row["id"] for row in faces] == [face_id]
    assert faces[0]["assign_source"] == "manual"
    assert np.array_equal(_pixels(jpeg), pixels)


def test_without_updating_the_database_the_scan_would_discard_the_assignment(scanned):
    """対照: EXIF を足しただけで DB を合わせないと、scan が割り当てを消す。

    修復が DB を書き換える理由。これが通らなくなったら、scan の差分判定が
    変わったということなので、修復の前提を見直すこと。
    """
    source, jpeg, database, connection, person_id, _ = scanned
    with Image.open(jpeg.with_suffix(".HEIC")) as image:
        exif = image.info["exif"]
    jpeg.write_bytes(insert_exif(jpeg.read_bytes(), exif))

    scan_directory(str(source), connection, workers=1)

    assert db.list_faces(connection, person_id=person_id) == []


def test_restore_leaves_a_file_changed_since_the_scan(scanned, capsys):
    """scan の後に変わったファイルは触らない（scan に任せる）。"""
    import os

    source, jpeg, database, connection, _, _ = scanned
    later = jpeg.stat().st_mtime + 3600
    os.utime(jpeg, (later, later))
    before = jpeg.read_bytes()

    _restore_module().main(["--db", str(database), "--apply", "--no-backup"])

    assert "DB と大きさか更新時刻が違う: 1 件" in capsys.readouterr().out
    assert jpeg.read_bytes() == before


def test_restore_leaves_a_jpeg_that_is_not_the_same_photo(tmp_path: Path, capsys):
    """同名でも別の写真なら、その HEIC の撮影日時を付けない。"""
    source = tmp_path / "media"
    heic = _write_heic_with_date(source / "IMG_0001.HEIC", seed=3)
    other = _write_heic_with_date(tmp_path / "other.HEIC", seed=4)
    with Image.open(other) as image:
        image.convert("RGB").save(source / "IMG_0001.jpg", "JPEG", quality=95)
    database = tmp_path / "test.db"
    connection = db.ensure_database(str(database))
    scan_directory(str(source), connection, workers=1)
    connection.close()
    before = (source / "IMG_0001.jpg").read_bytes()

    _restore_module().main(["--db", str(database), "--apply", "--no-backup"])

    assert "HEIC と同じ写真に見えない: 1 件" in capsys.readouterr().out
    assert (source / "IMG_0001.jpg").read_bytes() == before
    assert heic.exists()
