"""SEE-1615 §SPEC-P2 — dry-run 完整计划预览（derived/observed/runtime 分区）。

判据：plan_id 仅 hash derived（observed 变化不动 id）；预览零副作用
（spawn_godot / renew / import / census spy 全 0）；缺/旧 map 仅
`would_renew`+具名缺失+成本披露；通道/理由/失败路径/复用面四类问题键
齐全；`validate_plan` 对无 handler assumption lint 失败但不作运行门。
"""

from __future__ import annotations

import hashlib

import pytest

from godot_qa_toolkit.mutation import reporting
from godot_qa_toolkit.mutation.reporting import _execution_plan, _plan_meta

_SRC = ("extends RefCounted\n\n\nfunc work(n: int) -> int:\n"
        "\tvar acc = 0\n\tfor i in range(n):\n\t\tacc += i\n\treturn acc\n")


def _plan_with(**overrides):
    base = {
        "file": "f.gd", "budget": 50, "scoped": True,
        "selection_mode": "line", "exec_scheme": "cold-shadow/1",
        "inject_scheme": "disk-shadow/1", "slot_mode": "farm",
        "adaptive_timeout_s": 12, "baseline_reused": False,
        "baseline_dirty": False, "test_filter": {"applied": True},
        "diff_available": False, "retry_estimate": 9, "executed_estimate": 5,
    }
    base.update(overrides)
    plan = _execution_plan(base)
    return plan, _plan_meta(plan, base, extra={"budget": 50},
                            map_state={"present": True, "fresh": True,
                                       "reason": "map_fresh"})


# ------------------------------------------- 分区与 plan_id（仅 derived）


def test_partitions_cover_all_plan_fields():
    """三分区齐全，且五组内每个字段都被分类（无未分类字段）。"""
    plan, meta = _plan_with()
    parts = meta["partitions"]
    assert set(parts.keys()) == {"derived", "observed", "runtime"}
    classified = {k for view in parts.values()
                  for fields in view.values() for k in fields}
    plan_fields = {k for group in plan.values() for k in group}
    assert plan_fields <= classified
    assert "selection_mode" in parts["derived"]["selection"]
    assert "baseline_reused" in parts["observed"]["diff"]
    assert "slot_mode" in parts["runtime"]["route"]


def test_answers_channel_is_exec_scheme_not_inject_scheme():
    """channel 回答唯一来源 = route.exec_scheme（fixture 两 scheme 可分辨）。"""
    plan, meta = _plan_with()
    assert meta["answers"]["channel"] == "cold-shadow/1"
    assert plan["route"].get("inject_scheme") == "disk-shadow/1"
    assert meta["answers"]["channel"] != plan["route"].get("inject_scheme")


def test_plan_id_stable_under_derived_and_blind_to_observed():
    """derived 稳定：同 derived 输入两次同 id；observed 变化不动 id；
    derived 变化必改 id。"""
    plan, meta = _plan_with()
    pid = meta["plan_id"]
    # 同 derived、不同 observed 输入（map 状态翻转 + observed 字段变化）
    # → id 不变
    plan2, meta2 = _plan_with(baseline_dirty=True, baseline_reused=True,
                              retry_estimate=99)
    assert meta2["plan_id"] == pid
    # derived 变化（budget）→ id 变
    plan3 = _execution_plan({"budget": 51, "scoped": True})
    meta3 = _plan_meta(plan3, {"budget": 51}, extra=None,
                       map_state={"present": True, "fresh": True,
                                  "reason": "map_fresh"})
    assert meta3["plan_id"] != pid
    assert len(pid) == 40  # sha1 hex


# ------------------------------------------------- 零副作用 spy（dry-run 端到端）


def _fixture_project(tmp_path, with_map=None):
    root = tmp_path / "proj"
    (root / "src_game").mkdir(parents=True)
    (root / "src_game" / "cell.gd").write_text(_SRC, encoding="utf-8")
    if with_map is not None:
        from attribution_fixtures import write_map
        write_map(root, with_map)
    return root, root / "src_game" / "cell.gd"


