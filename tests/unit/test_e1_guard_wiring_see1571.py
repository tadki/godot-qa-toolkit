"""SEE-1571 A5 hardener：E1 执行根双绑定接缝覆盖闭口（A3 移交清单）。

A3 tidy 移交的三处接线块：
- `shadowpool.py` 三处 B2a proof-guard 重建块（fresh 农场 / warm 复用枝 /
  warm 新建枝）：失配 → 重建一次 → 复检，披露形态逐键钉死；
- `executor.py` E1 守卫的 farm 槽 / 真实仓漂移重建路径（`_provision_farm_slot`
  与 `_prepare_baseline_root`）：失配重签一次、仍失配收缴 baseline（count=0）、
  制备失败退回真实仓（D5 权威不动）。

全部为测试侧替身（`shadow.create_slot` / `prewarm_proof.guard_baseline_root`），
不启动 Godot、不写真实槽。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import executor
from godot_qa_toolkit.mutation import prewarm_proof as pp
from godot_qa_toolkit.mutation import runner as runner_mod
from godot_qa_toolkit.mutation import shadowpool
from godot_qa_toolkit.mutation.stagetiming import StageClock


def _guard_sequence(monkeypatch, results):
    seq = list(results)
    seen = []

    def guard(exec_root, project_root):
        seen.append((exec_root, project_root))
        return seq.pop(0) if seq else (True, "bound")

    monkeypatch.setattr(pp, "guard_baseline_root", guard)
    return seen


# ---------------------------------------------------------------------------
# executor：真实仓 / farm 槽漂移重建
# ---------------------------------------------------------------------------

class TestPrepareBaselineRoot:
    def _globs(self):
        return {"/p/a.gd": "res://tests/", "/p/b.gd": None}

    def test_guard_inactive_is_disclosed(self, tmp_path, capsys):
        proj = tmp_path / "proj"
        proj.mkdir()
        root, slot, veto = executor._prepare_baseline_root(
            str(proj), None, StageClock(), self._globs())
        assert (root, slot, veto) == (str(proj), None, None)
        assert "baseline double-binding inactive" in capsys.readouterr().err

    def test_real_repo_drift_reissues_once(self, tmp_path, monkeypatch):
        proj = tmp_path / "proj"
        proj.mkdir()
        _guard_sequence(monkeypatch, [
            (False, "input digest mismatch at execution root"),
            (True, "bound"),
        ])
        reissues = []
        monkeypatch.setattr(pp, "reissue_after_drift",
                            lambda root: reissues.append(root))
        root, slot, veto = executor._prepare_baseline_root(
            str(proj), None, StageClock(), self._globs())
        assert (root, slot, veto) == (str(proj), None, None)
        assert reissues == [str(proj)]

    def test_real_repo_drift_persisting_withholds_baselines(
            self, tmp_path, monkeypatch, capsys):
        proj = tmp_path / "proj"
        proj.mkdir()
        _guard_sequence(monkeypatch, [
            (False, "input digest mismatch at execution root"),
            (False, "output seal mismatch at execution root"),
        ])
        monkeypatch.setattr(pp, "reissue_after_drift", lambda root: {})
        root, slot, veto = executor._prepare_baseline_root(
            str(proj), None, StageClock(), self._globs())
        assert root == str(proj) and slot is None
        baselines, failures = veto
        assert baselines == {}
        # 只对非 None glob 记 failure（全量面未预跑不入账）
        assert failures == {
            "res://tests/": ("prewarm proof drift persists: "
                             "output seal mismatch at execution root")}
        assert "baselines withheld (count=0)" in capsys.readouterr().err

    def test_veto_short_circuits_precompute_baselines(
            self, tmp_path, monkeypatch):
        proj = tmp_path / "proj"
        proj.mkdir()
        target = proj / "a.gd"
        target.write_text("func a():\n\treturn 1\n")
        monkeypatch.setattr(
            runner_mod, "_auto_scan_tests_glob",
            lambda f, root: "res://tests/")
        monkeypatch.setattr(
            executor, "_prepare_baseline_root",
            lambda project_root, knob, clock, globs: (
                project_root, None,
                ({}, {"res://tests/": "prewarm proof drift persists: x"})))
        snapshots = []
        monkeypatch.setattr(
            executor, "_run_baseline_snapshot",
            lambda root, g, t: snapshots.append(g) or {"rc": 0})
        baselines, failures = executor._precompute_baselines(
            [str(target)], str(proj), 60, None)
        assert baselines == {}
        assert failures == {"res://tests/": "prewarm proof drift persists: x"}
        assert snapshots == [], "MUST_REBUILD 轮不得启动任何 baseline"

    def test_precompute_baselines_cleans_farm_slot(self, tmp_path,
                                                   monkeypatch):
        proj = tmp_path / "proj"
        proj.mkdir()
        target = proj / "a.gd"
        target.write_text("func a():\n\treturn 1\n")
        farm = tmp_path / "farm"
        farm.mkdir()
        monkeypatch.setattr(
            runner_mod, "_auto_scan_tests_glob",
            lambda f, root: "res://tests/")
        monkeypatch.setattr(
            executor, "_prepare_baseline_root",
            lambda project_root, knob, clock, globs: (
                str(farm), farm, None))
        monkeypatch.setattr(
            executor, "_run_baseline_snapshot",
            lambda root, g, t: {"rc": 0, "tail": "", "elapsed": 1.0})
        baselines, failures = executor._precompute_baselines(
            [str(target)], str(proj), 60, None)
        assert "res://tests/" in baselines
        assert failures == {}
        assert not farm.exists(), "farm 槽必须在 finally 清理"


class TestProvisionFarmSlot:
    def _patch_create(self, monkeypatch, tmp_path, failures=()):
        created = []
        roots = [tmp_path / f"farm{n}" for n in range(3)]

        def create(project_root, kind, **kwargs):
            created.append(str(project_root))
            if len(created) <= len(failures):
                raise failures[len(created) - 1]
            path = roots[len(created) - 1]
            path.mkdir(parents=True, exist_ok=True)
            return path, {"linked": 0, "copied": 1}

        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.shadow.create_slot", create)
        return created

    def test_drift_rebuilds_once_then_binds(self, tmp_path, monkeypatch):
        created = self._patch_create(monkeypatch, tmp_path)
        _guard_sequence(monkeypatch, [(False, "input digest mismatch"),
                                     (True, "bound")])
        knob: dict = {}
        root, slot = executor._provision_farm_slot("/p", knob)
        assert len(created) == 2
        assert root == str(tmp_path / "farm1") and slot == tmp_path / "farm1"
        assert knob == {"baseline_in_farm": True}

    def test_persistent_drift_falls_back_to_real_repo(
            self, tmp_path, monkeypatch, capsys):
        self._patch_create(monkeypatch, tmp_path)
        _guard_sequence(monkeypatch, [(False, "d1"), (False, "d2")])
        knob: dict = {}
        root, slot = executor._provision_farm_slot("/p", knob)
        assert (root, slot) == ("/p", None)
        assert knob == {}, "退回真实仓不得置位 baseline_in_farm"
        assert "still drifted" in capsys.readouterr().err

    def test_provisioning_failure_falls_back_to_real_repo(
            self, tmp_path, monkeypatch, capsys):
        from godot_qa_toolkit.mutation.shadow import SlotProvisionError
        self._patch_create(monkeypatch, tmp_path,
                           failures=[SlotProvisionError("no space")])
        knob: dict = {}
        root, slot = executor._provision_farm_slot("/p", knob)
        assert (root, slot) == ("/p", None)
        assert knob == {"baseline_in_farm": False}
        assert "provisioning failed" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# shadowpool：三处 B2a proof-guard 重建块
# ---------------------------------------------------------------------------

class TestFreshFarmProofGuard:
    def _harness(self, monkeypatch, tmp_path, guards):
        roots = [tmp_path / "slot0", tmp_path / "slot1"]

        def create(project_root, kind, **kwargs):
            path = roots[len(created)]
            path.mkdir(parents=True, exist_ok=True)
            created.append(str(path))
            return path, {"linked": 0, "copied": 1}

        created: list[str] = []
        monkeypatch.setattr(shadowpool.shadow, "prune_farm",
                            lambda kind, root: None)
        monkeypatch.setattr(shadowpool.shadow, "create_slot", create)
        return _guard_sequence(monkeypatch, guards), created

    def test_drift_rebuilds_fresh_slot_once(self, monkeypatch, tmp_path):
        seen, created = self._harness(monkeypatch, tmp_path, [
            (False, "input digest mismatch at execution root"),
            (True, "bound"),
        ])
        slot = shadowpool._acquire_fresh("/p", "linux", [], False)
        assert len(created) == 2
        assert slot.root == created[-1]
        assert ("proof-drift:input digest mismatch at execution root→rebuild"
                in slot.freshness)
        assert "proof-bound" in slot.freshness
        assert len(seen) == 2

    def test_persistent_drift_is_disclosed(self, monkeypatch, tmp_path):
        self._harness(monkeypatch, tmp_path, [
            (False, "d1"), (False, "d2")])
        slot = shadowpool._acquire_fresh("/p", "linux", [], False)
        assert "proof-drift:d1→rebuild" in slot.freshness
        assert "proof-drift-persists:d2" in slot.freshness

    def test_bound_first_passes_without_rebuild(self, monkeypatch, tmp_path):
        _, created = self._harness(monkeypatch, tmp_path, [(True, "bound")])
        slot = shadowpool._acquire_fresh("/p", "linux", [], False)
        assert len(created) == 1
        assert slot.freshness == ["fresh-farm:created(linked=0,copied=1)"]


class TestWarmReuseProofGuard:
    def _harness(self, monkeypatch, tmp_path, guards):
        slot_root = tmp_path / "warm_slot"
        slot_root.mkdir()
        rec = {"path": str(slot_root), "wiring_hash": "wh",
               "last_used_at": 1.0, "in_use": False}
        pool = {"slots": {"slot-1": rec}}

        class _NoLock:
            def __init__(self, root):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(shadowpool, "_PoolLock", _NoLock)
        monkeypatch.setattr(shadowpool, "_load_pool", lambda root: pool)
        monkeypatch.setattr(shadowpool, "_prune_pool_locked",
                            lambda root, pool, now: None)
        monkeypatch.setattr(shadowpool, "_try_slot_lock",
                            lambda root, sid: 5)
        monkeypatch.setattr(shadowpool, "_sentinel_layer",
                            lambda root, slot, rec: True)
        monkeypatch.setattr(shadowpool, "_targeted_three_way",
                            lambda root, slot, rec: True)
        monkeypatch.setattr(shadowpool, "_full_census_layer",
                            lambda root, slot: True)
        monkeypatch.setattr(shadowpool, "_sync_repo_delta",
                            lambda root, slot, rec: None)
        monkeypatch.setattr(shadowpool, "_save_pool",
                            lambda root, pool: None)
        monkeypatch.setattr(shadowpool.shadow, "godot_sentinel",
                            lambda root: "sentinel-1")
        rebuilds = []

        def rebuild(root, pool, slot, rec, wiring_hash, now):
            rebuilds.append(slot.slot_id)

        monkeypatch.setattr(shadowpool, "_rebuild_locked", rebuild)
        seen = _guard_sequence(monkeypatch, guards)
        return seen, rebuilds

    def test_reuse_branch_rebuilds_on_drift(self, monkeypatch, tmp_path):
        seen, rebuilds = self._harness(monkeypatch, tmp_path, [
            (False, "output seal mismatch at execution root"),
            (True, "bound"),
        ])
        slot = shadowpool._acquire_warm("/p", [], "wh", False)
        assert rebuilds == ["slot-1"], "失配必须整槽重建一次"
        assert "proof-drift:output seal mismatch at execution root→rebuild" \
            in slot.freshness
        assert "proof-bound" in slot.freshness
        assert slot.freshness[0] == "warm:reused"
        assert len(seen) == 2

    def test_reuse_branch_persistent_drift_disclosed(
            self, monkeypatch, tmp_path):
        _, rebuilds = self._harness(monkeypatch, tmp_path, [
            (False, "d1"), (False, "d2")])
        slot = shadowpool._acquire_warm("/p", [], "wh", False)
        assert len(rebuilds) == 1
        assert "proof-drift-persists:d2" in slot.freshness


class TestWarmCreateProofGuard:
    def _harness(self, monkeypatch, tmp_path, guards):
        roots = [tmp_path / "c0", tmp_path / "c1"]

        def create(project_root, kind, **kwargs):
            path = roots[len(created)]
            path.mkdir(parents=True, exist_ok=True)
            created.append(str(path))
            return path, {"linked": 0, "copied": 1}

        created: list[str] = []
        pool = {"slots": {}}

        class _NoLock:
            def __init__(self, root):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(shadowpool, "_PoolLock", _NoLock)
        monkeypatch.setattr(shadowpool, "_load_pool", lambda root: pool)
        monkeypatch.setattr(shadowpool, "_prune_pool_locked",
                            lambda root, pool, now: None)
        monkeypatch.setattr(shadowpool, "_try_slot_lock",
                            lambda root, sid: 5)
        monkeypatch.setattr(shadowpool, "_save_pool",
                            lambda root, pool: None)
        monkeypatch.setattr(shadowpool, "_new_record",
                            lambda *a, **k: {"path": a[1],
                                             "wiring_hash": "wh"})
        monkeypatch.setattr(shadowpool.shadow, "create_slot", create)
        return _guard_sequence(monkeypatch, guards), created

    def test_create_branch_rebuilds_on_drift(self, monkeypatch, tmp_path):
        seen, created = self._harness(monkeypatch, tmp_path, [
            (False, "input digest mismatch at execution root"),
            (True, "bound"),
        ])
        slot = shadowpool._acquire_warm("/p", [], "wh", False)
        assert len(created) == 2
        assert slot.root == created[-1]
        assert "proof-drift:input digest mismatch at execution root→rebuild" \
            in slot.freshness
        assert "proof-bound" in slot.freshness
        assert len(seen) == 2

    def test_create_branch_persistent_drift_disclosed(
            self, monkeypatch, tmp_path):
        _, created = self._harness(monkeypatch, tmp_path, [
            (False, "d1"), (False, "d2")])
        slot = shadowpool._acquire_warm("/p", [], "wh", False)
        assert len(created) == 2
        assert "proof-drift-persists:d2" in slot.freshness


class TestCostPriorsFailSoft:
    """E6-5 先验面 fail-soft：损坏先验 / 无效测量 / 写失败均不得阻断。"""

    def _priors_path(self, proj: Path) -> Path:
        path = proj / ".qa-cache" / "gqt-mutation-cost-priors.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_non_object_priors_degrades_to_parallel(self, tmp_path, capsys):
        from godot_qa_toolkit.mutation import cost_priors
        proj = tmp_path / "proj"
        proj.mkdir()
        self._priors_path(proj).write_text("[1, 2, 3]", encoding="utf-8")
        assert cost_priors.load(str(proj)) is None
        assert "cost priors unreadable" in capsys.readouterr().err
        assert cost_priors.prefers_serial(str(proj), 3, 2) is False

    def test_zero_per_mutant_measurement_is_not_actionable(
            self, tmp_path, capsys):
        from godot_qa_toolkit.mutation import cost_priors
        proj = tmp_path / "proj"
        proj.mkdir()
        self._priors_path(proj).write_text(
            '{"prewarm_s": 1.0, "baseline_s": 2.0, '
            '"per_mutant_s": 0.0, "slot_fixed_s": 3.0}', encoding="utf-8")
        assert cost_priors.prefers_serial(str(proj), 3, 2) is False
        assert capsys.readouterr().err == "", "无效先验无决策线可披露"

    def test_record_write_failure_does_not_raise(
            self, tmp_path, monkeypatch, capsys):
        from godot_qa_toolkit.mutation import cost_priors

        def boom(src, dst):
            raise OSError("read-only fs")

        monkeypatch.setattr(cost_priors.os, "replace", boom)
        proj = tmp_path / "proj"
        proj.mkdir()
        cost_priors.record(str(proj), {"per_mutant": 10.0}, 2, 5)
        assert "cost priors write failed" in capsys.readouterr().err
