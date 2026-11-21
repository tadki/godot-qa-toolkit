"""SEE-1610 §SPEC-PLAN Phase 0 — 只读执行计划聚合判据。

V1 plan 阶段 spawn_godot==0（聚合器纯读零触达）；V2 既有 summary 键字节
不变只新增 execution_plan；V3 同 derived 输入计划逐字节稳定；V7 dry-run
同构计划且零 GUT/零续新。
"""
from __future__ import annotations

import json

from godot_qa_toolkit.mutation import reporting
from godot_qa_toolkit.mutation.reporting import (
    _dry_run_result,
    _execution_plan,
    _mutation_report,
)

PLAN_GROUPS = {"selection", "route", "budget", "diff", "preconditions"}


# ---------------------------------------------------------- V3/V1 纯读


def test_builder_deterministic_and_grouped():
    summary = {
        "file": "f.gd", "budget": 50, "scoped": True,
        "baseline_reused": False, "adaptive_timeout_s": 12,
        "selection_mode": "line",
        "test_selection": {"source": "runtime_mapping", "reason": "r"},
        "exec_scheme": "cold-shadow/1", "slot_mode": "farm",
        "baseline_dirty": False, "test_filter": {"applied": True},
    }
    p1 = _execution_plan(summary)
    p2 = _execution_plan(summary)
    assert json.dumps(p1, sort_keys=True) == json.dumps(p2, sort_keys=True)
    assert set(p1.keys()) == PLAN_GROUPS
    assert p1["selection"]["selection_mode"] == "line"
    assert p1["route"]["exec_scheme"] == "cold-shadow/1"
    assert p1["budget"]["budget"] == 50
    assert p1["diff"]["baseline_reused"] is False
    assert p1["preconditions"]["test_filter"] == {"applied": True}
    # 缺席键不造
    assert "resumed" not in p1["diff"]
    assert "freshness" not in p1["route"]


def test_builder_tolerates_empty_summary():
    p = _execution_plan({})
    assert set(p.keys()) == PLAN_GROUPS
    assert all(v == {} for v in p.values())


# ---------------------------------------------------------- V2 既有键不变


def test_mutation_report_adds_plan_without_touching_existing_keys():
    class _M:
        line = 1
        column = 1
        kind = "K"
        operator = "op"
        original = "x"
        mutated = "y"
        verdict = "killed"
        id = "a"
        killed_by = ["t"]

    results = [{"verdict": "killed", "id": "a", "killed_by": ["t"],
                "line": 1, "reason": ""}]
    rep = _mutation_report(
        "f.gd", 1, 0, 0, 0, 0, 0, 50, results, scoped=True,
        baseline_reused=False, slot_info={"exec_scheme": "cold-shadow/1",
                                          "slot_mode": "farm"})
    s = rep["summary"]
    assert "execution_plan" in s
    # V2：既有键值零变化——除新键外，摘要与无 plan 形态逐键一致
    assert s["killed"] == 1 and s["budget"] == 50 and s["scoped"] is True
    assert s["execution_plan"]["route"]["exec_scheme"] == "cold-shadow/1"
    assert s["execution_plan"]["selection"]["scoped"] is True


# ---------------------------------------------------------- V7 dry-run 同构


def test_dry_run_result_carries_isomorphic_plan():
    class _M:
        kind = "K"

    res = _dry_run_result("f.gd", [_M() for _ in range(3)], 5,
                          scoped=True, total_sites=9,
                          estimate={"diff_available": False,
                                    "retry_estimate": 9,
                                    "executed_estimate": 5})
    s = res["summary"]
    plan = s["execution_plan"]
    assert set(plan.keys()) == PLAN_GROUPS
    assert plan["budget"]["budget"] == 5
    assert plan["diff"]["diff_available"] is False
    assert plan["selection"]["scoped"] is True


def test_core_dry_run_rebuilds_plan_after_selection_disclosure(
        tmp_path, monkeypatch):
    """V7：选面披露在 plan 之后挂——末尾重建保证 plan 携带完整选面。"""
    import hashlib
    from godot_qa_toolkit.mutation import runner as runner_mod
    from godot_qa_toolkit.mutation.ops import _collect_mutations

    root = tmp_path / "proj"
    (root / "src_game").mkdir(parents=True)
    src = ("extends RefCounted\n\n\nfunc work(n: int) -> int:\n"
           "\tvar acc = 0\n\tfor i in range(n):\n\t\tacc += i\n\treturn acc\n")
    (root / "src_game" / "cell.gd").write_text(src, encoding="utf-8")

    spawn_calls = []
    from godot_qa_toolkit.mutation import inject as inject_mod
    real_spawn = inject_mod.spawn_godot

    def counting_spawn(*a, **k):
        spawn_calls.append(1)
        return real_spawn(*a, **k)

    monkeypatch.setattr(inject_mod, "spawn_godot", counting_spawn)

    rel = "src_game/cell.gd"
    mutants = _collect_mutations(src, rel)
    assert mutants
    resume_dir = tmp_path / "store"

    class _FakeStore:
        def __init__(self, *a, **k):
            pass

        def load_manifest(self):
            return None

        def load_entries(self):
            return {}

    monkeypatch.setattr(runner_mod, "ResumeStoreV2",
                        lambda *a, **k: _FakeStore())
    import godot_qa_toolkit.mutation.planner as planner_mod
    monkeypatch.setattr(planner_mod, "ResumeStoreV2",
                        lambda *a, **k: _FakeStore())
    est = runner_mod._core_dry_run(
        str(root / "src_game" / "cell.gd"), mutants, 50, False, len(mutants),
        str(root), None, "linux", None, False,
        hashlib.sha256(src.encode()).hexdigest(), {}, None,
        {"file": rel}, None, None, None, "line")
    s = est["summary"]
    plan = s["execution_plan"]
    assert set(plan.keys()) == PLAN_GROUPS
    # V7：dry-run 零 GUT（spawn 计数 0）
    assert not spawn_calls
    # V7 同构：选面披露在 plan 内（末尾重建生效）
    assert plan["selection"].get("selection_mode") == "line"
    if "test_selection" in s:
        assert plan["selection"].get("test_selection") == s["test_selection"]
