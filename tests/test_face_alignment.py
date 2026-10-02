"""5点整列（ArcFace の前処理）。Issue #59 で `face.py` へ移した。

**整列は 1位正解率で +16.6pt を持っている**（整列なし 78.6% / あり 95.2%、
実データの手本126件）。ここが静かに壊れると、モデルを替えた意味が半分消える。
"""

import numpy as np
import pytest

from photoarchive_ai import face


def test_the_transform_puts_a_rotated_face_back_on_the_template():
    """回転・拡大・平行移動したテンプレートを、元のテンプレートへ戻せること。"""
    template = face.ARCFACE_TEMPLATE
    angle = np.deg2rad(20.0)
    rotation = np.array(
        [[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]]
    )
    moved = (template @ rotation.T) * 1.7 + np.array([12.0, -5.0])

    matrix = face.similarity_transform(moved, template)
    restored = moved @ matrix[:, :2].T + matrix[:, 2]
    assert np.allclose(restored, template, atol=1e-6)


def test_the_transform_never_mirrors_the_face():
    """**鏡像を許さない。** 許すと左右の取り違えを整列が「直して」隠してしまう。"""
    template = face.ARCFACE_TEMPLATE
    mirrored = template * np.array([-1.0, 1.0])
    matrix = face.similarity_transform(mirrored, template)
    assert np.linalg.det(matrix[:, :2]) > 0


def test_the_eyes_and_the_mouth_corners_are_swapped_together():
    """**片方だけ入れ替えると対応が崩れる。**

    MediaPipe の添字の左右と画面の左右は一致する保証がないので、目の左右が
    逆なら口角も一緒に入れ替える。
    """
    points = np.array([[70.0, 50.0], [30.0, 50.0], [50.0, 70.0], [68.0, 90.0], [32.0, 90.0]])
    ordered = face.order_five_points(points)
    assert ordered[0][0] < ordered[1][0]
    assert ordered[3][0] < ordered[4][0]
    # 鼻は動かさない
    assert tuple(ordered[2]) == (50.0, 70.0)


def test_points_already_in_order_are_left_alone():
    points = np.array([[30.0, 50.0], [70.0, 50.0], [50.0, 70.0], [32.0, 90.0], [68.0, 90.0]])
    assert np.array_equal(face.order_five_points(points), points)


def test_landmarks_are_scaled_to_pixels():
    """FaceMesh は 0.0-1.0 の相対座標を返す。画素へ直すこと。"""

    class _Mark:
        def __init__(self, x, y):
            self.x = x
            self.y = y

    landmarks = {index: _Mark(0.5, 0.5) for index in range(468)}
    landmarks[33] = _Mark(0.2, 0.4)
    landmarks[133] = _Mark(0.3, 0.4)
    landmarks[362] = _Mark(0.7, 0.4)
    landmarks[263] = _Mark(0.8, 0.4)
    landmarks[1] = _Mark(0.5, 0.6)
    landmarks[61] = _Mark(0.35, 0.8)
    landmarks[291] = _Mark(0.65, 0.8)

    points = face.five_points_from_landmarks(landmarks, width=200, height=100)
    assert points[0] == pytest.approx([50.0, 40.0])  # (0.2+0.3)/2 * 200
    assert points[1] == pytest.approx([150.0, 40.0])
    assert points[2] == pytest.approx([100.0, 60.0])
    assert points[3] == pytest.approx([70.0, 80.0])
    assert points[4] == pytest.approx([130.0, 80.0])