def _spy_all(monkeypatch):
    """spawn/renew/import/census 四 spy（预览期必须全 0）。"""
    from godot_qa_toolkit.mutation import (channel_router, inject,
                                           prewarm_proof, truth_select)
    calls = {"spawn": [], "renew": [], "import": [], "census": []}
    real_spawn = inject.spawn_godot

    def counting_spawn(*a, **k):
        calls["spawn"].append(1)
        return real_spawn(*a, **k)

    monkeypatch.setattr(inject, "spawn_godot", counting_spawn)
    monkeypatch.setattr(truth_select, "_resolve_renew",
                        lambda *a, **k: calls["renew"].append(1) or False)
    monkeypatch.setattr(prewarm_proof, "_run_import",
                        lambda *a, **k: calls["import"].append(1))
    monkeypatch.setattr(channel_router, "route_project_census",
                        lambda *a, **k: calls["census"].append(1))
    return calls


def _run_core_dry_run(root, target, monkeypatch, jobs=None):
    from godot_qa_toolkit.mutation import planner as planner_mod
    from godot_qa_toolkit.mutation import runner as runner_mod
    from godot_qa_toolkit.mutation.ops import _collect_mutations

    src = target.read_text(encoding="utf-8")
    mutants = _collect_mutations(src, str(target))
    assert mutants

    class _FakeStore:
        def __init__(self, *a, **k):
            pass

        def load_manifest(self):
            return None

        def load_entries(self):
            return {}

    monkeypatch.setattr(runner_mod, "ResumeStoreV2",
                        lambda *a, **k: _FakeStore())
    monkeypatch.setattr(planner_mod, "ResumeStoreV2",
                        lambda *a, **k: _FakeStore())
    return runner_mod._core_dry_run(
        str(target), mutants, 50, False, len(mutants), str(root), None,
        "linux", None, False,
        hashlib.sha256(src.encode()).hexdigest(), {}, None, None,
        {"file": "src_game/cell.gd"}, None, None, "full", jobs=jobs)


def test_dry_run_preview_zero_side_effects_and_meta_present(tmp_path,
                                                            monkeypatch):
    """预览端到端：spawn/renew/import/census 全 0；meta 键齐全。"""
    root, target = _fixture_project(tmp_path)
    calls = _spy_all(monkeypatch)
    s = _run_core_dry_run(root, target, monkeypatch)["summary"]
    assert calls == {"spawn": [], "renew": [], "import": [], "census": []}
    meta = s["execution_plan_meta"]
    assert meta["plan_id"]
    # §SPEC-DERIVED-A：derived 三键在 plan 分区 + static_preconditions 块
    derived = meta["partitions"]["derived"]
    assert derived["budget"]["explicit_timeout_s"] == 600
    assert derived["route"]["boot_wiring_hash"]
    assert derived["route"]["boot_wiring_hash"] != ""
    # dry-run 恒传 extra → 无显式 --tests 时如实披露 None
    assert derived["selection"]["explicit_tests_glob"] is None
    # §SPEC-PLANFIELD-A：jobs 入 derived（budget 组）+ dry-run stage_timings
    assert isinstance(derived["budget"]["jobs"], int)
    assert derived["budget"]["jobs"] >= 1
    st = s["stage_timings"]
    assert set(st.keys()) >= {"planning", "route_scan", "total"}
    # 精简夹具尾段可 <0.5ms（round3 后 0.0）——非负即可；真实分解由
    # KOL dry-run 实跑承载（route_scan 引用图解析为可读大头）
    assert st["total"] >= 0
    assert st["planning"] >= 0 and st["route_scan"] >= 0
    sp = meta["static_preconditions"]
    assert set(sp.keys()) == {"import_cache", "gut_cmdln", "godot_binary",
                              "baseline_health"}
    # 精简 fixture（无 addons/gut/.godot）→ ok=False 是诚实的探测值
    assert sp["import_cache"]["ok"] is False
    assert "missing=" in sp["import_cache"]["evidence"]
    assert sp["gut_cmdln"]["ok"] is False
    assert sp["gut_cmdln"]["evidence"].endswith("gut_cmdln.gd")
    assert sp["godot_binary"]["ok"] is True
    # F1：evidence 增强含 binary kind 与启动诊断（ok 语义不变严）
    assert "| kind=" in sp["godot_binary"]["evidence"]
    assert "launch_issue=" in sp["godot_binary"]["evidence"]
    # §SPEC-PREFLIGHT P3：默认 not_checked（不假装全绿）
    bh = sp["baseline_health"]
    assert bh["status"] == "not_checked"
    assert bh["reason"] == "check_baseline_not_requested"
    # §SPEC-PLANFIRST：dry-run = plan 全量——problems[] 在场（不阻断）
    plan_block = s["plan"]
    assert isinstance(plan_block["problems"], list)
    assert plan_block["budget_s"] <= 10
    assert plan_block["elapsed_s"] <= 10.5
    # 贵操作清单（三 op，各带 cost/reuse 口径）
    ops = meta["expensive_operations"]
    assert {o["op"] for o in ops} == {"baseline_gut",
                                      "attribution_map_build",
                                      "mutation_legs"}
    assert all("cost" in o and "reuse" in o and "skippable" in o
               for o in ops)
    assert set(meta["answers"].keys()) == {
        "channel", "reasons", "failure_paths", "reuse_surface"}
    assert meta["answers"]["channel"] == \
        s["execution_plan"]["route"].get("exec_scheme")
    assert meta["answers"]["reuse_surface"] == s["execution_plan"]["diff"]
    assert meta["answers"]["failure_paths"]


