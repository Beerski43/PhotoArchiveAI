"""実データと生成物の置き場が、シンボリックリンクでも git に入らないこと。

`output/` のように末尾に `/` を付けた規則は**ディレクトリにしか当たらない。**
利用者が `output` を NFS へのシンボリックリンクに替えたところ、`git add -A` が
リンクを拾い、リンク先のパス（公開しない語）をコミットしかけた。pre-commit の
検査が止めた（PR #85 のレビュー対応中。本筋の外だが直した）。
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("git") is None, reason="git が無い")
@pytest.mark.parametrize("name", ["output", "data", "models", "mediaFiles"])
def test_the_data_folders_are_ignored_even_as_symlinks(name):
    # 末尾の `/` が無いパスは、git にはディレクトリかどうか分からない（リンクと同じ扱い）。
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", name], cwd=REPO, check=False
    )
    assert result.returncode == 0, f"{name} がシンボリックリンクのとき git に入ってしまう"
