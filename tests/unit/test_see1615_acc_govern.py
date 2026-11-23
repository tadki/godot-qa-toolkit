"""SEE-1615 §SPEC-ACC-GOVERN —— acceptance 对等治理四组判据。

九类 failure_class 分派、四象限红分类（同上下文无插桩 replay：
签名不同/红转绿归 non_deterministic_replay 不得豁免）、
gherkin_scenarios 不耐受声明与草稿闭环、证据块四态与 union 降级、
空证据不误判收敛。
"""

from __future__ import annotations

import pytest

from godot_qa_toolkit.measurement import acc_govern as ag
from godot_qa_toolkit.measurement.acc_govern import (
    acceptance_evidence_block, acceptance_identity,
    classify_acceptance_failure, classify_red_quadrant, intolerant_draft,
    parse_gherkin_scenarios)
from godot_qa_toolkit.measurement.backup import MeasurementError
from godot_qa_toolkit.measurement.orchestrator import _parse_intolerant_config


# ------------------------------------------------- 九类 failure_class 分派

@pytest.mark.parametrize("kwargs,expected", [
    ({"executed": False}, "no_evidence"),
    ({"parse_ok": False}, "parse_error"),
    ({"timed_out": True}, "scenario_timeout"),
    ({"replay_failed": True}, "replay_check"),
    ({"reconciliation_ok": False}, "snapshot_reconciliation"),
    ({"converged": False}, "not_converged"),
    ({"nondeterministic": True}, "non_deterministic_replay"),
    ({"attributed": False}, "unattributed"),
    ({"assert_failed": True}, "scenario_assert_failure"),
])
def test_nine_failure_classes_dispatch(kwargs, expected):
    assert classify_acceptance_failure(**kwargs) == expected
    assert expected in ag.ACCEPTANCE_FAILURE_CLASSES


def test_failure_classes_exactly_nine():
    assert len(ag.ACCEPTANCE_FAILURE_CLASSES) == 9
    assert len(set(ag.ACCEPTANCE_FAILURE_CLASSES)) == 9


# ------------------------------------------------ 四象限红分类（replay）

def test_quadrant_existing_red_same_signature_absorbable():
    ident = acceptance_identity("f.feature", "s", 12)
    assert classify_red_quadrant(
        ident, {ident: "sig-a"}, ["sig-a"]) == "existing_red"


def test_quadrant_new_red_blocks():
    ident = acceptance_identity("f.feature", "s")
    assert classify_red_quadrant(ident, {}, ["sig-x"]) == "new_red"
    # 既有基线红但 replay 签名不同 → non_deterministic_replay（不得按名
    # 吸收——Archi A 项设计：签名不同归非确定，非 new_red）
    assert classify_red_quadrant(
        ident, {ident: "sig-a"}, ["sig-b"]) == "non_deterministic_replay"


def test_quadrant_replay_signature_mismatch_is_nondeterministic():
    ident = acceptance_identity("f.feature", "s")
    assert classify_red_quadrant(
        ident, {ident: "sig-a"}, ["sig-a", "sig-b"]) == \
        "non_deterministic_replay"


def test_quadrant_red_to_green_is_nondeterministic_not_exempted():
    """红转绿（基线不稳）不得豁免——归 non_deterministic_replay。"""
    ident = acceptance_identity("f.feature", "s")
    assert classify_red_quadrant(
        ident, {ident: "sig-a"}, ["green"]) == "non_deterministic_replay"


def test_quadrant_empty_replay_is_no_evidence():
    ident = acceptance_identity("f.feature", "s")
    assert classify_red_quadrant(ident, {}, []) == "no_evidence"


def test_quadrant_green_without_baseline_is_green():
    ident = acceptance_identity("f.feature", "s")
    assert classify_red_quadrant(ident, {}, ["green"]) == "green"


# ------------------------------------ acceptance_scenarios 声明与草稿闭环

