import math
import os
import re
import shutil
from functools import lru_cache
from datetime import date, datetime, time
from pathlib import Path
from typing import (
    Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple, Union,
)

import yaml

from . import appearance, db, expression, scoring, similar, strangers
from .dates import parse_date, taken_at
from .db import get_media_with_analysis
from .quality import MODEL_VERSION as QUALITY_VERSION


def load_rule(rule_path: str) -> Dict[str, Any]:
    """抽出ルールを読む。**YAML だけ**（#27）。

    ``.json`` は中身が YAML として読めても受け付けない。設定と同じく形式を1つに
    揃えるためで、古いファイルを指したままの設定に気づけるよう止める。
    """
    path = Path(rule_path)
    if path.suffix.lower() == ".json":
        raise ValueError(
            f"ルールファイル {rule_path} は JSON です。#27 で YAML だけを読むようになりました。"
            f" 同じ中身を {path.with_suffix('.yml')} に YAML で書き、rule_path をそちらへ向けてください。"
        )
    if not path.exists():
        raise FileNotFoundError(f"Rule file not found: {rule_path}")
    rule = yaml.safe_load(path.read_text(encoding="utf-8"))
    if rule is None:
        return {}
    if not isinstance(rule, dict):
        raise ValueError(f"ルールファイル {rule_path} の中身が辞書ではありません。")
    for key, default in RULE_NUMBERS.items():
        _rule_number(rule, key, default)
    for key in RULE_FLAGS:
        _rule_flag(rule, key)
    rule_weights(rule)
    year_limits(rule)
    return rule


#: ルールの数のキーと既定値（似た写真・写しの判定。#86・#93・#98・#103）。
RULE_NUMBERS = {
    "similar_seconds": similar.DEFAULT_SECONDS,
    "similar_distance": similar.DEFAULT_DISTANCE,
    "similar_far_distance": similar.FAR_DISTANCE,
    "copy_distance": similar.COPY_DISTANCE,
}

#: ルールの ``weights:`` に書けるキー（`scoring.Weights` の項目）。
WEIGHT_KEYS = scoring.Weights._fields


def rule_weights(rule: Dict[str, Any]) -> "scoring.Weights":
    """ルールの ``weights:`` から並びの重みを作る（#103）。書いていない項目は既定値。

    **知らないキーは止める**（綴りを間違えると、黙って既定値のまま並ぶため）。
    """
    given = rule.get("weights") or {}
    if not isinstance(given, dict):
        raise ValueError(f"ルールの weights は項目と数の組で書いてください: {given!r}")
    unknown = sorted(set(given) - set(WEIGHT_KEYS))
    if unknown:
        raise ValueError(
            f"ルールの weights に知らない項目があります: {', '.join(map(str, unknown))}"
            f"（書けるのは {', '.join(WEIGHT_KEYS)}）"
        )
    default = scoring.Weights.default()
    return scoring.Weights(**{
        key: float(_rule_number(given, key, getattr(default, key), label=f"weights.{key}"))
        for key in WEIGHT_KEYS
    })


#: 年ごとの対応で「年の分からない写真」の枚数を書くキー（PR #108・利用者の決定）。
COUNT_UNKNOWN = "unknown"
#: 年ごとの対応で「その年以降」の枚数を書くキーの形（``2027+``）。PR #108・利用者の決定。
LATER_SUFFIX = "+"


def year_limits(rule: Dict[str, Any]) -> Optional[Callable[[Optional[int]], Optional[int]]]:
    """年ごとに選ぶ枚数（``count_per_year``・#103）。``年 -> 枚数（None は上限なし）``。

    - 整数: 全部の年に同じ数（以前からの書き方）。0 や省略は上限なし（以前と同じ）
    - **年と枚数の対応**（``{2018: 50, 2019: 30, unknown: 10, 2027+: 30}``）:
      **書いていない年は選ばない**（利用者の決定・#102。既定の枚数は持たない）
      - ``unknown``: 年の分からない写真（撮影日時もフォルダ名からも年が決まらない）の枚数。
        書かなければ選ばない
      - ``2027+``: その年**以降**で、対応に書いていない年の枚数。これから増える年のため。
        **年が変わったら GUI で設定し直す運用**（利用者の決定・PR #108）。1つだけ書ける

    関数を返すのは、`_pick_by_year` が整数と対応を区別しなくて済むように。
    """
    value = rule.get("count_per_year")
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"ルールの count_per_year は数か、年と枚数の組で書いてください: {value!r}")
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"ルールの count_per_year は 0 以上で書いてください: {value!r}")
        return None if value == 0 else (lambda _year: value)
    if not isinstance(value, dict):
        raise ValueError(f"ルールの count_per_year は数か、年と枚数の組で書いてください: {value!r}")
    limits: Dict[int, int] = {}
    unknown = 0
    later: Optional[Tuple[int, int]] = None
    for key, count in value.items():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(
                f"ルールの count_per_year の {key} の枚数は 0 以上の整数で書いてください: {count!r}"
            )
        if key == COUNT_UNKNOWN:
            unknown = count
        elif isinstance(key, str) and re.fullmatch(r"\d{4}\+", key):
            if later is not None:
                raise ValueError("ルールの count_per_year に「〜年以降」（2027+ など）が2つあります。")
            later = (int(key[:-1]), count)
        elif isinstance(key, bool) or not isinstance(key, int) or not 1000 <= key <= 9999:
            raise ValueError(
                f"ルールの count_per_year の年は 4 桁の数・unknown・2027+ の形で書いてください: {key!r}"
            )
        else:
            limits[key] = count

    def limit(year: Optional[int]) -> int:
        if year is None:
            return unknown
        if year in limits:
            return limits[year]
        if later is not None and year >= later[0]:
            return later[1]
        return 0

    return limit


