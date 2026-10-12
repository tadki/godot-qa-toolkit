"""gqt doctor：editable 安装位置与当前工作树（或独立检出）的一致性自检。

缺陷背景（SEE-1319 缺陷2）：多工作树机器上 `pip install -e` 指向某个历史
任务工作树的 qa-toolkit 拷贝，gqt 实际执行旧代码，coverage/mutation 判据
不可跨任务复现。doctor 把这类错配在跑判据之前显式暴露（exit 2）。

独立安装模式（SEE-1396）：gqt 已从 KOL 拆出为独立 CLI，规范检出位于
任务工作树之外的可持久路径。可用 `GQT_TOOLKIT_ROOT` 环境变量显式指定
工具库根（路径不存在时降级 skipped，不误报）；未设置时退回从 cwd 向上
探测工具检出根（兼容既有工作树布局）。cwd 不在任何工具树内且未设置
环境变量时为 standalone skipped——独立全局使用是合法形态，绝不误报 exit 2。

判据链（任一环节失败均降级为 skipped，绝不误报错）：
1. editable location：`pip show` 的 Editable project location；
2. 工具库根：`GQT_TOOLKIT_ROOT` 环境变量，或从 cwd 向上找 gqt 检出根；
3. 比对：editable 路径 == 工具库根。HEAD 级比对由 install.sh 重装流程
   兜底（editable + 同根 ⇒ import 已指向当前树）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_TOOLKIT_ROOT_ENV = "GQT_TOOLKIT_ROOT"

_FIX_HINT = (
    "gqt 安装与当前工作树错配。修复：在 gqt 检出根运行 scripts/install.sh "
    "（gitlink/HEAD 更新后也须重跑一次）；独立安装场景先设置 "
    "GQT_TOOLKIT_ROOT=<gqt检出根> 再次自检。详见 scripts/install.sh 注释。"
)


def _pip_show() -> str | None:
    """pip show 原始输出；pip 不可用/未安装返回 None（不抛异常）。"""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "show", "godot-qa-toolkit"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"gqt doctor: pip show failed: {e}", file=sys.stderr)
        return None
    if r.returncode != 0:
        return None
    return r.stdout


def resolve_editable_location() -> str | None:
    """从 pip show 解析 Editable project location；非 editable/未安装 → None。"""
    out = _pip_show()
    if not out:
        return None
    for line in out.splitlines():
        if line.startswith("Editable project location:"):
            value = line.split(":", 1)[1].strip()
            return value or None
    return None


def locate_toolkit_root(start: Path) -> Path | None:
    """从 start 向上找工具库根；环境变量优先，退回 .dev/qa-toolkit 旧约定。

    SEE-1396：独立安装时由 `GQT_TOOLKIT_ROOT` 显式指定工具库根（路径缺失
    返回 None，仅降级 skipped，不误报）；历史 KOL 工作树布局仍可识别。
    """
    env_root = os.environ.get(_TOOLKIT_ROOT_ENV)
    if env_root:
        cand = Path(env_root)
        return cand if cand.is_dir() else None
    for p in (start, *start.parents):
        cand = p / ".dev" / "qa-toolkit"
        if cand.is_dir():
            return cand
    return None


def _gitlink_head(root: Path) -> str | None:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(root),
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"gqt doctor: git rev-parse failed in {root}: {e}", file=sys.stderr)
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def check_install(cwd: Path | None = None) -> tuple[bool, dict]:
    """执行一致性检查；返回 (ok, detail)。ok=False 时 detail 带修复指引。"""
    cwd = Path(cwd) if cwd else Path.cwd()
    editable = resolve_editable_location()
    toolkit_root = locate_toolkit_root(cwd)

    if editable is None and toolkit_root is None:
        # 两边都缺失：无从比对，也无从误报（例如在主仓外裸跑）。
        return True, {"status": "skipped", "reason": "no editable install and no toolkit root resolved"}

    if toolkit_root is None:
        # 有安装但目标树未解析（独立全局使用是合法形态）：跳过根比对，不误报。
        return True, {"status": "skipped", "reason": "cwd is not inside a toolkit tree (standalone usage)"}

    if editable is None:
        return False, {
            "status": "not_installed",
            "toolkit_root": str(toolkit_root),
            "fix_hint": _FIX_HINT,
        }

    if Path(editable).resolve() != toolkit_root.resolve():
        return False, {
            "status": "mismatch",
            "editable_location": editable,
            "toolkit_root": str(toolkit_root),
            "gitlink_head": _gitlink_head(toolkit_root),
            "fix_hint": _FIX_HINT,
        }

    return True, {
        "status": "ok",
        "toolkit_root": str(toolkit_root),
        "gitlink_head": _gitlink_head(toolkit_root),
    }


def cmd_doctor(_args) -> int:
    """gqt doctor 入口：stderr 打印诊断，错配时 exit 2（不污染 stdout JSON）。"""
    ok, detail = check_install()
    print(json.dumps(detail, ensure_ascii=False, indent=2), file=sys.stderr)
    if ok:
        return 0
    print(f"gqt doctor: install mismatch ({detail['status']})", file=sys.stderr)
    return 2
