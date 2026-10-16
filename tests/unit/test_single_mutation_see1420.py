"""§SPEC-1420-11：P3 单点重跑（定案 D7 卡尺）——`--mutation <id>` /
`--lines N`，1 baseline + 1 mutant 秒级确认。

显式寻址 = 强制新执行（绕过差分复用与 coverage 前置过滤——卡尺的公平通
道）；无命中 = 显式错误契约（ok=false，绝不静默空跑）。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.cli import build_parser
from godot_qa_toolkit.mutation import runner as mrunner

SRC = ("func add(a, b):\n"
       "\treturn a + b\n"
       "\n"
       "func sub(a, b):\n"
       "\treturn a - b\n")

KILL_TAIL = "* test_x\n\t\ttest_x [Failed]"


def _write_project(tmp_path, src=SRC):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    target = proj / "calc.gd"
    target.write_text(src)
    return proj, target


def _first_id(tmp_path):
    """任取一个真实存在位点 id（以目标文件路径为 file 成分，scan 确定）。"""
    from godot_qa_toolkit.mutation.ops import _collect_mutations
    from godot_qa_toolkit.mutation.scopemanifest import build_manifest
    target = tmp_path / "proj" / "calc.gd"
    _, _, tagged, _ = build_manifest(SRC, str(target),
                                     _collect_mutations(SRC, str(target)))
    return tagged[0].stable_id


class TestSingleMutation:
    def test_single_id_runs_one_baseline_plus_one_mutant(self, tmp_path,
                                                         monkeypatch):
        proj, target = _write_project(tmp_path)
        calls = {"n": 0}

        def fake(root, timeout_s=60, tests_glob=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return (0, "baseline ok")
            return (1, KILL_TAIL)

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake)
        sid = _first_id(tmp_path)
        r = mrunner.run_mutation(str(target), str(proj), budget=10,
                                 tests_glob="res://tests/",
                                 select_ids=[sid])
        # 卡尺判据：GUT 调用计数 = 2（1 baseline + 1 mutant）
        assert calls["n"] == 2
        assert r["summary"]["executed"] == 1
        assert r["summary"]["mutants"] == 1
        assert r["ok"] is True  # 被杀死

    def test_selection_bypasses_diff_reuse(self, tmp_path, monkeypatch):
        """已 killed 的位点显式寻址 → 强制新执行（卡尺不做缓存信任）。"""
        proj, target = _write_project(tmp_path)
        sid = _first_id(tmp_path)

        def kill_all(root, timeout_s=60, tests_glob=None):
            return (1, KILL_TAIL)

        monkeypatch.setattr(mrunner, "_run_gut_on_project", kill_all)
        mrunner.run_mutation(str(target), str(proj), budget=10,
                             tests_glob="res://tests/")
        calls2 = {"n": 0}

        def counting(root, timeout_s=60, tests_glob=None):
            calls2["n"] += 1
            if calls2["n"] == 1:
                return (0, "baseline ok")
            return (1, KILL_TAIL)

        monkeypatch.setattr(mrunner, "_run_gut_on_project", counting)
        r = mrunner.run_mutation(str(target), str(proj), budget=10,
                                 tests_glob="res://tests/",
                                 select_ids=[sid])
        assert calls2["n"] == 2  # 无视 diff 复用：baseline + 强制执行
        assert r["summary"]["executed"] == 1

    def test_unknown_id_is_explicit_error(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)

        def fake(root, timeout_s=60, tests_glob=None):
            return (0, "baseline ok")

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake)
        try:
            mrunner.run_mutation(str(target), str(proj), budget=10,
                                 tests_glob="res://tests/",
                                 select_ids=["calc.gd#nope#99"])
            raised = False
        except ValueError as e:
            raised = True
            assert "nope#99" in str(e)
        assert raised, "unknown id must fail loudly (no silent empty run)"


class TestMutationLines:
    def test_lines_selects_sites_on_that_line(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)
        calls = {"n": 0}

        def fake(root, timeout_s=60, tests_glob=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return (0, "baseline ok")
            return (1, KILL_TAIL)

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake)
        # SRC 行 2 = `return a + b`（3 个 AOR 位点）
        r = mrunner.run_mutation(str(target), str(proj), budget=10,
                                 tests_glob="res://tests/", select_lines=[2])
        assert r["summary"]["executed"] == 3
        assert calls["n"] == 4  # baseline + 3
        assert all(f["line"] == 2 for f in r["failures"]) or r["ok"] is True

    def test_lines_no_hit_is_explicit_error(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)
        monkeypatch.setattr(mrunner, "_run_gut_on_project",
                            lambda root, timeout_s=60, tests_glob=None:
                            (0, "baseline ok"))
        try:
            mrunner.run_mutation(str(target), str(proj), budget=10,
                                 tests_glob="res://tests/",
                                 select_lines=[999])
            raised = False
        except ValueError:
            raised = True
        assert raised


class TestCliWiring:
    def test_cli_flags_parse(self):
        args = build_parser().parse_args(
            ["mutation", "x.gd", "--project-root", "p",
             "--mutation", "x.gd#add#1"])
        assert args.mutation == "x.gd#add#1"
        args2 = build_parser().parse_args(
            ["mutation", "x.gd", "--project-root", "p", "--lines", "3"])
        assert args2.lines == 3
