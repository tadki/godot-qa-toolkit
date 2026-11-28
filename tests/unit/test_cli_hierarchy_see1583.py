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
  `--test-attribution` / `--attribution-causal` / `--gherkin-adaptive` /
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
                  "complexity", "arch", "parity", "registry-diff", "risk",
                  "scope")
OLD_TOP_LEVEL = ("accept", "arch", "attr-update", "canonical",
                 "canonical-diagnose", "complexity", "coverage", "parity",
                 "recipe", "registry", "risk")

NEW_TOOLS = {"gherkin", "mutation", "measure.suite", "measure.run",
             "measure.diagnose", "measure.map-update", "measure.coverage",
             "measure.complexity", "measure.arch", "measure.parity",
             "measure.registry-diff", "measure.risk"}

# §SPEC-004 全参数面共用的 argv：拆两个断言组（attribution / shard+output）
# 以守住复杂度阈值，两组解析同一 argv，断言覆盖范围与拆分前完全一致。
_MEASURE_RUN_FULL_ARGV = [
    "measure", "run", "res://src", "--project-root", "p",
    "--recipe", "r.json", "--out", "o.json",
    "--test-attribution", "--attribution-causal",
    "--gherkin-adaptive", "--arms-parallel",
    "--gherkin-probe-keys", "a,b",
    "--baseline-failures", "bf.json", "--intolerant-tests", "it.json",
    "--stability-baseline", "sb.json", "--evidence-dir", "ev",
    "--shard-width", "4", "--gherkin-shards", "2",
    "--gherkin-repeats", "2", "--shard-log-dir", "logs",
    "--timeout", "99"]


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

    # §SPEC-010：subprocess 面之外，程序化 main(argv) 调用同样必须走
    # argparse invalid-choice 拒绝，堵住任何 argv 预改写/别名翻译复活。
    @pytest.mark.parametrize("old", ("accept", "canonical", "recipe"))
    def test_old_top_level_rejected_programmatically(self, old, capsys):
        from godot_qa_toolkit.cli import main

        with pytest.raises(SystemExit) as ei:
            main([old])
        assert ei.value.code == 2
        assert "invalid choice" in capsys.readouterr().err


