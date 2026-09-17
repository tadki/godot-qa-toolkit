"""SPEC-004/005/008/009/010: mutation 扩展与有效性分类 unit tests (SEE-1312).

TDD 配对：先 RED 再 GREEN。mock GUT 模式沿既有 test_mutation.py 惯例。
"""

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


class TestTernaryMutations:
    """SPEC-004: 三元表达式 cond 取反 + 两臂交换。"""

    GD_TERNARY = "func f(x):\n\treturn 1 if x else 2\n"

    def test_ternary_produces_mutants(self):
        ms = _collect_mutations(self.GD_TERNARY, "x.gd")
        kinds = {m.kind for m in ms}
        assert "TERNARY" in kinds

    def test_ternary_cond_negation(self):
        ms = _collect_mutations(self.GD_TERNARY, "x.gd")
        neg = [m for m in ms if m.kind == "TERNARY" and m.operator == "cond_not"]
        assert len(neg) == 1

    def test_ternary_arm_swap(self):
        ms = _collect_mutations(self.GD_TERNARY, "x.gd")
        swaps = [m for m in ms if m.kind == "TERNARY" and m.operator == "arm_swap"]
        assert len(swaps) == 1

    def test_ternary_cond_not_roundtrip(self):
        src = self.GD_TERNARY
        ms = _collect_mutations(src, "x.gd")
        neg = next(m for m in ms if m.operator == "cond_not")
        out = _apply_mutation(src, neg)
        # cond 取反 = not (<cond>) 包裹
        assert "not ( x )".replace(" ","") in out.replace(" ","")
        # 重新解析必须合法（语法往返保证）
        _collect_mutations(out, "x.gd")

    def test_ternary_arm_swap_roundtrip(self):
        src = self.GD_TERNARY
        ms = _collect_mutations(src, "x.gd")
        swap = next(m for m in ms if m.operator == "arm_swap")
        out = _apply_mutation(src, swap)
        assert "2 if x else 1" in out
        _collect_mutations(out, "x.gd")

    def test_multiline_ternary_roundtrip(self):
        src = "func f(x):\n\tvar y = (\n\t\t1 if x else 2)\n"
        ms = _collect_mutations(src, "x.gd")
        assert any(m.kind == "TERNARY" for m in ms)
        for m in ms:
            out = _apply_mutation(src, m)
            _collect_mutations(out, "x.gd")


class TestGuardNotMutations:
    """SPEC-005: 守卫取反 + 去重（守卫表达式含可变异算子则不产守卫取反）。"""

    def test_guard_not_on_clean_condition(self):
        # is_valid(x) 不含可变异算子 → 产守卫取反
        src = "func f(x):\n\tif is_valid(x):\n\t\treturn true\n"
        ms = _collect_mutations(src, "x.gd")
        guards = [m for m in ms if m.kind == "GUARD_NOT"]
        assert len(guards) == 1

    def test_guard_not_skipped_when_condition_has_operators(self):
        # x > 0 含 ROR 可变异点 → 守卫取反冗余，去重跳过
        src = "func f(x):\n\tif x > 0:\n\t\treturn true\n"
        ms = _collect_mutations(src, "x.gd")
        assert not any(m.kind == "GUARD_NOT" for m in ms)

    def test_guard_not_on_while(self):
        src = "func f(x):\n\twhile is_ready(x):\n\t\tpass\n"
        ms = _collect_mutations(src, "x.gd")
        assert any(m.kind == "GUARD_NOT" for m in ms)

    def test_guard_not_roundtrip(self):
        src = "func f(x):\n\tif is_valid(x):\n\t\treturn true\n"
        ms = _collect_mutations(src, "x.gd")
        g = next(m for m in ms if m.kind == "GUARD_NOT")
        out = _apply_mutation(src, g)
        assert "if not (is_valid(x))" in out
        _collect_mutations(out, "x.gd")


