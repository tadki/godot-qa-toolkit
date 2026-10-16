"""§SPEC-1420-04（报告 id 列）+ §SPEC-1420-10（一致性回归：差分 vs
--mutate-all 全量跑 verdict 完全一致 / untouched 全披露）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import runner as mrunner

TARGET_SRC = ("func add(a, b):\n"
              "\treturn a + b\n"
              "\n"
              "func sub(a, b):\n"
              "\treturn a - b\n")


def _write_project(tmp_path, src=TARGET_SRC):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    target = proj / "calc.gd"
    target.write_text(src)
    return proj, target


KILL_TAIL = "* test_x\n\t\ttest_x [Failed]"


class TestReportIdColumn:
    def test_failures_carry_stable_id(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)

        def all_pass(root, timeout_s=60, tests_glob=None):
            return (0, "all passed")

        monkeypatch.setattr(mrunner, "_run_gut_on_project", all_pass)
        r = mrunner.run_mutation(str(target), str(proj), budget=10,
                                 tests_glob="res://tests/")
        assert r["failures"]
        for f in r["failures"]:
            # id = <file>#<scope_id>#<n>（D3）
            parts = f["id"].split("#")
            assert parts[-1].isdigit()
            assert parts[-2] in ("func:add", "func:sub")
            assert f["id"].endswith("calc.gd#") is False
            assert f["id"].split("#")[0].endswith("calc.gd")

    def test_id_stable_across_runs_same_scope_content(self, tmp_path,
                                                      monkeypatch):
        proj, target = _write_project(tmp_path)

        def all_pass(root, timeout_s=60, tests_glob=None):
            return (0, "all passed")

        monkeypatch.setattr(mrunner, "_run_gut_on_project", all_pass)
        r1 = mrunner.run_mutation(str(target), str(proj), budget=10,
                                  tests_glob="res://tests/")
        r2 = mrunner.run_mutation(str(target), str(proj), budget=10,
                                  tests_glob="res://tests/")
        key = lambda fs: sorted((f["kind"], f["original"], f["mutated"],
                                 f["id"]) for f in fs)  # noqa: E731
        assert key(r1["failures"]) == key(r2["failures"])


class TestDiffVsMutateAllConsistency:
    def test_no_truncation_verdicts_identical(self, tmp_path, monkeypatch):
        """定案 §四.3：无截断 fixture——差分跑 vs --mutate-all 全量跑，
        逐位点 verdict 完全一致（100%）。

        verdict 按 mutant 谓词确定性判定（add scope 杀、sub scope 活）——
        call 序 fake 对差分重跑子集天然不同位，不能充当逐位点 oracle。"""
        proj, target = _write_project(tmp_path)

        def mk_keyed():
            def keyed(m, *a, **k):
                verdict = "killed" if m.scope_id == "func:add" else "survived"
                return {"file": m.file, "line": m.line, "column": m.column,
                        "kind": m.kind, "operator": m.operator,
                        "original": m.original, "mutated": m.mutated,
                        "verdict": verdict, "reason": "keyed",
                        "id": m.stable_id}
            return keyed

        calls = {"n": 0}

        def gut(root, timeout_s=60, tests_glob=None):
            calls["n"] += 1
            return (0, "all passed")

        monkeypatch.setattr(mrunner, "_run_gut_on_project", gut)
        monkeypatch.setattr(mrunner, "_run_single_mutant", mk_keyed())
        diff_run = mrunner.run_mutation(str(target), str(proj), budget=10,
                                        tests_glob="res://tests/")
        assert diff_run["summary"]["killed"] == 3
        assert diff_run["summary"]["survived"] == 3
        # 二轮（零改动）：add 的 killed 复用 + sub 的 survived 重跑
        monkeypatch.setattr(mrunner, "_run_single_mutant", mk_keyed())
        r2 = mrunner.run_mutation(str(target), str(proj), budget=10,
                                  tests_glob="res://tests/")
        # 三轮 --mutate-all：全位点重跑
        monkeypatch.setattr(mrunner, "_run_single_mutant", mk_keyed())
        all_run = mrunner.run_mutation(str(target), str(proj), budget=10,
                                       tests_glob="res://tests/",
                                       mutate_all=True)
        # verdict 面：非 killed 明细逐一对照 + killed 计数对照
        fkey = lambda fs: sorted(  # noqa: E731
            (f["kind"], f["original"], f["mutated"], f["verdict"])
            for f in fs)
        assert fkey(r2["failures"]) == fkey(all_run["failures"])
        assert r2["summary"]["killed"] == all_run["summary"]["killed"]
        assert r2["ok"] == all_run["ok"] == diff_run["ok"]
        # 一致性口径：--mutate-all 全量位点的 verdict 集与差分位点的 verdict
        # 集逐位点相同（同一 fake 对同一 mutant 给同一 verdict——killed 复用
        # 的位点其 verdict 即首跑 verdict）
        assert diff_run["summary"]["killed"] == all_run["summary"]["killed"]

    def test_truncation_covered_consistent_and_untouched_disclosed(
            self, tmp_path, monkeypatch):
        """截断 fixture：covered verdict 一致 + untouched 全披露（行号区间）。"""
        src = ("func one(a):\n"
               "\treturn a + 1\n"
               "\n"
               "func two(a):\n"
               "\treturn a + 2\n")
        proj, target = _write_project(tmp_path, src)
        # 位点数手算：one = AOR 3（+→-,*,/）+ boundary 2（1→0,-1）= 5；
        # two = AOR 3（"2" 无 boundary 变异）→ 共 8。

        def mk_kill_all():
            state = {"n": 0}

            def kill_all(root, timeout_s=60, tests_glob=None):
                state["n"] += 1
                if state["n"] == 1:
                    return (0, "baseline ok")
                return (1, KILL_TAIL)

            return kill_all

        monkeypatch.setattr(mrunner, "_run_gut_on_project", mk_kill_all())
        full = mrunner.run_mutation(str(target), str(proj), budget=10,
                                    tests_glob="res://tests/")
        assert full["summary"]["killed"] == 8
        # 差分 + 预算截断：改 two 的函数体 → retry 3，budget=2 → untouched 1
        target.write_text(src.replace("return a + 2", "return a + 20"))
        monkeypatch.setattr(mrunner, "_run_gut_on_project", mk_kill_all())
        r2 = mrunner.run_mutation(str(target), str(proj), budget=2,
                                  tests_glob="res://tests/")
        s = r2["summary"]
        assert s["executed"] == 2 and s["retry_total"] == 3
        assert s["untouched_ranges"] and len(s["untouched_ranges"]) == 1
        # covered verdict 一致：two 已执行位点全 killed（同 fake 确定性）
        # 口径：复用 5（one 全 killed）+ executed 2（two 前两位点）= killed 7
        assert s["killed"] == 7
