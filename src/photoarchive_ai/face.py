"""顔の検出と特徴量生成。

役割を2つに絞っている。

1. MediaPipe による顔検出 (``detect_faces``)
2. 顔特徴量の生成 (``compute_embedding`` / ``compute_embedding_from_face_image``)

人物への紐づけはここでは行わない。``scan`` が顔を貯め、GUI が人物へ割り当て、
``match`` が残りを自動で紐づける、という順序を守るため。

**どのモデルを使うかは :mod:`embedding` が持つ**（次元数・距離尺度・閾値も）。
2026-10-02 に dlib ResNet(128次元・ユークリッド) から ArcFace(512次元・コサイン)
へ替えた。実データの手本126件で1位正解率 69.8% → 95.2%。

**重要1**: 検出した矩形をそのままモデルに渡すと、パディングの取り方の違いだけで
特徴量の距離が別人判定の閾値と同じオーダー(実測 0.03〜0.57)で動く。矩形の正規化
は必ず :func:`face_rect` に集約し、規約を変えるときは :data:`EMBED_VERSION` を
上げること（**再スキャンではなく ``photoarchive reembed`` で作り直す**）。

**重要2**: ArcFace は 112x112 のテンプレートに5点を合わせる前提で学習されている。
**整列を省くと 95.2% → 78.6% に落ちる**（現行 dlib をサムネイルから作り直した
82.5% にも負ける）。:func:`align_for_arcface` を通さずに推論しないこと。
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

from . import embedding

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


def clear_latest_error() -> None:
    """直近のエラーを消す。1ファイルの処理を始めるときに呼ぶ。

    これを呼ばないと、前のファイルで出たエラーが次のファイルの結果として
    報告される。1つのワーカーが続けて何件も処理するので、並列でも同じことが
    起きる。
    """
    global _latest_error_message
    _latest_error_message = ""


def _set_latest_error(msg: str) -> None:
    """Set the latest error message."""
    global _latest_error_message
    _latest_error_message = msg[:80]  # Truncate to 80 chars for display


FACE_DETECTION_MAX_SIZE = 1280
VIDEO_EXTENSIONS = {"mp4", "avi", "mov", "mkv"}

# 埋め込み用の矩形正規化パラメータ。変更したら EMBED_VERSION も上げること。
# 実データで 0.0 / 0.1 / 0.25 / 0.4 / 0.6 を比較したところ、0.0〜0.25 はほぼ
# 同等で、0.4 以上は急激に悪化した(0.6 では別人同士の誤一致率が 47〜59%)。
EMBED_PADDING = 0.25
EMBED_MIN_FACE_PX = 60

#: いま使う特徴量モデルの版。`Face.embed_version` に入り、**照合の絞り込みにも
#: 使う**（別の埋め込み空間の特徴量を混ぜて距離を取るのは常に誤り）。
EMBED_VERSION = embedding.ACTIVE.version

#: 検出器の版。**`EMBED_VERSION` を内包させない。**
#:
#: 以前は `f"mediapipe_fd1/{EMBED_VERSION}"` と連結しており、特徴量の規約を
#: 変えるだけで `Media.detector_version` が不一致になって**再検出**が走った。
#: サムネイルから特徴量を作り直せるようになった（`photoarchive reembed`）ので、
#: その必要がなくなった。**検出結果（矩形・サムネイル）は作り直さない。**
#:
#: 実データの `Media.detector_version` は 70,297 件すべてが連結された古い形を
#: 持っている。**素朴に文字列を変えると全件が不一致になり、441GB を NFS
#: （実測 24MB/s）から読み直すことになる。** 比較の側を
#: `detector_version_of` で正規化して、古い値と揃える。
DETECTOR_VERSION = "mediapipe_fd1"

THUMBNAIL_MAX_SIZE = 160
THUMBNAIL_QUALITY = 85

DLIB_MODEL_DIR_ENV = "PHOTOARCHIVE_DLIB_MODEL_DIR"
SHAPE_PREDICTOR_FILE = "shape_predictor_5_face_landmarks.dat"
FACE_RECOGNITION_FILE = "dlib_face_recognition_resnet_model_v1.dat"

#: ArcFace の ONNX。`models/` に手置きする（README の手順、`scripts/fetch_models.py`)。
ONNX_MODEL_DIR_ENV = "PHOTOARCHIVE_ONNX_MODEL_DIR"
ARCFACE_FILE = "w600k_r50.onnx"

#: ArcFace の入力。InsightFace の `ArcFaceONNX` と同じ前処理にそろえる。
ARCFACE_INPUT_SIZE = 112
ARCFACE_INPUT_MEAN = 127.5
ARCFACE_INPUT_STD = 127.5

#: 5点整列のテンプレート（112x112）。InsightFace と同じ。
#: 並びは (画面左の目, 画面右の目, 鼻, 画面左の口角, 画面右の口角)。
ARCFACE_TEMPLATE = np.array(
    [
        [38.2946, 51.6963],
        [73.5318, 51.5014],
        [56.0252, 71.7366],
        [41.5493, 92.3655],
        [70.7299, 92.2041],
    ],
    dtype=np.float64,
)

#: FaceMesh(468点) から5点を作る添字。目は目尻と目頭の中点にする。
#: 口角の 61 / 291 は `scoring.estimate_smile_score` が使うものと同じ。
FACEMESH_LEFT_EYE = (33, 133)
FACEMESH_RIGHT_EYE = (362, 263)
FACEMESH_NOSE = 1
FACEMESH_MOUTH_LEFT = 61
FACEMESH_MOUTH_RIGHT = 291

_face_detection = None
_face_detection_initialized = False
_dlib_models = None
_dlib_models_initialized = False
_arcface_session = None
_arcface_session_initialized = False

#: 5点が取れずに縮小で通した顔の数。**捨てずに数える。**
#: `reembed` が最後にまとめて報告する。
_alignment_fallbacks = 0


def detector_version_of(stored: Optional[str]) -> Optional[str]:
    """保存された検出器の版から、**検出器の部分だけ**を取り出す。

    古い連結形（`mediapipe_fd1/dlib_resnet_v1/sp5/pad0.25/full`）と新しい形
    （`mediapipe_fd1`）を同じ値に揃える。**これが無いと、特徴量モデルを
    替えただけで実データ 70,297 件すべてが再検出の対象になる。**
    """
    if not stored:
        return stored
    return stored.split("/", 1)[0]


def alignment_fallbacks() -> int:
    """5点が取れずに縮小で通した顔の数。"""
    return _alignment_fallbacks


def reset_alignment_fallbacks() -> None:
    global _alignment_fallbacks
    _alignment_fallbacks = 0


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


def detect_faces_with_scores(
    rgb: np.ndarray,
) -> List[Tuple[Tuple[int, int, int, int], Optional[float]]]:
    """顔の矩形と、検出器の確信度を返す。

    ``detect_faces`` は矩形だけを返す薄い包み。確信度は ``Face.detection_score``
    に保存し、GUI で怪しい検出を見分けるのに使う。
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
            faces.append(((top, right, bottom, left), _detection_score(detection)))
        return faces
    except Exception as error:
        logger.exception("MediaPipe face detection failed: %s", error)
        _set_latest_error(f"Face detection failed: {error}")
        return []


