"""SEE-1615 §SPEC-P1 — route 一次权威计算 + 下游漂移比对契约。

正反例：父层 identity/file_key 携带权威 exec_scheme；下游重解析
（core_setup / pre-baseline 门 / _loop_slot / worker 注入）只做比对校验，
双向漂移具名 `route_drift` fail-loud——零 baseline（_loop_baseline spy=0）、
零错误 file_key 写入（store/slot spy=0）、零引擎 spawn。稳定路径
（GQT_MUTATION_WORKER=0、stub 项目、匹配 scheme）行为不变。
"""

from __future__ import annotations

import pytest

from godot_qa_toolkit.mutation import runner as runner_mod
from godot_qa_toolkit.mutation import worker_channel as wc_mod
from godot_qa_toolkit.mutation.channel_router import RouteDecision
from godot_qa_toolkit.mutation.identity import COLD_SCHEME, WORKER_SCHEME

_SRC = ("extends RefCounted\n\nfunc add(a, b):\n\treturn a + b\n")


def _fixture_project(tmp_path):
    """stub 项目（gut_cmdln 无 gut.gd → 直驱结构性不可用）：drift 反例
    不需要真实 GUT——漂移必须在任何引擎调用前 fail-loud。"""
    proj = tmp_path / "proj"
    (proj / "tests" / "unit").mkdir(parents=True)
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub\n")
    (proj / "tests" / "unit" / "test_a.gd").write_text(
        "extends GutTest\nfunc t1() -> void:\n\tpass\n", encoding="utf-8")
    target = proj / "calc.gd"
    target.write_text(_SRC, encoding="utf-8")
    return proj, target


def _worker_route(target_res):
    return RouteDecision(target_res=target_res, channel="worker",
                         reasons=("eligible_worker",),
                         closure=(target_res,))


def _cold_route(target_res):
    return RouteDecision(target_res=target_res, channel="cold",
                         reasons=("target_in_gut_domain",),
                         closure=(target_res,))


# ---------------------------------------------- core_setup：父层 vs 实跑比对


def test_core_setup_drift_worker_parent_env_cold_named_fail(tmp_path,
                                                            monkeypatch):
    """父层权威=worker、实跑解析=cold（env 关断）→ route_drift 具名失败；
    spy：零 loop、零取槽、零注入会话（零 baseline / 零写入 / 零 spawn）。"""
    proj, target = _fixture_project(tmp_path)
    monkeypatch.setenv("GQT_MUTATION_WORKER", "0")
    parent = {"exec_scheme": WORKER_SCHEME}
    loop_calls, slot_calls, session_calls = [], [], []
    monkeypatch.setattr(runner_mod, "_run_mutant_loop_with_aborts",
                        lambda *a, **k: loop_calls.append(1))
    import godot_qa_toolkit.mutation.runhelp as runhelp_mod
    monkeypatch.setattr(runhelp_mod.shadowpool, "acquire_slot",
                        lambda *a, **k: slot_calls.append(1))
    monkeypatch.setattr(wc_mod.WorkerSession, "start",
                        lambda self: session_calls.append(1))
    r = runner_mod._run_mutation_core(str(target), str(proj), identity=parent,
                                      worker_channel=True)
    assert r["ok"] is False
    assert r["summary"]["route_drift"] is True
    assert r["summary"]["authoritative_scheme"] == WORKER_SCHEME
    assert r["summary"]["resolved_scheme"] == COLD_SCHEME
    assert any("route_drift" in f["reason"] for f in r["failures"])
    assert loop_calls == [] and slot_calls == [] and session_calls == []


def test_core_setup_drift_cold_parent_resolved_worker_named_fail(tmp_path,
                                                                 monkeypatch):
    """反向漂移：父层权威=cold、实跑解析=worker → route_drift 具名失败。"""
    proj, target = _fixture_project(tmp_path)
    parent = {"exec_scheme": COLD_SCHEME}
    monkeypatch.setattr(
        wc_mod, "resolve_exec_scheme",
        lambda project_root, target_res, flag=None:
            (WORKER_SCHEME, _worker_route(target_res)))
    loop_calls = []
    monkeypatch.setattr(runner_mod, "_run_mutant_loop_with_aborts",
                        lambda *a, **k: loop_calls.append(1))
    r = runner_mod._run_mutation_core(str(target), str(proj), identity=parent,
                                      worker_channel=None)
    assert r["ok"] is False
    assert r["summary"]["route_drift"] is True
    assert r["summary"]["authoritative_scheme"] == COLD_SCHEME
    assert r["summary"]["resolved_scheme"] == WORKER_SCHEME
    assert loop_calls == []


# ------------------------------------------ _loop_slot：漂移先于取槽（零写入）


def test_loop_slot_drift_fails_before_slot_provision(tmp_path, monkeypatch):
    """_loop_slot 覆盖：权威=cold、重解析=worker → RouteDriftError 先于
    farm_provision（acquire_slot spy=0 → 零槽写入）。"""
    import godot_qa_toolkit.mutation.runhelp as runhelp_mod
    from godot_qa_toolkit.mutation.stagetiming import StageClock

    proj, target = _fixture_project(tmp_path)
    monkeypatch.setattr(
        runner_mod, "resolve_exec_scheme",
        lambda project_root, target_res, flag=None:
            (WORKER_SCHEME, _worker_route(target_res)))
    slot_calls = []
    monkeypatch.setattr(runhelp_mod.shadowpool, "acquire_slot",
                        lambda *a, **k: slot_calls.append(1))
    with pytest.raises(wc_mod.RouteDriftError) as ei:
        runhelp_mod._loop_slot(str(proj), target, "linux",
                               {"exec_scheme": COLD_SCHEME}, None, True,
                               False, StageClock())
    assert "route_drift" in str(ei.value)
    assert ei.value.where == "loop_slot"
    assert slot_calls == []


