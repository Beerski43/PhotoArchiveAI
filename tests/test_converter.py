"""HEIC/HEIF から JPEG への変換。

変換先の決め方(同名JPEGが同じ写真ならスキップ、違う写真なら連番)が
この機能の要。元のHEICは決して変更しない。
"""

from pathlib import Path

import pytest
from PIL import Image

from photoarchive_ai.converter import (
    SAME_IMAGE_MAX_DIFF,
    _next_output_path,
    _same_image,
    convert_heic_files,
    image_difference,
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


def test_difference_is_small_for_the_same_picture_and_large_for_another(tmp_path: Path):
    first = write_image(tmp_path / "first.jpg", color=(10, 200, 90))
    copy = write_image(tmp_path / "copy.jpg", color=(10, 200, 90))
    other = write_image(tmp_path / "other.jpg", color=(200, 10, 10))

    assert image_difference(first, copy) <= SAME_IMAGE_MAX_DIFF
    assert image_difference(first, other) > SAME_IMAGE_MAX_DIFF
    assert _same_image(first, copy) is True
    assert _same_image(first, other) is False


def test_same_image_requires_the_same_dimensions(tmp_path: Path):
    """縮めれば同じに見えても、寸法が違えば別の写真として扱う（#79）。"""
    large = write_image(tmp_path / "large.jpg", color=(10, 200, 90), size=(400, 300))
    small = write_image(tmp_path / "small.jpg", color=(10, 200, 90), size=(200, 150))

    assert image_difference(large, small) is None
    assert _same_image(large, small) is False


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


def test_convert_skips_a_textured_photo_on_the_second_run(tmp_path: Path):
    """模様のある写真でも、2回目は重複の _1.jpg を作らない（#79）。

    以前は縮小画素のハッシュの完全一致で判定しており、JPEG の非可逆圧縮で
    画素がずれるため実データでは一度もスキップせず、_1.jpg を 656 件作った。
    単色の画像ではずれないので、上のテストはこれを見逃していた。
    """
    write_heic(tmp_path / "photo.heic", size=(320, 240), texture_seed=3)

    first = convert_heic_files(str(tmp_path))
    second = convert_heic_files(str(tmp_path))
    third = convert_heic_files(str(tmp_path))

    assert first == (1, 0)
    assert second == (0, 1)
    assert third == (0, 1)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["photo.heic", "photo.jpg"]


def test_convert_numbers_the_output_when_a_similar_but_different_photo_exists(tmp_path: Path):
    """模様の違う写真が同名で置いてあれば、上書きもスキップもせず連番にする。"""
    write_heic(tmp_path / "photo.heic", size=(320, 240), texture_seed=3)
    other = write_heic(tmp_path / "other.heic", size=(320, 240), texture_seed=4)
    with Image.open(other) as image:
        image.convert("RGB").save(tmp_path / "photo.jpg", "JPEG", quality=95)
    other.unlink()

    converted, skipped = convert_heic_files(str(tmp_path))

    assert (converted, skipped) == (1, 0)
    assert (tmp_path / "photo_1.jpg").exists()


_MEASURE_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "measure_heic_duplicates.py"


def _measure_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("measure_heic_duplicates", _MEASURE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_measurement_separates_the_same_photo_from_the_next_one(tmp_path: Path, capsys):
    """閾値を測るスクリプト（#79）が、同じ写真と連番の隣を分けて数えること。"""
    measure = _measure_module()
    write_heic(tmp_path / "IMG_0099.heic", size=(320, 240), texture_seed=3)
    convert_heic_files(str(tmp_path))
    nxt = write_heic(tmp_path / "IMG_0100.heic", size=(320, 240), texture_seed=4)
    with Image.open(nxt) as image:
        image.convert("RGB").save(tmp_path / "IMG_0100.jpg", "JPEG", quality=95)

    assert measure.neighbour_name("IMG_0099") == "IMG_0100.jpg"
    assert measure.neighbour_name("photo") is None
    # 読めない HEIC が混ざっていても止まらず、数える（PR #80 のレビュー指摘3）
    (tmp_path / "IMG_0002.HEIC").write_bytes(b"broken")
    (tmp_path / "IMG_0002.jpg").write_bytes(b"broken")
    assert measure.main([str(tmp_path), "--sample", "10"]) == 0

    out = capsys.readouterr().out
    assert "閾値を超えた同じ写真: 0 件" in out
    assert "閾値以下の別の写真: 0 件" in out
    assert "読めなかった組: 1 件" in out


def test_a_damaged_heic_is_asked_about_as_a_read_failure(tmp_path: Path):
    """壊れた HEIC は「読めない」として聞き、「書けない」とは言わない（PR #81 で利用者の依頼）。

    以前は両方を「Write failed」と聞いており、実データの壊れた HEIC 3件
    （ftyp の無いファイル）を書き込み先の問題と読ませていた。
    """
    (tmp_path / "IMG_6463.HEIC").write_bytes(b"\x00\x00\x00\x15infe" + b"\x00" * 16)
    write_heic(tmp_path / "good.heic", size=(320, 240), texture_seed=3)
    read_failures, write_failures = [], []

    converted, skipped = convert_heic_files(
        str(tmp_path),
        confirm_read_error=lambda path, error: read_failures.append(path.name) or True,
        confirm_write_error=lambda path, error: write_failures.append(path.name) or True,
    )

    assert read_failures == ["IMG_6463.HEIC"]
    assert write_failures == []
    assert (converted, skipped) == (1, 0)
    assert not (tmp_path / "IMG_6463.jpg").exists()


def test_a_damaged_heic_stops_the_run_without_a_read_confirmation(tmp_path: Path):
    (tmp_path / "broken.heic").write_bytes(b"not a heic")

    with pytest.raises(OSError):
        convert_heic_files(str(tmp_path), confirm_write_error=lambda path, error: True)
