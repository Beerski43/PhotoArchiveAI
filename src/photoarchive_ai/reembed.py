"""保存済みサムネイルから、顔の特徴量だけを作り直す。

**なぜ `scan` ではなく専用のコマンドなのか。**

1. **`scan --force-rescan` は手動割り当てを巻き添えにする。** 顔の行を消して
   作り直すので、GUI で積み上げた手本（実データで126件）と除外（268件）が消える
2. **元写真を読む必要がない。** 実データの元写真は NFS 上（実測 24MB/s）にあり、
   441GB を読み直すと 5.1 時間かかる。**サムネイルは DB の中にあり、全部で
   269MB。** 短辺の中央は 160px で、ArcFace の入力(112x112)に足りる
3. **検出結果を作り直す必要もない。** 矩形もサムネイルも変わらない

書き換えるのは **`Face.embedding` と `Face.embed_version` だけ。**
`person_id` / `assign_source` / `assign_score` / `assigned_at` / `age` に触らない。

**途中で止めても続きから再開できる。** 対象を「版が古い顔」で絞っているので、
済んだ顔は自然に対象から外れる。塊ごとに確定するので、止めた時点までが残る。

**並列化していない。** `onnxruntime` が1件の推論で既に全コアを使うし、`scan` の
並列はワーカーが親の SQLite 接続を複製して**実データのDBを壊した**（Issue #35）。
一度きりの作業なので、単一プロセスで安全側に倒す。

**逆に、ほかの重い処理と同時に走らせないこと。** 推論が全コアを使うので、
取り合うと**数倍遅くなる**（開発機で 190ms/件 → 1,765ms/件 を観測した）。
"""

from __future__ import annotations

import io
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

from . import db, embedding, face

logger = logging.getLogger("photoarchive.reembed")

#: 一度に読み書きする顔の件数。**塊ごとに確定する**ので、止めた時点までが残る。
CHUNK_SIZE = 200

#: サムネイルの短辺がこれ未満の顔は、特徴量を作らない。
#:
#: `face.EMBED_MIN_FACE_PX` と同じ基準。小さすぎる顔を 112x112 へ引き伸ばすと
#: 中身の無い特徴量ができ、**誤った紐づけの種になる。** 実データでは 443 件
#: （0.76%）が該当する。
#:
#: **作らないが、版は進める**（特徴量は NULL）。版を古いまま残すと
#: `count_faces_to_reembed` が数え続け、**全件終わったあとも毎回「作り直す顔:
#: 443 件」と出て「もう一度実行すれば続きから」と誤って案内する**（PR #60 の
#: 指摘3）。版を進めれば照合の対象から外れたまま、作り直しは完了に到達する。
#: `scan` が同じ状況を記録する形とも一致する。
MIN_THUMBNAIL_PX = face.EMBED_MIN_FACE_PX

ProgressCallback = Optional[Callable[[int, int, str], None]]


def thumbnail_to_rgb(blob: Optional[bytes]) -> Optional[np.ndarray]:
    """サムネイルの JPEG を RGB 配列に戻す。小さすぎるものは ``None``。"""
    if not blob:
        return None
    try:
        with Image.open(io.BytesIO(blob)) as image:
            if min(image.size) < MIN_THUMBNAIL_PX:
                return None
            return np.asarray(image.convert("RGB"))
    except Exception as error:
        logger.warning("サムネイルを読めなかった: %s", error)
        return None


