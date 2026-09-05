import io
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image

from .db import (
    add_face_embedding,
    get_person_embeddings,
    save_analysis_result,
    get_media_by_path,
)

logger = logging.getLogger(__name__)

ANALYZER_VERSION = "1.0"


def _read_image(path: Path) -> Optional[np.ndarray]:
    """Read image or video file. Returns None if file cannot be read, with warning logged."""
    suffix = path.suffix.lower().lstrip(".")
    if suffix in {"mp4", "avi", "mov", "mkv"}:
        capture = cv2.VideoCapture(str(path))
        try:
            ok, frame = capture.read()
            if not ok or frame is None:
                logger.warning(f"Failed to read video frame from: {path}")
                return None
            return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        except Exception as e:
            logger.warning(f"Error reading video file {path}: {e}")
            return None
        finally:
            capture.release()
    try:
        with Image.open(path) as image:
            return np.asarray(image.convert("RGB"))
    except Exception as e:
        logger.warning(f"Failed to read image file {path}: {e}")
        return None


def _load_mediapipe_face_detection():
    """Load mediapipe face detection with fallback."""
    try:
        import mediapipe as mp  # type: ignore
        return mp.solutions.face_detection.FaceDetection(
            model_selection=0,
            min_detection_confidence=0.5
        )
    except Exception:
        return None


def detect_faces(rgb: np.ndarray) -> List[Tuple[int, int, int, int]]:
    """Detect faces in RGB image using mediapipe.
    Returns list of (top, right, bottom, left) tuples compatible with face_recognition format.
    """
    detector = _load_mediapipe_face_detection()
    if detector is None:
        return []
    try:
        results = detector.process(rgb)
        if not results.detections:
            return []
        faces = []
        h, w = rgb.shape[:2]
        for detection in results.detections:
            bbox = detection.location_data.bounding_box
            left = max(0, int(bbox.xmin * w))
            top = max(0, int(bbox.ymin * h))
            right = min(w, int((bbox.xmin + bbox.width) * w))
            bottom = min(h, int((bbox.ymin + bbox.height) * h))
            faces.append((top, right, bottom, left))
        return faces
    except Exception:
        return []


def _load_mediapipe_face_mesh():
    """Load mediapipe face mesh with fallback."""
    try:
        import mediapipe as mp  # type: ignore
        return mp.solutions.face_mesh.FaceMesh(
            static_image_mode=True,
            max_num_faces=1,
            min_detection_confidence=0.5
        )
    except Exception:
        return None


def _get_landmarks_embedding(landmarks) -> Optional[List[float]]:
    """Convert 468 face landmarks to 128-dim embedding via PCA-like compression."""
    if not landmarks:
        return None
    # Flatten 468 landmarks (x, y, z) into 1404-dim vector
    flat = np.array([[lm.x, lm.y, lm.z] for lm in landmarks]).flatten()
    # Simple dimensionality reduction: take key landmark groups
    # Eyes: 0-10, 160-180
    # Nose: 6, 19-50
    # Mouth: 61-100
    # Jaw: 200-250
    indices = list(range(0, 30, 2)) + list(range(60, 100, 2)) + list(range(200, 250, 2))
    selected = flat[[i * 3 for i in indices if i * 3 < len(flat)]][:128]
    # Pad to 128 dimensions if needed
    if len(selected) < 128:
        selected = np.pad(selected, (0, 128 - len(selected)), mode='constant')
    return selected[:128].tolist()


def compute_face_embedding(rgb: np.ndarray, face_location: Tuple[int, int, int, int]) -> Optional[List[float]]:
    """Compute face embedding using mediapipe landmarks."""
    mesh = _load_mediapipe_face_mesh()
    if mesh is None:
        return None
    try:
        results = mesh.process(rgb)
        if not results.multi_face_landmarks or len(results.multi_face_landmarks) == 0:
            return None
        landmarks = results.multi_face_landmarks[0]
        embedding = _get_landmarks_embedding(landmarks.landmark)
        return embedding
    except Exception:
        return None


def _face_crop(rgb: np.ndarray, face_location: Tuple[int, int, int, int]) -> Image.Image:
    top, right, bottom, left = face_location
    crop = rgb[top:bottom, left:right]
    return Image.fromarray(crop)


def _face_to_bytes(face_image: Image.Image) -> bytes:
    output = io.BytesIO()
    face_image.save(output, format="JPEG", quality=90)
    return output.getvalue()


def _distance_to_similarity(distance: float) -> float:
    normalized = max(0.0, min(1.0, 1.0 - distance / 0.6))
    return normalized * 100.0


