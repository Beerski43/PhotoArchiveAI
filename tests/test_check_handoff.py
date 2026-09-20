"""引き継ぎの点検が、**自分の故障を隠さない**ことを確かめる。

`scripts/check_handoff.py` は `run_regression.sh` の 5/5 から毎回呼ばれる。
**点検が働かなかったことを「異常なし」と報告すると、壊れた番人に守られている
つもりになる。** ここで見張るのはその1点。

最初の版は `gh` が標準エラーに1行出すだけで壊れた（新版のお知らせなど）。
stdout と stderr を混ぜて JSON に渡していたため、読み取りに失敗して
「PR を確認できない」へ落ち、**PR の無いブランチの警告が黙って消えていた**
（PR #50 のレビュー指摘1）。
"""

import importlib.util
import os
import stat
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    """`scripts/` はパッケージではないので、パス指定で読む。"""
    path = REPO_ROOT / "scripts/check_handoff.py"
    spec = importlib.util.spec_from_file_location("check_handoff", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_handoff = _load_module()


def _fake_gh(directory: Path, script: str) -> None:
    """`gh` の代わりを `PATH` の先頭に置く。"""
    directory.mkdir(parents=True, exist_ok=True)
    fake = directory / "gh"
    fake.write_text(script, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def test_a_noisy_gh_does_not_turn_the_check_into_an_all_clear(tmp_path, monkeypatch, capsys):
    """**`gh` が標準エラーに何か書いても、判定が消えないこと。**

    `gh` は新版のお知らせなどを stderr に出す。それを stdout と混ぜて
    JSON に渡すと読み取りに失敗し、**PR の無いブランチの警告ごと消える。**
    消えたことは画面に出ないので、見張りが外れたと気づけない。
    """
    _fake_gh(
        tmp_path / "bin",
        "#!/usr/bin/env bash\n"
        'echo "gh: A new release of gh is available" >&2\n'
        "echo '[{\"headRefName\":\"feature/#1_already-open\",\"number\":7}]'\n",
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")

    warnings = check_handoff.check_branches_have_pull_requests(
        {"feature/#2_floating": ["abc1234 何かの作業"]}
    )

    assert any("PR が無く" in text for text in warnings), (
        "stderr に何か出ただけで、PR の無いブランチを見逃した"
    )
    assert not any("確認できなかった" in text for text in warnings)
    assert "PR #7 あり" not in capsys.readouterr().out  # 別ブランチの PR を流用しない


def test_a_branch_with_an_open_pull_request_is_not_a_warning(tmp_path, monkeypatch):
    """PR があれば作業は見える場所にある。鳴らさない。"""
    _fake_gh(
        tmp_path / "bin",
        "#!/usr/bin/env bash\n"
        "echo '[{\"headRefName\":\"feature/#2_floating\",\"number\":7}]'\n",
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")

    warnings = check_handoff.check_branches_have_pull_requests(
        {"feature/#2_floating": ["abc1234 何かの作業"]}
    )

    assert warnings == []


def test_a_gh_that_cannot_run_is_reported_instead_of_being_ignored(tmp_path, monkeypatch):
    """**確認できなかったことを、確認して問題が無かったことと同じ顔で出さない。**

    認証切れやコマンド不在は起こる（実際に起きた）。黙って飛ばすと、
    その回だけ見張りが外れていたことに誰も気づけない。
    """
    _fake_gh(
        tmp_path / "bin",
        '#!/usr/bin/env bash\necho "gh: authentication required" >&2\nexit 1\n',
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")

    warnings = check_handoff.check_branches_have_pull_requests(
        {"feature/#2_floating": ["abc1234 何かの作業"]}
    )

    assert any("確認できなかった" in text for text in warnings)


def test_a_documented_branch_without_a_pull_request_is_not_a_warning(
    tmp_path, monkeypatch
):
    """申し送りに書いてあるブランチは鳴らさない。

    **鳴りっぱなしの警告は、隣の本物ごと読まれなくなる。** PR はまだでも、
    申し送りに状態が書いてあるなら「浮いている」わけではない。
    """
    _fake_gh(tmp_path / "bin", "#!/usr/bin/env bash\necho '[]'\n")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(
        check_handoff, "branches_mentioned_in_handoff_notes", lambda: {"feature/#2_floating"}
    )

    warnings = check_handoff.check_branches_have_pull_requests(
        {"feature/#2_floating": ["abc1234 何かの作業"]}
    )

    assert warnings == []


def test_an_undocumented_branch_without_a_pull_request_is_a_warning(tmp_path, monkeypatch):
    """PR も申し送りも無いものだけを鳴らす。これが元々の目的（#48 の形）。"""
    _fake_gh(tmp_path / "bin", "#!/usr/bin/env bash\necho '[]'\n")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(check_handoff, "branches_mentioned_in_handoff_notes", set)

    warnings = check_handoff.check_branches_have_pull_requests(
        {"feature/#2_floating": ["abc1234 何かの作業"]}
    )

    assert any("feature/#2_floating" in text for text in warnings)


def test_a_slow_command_does_not_block_the_check():
    """**繋がらない環境で止まらないこと。**

    5/5 は PR の前に必ず通る。`git fetch` が返らないと、画面には
    `=== 5/5 引き継ぎの状態 ===` が出たきり何も起きない。
    """
    code, _, err = check_handoff.run("sleep", "5", timeout=0.5)

    assert code == 124
    assert "返らなかった" in err


def test_the_output_streams_are_not_mixed():
    """stdout と stderr を分けて返すこと。混ぜると JSON の読み取りが壊れる。"""
    code, out, err = check_handoff.run(
        sys.executable, "-c", "import sys; print('データ'); print('雑音', file=sys.stderr)"
    )

    assert code == 0
    assert out == "データ"
    assert err == "雑音"


def test_offline_does_not_touch_the_remote(monkeypatch):
    """`--offline` なら `git fetch` を呼ばないこと。"""
    calls = []

    def spy(*args, **kwargs):
        calls.append(args)
        return 0, "", ""

    monkeypatch.setattr(check_handoff, "run", spy)
    check_handoff.unmerged_branches(offline=True)

    assert not any("fetch" in args for args in calls)


def test_a_failed_fetch_says_the_judgement_used_stale_information(monkeypatch):
    """**黙って古い情報で判定しない。**

    取り込み忘れた remote の状態で「異常なし」と出るのが、いちばん危ない。
    """

    def failing(*args, **kwargs):
        if "fetch" in args:
            return 128, "", "could not resolve host"
        return 0, "", ""

    monkeypatch.setattr(check_handoff, "run", failing)
    _, warnings = check_handoff.unmerged_branches(offline=False)

    assert any("古い情報で判定している" in text for text in warnings)


def test_the_working_tree_is_shown_but_not_warned_about(capsys, monkeypatch):
    """コミット前に回すのは正常。毎回鳴らさない。"""
    monkeypatch.setattr(check_handoff, "run", lambda *a, **k: (0, " M docs/plan/ROADMAP.md", ""))

    check_handoff.show_working_tree()

    out = capsys.readouterr().out
    assert "ROADMAP.md" in out
    assert "正常" in out