#: ルールの真偽値のキーと既定値。**既定はここにだけ書く**（`select_media` も引く）。
RULE_FLAGS = {
    "include_video": True,
    "family_only": False,
    "remove_duplicate": False,
    "remove_similar": True,
    "include_auto_assigned": True,
}


def _rule_flag(rule: Dict[str, Any], key: str) -> bool:
    """ルールの真偽値。**読むとき（`load_rule`）に確かめる。**

    **真偽値だけを受け付ける。** 引用符付きの ``"false"`` は文字列で、そのまま
    使うと真として扱われ、黙って逆の結果になる（PR #91 のレビュー指摘1。
    ``family_only: "false"`` で家族だけに絞られていた）。
    """
    value = rule.get(key, RULE_FLAGS[key])
    if not isinstance(value, bool):
        raise ValueError(
            f"ルールの {key} は true か false で書いてください（いまの値: {value!r}）。"
        )
    return value


def include_auto_assigned(rule: Dict[str, Any]) -> bool:
    """自動割り当ての顔も家族として数えるか（#89・既定 true）。"""
    return _rule_flag(rule, "include_auto_assigned")


def family_sources(rule: Dict[str, Any]) -> Tuple[str, ...]:
    """家族の顔として数える割り当ての種別。"""
    if include_auto_assigned(rule):
        return (db.ASSIGN_MANUAL, db.ASSIGN_AUTO)
    return (db.ASSIGN_MANUAL,)


