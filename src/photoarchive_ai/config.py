"""アプリケーション設定の読み込み。

設定ファイルは JSON。次の順で探し、最初に見つかったものを使う。

1. 環境変数 ``PHOTOARCHIVE_CONFIG`` が指すファイル
2. カレントディレクトリの ``config/app_settings.json``
3. リポジトリ直下の ``config/app_settings.json``

3 があるのは、リポジトリルート以外から ``photoarchive`` を起動しても
設定が効くようにするため。以前は 2 だけを見ていたので、別の
ディレクトリから実行すると**例外にもならず空の設定が返っていた**。

3 が効くのは ``pip install -e .`` (editable install) のときだけ。
通常のインストールでは ``site-packages`` の下に置かれるので、
リポジトリ直下にあたるものが無く、この候補は使わない。
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

CONFIG_ENV_VAR = "PHOTOARCHIVE_CONFIG"
CONFIG_RELATIVE_PATH = Path("config/app_settings.json")
MODULE_PATH = Path(__file__).resolve()
# editable install なら src/photoarchive_ai/config.py からリポジトリ直下に戻る。
REPO_ROOT = MODULE_PATH.parents[2]
# 通常のインストールでモジュールが置かれるディレクトリの名前。
INSTALL_MARKERS = {"site-packages", "dist-packages"}

# 後方互換。探索の起点としてではなく、既定の置き場所を示すために残す。
DEFAULT_CONFIG_PATH = CONFIG_RELATIVE_PATH


def _is_installed_copy() -> bool:
    """このモジュールが site-packages などの下に置かれているか。

    通常のインストールでは ``REPO_ROOT`` が ``lib/python3.x`` を指すので、
    ``REPO_ROOT`` 自身を見ても判別できない。**モジュールの位置で判断する。**
    editable install でなければリポジトリ直下という概念が無く、探索先に
    混ぜても空振りするだけで、「探した場所」のログが誤解を招く。
    """
    return any(part in INSTALL_MARKERS for part in MODULE_PATH.parts)


def candidate_config_paths() -> Iterator[Path]:
    """設定ファイルの探索先を、優先順に返す。同じ場所は1度だけ。

    リポジトリ直下で実行すると 2 と 3 が同じ場所になる。重複したまま
    返すと「探した場所」のログに同じパスが2度並んで紛らわしい。
    """
    candidates = []
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(Path.cwd() / CONFIG_RELATIVE_PATH)
    if not _is_installed_copy():
        candidates.append(REPO_ROOT / CONFIG_RELATIVE_PATH)

    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        yield candidate


def find_settings_path() -> Optional[Path]:
    """実際に読む設定ファイル。見つからなければ None。"""
    for candidate in candidate_config_paths():
        if candidate.is_file():
            return candidate
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
