"""連写・似た写真を束ねる（#86）。`select` が束ごとに代表1枚だけを選ぶための材料。

**なぜ要るか。** 実データの複製で `select` を流すと、上位に同じ場面の連写が並んだ
（2026-10-09）。`remove_duplicate` はファイルハッシュの完全一致しか見ないので、
連写は1枚も減らない。

**2段で決める**（利用者の決定・2026-10-10）。

1. **DB だけで候補を絞る**（`candidate_groups`）: 同じフォルダ・同じ日（#93。
   日をまたぐ連写は撮影日時の近さでつなぐ）。撮影日時は秒まで要るので、EXIF の無い写真は入らない
2. **見た目で確かめる**（`scenes`）: 候補の中だけ dHash を取り、並びの順に見て、残した写真と
   近く、写っている家族が1人でも重なるものを同じ場面にする（連結にしない・#93。家族の
   集合が同じことまでは求めない・#98）。時間の離れた2枚は `FAR_DISTANCE` で厳しく見る。

これとは別に、**フォルダ・撮影日時を問わず同じ年の中で見た目がほぼ同じ写真**（別の端末の
写し・送られてきた写真）は、`select` が年ごとに選ぶところで `COPY_DISTANCE` で外す（#98）。
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
from functools import lru_cache
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

#: **代表と撮影日時が離れた写真**（代表との差が ``similar_seconds`` を超える）に使う距離の上限。
#: 連写の中でも、代表から ``similar_seconds`` より後ろの写真はこちらで見る（隣との差ではない。
#: 実データの測定もこの定義で数えた。PR #94 のレビュー指摘1）。
#:
#: 同じ行事の中まで候補を広げたとき（#93）に実データで見比べて決めた（2026-10-11）。
#: 外れた写真と外す原因になった写真の組を全部見たところ、14 以下はすべて同じ場面、
#: 時間の離れた 15〜16 の 8 組のうち 3 組は別の場面（着物で座った写真とドレスで立った
#: 写真・別のスタジオセット）だった。連写は #86 で 16 まで同じ場面だと確かめてあるので、
#: こちらだけ厳しくする（利用者の決定）。ルールの ``similar_distance`` がこれより小さければ
#: そちらに従う。
FAR_DISTANCE = 14

#: **フォルダ・撮影日時を問わず**、同じ年の中で同じ写真とみなす距離の上限（#98）。
#:
#: 別の端末のフォルダにある写し・送られてきて保存し直した写真（EXIF の日時が無い）は、
#: 同じフォルダ・同じ日の候補（`candidate_groups`）に入らない。利用者の `select` の
#: 出力で、そういう組が 29 組あった（2026-10-12）。総当たりで見ると、距離 0〜9 は
#: すべて同じ写真か同じ場面（年賀状に加工した写しも含む）、**10 に別の場面が1組**
#: （結婚式の入場と挨拶）。9 は利用者の決定。
#:
#: **両方に撮影日時があって日付が違えば比べない**（`on_different_days`。別の日は別の行事）。
COPY_DISTANCE = 9


def dhash(image: Image.Image) -> int:
    """64ビットの dHash。灰色にして 9×8 へ縮め、隣の画素との大小をビットにする。"""
    pixels = np.asarray(image.convert("L").resize((9, 8), Image.LANCZOS), dtype=np.int16)
    bits = (pixels[:, 1:] > pixels[:, :-1]).flatten()
    return int("".join("1" if bit else "0" for bit in bits), 2)


def encode(source: str, value: int) -> str:
    """`Media.look_hash` に書く形。``dhash8/exif:0123456789abcdef``。"""
    return f"{LOOK_VERSION}/{source}:{value:016x}"


def decode_all(text: Optional[str]) -> Dict[str, int]:
    """取った場所ごとの値。**先頭が主の値**（`measure` が取ったもの）。

    元写真から測り直した値を後ろに足して持つことがある（#98・`with_full`）:
    ``dhash8/exif:0123456789abcdef,full:fedcba9876543210``。
    未計測・版違い・読めない値は空（測り直す）。
    """
    if not text:
        return {}
    return dict(_decode_parts(text))


@lru_cache(maxsize=200_000)
def _decode_parts(text: str) -> Tuple[Tuple[str, int], ...]:
    """`decode_all` の中身。**結果を覚える**（#104。GUI の並べ直しで同じ値を何度も読む）。

    返すのは書き換えられない形（呼び出し側の辞書を共有しないため）。
    """
    version, _, rest = text.partition("/")
    if version != LOOK_VERSION:
        return ()
    values: Dict[str, int] = {}
    for part in rest.split(","):
        source, _, value = part.partition(":")
        if source not in (FROM_EXIF, FROM_IMAGE) or source in values:
            return ()
        try:
            values[source] = int(value, 16)
        except ValueError:
            return ()
    return tuple(values.items())


def decode(text: Optional[str]) -> Optional[Tuple[str, int]]:
    """``(どこから取ったか, 値)``（主の値）。未計測・版違い・読めない値は ``None``（測り直す）。"""
    values = decode_all(text)
    if not values:
        return None
    source = next(iter(values))
    return source, values[source]


def with_full(text: str, value: int) -> str:
    """主の値に、元写真から測った値を足した形。主が元写真なら、そのまま。"""
    if FROM_IMAGE in decode_all(text):
        return text
    return f"{text},{FROM_IMAGE}:{value:016x}"


def distance(a: Optional[str], b: Optional[str]) -> Optional[int]:
    """2枚の見た目の距離。**比べられなければ ``None``**（未計測・同じ場所から取った値が無い）。

    **取った場所が同じ値どうしだけを比べる**（サムネイル由来と元写真由来は比べない）。
    両方に元写真の値があれば、それを使う。
    """
    left, right = decode_all(a), decode_all(b)
    for source in (FROM_EXIF, FROM_IMAGE):
        if source in left and source in right:
            return bin(left[source] ^ right[source]).count("1")
    return None


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


def measure_full(path: str) -> Optional[int]:
    """元写真を縮小デコードした dHash。**EXIF のサムネイルは見ない。** 読めなければ ``None``。"""
    try:
        with Image.open(path) as image:
            image.draft("L", (64, 64))
            return dhash(image)
    except Exception as error:  # 壊れたファイル・消えたファイル
        logger.warning("見た目を測れませんでした: %s (%s)", path, error)
        return None


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


def candidate_groups(
    media_list: Iterable[Dict[str, Any]],
    seconds: float = DEFAULT_SECONDS,
) -> List[List[Dict[str, Any]]]:
    """同じ場面かもしれない写真の組（2枚以上のものだけ）。**DB の値だけで決める。**

    **同じフォルダ・同じ日**の写真（#93。行事の中なら時間は問わない。利用者の決定）。
    撮影日時の順に並べ、隣と同じ日か、差が ``seconds`` 以内なら連なる（後者は
    **日付をまたぐ連写**を落とさないため。#86 の連写の束はすべてこの組にも入る）。
    撮影日時が秒まで読めない写真と動画は入らない（写しは `selection._pick_by_year` が拾う）。

    **写っている家族の集合では分けない**（#98）。以前は集合が同じ写真だけを組にして
    いたので、顔の取りこぼしで1人ずれた同じ場面を束ねなかった（利用者の出力で 37 組）。
    家族が重なるかは `scenes` が代表と比べて見る。
    """
    dated = []
    for media in media_list:
        moment = _moment(media)
        if moment is not None:
            dated.append((os.path.dirname(media["path"]), moment, media))
    dated.sort(key=lambda item: (item[0], item[1], item[2]["id"]))
    groups: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    previous = None
    for folder, moment, media in dated:
        joins = (
            previous is not None
            and folder == previous[0]
            and (
                moment.date() == previous[1].date()
                or (moment - previous[1]).total_seconds() <= seconds
            )
        )
        if not joins:
            if len(current) > 1:
                groups.append(current)
            current = []
        current.append(media)
        previous = (folder, moment)
    if len(current) > 1:
        groups.append(current)
    return groups


def on_different_days(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    """**両方に撮影日時があり、日付が違う**か。どちらかが読めなければ False（#98）。

    別の日は別の行事（#93・利用者の決定）。写しの組（`COPY_DISTANCE`）でも守る。
    実データの写しの組は、両方に日時があれば同じ日（差は数秒）だった（2026-10-12）。
    """
    first, second = _moment(left), _moment(right)
    return first is not None and second is not None and first.date() != second.date()


def shares_family(
    family_of: Optional[Dict[int, FrozenSet[int]]], left: Dict[str, Any], right: Dict[str, Any]
) -> bool:
    """2枚に写っている家族が1人でも重なるか（#98）。**家族の写っていない写真どうしは重なる扱い。**

    ``family_of`` が無ければ問わない。
    """
    if family_of is None:
        return True
    a = family_of.get(left["id"], frozenset())
    b = family_of.get(right["id"], frozenset())
    return bool(a & b) or (not a and not b)


def scenes(
    group: Sequence[Dict[str, Any]],
    looks: Dict[int, Optional[str]],
    max_distance: int = DEFAULT_DISTANCE,
    seconds: float = DEFAULT_SECONDS,
    family_of: Optional[Dict[int, FrozenSet[int]]] = None,
    far_distance: int = FAR_DISTANCE,
) -> List[List[Dict[str, Any]]]:
    """候補の組を場面に分ける。``group`` は**並びの順**（残したい順）に渡す。

    先頭から順に見て、**すでに残した写真のどれかと近ければ、その場面に入れる**（残さない）。
    どれとも近くなければ、新しい場面の代表として残す。各場面の先頭が代表。

    **連結（1組でも近ければ同じ場面）にしない**（#93）。候補を行事まるごとに広げると、
    A〜B・B〜C が近いだけで遠い A と C が同じ場面になる。実データでは、残した代表から
    距離 18〜26 の写真（挙式と披露宴、衣装の違う写真）まで外れた（2026-10-11）。

    近さの上限は、**代表との**撮影日時の差が ``seconds`` 以内なら ``max_distance``（連写）、
    離れていれば ``far_distance``（既定 `FAR_DISTANCE`・ルールの ``similar_far_distance``）と
    ``max_distance`` の小さいほう。
    **日付の違う2枚は、連写（差が ``seconds`` 以内）のときだけ比べる。** 候補の組は
    0時をまたぐ連写で前後の日がつながるが、別の日は別の行事（PR #94 のレビュー指摘2）。
    見た目を測れなかった写真は、どの写真とも束ねない（消さずに残す）。

    **代表と写っている家族が1人も重ならなければ、近くても束ねない**（#98。
    `shares_family`）。集合が同じことまでは求めない（顔の取りこぼしでずれるため）。
    """
    far = min(max_distance, far_distance)
    kept: List[List[Dict[str, Any]]] = []
    for media in group:
        home = None
        for scene in kept:
            representative = scene[0]
            if not shares_family(family_of, representative, media):
                continue
            gap = distance(looks.get(representative["id"]), looks.get(media["id"]))
            if gap is None:
                continue
            apart = abs((_moment(representative) - _moment(media)).total_seconds())
            if apart > seconds and _moment(representative).date() != _moment(media).date():
                continue  # 別の日は別の行事。日付をまたいでつなぐのは連写だけ
            if gap <= (max_distance if apart <= seconds else far):
                home = scene
                break
        if home is None:
            kept.append([media])
        else:
            home.append(media)
    return kept
