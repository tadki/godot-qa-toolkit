"""SEE-1268/1312 变异算子机制（gdtoolkit lark AST 定位 → span 替换）。

从 runner 拆出以守住单文件 < 800 行纪律（owner-order §6.1）；runner 域内
通过 re-export 保持既有 import / monkeypatch 面不再变动。
"""

from __future__ import annotations

import re
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


def _token_pos(offs: list[int], token) -> int:
    return offs[token.line - 1] + token.column - 1


def _subtree_has_mutable_node(node) -> bool:
    """守卫取反去重：条件子树是否含自带可变异算子的节点。"""
    if hasattr(node, "data") and node.data in _MUTABLE_COND_NODES:
        return True
    return any(_subtree_has_mutable_node(c) for c in getattr(node, "children", []))


def _cond_span(src: str, offs: list[int], cond, if_tok, else_pos: int) -> tuple[int, int]:
    """三元条件 span：cond 可能是 Token（简单名，无 meta）——此时取 if/else 之间。"""
    meta = getattr(cond, "meta", None)
    if meta is not None and not meta.empty:
        return meta.start_pos, meta.end_pos
    return _token_pos(offs, if_tok) + len("if"), else_pos


def _span_token_ops(src: str, offs: list[int], node, table: dict, kind: str) -> list[Mutant]:
    """对 node 的直接子 Token 做表驱动替换（LOGICAL/MEMBERSHIP 共用形态）。"""
    mutants = []
    for child in node.children:
        tok = getattr(child, "value", None)
        if tok not in table:
            continue
        pos = _token_pos(offs, child)
        for m in table[tok]:
            mutants.append(Mutant(
                file="", line=child.line, column=child.column, operator=tok,
                original=tok, mutated=m, kind=kind, start_pos=pos, end_pos=pos + len(tok),
            ))
    return mutants


def _collect_mutations(src: str, file_path: str) -> list[Mutant]:
    """从 gdtoolkit lark AST（gather_metadata）收集全部变异点。

    span 定位：Tree.meta 提供 start_pos/end_pos（元数据模式），Token 用
    line/column 换算偏移。每 mutant 携带 span，_apply_mutation 做精确替换。
    """
    tree = gdtoolkit_parser.parse(src, gather_metadata=True)
    offs = _line_offsets(src)
    mutants: list[Mutant] = []

    def visit(node, depth=0):
        if not hasattr(node, "data"):
            return
        data = node.data
        mutants.extend(_span_mutants_for(src, offs, node, data))
        mutants.extend(_token_mutants_for(src, offs, node, data))

    _tree_depth_walk(tree, visit)
    # 填充 file 字段（_span/_token_mutants_for 无法感知 file_path）
    return [
        Mutant(file=file_path, line=m.line, column=m.column, operator=m.operator,
               original=m.original, mutated=m.mutated, kind=m.kind,
               start_pos=m.start_pos, end_pos=m.end_pos)
        for m in mutants
    ]


def _span_mutants_for(src: str, offs: list[int], node, data: str) -> list[Mutant]:
    """span 系变异点（依赖 Tree.meta 的 start/end_pos）。"""
    meta = getattr(node, "meta", None)
    if meta is None or meta.empty:
        return []
    s, e = meta.start_pos, meta.end_pos
    line, col = meta.line, meta.column
    out: list[Mutant] = []

    def mk(operator, original, mutated, kind, ln, cl, sp, ep):
        out.append(Mutant(file="", line=ln, column=cl, operator=operator,
                          original=original, mutated=mutated, kind=kind,
                          start_pos=sp, end_pos=ep))

    if data == "test_expr":
        out.extend(_ternary_mutants(src, offs, node, mk))
    elif data in ("if_branch", "elif_branch") and node.children:
        mk_guard_not(mk, node.children[0], "if", "if not")
    elif data == "while_stmt" and node.children:
        cond = node.children[0]
        if hasattr(cond, "data"):
            mk_guard_not(mk, cond, "while", "while not")
    elif data == "type_test":
        # is 取反：not (x is T) 包裹整个 type_test span
        mk("is_negate", "is", "not (is)", "IS_NOT", line, col, s, e)
    elif data in ("and_test", "asless_and_test", "or_test", "asless_or_test"):
        out.extend(_span_token_ops(src, offs, node, _LOGICAL_MUTATIONS, "LOGICAL"))
    elif data == "content_test":
        # 成员测试：'in' 是直接子 Token（not in 的 not_in_op 无位置，跳过）
        out.extend(_span_token_ops(src, offs, node, _MEMBERSHIP_MUTATIONS, "MEMBERSHIP"))

    return out


