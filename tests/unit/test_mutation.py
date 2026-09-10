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
        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.runner._run_gut_on_project",
            lambda root, timeout_s=60: (1, "fail"),  # tests fail = mutant killed
        )
        result = run_mutation(str(target), str(proj), budget=5)
        assert result["ok"] is True
        assert result["summary"]["killed"] == result["summary"]["mutants"]
        assert result["summary"]["survived"] == 0

    def test_survived_returns_ok_false(self, tmp_path, monkeypatch):
        proj, target = self._write_project(tmp_path)
        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.runner._run_gut_on_project",
            lambda root, timeout_s=60: (0, "pass"),  # tests pass = mutant survived
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
            lambda root, timeout_s=60: (1, "fail"),
        )
        run_mutation(str(target), str(proj), budget=3)
        assert target.read_text() == before

    def test_timeout_counted_as_not_killed(self, tmp_path, monkeypatch):
        proj, target = self._write_project(tmp_path)

        def timeout_run(root, timeout_s=60):
            import subprocess
            raise subprocess.TimeoutExpired(cmd="godot", timeout=timeout_s)

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

        def mixed(root, timeout_s=60):
            calls["n"] += 1
            return (1, "fail") if calls["n"] % 2 == 0 else (0, "pass")

        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.runner._run_gut_on_project", mixed
        )
        result = run_mutation(str(target), str(proj), budget=4)
        s = result["summary"]
        assert s["kill_rate"] == round(s["killed"] / s["mutants"], 4)
