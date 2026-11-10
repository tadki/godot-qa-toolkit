"""SEE-1550 §SPEC-014 —— mdr_expr（`*`/`/`/`%`）扫描面补全单测。

gdtoolkit 将乘/除/模解析为 mdr_expr 节点（STAR/SLASH/PERCENT Token）——
旧扫描端只访问 arith_expr/comparison 族，三方向全暗（Harti 实证 0 位点）。
本文件钉死三方向位点非零 + 方向表正确 + 混合表达式双算子并收。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation.ops import (
    _collect_mutations,
    _apply_mutation,
    OPS_VERSION,
)


def _collect(src: str) -> list:
    return _collect_mutations(src, "x.gd")


def _aor(src: str, op: str) -> list:
    return [m for m in _collect(src)
            if m.kind == "AOR" and m.original == op]


class TestMdrExprSitesNonZero:
    """§SPEC-014 判据：`a*b`/`a/b`/`a%b` 位点非零（此前 0/0/0）。"""

    def test_mul_sites_nonzero_and_directions(self):
        ms = _aor("func f(a, b):\n\treturn a * b\n", "*")
        assert len(ms) == 3
        assert sorted(m.mutated for m in ms) == ["+", "-", "/"]

    def test_div_sites_nonzero_and_directions(self):
        ms = _aor("func f(a, b):\n\treturn a / b\n", "/")
        assert len(ms) == 3
        assert {m.mutated for m in ms} == {"+", "-", "*"}

    def test_mod_sites_nonzero_and_directions(self):
        """`%` 算子为 §SPEC-014 新入表（此前连键都不存在）。"""
        ms = _aor("func f(a, b):\n\treturn a % b\n", "%")
        assert len(ms) == 3
        assert {m.mutated for m in ms} == {"+", "-", "*"}

    def test_mixed_expr_collects_both_mdr_and_arith(self):
        """`a * b + c`：mdr_expr 是 arith_expr 的子节点——`*` 与 `+` 两侧
        位点都必须在（修复前只收 `+`）。"""
        src = "func f(a, b, c):\n\treturn a * b + c\n"
        ms = _collect(src)
        ops = sorted(m.original for m in ms if m.kind == "AOR")
        assert ops == ["+", "*", "*", "*", "*", "+"] or \
            ops.count("*") == 3 and ops.count("+") == 3, ops
        assert {m.original for m in ms if m.kind == "AOR"} == {"+", "*"}


class TestMdrMutationApplication:
    def test_apply_mul_to_div_changes_source(self):
        src = "func f(a, b):\n\treturn a * b\n"
        m = next(m for m in _aor(src, "*") if m.mutated == "/")
        out = _apply_mutation(src, m)
        assert "return a / b" in out

    def test_apply_mod_to_sub_changes_source(self):
        src = "func f(a, b):\n\treturn a % b\n"
        m = next(m for m in _aor(src, "%") if m.mutated == "-")
        out = _apply_mutation(src, m)
        assert "return a - b" in out

    def test_apply_keeps_rest_of_source(self):
        src = "func f(a, b):\n\treturn a * b\nfunc g(a, b):\n\treturn a - b\n"
        m = _aor(src, "*")[0]
        out = _apply_mutation(src, m)
        assert "return a - b" in out.split("func g")[1]

    def test_mdr_site_inside_guard_dedupes_guard_not(self):
        """守卫表达式含可变异算子（含新入族的 mdr_expr）时，GUARD_NOT
        去重语义与 arith_expr 一致——纯 mdr 守卫不再产冗余位点。"""
        src = "func f(a, b):\n\tif a * b:\n\t\treturn 1\n\treturn 0\n"
        ms = _collect(src)
        guard_nots = [m for m in ms if m.kind == "GUARD_NOT"]
        assert guard_nots == [], \
            "纯 mdr 守卫含可变异算子 → GUARD_NOT 去重应跳过"


class TestOpsVersionBump:
    def test_ops_version_bumped_for_collection_semantics(self):
        """§SPEC-014：收集语义变更必须随 OPS_VERSION bump（旧缓存一次性
        失效并披露——identity.disclose_superseded 消费该值）。"""
        assert OPS_VERSION == "see1550/2"
