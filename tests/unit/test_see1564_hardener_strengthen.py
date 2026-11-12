"""SEE-1564 hardener 强化钉子（仅测试侧）：SRC 变更面变异判别力收口。

覆盖：StageClock 语义（钳制/fail-loud/取整/全键披露/EXEC 键并入守卫）、
RSS 解析与诚实缺省、qa_disk_bytes 农场合计、旋钮 stderr 披露与真值解析、
信封测量接线（total/merge/prewarm/test_scan/prefilter/baseline 归因）、
_accumulate_chunk 求和与 RSS max、slot_sync 槽成本 span（含 acquire）、
worker boot 收割三态（正常/respawn/drift 重收割去重）、coverage 身份归一
写侧、复用记录 reason 保真、dry-run 零测量字段契约。

本文件只新增判别力，不改动任何既有断言语义。
"""

import itertools
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import coverlink, executor, shadow, shadowpool
from godot_qa_toolkit.mutation import worker_channel as wc_mod
from godot_qa_toolkit.mutation.coverlink import COLD_SCHEME
from godot_qa_toolkit.mutation.coverlink import coverage_identity
from godot_qa_toolkit.mutation.executor import (ENV_BASELINE_IN_FARM,
                                                ENV_JOBS_CAP,
                                                run_mutation_files)
from godot_qa_toolkit.mutation.identity import WORKER_SCHEME, identity_pack
from godot_qa_toolkit.mutation.inject import detect_binary_kind
from godot_qa_toolkit.mutation.runner import _rebuild_reused_records
from godot_qa_toolkit.mutation.stagetiming import (STAGE_KEYS, StageClock,
                                                   merge_execution_stages)
import godot_qa_toolkit.mutation.stagetiming as st_mod
import godot_qa_toolkit.mutation.runner as runner_mod

GD_TARGET = ("func add(a, b):\n"
             "\tif a > b:\n"
             "\t\treturn a - b\n"
             "\treturn a + b\n")

KILL_TAIL = "* test_killed_by_probe\n\t\t[Failed]\n"


# --- StageClock 单元语义 -----------------------------------------------------

def test_stage_clock_clamps_negative_validates_keys_rounds():
    c = StageClock()
    with pytest.raises(ValueError):
        c.add("no_such_stage", 1.0)
    c.add("test_scan", 1.5)
    c.add("test_scan", 0.5)
    c.add("per_mutant", -7.0)
    c.add("total", 1.23456)
    d = c.disclosure()
    assert set(d.keys()) == set(STAGE_KEYS)
    assert d["test_scan"] == 2.0
    assert d["per_mutant"] == 0.0  # 负值钳制为 0
    assert d["total"] == 1.235     # round 3
    assert all(v == round(v, 3) for v in d.values())


def test_merge_execution_stages_only_exec_keys_numeric_guard():
    c = StageClock()
    merge_execution_stages(c, {
        "per_mutant": 1.0, "worker_boot": 0.5, "slot_sync": 0.25,
        "baseline": 9.0, "total": 99.0, "test_scan": 5.0,
        "prewarm": 3.0, "prefilter": 7.0, "bogus": "not-a-number"})
    d = c.disclosure()
    assert d["per_mutant"] == 1.0
    assert d["worker_boot"] == 0.5
    assert d["slot_sync"] == 0.25
    # 非 EXEC 键不得并入（避免父层编排阶段双计）
    assert d["baseline"] == 0.0 and d["total"] == 0.0
    assert d["test_scan"] == 0.0 and d["prewarm"] == 0.0
    assert d["prefilter"] == 0.0
    merge_execution_stages(c, None)  # None 安全：no-op
    assert c.disclosure()["per_mutant"] == 1.0


class _FakeStatusPath:
    def __init__(self, text):
        self._text = text

    def read_text(self):
        return self._text


def test_self_rss_peak_mb_parses_vmhwm_kb_to_mb_round1(monkeypatch):
    monkeypatch.setattr(st_mod, "Path",
                        lambda _p: _FakeStatusPath(
                            "Name:\tx\nVmHWM:\t1537 kB\nThreads:\t1\n"))
    assert st_mod.self_rss_peak_mb() == 1.5  # 1537/1024 = 1.5009... → 1.5


def test_self_rss_peak_mb_honest_zero_on_failure(monkeypatch):
    def boom(_p):
        raise OSError("no procfs")
    monkeypatch.setattr(st_mod, "Path", boom)
    assert st_mod.self_rss_peak_mb() == 0.0


