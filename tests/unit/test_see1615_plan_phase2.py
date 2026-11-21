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


def _run_core_dry_run(root, target, monkeypatch):
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
        {"file": "src_game/cell.gd"}, None, None, "full")


def test_dry_run_preview_zero_side_effects_and_meta_present(tmp_path,
                                                            monkeypatch):
    """预览端到端：spawn/renew/import/census 全 0；meta 键齐全。"""
    root, target = _fixture_project(tmp_path)
    calls = _spy_all(monkeypatch)
    s = _run_core_dry_run(root, target, monkeypatch)["summary"]
    assert calls == {"spawn": [], "renew": [], "import": [], "census": []}
    meta = s["execution_plan_meta"]
    assert meta["plan_id"]
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