def reembed_faces(
    connection,
    progress_callback: ProgressCallback = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
    chunk_size: int = CHUNK_SIZE,
) -> Dict[str, Any]:
    """版の古い顔の特徴量を、サムネイルから作り直す。

    ``dry_run`` は件数と見積り時間だけを返し、**DB に一切書かない。**
    """
    version = embedding.ACTIVE.version
    total = db.count_faces_to_reembed(connection, version)
    summary: Dict[str, Any] = {
        "version": version,
        "target": total,
        "written": 0,
        "too_small": 0,
        "failed": 0,
        "alignment_fallbacks": 0,
        "seconds": 0.0,
        "embedded": 0,
        "seconds_per_face": None,
        "dry_run": dry_run,
    }
    if limit is not None:
        summary["target"] = min(total, limit)
    if summary["target"] == 0:
        return summary
    if dry_run:
        # **見積りを定数で持たない。** 1件あたりの時間は機械と画像の大きさで
        # 変わる（開発機では実測 190ms）。少しだけ実際に作って測る。
        # **DB には書かない。**
        summary["seconds_per_face"] = _sample_rate(connection, version)
        return summary

    face.reset_alignment_fallbacks()
    started = time.monotonic()
    processed = 0
    pending: List[Tuple[int, bytes]] = []

    for face_id, thumbnail in db.iter_faces_to_reembed(connection, version, chunk_size):
        if limit is not None and processed >= limit:
            break
        processed += 1
        rgb = thumbnail_to_rgb(thumbnail)
        if rgb is None:
            # **作れないことを記録して進める。** 版を据え置くと永遠に対象に残る。
            summary["too_small"] += 1
            pending.append((face_id, None))
        else:
            vector = face.compute_embedding_from_face_image(rgb)
            if vector is None:
                summary["failed"] += 1
                pending.append((face_id, None))
            else:
                pending.append((face_id, db.encode_embedding(vector)))
        if len(pending) >= chunk_size:
            summary["written"] += db.save_face_embeddings(connection, pending, version)
            pending.clear()
        if progress_callback is not None:
            progress_callback(
                processed,
                summary["target"],
                f"{summary['written']} written",
            )

    if pending:
        summary["written"] += db.save_face_embeddings(connection, pending, version)
    # `written` は**書き戻した行数**。特徴量を作れた件数はそこから引く。
    summary["embedded"] = summary["written"] - summary["too_small"] - summary["failed"]
    summary["seconds"] = time.monotonic() - started
    if processed:
        summary["seconds_per_face"] = summary["seconds"] / processed
    summary["alignment_fallbacks"] = face.alignment_fallbacks()
    if progress_callback is not None:
        progress_callback(summary["target"], summary["target"], f"{summary['written']} written")
    return summary


#: 見積りのために実際に作ってみる件数。**DB には書かない。**
SAMPLE_SIZE = 12


def _sample_rate(connection, version: str) -> float:
    """1件あたりの所要時間を、少しだけ実際に作って測る。

    **定数で持たない。** 機械の速さと顔の大きさで変わるし、固定した数字は
    黙って古くなる。
    """
    started = time.monotonic()
    measured = 0
    for _face_id, thumbnail in db.iter_faces_to_reembed(connection, version, SAMPLE_SIZE):
        rgb = thumbnail_to_rgb(thumbnail)
        if rgb is not None and face.compute_embedding_from_face_image(rgb) is not None:
            measured += 1
        if measured >= SAMPLE_SIZE:
            break
    if measured == 0:
        return 0.0
    return (time.monotonic() - started) / measured


def format_summary(summary: Dict[str, Any]) -> str:
    """結果を人が読む形にする。``dry_run`` なら見積りを出す。"""
    if summary["dry_run"]:
        per_face = summary.get("seconds_per_face") or 0.0
        minutes = summary["target"] * per_face / 60.0
        return (
            f"作り直す顔: {summary['target']:,} 件（版 {summary['version']}）\n"
            f"見積り: 約 {minutes:.0f} 分"
            f"（{SAMPLE_SIZE} 件を実際に作って測った 1件あたり {per_face * 1000:.0f}ms）\n"
            "元写真は読みません（サムネイルから作り直します）。\n"
            "**ほかの重い処理と同時に走らせないこと。** モデルの推論は全コアを"
            "使うので、取り合うと数倍遅くなります。"
        )
    lines = [
        f"作り直した顔: {summary.get('embedded', summary['written']):,}"
        f" / {summary['target']:,} 件（版 {summary['version']}）",
        f"所要: {summary['seconds'] / 60.0:.1f} 分"
        f"（1件あたり {(summary.get('seconds_per_face') or 0.0) * 1000:.0f}ms）",
    ]
    if summary["too_small"]:
        lines.append(
            f"サムネイルが {MIN_THUMBNAIL_PX}px 未満で特徴量を作らなかった顔:"
            f" {summary['too_small']:,} 件（特徴量は NULL。照合の対象外）"
        )
    if summary["failed"]:
        lines.append(f"特徴量を作れなかった顔: {summary['failed']:,} 件（特徴量は NULL）")
    if summary["alignment_fallbacks"]:
        lines.append(
            f"5点が取れず縮小で通した顔: {summary['alignment_fallbacks']:,} 件"
            "（整列ありのほうが精度が高いので、件数として把握しておく）"
        )
    if summary["written"] < summary["target"]:
        # **作れなかった顔を「残り」に数えない。** 数えると、全件終わったあとも
        # 毎回「もう一度実行すれば」と誤って案内する（PR #60 の指摘3）。
        lines.append("残りは、もう一度実行すれば続きから作り直します。")
    return "\n".join(lines)
