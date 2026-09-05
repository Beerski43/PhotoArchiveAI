import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from .db import get_media_with_analysis


def load_rule(rule_path: str) -> Dict[str, Any]:
    path = Path(rule_path)
    if not path.exists():
        raise FileNotFoundError(f"Rule file not found: {rule_path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        return yaml.safe_load(text)
    return json.loads(text)


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        try:
            return datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return None


def _get_media_year(media: Dict[str, Any]) -> Optional[int]:
    date_value = media.get("shooting_date") or media.get("created_time")
    if not date_value:
        return None
    dt = _parse_date(date_value)
    return dt.year if dt else None


def _passes_date_filter(media: Dict[str, Any], rule: Dict[str, Any]) -> bool:
    date_rule = rule.get("date") or {}
    if not date_rule:
        return True
    start = _parse_date(date_rule.get("start"))
    end = _parse_date(date_rule.get("end"))
    media_date = media.get("shooting_date") or media.get("created_time")
    if not media_date:
        return False
    media_dt = _parse_date(media_date)
    if media_dt is None:
        return False
    if start and media_dt < start:
        return False
    if end and media_dt > end:
        return False
    return True


def _build_duplicate_groups(media_list: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for media in media_list:
        key = media.get("file_hash") or media.get("path")
        groups.setdefault(key, []).append(media)
    return groups


def select_media(connection, rule: Dict[str, Any]) -> List[Dict[str, Any]]:
    media_list = get_media_with_analysis(connection)
    filtered = [m for m in media_list if _passes_date_filter(m, rule)]
    if not rule.get("include_video", True):
        filtered = [m for m in filtered if m.get("type") != "video"]
    if rule.get("family_only"):
        filtered = [m for m in filtered if (m.get("family_score") or 0.0) > 0.0]

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


def copy_selected_media(
    selected_media: List[Dict[str, Any]],
    output_dir: str,
    source_root: str,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> int:
    source_root_path = Path(source_root).resolve()
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    copied = 0
    total = len(selected_media)
    for index, media in enumerate(selected_media, start=1):
        path_value = media.get("path")
        if path_value is None:
            continue
        source_path = Path(path_value)
        if not source_path.is_absolute():
            source_path = source_root_path / source_path
        source_path = source_path.resolve()

        if source_root_path in source_path.parents or source_path == source_root_path:
            try:
                relative = source_path.relative_to(source_root_path)
            except ValueError:
                relative = source_path.name
        else:
            relative = source_path.name
        destination = output_root.joinpath(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            base = destination.stem
            suffix = destination.suffix
            destination = destination.with_name(f"{base}_{copied}{suffix}")
        shutil.copy2(source_path, destination)
        copied += 1
        if progress_callback is not None:
            progress_callback(index, total, relative.as_posix())
    return copied