def _parse_date(value: Any) -> Optional[datetime]:
    """規則やメディアの日時を ``datetime`` にする。

    **YAML は引用符の無い `2023-12-31` を `datetime.date` で返す**ので、文字列以外も
    受ける（`datetime` は `date` の子なので先に見る。PR #72 のレビュー指摘3）。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        try:
            return datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return None


def _media_period(media: Dict[str, Any]) -> Optional[Tuple[datetime, datetime]]:
    """その写真の撮影時期を ``(最も早い, 最も遅い)`` で返す。分からなければ ``None``。

    引く順は **EXIF の撮影日時 → フォルダ名から起こした区間（#65）→ ファイル日時**。

    **ファイル日時へ落ちるのは、撮影日時が空のときだけ。** 壊れた値
    （`0000-00-00T00:00:00`）が入っている写真は、フォルダ名から起こせなければ
    「日付が読めない」として扱う（仕様書 §12.1 の `date` の行。ファイル日時は
    コピーで変わるので、壊れた EXIF の代わりにはしない）。
    **読めるかどうかは `dates.taken_at` に預ける**（CLAUDE.md §8）。
    """
    # **結果を覚える**（#104）。GUI の並べ直しのたびに全写真（約7万枚）の日付を読み直していた。
    return _period_of(
        media.get("shooting_date"), media.get("folder_date_from"),
        media.get("folder_date_to"), media.get("created_time"),
    )


@lru_cache(maxsize=200_000)
def _period_of(
    shooting_date: Optional[str],
    folder_date_from: Optional[str],
    folder_date_to: Optional[str],
    created_time: Optional[str],
) -> Optional[Tuple[datetime, datetime]]:
    """`_media_period` の中身。値は4つの文字列だけで決まる。"""
    taken = taken_at(shooting_date, folder_date_from, folder_date_to)
    if taken is not None and not taken.inferred:
        # 時刻まで持っているので、日付の範囲の指定と時刻で比べる。
        moment = _parse_date(shooting_date) or datetime.combine(taken.earliest, time.min)
        return moment, moment
    if taken is not None:
        return (
            datetime.combine(taken.earliest, time.min),
            datetime.combine(taken.latest, time.max),
        )
    if shooting_date or parse_date(created_time) is None:
        return None
    moment = _parse_date(created_time)
    return None if moment is None else (moment, moment)


def _get_media_year(media: Dict[str, Any]) -> Optional[int]:
    """年ごとの件数（``count_per_year``）を数える年。**フォルダ名の区間は1つの年に収まる。**"""
    period = _media_period(media)
    return None if period is None else period[0].year


def _rule_end(value: Any) -> Optional[datetime]:
    """規則の ``date.end``。**日付だけなら、その日の終わりまでを含める。**

    0時として読むと、`end: "2023-12-31"` で 12月31日に終わるフォルダ名の区間
    （`2023/` 直下・`2023/2312/`）も、12月31日の昼に撮った写真も外れる
    （PR #72 のレビュー指摘1）。
    """
    parsed = _parse_date(value)
    if parsed is not None and len(str(value)) == 10:
        parsed = datetime.combine(parsed.date(), time.max)
    return parsed


def _passes_date_filter(media: Dict[str, Any], rule: Dict[str, Any]) -> bool:
    """撮影時期が指定の範囲に入るか。**フォルダ名から起こした区間は、まるごと入るときだけ通す。**"""
    date_rule = rule.get("date") or {}
    if not date_rule:
        return True
    start = _parse_date(date_rule.get("start"))
    end = _rule_end(date_rule.get("end"))
    period = _media_period(media)
    if period is None:
        return False
    earliest, latest = period
    if start and earliest < start:
        return False
    if end and latest > end:
        return False
    return True


def _build_duplicate_groups(media_list: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for media in media_list:
        key = media.get("file_hash") or media.get("path")
        groups.setdefault(key, []).append(media)
    return groups


def stale_assignment_notice(connection) -> Optional[str]:
    """古い規則で付いた自動割り当てが残っていれば、その知らせ。無ければ None。

    **`select` は `match` の判定をそのまま使う。** 規則を変えたあと `match` を
    流し直していないと、古い判定で写真を選ぶことになる（利用者の要望
    「match と select で選定の仕組みが異なると、結果がおかしくなる」）。
    """
    from .matcher import MATCH_RULE

    stale = db.count_stale_auto_assignments(connection, MATCH_RULE)
    if not stale:
        return None
    return (
        f"自動割り当て {stale} 件は、いまの規則（{MATCH_RULE}）より前に付いたものです。"
        " `photoarchive match` を流し直してから select すると、いまの規則で選べます。"
    )


class Candidates:
    """`select` の候補（#103）。**重いもの（DB・測定）をここで一度だけ用意する。**

    `choose` は何度呼んでもよい（GUI で重みを動かすたびに並べ直す）。家族の顔の行・
    写真ごとの他人の重みはここに持ち、見た目の値（`look_hash`）は写真の辞書に持つ
    （一度測ったら同じ候補の中では読み直さない）。
    """

    def __init__(
        self,
        connection,
        media: List[Dict[str, Any]],
        rows: List[Dict[str, Any]],
        assign_sources: Sequence[str],
        root_paths: Sequence[Path],
        progress_callback=None,
        quality: Optional[Dict[int, Dict[str, Any]]] = None,
    ):
        self.connection = connection
        #: 写真全体の良し悪し（`db.photo_quality`・#106）。測っていない写真は入っていない。
        self.quality = quality or {}
        self.media = media
        self.rows = rows
        self.assign_sources = tuple(assign_sources)
        self.root_paths = list(root_paths)
        self.progress_callback = progress_callback
        self._strangers: Dict[Tuple[str, ...], Dict[int, float]] = {}
        #: 見た目を測れなかった写真の ID。**同じ候補の中では読み直さない**（PR #107 の
        #: レビュー指摘2。GUI で重みを動かすたびに NFS を読み直していた）。DB には書かないので、
        #: 次の `prepare`（CLI の次の実行）からは測り直す。
        self.unreadable: set = set()

    def family_rows(self, sources: Sequence[str]) -> List[Dict[str, Any]]:
        """``sources`` の種別の、割り当てのある顔の行（家族と親しい人・#110）。**用意していない種別は求めない。**

        家族だけが要るところ（`family_only`・似た写真）は `_family_of` が親しい人を外す。
        """
        missing = set(sources) - set(self.assign_sources)
        if missing:
            raise ValueError(
                f"候補を用意したときに含めなかった割り当ての種別です: {sorted(missing)}"
            )
        return [row for row in self.rows if row["assign_source"] in sources]

    def stranger_weights(self, sources: Sequence[str]) -> Dict[int, float]:
        """写真ごとの他人(減点)の顔の重み（`strangers.weights`）。種別ごとに一度だけ出す。"""
        key = tuple(sorted(sources))
        if key not in self._strangers:
            self._strangers[key] = strangers.weights(self.connection, self.family_rows(sources))
        return self._strangers[key]


def candidate_counts(candidates: "Candidates", rule: Dict[str, Any]) -> Dict[Optional[int], int]:
    """年ごとの候補の枚数（期間・動画・`family_only` で絞ったあと、束ねる前）。#104 の年ごとの表に出す。"""
    _, family_of = _scores(candidates, family_sources(rule), None)
    counts: Dict[Optional[int], int] = {}
    for media in _filtered(candidates.media, rule, family_of):
        year = _get_media_year(media)
        counts[year] = counts.get(year, 0) + 1
    return counts


