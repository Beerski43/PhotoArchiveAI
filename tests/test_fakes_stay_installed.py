"""`models` マーカーの無いテストが、実物のモデルを読みに行かないこと。

**回帰テスト（`-m "not models"`）は実物の学習済みモデルを要らない**という前提で
成り立っている（CLAUDE.md §5）。この前提が破れると、**モデルを置いている作者の
手元だけ通り、置いていないチェックアウトで落ちる。**

実際に起きた（PR #60 の指摘1）。`tests/test_scanner_incremental.py` が
`monkeypatch.undo()` を呼んでおり、**自分の差し替えだけでなく conftest の autouse
フィクスチャ（フェイクのモデル）まで巻き戻していた。** dlib のモデルは
`face_recognition_models` パッケージから見つかったので表に出なかったが、
ArcFace は gitignore された `models/` にしか無いので露出した。

**実行時に捕まえる番人は置けない。** `monkeypatch.undo()` は差し替えを**すべて**
巻き戻すので、番人ごと消える（これも PR #60 で実測した）。だから静的に見張る。
"""

from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

#: 実物のモデルを使うテストはここに集める。`pytestmark = pytest.mark.models` が付く。
MODELS_MARKED = {"test_face_real.py"}

#: この番人自身は、散文として `monkeypatch.undo()` を書くので除く。
SELF = Path(__file__).name


def _calls_undo(line: str) -> bool:
    """その行が `monkeypatch.undo()` を**呼んでいる**か。

    **コメントや散文を拾わない。** 最初に書いた版はコメント行まで数えて、
    自分の注意書きで落ちた。行頭（字下げを除く）が呼び出しであることを見る。
    """
    code = line.split("#", 1)[0].strip()
    return code.startswith("monkeypatch.undo()")


def test_no_unmarked_test_file_undoes_the_fakes():
    """**`monkeypatch.undo()` を `models` マーカーの外で使わない。**

    使うと conftest のフェイクが外れ、実物のモデルを読みに行く。必要なのが
    「自分が差し替えたぶんだけ戻す」ことなら、`monkeypatch.context()` を使う。
    """
    offenders = {}
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        if path.name in MODELS_MARKED or path.name == SELF:
            continue
        lines = [
            index + 1
            for index, line in enumerate(path.read_text(encoding="utf-8").splitlines())
            if _calls_undo(line)
        ]
        if lines:
            offenders[path.name] = lines

    assert not offenders, (
        "monkeypatch.undo() が `models` マーカーの外で使われている: "
        f"{offenders}。conftest のフェイクまで巻き戻り、実物のモデルを読みに行く。"
        " 自分の差し替えだけ戻したいなら monkeypatch.context() を使うこと。"
    )


def test_the_marked_file_really_carries_the_marker():
    """**免除した側が、本当に `models` マーカーを持っていること。**

    持っていないのに免除すると、免除リストが抜け道になる。
    """
    for name in MODELS_MARKED:
        text = (TESTS_DIR / name).read_text(encoding="utf-8")
        assert "pytest.mark.models" in text, f"{name} に models マーカーが無い"
