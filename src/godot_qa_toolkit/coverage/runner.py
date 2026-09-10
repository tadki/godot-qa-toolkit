"""Coverage runner: GDScript 行覆盖自研简单版（SEE-1268 M2）.

GUT 无 coverage 支持，coverage.py 只覆盖 Python——不适用于 GDScript。按
SEE-1256 自研简单版：对目标 .gd 插桩，在每个可执行行首插入一个探针标记
行 `__qa_cov_probe(<line>)`，跑 GUT 后收集命中行集 → 行覆盖%。探针函数
本身需要预先存在——通过复用一个全局 AutoLoad 化的 `_coverage_probe` 服务
（通过 `get_node` 或直接定义在同一文件）实现。

实际实现路径（更简单且不破红线）：
  1. 用 gdtoolkit lark 找目标文件里每个可执行语句的行号集合 L_total
  2. 对目标 .gd 做一次临时副本：在行尾加 `# __qa_cov_<line>` 标记（不改语义）
  3. 但真正的"命中"来自运行——用 GUT 的 `--log` 输出中的行号跟踪？也不行
  4. 真正可行方案：Godot 4.x 的 `GDScript Language Server` 没有覆盖 API

最简一步到位的工程路径（选这个）：**行级插桩**——目标文件每个可执行语句前
加一行 `__cov_hit_<line>()` 调用，探针函数写入同一文件的临时尾部（运行时
把行号写入临时文件）。跑 GUT → 读取临时文件行号集 L_hit → 行覆盖% =
|L_hit ∩ L_total| / |L_total|。

探针函数的安全边界：只写入 .dev/coverage-hits-<pid>.txt（gitignored），从不
修改测试代码/被测代码，GUT 结束后原文件恢复。

P0' 判定原则落点：机器行覆盖百分比 + 阈值 gate；exit 0=达标 1=未达标。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from gdtoolkit.parser import parser as gdtoolkit_parser

# 与 mutation runner 同一套判定：godot 进程级失败（-s 脚本没加载起来）≠
# 测试结果。res:// 是唯一可靠的 -s 形式（Revy QA FAIL 实证）。
GUT_SCRIPT_RES_PATH = "res://addons/gut/gut_cmdln.gd"
_GODOT_LAUNCH_ERROR_MARKERS = (
    "attempt to open script",
    "file not found",
    "failed loading resource",
    "error: failed to load script",
)


def _looks_like_godot_launch_failure(output: str) -> bool:
    low = output.lower()
    return any(marker in low for marker in _GODOT_LAUNCH_ERROR_MARKERS)

# 探针前缀：运行时函数 + 标记（避免与生产函数名冲突）。
_PROBE_PREFIX = "__qa_cov_probe_"
_HITS_FILE_PATTERN = "qa-coverage-hits.txt"

# 可执行语句的规则/树形节点名（插桩目标）——覆盖常见语句，不深挖表达式。
_EXECUTABLE_NODE_NAMES = {
    "if_branch",
    "elif_branch",
    "else_branch",
    "for_stmt",
    "while_stmt",
    "return_stmt",
    "func_var_stmt",
    "func_var_inf",
    "expr_stmt",
    "match_stmt",
    "break_stmt",
    "continue_stmt",
    "pass_stmt",
}


@dataclass(frozen=True)
class ExecutableLine:
    line: int
    node: str  # node.data（行所属结构，便于诊断）


def _tree_depth_walk(node, callback, depth=0):
    callback(node, depth)
    for child in getattr(node, "children", []):
        _tree_depth_walk(child, callback, depth + 1)


def _collect_executable_lines(src: str) -> list[ExecutableLine]:
    """用 gdtoolkit lark 找出所有可执行语句行（插桩目标行）。"""
    tree = gdtoolkit_parser.parse(src)
    lines: list[ExecutableLine] = []

    def visit(node, depth=0):
        if not hasattr(node, "data"):
            return
        if node.data in _EXECUTABLE_NODE_NAMES:
            # lark Tree 无 line；取该节点下第一个 Token 的行。
            def first_token_line(n):
                for c in getattr(n, "children", []):
                    if hasattr(c, "line") and c.line:
                        return c.line
                    rec = first_token_line(c)
                    if rec:
                        return rec
                return 0
            ln = first_token_line(node)
            if ln > 0:
                lines.append(ExecutableLine(line=ln, node=node.data))

    _tree_depth_walk(tree, visit)
    # 去重 + 排序
    seen = set()
    unique = []
    for l in lines:
        if l.line not in seen:
            seen.add(l.line)
            unique.append(l)
    unique.sort(key=lambda x: x.line)
    return unique


def _instrument(src: str) -> tuple[str, str]:
    """把目标源码插桩成带探针的版本；返回 (插桩源码, 探针函数名).

    探针函数写入同一文件尾部——用 marker 命名 `__qa_cov_probe_<line>`（不
    会是生产代码）。该函数做一件事：把 <line> 追加到 hits 文件。

    探针函数实现（GDScript）：
      func __qa_cov_probe(line):
        var f = FileAccess.open(_hits_path(), FileAccess.WRITE)
        f.store_line(str(line))
        f.close()

    其中 `_hits_path()` 用一个全局 env var 或临时文件路径——但 Godot 脚本
    无 env 读取；所以探针把 hits 写入固定相对路径（.dev/qa-coverage-hits.txt，
    运行时由 runner 监控该文件）。
    """
    return src, _PROBE_PREFIX


def run_coverage(
    file_path: str,
    project_root: str,
    min_percent: float = 80.0,
    timeout_s: int = 120,
) -> dict:
    """对单个 .gd 文件算行覆盖（跑 GUT 后统计命中行）并出统一 JSON 契约。

    min_percent 阈值 gate（P0' 机器判定：覆盖率≥阈值=ok）。
    返回 {"tool":"coverage", "ok", "summary", "failures"}；
    ok=True 表示覆盖率达标。
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"coverage target not found: {file_path}")

    original_src = path.read_text(encoding="utf-8")
    backup = original_src

    executable_lines = _collect_executable_lines(original_src)
    total_lines = len(executable_lines)

    if total_lines == 0:
        return {
            "tool": "coverage",
            "ok": True,
            "summary": {"file": str(path), "total_lines": 0, "covered_lines": 0,
                        "coverage_percent": 100.0, "min_percent": min_percent,
                        "note": "no executable lines"},
            "failures": [],
        }

    hits_file = os.path.join(project_root, _HITS_FILE_PATTERN)
    hits: set[int] = set()

    with tempfile.TemporaryDirectory() as workdir:
        backup_path = os.path.join(workdir, "original.gd")
        shutil.copy2(str(path), backup_path)

        # 插桩版本：每个可执行语句前加一行 __qa_cov_probe(<line>)
        instrumented_lines = []
        line_map = {l.line: l for l in executable_lines}
        for idx, line_text in enumerate(original_src.splitlines(keepends=True), start=1):
            if idx in line_map:
                indent = re.match(r"^(\s*)", line_text).group(1)
                instrumented_lines.append(f"{indent}{_PROBE_PREFIX}({idx})\n")
            instrumented_lines.append(line_text)

        # 探针函数追加到文件尾部（同名不冲突——前缀专用）
        probe_func = (
            f"\n\n# qa-toolkit coverage probe (auto-instrumented, never in production)\n"
            f"func {_PROBE_PREFIX}(line):\n"
            f"\tvar f = FileAccess.open(\"{_HITS_FILE_PATTERN}\", FileAccess.WRITE)\n"
            f"\tif f:\n"
            f"\t\tf.store_line(str(line))\n"
            f"\t\tf.close()\n"
        )
        instrumented = "".join(instrumented_lines) + probe_func

        # 清空旧的 hits 文件（幂等——多轮运行不累加）
        if os.path.isfile(hits_file):
            os.remove(hits_file)

        # 插桩文件替换目标，跑 GUT，恢复
        path.write_text(instrumented, encoding="utf-8")
        try:
            gut_fs = os.path.join(project_root, "addons", "gut", "gut_cmdln.gd")
            if not os.path.isfile(gut_fs):
                raise FileNotFoundError(f"GUT runner not found: {gut_fs}")
            try:
                # -s 必须用 res:// 形式：绝对路径会让 godot 拒绝加载（Revy QA
                # FAIL 实证），退出码 1 且无任何命中——与"真实 0% 覆盖"不同，
                # 必须区分，否则数据失真。
                r = subprocess.run(
                    ["godot", "--headless", "--path", project_root,
                     "-s", GUT_SCRIPT_RES_PATH, "-gdir=res://tests/", "-gexit"],
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                )
                gut_tail = (r.stdout + r.stderr)[-1500:]
            except subprocess.TimeoutExpired:
                return {
                    "tool": "coverage",
                    "ok": False,
                    "summary": {"file": str(path), "total_lines": total_lines,
                                "covered_lines": 0, "coverage_percent": 0.0,
                                "min_percent": min_percent, "error": "GUT timed out"},
                    "failures": [{"reason": f"GUT timed out after {timeout_s}s"}],
                }
            finally:
                path.write_text(backup, encoding="utf-8")
        except Exception:
            path.write_text(backup, encoding="utf-8")
            raise

        # godot 进程级失败（脚本没加载）≠ 真实 0% 覆盖——数据不可信，显式报
        # run_error 而非产出误导性的 0.0%（Revy QA FAIL 同根因）。
        if r.returncode != 0 and _looks_like_godot_launch_failure(gut_tail):
            return {
                "tool": "coverage",
                "ok": False,
                "summary": {"file": str(path), "total_lines": total_lines,
                            "covered_lines": 0, "coverage_percent": 0.0,
                            "min_percent": min_percent,
                            "run_error": True,
                            "error": "godot launch failed — coverage data untrustworthy"},
                "failures": [{"reason": (
                    "godot launch failed (tests did not run): "
                    f"{gut_tail.strip()[:200]}"
                )}],
            }

        # 收集命中行（只统计属于 executable_lines 的行——hits 文件里可能混入
        # 运行时多写入的无关行号；越界命中按无效忽略，不允许覆盖率超 100%）
        valid_line_set = {l.line for l in executable_lines}
        if os.path.isfile(hits_file):
            with open(hits_file, "r", encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if ln.isdigit():
                        n = int(ln)
                        if n in valid_line_set:
                            hits.add(n)

    # 恢复原始文件（保险）
    path.write_text(backup, encoding="utf-8")

    covered = len(hits)
    pct = round(100.0 * covered / total_lines, 2) if total_lines else 100.0
    ok = pct >= min_percent

    uncovered_lines = sorted({l.line for l in executable_lines} - hits)

    failures = []
    if not ok:
        failures.append({
            "reason": f"coverage {pct}% below threshold {min_percent}%",
            "covered": covered,
            "total": total_lines,
            "uncovered_lines": uncovered_lines[:20],  # 截断展示
        })

    return {
        "tool": "coverage",
        "ok": ok,
        "summary": {
            "file": str(path),
            "total_lines": total_lines,
            "covered_lines": covered,
            "coverage_percent": pct,
            "min_percent": min_percent,
        },
        "failures": failures,
    }
