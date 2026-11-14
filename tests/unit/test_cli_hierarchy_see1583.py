"""SEE-1583 §SPEC-001/003/004 — CLI 命令层级、输出契约与 SEE-1581 能力判据。

判据面：
- §SPEC-001：一级恰好 gherkin / mutation / measure / doctor；gherkin 下
  compile/run/mutate；measure 下 suite/run/diagnose/map-update/coverage/
  complexity/arch/parity/registry-diff/risk；measure 叶子无三级残留；
  全部旧一级命令调用即 exit 2（argparse invalid choice，无别名）。
- §SPEC-003：stdout JSON `tool` 值为新命令路径；ok/exit 0/1/2 语义保留；
  doctor 例外（stderr 诊断、stdout 空、exit 0/2）不变。
- §SPEC-004：SEE-1581 最终能力在新路径下逐项保留——mutation `--select`
  （fn 默认）/ `--attribution-map` / `--no-attribution-auto`、measure run
  `--test-attribution` / `--attribution-causal` / `--acceptance-adaptive` /
  `--arms-parallel`、measure map-update `--calibration-every` 等参数语义不变。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
FIXTURES = REPO / "tests" / "fixtures"

TOP_LEVEL = ("gherkin", "mutation", "measure", "doctor")
GHERKIN_LEAVES = ("compile", "run", "mutate")
MEASURE_LEAVES = ("suite", "run", "diagnose", "map-update", "coverage",
                  "complexity", "arch", "parity", "registry-diff", "risk")
OLD_TOP_LEVEL = ("accept", "arch", "attr-update", "canonical",
                 "canonical-diagnose", "complexity", "coverage", "parity",
                 "recipe", "registry", "risk")

NEW_TOOLS = {"gherkin", "mutation", "measure.suite", "measure.run",
             "measure.diagnose", "measure.map-update", "measure.coverage",
             "measure.complexity", "measure.arch", "measure.parity",
             "measure.registry-diff", "measure.risk"}


def _run_cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "godot_qa_toolkit.cli", *argv],
        capture_output=True, text=True, cwd=str(SRC),
    )


def _run_cli_json(*argv: str) -> tuple[int, dict]:
    proc = _run_cli(*argv)
    start = proc.stdout.index("{")
    return proc.returncode, json.loads(proc.stdout[start:])


def _parser():
    from godot_qa_toolkit.cli import build_parser

    return build_parser()


def _command_choices(parser) -> dict:
    for action in parser._actions:
        if action.dest == "command" and hasattr(action, "choices"):
            return action.choices
    raise AssertionError("top-level subparsers action missing")


def _leaf_parser(command: str, leaf: str | None = None):
    choices = _command_choices(_parser())
    sub = choices[command]
    if leaf is None:
        return sub
    for action in sub._actions:
        if hasattr(action, "choices") and action.choices and \
                isinstance(action.choices, dict):
            return action.choices[leaf]
    raise AssertionError(f"{command} has no sub-choices for {leaf}")


class TestSpec001Hierarchy:
    def test_top_level_exactly_four(self):
        assert set(_command_choices(_parser())) == set(TOP_LEVEL)

    def test_gherkin_leaves(self):
        gh = _leaf_parser("gherkin")
        subs = [sp for sp in gh._actions
                if isinstance(getattr(sp, "choices", None), dict)]
        assert subs, "gherkin must have a sub-choices action"
        assert set(subs[0].choices) == set(GHERKIN_LEAVES)

    def test_measure_leaves(self):
        ms = _leaf_parser("measure")
        subs = [sp for sp in ms._actions
                if isinstance(getattr(sp, "choices", None), dict)]
        assert subs, "measure must have a sub-choices action"
        assert set(subs[0].choices) == set(MEASURE_LEAVES)

    @pytest.mark.parametrize("command,leaf", [
        ("measure", leaf) for leaf in MEASURE_LEAVES
    ] + [("gherkin", leaf) for leaf in GHERKIN_LEAVES])
    def test_no_fourth_level_under_leaves(self, command, leaf):
        leaf_parser = _leaf_parser(command, leaf)
        nested = [sp for sp in leaf_parser._actions
                  if isinstance(getattr(sp, "choices", None), dict)
                  and sp.dest not in ("command",)]
        assert not nested, f"{command} {leaf} must not carry a third level"

    @pytest.mark.parametrize("old", OLD_TOP_LEVEL)
    def test_old_top_level_exit_2(self, old):
        proc = _run_cli(old)
        assert proc.returncode == 2, (
            f"old top-level entry {old!r} must be gone (exit 2), got "
            f"rc={proc.returncode}"
        )
        assert "invalid choice" in proc.stderr


class TestSpec003OutputContract:
    def test_measure_complexity_tool_and_exit0(self):
        rc, out = _run_cli_json("measure", "complexity", "--warn", "7",
                                "--max", "10", str(FIXTURES / "complex.gd"))
        assert rc == 0
        assert out["ok"] is True
        assert out["tool"] == "measure.complexity"

    def test_measure_complexity_fail_exit1(self):
        rc, out = _run_cli_json("measure", "complexity", "--max", "3",
                                str(FIXTURES / "complex.gd"))
        assert rc == 1
        assert out["ok"] is False
        assert out["tool"] == "measure.complexity"

    def test_measure_coverage_missing_target_error_contract(self):
        # measure 模式要求目标文件（或 --recover/--prune-sinks）：ok=False
        # + 领域错误 JSON，exit 1（无 godot 也不得先 traceback）。
        rc, out = _run_cli_json("measure", "coverage", "--project-root", ".")
        assert rc == 1
        assert out["tool"] == "measure.coverage"
        assert out["ok"] is False
        assert "error" in out["summary"]

    def test_gherkin_compile_needs_feature_or_keys(self):
        rc, out = _run_cli_json("gherkin", "compile", "--project-root", ".")
        assert rc == 1
        assert out["tool"] == "gherkin"
        assert out["ok"] is False

    def test_measure_arch_tool(self, tmp_path):
        rc, out = _run_cli_json("measure", "arch", str(tmp_path))
        assert out["tool"] == "measure.arch"

    def test_doctor_exception_unchanged(self):
        # doctor：stdout 为空、诊断在 stderr、exit 0/2（无 stdout JSON）。
        proc = _run_cli("doctor")
        assert proc.stdout.strip() == ""
        assert proc.stderr.strip() != ""
        assert proc.returncode in (0, 2)

    def test_old_command_stderr_is_usage_not_json(self):
        proc = _run_cli("canonical")
        assert proc.stdout.strip() == ""
        assert "invalid choice" in proc.stderr


class TestSpec004See1581Flags:
    def test_mutation_select_default_fn_and_choices(self):
        args = _parser().parse_args(
            ["mutation", "f.gd", "--project-root", "p"])
        assert args.select_mode == "fn"

    def test_mutation_select_choices_preserved(self):
        args = _parser().parse_args(
            ["mutation", "f.gd", "--project-root", "p", "--select", "file"])
        assert args.select_mode == "file"
        args = _parser().parse_args(
            ["mutation", "f.gd", "--project-root", "p", "--select", "full"])
        assert args.select_mode == "full"

    def test_mutation_attribution_map_and_auto_off(self):
        args = _parser().parse_args(
            ["mutation", "f.gd", "--project-root", "p",
             "--attribution-map", "m.json", "--no-attribution-auto"])
        assert args.attribution_map == "m.json"
        assert args.attribution_auto is False

    def test_mutation_see1420_caliper_flags_preserved(self):
        args = _parser().parse_args(
            ["mutation", "f.gd", "--project-root", "p",
             "--mutation", "f.gd#x#1", "--lines", "7",
             "--mutation-warning", "40", "--timeout-factor", "1.5",
             "--mutate-all", "--no-reuse-coverage", "--worker-channel"])
        assert args.mutation == "f.gd#x#1"
        assert args.lines == 7
        assert args.mutation_warning == 40
        assert args.timeout_factor == 1.5
        assert args.mutate_all is True
        assert args.no_reuse_coverage is True
        assert args.worker_channel is True

    def test_measure_run_see1581_flags(self):
        args = _parser().parse_args(
            ["measure", "run", "res://src", "--project-root", "p",
             "--recipe", "r.json", "--out", "o.json",
             "--test-attribution", "--attribution-causal",
             "--acceptance-adaptive", "--arms-parallel",
             "--acceptance-probe-keys", "a,b",
             "--baseline-failures", "bf.json", "--intolerant-tests", "it.json",
             "--stability-baseline", "sb.json", "--evidence-dir", "ev",
             "--shard-width", "4", "--acceptance-shards", "2",
             "--acceptance-repeats", "2", "--shard-log-dir", "logs",
             "--timeout", "99", "--test-attribution-out", "attr.json"])
        assert args.test_attribution is True
        assert args.attribution_causal is True
        assert args.acceptance_adaptive is True
        assert args.arms_parallel is True
        assert args.acceptance_probe_keys == "a,b"
        assert args.baseline_failures == "bf.json"
        assert args.intolerant_tests == "it.json"
        assert args.stability_baseline == "sb.json"
        assert args.evidence_dir == "ev"
        assert args.shard_width == 4
        assert args.acceptance_shards == 2
        assert args.acceptance_repeats == 2
        assert args.shard_log_dir == "logs"
        assert args.timeout == 99
        assert args.test_attribution_out == "attr.json"

    def test_measure_map_update_flags(self):
        args = _parser().parse_args(
            ["measure", "map-update", "res://src", "--project-root", "p",
             "--recipe", "r.json", "--baseline", "b.json", "--out", "o.json",
             "--changed", "a.gd", "--changed", "b.gd",
             "--calibration-every", "3", "--attribution-causal",
             "--timeout", "55"])
        assert args.recipe == "r.json"
        assert args.baseline == "b.json"
        assert args.out == "o.json"
        assert args.changed == ["a.gd", "b.gd"]
        assert args.calibration_every == 3
        assert args.attribution_causal is True
        assert args.timeout == 55

    def test_measure_diagnose_flags(self):
        args = _parser().parse_args(
            ["measure", "diagnose", "res://src", "--project-root", "p",
             "--recipe", "r.json", "--features", "a,b", "--out", "o.json",
             "--timeout", "480"])
        assert args.features == "a,b"
        assert args.out == "o.json"
        assert args.timeout == 480

    def test_measure_registry_diff_flags(self):
        args = _parser().parse_args(
            ["measure", "registry-diff", "old.json", "new.json",
             "--out", "d.json"])
        assert args.old == "old.json"
        assert args.new == "new.json"
        assert args.out == "d.json"

    def test_measure_suite_flags(self):
        args = _parser().parse_args(
            ["measure", "suite", "--project-root", "p", "--out", "r.json",
             "--exclude", "scope:entry:reason"])
        assert args.project_root == "p"
        assert args.out == "r.json"
        assert args.exclude == ["scope:entry:reason"]

    def test_gherkin_compile_keys_mode_flags(self):
        args = _parser().parse_args(
            ["gherkin", "compile", "--project-root", "p", "--keys", "a,b",
             "--out-dir", "d", "--write", "--no-clean"])
        assert args.keys == "a,b"
        assert args.out_dir == "d"
        assert args.write is True
        assert args.no_clean is True

    def test_gherkin_run_and_mutate_flags(self):
        args = _parser().parse_args(
            ["gherkin", "run", "--project-root", "p", "--key", "k",
             "--suite-dir", "d", "--acceptance-probe-keys", "a",
             "--timeout", "60"])
        assert args.gherkin_command == "run"
        assert args.suite_dir == "d"
        args = _parser().parse_args(
            ["gherkin", "mutate", "--project-root", "p", "--key", "k",
             "--worker", "--timeout", "30"])
        assert args.gherkin_command == "mutate"
        assert args.worker is True