def test_qa_disk_bytes_sums_qa_cache_and_linux_farm(monkeypatch, tmp_path):
    import godot_qa_toolkit.mutation.shadow as shadow_mod
    farm = tmp_path / "farm"
    farm.mkdir()
    monkeypatch.setattr(shadow_mod, "dir_size_bytes", lambda _p: 7)
    monkeypatch.setattr(shadow_mod, "binary_domain_root",
                        lambda root, kind: farm)
    assert st_mod.qa_disk_bytes(str(tmp_path)) == 14  # .qa-cache 7 + farm 7


def test_qa_disk_bytes_missing_paths_are_zero(tmp_path):
    # 缺失路径按 0 计、测量面不抛（真实 shadow 函数直跑）
    assert st_mod.qa_disk_bytes(str(tmp_path)) == 0


# --- 旋钮 stderr 披露与真值解析 ---------------------------------------------

def test_jobs_cap_invalid_warns_on_stderr(monkeypatch, capsys):
    monkeypatch.setenv(ENV_JOBS_CAP, "not-a-number")
    executor.jobs_cap()
    err = capsys.readouterr().err
    assert "invalid" in err and ENV_JOBS_CAP in err


def test_jobs_cap_default_floor_under_low_cpu(monkeypatch):
    """缺省公式 max(2, cpu//2) 的下限钳制仅在低核机器有区分度（cpu<4 时
    cpu//2 < 2）——钉 cpu=2：缺省 cap 恒 2，钳制摘除即 1 红。"""
    monkeypatch.setattr(executor.os, "cpu_count", lambda: 2)
    monkeypatch.delenv(ENV_JOBS_CAP, raising=False)
    assert executor.jobs_cap() == 2
    assert executor.default_jobs(None) == 2


def test_pool_max_slots_invalid_warns_on_stderr(monkeypatch, capsys):
    monkeypatch.setenv(shadow.ENV_POOL_MAX_SLOTS, "abc")
    shadow.pool_max_slots()
    err = capsys.readouterr().err
    assert "invalid" in err and shadow.ENV_POOL_MAX_SLOTS in err


def test_baseline_in_farm_truth_table(monkeypatch):
    monkeypatch.delenv(ENV_BASELINE_IN_FARM, raising=False)
    assert executor.baseline_in_farm_requested() is False
    for off in ("0", "false", "0 "):
        monkeypatch.setenv(ENV_BASELINE_IN_FARM, off)
        assert executor.baseline_in_farm_requested() is False, off
    for on in ("1", " true "):
        monkeypatch.setenv(ENV_BASELINE_IN_FARM, on)
        assert executor.baseline_in_farm_requested() is True, on


def test_knobs_disclosure_reports_effective_not_requested(monkeypatch):
    for var in (ENV_JOBS_CAP, shadow.ENV_POOL_MAX_SLOTS):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(ENV_BASELINE_IN_FARM, "1")
    disc = executor.knobs_disclosure(baseline_in_farm_effective=False)
    assert disc == {"baseline_in_farm": False}


# --- 复用记录重建保真 --------------------------------------------------------

class _FakeMutant:
    def __init__(self, sid):
        self.stable_id = sid
        self.file = "res://x.gd"
        self.line = 1
        self.column = 1
        self.kind = "expression"
        self.operator = "expr"
        self.original = "a"
        self.mutated = "b"


def test_rebuild_reused_records_keeps_reason_and_verdict():
    from godot_qa_toolkit.mutation import reporting
    m = _FakeMutant("sid-1")
    original_src = "func a():\n\tpass\n"
    out = _rebuild_reused_records(
        {"sid-1": {"verdict": "suspect", "reason": "canary mismatch"}},
        [m], original_src)
    assert len(out) == 1
    assert out[0]["verdict"] == "suspect"
    assert out[0]["reason"] == "canary mismatch"


def test_rebuild_reused_records_skips_unknown_ids():
    original_src = "func a():\n\tpass\n"
    out = _rebuild_reused_records(
        {"ghost": {"verdict": "killed", "reason": ""}}, [], original_src)
    assert out == []


# --- 信封/聚合接线强化（完整 run 假体栈） ------------------------------------

