"""SEE-1413：单文件默认并行 + 自动退串行单测。

钉死行为：
- 单文件 --jobs 缺省走并发切片路径（不再默认串行）；
- 变异点总数 ≤ jobs 自动退串行（判定在 prewarm/baseline 之前）；
- 显式 --jobs 1 仍强制串行；
- 单文件并发契约顶层形态 flat，summary.jobs 新增；
- --jobs 帮助文本 / README 示例断言。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit import cli as cli_mod
from godot_qa_toolkit.cli import build_parser
from godot_qa_toolkit.mutation import executor
import godot_qa_toolkit.mutation.runner as runner_mod

REPO = Path(__file__).parent.parent.parent
GD_TARGET = "func add(a, b):\n\treturn a + b\n"


class _SerialPool:
    def __init__(self, max_workers=None):
        self.max_workers = max_workers

    def map(self, fn, iterable):
        return [fn(x) for x in iterable]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


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


@pytest.fixture()
def sealed(monkeypatch):
    monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
    monkeypatch.setattr(
        executor, "_run_baseline_snapshot",
        lambda root, g, t: {"rc": 0, "tail": "", "elapsed": 5.0})
    monkeypatch.setattr(executor, "prewarm_import", lambda root: None)
    monkeypatch.setattr(
        runner_mod, "_run_gut_on_project",
        lambda root, timeout_s=60, tests_glob=None: (0, "pass"))


class TestDefaultParallelRouting:
    def test_single_file_default_jobs_goes_parallel(self, tmp_path, monkeypatch, sealed):
        """单文件 --jobs 缺省：走并发分片路径（不再退串行快路径）。"""
        proj, target = _write_project(tmp_path)
        route = {"path": None}
        orig = executor.run_mutation

        def spy(*a, **k):
            route["path"] = "serial-fallback"
            return orig(*a, **k)

        monkeypatch.setattr(executor, "run_mutation", spy)
        d = executor.run_mutation_files([target], proj, budget=50,
                                        tests_glob="res://tests/unit/save/",
                                        jobs=None)
        # toy fixture 收集的变异点 > 默认 jobs（cap 通常 ≥2，toy 文件 3+ 变异）
        collected = len(executor._collect_mutations(GD_TARGET, target))
        if collected > executor.default_jobs(None):
            assert route["path"] != "serial-fallback"
            assert d["jobs"] > 1

    def test_default_jobs_is_cpu_cap(self, monkeypatch):
        monkeypatch.setattr(executor.os, "cpu_count", lambda: 16)
        assert executor.default_jobs(None) == 8

    def test_single_file_parallel_contract_flat_shape(self, tmp_path, sealed):
        """单文件并发顶层形态 flat：既有键集零变化，summary 增 jobs 键。"""
        proj, target = _write_project(tmp_path)
        d = executor.run_mutation_files([target], proj, budget=50,
                                        tests_glob="res://tests/unit/save/",
                                        jobs=None)
        assert set(d) == {"tool", "ok", "jobs", "results"}
        assert len(d["results"]) == 1
        r = d["results"][0]
        assert set(r) == {"tool", "ok", "summary", "failures"}
        assert r["summary"]["file"].endswith("calc.gd")
        # 顶层 summary 语义沿用：jobs = 实际分片数 > 1（toy 3 变异 ≥ cap 2）
        assert d["jobs"] >= 1


class TestAutoSerialFallback:
    def test_few_mutants_falls_back_serial_zero_prewarm(self, tmp_path, monkeypatch):
        """变异点 ≤ jobs：退串行，且零 prewarm（判定在预热之前）。"""
        proj, target = _write_project(tmp_path)
        warm = {"n": 0}
        monkeypatch.setattr(
            executor, "prewarm_import",
            lambda root: warm.__setitem__("n", warm["n"] + 1))
        monkeypatch.setattr(
            runner_mod, "_run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None: (0, "pass"))
        d = executor.run_mutation_files(
            [target], proj, budget=1, timeout_s=60, dry_run=False,
            tests_glob="res://tests/unit/save/",
            jobs=executor.default_jobs(99) + 1)
        assert warm["n"] == 0, "退串行路径不得 prewarm"
        assert d["jobs"] == 1
        assert d["results"][0]["summary"]["jobs"] == 1

    def test_jobs_one_still_forces_serial(self, tmp_path, sealed):
        """显式 --jobs 1 强制串行（不享受并发、不退判定）。"""
        proj, target = _write_project(tmp_path)
        d = executor.run_mutation_files(
            [target], proj, budget=50, timeout_s=60, dry_run=False,
            tests_glob="res://tests/unit/save/", jobs=1)
        assert d["jobs"] == 1
        assert len(d["results"]) == 1

    def test_fallback_timing_store_wired(self, tmp_path, monkeypatch):
        """退串行路径接线 timing store（adaptive timeout 语义不静默丢失）。"""
        from godot_qa_toolkit.timing import TimingStore
        proj, target = _write_project(tmp_path)
        store = TimingStore(proj, target, "mutation", "res://tests/unit/save/")
        monkeypatch.setattr(
            runner_mod, "_run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None: (0, "pass"))
        monkeypatch.setattr(executor, "prewarm_import", lambda root: None)
        executor.run_mutation_files(
            [target], proj, budget=1, timeout_s=60, dry_run=False,
            tests_glob="res://tests/unit/save/",
            jobs=executor.default_jobs(99) + 1,
            timing_store=store)
        assert store.read(), "退串行 baseline 采样必须回写 timing store"


class TestDocsSurface:
    def test_jobs_help_mentions_both_slicing_levels(self):
        parser = build_parser()
        action = next(a for a in parser._subparsers._group_actions[0].choices["mutation"]._actions
                      if a.dest == "jobs")
        assert "per-file AND per-mutant" in action.help
        assert "single file" in action.help

    def test_readme_has_single_file_parallel_example(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        assert "gqt mutation <file> --project-root . --jobs 8" in readme


class TestCliRouting:
    def test_cli_single_file_default_routes_to_executor(self, tmp_path, monkeypatch, sealed):
        """CLI 层：单文件 --jobs 缺省 → run_mutation_files（并发路径）。"""
        from godot_qa_toolkit.mutation import executor as exec_mod
        proj, target = _write_project(tmp_path)
        called = {"executor": False}
        orig = exec_mod.run_mutation_files

        def spy(*a, **k):
            called["executor"] = True
            return orig(*a, **k)

        monkeypatch.setattr(exec_mod, "run_mutation_files", spy)
        args = build_parser().parse_args(
            ["mutation", target, "--project-root", proj])
        cli_mod.cmd_mutation(args)
        assert called["executor"] is True

    def test_cli_missing_file_error_reason_has_not_found(self, capsys):
        args = build_parser().parse_args(
            ["mutation", "/nonexistent/x.gd", "--project-root", "/tmp"])
        rc = cli_mod.cmd_mutation(args)
        out = cli_mod.json.loads(capsys.readouterr().out)
        assert rc == 1
        assert "not found" in out["failures"][0]["reason"]