class TestSpec010WiringPins:
    """§SPEC-010：每个叶子 set_defaults(func=...) 精确钉到具名 handler。

    hierarchy flags 测试只覆盖参数面；无 e2e 派发覆盖的叶子
    （suite/parity/registry-diff/risk/map-update/diagnose）错接 handler
    时全测试树无反例，故按结构逐叶钉住。
    """

    EXPECTED_WIRING = {
        ("gherkin", "compile"): "cmd_gherkin",
        ("gherkin", "run"): "cmd_gherkin",
        ("gherkin", "mutate"): "cmd_gherkin",
        ("measure", "suite"): "cmd_measure_suite",
        ("measure", "run"): "cmd_measure_run",
        ("measure", "diagnose"): "cmd_measure_diagnose",
        ("measure", "map-update"): "cmd_measure_map_update",
        ("measure", "coverage"): "cmd_measure_coverage",
        ("measure", "complexity"): "cmd_measure_complexity",
        ("measure", "arch"): "cmd_measure_arch",
        ("measure", "parity"): "cmd_measure_parity",
        ("measure", "registry-diff"): "cmd_measure_registry_diff",
        ("measure", "risk"): "cmd_measure_risk",
    }

    @pytest.mark.parametrize("command,leaf", sorted(EXPECTED_WIRING))
    def test_leaf_wired_to_named_handler(self, command, leaf):
        from godot_qa_toolkit import cli

        handler = getattr(cli, self.EXPECTED_WIRING[(command, leaf)])
        leaf_parser = _leaf_parser(command, leaf)
        assert leaf_parser._defaults.get("func") is handler, (
            f"{command} {leaf} must wire to "
            f"{self.EXPECTED_WIRING[(command, leaf)]}"
        )

    def test_top_level_wiring(self):
        from godot_qa_toolkit import cli

        choices = _command_choices(_parser())
        assert choices["mutation"]._defaults.get("func") is cli.cmd_mutation
        assert choices["doctor"]._defaults.get("func") is cli.cmd_doctor


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
    def test_mutation_select_default_line_and_choices(self):
        # SEE-1587：行级是默认主语义（fn/file 退为显式校准/回退视图）
        args = _parser().parse_args(
            ["mutation", "f.gd", "--project-root", "p"])
        assert args.select_mode == "line"
        args = _parser().parse_args(
            ["mutation", "f.gd", "--project-root", "p", "--select", "fn"])
        assert args.select_mode == "fn"

    def test_mutation_select_choices_preserved(self):
        args = _parser().parse_args(
            ["mutation", "f.gd", "--project-root", "p", "--select", "file"])
        assert args.select_mode == "file"
        args = _parser().parse_args(
            ["mutation", "f.gd", "--project-root", "p", "--select", "full"])
        assert args.select_mode == "full"

    def test_mutation_attribution_map_and_auto_off(self):
        # C3-R20 迁移（定案 20 / A2）：--attribution-map 与
        # --no-attribution-auto 退役 → argparse 用法错 exit 2
        for argv in (["mutation", "f.gd", "--project-root", "p",
                      "--attribution-map", "m.json"],
                     ["mutation", "f.gd", "--project-root", "p",
                      "--no-attribution-auto"]):
            with pytest.raises(SystemExit) as ei:
                _parser().parse_args(argv)
            assert ei.value.code == 2

    def test_mutation_see1420_caliper_flags_preserved(self):
        args = _parser().parse_args(
            ["mutation", "f.gd", "--project-root", "p",
             "--mutation", "f.gd#x#1", "--lines", "7",
             "--mutation-warning", "40", "--timeout-factor", "1.5",
             "--mutate-all", "--no-reuse-coverage", "--worker-channel"])
        assert args.mutation == "f.gd#x#1"
        # SEE-1587：--lines 接受单行/区间/多段规格（字符串形态，解析在
        # dispatch 早段完成）
        assert args.lines == "7"
        assert args.mutation_warning == 40
        assert args.timeout_factor == 1.5
        assert args.mutate_all is True
        assert args.no_reuse_coverage is True
        assert args.worker_channel is True

    def test_measure_run_see1581_attribution_flags(self):
        args = _parser().parse_args(_MEASURE_RUN_FULL_ARGV)
        assert args.test_attribution is True
        assert args.attribution_causal is True
        assert args.gherkin_adaptive is True
        assert args.arms_parallel is True
        assert args.gherkin_probe_keys == "a,b"
        assert args.baseline_failures == "bf.json"
        assert args.intolerant_tests == "it.json"
        assert args.stability_baseline == "sb.json"
        # C3-R20 迁移（定案 20 / A2）：--test-attribution-out 退役 →
        # argparse 用法错 exit 2；开启 --test-attribution 后归属产物随
        # 报告落盘，路径由代码推导为 <out>.attribution.json
        with pytest.raises(SystemExit) as ei:
            _parser().parse_args([
                "measure", "run", "res://src", "--project-root", "p",
                "--out", "o.json", "--test-attribution-out", "attr.json"])
        assert ei.value.code == 2
        from godot_qa_toolkit.cli import _attribution_args
        enabled, out, causal = _attribution_args(args)
        assert enabled is True and causal is True
        assert out == "o.json.attribution.json"

    def test_measure_run_see1581_shard_output_flags(self):
        args = _parser().parse_args(_MEASURE_RUN_FULL_ARGV)
        assert args.evidence_dir == "ev"
        assert args.shard_width == 4
        assert args.gherkin_shards == 2
        assert args.gherkin_repeats == 2
        assert args.shard_log_dir == "logs"
        assert args.timeout == 99

    def test_measure_map_update_flags(self):
        # C3-R20 迁移（定案 20 / A2）：map-update 新命令面——无位置参数、
        # 不收地图路径与手工增量输入；roots 与旧 flags 都是 exit 2
        args = _parser().parse_args(
            ["measure", "map-update", "--project-root", "p",
             "--timeout", "55"])
        assert args.project_root == "p"
        assert args.timeout == 55
        for argv in (["measure", "map-update", "res://src"],
                     ["measure", "map-update", "--baseline", "b.json"],
                     ["measure", "map-update", "--out", "o.json"],
                     ["measure", "map-update", "--recipe", "r.json"],
                     ["measure", "map-update", "--changed", "a.gd"]):
            with pytest.raises(SystemExit) as ei:
                _parser().parse_args(argv)
            assert ei.value.code == 2

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
             "--suite-dir", "d", "--gherkin-probe-keys", "a",
             "--timeout", "60"])
        assert args.gherkin_command == "run"
        assert args.suite_dir == "d"
        args = _parser().parse_args(
            ["gherkin", "mutate", "--project-root", "p", "--key", "k",
             "--worker", "--timeout", "30"])
        assert args.gherkin_command == "mutate"
        assert args.worker is True
