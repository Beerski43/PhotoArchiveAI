"""アプリケーション設定の読み込み。

設定ファイルは JSON。次の順で探し、最初に見つかったものを使う。

1. 環境変数 ``PHOTOARCHIVE_CONFIG`` が指すファイル
2. カレントディレクトリの ``config/app_settings.json``
3. リポジトリ(インストール元)直下の ``config/app_settings.json``

3 があるのは、リポジトリルート以外から ``photoarchive`` を起動しても
設定が効くようにするため。以前は 2 だけを見ていたので、別の
ディレクトリから実行すると**例外にもならず空の設定が返っていた**。
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

CONFIG_ENV_VAR = "PHOTOARCHIVE_CONFIG"
CONFIG_RELATIVE_PATH = Path("config/app_settings.json")
REPO_ROOT = Path(__file__).resolve().parents[2]

# 後方互換。探索の起点としてではなく、既定の置き場所を示すために残す。
DEFAULT_CONFIG_PATH = CONFIG_RELATIVE_PATH


def candidate_config_paths() -> Iterator[Path]:
    """設定ファイルの探索先を、優先順に返す。"""
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        yield Path(override).expanduser()
    yield Path.cwd() / CONFIG_RELATIVE_PATH
    yield REPO_ROOT / CONFIG_RELATIVE_PATH


def find_settings_path() -> Optional[Path]:
    """実際に読む設定ファイル。見つからなければ None。"""
    seen = set()
    for candidate in candidate_config_paths():
        resolved = candidate.expanduser()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        if resolved.is_file():
            return resolved
    return None


def load_settings() -> Dict[str, Any]:
    """設定を読む。見つからない・壊れている場合は空の辞書を返す。

    設定が無くても CLI の引数だけで動かせるようにするため、
    ここでは例外にしない。どこを見たかはログに残す。
    """
    path = find_settings_path()
    if path is None:
        logger.info(
            "設定ファイルが見つからない。探した場所: %s",
            ", ".join(str(candidate) for candidate in candidate_config_paths()),
        )
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            settings = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("設定ファイルを読めない (%s): %s", path, error)
        return {}
    if not isinstance(settings, dict):
        logger.warning("設定ファイルの中身が辞書ではない (%s)", path)
        return {}
    logger.info("設定ファイルを読んだ: %s", path)
    return settings


def get_database_path(settings: Dict[str, Any]) -> Optional[str]:
    return settings.get("database_path")


def get_source_root(settings: Dict[str, Any]) -> Optional[str]:
    return settings.get("source_root")


def get_output_root(settings: Dict[str, Any]) -> Optional[str]:
    return settings.get("output_root")


def get_rule_path(settings: Dict[str, Any]) -> Optional[str]:
    return settings.get("rule_path")


def get_dlib_model_dir(settings: Dict[str, Any]) -> Optional[str]:
    """dlib の学習済みモデルを置いたディレクトリ(任意)。"""
    return settings.get("dlib_model_dir")
