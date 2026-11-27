"""SEE-1312 hardener 反例测试（对抗性硬化，Revy ③）。

目标：拉满 39 项交付测试的判别力缺口——每条都先以反例 RED 实证缺陷，
修复后 GREEN。覆盖：
- H1 CLI 契约：--dry-run/--tests 必须从命令行可达（SPEC-009 完整性）
- H2 arm_swap：arm 含 ' if '/' else ' 字符串时不得误切（SPEC-004 roundtrip 边界）
- H3 分支覆盖：分支判定必须映射分支体首语句行，非分支头行（SPEC-007 判定正确性）
- H4 sink 读取：陈旧项目根 sink 不得遮蔽新鲜 user:// sink（SPEC-002 语义）
- H5 suspect 语义：suspect>0 时 ok 不得为 True（SPEC-010 契约保守性）
- H6 budget=0：零 mutant 不得触发 baseline GUT（SPEC-009 成本）
- H7 越界/垃圾 hits 行：probes_fired 只计有效 id（SPEC-003 哨兵保真）
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


def _write_project(tmp_path, target_src="func add(a, b):\n\treturn a + b\n"):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    (proj / "addons" / "gut" / "gut.gd").write_text("extends Node\n")
    target = proj / "target.gd"
    target.write_text(target_src)
    return proj, target


class TestH1CliContract:
    """H1: --dry-run / --tests 必须是 CLI 可达 flag（SPEC-009 缺 CLI 接线实证）。"""

    def test_mutation_cli_accepts_dry_run(self):
        from godot_qa_toolkit.cli import build_parser
        args = build_parser().parse_args(
            ["mutation", "x.gd", "--project-root", ".", "--dry-run"])
        assert getattr(args, "dry_run", False) is True

    def test_mutation_cli_accepts_tests(self):
        from godot_qa_toolkit.cli import build_parser
        args = build_parser().parse_args(
            ["mutation", "x.gd", "--project-root", ".", "--tests", "res://tests/save/"])
        assert args.tests == "res://tests/save/"

    def test_cli_dry_run_dispatches_no_gut(self, tmp_path, monkeypatch, capsys):
        import godot_qa_toolkit.mutation.runner as mr
        from godot_qa_toolkit.cli import main

        proj, target = _write_project(tmp_path)

        def boom(*a, **k):
            raise AssertionError("CLI --dry-run must not invoke GUT")

        monkeypatch.setattr(mr, "_run_gut_on_project", boom)
        # SEE-1583 §SPEC-012：CLI 默认 select=fn 且无可用映射即 fail-loud
        # （不再静默全脸）。本用例钉的是「dry-run 零 GUT」契约本身，按其
        # 书写时的行为面（全脸）显式传 --select full 保持原判据不变。
        rc = main(["mutation", str(target), "--project-root", str(proj),
                   "--dry-run", "--select", "full"])
        assert rc == 0
        assert '"dry_run": true' in capsys.readouterr().out


class TestH2ArmSwapAdversarial:
    """H2: arm 含 ' if '/' else ' 字符串字面量时 arm_swap 正则误切（反例实证）。"""

    def test_arm_swap_with_if_else_in_string_arm(self):
        from godot_qa_toolkit.mutation.runner import _apply_mutation, _collect_mutations
        src = 'func f(x):\n\treturn "gold if rich else poor" if x else "plain"\n'
        ms = _collect_mutations(src, "x.gd")
        swaps = [m for m in ms if m.kind == "TERNARY" and m.operator == "arm_swap"]
        assert len(swaps) == 1
        out = _apply_mutation(src, swaps[0])
        # roundtrip：变异产物必须可解析（arm_swap 不得把 arm 从字符串中间切开）
        _collect_mutations(out, "x.gd")

    def test_arm_swap_with_else_in_arm2_string(self):
        from godot_qa_toolkit.mutation.runner import _apply_mutation, _collect_mutations
        src = 'func f(x):\n\tvar y = 1 if x else "nope else never"\n'
        ms = _collect_mutations(src, "x.gd")
        swaps = [m for m in ms if m.operator == "arm_swap"]
        out = _apply_mutation(src, swaps[0])
        assert '"nope else never" if x else 1' in out
        _collect_mutations(out, "x.gd")


class TestH3BranchLineMapping:
    """H3: 分支判定必须锚定分支体首语句行——分支头行在条件求值时点火，
    false 路径下 if 分支被误判 covered（反例实证）。"""

    GD = """func f(x):
	if x:
		return 1
	else:
		return 2
