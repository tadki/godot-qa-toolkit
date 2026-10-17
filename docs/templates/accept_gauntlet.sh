#!/usr/bin/env bash
# accept_gauntlet.sh 模板（SEE-1426 P4，决策 rev1 §3.5）
#
# 硬序：互斥锁 → 清空 → 生成 → 冒烟(run) → run → mutate（每轮全量）。
# exit 非 0 即打回（CI 与本地同规则，v1 无 stamp）。
#
# 用法：bash accept_gauntlet.sh <project-root> <feature-path> [feature-key]
# 锁文件落 <project-root>/.qa-cache/accept-gauntlet.lock（flock，进程死
# 亡自动释放；复用 godot_gate.py 的 flock 模式）。并行双会话：第二条
# 流水线等待锁而非互删生成物。
set -euo pipefail

PROJECT_ROOT="${1:?usage: accept_gauntlet.sh <project-root> <feature-path> [feature-key]}"
FEATURE="${2:?usage: accept_gauntlet.sh <project-root> <feature-path> [feature-key]}"
KEY="${3:-$(basename "${FEATURE%.feature}" | tr -c 'A-Za-z0-9_' '_')}"

PYTHON_BIN="${PYTHON_BIN:-python3}"
GQT="PYTHONPATH=\"$(dirname "$0")/../../src\" $PYTHON_BIN -m godot_qa_toolkit.cli"

mkdir -p "$PROJECT_ROOT/.qa-cache"
exec 9>"$PROJECT_ROOT/.qa-cache/accept-gauntlet.lock"
echo "[gauntlet] waiting for project mutex: $PROJECT_ROOT"
flock 9
echo "[gauntlet] lock acquired: $PROJECT_ROOT"

cleanup() {
  # 变异轮的临时 IR 已由 run_mutate 内部恢复；这里只披露锁释放。
  echo "[gauntlet] releasing lock"
}
trap cleanup EXIT

echo "[gauntlet] 1/4 compile (clean-then-generate, opt-in write)"
eval "$GQT accept compile \"$FEATURE\" --project-root \"$PROJECT_ROOT\" --write" \
  || { echo "[gauntlet] FAIL at compile"; exit 1; }

echo "[gauntlet] 2/4 smoke + run (replay-check enforced)"
eval "$GQT accept run \"$PROJECT_ROOT\" --project-root \"$PROJECT_ROOT\" --key \"$KEY\"" \
  || { echo "[gauntlet] FAIL at run"; exit 1; }

echo "[gauntlet] 3/4 mutate (full matrix, survived = reject)"
eval "$GQT accept mutate \"$PROJECT_ROOT\" --project-root \"$PROJECT_ROOT\" --key \"$KEY\"" \
  || { echo "[gauntlet] FAIL at mutate"; exit 1; }

echo "[gauntlet] 4/4 arch guard over src/core (Pure Core boundary)"
if [ -d "$PROJECT_ROOT/src/core" ]; then
  eval "$GQT arch \"$PROJECT_ROOT/src/core\"" \
    || { echo "[gauntlet] FAIL at arch"; exit 1; }
else
  echo "[gauntlet] (no src/core — arch step skipped, see adoption_spec.md)"
fi

echo "[gauntlet] ALL GREEN: $PROJECT_ROOT ($KEY)"
