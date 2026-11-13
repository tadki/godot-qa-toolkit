"""SEE-1571 §SPEC-501：E2 零测试引用短路转正（默认生效 + 双披露 + 双路同判据）。

钉死行为（蓝本 = SEE-1568 批次二修正后原型语义）：
- 默认生效（无参数即启用）；门判据 = 派生 glob 零命中（串行 + 并行双路径
  一致；禁止 CLI 参数推断——v1 串行门误判教训）；
- verdict 语义 = 有意不执行 + 双披露（stderr 披露行 + summary 披露字段）；
- 非零引用目标行为逐位不变（scoped 照跑）；显式 --tests 与 dry-run 不短路；
- 与报告完备性兼容：零引用目标完整入报告（settled 记录，零静默缺失）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit import cli as cli_mod
from godot_qa_toolkit.cli import build_parser
from godot_qa_toolkit.mutation import executor
import godot_qa_toolkit.mutation.runner as runner_mod
from godot_qa_toolkit.mutation.testscan import (
    disclose_zero_ref,
    zero_ref_short_circuit_result,
)

GD_TARGET = "func add(a, b):\n\treturn a + b\n"
GD_WIDE_TARGET = (
    "func f(a, b):\n"
    "\tif a > 0:\n"
    "\t\treturn a + b\n"
    "\treturn a - b and a * b\n"
)


class _SerialPool:
    def __init__(self, max_workers=None):
        self.max_workers = max_workers

    def map(self, fn, iterable):
        return [fn(x) for x in iterable]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _write_zero_ref_project(tmp_path: Path, source: str = GD_TARGET) -> tuple[str, str]:
    """无任何测试引用目标的项目（tests/ 树存在但不引用 target）。"""
    proj = tmp_path / "proj"
    (proj / "tests" / "unit").mkdir(parents=True)
    (proj / "tests" / "unit" / "test_other.gd").write_text(
        'preload("res://unrelated.gd")\n')
    target = proj / "calc.gd"
    target.write_text(source)
    return str(proj), str(target)


def _write_scoped_project(tmp_path: Path) -> tuple[str, str]:
    proj = tmp_path / "proj"
    (proj / "tests" / "unit" / "save").mkdir(parents=True)
    (proj / "tests" / "unit" / "save" / "test_save.gd").write_text(
        'preload("res://calc.gd")\n')
    target = proj / "calc.gd"
    target.write_text(GD_TARGET)
    return str(proj), str(target)


@pytest.fixture()
def sealed(monkeypatch):
    """零 GUT 门：任何 GUT 调用即失败（短路轮必须零 GUT）。"""

    def _no_gut(root, timeout_s=60, tests_glob=None):
        raise AssertionError("zero-ref short-circuit must not run any GUT")

    monkeypatch.setattr(executor, "prewarm_import", lambda root: None)
    monkeypatch.setattr(runner_mod, "_run_gut_on_project", _no_gut)


class TestRecordShape:
    def test_record_shape_pins_batch2_archive(self):
        """披露记录形态逐键钉死（批次二留档 stdout 形态）。"""
        r = zero_ref_short_circuit_result("/x/calc.gd")
        assert r["tool"] == "mutation" and r["ok"] is True
        assert r["failures"] == []
        s = r["summary"]
        assert s == {
            "file": "/x/calc.gd", "mutants": 0, "jobs": 0,
            "killed": 0, "survived": 0, "timeout": 0,
            "run_errors": 0, "suspect": 0, "invalid_mutants": 0,
            "kill_rate": 0.0,
            "note": ("zero-ref short-circuit: no test references the target "
                     "— full-GUT fallback skipped (disclosed short-circuit; "
                     "potential kills intentionally not executed)"),
            "scoped": False,
            "zero_ref_short_circuit": True,
        }

    def test_stderr_disclosure_line_pins_batch2_archive(self, capsys):
        disclose_zero_ref("/x/calc.gd")
        err = capsys.readouterr().err
        assert err == ("zero-ref short-circuit: /x/calc.gd — no test "
                       "references the target, full-GUT fallback skipped "
                       "(disclosed)\n")


class TestSerialFace:
    def test_see1413_block_short_circuits_before_prewarm(
            self, tmp_path, monkeypatch, sealed,
            capsys: pytest.CaptureFixture[str]):
        """默认 jobs 单文件零引用：prewarm 之前短路，零 GUT，双披露在位。"""
        proj, target = _write_zero_ref_project(tmp_path)
        monkeypatch.setattr(executor.os, "cpu_count", lambda: 4)
        warm = {"n": 0}
        monkeypatch.setattr(
            executor, "prewarm_import",
            lambda root: warm.__setitem__("n", warm["n"] + 1))
        d = executor.run_mutation_files([target], proj, budget=50,
                                        timeout_s=60, dry_run=False,
                                        jobs=None)
        out = capsys.readouterr()
        assert warm["n"] == 0, "零引用短路不得触发 prewarm"
        assert d["ok"] is True and d["jobs"] == 1
        s = d["results"][0]["summary"]
        assert s["zero_ref_short_circuit"] is True and s["scoped"] is False
        assert s["mutants"] == 0 and s["jobs"] == 0
        assert "zero-ref short-circuit" in out.err
        assert d["stage_timings"]["prewarm"] == 0.0

    def test_parallel_bound_zero_ref_short_circuits(
            self, tmp_path, monkeypatch, sealed):
        """total > jobs（并发边界之上）同样短路——双路径同判据。"""
        proj, target = _write_zero_ref_project(tmp_path, GD_WIDE_TARGET)
        monkeypatch.setattr(executor.os, "cpu_count", lambda: 4)
        d = executor.run_mutation_files([target], proj, budget=50,
                                        timeout_s=60, dry_run=False,
                                        jobs=None)
        assert d["ok"] is True
        assert d["results"][0]["summary"]["zero_ref_short_circuit"] is True

    def test_runner_direct_face_keeps_master_behavior(
            self, tmp_path, monkeypatch, capsys: pytest.CaptureFixture[str]):
        """转正边界钉子：直达 run_mutation 的面（CLI --jobs 1 串行快路径）
        保持 master 行为——批次二原型的门只落在 run_mutation_files 编排层
        （SEE-1413 串行 dispatch + _plan_file_chunks 并行规划）双路径；
        run_mutation 本体不加门（30+ 直调它的 verdict/中断/beacon 语义测试
        面依赖既有契约）。显式 --jobs 1 属用户指定执行面（同显式 --tests
        旁路精神）。"""
        proj, target = _write_zero_ref_project(tmp_path)
        monkeypatch.setattr(
            runner_mod, "_run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None: (0, "pass [passed]"))
        args = build_parser().parse_args(
            ["mutation", target, "--project-root", proj, "--jobs", "1"])
        rc = cli_mod.cmd_mutation(args)
        captured = capsys.readouterr()
        out = cli_mod.json.loads(captured.out)
        assert rc == 1  # fake GUT 全 pass → survived → ok False
        assert "zero_ref_short_circuit" not in out["summary"]
        assert out["summary"]["mutants"] > 0

    def test_zero_mutant_zero_ref_keeps_no_sites(
            self, tmp_path, monkeypatch, sealed):
        """零位点文件不短路（no_sites 契约逐位不变——无可执行面即无执行）。"""
        proj = tmp_path / "proj"
        (proj / "tests").mkdir(parents=True)
        target = proj / "empty.gd"
        target.write_text("# no funcs\n")
        d = executor.run_mutation_files([str(target)], str(proj), budget=50,
                                        dry_run=False, jobs=None)
        s = d["results"][0]["summary"]
        assert "zero_ref_short_circuit" not in s
        assert s["mutants"] == 0


class TestNonZeroRefUntouched:
    def test_scoped_target_runs_normally(
            self, tmp_path, monkeypatch,
            capsys: pytest.CaptureFixture[str]):
        """非零引用目标行为逐位不变：scoped 照跑、无短路披露、无短路键。"""
        proj, target = _write_scoped_project(tmp_path)
        monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
        monkeypatch.setattr(executor, "prewarm_import", lambda root: None)
        monkeypatch.setattr(
            executor, "_run_baseline_snapshot",
            lambda root, g, t: {"rc": 0, "tail": "", "elapsed": 5.0})
        monkeypatch.setattr(
            runner_mod, "_run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None: (0, "pass [passed]"))
        d = executor.run_mutation_files([target], proj, budget=50,
                                        dry_run=False, jobs=None)
        err = capsys.readouterr().err
        assert "zero-ref short-circuit" not in err
        s = d["results"][0]["summary"]
        assert "zero_ref_short_circuit" not in s
        assert s["scoped"] is True

    def test_explicit_tests_glob_bypasses_gate(
            self, tmp_path, monkeypatch):
        """显式 --tests：零引用目标也不短路（用户指定执行面优先，照常执行）。"""
        proj, target = _write_zero_ref_project(tmp_path)
        monkeypatch.setattr(executor, "prewarm_import", lambda root: None)
        monkeypatch.setattr(
            runner_mod, "_run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None: (0, "pass [passed]"))
        d = executor.run_mutation_files(
            [target], proj, budget=50, dry_run=False,
            tests_glob="res://tests/unit/", jobs=1)
        s = d["results"][0]["summary"]
        assert "zero_ref_short_circuit" not in s
        assert s["scoped"] is True
        assert s["mutants"] > 0

    def test_dry_run_lists_mutants_for_zero_ref(
            self, tmp_path, sealed):
        """dry-run 不短路：规划面保持完整位点清单（E6-4 等价对照前提）。"""
        proj, target = _write_zero_ref_project(tmp_path, GD_WIDE_TARGET)
        d = executor.run_mutation_files([target], proj, budget=50,
                                        dry_run=True, jobs=None)
        s = d["results"][0]["summary"]
        assert "zero_ref_short_circuit" not in s
        assert s["mutants"] == len(
            executor._collect_mutations(GD_WIDE_TARGET, target))


class TestReportCompleteness:
    def test_multifile_zero_ref_member_settled_not_silent(
            self, tmp_path, monkeypatch, capsys: pytest.CaptureFixture[str]):
        """多文件混跑：零引用成员以 settled 记录入报告（零静默缺失）。"""
        proj = tmp_path / "proj"
        (proj / "tests" / "unit" / "save").mkdir(parents=True)
        (proj / "tests" / "unit" / "save" / "test_save.gd").write_text(
            'preload("res://calc.gd")\n')
        scoped = proj / "calc.gd"
        scoped.write_text(GD_TARGET)
        zero_ref = proj / "lonely.gd"
        zero_ref.write_text(GD_TARGET)
        monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
        monkeypatch.setattr(executor, "prewarm_import", lambda root: None)
        monkeypatch.setattr(
            executor, "_run_baseline_snapshot",
            lambda root, g, t: {"rc": 0, "tail": "", "elapsed": 5.0})
        monkeypatch.setattr(
            runner_mod, "_run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None: (0, "pass [passed]"))
        d = executor.run_mutation_files([str(scoped), str(zero_ref)], str(proj),
                                        budget=50, dry_run=False, jobs=None)
        by_file = {r["summary"]["file"]: r for r in d["results"]}
        assert len(by_file) == 2, "任何输入文件都不得从报告消失"
        lonely = by_file[str(zero_ref)]["summary"]
        assert lonely["zero_ref_short_circuit"] is True
        assert "zero-ref short-circuit" in capsys.readouterr().err
        calc = by_file[str(scoped)]["summary"]
        assert "zero_ref_short_circuit" not in calc


class TestSerialGateDryRunContract:
    """门自身的 dry-run 参数契约（A5 变异硬化发现的口径缺口）。

    生产路径上 `_run_mutation_files_impl` 先以 `_dry_run_list` 拦截 dry-run，
    门内 `not dry_run` 在当前调用图上恒真；但「dry-run 不短路」是门签名
    声明的语义，本钉子直接钉住门内违约（避免未来把 dry-run 路由进门的
    重构静默破坏零 GUT 契约）。
    """

    def test_dry_run_param_does_not_short_circuit(self, tmp_path):
        proj, target = _write_zero_ref_project(tmp_path, GD_WIDE_TARGET)
        ret, tests_glob, _selection_meta = executor._single_file_serial_gate(
            [target], proj, budget=50, timeout_s=60, tests_glob=None,
            jobs=1, dry_run=True, resume=False, binary_kind=None,
            timing_store=None, mutation_warning=None, timeout_factor=None,
            mutate_all=False, worker_channel=None)
        assert ret is None, "dry-run 面不得产出零引用短路终态"
        assert tests_glob is None