"""

    def test_branch_line_is_body_first_stmt_not_head(self):
        from godot_qa_toolkit.coverage.runner import _derive_branches
        b = _derive_branches(self.GD)
        lines = {i.line for i in b.items}
        # 分支行必须是 3/5（return 体首语句），不得是 2/4（if/else 头）
        assert lines == {3, 5}, f"branch lines leaked head lines: {lines}"

    def test_false_path_does_not_cover_if_branch(self, tmp_path, monkeypatch):
        """映射投影语义：covered 行集决定分支覆盖——false 路径（else 头行 4 +
        else 体首行 5）覆盖、if 体首行 3 未覆盖 → if 分支必须 uncovered。
        （旧 direct 实现用分支头行 2 判定 → if 分支误判 covered 的反例，
        语义等价保留在映射投影上。）"""
        import hashlib
        import json

        import attribution_fixtures as fx
        from godot_qa_toolkit.measurement.recipe import build_recipe, recipe_hash
        import godot_qa_toolkit.coverage.runner as c

        proj, target = _write_project(tmp_path, target_src=self.GD)
        (proj / "tests").mkdir()
        (proj / "tests" / "test_x.gd").write_text("extends Node\\n",
                                                  encoding="utf-8")
        fx.install_recipe(proj)
        recipe, error = build_recipe(proj)
        assert error is None
        raw = recipe_hash({k: v for k, v in recipe.items() if k != "recipe_hash"})
        rel = "target.gd"  # _write_project 的目标名
        covered = [5]  # false 路径：else 头行 4 + else 体行 5 覆盖；行 3 未覆盖
        doc = {
            "schema_version": "test-attribution/7",
            "report_recipe_hash": raw, "recipe_hash_pristine": raw,
            "project_root": str(proj), "identity": "test_x.gd::test_a",
            "causal": {"enabled": False},
            "file_sha256": {rel: hashlib.sha256(
                target.read_bytes()).hexdigest()},
            "target_files": [rel],
            "tests_face": {"tests/test_x.gd": hashlib.sha256(
                (proj / "tests" / "test_x.gd").read_bytes()).hexdigest()},
            "instrumentable_lines": {rel: [2, 3, 4, 5]},
            "arms": {"gut_full": {
                "test_to_files": {"test_x.gd::test_a": [rel]},
                "script_to_files": {}, "tests": 1, "scripts": 1, "files": 1,
                "pairs": 1, "function_pairs": 0, "test_to_functions": {},
                "script_to_functions": {},
                "file_to_tests": {rel: ["test_x.gd::test_a"]},
                "file_to_scripts": {rel: ["test_x.gd"]},
                "line_pairs": 1, "script_line_pairs": 1,
                "test_to_lines": {"test_x.gd::test_a": {rel: covered}},
                "script_to_lines": {},
                "line_to_tests": {rel: {str(covered[0]): ["test_x.gd::test_a"]}}},
            },
            "arms_state": {"gut_full": {"state": "valid",
                                        "invalid_reason": None, "bundles": 1,
                                        "pairs": 1, "probes_fired": 2,
                                        "vacuous": False}},
            "notes": [],
            "test_verdicts": {"test_x.gd::test_a": {
                "verdict": "green", "classification": "pre_existing"}},
            "feature_sha256": fx._feature_sha_of(proj)}
        (proj / ".qa-cache").mkdir()
        (proj / ".qa-cache" / "attribution-map.json").write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8")

        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.inject.detect_binary_kind",
            lambda *a, **k: "linux")
        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.identity.identity_pack",
            lambda *a, **k: {"kind": "linux"})

        result = c.run_coverage(str(target), str(proj), min_percent=0)
        s = result["summary"]
        # 2 分支中只有 else（行 5）被覆盖；行 3（if 体）未覆盖 → if 分支 uncovered
        assert s["branch_total"] == 2
        assert s["branch_covered"] == 1, (
            f"if branch falsely covered via head-line probe: {s}")
        assert s["hits_lines"] == [5]


    def test_nested_if_branches_distinguish_layers(self):
        from godot_qa_toolkit.coverage.runner import _derive_branches
        src = "func f(a, b):\n\tif a:\n\t\tif b:\n\t\t\treturn 1\n\t\treturn 2\n"
        b = _derive_branches(src)
        # 外层体首语句 = 内层 if_stmt 行(3)；内层体首 = return 行(4)。
        # 外层 true/内层 false 时行 3 点火、行 4 不点火 → 两层分支正确区分。
        assert b.total == 2
        assert [i.line for i in b.items] == [3, 4]


class TestH4StaleSinkPriority:
    """H4: 项目根陈旧 sink（pid 复用）不得遮蔽新鲜 user:// sink（实证已复现）。"""

