"""§SPEC-004：verdict 语义单测。

- ``unreachable`` 不进 verdict、不 skip：只做疑似孤儿诊断披露（静态不可
  判定面只披露不判定——动态 load/hub 截断/uid 间接引用真实反例）；
- ``invalid_mutant``/``run_error``/``timeout``/``killed``/``survived`` 语义
  与既有时序口径不变；
- 疑似孤儿披露 = 目标零静态引用 ∧ 存在 survived → 记录逐条标记 + summary
  名单；有静态引用（autoload/tscn/tests/其他脚本字面量）→ 不披露。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import diffengine, runner as mrunner
from godot_qa_toolkit.mutation.diffengine import SKIP_VERDICTS, RETRY_VERDICTS
from godot_qa_toolkit.mutation.identity import suspected_orphan
from godot_qa_toolkit.mutation.runner import run_mutation


def _write_project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    target = proj / "calc.gd"
    target.write_text("func add(a, b):\n\treturn a + b\n")
    return proj, target


class TestUnreachableNotAVerdict:
    def test_unreachable_absent_from_skip_and_retry_sets(self):
        """unreachable 不进 verdict：既不是复用档也不是重跑档——它根本
        不存在于 verdict 词表（诊断面独立于判定面）。"""
        assert "unreachable" not in SKIP_VERDICTS
        assert "unreachable" not in RETRY_VERDICTS
        all_verdicts = SKIP_VERDICTS | RETRY_VERDICTS
        assert all_verdicts == {"killed", "suspect", "invalid_mutant",
                                "survived", "timeout", "timeout_pinned",
                                "run_error"}

    def test_unreachable_sites_are_not_skipped(self, tmp_path, monkeypatch):
        """零静态引用目标的位点照常执行（不 skip）——orphan 负对照行为面。"""
        proj, target = _write_project(tmp_path)
        # calc.gd 无任何引用（无 autoload/tests/tscn/其他脚本指向它）
        calls = {"n": 0}

        def fake(root, timeout_s=60, tests_glob=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return (0, "* test_base\n  [passed]")
            return (1, "* test_x\n  [Failed] boom")

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake)
        r = run_mutation(str(target), str(proj), budget=3)
        # 位点被真实执行（killed），没有被静态结论跳过
        assert calls["n"] == 1 + 3
        assert r["summary"]["killed"] == 3
        # 全 killed → 无孤儿披露面（披露只挂 survived）
        assert "suspected_orphans" not in r["summary"]


class TestVerdictSemanticsUnchanged:
    def test_killed_requires_new_failures(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)
        calls = {"n": 0}

        def fake(root, timeout_s=60, tests_glob=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return (0, "* t0\n[passed]")
            return (1, "* test_new_kill\n[Failed] x")

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake)
        r = run_mutation(str(target), str(proj), budget=2)
        assert r["summary"]["killed"] == 2
        assert r["ok"] is True

    def test_survived_on_clean_pass(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)
        monkeypatch.setattr(
            mrunner, "_run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None: (0, "[passed]"))
        r = run_mutation(str(target), str(proj), budget=2)
        assert r["summary"]["survived"] == 2
        assert r["ok"] is False

    def test_timeout_keeps_existing_semantics(self, tmp_path, monkeypatch):
        import subprocess
        proj, target = _write_project(tmp_path)
        calls = {"n": 0}

        def fake(root, timeout_s=60, tests_glob=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return (0, "* t0\n[passed]")
            raise subprocess.TimeoutExpired(cmd="godot", timeout=timeout_s)

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake)
        r = run_mutation(str(target), str(proj), budget=2)
        assert r["summary"]["timeout"] == 2

    def test_launch_failure_still_run_error(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)
        monkeypatch.setattr(
            mrunner, "_run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None:
                (1, "ERROR: Attempt to open script 'res://addons/gut/"
                    "gut_cmdln.gd' resulted in error 'File not found'"))
        r = run_mutation(str(target), str(proj), budget=2)
        assert r["summary"].get("run_error") is True

    def test_suspect_on_dirty_baseline_unchanged(self, tmp_path, monkeypatch):
        """suspect（脏 baseline ∧ 差集空）语义不变——不可证杀保守档。"""
        proj, target = _write_project(tmp_path)

        def fake(root, timeout_s=60, tests_glob=None):
            return (1, "* test_old\n[Failed] pre-existing")

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake)
        r = run_mutation(str(target), str(proj), budget=2)
        assert r["summary"]["suspect"] == 2
        assert r["ok"] is False


class TestSuspectedOrphanDisclosure:
    def test_orphan_survivor_disclosed(self, tmp_path, monkeypatch):
        """orphan 负对照：零静态引用 + survived → 记录标记 + summary 名单。"""
        proj, target = _write_project(tmp_path)
        monkeypatch.setattr(
            mrunner, "_run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None: (0, "[passed]"))
        r = run_mutation(str(target), str(proj), budget=2)
        s = r["summary"]
        assert suspected_orphan(str(proj), "res://calc.gd") is True
        assert s["survived"] == 2
        assert s["suspected_orphans"] == [f["id"] for f in r["failures"]]
        assert all(f.get("suspected_orphan") is True for f in r["failures"])

    def test_referenced_target_not_disclosed(self, tmp_path, monkeypatch):
        """有静态引用（tests 字面量指向目标）→ 不披露（正常 survived 语义）。"""
        proj, target = _write_project(tmp_path)
        tests = proj / "tests"
        tests.mkdir()
        (tests / "test_calc.gd").write_text(
            'extends GutTest\n\nconst C := preload("res://calc.gd")\n\n'
            "func test_calc():\n\tpass\n")
        monkeypatch.setattr(
            mrunner, "_run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None: (0, "[passed]"))
        r = run_mutation(str(target), str(proj), budget=2)
        assert suspected_orphan(str(proj), "res://calc.gd") is False
        assert "suspected_orphans" not in r["summary"]

    def test_autoload_wiring_defeats_orphan(self, tmp_path):
        """autoload 接线（project.godot）即引用——save_manager 形态反例。"""
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "project.godot").write_text(
            '[autoload]\nSaveManager="*res://autoload/save_manager.gd"\n')
        autoload = proj / "autoload"
        autoload.mkdir()
        (autoload / "save_manager.gd").write_text("extends Node\n")
        assert suspected_orphan(str(proj),
                                "res://autoload/save_manager.gd") is False

    def test_scene_reference_defeats_orphan(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        scene = proj / "main.tscn"
        scene.write_text('[ext_resource type="Script" '
                         'path="res://forms/scene_script.gd" id="1"]\n')
        (proj / "forms").mkdir()
        (proj / "forms" / "scene_script.gd").write_text("extends Node\n")
        assert suspected_orphan(str(proj),
                                "res://forms/scene_script.gd") is False

    def test_qa_cache_copies_not_counted_as_references(self, tmp_path):
        """.qa-cache 影子槽副本不是引用（池槽会复制全树）。"""
        proj = tmp_path / "proj"
        proj.mkdir()
        pool_copy = proj / ".qa-cache" / "mutation-pool" / "slots" / "s"
        pool_copy.mkdir(parents=True)
        (pool_copy / "orphan_form.gd").write_text("extends Node\n")
        assert suspected_orphan(str(proj), "res://orphan_form.gd") is True

    def test_stem_word_reference_defeats_orphan(self, tmp_path):
        """无 res:// 字面量、但其他脚本按词干提及目标 → 非孤儿（动态 load
        构造路径的引用只能由词干层捕获）。"""
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "inventory.gd").write_text("extends Node\n")
        (proj / "game.gd").write_text(
            "extends Node\n# inventory keeps the item state\n")
        assert suspected_orphan(str(proj), "res://inventory.gd") is False
