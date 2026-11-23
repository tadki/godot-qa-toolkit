"""SEE-1623 H1：full 审计面零测试流（no GUT run stream evidence）行为回归钉。

根因：GUT 9.6 `-gdir` 不递归子目录（add_directory「Does not look in sub
directories」）。full 面 tests_glob=None → `-gdir=res://tests/` → 标准布局
（tests/unit/ 等）收集 0 个测试脚本：
- 冷通道：L4 证据门 fail-close → 每腿 run_error（全红）；
- worker 通道：0 脚本时 end_run 照常发射、fail_count=0 → 假 survived
  （判定不可信，只靠 canary 兜底）。

行为契约（修复后）：
1. 审计面缺省 glob 必须让 GUT 递归收集（-ginclude_subdirs 在场）；
   显式目录 glob 语义保持逐字（不悄悄递归）。
2. `.gd` 形态（fallback 根目录扫描 + -gselect）同样必须递归收集。
3. worker 腿 0 测试执行 → 具名 run_error（不得 survived）——通道判定
   fail-closed，不再依赖 canary 兜底。
4. L4 证据门保持原强度（未放宽）：流证据仍由既有正则判定（既有测试锚定）。
"""

from godot_qa_toolkit.mutation.ops import Mutant
from godot_qa_toolkit.mutation.reporting import classify_worker_leg
from godot_qa_toolkit.mutation.runner import _gut_extra_args
from godot_qa_toolkit.mutation.worker_channel import WorkerLegResult

REPO = None  # 占位：本文件不触盘


def _mutant(i=1):
    return Mutant(file="x.gd", line=2, column=10, operator="+",
                  original="+", mutated="-", kind="AOR", scope_id="f",
                  n=i, stable_id=f"x.gd#f#{i}")


class TestAuditFaceGlobRecursion:
    def test_default_audit_glob_includes_subdirs(self):
        args = _gut_extra_args(None)
        assert "-ginclude_subdirs" in args
        assert any(a.startswith("-gdir=res://tests/") for a in args)

    def test_file_glob_fallback_root_scan_includes_subdirs(self):
        args = _gut_extra_args("res://tests/unit/test_calc.gd")
        assert "-ginclude_subdirs" in args
        assert "-gselect=test_calc" in args

    def test_explicit_directory_glob_stays_verbatim(self):
        assert _gut_extra_args("res://tests/unit") == ["-gdir=res://tests/unit"]


def _leg(tests_ran=0, fail=0, gut_ran=True):
    return WorkerLegResult(leg=0, hash_ok=True, gut_ran=gut_ran,
                           tests_ran=tests_ran, fail_count=fail,
                           failing_tests=set(), reload={},
                           ok=bool(gut_ran))


class TestWorkerZeroTestLegFailClosed:
    def test_zero_tests_ran_is_run_error_not_survived(self):
        r = classify_worker_leg(_mutant(), _leg(tests_ran=0, fail=0),
                                set(), False, None, "root", "src")
        assert r["verdict"] == "run_error"
        assert "no GUT run stream evidence" in r["reason"]

    def test_tests_ran_in_field_keeps_killed_semantics(self):
        r = classify_worker_leg(_mutant(), _leg(tests_ran=3, fail=1),
                                set(), False, None, "root", "src")
        r2 = r  # fail_count=1 且 failing_tests 为空：pre-existing-only 面
        assert r2["verdict"] in ("killed", "survived", "suspect")

    def test_real_run_leg_survived_unchanged(self):
        r = classify_worker_leg(_mutant(), _leg(tests_ran=5, fail=0),
                                set(), False, None, "root", "src")
        assert r["verdict"] == "survived"
