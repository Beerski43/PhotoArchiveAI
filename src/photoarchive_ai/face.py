"""顔の検出と特徴量生成。

役割を2つに絞っている。

1. MediaPipe による顔検出 (``detect_faces``)
2. dlib の ResNet による128次元の顔特徴量生成 (``compute_embedding``)

人物への紐づけはここでは行わない。``scan`` が顔を貯め、GUI が人物へ割り当て、
``match`` が残りを自動で紐づける、という順序を守るため。

**重要**: 検出した矩形をそのまま dlib に渡すと、パディングの取り方の違いだけで
特徴量の距離が別人判定の閾値と同じオーダー(実測 0.03〜0.57)で動く。矩形の正規化
は必ず :func:`face_rect` に集約し、規約を変えるときは :data:`EMBED_VERSION` を
上げて再スキャン対象にすること。
"""

import importlib.util
import io
import logging
import os
import sys
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np
from PIL import Image

try:  # HEIC/HEIF を Pillow で開けるようにする
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception:  # pragma: no cover - 依存が無い環境でも検出処理は続行する
    pass

logger = logging.getLogger("photoarchive.face")

# Global state for tracking latest error message
_latest_error_message = ""


def get_latest_error() -> str:
    """Get the latest error message for display."""
    return _latest_error_message


def _set_latest_error(msg: str) -> None:
    """Set the latest error message."""
    global _latest_error_message
    _latest_error_message = msg[:80]  # Truncate to 80 chars for display


FACE_DETECTION_MAX_SIZE = 1280
VIDEO_EXTENSIONS = {"mp4", "avi", "mov", "mkv"}

# 埋め込み用の矩形正規化パラメータ。変更したら EMBED_VERSION も上げること。
EMBED_PADDING = 0.25
EMBED_MIN_FACE_PX = 60
EMBED_VERSION = "dlib_resnet_v1/sp5/pad0.25/full"
DETECTOR_VERSION = f"mediapipe_fd1/{EMBED_VERSION}"

THUMBNAIL_MAX_SIZE = 160
THUMBNAIL_QUALITY = 85

DLIB_MODEL_DIR_ENV = "PHOTOARCHIVE_DLIB_MODEL_DIR"
SHAPE_PREDICTOR_FILE = "shape_predictor_5_face_landmarks.dat"
FACE_RECOGNITION_FILE = "dlib_face_recognition_resnet_model_v1.dat"

_face_detection = None
_face_detection_initialized = False
_dlib_models = None
_dlib_models_initialized = False


@contextmanager
def _suppress_mediapipe_output():
    saved_stderr = os.dup(sys.stderr.fileno())
    null_stderr = os.open(os.devnull, os.O_WRONLY)
    try:
        sys.stderr.flush()
        os.dup2(null_stderr, sys.stderr.fileno())
        with redirect_stderr(io.StringIO()):
            yield
    finally:
        sys.stderr.flush()
        os.dup2(saved_stderr, sys.stderr.fileno())
        os.close(null_stderr)
        os.close(saved_stderr)


def read_rgb(path: Path) -> Optional[np.ndarray]:
    """画像または動画(先頭フレーム)を RGB 配列として読む。失敗時は None。"""
    exists = path.exists()
    is_file = path.is_file() if exists else False
    readable = bool(path.stat().st_mode & 0o444) if is_file else False
    logger.debug("File check: %s exists=%s is_file=%s readable=%s", path, exists, is_file, readable)
    if not is_file:
        msg = f"File is not available: {path}"
        logger.error(msg)
        _set_latest_error(msg)
        return None
    suffix = path.suffix.lower().lstrip(".")
    if suffix in VIDEO_EXTENSIONS:
        capture = cv2.VideoCapture(str(path))
        try:
            if not capture.isOpened():
                msg = f"Cannot open video: {path}"
                logger.error(msg)
                _set_latest_error(msg)
                return None
            ok, frame = capture.read()
            if not ok or frame is None:
                msg = f"Failed to read video: {path.name}"
                logger.warning(msg)
                _set_latest_error(msg)
                return None
            return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        except Exception as error:
            msg = f"Error reading video: {path.name}"
            logger.warning("%s: %s", msg, error)
            _set_latest_error(msg)
            return None
        finally:
            capture.release()
    try:
        with Image.open(path) as image:
            return np.asarray(image.convert("RGB"))
    except Exception as error:
        msg = f"Cannot read image: {path.name}"
        logger.warning("%s: %s", msg, error)
        _set_latest_error(msg)
        return None


# ---------------------------------------------------------------------------
# 顔検出 (MediaPipe)
# ---------------------------------------------------------------------------


