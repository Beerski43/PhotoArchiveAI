"""連写・似た写真を束ねる（#86）。`select` が束ごとに代表1枚だけを選ぶための材料。

**なぜ要るか。** 実データの複製で `select` を流すと、上位に同じ場面の連写が並んだ
（2026-10-09）。`remove_duplicate` はファイルハッシュの完全一致しか見ないので、
連写は1枚も減らない。

**2段で決める**（利用者の決定・2026-10-10）。

1. **DB だけで候補を絞る**（`candidate_runs`）: 同じフォルダ・撮影日時が近い・
   写っている家族が同じ。撮影日時は秒まで要るので、EXIF の無い写真は束ねない
2. **見た目で確かめる**（`scenes`）: 候補の中だけ dHash を取り、近いものを同じ場面にする。
   **時間だけで束ねると、構図を変えた写真まで消える**（2秒以内に撮った2枚でも、
   距離の中央は 10。10分より離れた2枚は 24 以上。2026-10-10・実データ）

**元写真は丸ごと読まない**（NFS・実測 24MB/s）。JPEG の EXIF に埋め込まれた
サムネイル（160×120 前後）は先頭の数十KBにあるので、そこだけ読む（`HEAD_BYTES`）。
無いときだけ元写真を縮小デコードする。

**サムネイル由来と元写真由来の値は比べない**（`distance`）。カメラによっては
3:2 の写真を 4:3 のサムネイルに黒帯付きで収めるので、同じ写真でも距離がずれる。
"""

from __future__ import annotations

import io
import logging
import os
from datetime import datetime
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from PIL import ExifTags, Image

from .dates import taken_moment

logger = logging.getLogger("photoarchive.similar")

#: 見た目の値の作り方。**変えたら上げる。** 保存済みの値は版が違えば読み捨てて測り直す。
LOOK_VERSION = "dhash8"

#: EXIF のサムネイルから取った値と、元写真を縮小して取った値。互いに比べない。
FROM_EXIF = "exif"
FROM_IMAGE = "full"

#: 先頭から読む量。EXIF（APP1）は 64KB を超えない。
HEAD_BYTES = 128 * 1024

#: 同じ場面とみなす撮影日時の間隔（秒）。**連なりで束ねる**（隣どうしがこの内側なら同じ候補）。
DEFAULT_SECONDS = 10

#: 同じ場面とみなす dHash の距離（64ビットのうち違うビットの数）。
#:
#: **実データで見比べて決めた**（2026-10-10・`scripts/measure_similar_photos.py`）。
#: 候補の中の隣り合う2枚を距離の帯ごとに 12 組ずつ見たところ、11〜14・15〜18 は
#: すべて同じ場面の連写、19〜24 は構図や写っている子が変わった組が混ざった。
#: 10分より離れて撮った2枚は 24 以上（中央 31）。
DEFAULT_DISTANCE = 16


def dhash(image: Image.Image) -> int:
    """64ビットの dHash。灰色にして 9×8 へ縮め、隣の画素との大小をビットにする。"""
    pixels = np.asarray(image.convert("L").resize((9, 8), Image.LANCZOS), dtype=np.int16)
    bits = (pixels[:, 1:] > pixels[:, :-1]).flatten()
    return int("".join("1" if bit else "0" for bit in bits), 2)


def encode(source: str, value: int) -> str:
    """`Media.look_hash` に書く形。``dhash8/exif:0123456789abcdef``。"""
    return f"{LOOK_VERSION}/{source}:{value:016x}"


def decode(text: Optional[str]) -> Optional[Tuple[str, int]]:
    """``(どこから取ったか, 値)``。未計測・版違い・読めない値は ``None``（測り直す）。"""
    if not text:
        return None
    head, _, value = text.partition(":")
    version, _, source = head.partition("/")
    if version != LOOK_VERSION or source not in (FROM_EXIF, FROM_IMAGE):
        return None
    try:
        return source, int(value, 16)
    except ValueError:
        return None


def distance(a: Optional[str], b: Optional[str]) -> Optional[int]:
    """2枚の見た目の距離。**比べられなければ ``None``**（未計測・取った場所が違う）。"""
    left, right = decode(a), decode(b)
    if left is None or right is None or left[0] != right[0]:
        return None
    return bin(left[1] ^ right[1]).count("1")


