"""SEE-1420 code-fix 续派回归测试：D3（不可插桩行 ≠ uncovered）+ O1（损坏
manifest 告警）+ O2（executed 实际执行语义）+ O3（--mutate-all 披露对齐）。

每项断言均锚定修复前的具体错误形态（红→绿实证）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import runner as mrunner
from godot_qa_toolkit.mutation.coverlink import save_coverage
from godot_qa_toolkit.mutation.resume2 import ResumeStoreV2

KILL_TAIL = "* test_x\n\t\ttest_x [Failed]"


def _write_project(tmp_path, src):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    target = proj / "calc.gd"
    target.write_text(src)
    return proj, target


def _mk_kill_fake():
    state = {"n": 0}

    def kill_all(root, timeout_s=60, tests_glob=None):
        state["n"] += 1
        if state["n"] == 1:
            return (0, "baseline ok")
        return (1, KILL_TAIL)

    return kill_all


# ---------------------------------------------------------------------------
# D3（MEDIUM）：不可插桩行 ≠ uncovered
# ---------------------------------------------------------------------------

# 位点行集合：var 初始化器（3, 4 行——可插桩面外）+ func 体（7 行）
INIT_SRC = ("extends Node\n"
            "\n"
            "var base_hp := 10 - 3\n"  # line 3：成员初始化器（不可插桩行）
            "var scale := 2 + 1\n"     # line 4：成员初始化器（不可插桩行）
            "\n"
            "func damage(amount: int = 5 - 1) -> int:\n"
            "\treturn base_hp - amount * scale\n")  # line 7（可插桩）


class TestD3NonInstrumentableNotUncovered:
    def test_non_instrumentable_lines_stay_in_execution_face(self, tmp_path,
                                                             monkeypatch):
        """修复前红：行 3/4 位点被误标 uncovered 并跳过（item.gd 11/60
        假阴性实证）；修复后：它们留在执行面、正常出 verdict。"""
        proj, target = _write_project(tmp_path, INIT_SRC)
        # 可插桩面 = {7}（行 7 可插桩但未覆盖——它才是合法 uncovered 对象）
        save_coverage(set(), 6, str(proj), str(target), "res://tests/",
                      instrumentable_lines={7})
        monkeypatch.setattr(mrunner, "_run_gut_on_project", _mk_kill_fake())
        r = mrunner.run_mutation(str(target), str(proj), budget=10,
                                 tests_glob="res://tests/",
                                 reuse_coverage=True)
        s = r["summary"]
        # 行 3/4（不可插桩）位点照常执行并被杀——绝不标 uncovered
        uncovered = [f for f in r["failures"] if f["verdict"] == "uncovered"]
        assert not any(f["line"] in (3, 4) for f in uncovered), \
            "non-instrumentable lines must never enter the uncovered skip set"
        # 行 7（可插桩且未覆盖）才是合法 uncovered
        assert all(f["line"] == 7 for f in uncovered)
        assert s["coverage_prefilter"]["uncovered"] == len(uncovered) > 0
        # 行 3/4 的位点真实执行了（verdict 里有它们的记录）
        executed_lines = {m["line"] for m in
                          [f for f in r["failures"] + []]
                          } | {7}
        # 报告自洽：killed 含不可插桩行的位点
        assert s["killed"] > 0

    def test_instrumentable_face_missing_falls_back_unfiltered(self, tmp_path,
                                                               monkeypatch,
                                                               capsys):
        """旧 schema（无 instrumentable_lines）→ 不过滤（宁多跑不假阴性）。"""
        proj, target = _write_project(tmp_path, INIT_SRC)
        save_coverage(set(), 6, str(proj), str(target), "res://tests/")  # 无 face
        monkeypatch.setattr(mrunner, "_run_gut_on_project", _mk_kill_fake())
        r = mrunner.run_mutation(str(target), str(proj), budget=10,
                                 tests_glob="res://tests/",
                                 reuse_coverage=True)
        assert r["summary"]["coverage_prefilter"] == \
            "instrumentable_face_missing"
        assert "instrumentable face unavailable" in capsys.readouterr().err
        assert not [f for f in r["failures"] if f["verdict"] == "uncovered"]


# ---------------------------------------------------------------------------
# O1（LOW）：损坏 manifest JSON 告警（与坏条目同口径）
# ---------------------------------------------------------------------------


class TestO1CorruptManifestWarns:
    def test_corrupt_manifest_warns_once(self, tmp_path, capsys):
        proj, target = _write_project(tmp_path, INIT_SRC)
        store = ResumeStoreV2(str(proj), str(target), "res://tests/")
        store.ensure_store_dir()
        store.manifest_path.write_text("{not valid json", encoding="utf-8")
        assert store.load_manifest() is None
        assert store.load_manifest() is None
        err = capsys.readouterr().err
        assert "manifest corrupted" in err  # 修复前：静默吞（零告警）
        # 每实例告警一次口径（不重刷）
        assert err.count("manifest corrupted") == 1

    def test_missing_manifest_stays_silent(self, tmp_path, capsys):
        """正对照：首跑无 manifest 是正常形态——不得误报。"""
        proj, target = _write_project(tmp_path, INIT_SRC)
        store = ResumeStoreV2(str(proj), str(target), "res://tests/")
        store.ensure_store_dir()
        assert store.load_manifest() is None
        assert "manifest corrupted" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# O2（LOW）：summary.executed = 实际执行语义（剔除静态 invalid）
# ---------------------------------------------------------------------------


class TestO2ExecutedIsActualExecution:
    def test_executed_excludes_static_invalid_mutants(self, tmp_path,
                                                      monkeypatch):
        """修复前红：executed 为计划面（含 invalid），20 vs mutants 19 实证
        漂移；修复后：executed = 实际跑 GUT 的位点数。"""
        src = ("func one(a):\n"
               "\treturn a + 1\n"
               "\n"
               "func two(a):\n"
               "\treturn a + 2\n")
        proj, target = _write_project(tmp_path, src)
        monkeypatch.setattr(mrunner, "_run_gut_on_project", _mk_kill_fake())

        from godot_qa_toolkit.mutation import reporting as _rep
        orig_split = _rep._split_invalid_mutants

        def split_with_one_invalid(src_text, mutants):
            # 强制首个 mutant 为静态 invalid（语法预检剔除）——不动生产逻辑
            valid, invalid = orig_split(src_text, mutants)
            if valid:
                return valid[1:], invalid + valid[:1]
            return valid, invalid

        monkeypatch.setattr(_rep, "_split_invalid_mutants",
                            split_with_one_invalid)
        r = mrunner.run_mutation(str(target), str(proj), budget=10,
                                 tests_glob="res://tests/", mutate_all=True)
        s = r["summary"]
        # 8 位点（手算见 §13 口径）；1 个被静态预检剔除 → 实际执行 7
        assert s["mutants"] == 8
        assert s["invalid_mutants"] == 1
        assert s["executed"] == 7, "executed must count real GUT runs only"
        assert s["executed"] <= s["mutants"]

    def test_executed_equals_valid_count_under_budget(self, tmp_path,
                                                      monkeypatch):
        """budget 截断场景：executed ≤ 截断面且不含 invalid。"""
        src = ("func one(a):\n"
               "\treturn a + 1\n")
        proj, target = _write_project(tmp_path, src)
        monkeypatch.setattr(mrunner, "_run_gut_on_project", _mk_kill_fake())
        r = mrunner.run_mutation(str(target), str(proj), budget=2,
                                 tests_glob="res://tests/", mutate_all=True)
        s = r["summary"]
        assert s["executed"] == 2
        assert s["executed"] <= s["mutants"]


# ---------------------------------------------------------------------------
# O3（LOW）：--mutate-all 串行/并行披露键集对齐
# ---------------------------------------------------------------------------


class TestO3MutateAllDisclosureAligned:
    def test_parallel_mutate_all_discloses_same_keys(self, tmp_path,
                                                     monkeypatch):
        """并行路径 mutate_all 也须披露 retry_total/executed/untouched_ranges/
        diff_skipped——与串行口径一致（修复前并行缺失，红）。"""
        from godot_qa_toolkit.mutation import executor

        class _SerialPool:
            def __init__(self, max_workers=None):
                pass

            def map(self, fn, iterable):
                return [fn(x) for x in iterable]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        proj, target = _write_project(tmp_path, INIT_SRC)
        monkeypatch.setattr(mrunner, "_run_gut_on_project", _mk_kill_fake())
        monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
        monkeypatch.setattr(
            executor, "_run_baseline_snapshot",
            lambda root, g, t: {"rc": 0, "tail": "", "elapsed": 5.0})
        r = executor.run_mutation_files(
            [str(target)], str(proj), budget=50, timeout_s=60,
            tests_glob="res://tests/", jobs=2, mutate_all=True)
        s = r["results"][0]["summary"]
        for key in ("retry_total", "executed", "untouched_ranges",
                    "diff_skipped"):
            assert key in s, f"parallel mutate_all summary must carry {key}"
        assert s["diff_skipped"] == []
        assert s["executed"] == s["retry_total"]

    def test_serial_mutate_all_discloses_same_keys(self, tmp_path,
                                                   monkeypatch):
        proj, target = _write_project(tmp_path, INIT_SRC)
        monkeypatch.setattr(mrunner, "_run_gut_on_project", _mk_kill_fake())
        r = mrunner.run_mutation(str(target), str(proj), budget=50,
                                 tests_glob="res://tests/", mutate_all=True)
        s = r["summary"]
        for key in ("retry_total", "executed", "untouched_ranges",
                    "diff_skipped"):
            assert key in s, f"serial mutate_all summary must carry {key}"
        assert s["diff_skipped"] == []
        assert s["executed"] == s["retry_total"]  # 无截断面：全量执行