def year_counts(connection, rule: Dict[str, Any]) -> Dict[Optional[int], int]:
    """`candidate_counts` と同じ数を、**候補を用意せずに** DB だけから数える（#113）。

    見え方・表情を測らないので速い（実データで約 1 秒）。GUI の年の表を、候補を読み込む前から
    出すためにある（利用者「最初から年毎の選ぶ枚数を入れれるようにしてほしい」）。
    """
    family_of = _family_of(db.family_faces(connection, assign_sources=family_sources(rule)))
    counts: Dict[Optional[int], int] = {}
    for media in _filtered(get_media_with_analysis(connection), rule, family_of):
        year = _get_media_year(media)
        counts[year] = counts.get(year, 0) + 1
    return counts


def explain(candidates: "Candidates", rule: Dict[str, Any], media_id: int) -> Dict[str, float]:
    """写真1枚の点の内訳（`scoring.photo_breakdown`。#104）。`choose` と同じ重み・同じ顔。"""
    sources = family_sources(rule)
    faces = [row for row in candidates.family_rows(sources) if row["media_id"] == media_id]
    weight = candidates.stranger_weights(sources).get(media_id, 0.0)
    return scoring.photo_breakdown(
        faces, weight, rule_weights(rule), candidates.quality.get(media_id)
    )


def _filtered(
    media_list: Iterable[Dict[str, Any]],
    rule: Dict[str, Any],
    family_of: Dict[int, FrozenSet[int]],
) -> List[Dict[str, Any]]:
    """期間・動画・`family_only` で絞る（`choose` と `candidate_counts` で同じ絞り方にする）。"""
    filtered = [m for m in media_list if _passes_date_filter(m, rule)]
    if not _rule_flag(rule, "include_video"):
        filtered = [m for m in filtered if m.get("type") != "video"]
    if _rule_flag(rule, "family_only"):
        filtered = [m for m in filtered if m["id"] in family_of]
    return filtered