# ----------------------------------------- 缺/旧 map：would_renew 披露


def test_missing_map_would_renew_named_missing_and_cost(tmp_path,
                                                        monkeypatch):
    """缺 map：would_renew=True + 具名缺失 + 成本披露；零续新。"""
    root, target = _fixture_project(tmp_path)  # 不写映射
    calls = _spy_all(monkeypatch)
    s = _run_core_dry_run(root, target, monkeypatch)["summary"]
    block = s["execution_plan_meta"]["map_state"]
    assert block["would_renew"] is True
    # §SPEC-MAPSTATE-A：present/fresh 原始探测值直出（三态直读）
    assert block["present"] is False
    assert block["fresh"] is None
    assert "attribution_map" in block["missing"]
    assert block["cost"]
    assert "auto map-update (full-chain GUT renewal)" in block["cost"]
    assert calls["renew"] == [] and calls["spawn"] == []


def test_stale_map_would_renew_named(tmp_path, monkeypatch):
    """旧 map（full 模式不触发 fail-loud）：would_renew=True + 具名新鲜度
    缺失；零续新。"""
    root, target = _fixture_project(
        tmp_path, with_map={"src_game/cell.gd": []})
    target.write_text(_SRC + "\n# drift\n", encoding="utf-8")  # 映射过期
    calls = _spy_all(monkeypatch)
    s = _run_core_dry_run(root, target, monkeypatch)["summary"]
    block = s["execution_plan_meta"]["map_state"]
    assert block["would_renew"] is True
    assert block["present"] is True
    assert block["fresh"] is False
    assert "attribution_map_freshness" in block["missing"]
    assert calls["renew"] == []


def test_fresh_map_no_would_renew(tmp_path, monkeypatch):
    """新鲜 map：would_renew=False、无映射缺失（fixture 无预导入证明，
    prewarm_proof 可在缺失名单——属需 import 的具名披露，非映射缺失）。"""
    root, target = _fixture_project(
        tmp_path, with_map={"src_game/cell.gd": []})
    calls = _spy_all(monkeypatch)
    s = _run_core_dry_run(root, target, monkeypatch)["summary"]
    block = s["execution_plan_meta"]["map_state"]
    assert block["would_renew"] is False
    assert block["present"] is True
    assert block["fresh"] is True
    assert "attribution_map" not in block["missing"]
    assert "attribution_map_freshness" not in block["missing"]
    assert calls["renew"] == []


# ------------------------------------- validate_plan：lint 失败不作运行门


def test_validate_plan_lints_assumption_without_handler():
    """无 handler assumption → lint 失败；有 handler → lint 通过。"""
    plan, meta = _plan_with()
    assert reporting.validate_plan(meta) == []
    broken = {**meta, "assumptions": [
        {"name": "selection_usable", "handler": None},
        {"name": "route_stable", "handler": "route_drift_gate"}]}
    problems = reporting.validate_plan(broken)
    assert problems
    assert any("selection_usable" in p for p in problems)


def test_would_renew_makes_lint_fail_but_not_a_run_gate(tmp_path,
                                                        monkeypatch):
    """缺 map 预览：lint 失败（selection_usable 无 handler），但 dry-run
    结果仍 ok=True——lint 不作运行门。"""
    root, target = _fixture_project(tmp_path)  # 缺 map
    _spy_all(monkeypatch)
    r = _run_core_dry_run(root, target, monkeypatch)
    s = r["summary"]
    meta = s["execution_plan_meta"]
    assert meta["map_state"]["would_renew"] is True
    problems = reporting.validate_plan(meta)
    assert any("selection_usable" in p for p in problems)
    assert r["ok"] is True


