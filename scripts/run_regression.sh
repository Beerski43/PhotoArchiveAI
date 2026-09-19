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

echo "=== 1/4 回帰テスト (pytest -m \"not models\") ==="
python -m pytest -q -m "not models" --durations=5 2>&1 | tee "$LOG"
pytest_status="${PIPESTATUS[0]}"
[ "$pytest_status" -eq 0 ] || failed=1

echo
echo "=== 2/4 コマンドの起動スモーク ==="
for cmd in "photoarchive --help" "photoarchive-gui --help"; do
  if $cmd >/dev/null 2>&1; then
    echo "  OK   $cmd"
  else
    echo "  NG   $cmd"
    failed=1
  fi
done

echo
echo "=== 3/4 実物モデルを使う確認 (pytest -m models) ==="
echo "    環境依存のため、結果は回帰テストの合否に含めない。"
python -m pytest -q -m models 2>&1 | tail -3

echo
echo "=== 4/4 作業履歴の状態 ==="
if [ -f scripts/archive_worklog.py ]; then
  python scripts/archive_worklog.py --check || true
else
  echo "  scripts/archive_worklog.py が無い"
fi

# 1 の結果をまとめる。pytest -q の最終行は
#   "37 passed, 3 deselected in 1.10s" のような形。
summary="$(grep -E '^[0-9]+ (passed|failed|error)|[0-9]+ (passed|failed|error)s? ' "$LOG" | tail -1)"
passed="$(printf '%s' "$summary" | grep -oE '[0-9]+ passed'  | grep -oE '^[0-9]+' || true)"
failures="$(printf '%s' "$summary" | grep -oE '[0-9]+ failed' | grep -oE '^[0-9]+' || true)"
errors="$(printf '%s' "$summary" | grep -oE '[0-9]+ error'  | grep -oE '^[0-9]+' || true)"
elapsed="$(printf '%s' "$summary" | grep -oE 'in [0-9.]+s'  | grep -oE '[0-9.]+' || true)"
failures=$(( ${failures:-0} + ${errors:-0} ))

echo
echo "------------------------------------------------------------"
printf '回帰テスト: %s passed / %s failed (%ss) 実行日: %s\n' \
  "${passed:-0}" "$failures" "${elapsed:-?}" "$(date +%Y-%m-%d)"
echo "------------------------------------------------------------"

exit "$failed"
