#!/usr/bin/env bash
#
# 回帰テスト。PR を起票する前に必ず実行し、最終行を PR 本文へ貼ること。
#
#   ./scripts/run_regression.sh
#
# 何を実行するか:
#   1. `pytest -m "not models"` — 実物の学習済みモデルを必要としないテスト全部
#   2. コマンドの起動スモーク — entry point の破損はテストでは捕まらないため
#   3. `pytest -m models` — 実物の dlib モデルがある環境でのみ。参考情報あつかい
#   4. 作業履歴の切り出しが必要かの確認 — 通知のみ。結果に影響しない
#   5. 引き継ぎの状態 — 通知のみ。結果に影響しない
#
# 終了コード: 1 と 2 がすべて成功したときだけ 0。
#
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1

if [ -z "${VIRTUAL_ENV:-}" ] && [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

LOG="$(mktemp -t photoarchive-regression-XXXXXX.log)"
trap 'rm -f "$LOG"' EXIT

failed=0

echo "=== 1/5 回帰テスト (pytest -m \"not models\") ==="
python -m pytest -q -m "not models" --durations=5 2>&1 | tee "$LOG"
pytest_status="${PIPESTATUS[0]}"
[ "$pytest_status" -eq 0 ] || failed=1

echo
echo "=== 2/5 コマンドの起動スモーク ==="
for cmd in "photoarchive --help" "photoarchive-gui --help"; do
  if $cmd >/dev/null 2>&1; then
    echo "  OK   $cmd"
  else
    echo "  NG   $cmd"
    failed=1
  fi
done

echo
echo "=== 3/5 実物モデルを使う確認 (pytest -m models) ==="
echo "    環境依存のため、結果は回帰テストの合否に含めない。"
python -m pytest -q -m models 2>&1 | tail -3

echo
echo "=== 4/5 作業履歴の状態 ==="
if [ -f scripts/archive_worklog.py ]; then
  python scripts/archive_worklog.py --check || true
else
  echo "  scripts/archive_worklog.py が無い"
fi

echo
echo "=== 5/5 引き継ぎの状態 ==="
# **文書に書いていない作業が、ブランチの中に浮いていないか。**
# セッションを切る前に確認する約束(CLAUDE.md §5)だが、思い出せるかに頼ると
# 忘れる。PR の前に必ず通るここへ置いて、目に入るようにする。
# 通知のみで、回帰テストの合否には含めない(gh がネットワークを使うため)。
if [ -f scripts/check_handoff.py ]; then
  # 1回だけ実行する(git fetch と gh を二度叩かない)。
  handoff="$(python scripts/check_handoff.py 2>&1)"
  if printf '%s' "$handoff" | grep -q "^気にすること:"; then
    printf '%s\n' "$handoff" | sed -n '/^気にすること:/,/^---/p' | grep -v '^---' | sed 's/^/  /'
  else
    echo "  引き継ぎで気になる点は見つからなかった"
  fi
else
  echo "  scripts/check_handoff.py が無い"
fi

# 1 の結果をまとめる。pytest は出力先が端末だと着色するので、
# 集計はシェルの grep ではなく Python 側で行う(エスケープを剥がしてから
# 数える)。集計できなければ非ゼロで返るので、0 passed が成功のように
# 見えることはない。
echo
echo "------------------------------------------------------------"
if ! python scripts/summarize_pytest.py "$LOG"; then
  failed=1
fi
echo "------------------------------------------------------------"

exit "$failed"