def test_gherkin_scenarios_parse_and_identity():
    cfg = {"gherkin_scenarios": [
        {"feature": "infi.feature", "scenario": "share flow",
         "scenario_line": 12, "example_row_line": 18,
         "reason": "source-contract assertion"},
    ]}
    out = parse_gherkin_scenarios(cfg)
    assert out[0]["feature"] == "infi.feature"
    assert out[0]["scenario_line"] == 12
    assert out[0]["example_row_line"] == 18
    ident = acceptance_identity("infi.feature", "share flow", 12, 18)
    assert ident == "infi.feature::share flow@12#18"


def test_gherkin_scenarios_validation_rejects_silent_exemption():
    with pytest.raises(ValueError):
        parse_gherkin_scenarios({"gherkin_scenarios": "nope"})
    with pytest.raises(ValueError):
        parse_gherkin_scenarios({"gherkin_scenarios": [
            {"feature": "f", "scenario": "s"}]})  # 缺 reason
    with pytest.raises(ValueError):
        parse_gherkin_scenarios({"gherkin_scenarios": [
            {"feature": "", "scenario": "s", "reason": "r"}]})


def test_intolerant_config_merges_gherkin_scenarios():
    cfg = {"tests": [{"name": "t1", "reason": "r1"}],
           "gherkin_scenarios": [
               {"feature": "f", "scenario": "s", "reason": "r"}]}
    tests, scoped, bare, _limits = _parse_intolerant_config(cfg)
    names = {t["name"] for t in tests}
    assert "t1" in names and "f::s" in names
    acc = [t for t in tests if t.get("gherkin")]
    assert acc and acc[0]["gherkin"]["feature"] == "f"
    # 声明校验失败 → MeasurementError（不得静默吸收）
    with pytest.raises(MeasurementError):
        _parse_intolerant_config({"gherkin_scenarios": [
            {"feature": "f", "scenario": "s"}]})


def test_intolerant_draft_roundtrip():
    entries = [{"feature": "f", "scenario": "s", "scenario_line": 3,
                "example_row_line": 5}]
    draft = intolerant_draft(entries)
    assert draft["gherkin_scenarios"][0]["scenario_line"] == 3
    # 草稿可直接被声明解析器接受（闭环）
    assert parse_gherkin_scenarios(draft)[0]["scenario"] == "s"


# ------------------------------------------ 证据块四态与空证据防假收敛

def test_evidence_block_four_states():
    unavailable = acceptance_evidence_block(executed=False, converged=None,
                                            rounds=0)
    assert unavailable["state"] == "unavailable"
    assert unavailable["converged"] is None

    na = acceptance_evidence_block(executed=True, converged=None,
                                   rounds=0, attempted=0)
    assert na["state"] == "not_applicable"

    partial = acceptance_evidence_block(executed=True, converged=False,
                                        rounds=2, partial_scenarios=1,
                                        attempted=5)
    assert partial["state"] == "partial"
    assert partial["union_integrity"] == "degraded"
    assert "acceptance partial != map partial" in partial["note"]

    valid = acceptance_evidence_block(executed=True, converged=True,
                                      rounds=3, attempted=5)
    assert valid["state"] == "valid"
    assert valid["union_integrity"] == "full"


def test_empty_evidence_never_converged():
    """空证据（未执行/零场景）不得报 valid/converged——防假收敛。"""
    for block in (acceptance_evidence_block(executed=False, converged=None,
                                            rounds=0),
                  acceptance_evidence_block(executed=True, converged=None,
                                            rounds=0, attempted=0)):
        assert block["state"] != "valid"
        assert block["converged"] is None


def test_retired_acceptance_scenarios_key_rejected():
    """SEE-1624 D-n4：旧键 `acceptance_scenarios` 退役——仍存在即具名拒绝
    （禁止静默忽略导致新键被读成空集）。"""
    import pytest as _pytest
    with _pytest.raises(ValueError, match="retired"):
        parse_gherkin_scenarios({"acceptance_scenarios": [
            {"feature": "f", "scenario": "s", "reason": "r"}]})