def test_dry_run_core_setup_passes_renew_false(tmp_path, monkeypatch):
    """零副作用守门单点：dry-run 经 _core_setup 进 _resolve_auto_glob 时
    必须 allow_renew=False（renew 抑制在 runner 侧单点钉住；line/fn 面
    才可达该点，full 面直接短路不经 resolve_selection）。"""
    from godot_qa_toolkit.mutation import runner as runner_mod
    from godot_qa_toolkit.mutation import truth_select

    root, target = _fixture_project(tmp_path)
    seen: dict = {}

    def fake_resolve(project_root, target_file, *a, **k):
        seen["renew"] = k.get("renew")
        raise truth_select.MutationSelectionError("stub_no_map", "stub")

    monkeypatch.setattr(truth_select, "resolve_selection", fake_resolve)
    with pytest.raises(truth_select.MutationSelectionError):
        runner_mod._run_mutation_core(str(target), str(root), dry_run=True,
                                      select_mode="fn")
    assert seen["renew"] is False


# ------------------------- §SPEC-DERIVED-A：derived 三键 + static_preconditions


def test_derived_keys_in_partitions_and_plan_id_sensitivity():
    """三静态输入独立成 derived 键（分属 budget/route/selection 组）；
    plan_id 对显式 timeout / wiring hash / 显式 tests glob 敏感，对
    observed（baseline_dirty）盲，同输入逐字节稳定。"""
    src = {"selection_mode": "line", "scoped": True,
           "exec_scheme": "cold-shadow/1", "budget": 50}
    extra = {"explicit_timeout_s": 120, "boot_wiring_hash": "wh-1",
             "explicit_tests_glob": "res://tests/custom/"}
    plan = _execution_plan(src, extra=extra)
    meta = _plan_meta(plan, src, extra=extra,
                      map_state={"present": True, "fresh": True,
                                 "reason": "map_fresh"})
    derived = meta["partitions"]["derived"]
    assert derived["budget"]["explicit_timeout_s"] == 120
    assert derived["route"]["boot_wiring_hash"] == "wh-1"
    assert derived["selection"]["explicit_tests_glob"] == \
        "res://tests/custom/"
    pid = meta["plan_id"]
    # 同输入重算 → 逐字节稳定
    again = _plan_meta(_execution_plan(src, extra=extra), src, extra=extra,
                       map_state={"present": True, "fresh": True,
                                  "reason": "map_fresh"})
    assert again["plan_id"] == pid
    # 显式 timeout / wiring hash 变化 → id 变（derived 敏感）
    for key, val, group in (("explicit_timeout_s", 240, "budget"),
                            ("boot_wiring_hash", "wh-2", "route"),
                            ("explicit_tests_glob", "res://other/",
                             "selection"),
                            ("jobs", 8, "budget")):
        src2 = dict(src)
        plan2 = _execution_plan(src2, extra={**extra, key: val})
        meta2 = _plan_meta(plan2, src2, extra={**extra, key: val},
                           map_state={"present": True, "fresh": True,
                                      "reason": "map_fresh"})
        assert meta2["plan_id"] != pid, key
        assert meta2["partitions"]["derived"][group][key] == val
    # observed 变化（baseline_dirty）→ id 不变
    plan3 = _execution_plan({**src, "baseline_dirty": True}, extra=extra)
    meta3 = _plan_meta(plan3, {**src, "baseline_dirty": True}, extra=extra,
                       map_state={"present": True, "fresh": True,
                                  "reason": "map_fresh"})
    assert meta3["plan_id"] == pid
    # 显式 tests glob 缺席 → derived 无该键（缺席不造）
    plan4 = _execution_plan(src, extra={"budget": 50})
    meta4 = _plan_meta(plan4, src, extra={"budget": 50})
    assert "explicit_tests_glob" not in meta4["partitions"]["derived"] \
        .get("selection", {})


def test_dry_run_jobs_passthrough_effective(tmp_path, monkeypatch):
    """§SPEC-PLANFIELD-A：jobs 穿透 dry-run——显式 1 → 1（串行快路径
    语义同源）；缺省 → 编排 effective（>=2 钳制）。"""
    root, target = _fixture_project(tmp_path)
    _spy_all(monkeypatch)
    s1 = _run_core_dry_run(root, target, monkeypatch, jobs=1)["summary"]
    d1 = s1["execution_plan_meta"]["partitions"]["derived"]
    assert d1["budget"]["jobs"] == 1
    root2, target2 = _fixture_project(tmp_path / "p2")
    _spy_all(monkeypatch)
    s2 = _run_core_dry_run(root2, target2, monkeypatch)["summary"]
    d2 = s2["execution_plan_meta"]["partitions"]["derived"]
    assert isinstance(d2["budget"]["jobs"], int) and d2["budget"]["jobs"] >= 2


