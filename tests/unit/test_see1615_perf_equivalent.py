"""SEE-1615 §SPEC-PERF2 —— 等价/无意义变异预剪枝判据。

每规则正例 + 反例 + 构造性证明；负例（动态类型不可证形态）必须保守保留；
开关默认 OFF；stable-id 机制与 diff/resume 兼容（剪枝在 id 标定后，
kept id 零位移）；build_manifest 披露 equivalent_pruned（规则/分母/逐点）。
"""

from __future__ import annotations

from godot_qa_toolkit.mutation import equivalent as eq
from godot_qa_toolkit.mutation.ops import _collect_mutations
from godot_qa_toolkit.mutation.scopemanifest import build_manifest


def _tagged(src: str) -> list:
    """生产同源：收集 → build_manifest 标定 stable id（不做剪枝）。"""
    mutants = _collect_mutations(src, "t.gd")
    _, _, tagged, _ = build_manifest(src, "t.gd", mutants)
    return tagged


def _ids(src: str) -> dict[str, str]:
    """{(line, operator, mutated): stable_id}——规则匹配用位点索引。"""
    out: dict[str, str] = {}
    for m in _tagged(src):
        out[f"{m.line}|{m.operator}|{m.mutated}"] = m.stable_id
    return out


def _pruned_rules(src: str) -> "tuple[dict[str, str], list]":
    kept, pruned = eq.prune_equivalent(src, _tagged(src))
    return {p["stable_id"]: p["rule"] for p in pruned}, kept


# 可证形态：两侧均为整数字面量（n+0 动态左操作数因 float −0.0 符号位
# 不可证，作负例单独覆盖）
SRC_ZERO = ("extends RefCounted\n\nfunc f() -> int:\n"
            "\treturn 5 + 0\n\nfunc g() -> int:\n"
            "\treturn 7 - 0\n")

SRC_MUL = ("extends RefCounted\n\nfunc f(n: int) -> int:\n"
           "\treturn n * 1\n")


def test_zero_add_only_minus_variant_pruned():
    """EQ_ARITH_ZERO_ADD 正例：n + 0 的 +→− 剪；反例：+→* 与 +→/ 不剪
    （5*0=0 ≠ 5、5/0 除零——均有可观测行为差，非等价）。"""
    rules, kept = _pruned_rules(SRC_ZERO)
    target = _ids(SRC_ZERO)["4|+|-"]
    assert rules.get(target) == eq.RULE_EQ_ZERO_ADD
    # 反例：同位点其余方向（* → 0、/ → 除零）均有可观测行为差，不剪
    star = _ids(SRC_ZERO)["4|+|*"]
    slash = _ids(SRC_ZERO)["4|+|/"]
    assert star not in rules and slash not in rules
    for rid, rule in rules.items():
        assert rule in eq._RULE_PROOFS


def test_zero_sub_identity_pruned():
    """EQ_ARITH_ZERO_SUB 正例：n − 0 的 −→+ 剪（n−0 ≡ n+0 ≡ n）。"""
    rules, _kept = _pruned_rules(SRC_ZERO)
    target = _ids(SRC_ZERO)["7|-|+"]
    assert rules.get(target) == eq.RULE_EQ_ZERO_SUB


def test_negatives_conservative_keep():
    """负例（保守保留）：左操作数非整数字面量（float −0.0 符号位/动态类型
    不可证）、右操作数非 0、float 字面量、非目标算子方向。"""
    src = ("extends RefCounted\n\nfunc f(x: float, a: int, b: int) -> float:\n"
           "\tvar t1 = x + 0\n\tvar t2 = a + b\n\tvar t3 = a - b\n"
           "\treturn t1 + t2 + t3\n")
    rules, kept = _pruned_rules(src)
    assert rules == {}


def test_one_mul_div_variant_not_pruned_dynamic_type_risk():
    """``n * 1`` 的 *→/ 反例：String/Array 重复语义 ``s * 1 ≡ s`` 但
    ``s / 1`` 运行时错误——动态类型不可证 → 保守保留（不剪）。"""
    rules, _kept = _pruned_rules(SRC_MUL)
    target = _ids(SRC_MUL)["4|*|/"]
    assert target not in rules


def test_armswap_symmetric_pruned_asymmetric_kept(tmp_path):
    """EQ_ARMSWAP_SYMMETRIC 正例：两臂文本相同 → 交换恒等；反例：两臂不同
    → 交换改变语义，保留。"""
    sym = ("extends RefCounted\n\nfunc f(c: bool, a: int) -> int:\n"
           "\treturn a if c else a\n")
    asym = ("extends RefCounted\n\nfunc f(c: bool, a: int, b: int) -> int:\n"
            "\treturn a if c else b\n")
    sym_rules, _ = _pruned_rules(sym)
    asym_rules, _ = _pruned_rules(asym)
    assert any(r == eq.RULE_EQ_ARMSWAP_SYMMETRIC for r in sym_rules.values())
    assert all(r != eq.RULE_EQ_ARMSWAP_SYMMETRIC for r in asym_rules.values())