def prepare(
    connection,
    assign_sources: Sequence[str] = (db.ASSIGN_MANUAL, db.ASSIGN_AUTO),
    source_roots: Union[str, Sequence[str], None] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> Candidates:
    """候補を用意する（#103）。見え方と表情の未計測分は、ここで測る。

    ``assign_sources`` の種別の顔だけを家族として読み、測るのもそれだけ（#89。
    `include_auto_assigned: false` のルールでは自動割り当ての顔を測らない）。
    """
    if isinstance(source_roots, (str, Path)):
        source_roots = [source_roots]
    root_paths = [Path(root).resolve() for root in source_roots or []]
    appearance.fill_missing(
        connection, assign_sources=tuple(assign_sources), progress_callback=progress_callback
    )
    expression.fill_missing(
        connection, assign_sources=tuple(assign_sources), progress_callback=progress_callback
    )
    rows = db.family_faces(connection, assign_sources=assign_sources)
    return Candidates(
        connection, get_media_with_analysis(connection), rows, assign_sources, root_paths,
        progress_callback, db.photo_quality(connection, QUALITY_VERSION),
    )


def _family_of(rows: Iterable[Dict[str, Any]]) -> Dict[int, FrozenSet[int]]:
    """写真ごとの写っている家族。**親しい人は入れない**（`family_only`・似た写真の「同じ家族」・#110）。"""
    people: Dict[int, set] = {}
    for row in rows:
        if not db.is_family(row):
            continue
        people.setdefault(row["media_id"], set()).add(row["person_id"])
    return {media_id: frozenset(found) for media_id, found in people.items()}


def family_scores(
    connection,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    assign_sources: Sequence[str] = (db.ASSIGN_MANUAL, db.ASSIGN_AUTO),
    weights: Optional["scoring.Weights"] = None,
) -> Tuple[Dict[int, float], Dict[int, FrozenSet[int]]]:
    """写真ごとの家族写真としての良さと、写真ごとに写っている家族（人物 ID の集合）。

    家族の写っていない写真は2つ目の辞書に入らない（`family_only` はこれで絞る）。

    **いまの割り当てからその場で**計算する。

    **「写っているか」と「どれだけ良いか」を同じ数で表さない**（PR #70 の
    レビュー指摘1）。点は鮮明さ・正面・笑顔がすべて 0 なら 0 になるので、
    「点 > 0」で絞ると、**家族が写っているのにボケて横を向いた写真が落ちる**
    （実データの複製で 20,828 枚中 433 枚）。`family_only` は集合で絞り、
    点は並びにだけ使う。

    **保存済みの `family_score` を読まない。** GUI で割り当てを直しても
    `AnalysisResult` は次の `match` まで古いまま（仕様書 §10.6）なので、それを
    読むと人が直した結果が `select` に届かない。式は `scoring.family_photo_score`
    （`match` が書く `family_score` と同じもの）。

    見え方と表情（目閉じ・笑顔・#95）が未計測の家族の顔は、先に測る（初回は数分。
    2回目からは差分だけ）。
    ``assign_sources`` の種別の顔だけを家族として数え、測るのもそれだけ（#89）。
    """
    candidates = prepare(connection, assign_sources, progress_callback=progress_callback)
    return _scores(candidates, assign_sources, weights)


def _scores(
    candidates: Candidates, sources: Sequence[str], weights: Optional["scoring.Weights"]
) -> Tuple[Dict[int, float], Dict[int, FrozenSet[int]]]:
    rows = candidates.family_rows(sources)
    return (
        scoring.family_photo_scores(
            rows, candidates.stranger_weights(sources), weights, candidates.quality
        ),
        _family_of(rows),
    )


def _source_path(path_value: str, root_paths: Sequence[Path]) -> Path:
    """メディアの元のファイル。相対パスで登録されたものは先頭の root から解く。"""
    source_path = Path(path_value)
    if not source_path.is_absolute() and root_paths:
        source_path = root_paths[0] / source_path
    return source_path.resolve()


def _rule_number(
    rule: Dict[str, Any], key: str, default: float, label: Optional[str] = None
) -> float:
    """ルールの数の値。**読むとき（`load_rule`）に確かめる**ので、写真を測り始めてから止まらない。"""
    value = rule.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)  # `.nan` は `< 0` で弾けず、並びが不定になる（PR #107 指摘4）
        or value < 0
    ):
        raise ValueError(f"ルールの {label or key} は 0 以上の数で書いてください: {value!r}")
    return value


