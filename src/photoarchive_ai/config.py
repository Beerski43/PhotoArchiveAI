"""アプリケーション設定の読み込み。

設定ファイルは YAML（#27）。次の順で探し、最初に見つかったものを使う。

1. 環境変数 ``PHOTOARCHIVE_CONFIG`` が指すファイル
2. カレントディレクトリの ``config/app_settings.yml``
3. リポジトリ直下の ``config/app_settings.yml``

3 があるのは、リポジトリルート以外から ``photoarchive`` を起動しても
設定が効くようにするため。以前は 2 だけを見ていたので、別の
ディレクトリから実行すると**例外にもならず空の設定が返っていた**。

3 が効くのは ``pip install -e .`` (editable install) のときだけ。
通常のインストールでは ``site-packages`` の下に置かれるので、
リポジトリ直下にあたるものが無く、この候補は使わない。

**古い ``config/app_settings.json`` は読まない。** 見つけたら YAML へ変換するよう
WARNING を出す。黙って空の設定を返すと、``scan`` が「source root が要る」とだけ
言って止まり、原因（設定ファイルの形式が変わったこと）に辿り着けないため。
"""

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import yaml

logger = logging.getLogger(__name__)

CONFIG_ENV_VAR = "PHOTOARCHIVE_CONFIG"
CONFIG_RELATIVE_PATH = Path("config/app_settings.yml")
#: #27 より前の設定ファイル。**読まない。** 見つけたら変換を促すためだけに持つ。
LEGACY_CONFIG_RELATIVE_PATH = Path("config/app_settings.json")
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


def find_legacy_settings_path() -> Optional[Path]:
    """YAML が無い場所に残っている古い ``app_settings.json``。無ければ None。

    探す場所は YAML と同じ（カレント → リポジトリ直下）。環境変数で ``.json`` を
    指している場合もここで拾う（指された先は YAML としては読まない）。
    """
    override = os.environ.get(CONFIG_ENV_VAR)
    if override and Path(override).suffix.lower() == ".json" and Path(override).expanduser().is_file():
        return Path(override).expanduser()
    for candidate in candidate_config_paths():
        legacy = candidate.parent.parent / LEGACY_CONFIG_RELATIVE_PATH
        if candidate.name == CONFIG_RELATIVE_PATH.name and legacy.is_file():
            return legacy
    return None


#: 「拡張子を変えるだけ」が成り立つ条件。**YAML はタブを字下げに使えない**
#: （PR #74 のレビュー指摘2）。
TAB_NOTE = "空白で字下げしていれば拡張子を変えるだけでも動きます。タブで字下げしていると読めません"


def legacy_settings_message(path: Path) -> str:
    """古い JSON の設定を見つけたときの案内。CLI と GUI で同じ文を出す。"""
    return (
        f"古い形式の設定ファイル {path} は読みません（#27 で YAML に変わりました）。"
        f" 同じ中身を {path.with_suffix('.yml')} に YAML で書いてください"
        f"（JSON の中身は YAML として読めるので、{TAB_NOTE}）。"
    )


def legacy_settings_stop_message(path: Path) -> str:
    """古い JSON のせいで止めるときの一文。**案内の本文は繰り返さない。**

    ``load_settings`` が同じ案内を WARNING で先に出している。止めるほうでも全文を
    出すと、端末に同じ長い文が2回並ぶ（PR #74 のレビュー指摘3）。
    """
    return f"設定ファイルが YAML になっていないため止めました（{path}。案内は上に出しています）。"


def _read_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_settings() -> Dict[str, Any]:
    """設定を読む。見つからない・壊れている場合は空の辞書を返す。

    設定が無くても CLI の引数だけで動かせるようにするため、
    ここでは例外にしない。どこを見たかはログに残す。
    """
    path = find_settings_path()
    if path is not None and path.suffix.lower() == ".json":
        # 環境変数が古い JSON を指している。YAML として読めてしまうが、読まない。
        path = None
    if path is None:
        legacy = find_legacy_settings_path()
        if legacy is not None:
            logger.warning(legacy_settings_message(legacy))
            return {}
        logger.info(
            "設定ファイルが見つからない。探した場所: %s",
            ", ".join(str(candidate) for candidate in candidate_config_paths()),
        )
        return {}
    try:
        settings = _read_yaml(path)
    except (OSError, yaml.YAMLError) as error:
        hint = ""
        if isinstance(error, yaml.YAMLError) and "\\t" in str(error):
            # JSON の拡張子だけを変えた設定でいちばん起きる。原因を名指しする。
            hint = "（タブで字下げしています。YAML では空白で字下げしてください）"
        logger.warning("設定ファイルを読めない (%s)%s: %s", path, hint, error)
        return {}
    if not isinstance(settings, dict):
        logger.warning("設定ファイルの中身が辞書ではない (%s)", path)
        return {}
    logger.info("設定ファイルを読んだ: %s", path)
    return settings