def _detection_score(detection) -> Optional[float]:
    """MediaPipe の ``score`` は繰り返し型。先頭の値を float で返す。"""
    score = getattr(detection, "score", None)
    if score is None:
        return None
    try:
        values = list(score)
    except TypeError:
        values = [score]
    if not values:
        return None
    try:
        return float(values[0])
    except (TypeError, ValueError):
        return None


def detect_faces(rgb: np.ndarray) -> List[Tuple[int, int, int, int]]:
    """RGB 画像から顔を検出し (top, right, bottom, left) の一覧を返す。"""
    return [location for location, _ in detect_faces_with_scores(rgb)]


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


def embedding_available() -> bool:
    """**いま使うモデルで**顔特徴量を実際に生成できるか。

    ファイルの所在を見るだけでは、モデルの読み込みそのものが失敗する場合を
    捕まえられない。``scan`` と ``reembed`` はこちらを使い、作れないなら
    始める前に中断する（顔は検出されるのに特徴量が NULL のまま
    「スキャン済み」として記録される事故を防ぐ）。
    """
    if embedding.ACTIVE is embedding.DLIB_RESNET:
        return _load_dlib_models() is not None
    return _load_arcface_session() is not None


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
    global _arcface_session, _arcface_session_initialized
    _dlib_models = None
    _dlib_models_initialized = False
    _face_detection = None
    _face_detection_initialized = False
    _arcface_session = None
    _arcface_session_initialized = False


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


# ---------------------------------------------------------------------------
# 5点整列（ArcFace の前処理）
# ---------------------------------------------------------------------------


def order_five_points(points: np.ndarray) -> np.ndarray:
    """5点を、テンプレートと同じ「画面左が先」の並びにそろえる。

    **MediaPipe の添字の左右と、画面の左右は一致する保証がない**（鏡像の写真や、
    正面でない顔がある）。テンプレートは0番目の x が1番目より小さい前提なので、
    目の左右が逆なら**口角も一緒に入れ替える。** 片方だけ入れ替えると対応が
    崩れ、整列が鏡像になる。
    """
    points = np.asarray(points, dtype=np.float64).copy()
    if points[0][0] > points[1][0]:
        points[[0, 1]] = points[[1, 0]]
        points[[3, 4]] = points[[4, 3]]
    return points