def _load_mediapipe_face_detection():
    """Load mediapipe face detection with fallback."""
    global _face_detection, _face_detection_initialized
    if _face_detection_initialized:
        return _face_detection
    try:
        import mediapipe as mp  # type: ignore

        with _suppress_mediapipe_output():
            _face_detection = mp.solutions.face_detection.FaceDetection(
                model_selection=1,
                min_detection_confidence=0.5,
            )
        _face_detection_initialized = True
        return _face_detection
    except Exception as error:
        _face_detection_initialized = True
        logger.exception("MediaPipe face detection initialization failed: %s", error)
        return None


def detect_faces(rgb: np.ndarray) -> List[Tuple[int, int, int, int]]:
    """RGB 画像から顔を検出し (top, right, bottom, left) の一覧を返す。

    検出は長辺 1280px に縮小して行い、座標は元解像度へ戻す。
    """
    detector = _load_mediapipe_face_detection()
    if detector is None:
        msg = "MediaPipe face detection is unavailable"
        logger.error(msg)
        _set_latest_error(msg)
        return []
    try:
        original_height, original_width = rgb.shape[:2]
        scale = min(1.0, FACE_DETECTION_MAX_SIZE / max(original_height, original_width))
        if scale < 1.0:
            detection_rgb = cv2.resize(
                rgb,
                (int(original_width * scale), int(original_height * scale)),
                interpolation=cv2.INTER_AREA,
            )
        else:
            detection_rgb = rgb
        with _suppress_mediapipe_output():
            results = detector.process(np.ascontiguousarray(detection_rgb))
        detections = results.detections or []
        if not detections:
            return []
        faces = []
        h, w = detection_rgb.shape[:2]
        x_scale = original_width / w
        y_scale = original_height / h
        for detection in detections:
            location_data = detection.location_data
            bbox = getattr(location_data, "relative_bounding_box", None)
            if bbox is None or (bbox.width == 0 and bbox.height == 0):
                bbox = location_data.bounding_box
            left = max(0, int(bbox.xmin * w * x_scale))
            top = max(0, int(bbox.ymin * h * y_scale))
            right = min(original_width, int((bbox.xmin + bbox.width) * w * x_scale))
            bottom = min(original_height, int((bbox.ymin + bbox.height) * h * y_scale))
            if right <= left or bottom <= top:
                continue
            faces.append((top, right, bottom, left))
        return faces
    except Exception as error:
        logger.exception("MediaPipe face detection failed: %s", error)
        _set_latest_error(f"Face detection failed: {error}")
        return []


# ---------------------------------------------------------------------------
# 顔特徴量 (dlib)
# ---------------------------------------------------------------------------


def _resolve_model_dir() -> Path:
    """dlib のモデルファイルがあるディレクトリを探す。

    ``face_recognition`` パッケージは import しない。``face_recognition_models``
    の ``__init__`` が ``pkg_resources`` に依存しており、新しい setuptools では
    ImportError になるため。``importlib.util.find_spec`` はモジュールを実行
    しないので、この問題を踏まずにパスだけ取り出せる。
    """
    candidates = []
    env_dir = os.environ.get(DLIB_MODEL_DIR_ENV)
    if env_dir:
        candidates.append(Path(env_dir))
    try:
        from .config import get_dlib_model_dir, load_settings

        configured = get_dlib_model_dir(load_settings())
        if configured:
            candidates.append(Path(configured))
    except Exception:  # pragma: no cover - 設定ファイルが壊れていても続行する
        pass
    try:
        spec = importlib.util.find_spec("face_recognition_models")
    except Exception:  # pragma: no cover - 環境依存
        spec = None
    if spec is not None and spec.submodule_search_locations:
        candidates.append(Path(list(spec.submodule_search_locations)[0]) / "models")
    candidates.append(Path(__file__).resolve().parents[2] / "models")

    for candidate in candidates:
        if (candidate / SHAPE_PREDICTOR_FILE).is_file() and (
            candidate / FACE_RECOGNITION_FILE
        ).is_file():
            return candidate
    raise FileNotFoundError(
        "dlib の顔特徴量モデルが見つかりません。"
        f" {SHAPE_PREDICTOR_FILE} と {FACE_RECOGNITION_FILE} を用意してください。"
        f" 環境変数 {DLIB_MODEL_DIR_ENV} でディレクトリを指定するか、"
        " `pip install git+https://github.com/ageitgey/face_recognition_models`"
        " を実行してください。"
    )


def has_dlib_models() -> bool:
    """モデルファイルが利用できるか。テストのスキップ判定に使う。"""
    try:
        _resolve_model_dir()
        return True
    except Exception:
        return False