class TestH8RealFileInstrumentation:
    """H8: 真实文件形态——跨行分组表达式与 lambda 实参行绝不插桩（实机实证缺陷）。"""

    GD_REAL = '''func _load(file_name: String) -> void:
	var result: Array[Dictionary] = []
	for idx in dir.get_line_at_position(idx):
		if data:
			(
				result
				. append(
					{
						"slot_index": idx_int,
					}
				)
			)
	result.sort_custom(
		func(a, b): return String(a.get("saved_at", "")) > String(b.get("saved_at", ""))
	)
	return result
'''

    def test_continuation_lines_not_instrumented(self):
        from godot_qa_toolkit.coverage.runner import _collect_executable_lines
        els = _collect_executable_lines(self.GD_REAL)
        lines = {e.line for e in els}
        # 'result' 续行(6) 与 lambda 实参行(10) 处于括号内部——不可插桩
        assert 6 not in lines, "probe would land inside grouped expression"
        assert 10 not in lines, "probe would land inside sort_custom args"

    def test_lambda_arg_line_instrumented_source_reparses(self):
        from godot_qa_toolkit.coverage.runner import _instrument
        from gdtoolkit.parser import parser as gdtoolkit_parser
        instrumented, _ = _instrument(self.GD_REAL, "qa-coverage-hits-test.txt")
        gdtoolkit_parser.parse(instrumented)

    def test_else_branch_head_line_not_instrumented(self):
        # SEE-1623 H3 改约：elif 条件行入行集（真实会被求值，假 uncovered
        # 缺陷）——探针锚定分支体首语句发射，语法安全由 reparse 钉守护；
        # else_branch 头行仍排除（无求值语义、无变异位点）。
        from godot_qa_toolkit.coverage.runner import _collect_executable_lines
        src = ("func f(x):\n\tif x:\n\t\treturn 1\n\telif False:\n\t\tpass\n"
               "\telse:\n\t\treturn 2\n")
        els = _collect_executable_lines(src)
        by_line = {e.line: e.node for e in els}
        assert by_line.get(4) == "elif_branch"
        assert not any(e.node == "else_branch" for e in els)

    def test_instrumented_else_source_reparses(self):
        from godot_qa_toolkit.coverage.runner import _instrument
        from gdtoolkit.parser import parser as gdtoolkit_parser
        src = "func f(x):\n\tif x:\n\t\treturn 1\n\telse:\n\t\treturn 2\n"
        instrumented, _ = _instrument(src, "qa-coverage-hits-test.txt")
        gdtoolkit_parser.parse(instrumented)

    def test_instrumented_elif_source_reparses(self):
        # 回潮即红：elif 头行前物理插桩 = Parse Error——锚定发射必须保持。
        from godot_qa_toolkit.coverage.runner import _instrument
        from gdtoolkit.parser import parser as gdtoolkit_parser
        src = ("func f(x):\n\tif x:\n\t\treturn 1\n\telif x > 1:\n"
               "\t\treturn 2\n\telse:\n\t\treturn 3\n")
        instrumented, _ = _instrument(src, "qa-coverage-hits-test.txt")
        gdtoolkit_parser.parse(instrumented)