def get_database_path(settings: Dict[str, Any]) -> Optional[str]:
    return settings.get("database_path")


def get_source_roots(settings: Dict[str, Any]) -> List[str]:
    """走査するメディアの root（#24）。**順序を保ち、空の値は落とす。**

    ``source_roots:`` に配列で書く。1つだけなら文字列でもよい。
    入れ子かどうかの検査は ``scanner.normalize_source_roots`` がする。

    **#24 より前の ``source_root:``（1つ）は読まない**（利用者の決定・PR #75）。
    残っていれば WARNING を出す。黙って無視すると、書いたつもりの値が効かない理由が
    分からない（古い ``app_settings.json`` と同じ扱い）。
    """
    if "source_root" in settings:
        logger.warning(
            "設定の source_root は読みません（#24 で source_roots に変わりました）。"
            " source_roots: に配列で書き直してください。"
        )
    value = settings.get("source_roots")
    if value is None:
        return []
    if isinstance(value, (str, Path)):
        value = [value]
    return [str(item) for item in value if item]


def get_output_root(settings: Dict[str, Any]) -> Optional[str]:
    return settings.get("output_root")


def get_rule_path(settings: Dict[str, Any]) -> Optional[str]:
    return settings.get("rule_path")


def get_dlib_model_dir(settings: Dict[str, Any]) -> Optional[str]:
    """dlib の学習済みモデルを置いたディレクトリ(任意)。"""
    return settings.get("dlib_model_dir")


# ----------------------------------------------------------------------
# 書き戻す（GUI の設定の窓・#131）
# ----------------------------------------------------------------------

#: GUI の設定の窓が直すキー。**ほかのキー（``dlib_model_dir`` など）は書き戻すときもそのまま残す。**
EDITABLE_KEYS = ("database_path", "source_roots", "output_root", "rule_path")


class SettingsWriteError(ValueError):
    """設定ファイルに書けない（読めないファイルを上書きしない・古い JSON を指している）。"""


def settings_write_path() -> Path:
    """GUI が設定を書く先。**次に起動したときに読まれる場所**にする。

    いま読んでいるファイルがあればそれ。無ければ ``PHOTOARCHIVE_CONFIG`` が指す先、
    それも無ければカレントディレクトリの ``config/app_settings.yml``（探す順の 1 と 2）。
    リポジトリ直下（探す順の 3）には自分からは作らない: 相対パスはカレントから解くので、
    カレントに置くほうが書いた値と解き方が食い違わない。
    """
    found = find_settings_path()
    if found is not None:
        return found
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path.cwd() / CONFIG_RELATIVE_PATH


def read_settings_for_write(path: Path) -> Dict[str, Any]:
    """書き戻す前に、いまのファイルの中身を読む。**読めなければ止める。**

    ``load_settings`` は読めないファイルを空の設定として扱う（設定が無くても動かすため）。
    書き戻す側でそれをすると、壊れたファイルの中身を空の設定で上書きしてしまう。
    """
    if path.suffix.lower() == ".json":
        raise SettingsWriteError(
            f"{path} は古い JSON の設定です。YAML の設定（{path.with_suffix('.yml')}）を指してください。"
        )
    if not path.exists():
        return {}
    try:
        settings = _read_yaml(path)
    except (OSError, yaml.YAMLError) as error:
        raise SettingsWriteError(f"設定ファイルを読めないので書きません（{path}）: {error}") from error
    if settings is None:
        return {}
    if not isinstance(settings, dict):
        raise SettingsWriteError(f"設定ファイルの中身が辞書ではないので書きません（{path}）")
    return settings


def write_settings(path: Path, values: Dict[str, Any]) -> Dict[str, Any]:
    """``values`` のキー（``EDITABLE_KEYS`` のうち）を設定ファイルに書く。書いた中身を返す。

    - **書く前に ``<名前>.bak`` に控える**（ルールの保存と同じ・コメントは消える）
    - 値が空（``None``・空文字・空の配列）のキーは消す（キーが無い＝設定しない）
    - ``values`` に無いキーと、``EDITABLE_KEYS`` 以外のキーは、もとのまま残す
    """
    settings = read_settings_for_write(path)
    for key, value in values.items():
        if key not in EDITABLE_KEYS:
            raise KeyError(f"設定の窓からは書かないキーです: {key}")
        if value is None or value == "" or value == []:
            settings.pop(key, None)
        else:
            settings[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    text = yaml.safe_dump(settings, allow_unicode=True, sort_keys=False)
    path.write_text(text, encoding="utf-8")
    return settings
