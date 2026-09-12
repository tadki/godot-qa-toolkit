"""Mutation runner: source-level GDScript mutants + kill/survive/timeout verdicts.

M2 简单一步到位（SEE-1256 自研，解耦可扩展）。算子集按经典 mutation
testing 最小完备集：AOR（算术运算符）、ROR（关系运算符）、UOI（一元取反）、
边界常量（0/1/-1/True/False 翻转）。用 gdtoolkit 的 lark parser 精确定位
节点 → 用 Python AST/gd2py 做局部字符串替换 → 写回原文件 → 跑 GUT 看该 mutant
是否被杀死。kill rate 永不进验收证据（owner-order L5 条文）。

P0' 判定原则落点：每个 mutant 输出机器可判的 killed/survived/timeout 布尔 +
结构化 failures；exit 0=全部杀死 1=有存活/超时。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from gdtoolkit.parser import parser as gdtoolkit_parser

# AOR: 算术运算符互换（两两排列足够——加法/减法/乘法/除法）
_ARITHMETIC_MUTATIONS = {
    "+": ["-", "*", "/"],
    "-": ["+", "*", "/"],
    "*": ["+", "-", "/"],
    "/": ["+", "-", "*"],
}

# ROR: 关系运算符互换（保语义对称性——等价类中最常见的陷阱是边界）
_COMPARISON_MUTATIONS = {
    "==": ["!=", "<=", ">="],
    "!=": ["==", "<", ">"],
    "<": ["<=", "==", ">="],
    "<=": ["<", "==", ">"],
    ">": [">=", "==", "<="],
    ">=": [">", "==", "<"],
}

# UOI: 一元取反去掉 not / 负号
_UNARY_MUTATIONS = {
    "not ": ["not (false) and "],  # 等价取反的激进替换（替代 not x 的简单去掉）
}

# 边界常量翻转（字面量级，risk 低）
_CONSTANT_MUTATIONS = {
    "0": ["1", "-1"],
    "1": ["0", "-1"],
    "true": ["false"],
    "false": ["true"],
}


@dataclass(frozen=True)
class Mutant:
    file: str
    line: int
    column: int
    operator: str
    original: str
    mutated: str
    kind: str  # "AOR" | "ROR" | "UOI" | "boundary"


def _tree_depth_walk(node, callback, depth=0):
    """Breadth/depth-neutral walk over a lark Tree."""
    callback(node, depth)
    for child in getattr(node, "children", []):
        _tree_depth_walk(child, callback, depth + 1)


def _collect_mutations(src: str, file_path: str) -> list[Mutant]:
    """从 gdtoolkit lark AST 收集所有可替换的运算符/常量节点为 Mutant 列表。

    lark 的 Tree 节点不传播 line/column（只有 Token 有）——所以定位取操作符
    Token 自身的位置，而非其父节点。
    """
    tree = gdtoolkit_parser.parse(src)
    mutants: list[Mutant] = []

    def visit(node, depth=0):
        if depth > 8:
            return
        if not hasattr(node, "data"):
            return

        kind = None
        candidates: list[str] = []
        token = None
        original = ""

        if node.data == "arith_expr":
            for child in node.children:
                tok = getattr(child, "value", None)
                if tok in _ARITHMETIC_MUTATIONS:
                    candidates = _ARITHMETIC_MUTATIONS[tok]
                    kind = "AOR"
                    original = tok
                    token = child
                    break
        elif node.data == "comparison":
            for child in node.children:
                tok = getattr(child, "value", None)
                if tok in _COMPARISON_MUTATIONS:
                    candidates = _COMPARISON_MUTATIONS[tok]
                    kind = "ROR"
                    original = tok
                    token = child
                    break
        elif node.data == "unary_expr":
            if node.children and getattr(node.children[0], "value", None) == "not":
                candidates = [""]
                kind = "UOI"
                original = "not"
                token = node.children[0]
        elif node.data == "atom":
            if len(node.children) == 1:
                tok = getattr(node.children[0], "value", None)
                if tok in _CONSTANT_MUTATIONS:
                    candidates = _CONSTANT_MUTATIONS[tok]
                    kind = "boundary"
                    original = tok
                    token = node.children[0]

        if kind is None or not candidates or token is None:
            return

        line = getattr(token, "line", 0)
        column = getattr(token, "column", 0)
        for mutated in candidates:
            mutants.append(Mutant(
                file=file_path,
                line=line,
                column=column,
                operator=original,
                original=original,
                mutated=mutated,
                kind=kind,
            ))

    _tree_depth_walk(tree, visit)
    return mutants


def _apply_mutation(src: str, mutant: Mutant) -> str:
    """将 mutant 应用回源码（精确到 line/column 的替换，而非全文替换）。"""
    lines = src.splitlines(keepends=True)
    if mutant.line <= 0 or mutant.line > len(lines):
        raise ValueError(f"mutant line out of range: {mutant.line}")

    target_line = lines[mutant.line - 1]
    col = mutant.column
    original = mutant.original

    # 边界检查：定位应落在原始运算符/字面量上。
    if not target_line[col - 1:col - 1 + len(original)] == original:
        # Fallback: 在该行内找第一个匹配（容忍 1 列误差）。
        idx = target_line.find(original)
        if idx < 0:
            raise ValueError(f"cannot locate '{original}' at {mutant.file}:{mutant.line}")
        col = idx + 1

    lines[mutant.line - 1] = (
        target_line[:col - 1] + mutant.mutated + target_line[col - 1 + len(original):]
    )
    return "".join(lines)


# GUT 的命令行入口必须以 res:// 形式传给 `godot -s`：Godot 对绝对路径的 -s
# 一律报 "Attempt to open script ... File not found"（Revy QA FAIL 实证），
# 文件真实存在也一样——res:// 是唯一可靠形式。
GUT_SCRIPT_RES_PATH = "res://addons/gut/gut_cmdln.gd"

# godot 进程级失败的特征（与"测试跑过但失败"截然不同——绝不能计入 kill）。
_GODOT_LAUNCH_ERROR_MARKERS = (
    "attempt to open script",
    "file not found",
    "failed loading resource",
    "error: failed to load script",
)


def _looks_like_godot_launch_failure(output: str) -> bool:
    low = output.lower()
    return any(marker in low for marker in _GODOT_LAUNCH_ERROR_MARKERS)


def _run_gut_on_project(project_root: str, timeout_s: int = 60) -> tuple[int, str]:
    """在项目里 headless 跑 GUT 全部测试；返回 (exit_code, 输出尾部)。

    -s 用 res:// 路径（绝对路径会让 godot 拒绝加载）；脚本存在性仍由调用方
    校验（文件系统检查），加载失败由 _looks_like_godot_launch_failure 识别。
    """
    gut_fs = os.path.join(project_root, "addons", "gut", "gut_cmdln.gd")
    if not os.path.isfile(gut_fs):
        raise FileNotFoundError(f"GUT runner not found: {gut_fs}")
    r = subprocess.run(
        ["godot", "--headless", "--path", project_root,
         "-s", GUT_SCRIPT_RES_PATH, "-gdir=res://tests/", "-gexit"],
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    tail = r.stdout[-2000:] if r.stdout else r.stderr[-1000:]
    return r.returncode, tail


def _parse_failing_tests(gut_output: str) -> set[str]:
    """从 GUT 输出解析失败测试名集合。

    GUT 结构：'* test_xxx' 开启一个测试，其后 [Failed] 行归属它；汇总行
    '---- N failing tests ----'。用测试名集合做 kill 判定——exit code 只能
    说明"有没有失败"，无法区分 pre-existing 失败与 mutant 引入的新失败
    （被测项目基线在无头环境就可能有 pre-existing failures，Revy QA 实证）。
    """
    import re as _re
    ansi = _re.compile(r"\x1b\[[0-9;]*m")
    failing: set[str] = set()
    current = None
    for raw in gut_output.splitlines():
        line = ansi.sub("", raw)
        m = _re.match(r"^\*\s+(test_\S+)", line)
        if m:
            current = m.group(1)
            continue
        if "[Failed]" in line and current:
            failing.add(current)
    return failing


def run_mutation(
    file_path: str,
    project_root: str,
    budget: int = 50,
    timeout_s: int = 60,
) -> dict:
    """对单个 .gd 文件做变异测试并出统一 JSON 契约。

    budget 控制 mutant 上限（mutation 风暴防御——100 变异 × GUT 全套 = 时间爆炸）。
    kill 判定 = 失败测试名集合相对 baseline 的差集（mutant 引入的新失败），
    而非 exit code——pre-existing failure 不算 killed。
    返回 {"tool":"mutation", "ok", "summary", "failures"}；ok=True 表示
    所有 mutant 均被测试杀死。
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"mutation target not found: {file_path}")

    original_src = path.read_text(encoding="utf-8")
    backup = original_src  # 永不改写原文件之外的任何东西（kill rate 证据隔离）

    try:
        mutants = _collect_mutations(original_src, str(path))
    except Exception as e:
        return {
            "tool": "mutation",
            "ok": False,
            "summary": {"file": str(path), "mutants": 0, "error": str(e)},
            "failures": [{"reason": f"mutation setup failed: {e}"}],
        }

    if not mutants:
        return {
            "tool": "mutation",
            "ok": True,
            "summary": {"file": str(path), "mutants": 0, "killed": 0, "survived": 0,
                        "timeout": 0, "kill_rate": 0.0, "note": "no mutation sites"},
            "failures": [],
        }

    mutants = mutants[:budget]
    results = []
    killed = survived = timeout_count = run_error_count = 0

    with tempfile.TemporaryDirectory() as workdir:
        # 备份原文件到临时区，逐个变异并跑 GUT。
        backup_path = os.path.join(workdir, "original.gd")
        shutil.copy2(str(path), backup_path)

        # Baseline：原文件的 GUT 结果（失败测试名集合）。pre-existing 失败
        # 不属于任何 mutant——kill 判定只看相对 baseline 的【新增】失败。
        # Revy QA retest 实证：baseline 必然花最久（实测项目 ~108s），默认
        # --timeout 60 下 TimeoutExpired 漏网成原始 traceback（无统一 JSON）——
        # 与 per-mutant 循环的 timeout 同款处理：归 run_error JSON 契约。
        try:
            baseline_rc, baseline_tail = _run_gut_on_project(project_root, timeout_s=timeout_s)
        except subprocess.TimeoutExpired:
            return {
                "tool": "mutation",
                "ok": False,
                "summary": {"file": str(path), "run_error": True,
                            "error": f"baseline GUT timed out after {timeout_s}s — mutation data untrustworthy"},
                "failures": [{"reason": (
                    f"baseline GUT timed out after {timeout_s}s (project's baseline run exceeds "
                    f"the timeout — raise --timeout or check why tests are this slow)"
                )}],
            }
        if baseline_rc != 0 and _looks_like_godot_launch_failure(baseline_tail):
            return {
                "tool": "mutation",
                "ok": False,
                "summary": {"file": str(path), "run_error": True,
                            "error": "godot launch failed on baseline — mutation data untrustworthy"},
                "failures": [{"reason": (
                    f"godot launch failed on baseline run: {baseline_tail.strip()[:200]}"
                )}],
            }
        baseline_failures = _parse_failing_tests(baseline_tail)

        for m in mutants:
            mutated_src = _apply_mutation(original_src, m)
            path.write_text(mutated_src, encoding="utf-8")
            try:
                rc, tail = _run_gut_on_project(project_root, timeout_s=timeout_s)
                if rc != 0 and _looks_like_godot_launch_failure(tail):
                    # godot 进程级失败（脚本没加载起来）≠ 测试抓到 mutant——
                    # Revy QA FAIL 实证：绝对 -s 路径恒 rc=1 被误判 killed。
                    # 记 run_error，kill rate 数据失真时宁可报错不报通过。
                    run_error_count += 1
                    results.append({"file": m.file, "line": m.line, "kind": m.kind,
                                    "original": m.original, "mutated": m.mutated,
                                    "verdict": "run_error",
                                    "reason": f"godot launch failed (tests did not run): {tail.strip()[:200]}"})
                elif rc == 0:
                    # 测试通过 = mutant 存活（测试没抓住它）→ survived
                    survived += 1
                    results.append({"file": m.file, "line": m.line, "kind": m.kind,
                                    "original": m.original, "mutated": m.mutated,
                                    "verdict": "survived",
                                    "reason": f"tests passed after {m.kind} {m.original}→{m.mutated}"})
                else:
                    # 测试失败——区分 pre-existing（基线就有）与 mutant 引入的
                    # 新失败：只有新失败才算 killed（Revy QA 第二层假阳性实证：
                    # 某些项目基线无头环境 rc=1 是常态，按 rc 判定则全部误杀）。
                    new_failures = _parse_failing_tests(tail) - baseline_failures
                    if new_failures:
                        killed += 1
                        results.append({"file": m.file, "line": m.line, "kind": m.kind,
                                        "original": m.original, "mutated": m.mutated,
                                        "verdict": "killed",
                                        "reason": (f"new failures after {m.kind} {m.original}→{m.mutated}: "
                                                   f"{sorted(new_failures)[:3]}")})
                    else:
                        survived += 1
                        results.append({"file": m.file, "line": m.line, "kind": m.kind,
                                        "original": m.original, "mutated": m.mutated,
                                        "verdict": "survived",
                                        "reason": (f"only pre-existing failures after {m.kind} "
                                                   f"{m.original}→{m.mutated} — not a kill")})
            except subprocess.TimeoutExpired:
                timeout_count += 1
                results.append({"file": m.file, "line": m.line, "kind": m.kind,
                                "original": m.original, "mutated": m.mutated,
                                "verdict": "timeout",
                                "reason": f"GUT timed out after {m.kind} {m.original}→{m.mutated}"})
            finally:
                # 立即恢复，保证下一个是基于原始代码变异。
                path.write_text(backup, encoding="utf-8")

    # 恢复原始文件（保险——最终态必须与原状一致）。
    path.write_text(backup, encoding="utf-8")

    total = killed + survived + timeout_count + run_error_count
    kill_rate = killed / total if total else 0.0
    failures = [r for r in results if r["verdict"] != "killed"]

    return {
        "tool": "mutation",
        "ok": survived == 0 and timeout_count == 0 and run_error_count == 0,
        "summary": {
            "file": str(path),
            "mutants": total,
            "killed": killed,
            "survived": survived,
            "timeout": timeout_count,
            "run_errors": run_error_count,
            "kill_rate": round(kill_rate, 4),
            "budget": budget,
        },
        "failures": failures,
    }
