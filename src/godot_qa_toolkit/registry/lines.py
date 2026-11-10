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

# 可插桩语句形态集合（与 runner 原 `_EXECUTABLE_NODE_NAMES` 逐项一致）
EXECUTABLE_NODE_NAMES = frozenset(
    {
        "if_branch",
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