def test_check_baseline_red_named_in_preconditions(tmp_path, monkeypatch):
    """§SPEC-PREFLIGHT P3：--check-baseline（env 单点）只读跑基线——红即
    具名（count/top）落 static_preconditions.baseline_health。"""
    from godot_qa_toolkit.measurement import orchestrator as mo
    from godot_qa_toolkit.mutation import runner as runner_mod

    root, target = _fixture_project(tmp_path)
    # 基线面所需：.gutconfig 默认面 + 可扫描 tests 目录 + acceptance 面
    (root / ".gutconfig.json").write_text('{"dirs": ["res://tests/unit"]}',
                                          encoding="utf-8")
    (root / "tests" / "unit").mkdir(parents=True)
    (root / "acceptance" / "features").mkdir(parents=True)
    (root / "acceptance" / "features" / "pf.feature").write_text(
        "Feature: pf\n\n  Scenario: s\n    Given x\n",
        encoding="utf-8")
    monkeypatch.setenv(runner_mod._ENV_CHECK_BASELINE, "1")
    monkeypatch.setattr(mo, "_acquire_lazy_baseline", lambda root, a, t: (
        {("res://tests/a.gd", "test_red1"), ("res://tests/b.gd", "test_red2"),
         ("res://tests/c.gd", "test_red3")}, {"face": "gutconfig_default",
                                              "wall_s": 0.2, "count": 3}))
    s = _run_core_dry_run(root, target, monkeypatch)["summary"]
    bh = s["execution_plan_meta"]["static_preconditions"]["baseline_health"]
    assert bh["status"] == "red"
    assert bh["count"] == 3
    assert len(bh["top"]) == 3 and all("::" in x for x in bh["top"])


def test_map_state_block_direct_read_three_states():
    """§SPEC-MAPSTATE-A：map_state 块直读 present/fresh（缺失/不可用/
    陈旧/新鲜四形态），既有 would_renew/missing/cost/reason 零变化；
    未探测（None）→ present/fresh 均 None（不造事实）。"""
    from godot_qa_toolkit.mutation.reporting import _map_state_block

    missing = _map_state_block({"present": False, "fresh": None,
                                "reason": "map_missing"}, None)
    assert (missing["present"], missing["fresh"]) == (False, None)
    unusable = _map_state_block({"present": True, "fresh": None,
                                 "reason": "map_unusable"}, None)
    assert (unusable["present"], unusable["fresh"]) == (True, None)
    assert unusable["reason"] == "map_unusable"
    stale = _map_state_block({"present": True, "fresh": False,
                              "reason": "map_stale:drift"}, None)
    assert (stale["present"], stale["fresh"]) == (True, False)
    fresh = _map_state_block({"present": True, "fresh": True,
                              "reason": "map_fresh"}, True)
    assert (fresh["present"], fresh["fresh"]) == (True, True)
    assert fresh["would_renew"] is False
    none = _map_state_block(None, None)
    assert none["present"] is None and none["fresh"] is None
    assert none["would_renew"] is None


def test_static_preconditions_block_shapes():
    """static_preconditions 独立块：三项各带 ok 与 evidence；未探测（None）
    → 缺席不造（既有 meta 形态不变）。"""
    plan = _execution_plan({"budget": 50, "scoped": True}, extra={"budget": 50})
    pc = {
        "import_cache": {"ok": True, "evidence": "/p/.godot"},
        "gut_cmdln": {"ok": False, "evidence": "/p/addons/gut/gut_cmdln.gd"},
        "godot_binary": {"ok": True, "evidence": "/usr/bin/godot"},
        "baseline_health": {"status": "not_checked",
                            "reason": "check_baseline_not_requested"},
    }
    meta = _plan_meta(plan, {"budget": 50}, extra={"budget": 50},
                      static_preconditions=pc)
    sp = meta["static_preconditions"]
    assert set(sp.keys()) == {"import_cache", "gut_cmdln", "godot_binary",
                              "baseline_health"}
    assert sp["import_cache"] == {"ok": True, "evidence": "/p/.godot"}
    assert sp["gut_cmdln"]["ok"] is False
    meta_none = _plan_meta(plan, {"budget": 50}, extra={"budget": 50})
    assert "static_preconditions" not in meta_none
