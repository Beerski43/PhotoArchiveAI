"""スコアの計算式そのものの確認。

これまで `scoring.py` は「呼んでも落ちない」ことしか確かめられていなかった。
テストの FaceMesh のフェイクが全ランドマークを (0.5, 0.5) で返すため、
`estimate_smile_score` は口の縦幅が 0 になり、**必ず 0.0 で早期 return する
枝しか通っていなかった**。ここでは `fake_face_mesh` で座標を与えて、
式が意図どおりの値を出すことを確かめる。

期待値はすべて実装の式から手で計算して書く。実装を呼んで得た値を
そのまま期待値にすると、式が壊れてもテストが一緒に壊れて気づけない。
"""

import numpy as np
import pytest

from photoarchive_ai import scoring

# 口のランドマーク番号。実装が見ているものと同じ。
MOUTH_LEFT = 61
MOUTH_RIGHT = 291
MOUTH_UPPER_LEFT = 78
MOUTH_UPPER_RIGHT = 308

FULL_IMAGE = (0, 200, 200, 0)  # (top, right, bottom, left)


def _image(value, size=200):
    array = np.zeros((size, size, 3), dtype=np.uint8)
    array[:, :] = value
    return array


# ---------------------------------------------------------------------------
# estimate_smile_score
# ---------------------------------------------------------------------------


def test_smile_score_follows_the_mouth_aspect_ratio(fake_face_mesh):
    # 200px の画像で 口幅 80px / 口の縦 40px → 比 2.0
    # スコア = (2.0 - 1.4) * 70 = 42.0
    fake_face_mesh.set_landmarks(
        {
            MOUTH_LEFT: (0.3, 0.4),
            MOUTH_RIGHT: (0.7, 0.4),
            MOUTH_UPPER_LEFT: (0.4, 0.6),
            MOUTH_UPPER_RIGHT: (0.6, 0.6),
        }
    )
    score = scoring.estimate_smile_score(_image(180), FULL_IMAGE)
    assert score == pytest.approx(42.0)


def test_a_round_mouth_scores_zero_instead_of_going_negative(fake_face_mesh):
    # 口幅 80px / 口の縦 80px → 比 1.0。式は (1.0 - 1.4) * 70 = -28.0 を出すが、
    # 0 で下げ止まること。負のスコアが DB に入ると並び替えが壊れる。
    fake_face_mesh.set_landmarks(
        {
            MOUTH_LEFT: (0.3, 0.4),
            MOUTH_RIGHT: (0.7, 0.4),
            MOUTH_UPPER_LEFT: (0.4, 0.8),
            MOUTH_UPPER_RIGHT: (0.6, 0.8),
        }
    )
    assert scoring.estimate_smile_score(_image(180), FULL_IMAGE) == 0.0


def test_an_extremely_wide_mouth_is_capped_at_100(fake_face_mesh):
    # 口幅 120px / 口の縦 10px → 比 12.0。式は 742.0 を出すが 100 で頭打ち。
    fake_face_mesh.set_landmarks(
        {
            MOUTH_LEFT: (0.2, 0.5),
            MOUTH_RIGHT: (0.8, 0.5),
            MOUTH_UPPER_LEFT: (0.3, 0.55),
            MOUTH_UPPER_RIGHT: (0.7, 0.55),
        }
    )
    assert scoring.estimate_smile_score(_image(180), FULL_IMAGE) == 100.0


def test_landmarks_on_a_single_point_score_zero():
    # フェイクの既定(全点が同じ座標)。口の縦幅が 0 なので割り算へ進まない。
    # この枝しか通っていなかったせいで、上の3件が長らく未検証だった。
    assert scoring.estimate_smile_score(_image(180), FULL_IMAGE) == 0.0


def test_smile_score_is_zero_when_no_face_mesh_is_found(fake_face_mesh):
    fake_face_mesh.detect_nothing()
    assert scoring.estimate_smile_score(_image(180), FULL_IMAGE) == 0.0


