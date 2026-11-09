"""§SPEC-009：L1 默认性能层单测。

- win64/DrvFS 持久槽 warm 池（冷首建一次性，每 run git 同步 + 定向重链，
  warm 边际 ≈0）；Linux/ext4 fresh 农场；
- coverage prefilter 自动启用（store fresh ∧ 身份包全匹配，任一漂移禁用并
  披露；uncovered 不进 ok 门禁、不跑 GUT）；
- killed_by 证书精确复用（加新 pin 不失效旧 killed）；
- 单点定向同步通道；`.qa-cache` 磁盘预算/LRU/prune；
- 确定性门：warm 全复用仅一次 baseline、零执行零 baseline。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import shadow, shadowpool
from godot_qa_toolkit.mutation.coverlink import load_reusable_coverage, \
    save_coverage
from godot_qa_toolkit.mutation.identity import identity_pack
from godot_qa_toolkit.mutation.inject import detect_binary_kind
from godot_qa_toolkit.mutation import runner as mrunner
from godot_qa_toolkit.mutation.runner import run_mutation

SRC = ("func add(a, b):\n"
       "\treturn a + b\n"
       "\n"
       "func sub(a, b):\n"
       "\treturn a - b\n")
KILL_TAIL = "* test_x\n\t\ttest_x [Failed]"


def _write_project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    tests = proj / "tests"
    tests.mkdir()
    (tests / "test_x.gd").write_text("extends GutTest\n\nfunc test_x():\n\tpass\n")
    target = proj / "calc.gd"
    target.write_text(SRC)
    return proj, target


def _ident(proj):
    return identity_pack(str(proj), detect_binary_kind())


class TestWarmPool:
    def test_cold_build_then_warm_zero_cost_reuse(self, tmp_path):
        """冷首建一次；第二次 acquire 复用同一槽（warm 边际 ≈0）。"""
        proj, _ = _write_project(tmp_path)
        s1 = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                     wiring_hash="w1")
        root1 = s1.root
        assert "warm:created" in " ".join(s1.freshness)
        shadowpool.release_slot(s1)
        s2 = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                     wiring_hash="w1")
        try:
            assert Path(s2.root) == Path(root1), "warm 池复用同一槽树"
            assert "warm:reused" in " ".join(s2.freshness)
            assert "warm:created" not in s2.freshness
        finally:
            shadowpool.release_slot(s2)

    def test_each_run_single_baseline_via_shared_snapshot(self, tmp_path,
                                                          monkeypatch):
        """确定性门：warm 全复用仅一次 baseline——零执行回退必须注入父层
        共享 baseline（B 场景 GUT 调用 = 1）。"""
        proj, target = _write_project(tmp_path)
        calls = {"gut": 0}
        state = {"n": 0}

        def fake(root, timeout_s=60, tests_glob=None):
            state["n"] += 1
            calls["gut"] += 1
            if state["n"] == 1:
                return (0, "* t0\n[passed]")
            return (1, KILL_TAIL)

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake)
        r1 = run_mutation(str(target), str(proj), budget=10,
                          tests_glob="res://tests/")
        assert r1["summary"]["killed"] == 6
        assert calls["gut"] == 7, "一轮 = 1 baseline + 6 mutant 轮"
        r2 = run_mutation(str(target), str(proj), budget=10,
                          tests_glob="res://tests/")
        # 二轮：全 killed 复用 → 仅 baseline 一次（零执行零额外 baseline）
        assert r2["summary"]["resumed"] == 6
        assert calls["gut"] == 8, "二轮只增 1 次 baseline（复用零 GUT）"

    def test_warm_reuse_across_runs_keeps_pool_record(self, tmp_path):
        proj, _ = _write_project(tmp_path)
        s1 = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                     wiring_hash="w1")
        shadowpool.release_slot(s1)
        import json
        pool = json.loads((proj / ".qa-cache" / "mutation-pool" /
                           "pool.json").read_text(encoding="utf-8"))
        assert len(pool["slots"]) == 1
        rec = list(pool["slots"].values())[0]
        assert rec["in_use"] is False
        assert rec["wiring_hash"] == "w1"


class TestCoveragePrefilterAuto:
    def test_auto_enabled_with_matching_identity(self, tmp_path, monkeypatch):
        """默认自动启用：store fresh ∧ 身份包全匹配 → uncovered 过滤生效。"""
        proj, target = _write_project(tmp_path)
        save_coverage({2}, 6, str(proj), str(target), "res://tests/",
                      instrumentable_lines={2, 5}, identity=_ident(proj))
        state = {"n": 0}

        def fake(root, timeout_s=60, tests_glob=None):
            state["n"] += 1
            if state["n"] == 1:
                return (0, "* t0\n[passed]")
            return (1, KILL_TAIL)

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake)
        r = run_mutation(str(target), str(proj), budget=10,
                         tests_glob="res://tests/")
        uncovered = [f for f in r["failures"] if f["verdict"] == "uncovered"]
        assert len(uncovered) == 3, "sub 的 3 个 uncovered 位点被过滤"
        assert r["ok"] is True, "uncovered 不进 ok 门禁"

    def test_identity_drift_disables_with_disclosure(self, tmp_path,
                                                     monkeypatch, capsys):
        """任一漂移（身份不符）→ 禁用并披露（宁多跑不假阴性）。"""
        proj, target = _write_project(tmp_path)
        save_coverage({2}, 6, str(proj), str(target), "res://tests/",
                      instrumentable_lines={2, 5},
                      identity=dict(_ident(proj), gut_addon_hash="drifted"))
        state = {"n": 0}

        def fake(root, timeout_s=60, tests_glob=None):
            state["n"] += 1
            if state["n"] == 1:
                return (0, "* t0\n[passed]")
            return (1, KILL_TAIL)

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake)
        r = run_mutation(str(target), str(proj), budget=10,
                         tests_glob="res://tests/")
        assert r["summary"]["killed"] == 6, "漂移 = 不过滤全跑"
        assert "identity drift" in capsys.readouterr().err

    def test_no_reuse_coverage_flag_disables(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)
        save_coverage({2}, 6, str(proj), str(target), "res://tests/",
                      instrumentable_lines={2, 5}, identity=_ident(proj))
        state = {"n": 0}

        def fake(root, timeout_s=60, tests_glob=None):
            state["n"] += 1
            if state["n"] == 1:
                return (0, "* t0\n[passed]")
            return (1, KILL_TAIL)

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake)
        r = run_mutation(str(target), str(proj), budget=10,
                         tests_glob="res://tests/", reuse_coverage=False)
        assert r["summary"]["killed"] == 6, "--no-reuse-coverage 全跑"


class TestKilledCertExactReuse:
    def test_new_pin_does_not_invalidate_old_killed(self, tmp_path,
                                                    monkeypatch):
        """killed_by 证书精确复用：加新 pin 不失效旧 killed（零额外重跑）。"""
        proj, target = _write_project(tmp_path)
        state = {"n": 0}

        def fake(root, timeout_s=60, tests_glob=None):
            state["n"] += 1
            if state["n"] == 1:
                return (0, "* t0\n[passed]")
            return (1, KILL_TAIL)

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake)
        r1 = run_mutation(str(target), str(proj), budget=10,
                          tests_glob="res://tests/")
        assert r1["summary"]["killed"] == 6
        assert r1["summary"].get("killed_by_certificates"), \
            "killed 记录必须携带证书"
        # 新增一个测试文件（加新 pin 的 hardener 循环形态）
        (proj / "tests" / "test_new_pin.gd").write_text(
            "extends GutTest\n\nfunc test_new_pin():\n\tpass\n")
        r2 = run_mutation(str(target), str(proj), budget=10,
                          tests_glob="res://tests/")
        assert r2["summary"]["resumed"] == 6, \
            "新增测试不失效旧 killed（kill 单调性）"
        assert r2["summary"]["killed"] == 6


class TestBudgetPrune:
    def test_pool_prune_by_count(self, tmp_path, monkeypatch):
        """超槽数（缺省 8）LRU prune：最久未用未持锁槽被删。"""
        proj, _ = _write_project(tmp_path)
        monkeypatch.setattr(shadow, "DEFAULT_POOL_MAX_SLOTS", 2)
        for i in range(3):
            s = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                        wiring_hash=f"w{i}")
            shadowpool.release_slot(s)
        import json
        pool = json.loads((proj / ".qa-cache" / "mutation-pool" /
                           "pool.json").read_text(encoding="utf-8"))
        assert len(pool["slots"]) <= 2, "超槽数必须 prune"

    def test_pool_prune_by_budget(self, tmp_path, monkeypatch):
        proj, _ = _write_project(tmp_path)
        monkeypatch.setenv(shadow.ENV_POOL_BUDGET_MB, "1")
        for i in range(2):
            s = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                        wiring_hash=f"wb{i}")
            shadowpool.release_slot(s)
        import json
        pool = json.loads((proj / ".qa-cache" / "mutation-pool" /
                           "pool.json").read_text(encoding="utf-8"))
        total = sum(int(v.get("size_bytes") or 0) for v in pool["slots"].values())
        assert total <= 1 * 1024 * 1024

    def test_locked_slot_never_pruned(self, tmp_path, monkeypatch):
        proj, _ = _write_project(tmp_path)
        monkeypatch.setattr(shadow, "DEFAULT_POOL_MAX_SLOTS", 1)
        held = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                       wiring_hash="wh")
        s2 = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                     wiring_hash="wh")
        # held 持锁 → prune 不删它；s2 是同 wiring 新槽（held 占用中）
        shadowpool.release_slot(s2)
        assert Path(held.root).is_dir(), "持锁槽不得被 prune"
        shadowpool.release_slot(held)


class TestLinuxFreshFarm:
    def test_fresh_farm_per_run(self, tmp_path):
        """Linux/ext4 fresh 农场：每 run 新建、release 即删（约秒级）。"""
        proj, _ = _write_project(tmp_path)
        s1 = shadowpool.acquire_slot(str(proj), "linux", ["calc.gd"],
                                     wiring_hash="w")
        root1 = Path(s1.root)
        assert s1.mode == "fresh"
        shadowpool.release_slot(s1)
        assert not root1.exists()

    def test_targeted_channel_on_single_point(self, tmp_path, monkeypatch):
        """单点定向同步通道：targeted_only acquire 跳过全量 delta。"""
        proj, _ = _write_project(tmp_path)
        import subprocess as sp
        calls = {"git": 0}
        real_run = sp.run

        def counting_run(*a, **k):
            if a and a[0] and a[0][0] == "git":
                calls["git"] += 1
            return real_run(*a, **k)

        monkeypatch.setattr(shadow.subprocess, "run", counting_run)
        slot = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                       wiring_hash="w", targeted_only=True)
        try:
            assert calls["git"] == 0, "定向通道不付全量 git status"
        finally:
            shadowpool.release_slot(slot)
