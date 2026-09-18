#!/usr/bin/env bash
# godot-qa-toolkit editable 安装脚本（SEE-1319 缺陷2 治理）。
#
# 目的：把系统级 `gqt` 的 editable 安装固定到【当前】工作树的 qa-toolkit，
# 消除多工作树机器上 gqt 执行旧代码（coverage 探针版本错配）的问题。
#
# 何时重跑：每次主仓 gitlink bump（子库 HEAD 变化）之后必须重跑一次，
# 使 editable 安装重新指向新工作树。`gqt doctor` 会在错配时提示本脚本。
#
# 重入安全：pip install -e 幂等，可重复执行。
#
# 首装等效命令（未装过 gqt / 无本脚本时手动执行，SEE-1319 D2）：
#   python3 -m pip install -e <本目录> [--break-system-packages]
# （PEP 668 externally-managed 环境——Ubuntu 23.04+/Debian 12+/WSL Python 3.12
#   系统解释器——必须带 --break-system-packages，pip 否则拒绝安装。）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLKIT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "[install.sh] toolkit root: $TOOLKIT_ROOT"

# 自检：确认脚本从工作树内的 qa-toolkit 运行（而非被拷贝到别处执行）。
if [ ! -f "$TOOLKIT_ROOT/pyproject.toml" ] || [ ! -d "$TOOLKIT_ROOT/src/godot_qa_toolkit" ]; then
    echo "[install.sh] ERROR: $TOOLKIT_ROOT is not a godot-qa-toolkit checkout" >&2
    exit 1
fi

# PEP 668 降级（SEE-1319 D1）：externally-managed 环境直接 install 被 pip 拒绝
# exit 1——先试常规安装，命中 PEP 668 拒绝时自动带 --break-system-packages 重试
# （重试仍失败则报错退出，不静默吞）。
if ! python3 -m pip install -e "$TOOLKIT_ROOT"; then
    echo "[install.sh] plain pip install failed — retrying with --break-system-packages (PEP 668 externally-managed environment)" >&2
    python3 -m pip install --break-system-packages -e "$TOOLKIT_ROOT"
fi

echo "[install.sh] verifying..."
python3 -c "import godot_qa_toolkit, os; p=os.path.dirname(godot_qa_toolkit.__file__); print(f'import resolves to: {p}'); assert p.startswith('$TOOLKIT_ROOT/src'), 'import does NOT resolve to this worktree'"

echo "[install.sh] done. Run 'gqt doctor' to confirm ok status."
