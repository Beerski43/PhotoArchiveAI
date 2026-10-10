"""顔の見え方（整列できるか・向き・鮮明さ）を、保存済みサムネイルから測る。

**なぜ要るか（2026-10-09・#66）。** 精度を落としていたのは閾値ではなく手本だった。

- **5点整列ができない顔**の特徴量は整列されていない（`face.align_for_arcface` は
  目印が取れないと縮小で通す）。ArcFace は整列を省くと大きく落ちる（CLAUDE.md §8）。
  そういう手本が他人を引き寄せる「磁石」になっていた。実データで、
  **割り当ての根拠から外すだけで誤りが −26%**（`match` の判定。測定は
  docs/history/details/2026-10-09-age-threshold-measured.md）
- `select` は「家族の顔がはっきり写っている写真」を選びたい（利用者の要望）。
  **ボケと横顔**を見分ける材料が要る

**元写真は読まない。** サムネイル（検出矩形の生クロップ）だけで測る。実データの
特徴量はすべてサムネイルから作り直したもの（`reembed`）なので、整列の判定と
特徴量の作り方が一致する。

測るのは**使う顔だけ**（割り当てのある顔）。1件 約17ms。
"""

from __future__ import annotations

import io
import logging
from typing import Callable, NamedTuple, Optional

import numpy as np

logger = logging.getLogger("photoarchive.appearance")

#: 鮮明さを測るときにそろえる大きさ。**ArcFace の入力と同じ。**
#: サムネイルの大きさは顔ごとに違い（短辺の中央 160px）、そのままでは
#: ラプラシアン分散が比べられない。
SHARPNESS_SIZE = 112

#: 一度に読むサムネイルの件数。
FILL_CHUNK_SIZE = 500


class Appearance(NamedTuple):
    """1つの顔の見え方。"""

    #: 5点整列ができたか。
    aligned: bool
    #: 横向きの度合い（鼻のずれ ÷ 両目の間隔）。正面で 0。整列できなければ None。
    yaw: Optional[float]
    #: 鮮明さ（ラプラシアン分散）。小さいほどボケている。
    sharpness: float


def yaw_from_points(points: np.ndarray) -> Optional[float]:
    """整列用の5点（左目・右目・鼻・口の左・口の右）から横向きの度合いを出す。

    **正面なら鼻は両目の真ん中にある。** 横を向くほど片側へずれる。
    両目の間隔で割るので、顔の大きさに依らない。
    """
    points = np.asarray(points, dtype=np.float64)
    left_eye, right_eye, nose = points[0], points[1], points[2]
    distance = float(np.linalg.norm(right_eye - left_eye))
    if distance <= 0:
        return None
    middle = (left_eye[0] + right_eye[0]) / 2.0
    return abs(float(nose[0]) - middle) / distance


def sharpness_of(rgb: np.ndarray) -> float:
    """ラプラシアン分散。`SHARPNESS_SIZE` にそろえてから測る。"""
    import cv2

    resized = cv2.resize(
        np.ascontiguousarray(rgb), (SHARPNESS_SIZE, SHARPNESS_SIZE), interpolation=cv2.INTER_AREA
    )
    gray = cv2.cvtColor(resized, cv2.COLOR_RGB2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _five_points(rgb: np.ndarray) -> Optional[np.ndarray]:
    """整列用の5点。**特徴量を作るときと同じ `face.detect_five_points`。**

    1か所に通すのは、テストのフェイクが差し替える口を1つにするため
    （`tests/fakes.py`。フェイクは特徴量の経路では5点を取れないことにしている）。
    """
    from . import face

    return face.detect_five_points(rgb)


def measure(rgb: np.ndarray) -> Optional[Appearance]:
    """顔画像（RGB）の見え方を測る。**目印の検出器が無い環境では None。**

    **整列の判定は `face.detect_five_points` に預ける**（特徴量を作るときと同じ関数）。
    ここで別の検出器を使うと、「整列できた」の意味が特徴量とずれる。

    **「測れない」と「測って整列できなかった」を混ぜない。** FaceMesh が読めない
    環境で ``aligned=False`` を書くと、**全部の手本が根拠から外れ**、二度と
    測り直されない（未計測ではなくなるため）。
    """
    from . import scoring

    if scoring._load_mediapipe_face_mesh() is None:
        return None
    points = _five_points(rgb)
    yaw = None if points is None else yaw_from_points(points)
    return Appearance(aligned=points is not None, yaw=yaw, sharpness=sharpness_of(rgb))


def measure_thumbnail(thumbnail: bytes) -> Optional[Appearance]:
    """保存済みサムネイル（JPEG）の見え方。読めなければ None。"""
    from PIL import Image

    try:
        rgb = np.asarray(Image.open(io.BytesIO(thumbnail)).convert("RGB"))
    except Exception as error:  # 壊れたサムネイル
        logger.warning("Could not read a face thumbnail: %s", error)
        return None
    if rgb.size == 0:
        return None
    return measure(rgb)


def fill_missing(
    connection,
    assign_sources=None,
    write: bool = True,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    """見え方が未計測の顔を測る。``{face_id: Appearance}`` を返す。

    ``assign_sources`` で対象を絞る（既定は手本と自動割り当て）。
    ``write=False`` なら DB に書かない（**`evaluate` は書かない約束**なので）。

    **測れなかった顔（サムネイルが壊れている）は書かない。** 書くと「測った」
    ことになり、二度と測り直されない。
    """
    from . import db

    sources = assign_sources or (db.ASSIGN_MANUAL, db.ASSIGN_AUTO)
    # **サムネイルを全件まとめて読まない**（数万件で数百MB。CLAUDE.md §8）。
    # id だけ先に取り、塊ごとに読む。
    face_ids = db.faces_without_appearance(connection, sources)
    measured = {}
    total = len(face_ids)
    if total and progress_callback is not None:
        progress_callback(0, total, "measuring faces")
    done = 0
    for start in range(0, total, FILL_CHUNK_SIZE):
        chunk = face_ids[start : start + FILL_CHUNK_SIZE]
        rows = []
        for face_id, thumbnail in db.face_thumbnails(connection, chunk).items():
            result = measure_thumbnail(thumbnail) if thumbnail is not None else None
            if result is not None:
                measured[face_id] = result
                rows.append((face_id, result))
        if write and rows:
            db.save_appearance(connection, rows)
        done += len(chunk)
        if progress_callback is not None:
            progress_callback(done, total, "measuring faces")
    return measured
