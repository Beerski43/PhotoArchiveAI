"""テストが使うフェイクの検出器。**子プロセスからも入れられる形で置く。**

`conftest.py` に置いたままでは、`scan` のワーカープロセスへ持ち込めない。
ワーカーは `spawn` で起こす（`fork` は親の SQLite 接続を引き継いでDBを壊す。
Issue #35）ので、**子は白紙のインタプリタとして始まり、親が `sys.modules` へ
差し込んだフェイクも monkeypatch も引き継がない。** 子で実物の mediapipe /
dlib を読みに行かせないために、ここを `worker_initializer` から呼ぶ。
"""

import sys
import types

import numpy as np


# FaceMesh のフェイクが何を返すか。テストは fake_face_mesh フィクスチャ越しに
# だけ触る。既定値を据え置くのは、既存のテストの意味を変えないため。
FACE_MESH_STATE = {"landmarks": {}, "detected": True}


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
                # 実物の MediaPipe は score を繰り返し型で返す。
                # detection_score の取り出しがその形に耐えるか確かめたいので、
                # フェイクも同じ形にしておく。
                detections.append(
                    types.SimpleNamespace(location_data=location_data, score=[0.93])
                )
            return types.SimpleNamespace(detections=detections)

    class FakeFaceMesh:
        """ランドマークをテストから差し替えられる FaceMesh のフェイク。

        既定は全点 (0.5, 0.5)。**この既定だけでは計算式が検証できない。**
        ``estimate_smile_score`` は口の4点の縦横比を見るので、全点が同じ座標
        だと縦幅が 0 になり、必ず 0.0 で早期 return する。式そのものを
        確かめるテストは ``fake_face_mesh`` フィクスチャで座標を与えること。
        """

        def __init__(self, static_image_mode=True, max_num_faces=1, min_detection_confidence=0.5):
            pass

        def process(self, image):
            if not FACE_MESH_STATE["detected"]:
                return types.SimpleNamespace(multi_face_landmarks=None)
            overrides = FACE_MESH_STATE["landmarks"]
            landmark = []
            for index in range(468):
                x, y = overrides.get(index, (0.5, 0.5))
                landmark.append(types.SimpleNamespace(x=x, y=y, z=0.0))
            landmarks = types.SimpleNamespace(landmark=landmark)
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


def install_fake_backends() -> None:
    """フェイクの mediapipe と dlib を、このプロセスへ入れる。

    **ワーカープロセスの入口から呼ぶ。** 親では `conftest` のフィクスチャが
    同じことをしているので、二重に呼んでも害が無いようにしてある。
    """
    from photoarchive_ai import face, scoring

    _install_fake_mediapipe_modules()
    face._load_dlib_models = lambda: (_FakeShapePredictor(), _FakeRecognitionModel())
    face.EMBED_MIN_FACE_PX = 4
    face.reset_model_cache()
    scoring.reset_model_cache()
