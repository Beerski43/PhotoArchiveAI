"""`face.py` の入出力まわりの確認。

特徴量そのもの(dlib)は実物のモデルが要るので `test_face_real.py` に置く。
ここで守るのは、モデルが無くても動く経路。

- `read_rgb`: 画像 / 動画の先頭フレーム / 読めないファイル
- `detect_faces_with_scores`: 長辺 1280px を超える画像の縮小と、矩形の戻し
- `face_rect`: 特徴量へ渡す矩形の正規化規約
- `crop_face` / `make_thumbnail` / `face_to_bytes`: 顔画像の切り出しと縮小
"""

import io

import numpy as np
import pytest
from PIL import Image

from photoarchive_ai import face
from tests.helpers import write_black_image, write_image, write_video


def _solid_rgb(color=(180, 120, 90), width=200, height=200):
    array = np.zeros((height, width, 3), dtype=np.uint8)
    array[:, :] = color
    return array


# ---------------------------------------------------------------------------
# read_rgb
# ---------------------------------------------------------------------------


def test_read_rgb_returns_the_image_as_rgb(tmp_path):
    path = write_image(tmp_path / "a.jpg", color=(200, 120, 90), size=(80, 60))
    rgb = face.read_rgb(path)
    assert rgb is not None
    assert rgb.shape == (60, 80, 3)  # (高さ, 幅, チャンネル)
    assert rgb.dtype == np.uint8
    # JPEG なので完全一致はしない
    assert rgb[30, 40] == pytest.approx(np.array([200, 120, 90]), abs=4)