def _write_project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "addons" / "gut").mkdir(parents=True, exist_ok=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    save = proj / "tests" / "unit" / "save"
    save.mkdir(parents=True, exist_ok=True)
    (save / "test_save.gd").write_text('preload("res://calc.gd")\n')
    target = proj / "calc.gd"
    target.write_text(GD_TARGET)
    return str(proj), str(target)


class _SerialPool:
    def __init__(self, max_workers=None):
        self.max_workers = max_workers

    def map(self, fn, iterable):
        return [fn(x) for x in iterable]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install_fakes(monkeypatch):
    monkeypatch.setattr(runner_mod, "_run_gut_on_project",
                        lambda root, timeout_s=60, tests_glob=None:
                            (1, KILL_TAIL))
    monkeypatch.setattr(runner_mod, "_run_baseline",
                        lambda root, glob, timeout_s:
                            (0, "OK. All tests passed", 1.5))
    monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
    monkeypatch.setattr(executor, "_run_baseline_snapshot",
                        lambda *a, **k: {"rc": 0, "tail": "pass",
                                         "elapsed": 5.0})
    # prewarm 假体：真实导入器在无 godot 二进制时即时失败（<0.5ms，round 3
    # 后为 0.0）——以 20ms 假耗时使 prewarm 阶段可区分摘除变异。
    monkeypatch.setattr(executor, "prewarm_import",
                        lambda root: time.sleep(0.02))


def _assert_round3(st):
    for key, v in st.items():
        assert v == round(v, 3), (key, v)


def test_envelope_stage_values_and_measurement_wiring(tmp_path, monkeypatch):
    """信封接线逐键钉死：total/merge/prewarm/test_scan/prefilter/baseline/
    slot_sync 阶段非零（cold 通道 worker_boot=0）；disk_delta =
    after−before；knobs 缺省不在信封。"""
    _install_fakes(monkeypatch)
    proj, target = _write_project(tmp_path)
    seen = {"n": 0}

    def fake_disk(_root):
        seen["n"] += 1
        return 100 if seen["n"] == 1 else 180

    monkeypatch.setattr(executor, "qa_disk_bytes", fake_disk)
    result = run_mutation_files([target], proj, budget=10, timeout_s=60,
                                tests_glob="res://tests/unit/save/", jobs=4)
    env_st = result["stage_timings"]
    assert env_st["total"] > 0.0
    assert env_st["per_mutant"] > 0.0    # 分片执行阶段已并入
    assert env_st["prewarm"] > 0.0       # 假体 20ms
    assert env_st["slot_sync"] > 0.0     # 单元 slot_sync 经 EXEC 键并入
    assert env_st["worker_boot"] == 0.0  # cold 通道无 boot 成本
    assert result["rss_peak_mb"] > 0
    assert result["rss_peak_mb"] == round(result["rss_peak_mb"], 1)
    assert result["disk_delta_bytes"] == 80  # after 180 − before 100
    assert "knobs" not in result  # 旋钮 unset → 信封无 knobs 键
    _assert_round3(env_st)
    s = result["results"][0]["summary"]
    _assert_round3(s["stage_timings"])
    assert s["rss_peak_mb"] == round(s["rss_peak_mb"], 1)


def test_unit_summary_baseline_attribution_and_stage_values(tmp_path,
                                                            monkeypatch):
    """单位级：baseline 归因 = 复用父层快照 elapsed（占比归因口径）；
    test_scan/prefilter/per_mutant/slot_sync 非零；round 3/1 钉死。"""
    _install_fakes(monkeypatch)
    proj, target = _write_project(tmp_path)
    result = run_mutation_files([target], proj, budget=10, timeout_s=60,
                                tests_glob="res://tests/unit/save/", jobs=4)
    s = result["results"][0]["summary"]
    # baseline 归因 = 复用父层快照 elapsed（5.0）；多分片逐 chunk 求和 →
    # 恒为 5.0 的正整数倍（分片数由规划决定，不钉死）。
    bl = s["stage_timings"]["baseline"]
    assert bl > 0.0 and bl % 5.0 == 0.0
    assert s["stage_timings"]["total"] > 0.0
    assert s["stage_timings"]["per_mutant"] > 0.0
    assert s["stage_timings"]["slot_sync"] > 0.0
    assert s["rss_peak_mb"] == round(s["rss_peak_mb"], 1)
    _assert_round3(s["stage_timings"])


def test_run_mutation_wraps_counted_deterministic_clock(tmp_path,
                                                        monkeypatch):
    """直接调 run_mutation（tests_glob=None）+ 全局单调钟计数器：
    runner test_scan 条件包裹、planner prefilter 包裹、baseline 归因、
    per_mutant 包裹逐一以恰好 1 tick 差钉死（摘除变异 → 0.0 即红）。
    直调路径才触发 runner 条件包裹与 _plan_serial——run_mutation_files
    分片路径下 executor 先解析 glob 且走 P3 寻址，均不经过。"""
    _install_fakes(monkeypatch)
    ticks = itertools.count(1.0, 1.0)
    monkeypatch.setattr(runner_mod.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(runner_mod, "_auto_scan_tests_glob",
                        lambda path, root: "res://tests/unit/save/")
    monkeypatch.setattr(runner_mod, "_run_cold_legs", _fake_cold_legs)
    proj, target = _write_project(tmp_path)
    result = runner_mod.run_mutation(target, proj, budget=10, timeout_s=60,
                                     tests_glob=None)
    s = result["summary"]
    assert s["stage_timings"]["test_scan"] == 1.0
    assert s["stage_timings"]["prefilter"] == 1.0
    assert s["stage_timings"]["baseline"] == 1.5  # 假体实测 elapsed
    assert s["stage_timings"]["per_mutant"] == 1.0
    assert s["stage_timings"]["total"] > 0.0


def test_dry_run_envelope_has_no_measurement_fields(tmp_path, monkeypatch):
    _install_fakes(monkeypatch)
    proj, target = _write_project(tmp_path)
    result = run_mutation_files([target], proj, budget=10, timeout_s=60,
                                tests_glob="res://tests/unit/save/", jobs=4,
                                dry_run=True)
    assert "stage_timings" not in result
    assert "rss_peak_mb" not in result
    assert "disk_delta_bytes" not in result
    assert "knobs" not in result


def test_single_file_knobs_projection_on_env_set(tmp_path, monkeypatch):
    """旋钮 set 时单文件 flat summary 投影 knobs（BDD flat 契约的 set 态）。"""
    _install_fakes(monkeypatch)
    monkeypatch.setenv(ENV_JOBS_CAP, "6")
    proj, target = _write_project(tmp_path)
    result = run_mutation_files([target], proj, budget=10, timeout_s=60,
                                tests_glob="res://tests/unit/save/", jobs=4)
    assert result["knobs"] == {"jobs_cap": 6}
    assert result["results"][0]["summary"]["knobs"] == {"jobs_cap": 6}


def test_file_summary_rss_round1_pinned(tmp_path, monkeypatch):
    """文件级聚合 rss 取整钉死：假体 VmHWM 1.23456789 → round(·,1)=1.2
    （真实 VmHWM 常为整 MB，round(·,1)==x 恒真——值依赖假阴性，故以假值
    钉结构）。摘除取整即红。"""
    _install_fakes(monkeypatch)
    monkeypatch.setattr(runner_mod, "self_rss_peak_mb", lambda: 1.23456789)
    proj, target = _write_project(tmp_path)
    result = run_mutation_files([target], proj, budget=10, timeout_s=60,
                                tests_glob="res://tests/unit/save/", jobs=4)
    s = result["results"][0]["summary"]
    assert s["rss_peak_mb"] == 1.2


def test_accumulate_chunk_sums_stages_and_maxes_rss():
    agg = {"file": "x", "mutants": 0, "killed": 0, "survived": 0,
           "suspect": 0, "timeout": 0, "run_errors": 0, "invalid_mutants": 0,
           "resumed": 0, "timeout_pinned": 0, "executed": None, "jobs": 0,
           "ok": True, "failures": [], "scoped": None, "budget": None,
           "baseline_reused": None, "total_sites": None, "attention": None,
           "freshness": [], "killed_by_certificates": [],
           "suspected_orphans": [], "stage_timings": {}, "rss_peak_mb": 0.0}
    executor._accumulate_chunk(agg, {"summary": {
        "stage_timings": {"per_mutant": 1.0, "slot_sync": 0.5},
        "rss_peak_mb": 5.0}}, ())
    executor._accumulate_chunk(agg, {"summary": {
        "stage_timings": {"per_mutant": 2.0, "slot_sync": 0.25},
        "rss_peak_mb": 3.0}}, ())
    assert agg["stage_timings"]["per_mutant"] == 3.0   # 跨分片求和
    assert agg["stage_timings"]["slot_sync"] == 0.75
    assert agg["rss_peak_mb"] == 5.0                   # 取 max 非覆盖/min




# --- baseline-in-farm 阶段归因（确定性时钟） ---------------------------------

def test_precompute_baselines_baseline_stage_includes_provisioning(
        tmp_path, monkeypatch):
    """opt-in 时 baseline 阶段含 farm 槽制备成本（外层包裹语义）。
    确定性单调钟调度：test_scan enter/exit（0.0/0.0）、outer baseline enter
    0.0、(制备) inner enter 5.0、snapshot、inner exit 8.0（chain 接管）、
    outer exit 9.0。嵌套同名 stage 求和 → 披露 = outer(9.0) + inner(3.0)
    = 12.0。外层摘除 → 3.0；内层摘除 → 9.0——断言 12.0 双杀。"""
    ticks = itertools.chain([0.0, 0.0, 0.0, 5.0], itertools.count(8.0, 1.0))
    monkeypatch.setattr(st_mod.time, "monotonic", lambda: next(ticks))
    monkeypatch.setenv(ENV_BASELINE_IN_FARM, "1")
    proj = str(tmp_path / "proj")
    fake_slot = tmp_path / "farm-slot"
    fake_slot.mkdir()
    monkeypatch.setattr(shadow, "create_slot",
                        lambda root, kind: (None, fake_slot, {})[1:])
    monkeypatch.setattr(executor, "_run_baseline_snapshot",
                        lambda root, g, t: {"rc": 0, "tail": "pass",
                                            "elapsed": 1.0})
    knob_state: dict = {}
    clock = StageClock()
    baselines, failures = executor._precompute_baselines(
        ["x.gd"], proj, 60, "res://tests/", clock=clock,
        knob_state=knob_state)
    assert baselines and not failures
    assert knob_state["baseline_in_farm"] is True
    assert clock.disclosure()["baseline"] == 12.0  # outer(9)+inner(3)
    assert not fake_slot.exists()                  # fresh 槽用完即弃


def test_baseline_failure_reason_key_uses_actual_root(tmp_path, monkeypatch):
    """失败归因键用实际执行 root（opt-in 时 = farm 槽）：快照 rc≠0 时从
    同源登记表命中映射原因；键源改写（回真实仓根）即落通用兜底 → 红。"""
    monkeypatch.setenv(ENV_BASELINE_IN_FARM, "1")
    proj = str(tmp_path / "proj")
    fake_slot = tmp_path / "farm-slot"
    fake_slot.mkdir()
    monkeypatch.setattr(shadow, "create_slot",
                        lambda root, kind: (None, fake_slot, {})[1:])
    monkeypatch.setattr(executor, "_run_baseline_snapshot",
                        lambda root, g, t: {"rc": 1, "tail": "boom",
                                            "elapsed": 0.5})
    monkeypatch.setattr(executor, "_baseline_key",
                        lambda root, g: f"{root}|{g}")
    monkeypatch.setattr(executor, "_BASELINE_FAILURE_REASONS",
                        {f"{fake_slot}|res://tests/": "farm-mapped-reason"})
    knob_state: dict = {}
    baselines, failures = executor._precompute_baselines(
        ["x.gd"], proj, 60, "res://tests/", knob_state=knob_state)
    assert not baselines
    assert failures == {"res://tests/": "farm-mapped-reason"}
    assert knob_state["baseline_in_farm"] is True


# --- slot_sync 槽成本 span（含 acquire） -------------------------------------

def _fake_cold_legs(path, project_root, original_src, backup, mutants,
                    tests_glob, baseline_failures, baseline_dirty, slot,
                    auto_name, target_rel, target_res, ownership_fn,
                    reporter, store2, prior_entries, scopes):
    from godot_qa_toolkit.mutation.reporting import _mutant_record
    return [(m, _mutant_record(m, "killed", "", src=original_src))
            for m in mutants]


def _install_slot_fakes(monkeypatch, proj):
    """冷腿假体 + 槽假体 + 全局单调钟计数器（1.0 步进）。

    tick 记账（pristine）：t_slot=1.0 → acquire 假体 3 tick（2.0/3.0/4.0，
    span 内证据）→ stage enter 5.0 / exit 6.0 → add 处 7.0 → slot_sync =
    7.0−1.0 = 6.0；per_mutant = 6.0−5.0 = 1.0。span 平移变异（t_slot 移到
    acquire 后）：t_slot=4.0 → slot_sync = 7.0−4.0 = 3.0 ≠ 6.0 红。摘除
    变异 → 0 红。seed_beacon/resolve_exec_scheme 假体零 tick。"""
    ticks = itertools.count(1.0, 1.0)
    monkeypatch.setattr(runner_mod.time, "monotonic", lambda: next(ticks))

    def fake_acquire(root, kind, rel, h, targeted_only=False):
        for _ in range(3):  # acquire 消耗 3 tick（制备成本进 span）
            runner_mod.time.monotonic()
        slot_dir = Path(proj) / "fake-slot"
        slot_dir.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(root=str(slot_dir), project_root=proj,
                               mode="fresh", kind=kind, freshness=[])

    monkeypatch.setattr(shadowpool, "acquire_slot", fake_acquire)
    monkeypatch.setattr(shadowpool, "release_slot", lambda slot: [])
    monkeypatch.setattr(runner_mod, "resolve_exec_scheme",
                        lambda *a, **k: ("cold", None))
    monkeypatch.setattr(runner_mod.beacon, "seed_beacon_script",
                        lambda root: None)
    monkeypatch.setattr(runner_mod, "_run_cold_legs", _fake_cold_legs)
    monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
    monkeypatch.setattr(executor, "_run_baseline_snapshot",
                        lambda *a, **k: {"rc": 0, "tail": "pass",
                                         "elapsed": 5.0})
    monkeypatch.setattr(runner_mod, "_run_gut_on_project",
                        lambda root, timeout_s=60, tests_glob=None:
                            (1, KILL_TAIL))


def test_slot_sync_span_includes_acquire_cost(tmp_path, monkeypatch):
    """slot_sync span 必须含槽获取成本（确定性钟，2 分片恒定）。

    tick 记账：t_slot=1.0 → acquire 3 tick → stage enter 5.0 / exit 6.0 →
    add 处 7.0 → 每分片 slot_sync=6.0、per_mutant=1.0。span 平移变异
    （t_slot 移到 acquire 后）→ slot_sync=3.0/分片；摘除 → 0——即红。
    附带：envelope test_scan=3.0（precompute 1 + 2 分片 plan）、envelope
    prefilter=2.0、unit prefilter=2.0——三处 stage 包裹摘除变异即红。"""
    proj_root = tmp_path / "projbase"
    _install_slot_fakes(monkeypatch, str(proj_root))
    proj, target = _write_project(tmp_path)
    result = run_mutation_files([target], proj, budget=10, timeout_s=60,
                                tests_glob="res://tests/unit/save/", jobs=2)
    s = result["results"][0]["summary"]
    assert s["stage_timings"]["per_mutant"] == 2.0    # 2 分片 × 1 tick
    assert s["stage_timings"]["slot_sync"] == 12.0    # 2 分片 × 6.0
    env_st = result["stage_timings"]
    # _plan_file_chunks 按文件调用（非按分片）：envelope test_scan =
    # precompute(1.0) + plan(1.0) = 2.0；prefilter = executor 包裹 1.0。
    assert env_st["test_scan"] == 2.0
    assert env_st["prefilter"] == 1.0


# --- worker boot 收割语义（run_worker_legs 假体栈） --------------------------

class _FakeLeg:
    def __init__(self, fatal=False, state_drift=False):
        self.fatal = fatal
        self.state_drift = state_drift
        self.gut_ran = True
        self.fatal_reason = "fake session fatal"


class _FakeSession:
    def __init__(self, boot):
        self.boot_s = boot
        self.closed = False
        self.dead = False
        self._leg = _FakeLeg()

    def close(self):
        self.closed = True

    def run_leg(self, seq, target_res, mutant_sha, closure, tests_glob):
        return self._leg


def _run_legs(monkeypatch, mutants, boots, timings_out, legs=None):
    """run_worker_legs 全假体：parity 门直通、canary 关、注入零错。

    boots = 逐会话 boot_s 值（超出部分 0.0）；legs = 逐会话 _leg（缺省
    健康腿）。返回 (sessions, pairs, fatal_events)。"""
    sessions: list = []
    leg_it = iter(legs or [])

    def fake_session(slot_root, project_root, kind, godot_bin=None,
                     leg_timeout_s=60):
        boot = boots[len(sessions)] if len(sessions) < len(boots) else 0.0
        s = _FakeSession(boot)
        try:
            s._leg = next(leg_it)
        except StopIteration:
            pass
        sessions.append(s)
        return s

    monkeypatch.setattr(wc_mod, "WorkerSession", fake_session)
    monkeypatch.setattr(wc_mod, "_parity_gate_leg",
                        lambda *a, **k: (False, None))
    monkeypatch.setattr(wc_mod, "canary_due", lambda seq: False)

    def inject_fn(_m):
        return "sha", None

    killed = {"verdict": "killed", "reason": "", "elapsed_s": 0.1}

    def cold_runner(_m, _seq):
        return dict(killed)

    def classify_fn(_m, _leg):
        return dict(killed)

    def record_fn(_m, _verdict, _elapsed=0.0):
        pass

    pairs, fatal = wc_mod.run_worker_legs(
        mutants, SimpleNamespace(root="/tmp/unused", project_root="/tmp"),
        "res://x.gd", "res://x.gd", ["res://dep.gd"], "linux",
        "res://tests/", inject_fn, cold_runner, record_fn, classify_fn, 60,
        baseline_failures=set(), parity_inject_fn=None,
        timings_out=timings_out)
    return sessions, pairs, fatal


def test_runner_worker_boot_harvest_counted(tmp_path, monkeypatch):
    """runner 把 worker 会话 boot 收割进 summary（worker_boot add 摘除 →
    0 红）：worker 路由 + shim 写 timings_out → 确定值入账。"""
    _install_fakes(monkeypatch)
    ticks = itertools.count(1.0, 1.0)
    monkeypatch.setattr(runner_mod.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(runner_mod, "resolve_exec_scheme",
                        lambda *a, **k: (WORKER_SCHEME, None))

    def shim(path, project_root, original_src, mutants, tests_glob,
             baseline_failures, baseline_dirty, slot, auto_name,
             target_rel, target_res, kind, reporter, store2,
             prior_entries, scopes, ownership_fn, timings_out=None):
        if timings_out is not None:
            timings_out["worker_boot_s"] = 2.5
        return _fake_cold_legs(
            path, project_root, original_src, "", mutants, tests_glob,
            baseline_failures, baseline_dirty, slot, auto_name, target_rel,
            target_res, ownership_fn, reporter, store2, prior_entries,
            scopes), []

    monkeypatch.setattr(wc_mod, "execute_worker_branch", shim)

    def fake_acquire(root, kind, rel, h, targeted_only=False):
        slot_dir = tmp_path / "wslot"
        slot_dir.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(root=str(slot_dir), project_root=str(tmp_path),
                               mode="fresh", kind=kind, freshness=[])

    monkeypatch.setattr(shadowpool, "acquire_slot", fake_acquire)
    monkeypatch.setattr(shadowpool, "release_slot", lambda slot: [])
    monkeypatch.setattr(runner_mod.beacon, "seed_beacon_script",
                        lambda root: None)
    proj, target = _write_project(tmp_path)
    result = run_mutation_files([target], proj, budget=10, timeout_s=60,
                                tests_glob="res://tests/unit/save/", jobs=2)
    s = result["results"][0]["summary"]
    # 2 分片 × shim 2.5 逐 chunk 收割求和
    assert s["stage_timings"]["worker_boot"] == 5.0


def test_worker_boot_harvest_rounds_and_happy_path(monkeypatch):
    timings = {}
    sessions, pairs, fatal = _run_legs(monkeypatch, ["m1"], [1.5004],
                                       timings)
    assert len(pairs) == 1 and fatal == []
    assert timings["worker_boot_s"] == 1.5  # round 3
    assert sessions[-1].closed is True      # finally 收割 + close


def test_worker_boot_respawn_harvest_sums_both_sessions(monkeypatch):
    """会话致命 → 旧会话 boot 收割 + respawn 新会话 → 合计两段（缺旧会话
    收割位点即 1.5 ≠ 2.5 红）。逐次收割各自 round 3：1.5 + 1.0 = 2.5。"""
    timings = {}
    fatal_leg = _FakeLeg(fatal=True, state_drift=False)
    ok_leg = _FakeLeg()
    _sessions, pairs, fatal = _run_legs(
        monkeypatch, ["m1", "m2"], [1.5004, 1.0004], timings,
        legs=[fatal_leg, ok_leg])
    assert timings["worker_boot_s"] == 2.5
    assert len(fatal) == 1


def test_worker_boot_drift_harvest_dedup(monkeypatch):
    """drift 致命收割 + finally 对同一会话再收割 → 清零防重复计数（合计
    恒 1.5；收割后不清零即 3.001 红）。"""
    timings = {}
    drift_leg = _FakeLeg(fatal=True, state_drift=True)
    _sessions, pairs, fatal = _run_legs(
        monkeypatch, ["m1"], [1.5004], timings, legs=[drift_leg])
    assert timings["worker_boot_s"] == 1.5


def test_worker_session_start_accumulates_boot(monkeypatch, tmp_path):
    """WorkerSession.start 真体：spawn 前后单调钟差入 boot_s（收割源头）。
    mutant_env 钉死空 env——真实实现按二进制探测走 win64 分支时会经
    wslpath 拉真子进程（环境敏感，不属本钉子对象）。"""
    import godot_qa_toolkit.mutation.inject as inject_mod
    slot_root = tmp_path / "slot"
    slot_root.mkdir()
    (slot_root / "project.godot").write_text("[application]\n")
    proj = tmp_path / "projroot"
    proj.mkdir()
    sess = wc_mod.WorkerSession(str(slot_root), str(proj), "linux",
                                godot_bin="/fake/godot", leg_timeout_s=60)
    ticks = iter([0.0, 2.5])
    monkeypatch.setattr(wc_mod.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(inject_mod, "mutant_env",
                        lambda cfg_fs, user_dir: ({}, None))

    class _FakeGate:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(wc_mod, "GlobalGodotGate", _FakeGate)
    monkeypatch.setattr(wc_mod.subprocess, "Popen",
                        lambda *a, **k: SimpleNamespace(
                            poll=lambda: None, pid=4321,
                            wait=lambda timeout=None: 0))
    monkeypatch.setattr(type(sess), "_await_boot", lambda self: None)
    sess.start()
    assert sess.boot_s == 2.5
    sess.close()


# --- coverage 身份归一（写侧） -----------------------------------------------

def test_save_coverage_normalizes_identity_to_cold(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    target = proj / "calc.gd"
    target.write_text("func add(a, b):\n\treturn a + b\n")
    cold = identity_pack(str(proj), detect_binary_kind())
    worker_ident = {**cold, "exec_scheme": WORKER_SCHEME}
    assert coverlink.save_coverage(
        {2}, 6, str(proj), str(target), "res://tests/",
        instrumentable_lines={2, 5}, identity=worker_ident) is True
    cache = coverlink.coverage_cache_path(str(proj), str(target),
                                          "res://tests/",
                                          detect_binary_kind())
    doc = json.loads(cache.read_text(encoding="utf-8"))
    assert doc["identity"]["exec_scheme"] == COLD_SCHEME
    # 读侧以 cold 身份命中（写侧归一的闭环证据）
    lines, reason = coverlink.load_reusable_coverage(
        str(proj), str(target), "res://tests/",
        binary_kind=detect_binary_kind(), identity=cold)
    assert lines == {2} and reason is None


def test_coverage_identity_worker_to_cold_and_none():
    ident = {"ops_version": "x", "exec_scheme": WORKER_SCHEME}
    assert coverage_identity(ident) == {**ident, "exec_scheme": COLD_SCHEME}
    assert coverage_identity(None) is None


# --- D-1 守卫：父层身份解析必须真实消费 worker_flag --------------------------

def test_d1_parent_identity_honors_worker_flag(tmp_path, monkeypatch):
    """identity_for_target 的 worker_flag 必须真实参与身份解析：强制 False
    时父层 manifest 落 cold 键、worker 记录落 worker 键 → 恰两个 file_key
    目录即红（吞 flag 的路由假体会使该变异不可见——flag 必须感知）。"""
    from godot_qa_toolkit.mutation.identity import COLD_SCHEME

    proj = tmp_path / "proj"
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    save = proj / "tests" / "unit" / "save"
    save.mkdir(parents=True)
    (save / "test_save.gd").write_text(
        'preload("res://calc.gd")\n\n'
        "func test_killed_by_probe():\n\tpass\n")
    target = proj / "calc.gd"
    target.write_text(GD_TARGET)

    def flag_aware(project_root, target_res, flag=None):
        if flag is False:
            return (COLD_SCHEME, None)
        return (WORKER_SCHEME, None)

    monkeypatch.setattr(wc_mod, "resolve_exec_scheme", flag_aware)
    monkeypatch.setattr(runner_mod, "resolve_exec_scheme", flag_aware)

    def shim(path, project_root, original_src, mutants, tests_glob,
             baseline_failures, baseline_dirty, slot, auto_name,
             target_rel, target_res, kind, reporter, store2,
             prior_entries, scopes, ownership_fn, timings_out=None):
        raw = runner_mod._run_cold_legs(
            path, project_root, original_src, "", mutants, tests_glob,
            baseline_failures, baseline_dirty, slot, auto_name, target_rel,
            target_res, ownership_fn, reporter, store2, prior_entries, scopes)
        return raw, []

    monkeypatch.setattr(wc_mod, "execute_worker_branch", shim)
    monkeypatch.setattr(runner_mod, "_run_gut_on_project",
                        lambda root, timeout_s=60, tests_glob=None:
                            (1, KILL_TAIL))
    monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
    monkeypatch.setattr(executor, "_run_baseline_snapshot",
                        lambda *a, **k: {"rc": 0, "tail": "pass",
                                         "elapsed": 5.0})

    result = run_mutation_files([str(target)], str(proj), budget=10,
                                timeout_s=60,
                                tests_glob="res://tests/unit/save/", jobs=2)
    base = Path(proj) / ".qa-cache" / "gqt-mutation-resume-v2"
    keys = {p.name for p in base.iterdir() if p.is_dir()} \
        if base.is_dir() else set()
    assert len(keys) == 1, (
        "records and manifest must share one file_key when the parent "
        f"resolves the worker channel (D-1 flag must be honored: {sorted(keys)})")
