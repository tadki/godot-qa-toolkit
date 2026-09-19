"""gqt doctor：editable 安装位置与当前工作树 gitlink 的一致性自检。

缺陷背景（SEE-1319 缺陷2）：多工作树机器上 `pip install -e` 指向某个历史
任务工作树的 qa-toolkit 拷贝，gqt 实际执行旧代码，coverage/mutation 判据
不可跨任务复现。doctor 把这类错配在跑判据之前显式暴露（exit 2）。

判据链（任一环节失败均降级为 skipped，绝不误报错）：
1. editable location：`pip show` 的 Editable project location；
2. 工具库根：从 cwd 向上找 `.dev/qa-toolkit`；
3. 比对：editable 路径 == 工具库根。HEAD 级比对由 install.sh 重装流程
   兜底（editable + 同根 ⇒ import 已指向当前树）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_FIX_HINT = (
    "gqt 安装与当前工作树错配。修复：bash .dev/qa-toolkit/scripts/install.sh "
    "（gitlink bump 后也须重跑一次）。详见 scripts/install.sh 注释。"
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
    """从 start 向上找名为 .dev/qa-toolkit 的目录；找不到返回 None。"""
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
        return True, {"status": "skipped", "reason": "no editable install and no .dev/qa-toolkit above cwd"}

    if toolkit_root is None:
        # 有安装但 cwd 不在工具树内：跳过根比对（无法判定目标树）。
        return True, {"status": "skipped", "reason": "cwd is not inside a .dev/qa-toolkit tree"}

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
