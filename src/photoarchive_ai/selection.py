import os
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

import yaml

from . import appearance, db, scoring
from .dates import parse_date, taken_at
from .db import get_media_with_analysis


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
    return rule


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
    taken = taken_at(
        media.get("shooting_date"), media.get("folder_date_from"), media.get("folder_date_to")
    )
    if taken is not None and not taken.inferred:
        # 時刻まで持っているので、日付の範囲の指定と時刻で比べる。
        moment = _parse_date(media.get("shooting_date")) or datetime.combine(
            taken.earliest, time.min
        )
        return moment, moment
    if taken is not None:
        return (
            datetime.combine(taken.earliest, time.min),
            datetime.combine(taken.latest, time.max),
        )
    if media.get("shooting_date") or parse_date(media.get("created_time")) is None:
        return None
    moment = _parse_date(media.get("created_time"))
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


def family_scores(
    connection, progress_callback: Optional[Callable[[int, int, str], None]] = None
) -> Tuple[Dict[int, float], Set[int]]:
    """写真ごとの家族写真としての良さと、家族の顔が写っている写真の集合。

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

    見え方が未計測の家族の顔は、先に測る（初回は数分。2回目からは差分だけ）。
    """
    appearance.fill_missing(connection, progress_callback=progress_callback)
    rows = db.family_faces(connection)
    return scoring.family_photo_scores(rows), {row["media_id"] for row in rows}


def select_media(
    connection,
    rule: Dict[str, Any],
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> List[Dict[str, Any]]:
    media_list = get_media_with_analysis(connection)
    scores, with_family = family_scores(connection, progress_callback)
    for media in media_list:
        media["family_score"] = scores.get(media["id"], 0.0)
    filtered = [m for m in media_list if _passes_date_filter(m, rule)]
    if not rule.get("include_video", True):
        filtered = [m for m in filtered if m.get("type") != "video"]
    if rule.get("family_only"):
        filtered = [m for m in filtered if m["id"] in with_family]

    filtered.sort(key=lambda m: (
        -(m.get("family_score") or 0.0),
        -(m.get("quality_score") or 0.0),
        -(m.get("smile_score") or 0.0),
        -(m.get("face_count") or 0),
    ))

    if rule.get("remove_duplicate"):
        groups = _build_duplicate_groups(filtered)
        filtered = [sorted(group, key=lambda m: (
            -(m.get("family_score") or 0.0),
            -(m.get("quality_score") or 0.0),
        ))[0] for group in groups.values()]

    count_per_year = rule.get("count_per_year")
    if count_per_year:
        selected: List[Dict[str, Any]] = []
        by_year: Dict[Optional[int], List[Dict[str, Any]]] = {}
        for media in filtered:
            year = _get_media_year(media)
            by_year.setdefault(year, []).append(media)
        for year, entries in by_year.items():
            selected.extend(entries[:count_per_year])
        return selected

    return filtered


def _link_name(rank: int, width: int, media: Dict[str, Any], source_path: Path) -> str:
    """リンクの名前。``<順位>_<年>_m<Media.id><元の拡張子>``（#83）。

    名前を見れば `select` の並びと年が分かり、名前順に並べると `select` の順になる。
    **元のファイル名は引き継がない。** ID が入るので、別の root にある同じ名前の
    写真もぶつからない。
    """
    year = _get_media_year(media)
    return f"{rank:0{width}d}_{year if year is not None else 'unknown'}_m{media.get('id')}{source_path.suffix}"


def _remove_previous_links(output_root: Path) -> None:
    """前回の出力（出力先の**直下にあるシンボリックリンク**）を消す。

    出力は平らなので、残すと前回の結果と区別が付かない。**通常のファイルと
    サブフォルダには触らない**（利用者が置いたものや、コピーで出力していた
    頃の写真を消さない）。リンク先が消えた壊れたリンクも消す。
    """
    for entry in output_root.iterdir():
        if entry.is_symlink():
            entry.unlink()


def link_selected_media(
    selected_media: List[Dict[str, Any]],
    output_dir: str,
    source_roots: Union[str, Sequence[str]],
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> int:
    """選んだメディアへのシンボリックリンクを、出力先の**直下**に張る（#83）。

    名前は `_link_name`、リンク先は元ファイルの絶対パス。**元ファイルは読まない・
    変えない。** 張る前に前回のリンクを消す（`_remove_previous_links`）。
    順位は渡された並びの 1 からで、``path`` の無い項目は飛ばすが**順位は詰めない**
    （番号が `select` の並びと一致する）。相対パスで登録されたメディアは先頭の
    root から解く。同じ名前の実体のファイルがあれば、上書きせずに
    ``FileExistsError`` で止まる。
    """
    if isinstance(source_roots, (str, Path)):
        source_roots = [source_roots]
    root_paths = [Path(root).resolve() for root in source_roots]
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    _remove_previous_links(output_root)
    linked = 0
    total = len(selected_media)
    width = max(4, len(str(total)))
    for rank, media in enumerate(selected_media, start=1):
        path_value = media.get("path")
        if path_value is None:
            continue
        source_path = Path(path_value)
        if not source_path.is_absolute() and root_paths:
            source_path = root_paths[0] / source_path
        source_path = source_path.resolve()

        name = _link_name(rank, width, media, source_path)
        (output_root / name).symlink_to(source_path)
        linked += 1
        if progress_callback is not None:
            progress_callback(rank, total, name)
    return linked
