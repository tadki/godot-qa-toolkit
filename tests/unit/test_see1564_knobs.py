"""SEE-1564 §SPEC-203 测量专用 gated 旋钮：等价钉子 + 生效/披露断言。

红线：所有旋钮缺省 = master 现行为；GQT_MUTATION_BASELINE_IN_FARM 仅显式
opt-in（D5「真实仓 baseline 权威」默认路径不动）；钳制语义保留。
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import executor, shadow, shadowpool
from godot_qa_toolkit.mutation.executor import (
    ENV_BASELINE_IN_FARM, ENV_JOBS_CAP, default_jobs)

GD_TARGET = ("func add(a, b):\n"
             "\tif a > b:\n"
             "\t\treturn a - b\n"
             "\treturn a + b\n")


def _master_jobs_cap() -> int:
    return max(2, (os.cpu_count() or 2) // 2)


# --- GQT_MUTATION_JOBS_CAP -------------------------------------------------

def test_jobs_cap_unset_matches_master(monkeypatch):
    monkeypatch.delenv(ENV_JOBS_CAP, raising=False)
    cap = _master_jobs_cap()
    assert default_jobs(None) == cap
    assert default_jobs(cap + 10) == cap  # 上钳语义保留
    assert default_jobs(1) == 2           # 下限语义保留


def test_jobs_cap_env_override_effective_and_clamped(monkeypatch):
    monkeypatch.setenv(ENV_JOBS_CAP, "3")
    assert default_jobs(None) == 3
    assert default_jobs(99) == 3      # 显式 jobs 仍被覆写后的上限钳制
    assert default_jobs(2) == 2


def test_jobs_cap_floor_and_invalid_fall_back(monkeypatch):
    monkeypatch.setenv(ENV_JOBS_CAP, "1")
    assert default_jobs(None) == 2  # 钳制下限保留（cap>=2）
    monkeypatch.setenv(ENV_JOBS_CAP, "not-a-number")
    assert default_jobs(None) == _master_jobs_cap()  # 无效值回退默认并告警


# --- GQT_MUTATION_POOL_MAX_SLOTS ------------------------------------------

def test_pool_max_slots_unset_matches_default(monkeypatch):
    monkeypatch.delenv(shadow.ENV_POOL_MAX_SLOTS, raising=False)
    assert shadow.pool_max_slots() == shadow.DEFAULT_POOL_MAX_SLOTS


def test_pool_max_slots_env_override_with_clamp(monkeypatch):
    monkeypatch.setenv(shadow.ENV_POOL_MAX_SLOTS, "12")
    assert shadow.pool_max_slots() == 12
    monkeypatch.setenv(shadow.ENV_POOL_MAX_SLOTS, "0")
    assert shadow.pool_max_slots() == 1  # max(1, v) 钳制（budget 先例同款）
    monkeypatch.setenv(shadow.ENV_POOL_MAX_SLOTS, "abc")
    assert shadow.pool_max_slots() == shadow.DEFAULT_POOL_MAX_SLOTS


def test_prune_pool_locked_honors_env_override(tmp_path, monkeypatch):
    """warm 池 prune 按覆写后的槽数收口（缺省路径走 DEFAULT 常量不变）。"""
    monkeypatch.setenv(shadow.ENV_POOL_MAX_SLOTS, "2")
    slots = {f"slot-{i}": {"in_use": False, "last_used_at": float(i),
                           "size_bytes": 1}
             for i in range(5)}
    monkeypatch.setattr(shadowpool, "_pool_dir",
                        lambda root: tmp_path / "pool")
    monkeypatch.setattr(shadow, "pool_budget_bytes",
                        lambda: 10 * 1024 * 1024)
    pool = {"schema": shadowpool.POOL_SCHEMA, "slots": dict(slots)}
    shadowpool._prune_pool_locked(str(tmp_path), pool, 1.0)
    assert len(pool["slots"]) == 2  # LRU 最旧先出，按覆写上限收口


def test_prune_pool_locked_default_uses_constant(tmp_path, monkeypatch):
    monkeypatch.delenv(shadow.ENV_POOL_MAX_SLOTS, raising=False)
    monkeypatch.setattr(shadow, "DEFAULT_POOL_MAX_SLOTS", 3)
    slots = {f"slot-{i}": {"in_use": False, "last_used_at": float(i),
                           "size_bytes": 1}
             for i in range(6)}
    monkeypatch.setattr(shadowpool, "_pool_dir",
                        lambda root: tmp_path / "pool")
    monkeypatch.setattr(shadow, "pool_budget_bytes",
                        lambda: 10 * 1024 * 1024)
    pool = {"schema": shadowpool.POOL_SCHEMA, "slots": dict(slots)}
    shadowpool._prune_pool_locked(str(tmp_path), pool, 1.0)
    assert len(pool["slots"]) == 3  # 缺省 = monkeypatched 常量（既有 seam）


# --- GQT_MUTATION_BASELINE_IN_FARM -----------------------------------------

def _write_project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    target = proj / "calc.gd"
    target.write_text(GD_TARGET)
    return str(proj), str(target)


def test_baseline_in_farm_default_off_runs_real_repo(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_BASELINE_IN_FARM, raising=False)
    proj, _target = _write_project(tmp_path)
    captured = {}
    monkeypatch.setattr(executor, "_run_baseline_snapshot",
                        lambda root, g, t: captured.update(root=root)
                        or {"rc": 0, "tail": "pass", "elapsed": 1.0})
    knob_state: dict = {}
    baselines, failures = executor._precompute_baselines(
        ["x.gd"], proj, 60, "res://tests/", knob_state=knob_state)
    assert captured["root"] == proj  # D5：真实仓 baseline 权威不动
    assert baselines and not failures
    assert knob_state == {}


def test_baseline_in_farm_optin_runs_in_farm_slot(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_BASELINE_IN_FARM, "1")
    proj, _target = _write_project(tmp_path)
    fake_slot = tmp_path / "farm-slot"
    fake_slot.mkdir()
    calls = {"created": 0, "removed": 0}
    monkeypatch.setattr(shadow, "create_slot",
                        lambda root, kind: (calls.__setitem__(
                            "created", calls["created"] + 1),
                            fake_slot, {"linked": 1, "copied": 0})[1:])
    monkeypatch.setattr(executor.shutil, "rmtree",
                        lambda p, ignore_errors=False:
                        calls.__setitem__("removed", calls["removed"] + 1))
    captured = {}
    monkeypatch.setattr(executor, "_run_baseline_snapshot",
                        lambda root, g, t: captured.update(root=root)
                        or {"rc": 0, "tail": "pass", "elapsed": 1.0})
    knob_state: dict = {}
    baselines, _failures = executor._precompute_baselines(
        ["x.gd"], proj, 60, "res://tests/", knob_state=knob_state)
    assert calls["created"] == 1
    assert captured["root"] == str(fake_slot)  # baseline 在影子槽内实测
    assert baselines["res://tests/"]["rc"] == 0
    assert calls["removed"] == 1  # fresh 槽用完即弃，不残留
    assert knob_state["baseline_in_farm"] is True


def test_baseline_in_farm_provision_failure_falls_back_disclosed(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(ENV_BASELINE_IN_FARM, "1")
    proj, _target = _write_project(tmp_path)
    from godot_qa_toolkit.mutation.shadow import SlotProvisionError
    monkeypatch.setattr(shadow, "create_slot",
                        lambda root, kind: (_ for _ in ()).throw(
                            SlotProvisionError("disk full")))
    captured = {}
    monkeypatch.setattr(executor, "_run_baseline_snapshot",
                        lambda root, g, t: captured.update(root=root)
                        or {"rc": 0, "tail": "pass", "elapsed": 1.0})
    knob_state: dict = {}
    executor._precompute_baselines(["x.gd"], proj, 60, "res://tests/",
                                   knob_state=knob_state)
    assert captured["root"] == proj  # 降级回真实仓（不静默）
    assert knob_state["baseline_in_farm"] is False
    err = capsys.readouterr().err
    assert "baseline-in-farm" in err and "real repo" in err


def test_knob_disclosure_only_when_set(tmp_path, monkeypatch):
    from godot_qa_toolkit.mutation.executor import knobs_disclosure
    for var in (ENV_JOBS_CAP, shadow.ENV_POOL_MAX_SLOTS, ENV_BASELINE_IN_FARM):
        monkeypatch.delenv(var, raising=False)
    assert knobs_disclosure() == {}
    monkeypatch.setenv(ENV_JOBS_CAP, "6")
    monkeypatch.setenv(shadow.ENV_POOL_MAX_SLOTS, "16")
    monkeypatch.setenv(ENV_BASELINE_IN_FARM, "1")
    disc = knobs_disclosure(baseline_in_farm_effective=True)
    assert disc == {"jobs_cap": 6, "pool_max_slots": 16,
                    "baseline_in_farm": True}