def similarity_transform(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    """``source`` を ``target`` に重ねる相似変換（回転＋等倍＋平行移動）の 2x3 行列。

    Umeyama の閉じた式で解く。**RANSAC を使わない**（5点しかないので、乱数に
    結果が左右されると同じ写真から同じ特徴量が出なくなる）。**鏡像は許さない。**
    許すと左右の取り違えを整列が「直して」隠してしまう。
    """
    source = np.asarray(source, dtype=np.float64)
    target = np.asarray(target, dtype=np.float64)
    source_mean = source.mean(axis=0)
    target_mean = target.mean(axis=0)
    source_centered = source - source_mean
    target_centered = target - target_mean
    covariance = (target_centered.T @ source_centered) / len(source)
    u_matrix, singular, vt_matrix = np.linalg.svd(covariance)
    correction = np.ones(2)
    if np.linalg.det(u_matrix @ vt_matrix) < 0:
        # 鏡像になる解。最も小さい特異値の符号を反転して、回転だけに戻す。
        correction[1] = -1.0
    rotation = u_matrix @ np.diag(correction) @ vt_matrix
    variance = source_centered.var(axis=0).sum()
    scale = 1.0 if variance == 0 else float((singular * correction).sum() / variance)
    matrix = np.zeros((2, 3), dtype=np.float64)
    matrix[:, :2] = scale * rotation
    matrix[:, 2] = target_mean - scale * rotation @ source_mean
    return matrix


def five_points_from_landmarks(landmarks, width: int, height: int) -> np.ndarray:
    """FaceMesh の468点から、整列に使う5点を画素座標で取り出す。"""

    def point(index: int) -> np.ndarray:
        mark = landmarks[index]
        return np.array([mark.x * width, mark.y * height], dtype=np.float64)

    def eye(pair: Tuple[int, int]) -> np.ndarray:
        return (point(pair[0]) + point(pair[1])) / 2.0

    return order_five_points(
        np.array(
            [
                eye(FACEMESH_LEFT_EYE),
                eye(FACEMESH_RIGHT_EYE),
                point(FACEMESH_NOSE),
                point(FACEMESH_MOUTH_LEFT),
                point(FACEMESH_MOUTH_RIGHT),
            ]
        )
    )


def detect_five_points(rgb: np.ndarray) -> Optional[np.ndarray]:
    """顔画像から整列用の5点を取る。取れなければ ``None``。

    FaceMesh は `scoring` と同じものを使う（**新しい依存を増やさない**）。
    """
    from . import scoring

    mesh = scoring._load_mediapipe_face_mesh()
    if mesh is None:
        return None
    try:
        with _suppress_mediapipe_output():
            results = mesh.process(np.ascontiguousarray(rgb))
        if not getattr(results, "multi_face_landmarks", None):
            return None
        height, width = rgb.shape[:2]
        return five_points_from_landmarks(
            results.multi_face_landmarks[0].landmark, width, height
        )
    except Exception as error:  # pragma: no cover - 環境依存
        logger.debug("Landmark detection for alignment failed: %s", error)
        return None


def align_for_arcface(rgb: np.ndarray) -> np.ndarray:
    """顔画像を ArcFace の 112x112 テンプレートへ整列する。

    **5点が取れなければ縮小で通し、件数を数える。** 捨てると、整列できる顔だけで
    精度を語ることになる（実データの手本126件では14件が整列できなかった）。
    **整列は +16.6pt を持っている**ので、落ちた件数は把握しておく必要がある。
    """
    global _alignment_fallbacks
    size = ARCFACE_INPUT_SIZE
    points = detect_five_points(rgb)
    if points is None:
        _alignment_fallbacks += 1
        return cv2.resize(rgb, (size, size), interpolation=cv2.INTER_LINEAR)
    matrix = similarity_transform(points, ARCFACE_TEMPLATE)
    return cv2.warpAffine(rgb, matrix, (size, size), flags=cv2.INTER_LINEAR)


# ---------------------------------------------------------------------------
# 特徴量
# ---------------------------------------------------------------------------


def compute_embedding(
    rgb: np.ndarray,
    face_location: Tuple[int, int, int, int],
) -> Optional[np.ndarray]:
    """元写真と顔の位置から特徴量を作る。小さすぎる顔やモデル不在なら ``None``。

    いま使うモデルは `embedding.ACTIVE`。**次元数も距離尺度もそこに書いてある。**
    """
    height, width = rgb.shape[:2]
    left, top, right, bottom = face_rect(face_location, width, height)
    if min(right - left, bottom - top) < EMBED_MIN_FACE_PX:
        logger.debug("Face too small for embedding: %s", face_location)
        return None
    if embedding.ACTIVE is embedding.DLIB_RESNET:
        return _compute_dlib_embedding(rgb, (left, top, right, bottom))
    return compute_embedding_from_face_image(rgb[top:bottom, left:right])


def compute_embedding_from_face_image(face_rgb: np.ndarray) -> Optional[np.ndarray]:
    """**切り出し済みの顔画像**から特徴量を作る。

    `photoarchive reembed` が保存済みサムネイルから作り直すのに使う。
    **元写真を読まずに済むのがこの入口の目的**（実データの元写真は NFS 上で
    読み直すと 5.1 時間かかる）。

    サムネイルは検出矩形の生クロップなので、**ここでは `face_rect` を通さない。**
    通すと二重にパディングすることになる。
    """
    if face_rgb is None or face_rgb.size == 0:
        return None
    if embedding.ACTIVE is embedding.DLIB_RESNET:
        height, width = face_rgb.shape[:2]
        return _compute_dlib_embedding(face_rgb, (0, 0, width, height))
    return _compute_arcface_embedding(face_rgb)


def _compute_dlib_embedding(
    rgb: np.ndarray, rect: Tuple[int, int, int, int]
) -> Optional[np.ndarray]:
    """dlib ResNet の128次元特徴量。``rect`` は ``(left, top, right, bottom)``。"""
    models = _load_dlib_models()
    if models is None:
        return None
    try:
        import dlib  # type: ignore

        shape_predictor, recognition_model = models
        image = np.ascontiguousarray(rgb)
        rectangle = dlib.rectangle(*rect)
        shape = shape_predictor(image, rectangle)
        descriptor = recognition_model.compute_face_descriptor(image, shape, 0)
        return np.asarray(descriptor, dtype=np.float32)
    except Exception as error:
        logger.exception("dlib embedding failed: %s", error)
        _set_latest_error(f"Embedding failed: {error}")
        return None


def _compute_arcface_embedding(face_rgb: np.ndarray) -> Optional[np.ndarray]:
    """ArcFace の512次元特徴量。**5点整列してから推論する。**

    前処理は InsightFace の ``ArcFaceONNX`` と同じ。入力は **RGB**、
    ``(x - 127.5) / 127.5``、NCHW。
    """
    session = _load_arcface_session()
    if session is None:
        return None
    try:
        aligned = align_for_arcface(np.ascontiguousarray(face_rgb)).astype(np.float32)
        blob = (aligned - ARCFACE_INPUT_MEAN) / ARCFACE_INPUT_STD
        blob = np.transpose(blob, (2, 0, 1))[None, ...]
        name = session.get_inputs()[0].name
        output = session.run(None, {name: blob})[0]
        return np.asarray(output[0], dtype=np.float32)
    except Exception as error:
        logger.exception("ArcFace embedding failed: %s", error)
        _set_latest_error(f"Embedding failed: {error}")
        return None


def _resolve_arcface_path() -> Path:
    """ArcFace の ONNX を探す。探索順は dlib のモデルと同じ考え方。

    1. 環境変数 ``PHOTOARCHIVE_ONNX_MODEL_DIR``
    2. 環境変数 ``PHOTOARCHIVE_DLIB_MODEL_DIR``（同じ置き場にまとめる運用）
    3. リポジトリ直下の ``models/``
    """
    candidates = []
    for variable in (ONNX_MODEL_DIR_ENV, DLIB_MODEL_DIR_ENV):
        value = os.environ.get(variable)
        if value:
            candidates.append(Path(value))
    candidates.append(Path(__file__).resolve().parents[2] / "models")
    for candidate in candidates:
        if (candidate / ARCFACE_FILE).is_file():
            return candidate / ARCFACE_FILE
    raise FileNotFoundError(
        f"ArcFace のモデルが見つかりません。{ARCFACE_FILE} を models/ に置いてください。"
        " `python scripts/fetch_models.py` で取得できます"
        f"（環境変数 {ONNX_MODEL_DIR_ENV} でディレクトリを指定することもできます）。"
    )


def _load_arcface_session():
    """onnxruntime のセッションを返す。失敗時は ``None``。"""
    global _arcface_session, _arcface_session_initialized
    if _arcface_session_initialized:
        return _arcface_session
    _arcface_session_initialized = True
    try:
        import onnxruntime

        options = onnxruntime.SessionOptions()
        options.log_severity_level = 3
        _arcface_session = onnxruntime.InferenceSession(
            str(_resolve_arcface_path()),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        return _arcface_session
    except Exception as error:
        logger.exception("ArcFace model initialization failed: %s", error)
        _set_latest_error(f"ArcFace model unavailable: {error}")
        _arcface_session = None
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