# ------------------------------------- pre-baseline 门：漂移不得先付 baseline


def test_pre_baseline_gate_blocks_before_loop_baseline(tmp_path, monkeypatch):
    """_run_mutant_loop 入口门：漂移在 _loop_baseline 前具名失败
    （baseline spy=0 → 零 baseline 支付）。"""
    from godot_qa_toolkit.mutation.stagetiming import StageClock

    proj, target = _fixture_project(tmp_path)
    monkeypatch.setattr(
        runner_mod, "resolve_exec_scheme",
        lambda project_root, target_res, flag=None:
            (WORKER_SCHEME, _worker_route(target_res)))
    baseline_calls = []
    monkeypatch.setattr(runner_mod, "_loop_baseline",
                        lambda *a, **k: baseline_calls.append(1))
    with pytest.raises(wc_mod.RouteDriftError) as ei:
        runner_mod._run_mutant_loop(
            target, str(proj), _SRC, "", [], None, 60,
            identity={"exec_scheme": COLD_SCHEME}, worker_channel=True,
            clock=StageClock())
    assert "route_drift" in str(ei.value)
    assert ei.value.where == "pre_baseline"
    assert baseline_calls == []


# --------------------------------------------- worker 注入路径：closure 比对


def test_worker_injection_route_drift_named(tmp_path, monkeypatch):
    """worker 注入路径：scheme=worker 但 route_target 重解析判 cold →
    RouteDriftError（where=worker_injection），不进入逐腿编排。"""
    proj, target = _fixture_project(tmp_path)
    monkeypatch.setattr(wc_mod, "route_target",
                        lambda project_root, target_res:
                            _cold_route(target_res))
    legs_calls = []
    monkeypatch.setattr(wc_mod, "run_worker_legs",
                        lambda *a, **k: legs_calls.append(1))
    with pytest.raises(wc_mod.RouteDriftError) as ei:
        wc_mod.execute_worker_branch(
            target, str(proj), _SRC, [], None, set(), False, None, None,
            "calc.gd", "res://calc.gd", "linux", None, None, {}, None, None,
            expected_scheme=WORKER_SCHEME)
    assert ei.value.where == "worker_injection"
    assert legs_calls == []


def test_worker_injection_stable_path_passes(tmp_path, monkeypatch):
    """worker 注入稳定路径：scheme=worker 且 route_target=worker → 放行，
    逐腿编排收到 route.closure（行为不变）。"""
    proj, target = _fixture_project(tmp_path)
    monkeypatch.setattr(wc_mod, "route_target",
                        lambda project_root, target_res:
                            _worker_route(target_res))
    seen = {}

    def fake_legs(mutants, slot, target_rel, target_res, closure, *a, **k):
        seen["closure"] = closure
        return [], []

    monkeypatch.setattr(wc_mod, "run_worker_legs", fake_legs)
    wc_mod.execute_worker_branch(
        target, str(proj), _SRC, [], None, set(), False, None, None,
        "calc.gd", "res://calc.gd", "linux", None, None, {}, None, None,
        expected_scheme=WORKER_SCHEME)
    assert seen["closure"] == ("res://calc.gd",)


# --------------------------------------------------------------- 稳定路径


def test_stable_env_off_path_no_drift(tmp_path, monkeypatch):
    """GQT_MUTATION_WORKER=0 稳定路径：无父层漂移（identity=None 首次解析
    即权威）→ setup 正常产出 cold 身份，流程照常。"""
    proj, target = _fixture_project(tmp_path)
    monkeypatch.setenv("GQT_MUTATION_WORKER", "0")
    monkeypatch.setattr(runner_mod, "_core_caliper_and_early",
                        lambda *a, **k: (None, None, {}))
    monkeypatch.setattr(runner_mod, "_core_plan", lambda *a, **k: None)
    r = runner_mod._run_mutation_core(str(target), str(proj))
    assert r["ok"] is True
    assert "route_drift" not in r.get("summary", {})


def test_stable_stub_project_resolves_cold(tmp_path):
    """stub 项目（无 gut.gd）稳定路径：worker 请求仍解析 cold（既有降级
    披露语义），core_setup 身份与实跑同源 → 无漂移。"""
    from godot_qa_toolkit.mutation.worker_channel import resolve_exec_scheme

    proj, target = _fixture_project(tmp_path)
    scheme, route = resolve_exec_scheme(str(proj), "res://calc.gd", True)
    assert scheme == COLD_SCHEME
    # 静态 route 仍报 worker 资格；降级由 scheme 承载（gut.gd 缺失披露）
    assert route is not None and route.channel == "worker"


def test_matching_parent_identity_no_drift(tmp_path, monkeypatch):
    """匹配路径：父层权威=cold、实跑解析=cold（env 关断）→ 无漂移，
    身份 exec_scheme 保持一致（file_key 稳定）。"""
    proj, target = _fixture_project(tmp_path)
    monkeypatch.setenv("GQT_MUTATION_WORKER", "0")
    parent = {"exec_scheme": COLD_SCHEME}
    monkeypatch.setattr(runner_mod, "_core_caliper_and_early",
                        lambda *a, **k: (None, None, {}))
    monkeypatch.setattr(runner_mod, "_core_plan", lambda *a, **k: None)
    r = runner_mod._run_mutation_core(str(target), str(proj), identity=parent)
    assert r["ok"] is True
    assert "route_drift" not in r.get("summary", {})