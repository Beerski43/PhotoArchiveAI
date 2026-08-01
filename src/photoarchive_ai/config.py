import json
from pathlib import Path
from typing import Any, Dict, Optional


DEFAULT_CONFIG_PATH = Path("config/app_settings.json")


def load_settings() -> Dict[str, Any]:
    if not DEFAULT_CONFIG_PATH.exists():
        return {}
    with DEFAULT_CONFIG_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def get_database_path(settings: Dict[str, Any]) -> Optional[str]:
    return settings.get("database_path")


def get_source_root(settings: Dict[str, Any]) -> Optional[str]:
    return settings.get("source_root")


def get_output_root(settings: Dict[str, Any]) -> Optional[str]:
    return settings.get("output_root")


def get_rule_path(settings: Dict[str, Any]) -> Optional[str]:
    return settings.get("rule_path")
