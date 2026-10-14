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
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit import cli as cli_mod
from godot_qa_toolkit.cli import build_parser
from godot_qa_toolkit.mutation import executor
import godot_qa_toolkit.mutation.runner as runner_mod

REPO = Path(__file__).parent.parent.parent
GD_TARGET = "func add(a, b):\n\treturn a + b\n"
GD_PARALLEL_TARGET = (
    "func f(a, b):\n"
    "\tif a > 0:\n"
    "\t\treturn a + b\n"
    "\treturn a - b and a * b\n"
)


class _SerialPool:
    def __init__(self, max_workers=None):
        self.max_workers = max_workers

    def map(self, fn, iterable):
        return [fn(x) for x in iterable]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _write_project(tmp_path: Path, source: str = GD_TARGET) -> tuple[str, str]:
    proj = tmp_path / "proj"
    (proj / "addons" / "gut").mkdir(parents=True, exist_ok=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    save = proj / "tests" / "unit" / "save"
    save.mkdir(parents=True, exist_ok=True)
    (save / "test_save.gd").write_text('preload("res://calc.gd")\n')
    target = proj / "calc.gd"
    target.write_text(source)
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
    def test_single_file_default_jobs_goes_parallel(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
            sealed: None) -> None:
        """单文件 --jobs 缺省：必须真实进入 per-mutant 分片路径。"""
        proj, target = _write_project(tmp_path, GD_PARALLEL_TARGET)
        monkeypatch.setattr(executor.os, "cpu_count", lambda: 8)
        calls = []
        orig = executor.run_mutation

        def spy(*a, **k):
            calls.append(k)
            return orig(*a, **k)

        monkeypatch.setattr(executor, "run_mutation", spy)
        d = executor.run_mutation_files([target], proj, budget=50,
                                        tests_glob="res://tests/unit/save/",
                                        jobs=None)
        collected = len(executor._collect_mutations(GD_PARALLEL_TARGET, target))
        expected_jobs = min(4, collected)
        assert expected_jobs > 1
        assert d["jobs"] == expected_jobs
        assert len(calls) == expected_jobs
        assert all("mutant_slice" in call for call in calls)
        assert d["results"][0]["summary"]["jobs"] == expected_jobs

    def test_default_jobs_is_cpu_cap(self, monkeypatch):
        monkeypatch.setattr(executor.os, "cpu_count", lambda: 16)
        assert executor.default_jobs(None) == 8

    def test_single_file_parallel_contract_flat_shape(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
            sealed: None, capsys: pytest.CaptureFixture[str]) -> None:
        """CLI 默认单文件保持 flat 契约，summary.jobs 记录实际分片数。"""
        proj, target = _write_project(tmp_path, GD_PARALLEL_TARGET)
        monkeypatch.setattr(executor.os, "cpu_count", lambda: 8)
        args = build_parser().parse_args(
            ["mutation", target, "--project-root", proj])
        cli_mod.cmd_mutation(args)
        out = cli_mod.json.loads(capsys.readouterr().out)
        assert set(out) == {"tool", "ok", "summary", "failures"}
        summary = out["summary"]
        assert set(summary) == {
            "file", "mutants", "killed", "survived", "suspect", "timeout",
            "run_errors", "invalid_mutants", "kill_rate", "budget",
            "adaptive_timeout_s", "scoped", "baseline_reused",
            "timing_source", "samples_n", "baseline_p90", "jobs",
        }
        collected = len(executor._collect_mutations(GD_PARALLEL_TARGET, target))
        assert summary["file"].endswith("calc.gd")
        assert summary["jobs"] == min(4, collected)
        assert summary["mutants"] == collected

    def test_merge_results_reports_per_file_chunk_counts(self) -> None:
        """合并层按文件记录实际 chunk 数，而非误用池上限或全局 chunk 数。"""
        chunks = [{"file": "a.gd"}, {"file": "a.gd"}, {"file": "b.gd"}]
        chunk_results = [
            {"summary": {"mutants": 1, "killed": 1, "budget": 50,
                         "scoped": True, "baseline_reused": True},
             "failures": []}
            for _ in chunks
        ]
        merged = executor._merge_results(chunks, chunk_results, jobs=2)
        by_file = {r["summary"]["file"]: r for r in merged["results"]}
        assert merged["jobs"] == 3
        assert by_file["a.gd"]["summary"]["jobs"] == 2
        assert by_file["b.gd"]["summary"]["jobs"] == 1

    def test_merge_results_preserves_abort_and_timing_metadata(self) -> None:
        """baseline 中止不得被零计数合并成 ok，已有 timing 字段不得丢失。"""
        chunks = [{"file": "a.gd"}]
        chunk_results = [{
            "ok": False,
            "summary": {
                "run_error": True, "error": "baseline aborted",
                "adaptive_timeout_s": 0, "timing_source": "explicit",
                "samples_n": 0, "baseline_p90": None,
            },
            "failures": [{"reason": "baseline aborted"}],
        }]
        merged = executor._merge_results(chunks, chunk_results, jobs=1)
        result = merged["results"][0]
        summary = result["summary"]
        assert merged["ok"] is False
        assert result["ok"] is False
        assert summary["run_error"] is True
        assert summary["error"] == "baseline aborted"
        assert summary["adaptive_timeout_s"] == 0
        assert summary["timing_source"] == "explicit"
        assert summary["samples_n"] == 0
        assert summary["baseline_p90"] is None
        assert "resumed" not in summary

    def test_dry_run_and_settled_results_include_jobs(
            self, tmp_path: Path, sealed: None) -> None:
        """dry-run 与零 chunk 的 settled 结果也要遵守 summary.jobs 形态。"""
        proj, target = _write_project(tmp_path)
        dry = executor.run_mutation_files(
            [target], proj, dry_run=True, tests_glob="res://tests/unit/save/",
            jobs=4)
        assert dry["results"][0]["summary"]["jobs"] == 1
        no_sites = executor._settled_no_sites("noop.gd", True)
        error = executor._settled_error("broken.gd", ValueError("broken"))
        assert no_sites["summary"]["jobs"] == 0
        assert error["summary"]["jobs"] == 0


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

    def test_mutation_count_equal_jobs_falls_back_before_prewarm(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sealed: None) -> None:
        """等值边界 total == jobs 必须退串行，防止 <= 退化为 <。"""
        proj, target = _write_project(tmp_path)
        monkeypatch.setattr(executor.os, "cpu_count", lambda: 16)
        warm = {"n": 0}
        monkeypatch.setattr(
            executor, "prewarm_import",
            lambda root: warm.__setitem__("n", warm["n"] + 1))
        collected = len(executor._collect_mutations(GD_TARGET, target))
        assert collected == 3
        d = executor.run_mutation_files(
            [target], proj, budget=collected, timeout_s=60, dry_run=False,
            tests_glob="res://tests/unit/save/", jobs=collected)
        assert warm["n"] == 0
        assert d["jobs"] == 1
        assert d["results"][0]["summary"]["jobs"] == 1

    def test_jobs_one_still_forces_serial(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
            sealed: None) -> None:
        """显式 --jobs 1 强制串行，不经过 prewarm 或分片合并。"""
        proj, target = _write_project(tmp_path)
        warm = {"n": 0}
        monkeypatch.setattr(
            executor, "prewarm_import",
            lambda root: warm.__setitem__("n", warm["n"] + 1))
        d = executor.run_mutation_files(
            [target], proj, budget=50, timeout_s=60, dry_run=False,
            tests_glob="res://tests/unit/save/", jobs=1)
        assert warm["n"] == 0
        assert d["jobs"] == 1
        assert len(d["results"]) == 1
        assert d["results"][0]["summary"]["jobs"] == 1

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
    def test_cli_single_file_default_routes_to_executor(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
            sealed: None) -> None:
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

    def test_cli_default_fallback_receives_timing_store(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
            sealed: None, capsys: pytest.CaptureFixture[str]) -> None:
        """CLI 默认退串行必须把 TimingStore 接到 executor，而非恒传 None。"""
        from godot_qa_toolkit.mutation import executor as exec_mod
        proj, target = _write_project(tmp_path)
        monkeypatch.setattr(exec_mod.os, "cpu_count", lambda: 16)
        spy = Mock(wraps=exec_mod.run_mutation_files)
        monkeypatch.setattr(exec_mod, "run_mutation_files", spy)
        args = build_parser().parse_args(
            ["mutation", target, "--project-root", proj])
        cli_mod.cmd_mutation(args)
        assert spy.call_args.kwargs["timing_store"] is not None
        out = cli_mod.json.loads(capsys.readouterr().out)
        assert out["summary"]["jobs"] == 1

    def test_cli_negative_budget_returns_flat_empty_result(
            self, tmp_path: Path, sealed: None,
            capsys: pytest.CaptureFixture[str]) -> None:
        """负 budget 不得让默认单文件解包空 results 并抛出 IndexError。"""
        proj, target = _write_project(tmp_path)
        args = build_parser().parse_args(
            ["mutation", target, "--project-root", proj, "--budget", "-1"])
        rc = cli_mod.cmd_mutation(args)
        out = cli_mod.json.loads(capsys.readouterr().out)
        assert rc == 0
        assert set(out) == {"tool", "ok", "summary", "failures"}
        assert out["summary"]["mutants"] == 0
        assert out["summary"]["jobs"] == 0

    def test_cli_jobs_one_keeps_flat_contract_and_summary_jobs(
            self, tmp_path: Path, sealed: None,
            capsys: pytest.CaptureFixture[str]) -> None:
        """CLI 显式 jobs=1 的串行结果同样携带 summary.jobs=1。"""
        proj, target = _write_project(tmp_path)
        args = build_parser().parse_args(
            ["mutation", target, "--project-root", proj, "--jobs", "1"])
        cli_mod.cmd_mutation(args)
        out = cli_mod.json.loads(capsys.readouterr().out)
        assert set(out) == {"tool", "ok", "summary", "failures"}
        assert out["summary"]["jobs"] == 1

    def test_cli_missing_file_error_reason_has_not_found(self, capsys):
        args = build_parser().parse_args(
            ["mutation", "/nonexistent/x.gd", "--project-root", "/tmp"])
        rc = cli_mod.cmd_mutation(args)
        out = cli_mod.json.loads(capsys.readouterr().out)
        assert rc == 1
        assert "not found" in out["failures"][0]["reason"]