def _load_dlib_models():
    """(shape_predictor, face_recognition_model) を返す。失敗時は None。"""
    global _dlib_models, _dlib_models_initialized
    if _dlib_models_initialized:
        return _dlib_models
    _dlib_models_initialized = True
    try:
        import dlib  # type: ignore

        model_dir = _resolve_model_dir()
        _dlib_models = (
            dlib.shape_predictor(str(model_dir / SHAPE_PREDICTOR_FILE)),
            dlib.face_recognition_model_v1(str(model_dir / FACE_RECOGNITION_FILE)),
        )
        return _dlib_models
    except Exception as error:
        logger.exception("dlib face recognition model initialization failed: %s", error)
        _set_latest_error(f"dlib model unavailable: {error}")
        _dlib_models = None
        return None


def reset_model_cache() -> None:
    """テストからモデルを差し替えるためにキャッシュを捨てる。"""
    global _dlib_models, _dlib_models_initialized, _face_detection, _face_detection_initialized
    _dlib_models = None
    _dlib_models_initialized = False
    _face_detection = None
    _face_detection_initialized = False


def face_rect(
    face_location: Tuple[int, int, int, int],
    width: int,
    height: int,
) -> Tuple[int, int, int, int]:
    """検出矩形を正方形化し、一定率のパディングを付けて画像内に収める。

    戻り値は (left, top, right, bottom)。``scan`` も GUI も必ずこの関数を通し、
    特徴量の入力矩形の規約を1つに保つこと。
    """
    top, right, bottom, left = face_location
    center_x = (left + right) / 2.0
    center_y = (top + bottom) / 2.0
    size = max(right - left, bottom - top) * (1.0 + EMBED_PADDING * 2.0)
    half = size / 2.0
    new_left = int(round(max(0, center_x - half)))
    new_top = int(round(max(0, center_y - half)))
    new_right = int(round(min(width, center_x + half)))
    new_bottom = int(round(min(height, center_y + half)))
    return new_left, new_top, new_right, new_bottom


def compute_embedding(
    rgb: np.ndarray,
    face_location: Tuple[int, int, int, int],
) -> Optional[np.ndarray]:
    """顔の128次元特徴量を返す。小さすぎる顔やモデル不在の場合は None。"""
    models = _load_dlib_models()
    if models is None:
        return None
    height, width = rgb.shape[:2]
    left, top, right, bottom = face_rect(face_location, width, height)
    if min(right - left, bottom - top) < EMBED_MIN_FACE_PX:
        logger.debug("Face too small for embedding: %s", face_location)
        return None
    try:
        import dlib  # type: ignore

        shape_predictor, recognition_model = models
        image = np.ascontiguousarray(rgb)
        rectangle = dlib.rectangle(left, top, right, bottom)
        shape = shape_predictor(image, rectangle)
        descriptor = recognition_model.compute_face_descriptor(image, shape, 0)
        return np.asarray(descriptor, dtype=np.float32)
    except Exception as error:
        logger.exception("dlib embedding failed: %s", error)
        _set_latest_error(f"Embedding failed: {error}")
        return None


# ---------------------------------------------------------------------------
# 顔画像の切り出し
# ---------------------------------------------------------------------------


def crop_face(rgb: np.ndarray, face_location: Tuple[int, int, int, int]) -> Image.Image:
    top, right, bottom, left = face_location
    return Image.fromarray(rgb[top:bottom, left:right])


def face_to_bytes(face_image: Image.Image, quality: int = 90) -> bytes:
    output = io.BytesIO()
    face_image.convert("RGB").save(output, format="JPEG", quality=quality)
    return output.getvalue()


def make_thumbnail(
    rgb: np.ndarray,
    face_location: Tuple[int, int, int, int],
    max_size: int = THUMBNAIL_MAX_SIZE,
) -> Optional[bytes]:
    """一覧表示用の小さな顔画像を作る。原寸のまま貯めるとDBが数GBになる。"""
    crop = crop_face(rgb, face_location)
    if crop.width == 0 or crop.height == 0:
        return None
    crop.thumbnail((max_size, max_size))
    return face_to_bytes(crop, quality=THUMBNAIL_QUALITY)


def load_face_image_bytes(path: Path, face_location: Tuple[int, int, int, int]) -> bytes:
    """元画像から顔を切り出し直す。拡大プレビュー用。"""
    rgb = read_rgb(path)
    if rgb is None:
        raise FileNotFoundError(f"Cannot load image: {path}")
    return face_to_bytes(crop_face(rgb, face_location))


def detect_faces_in_file(path: str) -> List[Tuple[int, int, int, int]]:
    image_path = Path(path)
    rgb = read_rgb(image_path)
    if rgb is None:
        return []
    return detect_faces(rgb)


def embedding_to_list(embedding: Optional[Sequence[float]]) -> Optional[List[float]]:
    if embedding is None:
        return None
    return [float(value) for value in embedding]
