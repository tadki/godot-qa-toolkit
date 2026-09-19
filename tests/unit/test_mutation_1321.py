"""SEE-1321 SPEC-003~009：内存注入 / XDG 隔离 / baseline 复用 / 契约兼容单测。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import executor
from godot_qa_toolkit.mutation.runner import run_mutation
import godot_qa_toolkit.mutation.runner as runner_mod

GD_TARGET = "func add(a, b):\n\treturn a + b\n"


def _write_project(tmp_path, files=("calc.gd",)):
    proj = tmp_path / "proj"
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    tests = proj / "tests" / "unit" / "save"
    tests.mkdir(parents=True)
    (tests / "test_save.gd").write_text('preload("res://calc.gd")\n')
    paths = []
    for name in files:
        p = proj / name
        p.write_text(GD_TARGET)
        paths.append(str(p))
    return str(proj), paths


@pytest.fixture()
def fake_gut(monkeypatch):
    """默认 baseline 通过、每 mutant 通过；记录每轮进程窗口的活跃 env。"""
    state = {"calls": [], "globs": []}

    def fake_run(root, timeout_s=60, tests_glob=None):
        state["calls"].append(runner_mod._ACTIVE_ENV)
        state["globs"].append(tests_glob)
        return (0, "pass")

    monkeypatch.setattr(runner_mod, "_run_gut_on_project", fake_run)
    return state


class TestSpec006XdgIsolation:
    def test_xdg_env_injected_per_process(self, tmp_path, monkeypatch, fake_gut):
        """SPEC-006：每个 Godot 子进程注入 per-process XDG_DATA_HOME。"""
        proj, files = _write_project(tmp_path)
        import godot_qa_toolkit.mutation.inject as inject_mod
        # 本测试中断言 WSL 侧绝对路径；win64 转换在真机 subprocess 层生效
        monkeypatch.setattr(inject_mod, "_win_path", lambda p: p)
        run_mutation(files[0], proj, budget=2)
        envs = [c for c in fake_gut["calls"] if c]
        assert envs, "mutant loop must populate _ACTIVE_ENV"
        xdg_values = {e["XDG_DATA_HOME"] for e in envs if "XDG_DATA_HOME" in e}
        assert xdg_values, "XDG_DATA_HOME must be present in child env"
        for v in xdg_values:
            assert Path(v).is_absolute()

    def test_xdg_inject_failure_falls_back(self, tmp_path, monkeypatch, fake_gut):
        """SPEC-006 兜底：user 目录创建失败 → env 无 XDG 但不崩溃。"""
        proj, files = _write_project(tmp_path)
        import godot_qa_toolkit.mutation.inject as inject_mod
        monkeypatch.setattr(inject_mod, "make_user_dir", lambda: None)
        result = run_mutation(files[0], proj, budget=2)
        assert result["summary"]["mutants"] == 2
        assert all("XDG_DATA_HOME" not in (c or {}) for c in fake_gut["calls"])

    def test_mutant_loop_passes_host_script(self, tmp_path, monkeypatch):
        proj, files = _write_project(tmp_path)
        scripts = []
        orig = runner_mod._run_gut_on_project

        def fake(root, timeout_s=60, tests_glob=None):
            scripts.append(runner_mod._ACTIVE_SCRIPT)
            return (0, "pass")

        monkeypatch.setattr(runner_mod, "_run_gut_on_project", fake)
        run_mutation(files[0], proj, budget=2)
        # 宿主脚本按 PID 隔离命名（.gqt_mutation_host_<pid>.gd）
        assert any(".gqt_mutation_host_" in s and s.endswith(".gd")
                   for s in scripts)

    def test_no_env_leakage_after_loop(self, tmp_path, monkeypatch):
        proj, files = _write_project(tmp_path)
        run_mutation(files[0], proj, budget=1)
        assert runner_mod._ACTIVE_ENV is None
        assert runner_mod._ACTIVE_SCRIPT == runner_mod.GUT_SCRIPT_RES_PATH


class TestSpec007MemoryInjection:
    def test_target_disk_never_mutated_during_loops(self, tmp_path, monkeypatch):
        """SPEC-007：mutant 循环中磁盘目标文件全程保持原始内容。"""
        proj, files = _write_project(tmp_path)
        target = Path(files[0])
        original = target.read_text()
        snapshot = {}

        def disk_guard(root, timeout_s=60, tests_glob=None):
            snapshot["at_run"] = target.read_text()
            if "first" not in snapshot:
                snapshot["first"] = True
                return (0, "pass")
            return (1, "* test_kill_x\n    [Failed]: caught\n")

        monkeypatch.setattr(runner_mod, "_run_gut_on_project", disk_guard)
        result = run_mutation(files[0], proj, budget=2)
        assert snapshot["at_run"] == original, "disk must stay original"
        assert result["summary"]["killed"] == result["summary"]["mutants"]

    def test_mutated_source_in_cfg_matches_apply(self, tmp_path, monkeypatch):
        """SPEC-007：内存变异体 = _apply_mutation 结果传给宿主 cfg。"""
        import json
        proj, files = _write_project(tmp_path)
        cfgs = []
        import godot_qa_toolkit.mutation.inject as inject_mod
        # cfg 路径关掉 wslpath 转换（真机才需要 win64 形式，本测试用 Linux 侧读回）
        monkeypatch.setattr(inject_mod, "_win_path", lambda p: p)

        def capture(root, timeout_s=60, tests_glob=None):
            if runner_mod._ACTIVE_ENV:
                cfgs.append(json.loads(
                    Path(runner_mod._ACTIVE_ENV["GQT_MUTATION_CFG"]).read_text()))
            return (0, "pass")

        monkeypatch.setattr(runner_mod, "_run_gut_on_project", capture)
        run_mutation(files[0], proj, budget=2)
        assert cfgs, "mutant host cfg must be handed over"
        assert cfgs[0]["target_res"].endswith("/calc.gd")
        assert "mutated_source" in cfgs[0]


class TestSpec005AdaptiveTimeoutAnchor:
    def test_precomputed_baseline_anchors_timeout(self, tmp_path, monkeypatch):
        proj, files = _write_project(tmp_path)
        monkeypatch.setattr(
            runner_mod, "_run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None: (0, "pass"))
        pre = {"rc": 0, "tail": "", "elapsed": 10.0}
        result = run_mutation(files[0], proj, budget=1, timeout_s=20,
                              precomputed_baseline=pre)
        # 10*2+1：锚 = 非并发单进程 baseline 耗时 × k
        assert result["summary"]["adaptive_timeout_s"] == 21
        assert result["summary"]["baseline_reused"] is True


class TestSpec003Jobs:
    def test_jobs_default_clamp(self):
        import os
        cpu = os.cpu_count() or 2
        assert executor.default_jobs(None) == max(2, cpu // 2)

    def test_jobs_oversize_clamped(self):
        assert executor.default_jobs(999) == executor.default_jobs(None)

    def test_jobs_zero_positive_floor(self):
        assert executor.default_jobs(0) >= 2

    def test_prewarm_called_for_concurrent(self, tmp_path, monkeypatch):
        proj, files = _write_project(tmp_path, files=("calc.gd", "other.gd"))
        calls = {"n": 0}
        monkeypatch.setattr(
            executor, "prewarm_import", lambda root: calls.__setitem__("n", calls["n"] + 1))
        monkeypatch.setattr(
            executor, "_run_baseline_snapshot", lambda root, g, t: None)
        # 串行假 executor（避免真进程池跑真 godot）；worker 路由回本进程的
        # mock seam（真 worker 是独立进程、丢失 monkeypatch）
        with monkeypatch.context() as mp:
            mp.setattr(executor, "_worker_pool", _SerialPool)
            mp.setattr(runner_mod, "_run_gut_on_project",
                       lambda root, timeout_s=60, tests_glob=None: (0, "pass"))
            result = executor.run_mutation_files(
                files, proj, budget=2, timeout_s=60, dry_run=False,
                tests_glob="res://tests/unit/save/", jobs=2)
        assert calls["n"] == 1
        assert result["jobs"] == 2

    def test_no_prewarm_for_serial(self, tmp_path, monkeypatch):
        proj, files = _write_project(tmp_path)
        calls = {"n": 0}
        monkeypatch.setattr(
            executor, "prewarm_import", lambda root: calls.__setitem__("n", calls["n"] + 1))
        executor.run_mutation_files(files, proj, budget=1, timeout_s=60,
                                    dry_run=False, tests_glob=None, jobs=1)
        assert calls["n"] == 0


class _SerialPool:
    """进程池替身：当前进程顺序执行任务映射。"""

    def __init__(self, max_workers=None):
        pass

    def map(self, fn, iterable):
        return [fn(x) for x in iterable]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestSpec008BaselineReuse:
    def test_baseline_shared_across_files_same_glob(self, tmp_path, monkeypatch):
        proj, files = _write_project(tmp_path, files=("calc.gd", "other.gd"))
        monkeypatch.setattr(executor, "prewarm_import", lambda root: None)
        calls = {"baseline": 0}

        def fake(root, timeout_s=60, tests_glob=None):
            if runner_mod._ACTIVE_ENV is None:
                calls["baseline"] += 1
            return (0, "pass")

        monkeypatch.setattr(runner_mod, "_run_gut_on_project", fake)
        result = executor.run_mutation_files(
            files, proj, budget=1, timeout_s=60, dry_run=False,
            tests_glob="res://tests/unit/save/", jobs=1)
        assert calls["baseline"] == 1, "same-glob baseline runs once"


class TestSpec009ContractCompat:
    def test_single_file_shape_unchanged(self, tmp_path, monkeypatch):
        proj, files = _write_project(tmp_path)
        monkeypatch.setattr(
            runner_mod, "_run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None: (0, "pass"))
        result = run_mutation(files[0], proj, budget=1)
        assert set(result) == {"tool", "ok", "summary", "failures"}
        assert result["summary"]["file"].endswith("calc.gd")

    def test_new_fields_only_added(self, tmp_path, monkeypatch):
        proj, files = _write_project(tmp_path)
        monkeypatch.setattr(
            runner_mod, "_run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None: (0, "pass"))
        s = run_mutation(files[0], proj, budget=1)["summary"]
        assert "scoped" in s and "baseline_reused" in s


class TestSpec001AutoScan:
    def test_auto_scan_scopes_when_refs_found(self, tmp_path, monkeypatch, fake_gut):
        proj, files = _write_project(tmp_path)
        run_mutation(files[0], proj, budget=1)
        assert fake_gut["globs"][0] == "res://tests/unit/save/"

    def test_zero_hits_falls_back_to_full(self, tmp_path, monkeypatch):
        proj, files = _write_project(tmp_path)
        monkeypatch.setattr(
            runner_mod, "_run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None: (0, "pass"))
        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.testscan.derive_tests_glob",
            lambda *a, **k: None)
        s = run_mutation(files[0], proj, budget=1)["summary"]
        assert s["scoped"] is False


class TestSpec002ExplicitWins:
    def test_explicit_tests_glob_not_scanned(self, tmp_path, monkeypatch):
        proj, files = _write_project(tmp_path)
        scanned = {"v": False}
        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.testscan.derive_tests_glob",
            lambda *a, **k: scanned.__setitem__("v", True) or "res://tests/x/")
        globs = []

        def fake(root, timeout_s=60, tests_glob=None):
            globs.append(tests_glob)
            return (0, "pass")

        monkeypatch.setattr(runner_mod, "_run_gut_on_project", fake)
        run_mutation(files[0], proj, budget=1, tests_glob="res://tests/unit/save/")
        assert scanned["v"] is False
        assert all(g == "res://tests/unit/save/" for g in globs)
        assert len(globs) >= 1


class TestSpec013DefensiveSameFile:
    def test_repeated_file_raises(self):
        with pytest.raises(ValueError):
            executor._group_by_file(["a.gd", "a.gd"])
