"""家族の写真に写った**他人(減点)の顔**を集める（#100・#110・#139）。`select` の並びで点を引くため。

**なぜ要るか。** 家族の写りが良ければ、隣に写したくない人が大きく写っていても上位に来る（利用者の要望・2026-10-12）。

**減点するのは、利用者が選んだ顔だけ**（#110・2026-10-17 の利用者の決定）。顔認証外（`rejected`）にした顔のうち
「減点する」の印（`Face.penalize`）を付けた顔だけを数え、**未分類の顔と、印の無い顔認証外の顔は加点にも減点にも
入れない。**

**引く点は、家族の顔と同じ項目で出した顔の点**（#139・利用者の決定）。鮮明さ・正面・目・笑顔・大きさ・見切れで
`scoring.face_score` を出し、その和に重み（`Weights.strangers`）をかけて引く。以前（#100・#110）は大きさ
（家族の顔と比べた「目立ち具合」）だけで引いていたので、ボケた背景の他人もくっきり写った他人と同じだけ引かれた。
見え方と表情は `select` の準備（`selection.prepare`）と `match` のあとで測る（`appearance.fill_missing` の
``penalized``）。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List


def faces(connection, family_rows: Iterable[Dict[str, Any]]) -> Dict[int, List[Dict[str, Any]]]:
    """写真ごとの他人(減点)の顔（``{media_id: [行]}``・無い写真は入らない）。

    ``family_rows`` は `db.family_faces` の行。**家族の写った写真だけ**を見る（家族の写っていない写真は点が付かない）。
    行は `db.penalized_faces` の形（見え方・表情・矩形）。
    """
    from . import db

    media_ids = {row["media_id"] for row in family_rows if db.is_family(row)}
    if not media_ids:
        return {}
    found: Dict[int, List[Dict[str, Any]]] = {}
    for face in db.penalized_faces(connection, sorted(media_ids)):
        found.setdefault(face["media_id"], []).append(face)
    return found