class TestOpsSecondTier:
    """SPEC-008: 算子族第二梯队 + UOI token 级修正。"""

    def test_logical_and_swapped(self):
        src = "func f(a, b):\n\tif a and b:\n\t\treturn true\n"
        ms = _collect_mutations(src, "x.gd")
        logic = [m for m in ms if m.kind == "LOGICAL"]
        assert any(m.original == "and" and m.mutated == "or" for m in logic)

    def test_membership_in_negated(self):
        src = "func f(a, b):\n\tif a in b:\n\t\treturn true\n"
        ms = _collect_mutations(src, "x.gd")
        mem = [m for m in ms if m.kind == "MEMBERSHIP"]
        assert any(m.original == "in" and m.mutated == "not in" for m in mem)

    def test_is_negated(self):
        src = "func f(a):\n\tif a is int:\n\t\treturn true\n"
        ms = _collect_mutations(src, "x.gd")
        ism = [m for m in ms if m.kind == "IS_NOT"]
        assert len(ism) == 1

    def test_is_not_roundtrip(self):
        src = "func f(a):\n\tif a is int:\n\t\treturn true\n"
        ms = _collect_mutations(src, "x.gd")
        m = next(m for m in ms if m.kind == "IS_NOT")
        out = _apply_mutation(src, m)
        assert "not (a is int)" in out
        _collect_mutations(out, "x.gd")

    def test_uoi_deletes_not_token(self):
        # 经典删 not：not a → a（不再是语义等价的 not(false) and 拼接）
        src = "func f(a):\n\tif not a:\n\t\treturn true\n"
        ms = _collect_mutations(src, "x.gd")
        uoi = [m for m in ms if m.kind == "UOI"]
        assert len(uoi) == 1
        out = _apply_mutation(src, uoi[0])
        assert "if a:" in " ".join(out.split())

    def test_uoi_excludes_not_in(self):
        # 'not in' 的 not 不是一元取反——绝不能删
        src = "func f(a, b):\n\tif a not in b:\n\t\treturn true\n"
        ms = _collect_mutations(src, "x.gd")
        assert not any(m.kind == "UOI" for m in ms)

    def test_constants_collected(self):
        # 常量边界翻转现可触达（原 atom 节点在 lark 折叠下永不出现，已死代码）
        src = "func f():\n\tvar x = true\n"
        ms = _collect_mutations(src, "x.gd")
        assert any(m.kind == "boundary" and m.original == "true" for m in ms)

    def test_constants_do_not_match_substrings(self):
        # 10 不是 1 的变异点（Token 级精确匹配）
        src = "func f():\n\tvar x = 10\n"
        ms = _collect_mutations(src, "x.gd")
        assert not any(m.kind == "boundary" for m in ms)


class TestCostControl:
    """SPEC-009: --tests 子集 + survived 全量复核 + --dry-run + 自适应 timeout。"""

    def _write_project(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "addons" / "gut").mkdir(parents=True)
        (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
        target = proj / "calc.gd"
        target.write_text("func add(a, b):\n\treturn a + b\n")
        return proj, target

    def test_dry_run_lists_mutants_without_gut(self, tmp_path, monkeypatch):
        proj, target = self._write_project(tmp_path)

        def boom(*a, **k):
            raise AssertionError("dry-run must not invoke GUT")

        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.runner._run_gut_on_project", boom
        )
        result = run_mutation(str(target), str(proj), dry_run=True)
        assert result["ok"] is True
        assert result["summary"]["dry_run"] is True
        assert result["summary"]["mutants"] > 0

    def test_tests_subset_scopes_gut_dir(self, tmp_path, monkeypatch):
        proj, target = self._write_project(tmp_path)
        captured = {}

        def fake_run(root, timeout_s=60, tests_glob=None):
            captured["tests_glob"] = tests_glob
            return (0, "pass")

        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.runner._run_gut_on_project", fake_run
        )
        run_mutation(str(target), str(proj), tests_glob="res://tests/save/")
        assert captured["tests_glob"] == "res://tests/save/"

    def test_adaptive_timeout_scales_with_baseline(self, tmp_path, monkeypatch):
        proj, target = self._write_project(tmp_path)
        captured = {}

        def fake_run(root, timeout_s=60, tests_glob=None):
            captured.setdefault("timeouts", []).append(timeout_s)
            return (0, "pass")

        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.runner._run_gut_on_project", fake_run
        )
        # baseline 耗时 10s（fake），timeout_per_test=20 → 自适应上限 ≥ 20
        result = run_mutation(str(target), str(proj), timeout_s=20)
        # 自适应上限记录在 summary：baseline 快（fake 秒回）→ adaptive_timeout
        # 不低于调用方显式 timeout（只放大不收紧）
        assert result["summary"].get("adaptive_timeout_s", 0) >= 20
        assert captured["timeouts"], "baseline run must record timeout ceiling"


class TestValidityClassification:
    """SPEC-010: invalid_mutant 语法预检 + suspect 标签。"""

    def _write_project(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "addons" / "gut").mkdir(parents=True)
        (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
        target = proj / "calc.gd"
        target.write_text("func add(a, b):\n\treturn a + b\n")
        return proj, target

    def test_suspect_when_mutant_run_matches_dirty_baseline(self, tmp_path, monkeypatch):
        # baseline 已红 ∧ mutant run 失败集合 == baseline（差集为空）→ suspect
        # 而非 survived：无法区分「测试没抓住」与「测试根本没跑到」。
        proj, target = self._write_project(tmp_path)
        dirty = (1, "---- 1 failing tests ----\n* test_old_flaky\n    [Failed]: boom\n")
        calls = {"n": 0}

        def fake_run(root, timeout_s=60, tests_glob=None):
            calls["n"] += 1
            return dirty if calls["n"] > 1 else dirty

        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.runner._run_gut_on_project", fake_run
        )
        result = run_mutation(str(target), str(proj), budget=2)
        s = result["summary"]
        assert s.get("suspect", 0) == s["mutants"], s
        assert s["killed"] == 0

    def test_clean_baseline_survived_not_suspect(self, tmp_path, monkeypatch):
        proj, target = self._write_project(tmp_path)
        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.runner._run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None: (0, "pass"),
        )
        result = run_mutation(str(target), str(proj), budget=2)
        assert result["summary"].get("suspect", 0) == 0
