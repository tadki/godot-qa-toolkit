"""SEE-1615 §SPEC-PERF2 —— 等价/无意义变异预剪枝（构造性证明规则集）。

设计红线（Owner 已批口径）：
- 只有**构造性语义证明**的规则才可剪；无法证明者一律保守保留。
- 剪枝发生在 stable-id 标定**之后**：pruned 位点保留 stable id（逐点清单
  可查、不隐形），同 scope 内后续位点 id 零位移（diff/resume 机制不变）。
- 开关默认 OFF（新机制 opt-in，env `GQT_MUTATION_EQUIV_PRUNE=1`）；披露键
  `equivalent_pruned`（规则名/剪前剪后分母/逐点清单/节省成本计数），既有
  消费方键零变化。

规则集（全部静态可判定；动态类型风险一律保守排除）：
- EQ_ARITH_ZERO_ADD：``INT + 0`` 的 ``+→−`` 变体 —— n+0 ≡ n−0 ≡ n（Godot
  int 为 64 位有符号，两侧均不溢出；操作数求值次数/顺序不变）。仅两侧均
  为整数字面量时成立：右操作数动态类型不可知，float −0.0 符号位（1.0/x
  可观测）与 String 重复等引擎多态使放宽不安全（负例测试钉住）。
- EQ_ARITH_ZERO_SUB：``INT − 0`` 的 ``−→+`` 变体 —— 同上恒等。
- EQ_ARMSWAP_SYMMETRIC：两臂文本相同的 ternary arm_swap —— 交换等价于
  恒等变换（构造性：程序文本逐字节相同）。

不可证明而保守保留的候选（负例记录，report 逐条引用）：
- ``x * 1`` 的 ``*→/``（String/Array 重复语义 ``s * 1 ≡ s`` 但 ``s / 1``
  运行时错误——动态类型不可证）；``x + 0`` 左操作数非整数字面量（float
  −0.0 符号位可观测）；比较/逻辑/成员/UOI 族（均存在可观测行为差）。
"""

from __future__ import annotations

import os
import re

from .ops import _line_offsets, _token_pos

ENV_EQUIV_PRUNE = "GQT_MUTATION_EQUIV_PRUNE"

RULE_EQ_ZERO_ADD = "EQ_ARITH_ZERO_ADD"
RULE_EQ_ZERO_SUB = "EQ_ARITH_ZERO_SUB"
RULE_EQ_ARMSWAP_SYMMETRIC = "EQ_ARMSWAP_SYMMETRIC"

_RULE_PROOFS = {
    RULE_EQ_ZERO_ADD: (
        "INT n + 0 ≡ INT n − 0 ≡ n（64 位 int 两侧均不溢出；操作数求值"
        "次数/顺序不变；仅两侧均为整数字面量时成立）"),
    RULE_EQ_ZERO_SUB: (
        "INT n − 0 ≡ INT n + 0 ≡ n（同 EQ_ARITH_ZERO_ADD 论证域）"),
    RULE_EQ_ARMSWAP_SYMMETRIC: (
        "两臂文本相同的 ternary 交换后程序文本逐字节相同（恒等变换）"),
}

_INT_LIT_RE = re.compile(r"^\d+$")


def resolve_equiv_prune(flag: bool | None = None) -> bool:
    """等价剪枝开关：显式 flag 最高优先；env 1/true/yes/on 开启；默认 OFF。"""
    if flag is not None:
        return bool(flag)
    return os.environ.get(ENV_EQUIV_PRUNE, "").strip().lower() \
        in ("1", "true", "yes", "on")


def _int_literal(node) -> int | None:
    """NUMBER Token 且值为纯整数字面量（拒绝 float/负号/表达式）→ int 值。"""
    if getattr(node, "type", "") != "NUMBER":
        return None
    text = str(getattr(node, "value", ""))
    if not _INT_LIT_RE.match(text):
        return None
    return int(text)


def find_equivalent(src: str, mutants: list) -> list[dict]:
    """返回可剪位点清单（含 stable_id/规则/证明引用）。

    位置判定：对 src 单次 gdtoolkit 解析，收集 arith_expr/mdr_expr 的
    (算子 Token start_pos → 左右操作数整型字面量性)；与 mutant 的
    (start_pos, original, mutated) 三元组精确匹配。未命中/不确定 → 保守
    保留（不剪）。"""
    from gdtoolkit.parser import parser as gdtoolkit_parser

    tree = gdtoolkit_parser.parse(src)
    offs = _line_offsets(src)
    sites: dict[int, tuple] = {}

    def visit(node) -> None:
        if _is_arith_site(node):
            sites[_site_key(node, offs)] = _site_tuple(node)
        for child in getattr(node, "children", []) or []:
            if hasattr(child, "data") or getattr(child, "children", None):
                visit(child)

    visit(tree)
    return _match_rules(mutants, sites)


def _is_arith_site(node) -> bool:
    data = getattr(node, "data", None)
    return data in ("arith_expr", "mdr_expr") and len(node.children) == 3


def _site_key(node, offs: list[int]) -> int:
    return _token_pos(offs, node.children[1])


def _site_tuple(node) -> tuple:
    op = str(getattr(node.children[1], "value", "") or "")
    return (op, _int_literal(node.children[0]), _int_literal(node.children[2]))


def _match_rules(mutants: list, sites: dict[int, tuple]) -> list[dict]:
    pruned: list[dict] = []
    for m in mutants:
        rule = _match_one(m, sites)
        if rule is not None:
            pruned.append({
                "stable_id": getattr(m, "stable_id", "") or "",
                "rule": rule,
                "proof": _RULE_PROOFS[rule],
                "line": m.line,
                "column": m.column,
                "kind": m.kind,
                "operator": m.operator,
                "original": m.original,
                "mutated": m.mutated,
            })
    return pruned


def _match_one(m, sites: dict[int, tuple]) -> "str | None":
    if m.kind == "TERNARY" and m.operator == "arm_swap":
        a1, a2 = m.original.split("\x00", 1)
        return RULE_EQ_ARMSWAP_SYMMETRIC if a1 == a2 else None
    if m.kind != "AOR":
        return None
    site = sites.get(m.start_pos)
    if site is None:
        return None
    op, left, right = site
    if op == "+" and m.mutated == "-" and left is not None and right == 0:
        return RULE_EQ_ZERO_ADD
    if op == "-" and m.mutated == "+" and left is not None and right == 0:
        return RULE_EQ_ZERO_SUB
    return None


def prune_equivalent(src: str, mutants: list) -> "tuple[list, list[dict]]":
    """(kept, pruned_details)——kept 保序（id 零位移），pruned 含 stable_id。"""
    details = find_equivalent(src, mutants) if mutants else []
    by_id = {p["stable_id"]: p for p in details}
    kept: list = []
    pruned: list[dict] = []
    for m in mutants:
        sid = getattr(m, "stable_id", "") or ""
        if sid in by_id:
            pruned.append(by_id[sid])
        else:
            kept.append(m)
    return kept, pruned