def _estimate_smile_score(rgb: np.ndarray, face_location: Tuple[int, int, int, int]) -> float:
    """Estimate smile score from face landmarks using mediapipe."""
    mesh = _load_mediapipe_face_mesh()
    if mesh is None:
        return 0.0
    try:
        results = mesh.process(rgb)
        if not results.multi_face_landmarks or len(results.multi_face_landmarks) == 0:
            return 0.0
        landmarks = results.multi_face_landmarks[0].landmark
        # Mouth landmarks: 61-100 (especially 61, 291 for corners, 78, 308 for center)
        h, w = rgb.shape[:2]
        mouth_corners = [landmarks[61], landmarks[291]]  # Left and right corners
        mouth_center_upper = [landmarks[78], landmarks[308]]  # Upper center points
        xs = [lm.x * w for lm in mouth_corners + mouth_center_upper]
        ys = [lm.y * h for lm in mouth_corners + mouth_center_upper]
        width = max(xs) - min(xs)
        height = max(ys) - min(ys)
        if height <= 0:
            return 0.0
        ratio = width / height
        return min(100.0, max(0.0, (ratio - 1.4) * 70.0))
    except Exception:
        return 0.0


def _estimate_quality(rgb: np.ndarray, face_location: Tuple[int, int, int, int]) -> float:
    top, right, bottom, left = face_location
    face_region = rgb[top:bottom, left:right]
    if face_region.size == 0:
        return 0.0
    gray = cv2.cvtColor(face_region, cv2.COLOR_RGB2GRAY)
    brightness = float(np.mean(gray)) / 255.0
    image_area = float(rgb.shape[0] * rgb.shape[1])
    face_area = float(max(1, (bottom - top) * (right - left)))
    size_ratio = min(1.0, face_area / (image_area * 0.12))
    return min(100.0, brightness * 60.0 + size_ratio * 40.0)


def analyze_media(db_connection, media: Dict[str, any]) -> None:
    path = Path(media["path"])
    rgb = _read_image(path)
    if rgb is None:
        save_analysis_result(db_connection, media["id"], 0, 0.0, 0.0, 0.0)
        return
    face_locations = detect_faces(rgb)
    face_count = len(face_locations)
    best_family_score = 0.0
    best_smile_score = 0.0
    best_quality_score = 0.0
    all_person_embeddings = []
    person_ids = []
    for person in db_connection.execute("SELECT id FROM Person").fetchall():
        person_ids.append(person["id"])
        all_person_embeddings.extend(get_person_embeddings(db_connection, person["id"]))
    for location in face_locations:
        embedding = compute_face_embedding(rgb, location)
        if embedding is None:
            continue
        similarity = 0.0
        matched_person_id = None
        if all_person_embeddings:
            try:
                # Compute euclidean distances between embedding and all registered embeddings
                embedding_arr = np.array(embedding)
                distances = np.array([np.linalg.norm(embedding_arr - np.array(e)) for e in all_person_embeddings])
                best_index = int(np.argmin(distances))
                distance = float(distances[best_index])
                similarity = _distance_to_similarity(distance)
                matched_person_id = person_ids[best_index % len(person_ids)] if person_ids else None
            except Exception:
                pass
        cropped = _face_crop(rgb, location)
        face_image_bytes = _face_to_bytes(cropped)
        add_face_embedding(
            db_connection,
            matched_person_id,
            embedding,
            similarity,
            face_image_bytes,
            media_id=media["id"],
        )
        best_family_score = max(best_family_score, similarity)
        best_smile_score = max(best_smile_score, _estimate_smile_score(rgb, location))
        best_quality_score = max(best_quality_score, _estimate_quality(rgb, location))
    save_analysis_result(
        db_connection,
        media["id"],
        face_count,
        best_family_score,
        best_smile_score,
        best_quality_score,
        duplicate_group=None,
        event_category=None,
    )
    db_connection.execute(
        "UPDATE Media SET analyzed_date = ?, analyzer_version = ? WHERE id = ?",
        (datetime.utcnow().isoformat(), ANALYZER_VERSION, media["id"]),
    )
    db_connection.commit()


def analyze_database(db_connection) -> None:
    cursor = db_connection.cursor()
    rows = cursor.execute(
        "SELECT * FROM Media WHERE analyzed_date IS NULL OR analyzer_version != ? ORDER BY path",
        (ANALYZER_VERSION,),
    ).fetchall()
    for row in rows:
        analyze_media(db_connection, dict(row))


def detect_faces_in_file(path: str) -> List[Tuple[int, int, int, int]]:
    image_path = Path(path)
    rgb = _read_image(image_path)
    if rgb is None:
        return []
    return detect_faces(rgb)


def load_face_image_bytes(path: Path, face_location: Tuple[int, int, int, int]) -> bytes:
    rgb = _read_image(path)
    if rgb is None:
        raise FileNotFoundError(f"Cannot load image: {path}")
    cropped = _face_crop(rgb, face_location)
    return _face_to_bytes(cropped)