def _remove_similar(
    connection,
    ranked: List[Dict[str, Any]],
    family_of: Dict[int, FrozenSet[int]],
    rule: Dict[str, Any],
    root_paths: Sequence[Path],
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    unreadable: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """同じ行事の中の連写・似た写真を、場面ごとに並びの先頭1枚へまとめる（#86・#93・`similar`）。

    ``ranked`` は並べ終えた一覧。**見た目は候補の写真だけを測り**、`Media.look_hash` に
    残す（2回目からは読まない）。測れなかった写真は束ねずに残す。
    """
    seconds = _rule_number(rule, "similar_seconds", similar.DEFAULT_SECONDS)
    max_distance = _rule_number(rule, "similar_distance", similar.DEFAULT_DISTANCE)
    far_distance = _rule_number(rule, "similar_far_distance", similar.FAR_DISTANCE)
    groups = similar.candidate_groups(ranked, seconds)
    looks: Dict[int, Optional[str]] = {}
    unmeasured: List[Dict[str, Any]] = []
    unreadable = set() if unreadable is None else unreadable
    for group in groups:
        for media in group:
            if media["id"] in unreadable:
                continue
            if similar.decode(media.get("look_hash")) is None:
                unmeasured.append(media)
            else:
                looks[media["id"]] = media["look_hash"]
    measured: List[Tuple[int, str]] = []
    for done, media in enumerate(unmeasured, start=1):
        value = similar.measure(str(_source_path(media["path"], root_paths)))
        if value is not None:
            looks[media["id"]] = value
            # 年ごとに選ぶところ（`_pick_by_year`）でも同じ値を使う。測り直さない
            media["look_hash"] = value
            measured.append((media["id"], value))
        else:
            unreadable.add(media["id"])
        if len(measured) >= 500:
            db.save_look_hashes(connection, measured)
            measured = []
        if progress_callback is not None:
            progress_callback(done, len(unmeasured), os.path.basename(media["path"]))
    db.save_look_hashes(connection, measured)

    rank = {media["id"]: index for index, media in enumerate(ranked)}
    dropped = set()
    for group in groups:
        ordered = sorted(group, key=lambda media: rank[media["id"]])
        for scene in similar.scenes(
            ordered, looks, int(max_distance), seconds, family_of=family_of,
            far_distance=int(far_distance),
        ):
            # 先頭が代表（並びのいちばん上）。残りを外す
            dropped.update(media["id"] for media in scene[1:])
    return [media for media in ranked if media["id"] not in dropped]


class _Looks:
    """`_pick_by_year` が使う見た目の値。**要るときに測り、`Media.look_hash` に残す。**"""

    def __init__(
        self, connection, root_paths: Sequence[Path], progress_callback=None,
        unreadable: Optional[set] = None,
    ):
        self.connection = connection
        #: 測れなかった写真の ID（`Candidates.unreadable` と共有する）。
        self.unreadable = set() if unreadable is None else unreadable
        self.root_paths = root_paths
        self.progress_callback = progress_callback
        self.pending: List[Tuple[int, str]] = []
        #: 元写真を読んだ回数（進み具合に出す）。
        self.reads = 0

    def _store(self, media: Dict[str, Any], value: str) -> None:
        media["look_hash"] = value
        self.pending.append((media["id"], value))
        if len(self.pending) >= 500:
            self.flush()

    def _path(self, media: Dict[str, Any]) -> str:
        """読む元写真のパス。読むたびに進み具合を知らせる。

        **総数は分からない**（年の枠が埋まるまで、何枚読むかは見てみないと決まらない）ので、
        件数だけを知らせる（``total=None``）。以前は ``(0, 0)`` を渡していて、表示が
        毎回「100% (0/0)」で終わった段とみなし、1枚ごとに行を描き直していた（PR #99）。
        """
        self.reads += 1
        if self.progress_callback is not None:
            self.progress_callback(self.reads, None, os.path.basename(media["path"]))
        return str(_source_path(media["path"], self.root_paths))

    def of(self, media: Dict[str, Any]) -> Optional[str]:
        """主の値（`similar.measure`）。測れなければ None（保存しない）。"""
        if similar.decode(media.get("look_hash")) is None:
            if media["id"] in self.unreadable:
                return None
            value = similar.measure(self._path(media))
            if value is None:
                self.unreadable.add(media["id"])
                return None
            self._store(media, value)
        return media["look_hash"]

    def with_full(self, media: Dict[str, Any]) -> Optional[str]:
        """元写真から測った値も持たせた値。**取った場所が違う2枚を比べるときだけ**呼ぶ。"""
        current = self.of(media)
        if current is None or similar.FROM_IMAGE in similar.decode_all(current):
            return current
        value = similar.measure_full(self._path(media))
        if value is None:
            return current
        self._store(media, similar.with_full(current, value))
        return media["look_hash"]

    def flush(self) -> None:
        db.save_look_hashes(self.connection, self.pending)
        self.pending = []


def _is_copy(
    media: Dict[str, Any],
    kept: Sequence[Dict[str, Any]],
    family_of: Dict[int, FrozenSet[int]],
    looks: _Looks,
    copy_distance: int = similar.COPY_DISTANCE,
) -> bool:
    """すでに選んだ写真のどれかと、見た目がほぼ同じか（既定 `similar.COPY_DISTANCE`・#98）。"""
    if media.get("type") != "image":
        return False
    for other in kept:
        if other.get("type") != "image" or not similar.shares_family(family_of, other, media):
            continue
        if similar.on_different_days(other, media):
            continue
        mine, theirs = looks.of(media), looks.of(other)
        if mine is None:
            return False
        if theirs is None:
            continue
        gap = similar.distance(theirs, mine)
        if gap is None:
            # **取った場所が違う**（EXIF のサムネイルと元写真）。保存し直した写しは
            # EXIF のサムネイルを持たないので、両方を元写真から測って比べる
            gap = similar.distance(looks.with_full(other), looks.with_full(media))
        if gap is not None and gap <= copy_distance:
            return True
    return False


def _pick_by_year(
    connection,
    ranked: List[Dict[str, Any]],
    family_of: Dict[int, FrozenSet[int]],
    limit_of: Optional[Callable[[Optional[int]], Optional[int]]],
    remove_copies: bool,
    root_paths: Sequence[Path],
    progress_callback: Optional[Callable[[int, Optional[int], str], None]] = None,
    copy_distance: int = similar.COPY_DISTANCE,
    unreadable: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """並びの順に、年ごとに ``limit_of(年)`` 枚まで選ぶ（`year_limits`）。

    ``remove_copies`` なら、**同じ年にすでに選んだ写真と見た目がほぼ同じ写真を外す**
    （#98。フォルダ・撮影日時を問わない）。別の端末のフォルダにある写しや、送られてきて
    保存し直した写真（EXIF の日時が無い）は、同じ行事の候補（`similar.candidate_groups`）に
    入らないので、ここで拾う。**見た目は選ぶかどうかを決める写真だけを測る**
    （年の枠が埋まれば、その年の残りは測らない）。

    年ごとの枚数があれば年ごとにまとめて返し（年は並びに初めて出た順）、
    無ければ並びの順のまま返す。選ばない年（枚数 0）の写真は、`choose` がここへ来る前に外す。
    """
    looks = _Looks(connection, root_paths, progress_callback, unreadable)
    by_year: Dict[Optional[int], List[Dict[str, Any]]] = {}
    in_order: List[Dict[str, Any]] = []
    for media in ranked:
        year = _get_media_year(media)
        kept = by_year.setdefault(year, [])
        limit = None if limit_of is None else limit_of(year)
        if limit is not None and len(kept) >= limit:
            continue
        if remove_copies and _is_copy(media, kept, family_of, looks, copy_distance):
            continue
        kept.append(media)
        in_order.append(media)
    looks.flush()
    if limit_of is not None:
        return [media for entries in by_year.values() for media in entries]
    return in_order


def select_media(
    connection,
    rule: Dict[str, Any],
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    source_roots: Union[str, Sequence[str], None] = None,
) -> List[Dict[str, Any]]:
    """ルールに従って写真を選び、`select` の並びで返す（`choose(prepare(...))`）。

    ``source_roots`` は相対パスで登録されたメディアを解くのに使う（見た目を測るとき）。
    """
    candidates = prepare(connection, family_sources(rule), source_roots, progress_callback)
    return choose(candidates, rule)


def choose(candidates: Candidates, rule: Dict[str, Any]) -> List[Dict[str, Any]]:
    """用意した候補から、ルールに従って選ぶ（#103）。**何度呼んでもよい。**

    重みで点を出し、並べ、`remove_duplicate`・似た写真・写しで束ね、年ごとに取る。
    **DB に書くのは見た目の値（`look_hash`）を初めて測ったときだけ**で、同じ候補の中で
    二度は測らない（GUI で重みを動かすたびに呼ぶため）。
    """
    scores, family_of = _scores(candidates, family_sources(rule), rule_weights(rule))
    for media in candidates.media:
        media["family_score"] = scores.get(media["id"], 0.0)
    filtered = _filtered(candidates.media, rule, family_of)
    # **選ばない年の写真は、束ねる前に外す**（PR #107 のレビュー指摘1）。残すと、書いていない
    # 年の写真が似た写真の代表になって書いた年の写真を外し（年越しの連写）、見た目も測っていた。
    limit_of = year_limits(rule)
    if limit_of is not None:
        filtered = [m for m in filtered if limit_of(_get_media_year(m)) != 0]

    # **家族の写った写真が先。** 他人(減点)の顔の重みを引くと点が負になりうる（#100・#110）ので、
    # 点だけで並べると、家族の写った写真が家族のいない写真（点 0）より後ろに来る
    # （`family_only: false` のとき。PR #101 のレビュー指摘1・利用者の決定）。
    filtered.sort(key=lambda m: (
        m["id"] not in family_of,
        -(m.get("family_score") or 0.0),
        -(m.get("quality_score") or 0.0),
        -(m.get("smile_score") or 0.0),
        -(m.get("face_count") or 0),
    ))

    if _rule_flag(rule, "remove_duplicate"):
        groups = _build_duplicate_groups(filtered)
        filtered = [sorted(group, key=lambda m: (
            m["id"] not in family_of,
            -(m.get("family_score") or 0.0),
            -(m.get("quality_score") or 0.0),
        ))[0] for group in groups.values()]

    if _rule_flag(rule, "remove_similar"):
        filtered = _remove_similar(
            candidates.connection, filtered, family_of, rule, candidates.root_paths,
            candidates.progress_callback, candidates.unreadable,
        )

    picked = _pick_by_year(
        candidates.connection,
        filtered,
        family_of,
        limit_of,
        _rule_flag(rule, "remove_similar"),
        candidates.root_paths,
        candidates.progress_callback,
        int(_rule_number(rule, "copy_distance", similar.COPY_DISTANCE)),
        candidates.unreadable,
    )
    # **浅く写して返す**（PR #107 のレビュー指摘3）。候補の辞書は次の `choose` が点を書き換える
    # ので、そのまま返すと前に返した結果の点まで変わる。見た目の値（`look_hash`）は候補の辞書に
    # 残したいので、写すのは返す直前だけ。
    return [dict(media) for media in picked]


def _output_name(rank: int, width: int, media: Dict[str, Any], source_path: Path) -> str:
    """出力の名前。``<順位>_<年>_m<Media.id><元の拡張子>``（#83）。

    名前を見れば `select` の並びと年が分かり、名前順に並べると `select` の順になる。
    **元のファイル名は引き継がない。** ID が入るので、別の root にある同じ名前の
    写真もぶつからない。
    """
    year = _get_media_year(media)
    return f"{rank:0{width}d}_{year if year is not None else 'unknown'}_m{media.get('id')}{source_path.suffix}"


def _remove_previous_output(output_root: Path) -> None:
    """前回の出力（出力先の**直下にあるファイル**）を消す。

    出力は平らなので、残すと前回の結果と区別が付かない。**出力先は `select`
    専用**で、このツールが置いたかどうかは見ない（利用者が決めた・PR #85）。
    シンボリックリンクも消す（リンクで出力していた頃の残り）。**サブフォルダには
    触らない。**
    """
    for entry in output_root.iterdir():
        if entry.is_symlink() or entry.is_file():
            entry.unlink()


def _check_output_is_not_an_input(
    output_root: Path, root_paths: Sequence[Path], sources: Sequence[Path]
) -> None:
    """出力先が root やその中なら止める。**直下のファイルを消すので、元写真が消える。**"""
    output = output_root.resolve()
    for root in root_paths:
        if output == root or root in output.parents:
            raise ValueError(
                f"出力先が写真の root の中にあります。直下のファイルを消すので使えません: {output}"
            )
    for source in sources:
        if source.parent == output:
            raise ValueError(f"出力先に元の写真があります。直下のファイルを消すので使えません: {source}")


def copy_selected_media(
    selected_media: List[Dict[str, Any]],
    output_dir: str,
    source_roots: Union[str, Sequence[str]],
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> int:
    """選んだメディアを、出力先の**直下**へ平らにコピーする（#83）。

    名前は `_output_name`。コピーする前に、前回の出力（直下のファイル）を消す
    （`_remove_previous_output`）。順位は渡された並びの 1 からで、``path`` の無い
    項目は飛ばすが**順位は詰めない**（番号が `select` の並びと一致する）。
    相対パスで登録されたメディアは先頭の root から解く。

    **消す前に、止まる理由を全部調べる**（PR #85 のレビュー指摘1）。途中で止まると、
    前回の結果も今回の結果も揃っていない出力が残る。

    - 出力先が root やその中なら止める（`_check_output_is_not_an_input`）
    - 元のファイルが無ければ止める
    - 同じ名前のサブフォルダがあれば止める（ファイルは前回の出力として消す）
    """
    if isinstance(source_roots, (str, Path)):
        source_roots = [source_roots]
    root_paths = [Path(root).resolve() for root in source_roots]
    output_root = Path(output_dir)
    total = len(selected_media)
    width = max(4, len(str(total)))
    entries: List[Tuple[int, Path, Path]] = []
    for rank, media in enumerate(selected_media, start=1):
        path_value = media.get("path")
        if path_value is None:
            continue
        source_path = _source_path(path_value, root_paths)
        entries.append((rank, output_root / _output_name(rank, width, media, source_path), source_path))

    _check_output_is_not_an_input(output_root, root_paths, [source for _, _, source in entries])
    missing = [str(source) for _, _, source in entries if not source.is_file()]
    if missing:
        more = f" ほか {len(missing) - 1} 件" if len(missing) > 1 else ""
        raise FileNotFoundError(f"元のファイルがありません（何も消していません）: {missing[0]}{more}")
    if output_root.exists():
        clashes = [
            target.name for _, target, _ in entries if target.is_dir() and not target.is_symlink()
        ]
        if clashes:
            more = f" ほか {len(clashes) - 1} 件" if len(clashes) > 1 else ""
            raise FileExistsError(
                f"出力先に同じ名前のフォルダがあります（何も消していません）: {clashes[0]}{more}"
            )

    output_root.mkdir(parents=True, exist_ok=True)
    _remove_previous_output(output_root)
    for rank, target, source_path in entries:
        shutil.copy2(source_path, target)
        if progress_callback is not None:
            progress_callback(rank, total, target.name)
    return len(entries)
