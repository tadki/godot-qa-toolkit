"""§SPEC-1420-07：差分默认开启 + --mutate-all 退路 + budget-on-retry（D6）。

差分默认开启（manifest 在场即差分）；summary.diff_skipped 逐条 id 披露；
budget 按差分后 retry 数计，强制披露 retry_total/executed/untouched（附行
号区间）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import runner as mrunner
from godot_qa_toolkit.mutation.diffengine import budget_on_retry, \
    untouched_ranges
from godot_qa_toolkit.mutation.ops import Mutant

TARGET_SRC = ("func add(a, b):\n"
              "\treturn a + b\n")


def _write_project(tmp_path, src=TARGET_SRC):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    target = proj / "calc.gd"
    target.write_text(src)
    return proj, target


class TestBudgetOnRetryPure:
    def test_budget_caps_retry_set(self):
        executed, untouched = budget_on_retry([1, 2, 3, 4, 5], 3)
        assert executed == [1, 2, 3]
        assert untouched == [4, 5]

    def test_order_preserved_no_rotation(self):
        """拒轮转（D6）：截断保序——seq 寻址与差分对照的固定基准。"""
        executed, _ = budget_on_retry(["a", "b", "c"], 2)
        assert executed == ["a", "b"]

    def test_zero_or_negative_budget_executes_nothing(self):
        executed, untouched = budget_on_retry([1, 2], 0)
        assert executed == [] and untouched == [1, 2]

    def test_untouched_ranges_merge_contiguous_lines(self):
        mutants = [Mutant(file="x.gd", line=l, column=1, operator="+",
                          original="+", mutated="-", kind="AOR")
                   for l in (2, 3, 4, 9, 11)]
        assert untouched_ranges(mutants) == [[2, 4], [9, 9], [11, 11]]


class TestDiffDefaultOn:
    def test_second_run_uses_diff_by_default(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)
        calls1 = {"n": 0}

        def kill_all(root, timeout_s=60, tests_glob=None):
            calls1["n"] += 1
            return (1, "* test_x\n\t\ttest_x [Failed]")

        monkeypatch.setattr(mrunner, "_run_gut_on_project", kill_all)
        mrunner.run_mutation(str(target), str(proj), budget=10,
                             tests_glob="res://tests/")
        calls2 = {"n": 0}

        def counting_kill_all(root, timeout_s=60, tests_glob=None):
            calls2["n"] += 1
            return (1, "* test_x\n\t\ttest_x [Failed]")

        monkeypatch.setattr(mrunner, "_run_gut_on_project", counting_kill_all)
        r2 = mrunner.run_mutation(str(target), str(proj), budget=10,
                                  tests_glob="res://tests/")
        # 差分默认开启（无 --mutate-all / --resume）：killed 全复用
        assert r2["summary"]["resumed"] == 3
        assert r2["summary"]["executed"] == 0
        assert calls2["n"] == 1  # 仅 baseline

    def test_mutate_all_bypasses_diff(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)

        def kill_all(root, timeout_s=60, tests_glob=None):
            return (1, "* test_x\n\t\ttest_x [Failed]")

        monkeypatch.setattr(mrunner, "_run_gut_on_project", kill_all)
        mrunner.run_mutation(str(target), str(proj), budget=10,
                             tests_glob="res://tests/")
        calls2 = {"n": 0}

        def counting_kill_all(root, timeout_s=60, tests_glob=None):
            calls2["n"] += 1
            return (1, "* test_x\n\t\ttest_x [Failed]")

        monkeypatch.setattr(mrunner, "_run_gut_on_project", counting_kill_all)
        r2 = mrunner.run_mutation(str(target), str(proj), budget=10,
                                  tests_glob="res://tests/", mutate_all=True)
        # --mutate-all：全部位点重跑（executed == retry_total == 3）
        assert r2["summary"]["executed"] == 3
        assert "resumed" not in r2["summary"]
        assert calls2["n"] == 4  # baseline + 3

    def test_diff_skipped_lists_ids(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)

        def kill_all(root, timeout_s=60, tests_glob=None):
            return (1, "* test_x\n\t\ttest_x [Failed]")

        monkeypatch.setattr(mrunner, "_run_gut_on_project", kill_all)
        mrunner.run_mutation(str(target), str(proj), budget=10,
                             tests_glob="res://tests/")
        r2 = mrunner.run_mutation(str(target), str(proj), budget=10,
                                  tests_glob="res://tests/")
        skipped = r2["summary"]["diff_skipped"]
        assert len(skipped) == 3
        assert all(s.startswith("res") or "#" in s for s in skipped)
        assert all("calc.gd#add#" in s for s in skipped)


class TestBudgetOnRetryEndToEnd:
    def test_budget_applies_to_retry_set_with_disclosure(
            self, tmp_path, monkeypatch):
        """D6 端到端：首跑全 killed 落盘 → 源码改一个 func → 差分 retry 集
        = 该 scope + budget 截断 → retry_total/executed/untouched_ranges
        强制披露。"""
        src = ("func one(a):\n"
               "\treturn a + 1\n"
               "\n"
               "func two(a):\n"
               "\treturn a + 2\n")
        proj, target = _write_project(tmp_path, src)

        def mk_kill_all():
            state = {"n": 0}

            def kill_all(root, timeout_s=60, tests_glob=None):
                state["n"] += 1
                if state["n"] == 1:
                    return (0, "baseline ok")
                return (1, "* test_x\n\t\ttest_x [Failed]")

            return kill_all

        monkeypatch.setattr(mrunner, "_run_gut_on_project", mk_kill_all())
        full = mrunner.run_mutation(str(target), str(proj), budget=10,
                                    tests_glob="res://tests/")
        # 位点数：one = AOR 3 + boundary 2 = 5；two = AOR 3 → 共 8
        assert full["summary"]["killed"] == 8
        # 只改 func two 的函数体 → 该 scope 全跑；one 的 killed 复用
        target.write_text(src.replace("return a + 2", "return a + 22"))
        monkeypatch.setattr(mrunner, "_run_gut_on_project", mk_kill_all())
        r2 = mrunner.run_mutation(str(target), str(proj), budget=1,
                                  tests_glob="res://tests/")
        s = r2["summary"]
        assert s["resumed"] == 5  # one 的 killed 复用
        assert s["retry_total"] == 3  # two 的位点全跑（scope 改写）
        assert s["executed"] == 1  # budget=1 截断 retry 集
        assert len(s["untouched_ranges"]) == 1  # 剩余 2 位点落单行区间
        assert s["killed"] == 5 + 1  # 复用 5 + 新执行 1