def test_smile_score_is_zero_when_the_face_mesh_model_is_unavailable(monkeypatch):
    monkeypatch.setattr(scoring, "_load_mediapipe_face_mesh", lambda: None)
    assert scoring.estimate_smile_score(_image(180), FULL_IMAGE) == 0.0


def test_smile_score_is_zero_for_an_empty_face_region(fake_face_mesh):
    fake_face_mesh.set_landmarks({MOUTH_LEFT: (0.3, 0.4), MOUTH_RIGHT: (0.7, 0.4)})
    # top == bottom の矩形。切り出すと画素が1つも無い。
    assert scoring.estimate_smile_score(_image(180), (50, 60, 50, 10)) == 0.0


# ---------------------------------------------------------------------------
# estimate_quality
# ---------------------------------------------------------------------------


def test_quality_combines_brightness_and_face_size():
    # 100x100 の一様な灰色(128)、顔は 30x30。
    #   明るさ = 128 / 255 = 0.50196 → * 60 = 30.118
    #   顔の面積比 = 900 / (10000 * 0.12) = 0.75 → * 40 = 30.0
    rgb = _image(128, size=100)
    score = scoring.estimate_quality(rgb, (0, 30, 30, 0))
    assert score == pytest.approx(30.118 + 30.0, abs=0.01)


def test_a_dark_face_only_earns_the_size_part():
    rgb = _image(0, size=100)
    # 明るさ 0 なので 顔の面積比の 30.0 だけが残る
    assert scoring.estimate_quality(rgb, (0, 30, 30, 0)) == pytest.approx(30.0)


def test_a_large_bright_face_reaches_the_maximum():
    rgb = _image(255, size=100)
    # 面積比は 10000 / 1200 = 8.33 だが 1.0 で頭打ちになる
    assert scoring.estimate_quality(rgb, (0, 100, 100, 0)) == pytest.approx(100.0)


def test_a_small_face_scores_lower_than_a_large_one_at_the_same_brightness():
    rgb = _image(128, size=100)
    small = scoring.estimate_quality(rgb, (0, 10, 10, 0))
    large = scoring.estimate_quality(rgb, (0, 40, 40, 0))
    assert small < large


def test_quality_is_zero_for_an_empty_face_region():
    assert scoring.estimate_quality(_image(128, size=100), (50, 60, 50, 10)) == 0.0


# ---------------------------------------------------------------------------
# score_face / distance_to_similarity / aggregate_media_scores
# ---------------------------------------------------------------------------


def test_score_face_returns_both_scores(fake_face_mesh):
    fake_face_mesh.set_landmarks(
        {
            MOUTH_LEFT: (0.3, 0.4),
            MOUTH_RIGHT: (0.7, 0.4),
            MOUTH_UPPER_LEFT: (0.4, 0.6),
            MOUTH_UPPER_RIGHT: (0.6, 0.6),
        }
    )
    rgb = _image(128)
    smile, quality = scoring.score_face(rgb, FULL_IMAGE)
    assert smile == pytest.approx(scoring.estimate_smile_score(rgb, FULL_IMAGE))
    assert quality == pytest.approx(scoring.estimate_quality(rgb, FULL_IMAGE))


@pytest.mark.parametrize(
    "distance, expected",
    [
        (0.0, 100.0),
        (0.3, 50.0),  # 基準距離 0.6 のちょうど半分
        (0.6, 0.0),
        (1.2, 0.0),  # 基準を超えても負にはしない
        (-0.1, 100.0),  # 念のため下側もクリップする
    ],
)
def test_distance_to_similarity(distance, expected):
    assert scoring.distance_to_similarity(distance) == pytest.approx(expected)


def test_media_scores_take_the_best_face():
    # メディア単位のスコアは「一番良く写っている顔」で代表させる仕様
    smile, quality = scoring.aggregate_media_scores([(10.0, 20.0), (5.0, 90.0)])
    assert (smile, quality) == (10.0, 90.0)


@pytest.mark.parametrize("face_scores", [None, []])
def test_media_without_faces_scores_zero(face_scores):
    assert scoring.aggregate_media_scores(face_scores) == (0.0, 0.0)