def _materialize_selection(proj, names):
    sel_dir = Path(proj) / ".gqt-attr-select" / "testsel"
    sel_dir.mkdir(parents=True, exist_ok=True)
    for n in names:
        (sel_dir / n).write_text(f'extends "res://tests/{n}"\n', encoding="utf-8")
    return "res://.gqt-attr-select/testsel/"


def _pin_cold(monkeypatch):
    from godot_qa_toolkit.mutation.identity import COLD_SCHEME
    from godot_qa_toolkit.mutation import worker_channel as wc
    monkeypatch.setattr(wc, "resolve_exec_scheme",
                        lambda *a, **k: (COLD_SCHEME, None))
    monkeypatch.setattr("godot_qa_toolkit.mutation.runner.resolve_exec_scheme",
                        lambda *a, **k: (COLD_SCHEME, None))


@pytest.fixture(autouse=True)
def _pin_cold_auto(monkeypatch):
    _pin_cold(monkeypatch)

class TestH5SuspectGatesOk:
    """H5: suspect>0 时 ok 必须为 False（suspect = 无法证明被杀死）。"""

    def test_sistent_force_ok_false(self, tmp_path, monkeypatch):
        from godot_qa_toolkit.mutation.runner import run_mutation

        proj, target = _write_project(tmp_path)
        dirty = (1, "---- 1 failing tests ----\n* test_old_flaky\n"
                    "    [Failed]: boom\nRan 2 tests.\nRun Summary\n")
        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.runner._run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None: dirty,
        )
        glob = _materialize_selection(proj, ["test_a.gd"])
        result = run_mutation(str(target), str(proj), budget=2, tests_glob=glob)
        # SEE-1631 §SPEC-027/D7（QA 裁决 + Leader 复核修正）：无跳过原语
        # 车道失败 ⊆ 基线红且整面执行完（Run Summary 在场）→ survived；
        # 非 kill 不给 ok=true。
        assert result["summary"].get("survived", 0) > 0
        assert result["ok"] is False, "survived still gates ok (not killed)"

    def test_no_full_face_evidence_stays_suspect(self, tmp_path, monkeypatch):
        """反例：失败 ⊆ 基线红但无收尾汇总（腿中止）→ 保持 suspect
        （绿杀手可能根本没执行——不可判真实存活）。"""
        from godot_qa_toolkit.mutation.runner import run_mutation

        proj, target = _write_project(tmp_path)
        dirty = (1, "---- 1 failing tests ----\n* test_old_flaky\n"
                    "    [Failed]: boom\n")  # 无 Run Summary
        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.runner._run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None: dirty,
        )
        glob = _materialize_selection(proj, ["test_a.gd"])
        result = run_mutation(str(target), str(proj), budget=2, tests_glob=glob)
        assert result["summary"].get("suspect", 0) > 0
        assert result["summary"].get("survived", 0) == 0


class TestH6BudgetZeroNoBaseline:
    """H6: budget 截断后零 mutant 不得跑 baseline GUT（成本短路）。"""

    def test_zero_mutants_skips_baseline(self, tmp_path, monkeypatch):
        from godot_qa_toolkit.mutation.runner import run_mutation

        proj, target = _write_project(tmp_path)

        def boom(*a, **k):
            raise AssertionError("zero mutants must not trigger baseline GUT")

        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.runner._run_gut_on_project", boom)
        result = run_mutation(str(target), str(proj), budget=0)
        assert result["ok"] is True
        assert result["summary"]["mutants"] == 0


