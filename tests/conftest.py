import os
import sys
import types

import numpy as np
import pytest

# 実物の mediapipe / dlib を読み込むと重く、環境にも左右されるため差し替える。
# ただし実装が読む属性と同じものを返すこと。以前のフェイクは
# `bounding_box` しか持たず、実装が先に見る `relative_bounding_box` を
# 通っていなかったため、検出経路が一度も検証されていなかった。

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _install_fake_mediapipe_modules():
    if "mediapipe" in sys.modules:
        return

    fake_mediapipe = types.ModuleType("mediapipe")
    fake_solutions = types.ModuleType("mediapipe.solutions")
    fake_face_detection = types.ModuleType("mediapipe.solutions.face_detection")
    fake_face_mesh = types.ModuleType("mediapipe.solutions.face_mesh")

    class FakeFaceDetection:
        """画像の明るさで顔の数を変える。顔0件のケースもテストできるようにする。

        真っ黒(平均輝度が10未満)の画像は「顔なし」とみなす。
        """

        def __init__(self, model_selection=0, min_detection_confidence=0.5):
            pass

        def process(self, image):
            detections = []
            if float(np.mean(image)) >= 10.0:
                location_data = types.SimpleNamespace(
                    relative_bounding_box=types.SimpleNamespace(
                        xmin=0.1, ymin=0.1, width=0.4, height=0.5
                    ),
                    bounding_box=types.SimpleNamespace(xmin=0.1, ymin=0.1, width=0.4, height=0.5),
                )
                detections.append(types.SimpleNamespace(location_data=location_data))
            return types.SimpleNamespace(detections=detections)

    class FakeFaceMesh:
        def __init__(self, static_image_mode=True, max_num_faces=1, min_detection_confidence=0.5):
            pass

        def process(self, image):
            landmarks = types.SimpleNamespace(
                landmark=[types.SimpleNamespace(x=0.5, y=0.5, z=0.0) for _ in range(468)]
            )
            return types.SimpleNamespace(multi_face_landmarks=[landmarks])

    fake_face_detection.FaceDetection = FakeFaceDetection
    fake_face_mesh.FaceMesh = FakeFaceMesh
    fake_solutions.face_detection = fake_face_detection
    fake_solutions.face_mesh = fake_face_mesh
    fake_mediapipe.solutions = fake_solutions

    sys.modules["mediapipe"] = fake_mediapipe
    sys.modules["mediapipe.solutions"] = fake_solutions
    sys.modules["mediapipe.solutions.face_detection"] = fake_face_detection
    sys.modules["mediapipe.solutions.face_mesh"] = fake_face_mesh


_install_fake_mediapipe_modules()


def fake_embedding_for(image_region: np.ndarray) -> np.ndarray:
    """顔画像の内容から決まる128次元ベクトルを作る。

    同じ見た目なら同じベクトル、違う見た目なら離れたベクトルになる必要がある
    (そうでないと matcher のテストが書けない)。ここでは領域の平均色をそのまま
    座標にして、色が近い顔ほど距離が近くなるようにしている。
    """
    if image_region.size == 0:
        mean = np.zeros(3, dtype=np.float64)
    else:
        mean = np.asarray(image_region, dtype=np.float64).reshape(-1, 3).mean(axis=0) / 255.0
    vector = np.zeros(128, dtype=np.float32)
    vector[0:3] = mean
    return vector


class _FakeShapePredictor:
    def __call__(self, image, rectangle):
        return (image, rectangle)


class _FakeRecognitionModel:
    def compute_face_descriptor(self, image, shape, num_jitters=0):
        _, rectangle = shape
        left, top = rectangle.left(), rectangle.top()
        right, bottom = rectangle.right(), rectangle.bottom()
        region = np.asarray(image)[max(0, top):bottom, max(0, left):right]
        return fake_embedding_for(region)


@pytest.fixture(autouse=True)
def fake_face_models(monkeypatch):
    """dlib のモデル読み込みを差し替える。実物の .dat は使わない。"""
    from photoarchive_ai import face, scoring

    monkeypatch.setattr(
        face, "_load_dlib_models", lambda: (_FakeShapePredictor(), _FakeRecognitionModel())
    )
    monkeypatch.setattr(face, "EMBED_MIN_FACE_PX", 4)
    face.reset_model_cache()
    scoring.reset_model_cache()
    yield
    face.reset_model_cache()
    scoring.reset_model_cache()


# Make QMessageBox non-interactive in tests to avoid modal dialogs blocking execution.
try:
    from PySide6.QtWidgets import QMessageBox

    QMessageBox.information = lambda *a, **k: None
    QMessageBox.warning = lambda *a, **k: None
    QMessageBox.question = lambda *a, **k: QMessageBox.Yes
except Exception:
    # If PySide6 is unavailable in the collection stage, skip patching here.
    pass
