"""公開しない語の検査（#77。`scripts/check_private_terms.py`）。

**語は架空のもの**を使う。実際の一覧（`config/private_terms.yml`）は git に入らない。
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_private_terms.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_private_terms", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass が自分のモジュールを引くため
    spec.loader.exec_module(module)
    return module

TERMS = """
terms:
  - {text: "山田太郎", replace: "${PERSON_1}"}
  - {text: "山田", replace: "${SURNAME}"}
  - {text: "taro", replace: "person1", ignore_case: true}
  - {text: "/home/example", replace: "${HOME}"}
"""


@pytest.fixture()
def checker(tmp_path, monkeypatch):
    module = _load()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    terms = tmp_path / "terms.yml"
    terms.write_text(TERMS, encoding="utf-8")
    monkeypatch.setattr(module, "REPO_ROOT", repo)
    return module, repo, terms


def _add(repo: Path, name: str, text: str) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=repo, check=True)


def test_longer_terms_are_replaced_first(checker):
    """短い語（姓）が長い語（氏名）の一部を先に置き換えないこと。"""
    module, _, terms = checker
    loaded = module.load_terms(terms)

    assert module.replace("山田太郎と山田花子", loaded) == "${PERSON_1}と${SURNAME}花子"
    assert module.replace("Taro_photo/TARO", loaded) == "person1_photo/person1"


def test_a_tracked_file_with_a_term_fails_without_printing_the_term(checker, capsys):
    """見つけたら失敗し、**語は出さず変数だけを出す**（出力は PR 本文に貼られる）。"""
    module, repo, terms = checker
    _add(repo, "docs/a.md", "ok\n山田太郎の写真\n")
    _add(repo, "taro-notes.md", "clean\n")

    assert module.main(["--terms", str(terms)]) == 1

    out = capsys.readouterr().out
    assert "docs/a.md:2: 公開しない語（→ ${PERSON_1}）" in out
    assert "person1-notes.md: ファイル名に公開しない語（→ person1）" in out
    assert "山田" not in out and "taro" not in out.lower()


def test_a_clean_repository_passes(checker, capsys):
    module, repo, terms = checker
    _add(repo, "a.md", "${PERSON_1} の写真\n")

    assert module.main(["--terms", str(terms)]) == 0
    assert "公開しない語はありません" in capsys.readouterr().out


def test_staged_content_is_checked_before_commit(checker):
    """pre-commit: 作業ツリーではなく、コミットしようとしている中身を見る。"""
    module, repo, terms = checker
    _add(repo, "a.md", "山田太郎\n")
    (repo / "a.md").write_text("clean\n", encoding="utf-8")  # 作業ツリーだけ直しても通さない

    assert module.main(["--terms", str(terms), "--staged"]) == 1


def test_the_commit_message_is_checked(checker, tmp_path):
    module, _, terms = checker
    message = tmp_path / "MSG"
    message.write_text("#77 /home/example を直す\n", encoding="utf-8")

    assert module.main(["--terms", str(terms), "--message", str(message)]) == 1
    message.write_text("#77 ${HOME} を直す\n", encoding="utf-8")
    assert module.main(["--terms", str(terms), "--message", str(message)]) == 0


def test_fix_replaces_tracked_files(checker):
    module, repo, terms = checker
    _add(repo, "a.md", "山田太郎 /home/example/x\n")
    _add(repo, "image.bin", "")
    (repo / "image.bin").write_bytes(b"\x00" + "山田".encode())

    assert module.main(["--terms", str(terms), "--fix"]) == 0

    assert (repo / "a.md").read_text(encoding="utf-8") == "${PERSON_1} ${HOME}/x\n"
    assert "山田".encode() in (repo / "image.bin").read_bytes()  # バイナリは触らない


def test_filter_repo_expressions_keep_case_insensitive_terms(checker, tmp_path):
    """履歴の書き換え（段2）に渡す式。大文字小文字を区別しない語は正規表現にする。"""
    module, _, terms = checker
    out = tmp_path / "expressions.txt"

    assert module.main(["--terms", str(terms), "--filter-repo-expressions", str(out)]) == 0

    lines = out.read_text(encoding="utf-8").splitlines()
    assert "literal:山田太郎==>${PERSON_1}" in lines
    # 長い語から順（短い語が長い語の一部を先に置き換えないように）
    assert lines.index("literal:山田太郎==>${PERSON_1}") < lines.index("literal:山田==>${SURNAME}")
    assert "regex:(?i)taro==>person1" in lines


def test_without_a_term_list_the_check_is_skipped(checker, tmp_path, capsys):
    """一覧の無い環境（ほかの人の手元）では止めない。検査できないことは知らせる。"""
    module, _, _ = checker

    assert module.main(["--terms", str(tmp_path / "missing.yml")]) == 0
    assert "検査を飛ばします" in capsys.readouterr().out


def test_the_repository_holds_no_private_terms():
    """このリポジトリ自体に、手元の一覧の語が入っていないこと（一覧がある環境だけ）。"""
    module = _load()
    if not module.DEFAULT_TERMS.exists():
        pytest.skip("公開しない語の一覧が無い環境")

    assert module.main([]) == 0