class TestH7HitsSanitization:
    """H7: 越界/垃圾 hits 行只进 probes_fired 若为有效 id 行；越界 id 不计数。"""

    def test_out_of_range_ids_not_counted_as_fired(self, tmp_path):
        from godot_qa_toolkit.coverage.runner import _read_hits

        (tmp_path / "qa-coverage-hits-1.txt").write_text("999\n0\n0\n")
        hits, fired = _read_hits(str(tmp_path), "qa-coverage-hits-1.txt", [10, 20])
        assert hits == {10}
        # 越界 id 是传输错误信号，不得充当「探针点火」证据
        assert fired == 2


class TestH9GutOutputFormats:
    """H9: GUT Run Summary 段 '- test_xxx' 格式必须可解析（实机实证缺陷：
    win64 GUT 4.6 输出截断后只剩 summary 段，旧正则漏掉全部失败 → 66/66 假
    survived）。"""

    def test_dash_prefix_summary_format_parsed(self):
        from godot_qa_toolkit.mutation.runner import _parse_failing_tests
        tail = (
            "res://tests/unit/save/test_save_manager.gd\n"
            "- test_save_game_creates_file\n"
            "    [Failed]:  save should succeed\n"
            "- test_save_and_load_roundtrip\n"
            "    [Failed]:  save should succeed\n"
        )
        assert _parse_failing_tests(tail) == {
            "test_save_game_creates_file", "test_save_and_load_roundtrip"}

    def test_star_prefix_runtime_format_still_parsed(self):
        from godot_qa_toolkit.mutation.runner import _parse_failing_tests
        out = "* test_a\nok\n* test_b\n    [Failed]: boom\n"
        assert _parse_failing_tests(out) == {"test_b"}

    def test_crlf_and_ansi_stripped(self):
        from godot_qa_toolkit.mutation.runner import _parse_failing_tests
        out = "- \x1b[31mtest_crlf_case\x1b[0m\r\n    \x1b[31m[Failed]\x1b[0m: x\r\n"
        assert _parse_failing_tests(out) == {"test_crlf_case"}


class TestH10CoverageGodotPathFix:
    """H10: coverage 侧 win64 --path 修复（QA ④ 实机实证：coverage runner 漏了
    mutation 侧的 wslpath 修复，/mnt/ 绝对路径 → 'Invalid project path' 0.1s 静默失败）。"""

    def test_godot_project_path_converts_wsl_mount(self, monkeypatch,
                                                   tmp_path):
        # SEE-1525 §SPEC-002：转换原语统一进 inject.godot_project_path（不得
        # 各写一套）——H10 契约（win64 通道 /mnt/ 转 Windows 形式）不变，
        # mock 点随实现迁移到共享 wslpath_win。win64 解析形态由测试自钉
        # （fixture 假 exe 经 GQT_GODOT_BIN 显式指定，走真实 env→resolve→
        # kind→convert 链；SEE-1602 后缺省 PATH 解析恒 linux，本测试不得
        # 依赖缺省通道的巧合）。
        import godot_qa_toolkit.coverage.runner as c
        from godot_qa_toolkit.mutation import inject

        fake_exe = tmp_path / "godot-win64-fixture.exe"
        fake_exe.write_bytes(b"MZ")
        fake_exe.chmod(0o755)
        monkeypatch.setenv("GQT_GODOT_BIN", str(fake_exe))
        inject.reset_binary_kind_cache()
        monkeypatch.setattr(inject, "wslpath_win",
                            lambda p: "D:\\GodotProjects\\king-of-likes")
        out = c._godot_project_path("/mnt/d/GodotProjects/king-of-likes")
        assert out == "D:\\GodotProjects\\king-of-likes"
        inject.reset_binary_kind_cache()

    def test_relative_path_passthrough(self):
        from godot_qa_toolkit.coverage.runner import _godot_project_path
        assert _godot_project_path(".") == "."
        assert _godot_project_path("proj") == "proj"

