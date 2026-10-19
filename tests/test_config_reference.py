"""設定ファイルの説明（`docs/operation/CONFIG.md`・#96）が実装から古くならない。

**一覧は黙って古くなる。** ルールのキーを足しても、説明の表に足し忘れれば
利用者は書けることを知らないままになる。既定を変えても、表の数字は前のまま残る。
そこで、**コードが読むキー**と**コードが持つ既定**から、表とサンプルを照らし合わせる。
"""

import re
from pathlib import Path
from typing import Dict, List, Optional

from photoarchive_ai import config, scoring, selection

REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCE = REPO_ROOT / "docs/operation/CONFIG.md"


def _settings_keys() -> List[str]:
    """`config.py` が設定から読むキー（``settings.get("...")``）。"""
    source = Path(config.__file__).read_text(encoding="utf-8")
    return sorted(set(re.findall(r'settings\.get\("(\w+)"\)', source)))


def _terms_keys() -> List[str]:
    """`scripts/check_private_terms.py` が ``terms`` の各項目から読むキー。"""
    source = (REPO_ROOT / "scripts/check_private_terms.py").read_text(encoding="utf-8")
    found = re.findall(r'entry\["(\w+)"\]|entry\.get\("(\w+)"', source)
    return sorted({key for pair in found for key in pair if key})


def _rule_keys() -> List[str]:
    """ルールの最上位に書けるキー（`selection.load_rule` が確かめるものと、日付・枚数・重み）。"""
    return sorted([*selection.RULE_FLAGS, *selection.RULE_NUMBERS, "count_per_year", "date", "weights"])


def _row(text: str, key: str) -> Optional[List[str]]:
    """表の中で、最初の欄が `` `key` `` で始まる行の欄。無ければ None。"""
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.startswith("|") and cells and re.match(rf"`{re.escape(key)}`", cells[0]):
            return cells
    return None


def _default_in(cells: List[str]) -> Optional[str]:
    """行の中の既定の値（`` `true` `` や `` `0.35` `` だけの欄）。"""
    for cell in cells[1:]:
        match = re.fullmatch(r"`([^`]+)`", cell)
        if match:
            return match.group(1)
    return None


def _same(written: Optional[str], default) -> bool:
    if written is None:
        return False
    if isinstance(default, bool):
        return written == ("true" if default else "false")
    try:
        return float(written) == float(default)
    except ValueError:
        return False


def missing_from_reference(text: str) -> List[str]:
    """説明に書かれていないキーと、既定が実装と違う行。"""
    problems = []
    keys = (
        _settings_keys()
        + ["date.start", "date.end", *_rule_keys()]
        + list(selection.WEIGHT_KEYS)
        + [f"terms[].{key}" for key in _terms_keys()]
    )
    for key in keys:
        if _row(text, key) is None and f"`{key}`" not in text:
            problems.append(f"{key}: 書かれていない")
    defaults: Dict[str, object] = {
        **selection.RULE_FLAGS,
        **selection.RULE_NUMBERS,
        **scoring.Weights.default()._asdict(),
    }
    for key, default in defaults.items():
        cells = _row(text, key)
        if cells is None:
            problems.append(f"{key}: 表の行が無い")
        elif not _same(_default_in(cells), default):
            problems.append(f"{key}: 既定が {_default_in(cells)!r}（実装は {default!r}）")
    return problems


def test_the_reference_describes_every_key_with_its_current_default():
    problems = missing_from_reference(REFERENCE.read_text(encoding="utf-8"))

    assert not problems, (
        "docs/operation/CONFIG.md が実装と食い違っている。キーを足した・既定を変えたなら、"
        "表も直すこと。\n  " + "\n  ".join(problems)
    )


def test_the_check_would_catch_a_missing_key_and_a_stale_default():
    """この番人自身が働くことを確かめる。"""
    text = REFERENCE.read_text(encoding="utf-8")
    dropped = "\n".join(line for line in text.splitlines() if not line.startswith("| `copy_distance`"))
    stale = text.replace("| `member_bonus` | `20` |", "| `member_bonus` | `10` |")

    assert any(problem.startswith("copy_distance") for problem in missing_from_reference(dropped))
    assert "member_bonus: 既定が '10'（実装は 20.0）" in missing_from_reference(stale)


def test_the_samples_mention_every_key():
    """サンプルは利用者が最初に写すもの。書けるキーはコメントでも載せておく。"""
    settings = (REPO_ROOT / "config/app_settings.sample.yml").read_text(encoding="utf-8")
    rule = (REPO_ROOT / "config/rule.sample.yml").read_text(encoding="utf-8")
    terms = (REPO_ROOT / "config/private_terms.sample.yml").read_text(encoding="utf-8")

    missing = [key for key in _settings_keys() if f"{key}:" not in settings]
    missing += [key for key in [*_rule_keys(), *selection.WEIGHT_KEYS] if f"{key}:" not in rule]
    missing += [key for key in _terms_keys() if f"{key}:" not in terms]
    assert not missing, f"サンプルに無いキー: {missing}"


def test_the_reference_is_reachable_from_the_indexes():
    """索引から辿れない説明は、無いのと同じ。"""
    for index in ("README.md", "docs/README.md", "docs/spec/Specification.md"):
        assert "operation/CONFIG.md" in (REPO_ROOT / index).read_text(encoding="utf-8"), index