def test_env_default_off(monkeypatch):
    monkeypatch.delenv(eq.ENV_EQUIV_PRUNE, raising=False)
    assert eq.resolve_equiv_prune() is False
    for v in ("1", "true", "yes", "on"):
        monkeypatch.setenv(eq.ENV_EQUIV_PRUNE, v)
        assert eq.resolve_equiv_prune() is True
    for v in ("0", "false", "no", "off", ""):
        monkeypatch.setenv(eq.ENV_EQUIV_PRUNE, v)
        assert eq.resolve_equiv_prune() is False


def test_kept_ids_unchanged_and_order_stable():
    """剪枝在 id 标定后：kept 的 stable_id 与集合与关闭时逐位一致（id 零
    位移），剪去的保留各自 id（逐点可查，不隐形）。"""
    src = SRC_ZERO
    ids_on = _ids(src)  # _ids 不做剪枝——全量 id 基准
    mutants = _collect_mutations(src, "t.gd")
    kept, pruned = eq.prune_equivalent(src, mutants)
    kept_ids = {m.stable_id for m in kept}
    all_ids = {m.stable_id for m in mutants}
    assert kept_ids == all_ids - {p["stable_id"] for p in pruned}
    for m in kept:
        assert m.stable_id in ids_on.values()


def test_rules_proofs_complete():
    """每规则必须有构造性证明文本（正例/反例由上列用例承载）。"""
    for rule in (eq.RULE_EQ_ZERO_ADD, eq.RULE_EQ_ZERO_SUB,
                 eq.RULE_EQ_ARMSWAP_SYMMETRIC):
        assert rule in eq._RULE_PROOFS and len(eq._RULE_PROOFS[rule]) > 20


# ------------------------------------------------- build_manifest 披露接线


def test_build_manifest_prune_disclosure():
    """开关 ON：manifest 位点排除剪枝项，披露含规则/分母/逐点（含 id）。"""
    from godot_qa_toolkit.mutation.scopemanifest import build_manifest

    src = SRC_ZERO
    mutants = _collect_mutations(src, "t.gd")
    out: dict = {}
    header, spans, tagged, scopes = build_manifest(
        src, "t.gd", mutants, equivalent_prune=True, prune_out=out)
    d = out["equivalent_pruned"]
    assert d["enabled"] is True
    assert d["sites_before"] == len(mutants)
    assert d["sites_after"] == len(tagged)
    assert d["sites_after"] < d["sites_before"]
    assert d["items"] and all(i["stable_id"] for i in d["items"])
    assert set(d["rules"]) <= {eq.RULE_EQ_ZERO_ADD, eq.RULE_EQ_ZERO_SUB}
    pruned_ids = {i["stable_id"] for i in d["items"]}
    assert pruned_ids.isdisjoint({m.stable_id for m in tagged})


def test_build_manifest_prune_off_is_identity():
    """开关 OFF（默认）：manifest 与无参调用逐位一致，披露 enabled=false。"""
    from godot_qa_toolkit.mutation.scopemanifest import build_manifest

    src = SRC_ZERO
    mutants = _collect_mutations(src, "t.gd")
    base = build_manifest(src, "t.gd", mutants)
    out: dict = {}
    on = build_manifest(src, "t.gd", mutants, equivalent_prune=False,
                        prune_out=out)
    assert on[2] == base[2]  # mutants 逐位一致
    assert out["equivalent_pruned"]["enabled"] is False
    assert out["equivalent_pruned"]["items"] == []


def test_env_switch_controls_build_manifest(monkeypatch):
    monkeypatch.delenv(eq.ENV_EQUIV_PRUNE, raising=False)
    src = SRC_ZERO
    mutants = _collect_mutations(src, "t.gd")
    out: dict = {}
    build_manifest(src, "t.gd", mutants, prune_out=out)
    assert out["equivalent_pruned"]["enabled"] is False
    monkeypatch.setenv(eq.ENV_EQUIV_PRUNE, "1")
    out2: dict = {}
    build_manifest(src, "t.gd", mutants, prune_out=out2)
    assert out2["equivalent_pruned"]["enabled"] is True
    assert out2["equivalent_pruned"]["sites_after"] < \
        out2["equivalent_pruned"]["sites_before"]
