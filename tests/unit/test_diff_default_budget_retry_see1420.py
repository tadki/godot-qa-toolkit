"""§SPEC-1420-07：差分默认开启 + --mutate-all 退路 + budget-on-retry（D6）。

差分默认开启（manifest 在场即差分）；summary.diff_skipped 逐条 id 披露；
budget 按差分后 retry 数计，强制披露 retry_total/executed/untouched（附行
号区间）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest
from godot_qa_toolkit.mutation import runner as mrunner
from godot_qa_toolkit.mutation.diffengine import budget_on_retry, \
    untouched_ranges
from godot_qa_toolkit.mutation.ops import Mutant

def _pin_cold(monkeypatch):
    from godot_qa_toolkit.mutation.identity import COLD_SCHEME
    from godot_qa_toolkit.mutation import worker_channel as wc
    monkeypatch.setattr(wc, "resolve_exec_scheme",
                        lambda *a, **k: (COLD_SCHEME, None))
    monkeypatch.setattr(mrunner, "resolve_exec_scheme",
                        lambda *a, **k: (COLD_SCHEME, None))


def _add_gut(proj):
    (proj / "addons" / "gut" / "gut.gd").write_text("extends Node\n")


def _materialize_selection(proj, names):
    sel_dir = proj / ".gqt-attr-select" / "testsel"
    sel_dir.mkdir(parents=True, exist_ok=True)
    for n in names:
        (sel_dir / n).write_text(f'extends "res://tests/{n}"\n', encoding="utf-8")
    return "res://.gqt-attr-select/testsel/"


@pytest.fixture(autouse=True)
def _pin_cold_auto(monkeypatch):
    _pin_cold(monkeypatch)


def _pin_cold(monkeypatch):
    from godot_qa_toolkit.mutation.identity import COLD_SCHEME
    from godot_qa_toolkit.mutation import worker_channel as wc
    monkeypatch.setattr(wc, "resolve_exec_scheme",
                        lambda *a, **k: (COLD_SCHEME, None))
    monkeypatch.setattr(mrunner, "resolve_exec_scheme",
                        lambda *a, **k: (COLD_SCHEME, None))


def _add_gut(proj):
    (proj / "addons" / "gut" / "gut.gd").write_text("extends Node\n")


TARGET_SRC = ("func add(a, b):\n"
              "\treturn a + b\n")


def _write_project(tmp_path, src=TARGET_SRC):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "addons" / "gut").mkdir(parents=True)
    # §SPEC-031（R4）：插件标记（真实 gut addon 形态）——范围默认排除②
    # 依据 plugin.cfg 识别插件（无标记的 stub 会被当生产内容进宇宙）。
    (proj / "addons" / "gut" / "plugin.cfg").write_text("[plugin]\n")
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    # SEE-1550：killer 测试脚本必须在场（killed_by 证书按真实 tests 树
    # 解析归属 + sha1 校验）——killer 名与 fake GUT 输出的 test_x 对齐。
    tests = proj / "tests"
    tests.mkdir()
    (tests / "test_x.gd").write_text("extends GutTest\n\nfunc test_x():\n\tpass\n")
    _add_gut(proj)
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
                             tests_glob=_materialize_selection(proj, ["test_a.gd"]))
        calls2 = {"n": 0}

        def counting_kill_all(root, timeout_s=60, tests_glob=None):
            calls2["n"] += 1
            return (1, "* test_x\n\t\ttest_x [Failed]")

        monkeypatch.setattr(mrunner, "_run_gut_on_project", counting_kill_all)
        r2 = mrunner.run_mutation(str(target), str(proj), budget=10,
                                  tests_glob=_materialize_selection(proj, ["test_a.gd"]))
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
                             tests_glob=_materialize_selection(proj, ["test_a.gd"]))
        calls2 = {"n": 0}

        def counting_kill_all(root, timeout_s=60, tests_glob=None):
            calls2["n"] += 1
            return (1, "* test_x\n\t\ttest_x [Failed]")

        monkeypatch.setattr(mrunner, "_run_gut_on_project", counting_kill_all)
        r2 = mrunner.run_mutation(str(target), str(proj), budget=10,
                                  tests_glob=_materialize_selection(proj, ["test_a.gd"]), mutate_all=True)
        # --mutate-all：全部位点重跑（executed == retry_total == 3）
        assert r2["summary"]["executed"] == 3
        assert "resumed" not in r2["summary"]
        assert calls2["n"] == 4  # baseline + 3

    def test_diff_skipped_lists_ids(self, tmp_path, monkeypatch):
        """A09 后：证书唯一来源 = 新鲜运行时映射——种子 killer 身份映射后，
        二轮差分 skipped 全命中（id 列表）。"""
        proj, target = _write_project(tmp_path)

        def kill_all(root, timeout_s=60, tests_glob=None):
            return (1, "* test_x\n\t\ttest_x [Failed]")

        monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
        # SEE-1631 §SPEC-020 映射：本测试主体为 legacy 冷启动机制
        # （共享快照/预算重试/prefilter 复用）——显式回退 cold 源；
        # map 源行为由 test_c2_verdict_source_see1631.py 专属覆盖。
        monkeypatch.setenv("GQT_MUTATION_BASELINE_SOURCE", "cold")
        import attribution_fixtures as _fx
        from godot_qa_toolkit.mutation import truth_select as _ts
        _fx.install_recipe(proj)
        _fx.line_facts_map(proj, {"test_x.gd::test_x": {"calc.gd": [2, 3]}},
                           instrumentable={"calc.gd": [2, 3]})
        _ts._fresh_recipe_hash.cache_clear()
        _ts._test_scripts_by_name.cache_clear()

        monkeypatch.setattr(mrunner, "_run_gut_on_project", kill_all)
        mrunner.run_mutation(str(target), str(proj), budget=10,
                             tests_glob=_materialize_selection(proj, ["test_a.gd"]),
                             select_mode="line", reuse_coverage=False)
        r2 = mrunner.run_mutation(str(target), str(proj), budget=10,
                                  tests_glob=_materialize_selection(proj, ["test_a.gd"]),
                                  select_mode="line", reuse_coverage=False)
        skipped = r2["summary"]["diff_skipped"]
        assert len(skipped) == 3
        assert all(s.startswith("res") or "#" in s for s in skipped)
        assert all("calc.gd#func:add#" in s for s in skipped)



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

        # A09：killed 复用唯一依据 = 新鲜运行时映射证书——首跑前与源码
        # 变更后各刷新一次映射（模拟生产 map 刷新）；coverage 过滤关断。
        import attribution_fixtures as _fx
        from godot_qa_toolkit.mutation import truth_select as _ts

        def _seed_map():
            _fx.install_recipe(proj)
            _fx.line_facts_map(proj, {"test_x.gd::test_x": {"calc.gd": [2, 5]}},
                               instrumentable={"calc.gd": [2, 5]})
            _ts._fresh_recipe_hash.cache_clear()
            _ts._test_scripts_by_name.cache_clear()

        monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
        # SEE-1631 §SPEC-020 映射：本测试主体为 legacy 冷启动机制
        # （共享快照/预算重试/prefilter 复用）——显式回退 cold 源；
        # map 源行为由 test_c2_verdict_source_see1631.py 专属覆盖。
        monkeypatch.setenv("GQT_MUTATION_BASELINE_SOURCE", "cold")
        monkeypatch.setattr(mrunner, "_run_gut_on_project", mk_kill_all())
        _seed_map()
        full = mrunner.run_mutation(str(target), str(proj), budget=10,
                                    tests_glob=_materialize_selection(proj, ["test_a.gd"]),
                                    reuse_coverage=False)
        # 位点数：one = AOR 3 + boundary 2 = 5；two = AOR 3 → 共 8
        assert full["summary"]["killed"] == 8
        # 只改 func two 的函数体 → 该 scope 全跑；one 的 killed 复用
        target.write_text(src.replace("return a + 2", "return a + 22"))
        _seed_map()
        monkeypatch.setattr(mrunner, "_run_gut_on_project", mk_kill_all())
        r2 = mrunner.run_mutation(str(target), str(proj), budget=1,
                                  tests_glob=_materialize_selection(proj, ["test_a.gd"]),
                                  reuse_coverage=False)
        s = r2["summary"]
        assert s["resumed"] == 5  # one 的 killed 复用
        assert s["retry_total"] == 3  # two 的位点全跑（scope 改写）
        assert s["executed"] == 1  # budget=1 截断 retry 集
        assert len(s["untouched_ranges"]) == 1  # 剩余 2 位点落单行区间
        assert s["killed"] == 5 + 1  # 复用 5 + 新执行 1