def _exif_segment(head: bytes) -> Optional[bytes]:
    """JPEG の先頭から EXIF（APP1・``Exif`` と NUL 2バイトで始まる）の中身を取り出す。

    **画像として開かない。** EXIF の後ろの見出し（量子化表など）まで読めていなくても取れる。
    """
    if not head.startswith(b"\xff\xd8"):
        return None
    position = 2
    while position + 4 <= len(head):
        if head[position] != 0xFF:
            return None
        marker = head[position + 1]
        if marker == 0xDA:  # 本体の始まり。EXIF はこれより前にある
            return None
        length = int.from_bytes(head[position + 2 : position + 4], "big")
        body = head[position + 4 : position + 2 + length]
        if marker == 0xE1 and body.startswith(b"Exif\x00\x00"):
            return body if len(body) == length - 2 else None
        position += 2 + length
    return None


def exif_thumbnail(head: bytes) -> Optional[Image.Image]:
    """先頭のバイト列から、EXIF に埋め込まれたサムネイルを取り出す。無ければ ``None``。"""
    raw = _exif_segment(head)
    if raw is None:
        return None
    try:
        exif = Image.Exif()
        exif.load(raw)
        ifd1 = exif.get_ifd(ExifTags.IFD.IFD1)
    except Exception:
        return None
    offset, length = ifd1.get(0x0201), ifd1.get(0x0202)
    if not offset or not length:
        return None
    start = 6 + offset
    data = raw[start : start + length]
    if len(data) != length:
        return None
    try:
        thumbnail = Image.open(io.BytesIO(data))
        thumbnail.load()
    except Exception:
        return None
    return thumbnail


def measure(path: str) -> Optional[str]:
    """1枚の見た目の値（`encode` の形）。**読めなければ ``None``**（保存しない。次の回に測り直す）。"""
    try:
        with open(path, "rb") as handle:
            head = handle.read(HEAD_BYTES)
        thumbnail = exif_thumbnail(head)
        if thumbnail is not None:
            return encode(FROM_EXIF, dhash(thumbnail))
        with Image.open(path) as image:
            # JPEG は縮小しながらデコードできる（1/8 まで）。全画素を起こさない。
            image.draft("L", (64, 64))
            return encode(FROM_IMAGE, dhash(image))
    except Exception as error:  # 壊れたファイル・消えたファイル
        logger.warning("見た目を測れませんでした: %s (%s)", path, error)
        return None


def _moment(media: Dict[str, Any]) -> Optional[datetime]:
    if media.get("type") != "image":
        return None
    return taken_moment(media.get("shooting_date"))


def candidate_runs(
    media_list: Iterable[Dict[str, Any]],
    family_of: Dict[int, FrozenSet[int]],
    seconds: float = DEFAULT_SECONDS,
) -> List[List[Dict[str, Any]]]:
    """同じ場面かもしれない写真の連なり（2枚以上のものだけ）。**DB の値だけで決める。**

    同じフォルダで、撮影日時の差が ``seconds`` 以内に連なり、写っている家族の集合が
    隣と同じもの。家族の写っていない写真どうしも（空の集合として）束ねうる。
    撮影日時が秒まで読めない写真と動画は入らない。
    """
    dated = []
    for media in media_list:
        moment = _moment(media)
        if moment is not None:
            dated.append((os.path.dirname(media["path"]), moment, media))
    dated.sort(key=lambda item: (item[0], item[1], item[2]["id"]))
    runs: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    previous = None
    for folder, moment, media in dated:
        family = family_of.get(media["id"], frozenset())
        joins = (
            previous is not None
            and folder == previous[0]
            and (moment - previous[1]).total_seconds() <= seconds
            and family == previous[2]
        )
        if not joins:
            if len(current) > 1:
                runs.append(current)
            current = []
        current.append(media)
        previous = (folder, moment, family)
    if len(current) > 1:
        runs.append(current)
    return runs


def scenes(
    run: Sequence[Dict[str, Any]], looks: Dict[int, Optional[str]], max_distance: int = DEFAULT_DISTANCE
) -> List[List[Dict[str, Any]]]:
    """候補の連なりを、見た目の近さで場面に分ける。**近い2枚が1組でもあれば同じ場面**（連結）。

    見た目を測れなかった写真は、どの写真とも束ねない（消さずに残す）。
    """
    parent = list(range(len(run)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for i in range(len(run)):
        for j in range(i + 1, len(run)):
            gap = distance(looks.get(run[i]["id"]), looks.get(run[j]["id"]))
            if gap is not None and gap <= max_distance:
                parent[root(j)] = root(i)
    grouped: Dict[int, List[Dict[str, Any]]] = {}
    for index, media in enumerate(run):
        grouped.setdefault(root(index), []).append(media)
    return list(grouped.values())
