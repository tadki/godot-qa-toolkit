"""Mutation runner unit tests (SEE-1268 M2, TDD)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation.runner import (
    Mutant,
    _apply_mutation,
    _collect_mutations,
    run_mutation,
)

GD_WITH_OPS = """extends Node

func add(a: int, b: int) -> int:
	return a + b

func is_positive(a: int) -> bool:
	return a > 0

func negate(a: int) -> int:
	return -a
"""


class TestCollectMutations:
    def test_arith_and_comparison_and_unary_found(self):
        ms = _collect_mutations(GD_WITH_OPS, "x.gd")
        kinds = {m.kind for m in ms}
        assert "AOR" in kinds
        assert "ROR" in kinds

    def test_aor_covers_all_arith_ops(self):
        ms = _collect_mutations(GD_WITH_OPS, "x.gd")
        aor = {m.original for m in ms if m.kind == "AOR"}
        assert "+" in aor

    def test_ror_has_comparison_ops(self):
        ms = _collect_mutations(GD_WITH_OPS, "x.gd")
        ror = {m.original for m in ms if m.kind == "ROR"}
        assert ">" in ror

    def test_positions_are_nonzero(self):
        ms = _collect_mutations(GD_WITH_OPS, "x.gd")
        assert all(m.line > 0 for m in ms)

    def test_no_sites_returns_empty(self):
        ms = _collect_mutations("extends Node\nfunc f():\n\tpass\n", "x.gd")
        assert ms == []


class TestApplyMutation:
    def test_replaces_operator_at_position(self):
        src = "func add(a, b):\n\treturn a + b\n"
        m = Mutant(file="x.gd", line=2, column=11, operator="+", original="+",
                   mutated="-", kind="AOR")
        out = _apply_mutation(src, m)
        assert "return a - b" in out

    def test_preserves_rest_of_source(self):
        src = "func add(a, b):\n\treturn a + b\nfunc sub(a, b):\n\treturn a - b\n"
        m = Mutant(file="x.gd", line=2, column=11, operator="+", original="+",
                   mutated="*", kind="AOR")
        out = _apply_mutation(src, m)
        assert "return a * b" in out
        assert "return a - b" in out


class TestRunMutation:
    def _write_project(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "addons" / "gut").mkdir(parents=True)
        (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
        target = proj / "calc.gd"
        target.write_text("func add(a, b):\n\treturn a + b\n")
        return proj, target

    def test_all_killed_returns_ok_true(self, tmp_path, monkeypatch):
        proj, target = self._write_project(tmp_path)
        # First call = baseline (clean pass); each mutant call fails on a NEW
        # test name — baseline-diff kill semantics.
        calls = {"n": 0}
        def fake_run(root, timeout_s=60, tests_glob=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return (0, "all pass")
            return (1, f"* test_kill_{calls['n']}\n    [Failed]: caught\n")
        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.runner._run_gut_on_project", fake_run
        )
        result = run_mutation(str(target), str(proj), budget=5)
        assert result["ok"] is True
        assert result["summary"]["killed"] == result["summary"]["mutants"]
        assert result["summary"]["survived"] == 0

    def test_survived_returns_ok_false(self, tmp_path, monkeypatch):
        proj, target = self._write_project(tmp_path)
        # Baseline and every mutant run: tests pass → mutants survive.
        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.runner._run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None: (0, "pass"),
        )
        result = run_mutation(str(target), str(proj), budget=5)
        assert result["ok"] is False
        assert result["summary"]["survived"] == result["summary"]["mutants"]
        assert len(result["failures"]) == result["summary"]["mutants"]

    def test_original_file_restored(self, tmp_path, monkeypatch):
        proj, target = self._write_project(tmp_path)
        before = target.read_text()
        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.runner._run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None: (0, "pass"),
        )
        run_mutation(str(target), str(proj), budget=3)
        assert target.read_text() == before

    def test_timeout_counted_as_not_killed(self, tmp_path, monkeypatch):
        proj, target = self._write_project(tmp_path)

        def timeout_run(root, timeout_s=60, tests_glob=None):
            import subprocess
            if timeout_run.calls == 0:
                timeout_run.calls = 1
                return (0, "baseline pass")
            raise subprocess.TimeoutExpired(cmd="godot", timeout=timeout_s)

        timeout_run.calls = 0
        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.runner._run_gut_on_project", timeout_run
        )
        result = run_mutation(str(target), str(proj), budget=3)
        assert result["ok"] is False
        assert result["summary"]["timeout"] == result["summary"]["mutants"]

    def test_no_sites_ok(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "addons" / "gut").mkdir(parents=True)
        (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
        target = proj / "noop.gd"
        target.write_text("extends Node\nfunc f():\n\tpass\n")
        result = run_mutation(str(target), str(proj))
        assert result["ok"] is True
        assert result["summary"]["mutants"] == 0

    def test_kill_rate_computed(self, tmp_path, monkeypatch):
        proj, target = self._write_project(tmp_path)
        calls = {"n": 0}

        def mixed(root, timeout_s=60, tests_glob=None):
            calls["n"] += 1
            return (1, "fail") if calls["n"] % 2 == 0 else (0, "pass")

        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.runner._run_gut_on_project", mixed
        )
        result = run_mutation(str(target), str(proj), budget=4)
        s = result["summary"]
        assert s["kill_rate"] == round(s["killed"] / s["mutants"], 4)

    def test_godot_launch_failure_is_not_counted_as_killed(self, tmp_path, monkeypatch):
        # Revy QA FAIL: godot's "Attempt to open script ... File not found" also
        # exits non-zero, which the runner misread as "mutant killed". A launch
        # failure must surface as a run_error in the contract — not a kill —
        # because it says nothing about whether tests caught the mutant. The
        # baseline run hits the same failure, so run_mutation aborts early.
        proj, target = self._write_project(tmp_path)
        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.runner._run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None: (1, "ERROR: Attempt to open script 'res://addons/gut/gut_cmdln.gd' resulted in error 'File not found'"),
        )
        result = run_mutation(str(target), str(proj), budget=3)
        assert result["ok"] is False
        s = result["summary"]
        # Baseline run hits the same launch failure → early abort, no mutant
        # verdicts at all (killed key absent from the abort contract).
        assert s.get("run_error") is True
        assert s.get("killed", 0) == 0, "launch failure must never count as a kill"
        assert any("launch failed" in f["reason"] for f in result["failures"])

    def test_baseline_timeout_returns_run_error_json_not_crash(self, tmp_path, monkeypatch):
        # Revy QA retest HIGH: baseline run on a slow project (KOL baseline
        # ~108s) exceeds the default --timeout 60 → TimeoutExpired propagated as
        # a raw traceback with stdout 0 bytes and no JSON contract. It must be
        # captured into the unified run_error JSON like the launch-failure case.
        proj, target = self._write_project(tmp_path)

        def timeout_run(root, timeout_s=60, tests_glob=None):
            import subprocess
            raise subprocess.TimeoutExpired(cmd="godot", timeout=timeout_s)

        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.runner._run_gut_on_project", timeout_run
        )
        result = run_mutation(str(target), str(proj), budget=2, timeout_s=60)
        assert result["ok"] is False
        assert result["summary"].get("run_error") is True, result["summary"]
        assert "timed out" in result["failures"][0]["reason"]
        assert set(result) >= {"tool", "ok", "summary", "failures"}

    def test_invokes_gut_via_res_path(self, tmp_path, monkeypatch):
        # The runner must ask godot to load GUT via res:// — an absolute -s path
        # makes godot fail to load the script regardless of its existence.
        proj, target = self._write_project(tmp_path)
        captured = {}

        def fake_run(root, timeout_s=60, tests_glob=None):
            captured["root"] = root
            return (1, "fail")

        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.runner._run_gut_on_project", fake_run
        )
        run_mutation(str(target), str(proj), budget=1)
        # _run_gut_on_project itself constructs the command; assert indirectly by
        # checking the module constant the real implementation derives from.
        from godot_qa_toolkit.mutation import runner as m
        assert m.GUT_SCRIPT_RES_PATH == "res://addons/gut/gut_cmdln.gd"

    def test_preexisting_failures_do_not_count_as_killed(self, tmp_path, monkeypatch):
        # KOL baseline GUT has pre-existing failures (headless virtual-cursor
        # E2E): rc=1 even with the ORIGINAL file. Kill must be decided by the
        # FAILING-TEST-NAME DIFF vs baseline, not by the exit code — otherwise
        # every mutant on such a project is falsely "killed".
        proj, target = self._write_project(tmp_path)
        baseline = (1, "---- 2 failing tests ----\n* test_old_flaky\n    [Failed]: boom\n")
        mutant_same = (1, "---- 2 failing tests ----\n* test_old_flaky\n    [Failed]: boom\n")

        calls = {"n": 0}
        def fake_run(root, timeout_s=60, tests_glob=None):
            calls["n"] += 1
            return baseline if calls["n"] == 1 else mutant_same

        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.runner._run_gut_on_project", fake_run
        )
        result = run_mutation(str(target), str(proj), budget=2)
        s = result["summary"]
        assert s["killed"] == 0, f"pre-existing failures must not be kills: {s}"
        # SEE-1312 suspect：baseline 脏 ∧ 差集空 → suspect（不再归 survived）
        assert s.get("suspect", 0) + s["survived"] == s["mutants"]

    def test_new_failure_counts_as_killed(self, tmp_path, monkeypatch):
        # Baseline fails on test_old_flaky; the mutant additionally fails
        # test_add — that NEW failure is a genuine kill.
        proj, target = self._write_project(tmp_path)
        calls = {"n": 0}
        def fake_run(root, timeout_s=60, tests_glob=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return (1, "---- 1 failing tests ----\n* test_old_flaky\n    [Failed]: boom\n")
            return (1, "---- 2 failing tests ----\n* test_old_flaky\n    [Failed]: boom\n* test_add\n    [Failed]: new\n")

        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.runner._run_gut_on_project", fake_run
        )
        result = run_mutation(str(target), str(proj), budget=2)
        s = result["summary"]
        assert s["killed"] == s["mutants"]
        assert s["survived"] == 0


class TestRunMutationInterruptRecovery:
    """SEE-1319 缺陷1：外层 run_mutation 在任何异常/中断路径均须恢复原文件。

    per-mutant 的 finally 已恢复；但 SIGTERM/SIGINT/未知异常发生在循环外层
    （baseline 或 record 拼装阶段）时，工作树会遗留活跃变异体——外部
    TaskStop 实证 count+=1 → count+=0 遗留进 git diff。
    """

    def _write_project(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "addons" / "gut").mkdir(parents=True)
        (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
        target = proj / "calc.gd"
        target.write_text("func add(a, b):\n\treturn a + b\n")
        return proj, target

    def test_run_mutation_restores_on_exception(self, tmp_path, monkeypatch):
        proj, target = self._write_project(tmp_path)
        before = target.read_text()

        def crash_run(root, timeout_s=60, tests_glob=None):
            raise RuntimeError("simulated SIGTERM mid-loop")

        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.runner._run_gut_on_project", crash_run
        )
        with pytest.raises(RuntimeError):
            run_mutation(str(target), str(proj), budget=2)
        assert target.read_text() == before, (
            "外层异常路径必须恢复原文件，不得遗留变异体"
        )

    def test_run_mutation_restores_on_sigterm(self, tmp_path, monkeypatch):
        # KeyboardInterrupt（SIGINT 的 Python 映射）也必须走兜底恢复。
        proj, target = self._write_project(tmp_path)
        before = target.read_text()

        def crash_run(root, timeout_s=60, tests_glob=None):
            raise KeyboardInterrupt

        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.runner._run_gut_on_project", crash_run
        )
        with pytest.raises(KeyboardInterrupt):
            run_mutation(str(target), str(proj), budget=2)
        assert target.read_text() == before