def _ternary_mutants(src: str, offs: list[int], node, mk) -> list[Mutant]:
    """三元表达式（children = [arm1, 'if', cond, 'else', arm2]）：cond 取反 + 两臂交换。"""
    out: list[Mutant] = []
    children = node.children
    if len(children) != 5:
        return out
    if_tok, else_tok = children[1], children[3]
    cond = children[2]
    meta = node.meta
    s, e = meta.start_pos, meta.end_pos
    if_pos = _token_pos(offs, if_tok)
    else_pos = _token_pos(offs, else_tok)
    cond_s, cond_e = _cond_span(src, offs, cond, if_tok, else_pos)
    if cond_s < cond_e:
        mk("cond_not", "if", "if not", "TERNARY", if_tok.line, if_tok.column,
           cond_s, cond_e)
    # 两臂交换：span = 整个三元；arm 边界由 collect 端 AST 锚点算出并以
    # 'arm1|arm2' 形式编码进 original（apply 端零猜测——arm 含 ' if '/' else '
    # 字符串字面量时 regex 会误切，硬ener 反例实证）。
    a1 = src[s:if_pos].strip()
    a2 = src[else_pos + len("else"):e].strip()
    if a1 and a2:
        mk("arm_swap", f"{a1}\x00{a2}", f"{a2}\x00{a1}", "TERNARY",
           meta.line, meta.column, s, e)
    return out


def mk_guard_not(mk, cond, original: str, mutated: str) -> None:
    """守卫取反：cond 不含自带可变异算子时产 mutant（含则冗余跳过）。"""
    if not (hasattr(cond, "meta") and not getattr(cond.meta, "empty", True)):
        return
    if _subtree_has_mutable_node(cond):
        return
    mk("guard_not", original, mutated, "GUARD_NOT",
       cond.meta.line, cond.meta.column, cond.meta.start_pos, cond.meta.end_pos)


def _token_mutants_for(src: str, offs: list[int], node, data: str) -> list[Mutant]:
    """token 系变异点（算术/比较/常量/UOI——直接子 Token 值匹配）。"""
    out: list[Mutant] = []

    def mk(operator, original, mutated, kind, ln, cl, sp, ep):
        out.append(Mutant(file="", line=ln, column=cl, operator=operator,
                          original=original, mutated=mutated, kind=kind,
                          start_pos=sp, end_pos=ep))

    if data in ("arith_expr", "comparison", "asless_comparison"):
        table = (_ARITHMETIC_MUTATIONS if "arith" in data else _COMPARISON_MUTATIONS)
        kind = "AOR" if "arith" in data else "ROR"
        for child in node.children:
            tok = getattr(child, "value", None)
            if tok not in table:
                continue
            pos = _token_pos(offs, child)
            for m in table[tok]:
                mk(tok, tok, m, kind, child.line, child.column, pos, pos + len(tok))
    elif data == "asless_actual_not_test" and node.children:
        # UOI 经典删 not：第一个子 Token 是 'not'（值匹配确保不是 not in——
        # 'not in' 走 content_test/not_in_op 路径，不会到这）
        first = node.children[0]
        if getattr(first, "value", None) == "not":
            pos = _token_pos(offs, first)
            mk("not", "not", "", "UOI", first.line, first.column, pos, pos + len("not"))

    # 常量 Token 级精确匹配：NUMBER/关键字 NAME Token 值全等才产变异点
    for child in getattr(node, "children", []):
        val = getattr(child, "value", None)
        if (val in _CONSTANT_MUTATIONS
                and getattr(child, "type", "") in ("NUMBER", "NAME")):
            pos = _token_pos(offs, child)
            for m in _CONSTANT_MUTATIONS[val]:
                mk(val, val, m, "boundary", child.line, child.column,
                   pos, pos + len(val))
    return out


