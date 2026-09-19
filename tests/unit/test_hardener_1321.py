"""SEE-1321 SPEC-014 硬化：同文件并发对抗项 + dry-run 组合 + 移交缺陷修复用例。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import executor
from godot_qa_toolkit.mutation.runner import run_mutation
import godot_qa_toolkit.mutation.runner as runner_mod

GD_TARGET = "func add(a, b):\n\treturn a + b\n"
GD_NO_OPS = "extends Node\n\nfunc noop():\n\tpass\n"


def _write_project(tmp_path, files=("calc.gd", "other.gd")):
    proj = tmp_path / "proj"
    (proj / "addons" / "gut").mkdir(parents=True, exist_ok=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    save = proj / "tests" / "unit" / "save"
    save.mkdir(parents=True, exist_ok=True)
    (save / "test_save.gd").write_text('preload("res://calc.gd")\n')
    paths = []
    for name in files:
        p = proj / name
        p.write_text(GD_TARGET)
        paths.append(str(p))
    return str(proj), paths


class _SerialPool:
    def __init__(self, max_workers=None):
        self.max_workers = max_workers

    def map(self, fn, iterable):
        return [fn(x) for x in iterable]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def no_gut(monkeypatch):
    """GUT 调用探针——任何真实 GUT 调用都会留下记录并被本组用例断言。"""
    state = {"calls": 0}

    def probe(root, timeout_s=60, tests_glob=None):
        state["calls"] += 1
        return (0, "pass")

    monkeypatch.setattr(runner_mod, "_run_gut_on_project", probe)
    return state


@pytest.fixture()
def sealed(monkeypatch, no_gut):
    """密闭并发路径：_SerialPool + 假 baseline + 假预热。"""
    monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
    monkeypatch.setattr(
        executor, "_run_baseline_snapshot",
        lambda root, g, t: {"rc": 0, "tail": "", "elapsed": 5.0})
    monkeypatch.setattr(executor, "prewarm_import", lambda root: None)
    return no_gut


class TestDryRunCombinations:
    """Refacty 移交缺陷：dry-run×多文件/jobs 违反「只清点不跑 GUT」。"""

    def test_dry_run_multi_file_zero_gut_calls(self, tmp_path, sealed):
        """dry_run×多文件：零 GUT 调用（父层 baseline 与 worker mutant 轮都不许跑）。"""
        proj, files = _write_project(tmp_path)
        d = executor.run_mutation_files(
            files, proj, budget=3, timeout_s=60, dry_run=True,
            tests_glob="res://tests/unit/save/", jobs=4)
        assert sealed["calls"] == 0, \
            f"dry-run multi-file leaked {sealed['calls']} GUT call(s)"
        # 清点契约仍有产出
        assert d["results"]

    def test_dry_run_single_file_jobs_zero_gut_calls(self, tmp_path, sealed):
        """dry_run×单文件 jobs>1：零 GUT 调用。"""
        proj, files = _write_project(tmp_path)
        d = executor.run_mutation_files(
            [files[0]], proj, budget=3, timeout_s=60, dry_run=True,
            tests_glob="res://tests/unit/save/", jobs=4)
        assert sealed["calls"] == 0, \
            f"dry-run single-file jobs leaked {sealed['calls']} GUT call(s)"

    def test_dry_run_no_prewarm(self, tmp_path, monkeypatch, no_gut):
        """dry_run 不允许预热 import（预热也是一次真实 Godot 进程）。"""
        proj, files = _write_project(tmp_path)
        warm = {"n": 0}
        monkeypatch.setattr(
            executor, "prewarm_import",
            lambda root: warm.__setitem__("n", warm["n"] + 1))
        monkeypatch.setattr(
            executor, "_run_baseline_snapshot",
            lambda root, g, t: {"rc": 0, "tail": "", "elapsed": 5.0})
        executor.run_mutation_files(
            files, proj, budget=2, timeout_s=60, dry_run=True,
            tests_glob="res://tests/unit/save/", jobs=2)
        assert warm["n"] == 0, "dry-run must not prewarm"


class TestSliceBoundaries:
    """mutant_slice 非法区间语义对抗。"""

    def test_slice_beyond_total_is_noop(self, tmp_path, sealed):
        proj, files = _write_project(tmp_path)
        pre = {"rc": 0, "tail": "", "elapsed": 5.0}
        r = run_mutation(files[0], proj, budget=3,
                         tests_glob="res://tests/unit/save/",
                         precomputed_baseline=pre, mutant_slice=(3, 9))
        s = r["summary"]
        assert s["mutants"] == 0
        assert s["killed"] == 0 and s["survived"] == 0

    def test_slice_stop_beyond_total_clamps(self, tmp_path, sealed):
        """stop > 总数：clamp 到总数而不是抛错（边界值对抗——分片上行越界必须安全）。"""
        proj, files = _write_project(tmp_path)
        pre = {"rc": 0, "tail": "", "elapsed": 5.0}
        r = run_mutation(files[0], proj, budget=3,
                         tests_glob="res://tests/unit/save/",
                         precomputed_baseline=pre, mutant_slice=(2, 99))
        assert r["summary"]["mutants"] == 1

    def test_slice_zero_length_is_noop(self, tmp_path, sealed):
        proj, files = _write_project(tmp_path)
        pre = {"rc": 0, "tail": "", "elapsed": 5.0}
        r = run_mutation(files[0], proj, budget=3,
                         tests_glob="res://tests/unit/save/",
                         precomputed_baseline=pre, mutant_slice=(1, 1))
        assert r["summary"]["mutants"] == 0


class TestInvalidPerSlice:
    def test_invalid_mutant_counts_partition_by_slice(self, tmp_path, sealed):
        """invalid_mutant 预检随分片切片：各片 invalid 合计 = 串行 invalid。"""
        proj, files = _write_project(tmp_path)
        pre = {"rc": 0, "tail": "", "elapsed": 5.0}
        serial = run_mutation(files[0], proj, budget=3,
                              tests_glob="res://tests/unit/save/",
                              precomputed_baseline=pre)
        merged = 0
        for sl in [(0, 1), (1, 2), (2, 3)]:
            ch = run_mutation(files[0], proj, budget=3,
                              tests_glob="res://tests/unit/save/",
                              precomputed_baseline=pre, mutant_slice=sl)
            merged += ch["summary"]["invalid_mutants"]
        assert merged == serial["summary"]["invalid_mutants"]


class TestBaselineReuseInvalidation:
    def test_dirty_baseline_snapshot_rejected_to_serial(self, tmp_path, monkeypatch):
        """脏 baseline（rc != 0）快照：父层拒绝分片、文件退回串行单任务
        （worker 自跑 baseline，绝不用失真锚）。"""
        proj, files = _write_project(tmp_path)
        monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
        monkeypatch.setattr(
            executor, "_run_baseline_snapshot",
            lambda root, g, t: {"rc": 1, "tail": "noise", "elapsed": 5.0})
        monkeypatch.setattr(executor, "prewarm_import", lambda root: None)
        monkeypatch.setattr(
            runner_mod, "_run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None: (0, "pass"))
        d = executor.run_mutation_files(
            files, proj, budget=2, timeout_s=60, dry_run=False,
            tests_glob="res://tests/unit/save/", jobs=4)
        # 脏 baseline 拒绝分片：每文件 baseline_reused 必须是 False（各自自跑）
        assert all(c["summary"]["baseline_reused"] is False
                   for c in d["results"])
        assert all(c["summary"]["mutants"] == 2 for c in d["results"])


class TestSliceRunErrorNotSwallowed:
    def test_inject_failure_propagates_to_merge(self, tmp_path, monkeypatch):
        """某 worker 注入失败（GQT_INJECT_FAIL）→ 分片 mutant 记 run_error、
        合并层保留、顶层 ok=False——绝不静默吞成 survived/killed。"""
        proj, files = _write_project(tmp_path)
        monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
        monkeypatch.setattr(
            executor, "_run_baseline_snapshot",
            lambda root, g, t: {"rc": 0, "tail": "", "elapsed": 5.0})
        monkeypatch.setattr(executor, "prewarm_import", lambda root: None)

        def fake(root, timeout_s=60, tests_glob=None):
            # 首次为 baseline（无 ACTIVE_ENV）→ 干净；注入轮 → 宿主失败
            if runner_mod._ACTIVE_ENV:
                return (3, "GQT_INJECT_FAIL: cannot write impl")
            return (0, "pass")

        monkeypatch.setattr(runner_mod, "_run_gut_on_project", fake)
        d = executor.run_mutation_files(
            [files[0]], proj, budget=3, timeout_s=60, dry_run=False,
            tests_glob="res://tests/unit/save/", jobs=4)
        s = d["results"][0]["summary"]
        assert s["run_errors"] == s["mutants"], s
        assert d["ok"] is False


class TestTestIsolation:
    """测试密闭性：并发用例一律 _SerialPool，真实 ProcessPoolExecutor 创建
    即视为泄漏（编排环境挂起的第一疑因）。"""

    def test_no_real_process_pool_in_concurrent_tests(self):
        """规格化对抗：run_mutation_files 的池工厂必须经模块变量 `_worker_pool`
        间接（可被替身注入）；直连 concurrent.futures 的常量引用一旦回退就红。"""
        import inspect
        from godot_qa_toolkit.mutation import executor as ex
        src = inspect.getsource(ex.run_mutation_files)
        assert "ProcessPoolExecutor(" not in src, \
            "run_mutation_files must not instantiate ProcessPoolExecutor directly"
        assert "_worker_pool(" in src


class TestPrewarmPayload:
    def test_prewarm_passes_clamped_jobs(self, tmp_path, monkeypatch):
        """jobs 钳制在真实池调用侧生效：_worker_pool(max_workers=) 收到钳制值。"""
        proj, files = _write_project(tmp_path)
        seen = {}
        class _SpyingPool(_SerialPool):
            def __init__(self, max_workers=None):
                seen["mw"] = max_workers
        monkeypatch.setattr(executor, "_worker_pool", _SpyingPool)
        monkeypatch.setattr(
            executor, "_run_baseline_snapshot",
            lambda root, g, t: {"rc": 0, "tail": "", "elapsed": 5.0})
        monkeypatch.setattr(executor, "prewarm_import", lambda root: None)
        monkeypatch.setattr(
            runner_mod, "_run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None: (0, "pass"))
        executor.run_mutation_files(
            files, proj, budget=2, timeout_s=60, dry_run=False,
            tests_glob="res://tests/unit/save/", jobs=999)
        assert seen["mw"] == executor.default_jobs(None)
