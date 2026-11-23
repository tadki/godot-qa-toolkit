"""Instrumentation target-line collection for probe-manifest/2 (§SPEC-029).

Reverse-publicized from coverage/runner.py (SEE-1546 WP2 PR-B): the
instrumentation target line set *is* the I-side definition of probe-manifest/2,
so it belongs in the neutral registry layer. coverage/runner now imports these
public names — the measurement layer depends on the base layer, matching the
WP1b layering. Names lose the leading underscore; semantics are unchanged
(executable statement starts only, paren-depth 0, class_def bodies pruned).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from gdtoolkit.parser import parser as gdtoolkit_parser
from lark import Tree

# 可插桩语句形态集合（SEE-1623 H3：elif_branch 条件行纳入——真实会被求值
# 的行此前被标 uncovered 并被 mutation prefilter 跳过；else_branch 仍排除，
# 其头行无求值语义且无变异位点）
EXECUTABLE_NODE_NAMES = frozenset(
    {
        "if_branch",
        "elif_branch",
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
)

IF_BRANCH_NODES = ("if_branch", "elif_branch", "else_branch")


@dataclass(frozen=True)
class ExecutableLine:
    line: int
    node: str


def tree_depth_walk(node, callback, depth=0, prune=None):
    """Depth-first walk with an optional prune predicate (class_def bodies)."""
    if prune is not None and prune(node):
        return
    callback(node, depth)
    for child in getattr(node, "children", []):
        if hasattr(child, "data"):
            tree_depth_walk(child, callback, depth + 1, prune)


def node_line(node) -> int | None:
    meta = getattr(node, "meta", None)
    if meta is None or meta.empty:
        return None
    return getattr(meta, "line", None)


def paren_depths(src: str) -> list[int]:
    depths: list[int] = []
    depth = 0
    for line in src.splitlines():
        depths.append(depth)
        cleaned = re.sub(r'"(?:[^"\\]|\\.)*"', '""', line)
        cleaned = re.sub(r"'(?:[^'\\]|\\.)*'", "''", line)
        cleaned = re.sub(r"#.*", "", cleaned)
        depth += (cleaned.count("(") + cleaned.count("[") + cleaned.count("{")
                  - cleaned.count(")") - cleaned.count("]") - cleaned.count("}"))
    return depths


def dedupe_sorted(lines: list[ExecutableLine]) -> list[ExecutableLine]:
    by_line: dict[int, ExecutableLine] = {}
    for entry in lines:
        if entry.line not in by_line:
            by_line[entry.line] = entry
    return [by_line[l] for l in sorted(by_line)]


def collect_executable_lines(src: str) -> list[ExecutableLine]:
    """用 gdtoolkit lark（gather_metadata）找出所有可执行语句的【起始行】。

    语句跨多行时只取节点起始行——探针插在语句前，绝不落进表达式内部
    （SEE-1308 实测：探针插进跨行分组表达式内部 → Parse Error）。
    """
    tree = gdtoolkit_parser.parse(src, gather_metadata=True)
    lines: list[ExecutableLine] = []
    depths = paren_depths(src)

    def visit(node, depth=0):
        if not hasattr(node, "data"):
            return
        if node.data in EXECUTABLE_NODE_NAMES:
            ln = node_line(node)
            if ln and ln <= len(depths) and depths[ln - 1] == 0:
                lines.append(ExecutableLine(line=ln, node=node.data))
            # 括号内部行（depths>0）不是语句起始位置——即使 AST 报告该行为
            # 语句行（如跨行分组表达式 / lambda 实参行），也绝不插桩。

    # 内部类（class_def）整体不插桩（SEE-1367 §SPEC-017 硬化 反例实证）：
    # 探针函数追加在外层类作用域，内部类方法的 self 看不到它们——
    # Godot Parse Error "Function __qa_cov_probe_N() not found in base self"，
    # 目标文件整体拒载（GUT 侧表现为 "does not extend GutTest"），零探针假象。
    # 代价：内部类体（通常为测试桩）不计量——在套件自测量场景可接受。
    tree_depth_walk(tree, visit, prune=lambda n: getattr(n, "data", None) == "class_def")
    return dedupe_sorted(lines)


def elif_head_anchors(src: str) -> dict[int, int]:
    """SEE-1623 H3：elif 头行 → 分支体首语句行（探针锚定发射点）。

    elif 条件行进入可插桩行集，但 if 链内其前无合法插桩位置（同缩进语句
    会破坏 if/elif/else 链——硬化反例：探针插头行前 → Parse Error）。头行
    探针改在【分支体首语句】的插入点一并发射：体首执行 ⟺ 条件为真，与
    if_branch 头行「求值即点火」的差异（false 路径求值不计）在
    docs/contract.md 披露。体首不可得（class_def 剪枝等）→ 不入映射——
    头行保持未覆盖，与既有行为一致，不虚构锚点。"""
    tree = gdtoolkit_parser.parse(src, gather_metadata=True)
    anchors: dict[int, int] = {}

    def visit(node, depth=0):
        if getattr(node, "data", None) == "elif_branch":
            head = node_line(node)
            body_first = None
            children = [c for c in getattr(node, "children", [])
                        if hasattr(c, "data")]
            stack = children[1:]
            while stack:
                cur = stack.pop(0)
                if getattr(cur, "data", None) == "class_def":
                    continue
                ln = node_line(cur)
                if ln is not None:
                    body_first = (ln if body_first is None
                                  else min(body_first, ln))
                stack.extend(c for c in getattr(cur, "children", [])
                             if hasattr(c, "data"))
            if head is not None and body_first is not None \
                    and body_first != head:
                anchors[head] = body_first

    tree_depth_walk(tree, visit,
                    prune=lambda n: getattr(n, "data", None) == "class_def")
    return anchors
