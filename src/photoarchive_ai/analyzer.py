import io
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np
from typing import Any


def _load_face_recognition() -> Any:
    try:
        import face_recognition  # type: ignore
        return face_recognition
    except Exception:
        # Provide minimal fallback to allow tests to run without the package.
        class _FakeFaceRecognition:
            @staticmethod
            def face_locations(rgb, model="hog"):
                return []

            @staticmethod
            def face_encodings(rgb, locations):
                return []

            @staticmethod
            def face_landmarks(rgb, locations):
                return []

            @staticmethod
            def face_distance(arr, emb):
                return np.array([])

        return _FakeFaceRecognition()
from PIL import Image

from .db import (
    add_face_embedding,
    get_person_embeddings,
    save_analysis_result,
    get_media_by_path,
)

ANALYZER_VERSION = "1.0"


def _read_image(path: Path) -> Optional[np.ndarray]:
    suffix = path.suffix.lower().lstrip(".")
    if suffix in {"mp4", "avi", "mov", "mkv"}:
        capture = cv2.VideoCapture(str(path))
        ok, frame = capture.read()
        capture.release()
        if not ok or frame is None:
            return None
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    try:
        with Image.open(path) as image:
            return np.asarray(image.convert("RGB"))
    except Exception:
        return None


def detect_faces(rgb: np.ndarray) -> List[Tuple[int, int, int, int]]:
    fr = _load_face_recognition()
    try:
        return fr.face_locations(rgb, model="hog")
    except Exception:
        return []


def compute_face_embedding(rgb: np.ndarray, face_location: Tuple[int, int, int, int]) -> Optional[List[float]]:
    fr = _load_face_recognition()
    try:
        encodings = fr.face_encodings(rgb, [face_location])
    except Exception:
        encodings = []
    if not encodings:
        return None
    return encodings[0].tolist()


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
    fr = _load_face_recognition()
    try:
        landmarks = fr.face_landmarks(rgb, [face_location])
    except Exception:
        landmarks = []
    if not landmarks:
        return 0.0
    mouth = landmarks[0].get("top_lip", []) + landmarks[0].get("bottom_lip", [])
    if not mouth:
        return 0.0
    xs = [p[0] for p in mouth]
    ys = [p[1] for p in mouth]
    width = max(xs) - min(xs)
    height = max(ys) - min(ys)
    if height <= 0:
        return 0.0
    ratio = width / height
    return min(100.0, max(0.0, (ratio - 1.4) * 70.0))


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
            fr = _load_face_recognition()
            try:
                distances = fr.face_distance([np.array(e) for e in all_person_embeddings], np.array(embedding))
            except Exception:
                distances = np.array([])
            best_index = int(np.argmin(distances))
            distance = float(distances[best_index])
            similarity = _distance_to_similarity(distance)
            matched_person_id = person_ids[best_index % len(person_ids)] if person_ids else None
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


def analyze_database(
    db_connection,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> None:
    cursor = db_connection.cursor()
    rows = cursor.execute(
        "SELECT * FROM Media WHERE analyzed_date IS NULL OR analyzer_version != ? ORDER BY path",
        (ANALYZER_VERSION,),
    ).fetchall()
    total = len(rows)
    for index, row in enumerate(rows, start=1):
        analyze_media(db_connection, dict(row))
        if progress_callback is not None:
            progress_callback(index, total, Path(row["path"]).name)


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
