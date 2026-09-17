"""Mutation runner: source-level GDScript mutants + kill/survive/timeout verdicts.

SEE-1268 自研（gdtoolkit lark AST 定位 → 源码 span 替换 → 跑 GUT 看该 mutant
是否被杀死）。kill rate 永不进验收证据（owner-order L5 条文）。

P0' 判定原则落点：每个 mutant 输出机器可判的 killed/survived/timeout 布尔 +
结构化 failures；exit 0=全部杀死 1=有存活/超时。

SEE-1312 增强（plan-debate 定案）：
- 变异点扩展：三元表达式（cond 取反 + 两臂交换）、守卫取反（含去重）、
  算子第二梯队（and⇄or / in→not in / is 取反）、UOI token 级删 not（排除 not in）、
  常量 Token 级精确匹配（原 atom 节点在 lark 折叠下永不出现，已死代码——改直查）。
- 定位机制升级为 span 替换（gather_metadata 提供 start_pos/end_pos），
  取代脆弱的 line/column 单点替换。
- 成本控制：--dry-run（只清点不跑 GUT）、--tests 受影响测试子集、
  自适应 timeout（baseline 实测耗时 × k）。
- 有效性分类：invalid_mutant 语法预检、suspect 标签（baseline 脏 ∧ 差集空）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from gdtoolkit.parser import parser as gdtoolkit_parser

# ---------------------------------------------------------------------------
# 变异算子表
# ---------------------------------------------------------------------------

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

# 第二梯队：逻辑/成员/类型（SEE-1312 短板实测：裸条件守卫判别力无机器判据）
_LOGICAL_MUTATIONS = {"and": ["or"], "or": ["and"]}
# 'not in' 的 not_in_op 节点无子 Token（无位置信息）——只支持 in→not in 方向
_MEMBERSHIP_MUTATIONS = {"in": ["not in"]}

# 边界常量翻转（Token 级精确匹配——值相等才算，杜绝 10 被当成 1 的子串误配）
_CONSTANT_MUTATIONS = {
    "0": ["1", "-1"],
    "1": ["0", "-1"],
    "true": ["false"],
    "false": ["true"],
}

# 守卫取反去重：条件子树含这些节点（自带可变异算子）时，守卫取反冗余——跳过。
_MUTABLE_COND_NODES = {
    "arith_expr", "comparison", "asless_comparison",
    "and_test", "asless_and_test", "or_test", "asless_or_test",
    "content_test", "type_test", "asless_actual_not_test",
    "test_expr", "unary_expr",
}


@dataclass(frozen=True)
class Mutant:
    file: str
    line: int
    column: int
    operator: str
    original: str
    mutated: str
    kind: str  # "AOR" | "ROR" | "UOI" | "boundary" | "TERNARY" | "GUARD_NOT" | "LOGICAL" | "MEMBERSHIP" | "IS_NOT"
    start_pos: int = -1  # span 替换起点（gather_metadata 偏移）；-1 = 退回 line/column
    end_pos: int = -1    # span 替换终点（不含）


def _tree_depth_walk(node, callback, depth=0):
    """Breadth/depth-neutral walk over a lark Tree."""
    callback(node, depth)
    for child in getattr(node, "children", []):
        _tree_depth_walk(child, callback, depth + 1)


def _line_offsets(src: str) -> list[int]:
    offs = [0]
    for line in src.splitlines(keepends=True):
        offs.append(offs[-1] + len(line))
    return offs


def _token_pos(src: str, offs: list[int], token) -> int:
    return offs[token.line - 1] + token.column - 1


def _subtree_has_mutable_node(node) -> bool:
    """守卫取反去重：条件子树是否含自带可变异算子的节点。"""
    if hasattr(node, "data") and node.data in _MUTABLE_COND_NODES:
        return True
    return any(_subtree_has_mutable_node(c) for c in getattr(node, "children", []))


def _collect_mutations(src: str, file_path: str) -> list[Mutant]:
    """从 gdtoolkit lark AST（gather_metadata）收集全部变异点。

    span 定位：Tree.meta 提供 start_pos/end_pos（元数据模式），Token 用
    line/column 换算偏移。每 mutant 携带 span，_apply_mutation 做精确替换。
    """
    tree = gdtoolkit_parser.parse(src, gather_metadata=True)
    offs = _line_offsets(src)
    mutants: list[Mutant] = []

    def mk(operator, original, mutated, kind, line, column, start_pos, end_pos):
        mutants.append(Mutant(
            file=file_path, line=line, column=column, operator=operator,
            original=original, mutated=mutated, kind=kind,
            start_pos=start_pos, end_pos=end_pos,
        ))

    def visit(node, depth=0):
        if not hasattr(node, "data"):
            return
        data = node.data

        # --- span 系（Tree.meta）---
        meta = getattr(node, "meta", None)
        if meta is not None and not meta.empty:
            s, e = meta.start_pos, meta.end_pos
            line, col = meta.line, meta.column

            if data == "test_expr":
                # 三元表达式：children = [arm1, 'if', cond, 'else', arm2]
                children = node.children
                if len(children) == 5:
                    if_tok, else_tok = children[1], children[3]
                    cond = children[2]
                    arm1_tok, arm2_tok = children[0], children[4]
                    if_pos = _token_pos(src, offs, if_tok)
                    else_pos = _token_pos(src, offs, else_tok)
                    # cond 取反：not (cond) 包裹 cond span。cond 可能是 Token
                    #（简单名，无 meta）——此时 span 取 if/else 关键字之间。
                    cond_s = cond_e = -1
                    if hasattr(cond, "meta") and not getattr(cond.meta, "empty", True):
                        cond_s, cond_e = cond.meta.start_pos, cond.meta.end_pos
                    else:
                        if_pos_ = _token_pos(src, offs, if_tok)
                        cond_s = if_pos_ + len("if")
                        cond_e = else_pos
                    if cond_s < cond_e:
                        cline, ccol = if_tok.line, if_tok.column
                        mk("cond_not", "if", "if not", "TERNARY", cline, ccol,
                           cond_s, cond_e)
                    # 两臂交换：span1 = [start, if_pos)，span2 = (else_end, end]
                    a1 = src[s:if_pos].strip()
                    a2 = src[else_pos + len("else"):e].strip()
                    if a1 and a2:
                        mk("arm_swap", f"{a1}⇄{a2}", f"{a2}⇄{a1}", "TERNARY",
                           line, col, s, e)

            elif data in ("if_branch", "elif_branch") and node.children:
                # 守卫取反：cond = children[0]（expr Tree）
                cond = node.children[0]
                if hasattr(cond, "meta") and not getattr(cond.meta, "empty", True):
                    if not _subtree_has_mutable_node(cond):
                        cs, ce = cond.meta.start_pos, cond.meta.end_pos
                        mk("guard_not", "if", "if not", "GUARD_NOT",
                           cond.meta.line, cond.meta.column, cs, ce)

            elif data == "while_stmt" and node.children:
                cond = node.children[0]
                if (hasattr(cond, "data") and hasattr(cond, "meta")
                        and not getattr(cond.meta, "empty", True)
                        and not _subtree_has_mutable_node(cond)):
                    cs, ce = cond.meta.start_pos, cond.meta.end_pos
                    mk("guard_not", "while", "while not", "GUARD_NOT",
                       cond.meta.line, cond.meta.column, cs, ce)

            elif data == "type_test":
                # is 取反：not (x is T) 包裹整个 type_test span
                mk("is_negate", "is", "not (is)", "IS_NOT", line, col, s, e)

            elif data in ("and_test", "asless_and_test", "or_test", "asless_or_test"):
                # 逻辑运算符互换：'and'/'or' 是直接子 Token
                for child in node.children:
                    tok = getattr(child, "value", None)
                    if tok in _LOGICAL_MUTATIONS:
                        for m in _LOGICAL_MUTATIONS[tok]:
                            mk(tok, tok, m, "LOGICAL", child.line, child.column,
                               _token_pos(src, offs, child),
                               _token_pos(src, offs, child) + len(tok))

            elif data == "content_test":
                # 成员测试：'in' 是直接子 Token（not in 的 not_in_op 无位置，跳过）
                for child in node.children:
                    tok = getattr(child, "value", None)
                    if tok in _MEMBERSHIP_MUTATIONS:
                        for m in _MEMBERSHIP_MUTATIONS[tok]:
                            mk(tok, tok, m, "MEMBERSHIP", child.line, child.column,
                               _token_pos(src, offs, child),
                               _token_pos(src, offs, child) + len(tok))

        # --- token 系（算术/比较/常量/UOI）---
        if data in ("arith_expr", "comparison", "asless_comparison"):
            table = (_ARITHMETIC_MUTATIONS if "arith" in data else _COMPARISON_MUTATIONS)
            kind = "AOR" if "arith" in data else "ROR"
            for child in node.children:
                tok = getattr(child, "value", None)
                if tok in table:
                    pos = _token_pos(src, offs, child)
                    for m in table[tok]:
                        mk(tok, tok, m, kind, child.line, child.column,
                           pos, pos + len(tok))
        elif data == "asless_actual_not_test" and node.children:
            # UOI 经典删 not：第一个子 Token 是 'not'（值匹配确保不是 not in——
            # 'not in' 走 content_test/not_in_op 路径，不会到这）
            first = node.children[0]
            if getattr(first, "value", None) == "not":
                pos = _token_pos(src, offs, first)
                mk("not", "not", "", "UOI", first.line, first.column,
                   pos, pos + len("not"))

        # 常量 Token 级精确匹配：NUMBER/关键字 NAME Token 值全等才产变异点
        for child in getattr(node, "children", []):
            val = getattr(child, "value", None)
            if (val in _CONSTANT_MUTATIONS
                    and getattr(child, "type", "") in ("NUMBER", "NAME")):
                pos = _token_pos(src, offs, child)
                for m in _CONSTANT_MUTATIONS[val]:
                    mk(val, val, m, "boundary", child.line, child.column,
                       pos, pos + len(val))

    _tree_depth_walk(tree, visit)
    return mutants


def _apply_mutation(src: str, mutant: Mutant) -> str:
    """应用 mutant：优先 span 精确替换（SEE-1312），退化 line/column 单点替换。"""
    if mutant.start_pos >= 0 and mutant.end_pos >= mutant.start_pos:
        if mutant.operator == "cond_not" or mutant.operator == "guard_not":
            # 在 span 前插 'not ('，span 后补 ')'——span 为 cond 全文
            return (
                src[:mutant.start_pos]
                + "not (" + src[mutant.start_pos:mutant.end_pos] + ")"
                + src[mutant.end_pos:]
            )
        if mutant.operator == "is_negate":
            return (
                src[:mutant.start_pos]
                + "not (" + src[mutant.start_pos:mutant.end_pos] + ")"
                + src[mutant.end_pos:]
            )
        if mutant.operator == "arm_swap":
            # span = 整个三元；重排为 arm2 if cond else arm1
            text = src[mutant.start_pos:mutant.end_pos]
            m = re.match(
                r"^(.+?)\bif\b(.+?)\belse\b(.+)$", text, flags=re.DOTALL,
            )
            if not m:
                raise ValueError(
                    f"ternary arm_swap span not re-parseable: {text[:80]!r}"
                )
            arm1, cond, arm2 = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
            return src[:mutant.start_pos] + f"{arm2} if {cond} else {arm1}" + src[mutant.end_pos:]
        # 通用 token 替换（AOR/ROR/LOGICAL/MEMBERSHIP/UOI/boundary）
        return src[:mutant.start_pos] + mutant.mutated + src[mutant.end_pos:]

    # 退化路径（无 span 的 Mutant——外部构造的测试用例）：line/column 定位。
    lines = src.splitlines(keepends=True)
    if mutant.line <= 0 or mutant.line > len(lines):
        raise ValueError(f"mutant line out of range: {mutant.line}")
    target_line = lines[mutant.line - 1]
    col = mutant.column
    original = mutant.original
    if not target_line[col - 1:col - 1 + len(original)] == original:
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


def _run_gut_on_project(project_root: str, timeout_s: int = 60,
                        tests_glob: str | None = None) -> tuple[int, str]:
    """在项目里 headless 跑 GUT；返回 (exit_code, 输出尾部)。

    -s 用 res:// 路径（绝对路径会让 godot 拒绝加载）；脚本存在性仍由调用方
    校验（文件系统检查），加载失败由 _looks_like_godot_launch_failure 识别。
    tests_glob（SPEC-009）：受影响测试子集（如 res://tests/save/），替代全量
    -gdir=res://tests/ 以压缩单轮成本。
    """
    gut_fs = os.path.join(project_root, "addons", "gut", "gut_cmdln.gd")
    if not os.path.isfile(gut_fs):
        raise FileNotFoundError(f"GUT runner not found: {gut_fs}")
    gdir = tests_glob if tests_glob else "res://tests/"
    r = subprocess.run(
        ["godot", "--headless", "--path", project_root,
         "-s", GUT_SCRIPT_RES_PATH, f"-gdir={gdir}", "-gexit"],
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
    ansi = re.compile(r"\x1b\[[0-9;]*m")
    failing: set[str] = set()
    current = None
    for raw in gut_output.splitlines():
        line = ansi.sub("", raw)
        m = re.match(r"^\*\s+(test_\S+)", line)
        if m:
            current = m.group(1)
            continue
        if "[Failed]" in line and current:
            failing.add(current)
    return failing


# 自适应 timeout 倍数：per-mutant 上限 = baseline 实测耗时 × 该倍数。
_TIMEOUT_SCALE = 2.0


def run_mutation(
    file_path: str,
    project_root: str,
    budget: int = 50,
    timeout_s: int = 60,
    dry_run: bool = False,
    tests_glob: str | None = None,
) -> dict:
    """对单个 .gd 文件做变异测试并出统一 JSON 契约。

    budget 控制 mutant 上限（mutation 风暴防御——100 变异 × GUT 全套 = 时间爆炸）。
    kill 判定 = 失败测试名集合相对 baseline 的差集（mutant 引入的新失败），
    而非 exit code——pre-existing failure 不算 killed。
    dry_run（SPEC-009）：只清点变异点不跑 GUT——CI 快闸与成本评估入口。
    tests_glob（SPEC-009）：受影响测试子集，passed to _run_gut_on_project。
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

    if dry_run:
        # SPEC-009：清点模式——零 GUT 调用，mutant 清单即产出。
        kinds: dict[str, int] = {}
        for m in mutants[:budget]:
            kinds[m.kind] = kinds.get(m.kind, 0) + 1
        return {
            "tool": "mutation",
            "ok": True,
            "summary": {"file": str(path), "mutants": len(mutants[:budget]),
                        "dry_run": True, "kinds": kinds},
            "failures": [],
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

    # SPEC-010：invalid_mutant 语法预检——变异产物不合法 = 算子 bug，提前剔除。
    valid_mutants = []
    invalid = []
    for m in mutants:
        try:
            mutated_src = _apply_mutation(original_src, m)
            gdtoolkit_parser.parse(mutated_src)
            valid_mutants.append(m)
        except Exception:
            invalid.append(m)
    mutants = valid_mutants

    results = []
    for m in invalid:
        results.append({"file": m.file, "line": m.line, "kind": m.kind,
                        "original": m.original, "mutated": m.mutated,
                        "verdict": "invalid_mutant",
                        "reason": f"mutated source fails to parse (operator bug): "
                                  f"{m.kind} {m.original}→{m.mutated}"})
    invalid_count = len(invalid)
    killed = survived = timeout_count = run_error_count = suspect_count = 0

    with tempfile.TemporaryDirectory() as workdir:
        # 备份原文件到临时区，逐个变异并跑 GUT。
        backup_path = os.path.join(workdir, "original.gd")
        shutil.copy2(str(path), backup_path)

        # Baseline：原文件的 GUT 结果（失败测试名集合）。pre-existing 失败
        # 不属于任何 mutant——kill 判定只看相对 baseline 的【新增】失败。
        # Revy QA retest 实证：baseline 必然花最久（实测项目 ~108s），默认
        # --timeout 60 下 TimeoutExpired 漏网成原始 traceback（无统一 JSON）——
        # 与 per-mutant 循环的 timeout 同款处理：归 run_error JSON 契约。
        t0 = time.monotonic()
        try:
            baseline_rc, baseline_tail = _run_gut_on_project(
                project_root, timeout_s=timeout_s, tests_glob=tests_glob)
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
        baseline_elapsed = time.monotonic() - t0
        # SPEC-009 自适应 timeout：per-mutant 上限 = baseline 耗时 × k（不降
        # 低于调用方显式 timeout——只放大，不收紧）。
        adaptive_timeout = max(timeout_s, int(baseline_elapsed * _TIMEOUT_SCALE) + 1)

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
        baseline_dirty = bool(baseline_failures)

        for m in mutants:
            mutated_src = _apply_mutation(original_src, m)
            path.write_text(mutated_src, encoding="utf-8")
            try:
                rc, tail = _run_gut_on_project(
                    project_root, timeout_s=adaptive_timeout, tests_glob=tests_glob)
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
                        # SPEC-010 suspect 标签：baseline 脏 ∧ 差集空——无法区分
                        # 「测试没抓住 mutant」与「测试根本没跑到该区域」，保守
                        # 标记（不计 killed 也不计 survived）。
                        if baseline_dirty:
                            suspect_count += 1
                            results.append({"file": m.file, "line": m.line, "kind": m.kind,
                                            "original": m.original, "mutated": m.mutated,
                                            "verdict": "suspect",
                                            "reason": (f"dirty baseline ∧ empty diff after {m.kind} "
                                                       f"{m.original}→{m.mutated} — cannot distinguish "
                                                       f"survive from unexercised")})
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

    total = killed + survived + timeout_count + run_error_count + invalid_count + suspect_count
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
            "suspect": suspect_count,
            "timeout": timeout_count,
            "run_errors": run_error_count,
            "invalid_mutants": invalid_count,
            "kill_rate": round(kill_rate, 4),
            "budget": budget,
            "adaptive_timeout_s": adaptive_timeout,
        },
        "failures": failures,
    }