# wrap-not（not包裹类 mutant 的 apply 形态）：cond_not / guard_not / is_negate 同构
_WRAP_NOT_OPERATORS = ("cond_not", "guard_not", "is_negate")


def _wrap_not_span(src: str, mutant: Mutant) -> str:
    return (
        src[:mutant.start_pos]
        + "not (" + src[mutant.start_pos:mutant.end_pos] + ")"
        + src[mutant.end_pos:]
    )


def _apply_mutation(src: str, mutant: Mutant) -> str:
    """应用 mutant：优先 span 精确替换（SEE-1312），退化 line/column 单点替换。"""
    if mutant.start_pos >= 0 and mutant.end_pos >= mutant.start_pos:
        if mutant.operator in _WRAP_NOT_OPERATORS:
            # 在 span 前插 'not ('，span 后补 ')'——span 为 cond/type_test 全文
            return _wrap_not_span(src, mutant)
        if mutant.operator == "arm_swap":
            return _apply_arm_swap(src, mutant)
        # 通用 token 替换（AOR/ROR/LOGICAL/MEMBERSHIP/UOI/boundary）
        return src[:mutant.start_pos] + mutant.mutated + src[mutant.end_pos:]

    return _apply_mutation_fallback(src, mutant)


def _apply_arm_swap(src: str, mutant: Mutant) -> str:
    # span = 整个三元；arm 边界已由 collect 端以 '\x00' 编码在 original 中
    # （arm 文本可能含 ' if '/' else ' 字面量——regex 切分会误伤，改精确拼接）。
    # cond 文本从 span 内两锚点之间截取：cond 恒在 arm1 与 'else' 之间，用
    # 'if' 关键字在 span 内【最后】出现位置之后到 'else' 关键字【最后】出现
    # 位置之前——但字面量同样可含关键字，故 cond 一并编码：original 为
    # 'arm1\x00arm2'，cond 由 span 文本去掉两臂后剩余段恢复。
    arm1, arm2 = mutant.original.split("\x00", 1)
    text = src[mutant.start_pos:mutant.end_pos]
    a1 = text.find(arm1)
    a2 = text.rfind(arm2)
    if a1 < 0 or a2 < 0:
        raise ValueError(f"ternary arm_swap anchors missing in span: {text[:80]!r}")
    cond = text[a1 + len(arm1):a2].strip()
    # 去 cond 与两臂之间的 'if'/'else' 关键字（collect 端 anchor 含它们）
    for kw in ("if", "else"):
        if cond.startswith(kw):
            cond = cond[len(kw):].strip()
        if cond.endswith(kw):
            cond = cond[:-len(kw)].strip()
    return src[:mutant.start_pos] + f"{arm2} if {cond} else {arm1}" + src[mutant.end_pos:]


def _apply_mutation_fallback(src: str, mutant: Mutant) -> str:
    """退化路径（无 span 的 Mutant——外部构造的测试用例）：line/column 定位。"""
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



def _split_invalid_mutants(src: str, mutants: list["Mutant"]) -> tuple[list["Mutant"], list["Mutant"]]:
    """SPEC-010：invalid_mutant 语法预检——变异产物不合法 = 算子 bug，提前剔除。"""
    valid: list[Mutant] = []
    invalid: list[Mutant] = []
    for m in mutants:
        try:
            gdtoolkit_parser.parse(_apply_mutation(src, m))
            valid.append(m)
        except Exception:
            invalid.append(m)
    return valid, invalid