def test_read_rgb_reads_the_first_frame_of_a_video_and_fixes_the_channel_order(tmp_path):
    # 1枚目は BGR の (255, 0, 0) = 青、2枚目は緑。
    path = write_video(tmp_path / "clip.mp4", colors=((255, 0, 0), (0, 255, 0)))
    rgb = face.read_rgb(path)
    assert rgb is not None
    assert rgb.shape[2] == 3
    red, green, blue = (float(value) for value in rgb[rgb.shape[0] // 2, rgb.shape[1] // 2])
    # BGR のまま返すと赤が 255 になる。RGB へ直していれば青が高い。
    assert blue > 200
    assert red < 60
    # 2枚目(緑)ではないこと
    assert green < 120


def test_read_rgb_returns_none_for_a_missing_file(tmp_path):
    face.clear_latest_error()
    assert face.read_rgb(tmp_path / "does-not-exist.jpg") is None
    assert "not available" in face.get_latest_error()


def test_read_rgb_returns_none_for_a_directory(tmp_path):
    directory = tmp_path / "album"
    directory.mkdir()
    assert face.read_rgb(directory) is None


def test_read_rgb_returns_none_for_a_file_that_is_not_an_image(tmp_path):
    path = tmp_path / "broken.jpg"
    path.write_text("これは画像ではない", encoding="utf-8")
    face.clear_latest_error()
    assert face.read_rgb(path) is None
    assert "broken.jpg" in face.get_latest_error()


def test_read_rgb_returns_none_for_a_file_that_is_not_a_video(tmp_path):
    path = tmp_path / "broken.mp4"
    path.write_bytes(b"not a video")
    face.clear_latest_error()
    assert face.read_rgb(path) is None
    assert face.get_latest_error() != ""


def test_the_latest_error_is_cleared_before_the_next_file(tmp_path):
    # 大域変数なので、消さないと前のファイルのエラーが次に付く
    assert face.read_rgb(tmp_path / "missing.jpg") is None
    assert face.get_latest_error() != ""
    face.clear_latest_error()
    assert face.get_latest_error() == ""


# ---------------------------------------------------------------------------
# detect_faces / detect_faces_with_scores
# ---------------------------------------------------------------------------


def test_detection_rectangles_come_back_in_the_original_scale():
    # フェイクの検出器は相対矩形 (xmin .1, ymin .1, w .4, h .5) を返す。
    # 長辺 2000px は 1280px へ縮めてから検出されるので、戻し忘れると
    # 矩形が 0.64 倍のままになる。
    rgb = _solid_rgb(width=2000, height=1000)
    assert max(rgb.shape[:2]) > face.FACE_DETECTION_MAX_SIZE

    (top, right, bottom, left), score = face.detect_faces_with_scores(rgb)[0]

    assert (left, top, right, bottom) == (200, 100, 1000, 600)
    assert score == pytest.approx(0.93)


def test_small_images_are_not_resized():
    rgb = _solid_rgb(width=200, height=200)
    assert max(rgb.shape[:2]) <= face.FACE_DETECTION_MAX_SIZE
    (top, right, bottom, left) = face.detect_faces(rgb)[0]
    assert (left, top, right, bottom) == (20, 20, 100, 120)


def test_no_faces_are_reported_for_a_black_image():
    assert face.detect_faces(_solid_rgb(color=(0, 0, 0))) == []


def test_detection_returns_nothing_when_the_detector_is_unavailable(monkeypatch):
    monkeypatch.setattr(face, "_load_mediapipe_face_detection", lambda: None)
    face.clear_latest_error()
    assert face.detect_faces(_solid_rgb()) == []
    assert "unavailable" in face.get_latest_error()


def test_detect_faces_in_file_reads_the_file_first(tmp_path):
    path = write_image(tmp_path / "face.jpg", size=(200, 200))
    assert len(face.detect_faces_in_file(str(path))) == 1
    assert face.detect_faces_in_file(str(tmp_path / "missing.jpg")) == []
    assert face.detect_faces_in_file(str(write_black_image(tmp_path / "dark.jpg"))) == []


# ---------------------------------------------------------------------------
# face_rect — 特徴量へ渡す矩形の正規化
# ---------------------------------------------------------------------------


def test_face_rect_squares_the_rectangle_on_its_longer_side():
    # 幅200 x 高さ100 の矩形。長辺 200 に合わせて正方形化し、
    # 前後に 25% ずつ足すので 200 * 1.5 = 300 の正方形になる。
    left, top, right, bottom = face.face_rect((100, 300, 200, 100), 1000, 1000)
    assert (left, top, right, bottom) == (50, 0, 350, 300)
    assert right - left == bottom - top == 300


def test_face_rect_pads_by_the_documented_ratio():
    left, top, right, bottom = face.face_rect((100, 200, 200, 100), 1000, 1000)
    assert (left, top, right, bottom) == (75, 75, 225, 225)
    assert right - left == 100 * (1.0 + face.EMBED_PADDING * 2.0)


def test_face_rect_stays_inside_the_image():
    # 画像の左上に接した顔。はみ出す分は切り詰められ、正方形ではなくなる。
    left, top, right, bottom = face.face_rect((0, 40, 40, 0), 1000, 1000)
    assert (left, top, right, bottom) == (0, 0, 50, 50)

    height, width = 120, 160
    left, top, right, bottom = face.face_rect((80, 160, 120, 120), width, height)
    assert 0 <= left < right <= width
    assert 0 <= top < bottom <= height


def test_embed_version_records_the_padding():
    """パディングを変えたら `EMBED_VERSION` も上げる、という約束の見張り。

    規約を変えると特徴量の距離が別人判定の閾値と同じオーダーで動く。版が
    据え置かれると、古い特徴量と新しい特徴量が同じ版として混ざってしまう。
    """
    assert f"pad{face.EMBED_PADDING}" in face.EMBED_VERSION
    assert face.EMBED_VERSION in face.DETECTOR_VERSION


# ---------------------------------------------------------------------------
# crop_face / make_thumbnail / face_to_bytes
# ---------------------------------------------------------------------------


def test_crop_face_cuts_exactly_the_given_rectangle():
    rgb = _solid_rgb(width=200, height=200)
    rgb[20:60, 30:90] = (10, 20, 30)
    crop = face.crop_face(rgb, (20, 90, 60, 30))
    assert crop.size == (60, 40)  # PIL は (幅, 高さ)
    assert crop.getpixel((0, 0)) == (10, 20, 30)


def test_make_thumbnail_shrinks_the_face_to_the_listing_size():
    # 原寸のまま貯めると DB が数GBになるので、必ず縮む必要がある
    rgb = _solid_rgb(width=600, height=600)
    data = face.make_thumbnail(rgb, (0, 600, 600, 0))
    assert data is not None
    assert data[:2] == b"\xff\xd8"  # JPEG
    with Image.open(io.BytesIO(data)) as thumbnail:
        assert max(thumbnail.size) <= face.THUMBNAIL_MAX_SIZE


def test_make_thumbnail_honours_an_explicit_size():
    rgb = _solid_rgb(width=600, height=400)
    with Image.open(io.BytesIO(face.make_thumbnail(rgb, (0, 600, 400, 0), max_size=48))) as image:
        assert max(image.size) <= 48


def test_make_thumbnail_returns_none_for_an_empty_rectangle():
    rgb = _solid_rgb(width=200, height=200)
    assert face.make_thumbnail(rgb, (50, 120, 50, 20)) is None


def test_face_to_bytes_produces_a_decodable_jpeg():
    image = Image.fromarray(_solid_rgb(width=40, height=30))
    data = face.face_to_bytes(image)
    with Image.open(io.BytesIO(data)) as decoded:
        assert decoded.size == (40, 30)
        assert decoded.format == "JPEG"


def test_load_face_image_bytes_recuts_from_the_original_file(tmp_path):
    path = write_image(tmp_path / "photo.jpg", size=(200, 200))
    data = face.load_face_image_bytes(path, (20, 120, 80, 40))
    with Image.open(io.BytesIO(data)) as decoded:
        assert decoded.size == (80, 60)


def test_load_face_image_bytes_raises_when_the_original_is_gone(tmp_path):
    with pytest.raises(FileNotFoundError):
        face.load_face_image_bytes(tmp_path / "gone.jpg", (0, 10, 10, 0))


def test_embedding_to_list_keeps_none():
    assert face.embedding_to_list(None) is None
    assert face.embedding_to_list(np.array([1.0, 2.0], dtype=np.float32)) == [1.0, 2.0]
