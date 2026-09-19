"""HEIC/HEIF から JPEG への変換。

変換先の決め方(同名JPEGが同じ写真ならスキップ、違う写真なら連番)が
この機能の要。元のHEICは決して変更しない。
"""

from pathlib import Path

import pytest
from PIL import Image

from photoarchive_ai.converter import (
    _image_fingerprint,
    _next_output_path,
    _same_image,
    convert_heic_files,
)

from tests.helpers import write_heic, write_image


def test_convert_writes_jpeg_beside_the_source_and_keeps_the_original(tmp_path: Path):
    source = write_heic(tmp_path / "2023" / "photo.heic", color=(10, 200, 90))
    original_bytes = source.read_bytes()

    converted, skipped = convert_heic_files(str(tmp_path))

    assert (converted, skipped) == (1, 0)
    output = tmp_path / "2023" / "photo.jpg"
    assert output.exists()
    with Image.open(output) as image:
        assert image.format == "JPEG"
    assert source.read_bytes() == original_bytes


def test_convert_finds_sources_recursively_and_ignores_other_extensions(tmp_path: Path):
    write_heic(tmp_path / "a" / "one.heic")
    write_heic(tmp_path / "a" / "b" / "two.HEIF", color=(30, 30, 200))
    write_image(tmp_path / "a" / "already.jpg")
    (tmp_path / "a" / "notes.txt").write_text("x", encoding="utf-8")

    converted, skipped = convert_heic_files(str(tmp_path))

    assert (converted, skipped) == (2, 0)
    assert (tmp_path / "a" / "one.jpg").exists()
    assert (tmp_path / "a" / "b" / "two.jpg").exists()


def test_convert_skips_when_an_identical_jpeg_already_exists(tmp_path: Path):
    """同じ写真が既にあるなら書き直さない。2回目の実行が無害であること。"""
    write_heic(tmp_path / "photo.heic", color=(120, 60, 200))

    first = convert_heic_files(str(tmp_path))
    output = tmp_path / "photo.jpg"
    stamp = output.stat().st_mtime_ns
    contents = output.read_bytes()

    second = convert_heic_files(str(tmp_path))

    assert first == (1, 0)
    assert second == (0, 1)
    assert output.stat().st_mtime_ns == stamp
    assert output.read_bytes() == contents
    assert not (tmp_path / "photo_1.jpg").exists()


def test_convert_numbers_the_output_when_a_different_jpeg_exists(tmp_path: Path):
    write_heic(tmp_path / "photo.heic", color=(10, 200, 90))
    write_image(tmp_path / "photo.jpg", color=(200, 10, 10))
    occupied = (tmp_path / "photo.jpg").read_bytes()

    converted, skipped = convert_heic_files(str(tmp_path))

    assert (converted, skipped) == (1, 0)
    assert (tmp_path / "photo_1.jpg").exists()
    assert (tmp_path / "photo.jpg").read_bytes() == occupied


def test_convert_reports_progress_for_every_source(tmp_path: Path):
    write_heic(tmp_path / "one.heic")
    write_heic(tmp_path / "two.heic", color=(30, 30, 200))
    seen = []

    convert_heic_files(str(tmp_path), progress_callback=lambda *args: seen.append(args))

    assert [(current, total) for current, total, _ in seen] == [(1, 2), (2, 2)]
    assert sorted(name for _, _, name in seen) == ["one.heic", "two.heic"]


def test_convert_raises_for_a_missing_directory(tmp_path: Path):
    with pytest.raises(ValueError):
        convert_heic_files(str(tmp_path / "does-not-exist"))


def test_convert_asks_before_continuing_when_the_output_cannot_be_written(tmp_path: Path):
    """書き込めないファイルに当たったとき、確認の答えどおりに動くこと。"""
    write_heic(tmp_path / "photo.heic")
    asked = []

    def deny(path, error):
        asked.append(path)
        return False

    def allow(path, error):
        asked.append(path)
        return True

    def fail_to_save(*args, **kwargs):
        raise OSError("read-only file system")

    import photoarchive_ai.converter as converter

    original = Image.Image.save
    Image.Image.save = fail_to_save
    try:
        with pytest.raises(OSError):
            converter.convert_heic_files(str(tmp_path), confirm_write_error=deny)
        converted, skipped = converter.convert_heic_files(
            str(tmp_path), confirm_write_error=allow
        )
    finally:
        Image.Image.save = original

    assert (converted, skipped) == (0, 0)
    assert len(asked) == 2


def test_fingerprint_matches_the_same_picture_and_differs_for_another(tmp_path: Path):
    first = write_image(tmp_path / "first.jpg", color=(10, 200, 90))
    copy = write_image(tmp_path / "copy.jpg", color=(10, 200, 90))
    other = write_image(tmp_path / "other.jpg", color=(200, 10, 10))

    assert _image_fingerprint(first) == _image_fingerprint(copy)
    assert _image_fingerprint(first) != _image_fingerprint(other)
    assert _same_image(first, copy) is True
    assert _same_image(first, other) is False


def test_same_image_is_false_when_a_file_cannot_be_read(tmp_path: Path):
    readable = write_image(tmp_path / "readable.jpg")
    broken = tmp_path / "broken.jpg"
    broken.write_text("not an image", encoding="utf-8")

    assert _same_image(readable, broken) is False


def test_next_output_path_walks_past_occupied_numbers(tmp_path: Path):
    source = write_heic(tmp_path / "photo.heic", color=(10, 200, 90))
    write_image(tmp_path / "photo.jpg", color=(200, 10, 10))
    write_image(tmp_path / "photo_1.jpg", color=(10, 10, 200))

    assert _next_output_path(source) == tmp_path / "photo_2.jpg"
