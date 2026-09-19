"""SEE-1321 编码轮 2：同文件 per-mutant 并发（SPEC-003 修订路径）单测。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import executor
from godot_qa_toolkit.mutation.runner import run_mutation
import godot_qa_toolkit.mutation.runner as runner_mod

GD_TARGET = "func add(a, b):\n\treturn a + b\n"


def _write_project(tmp_path, files=("calc.gd", "other.gd")):
    proj = tmp_path / "proj"
    (proj / "addons" / "gut").mkdir(parents=True, exist_ok=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    save = proj / "tests" / "unit" / "save"
    save.mkdir(parents=True, exist_ok=True)
    (save / "test_save.gd").write_text('preload("res://calc.gd")\n')
    paths = []
    for name in files:
        p = proj / name
        p.write_text(GD_TARGET)
        paths.append(str(p))
    return str(proj), paths


class _SerialPool:
    def __init__(self, max_workers=None):
        self.max_workers = max_workers

    def map(self, fn, iterable):
        return [fn(x) for x in iterable]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def passing_gut(monkeypatch):
    monkeypatch.setattr(
        runner_mod, "_run_gut_on_project",
        lambda root, timeout_s=60, tests_glob=None: (0, "pass"))


class TestMutantSlice:
    def test_slice_runs_only_requested_mutants(self, tmp_path, monkeypatch, passing_gut):
        """mutant_slice 语义：worker 只跑 [start, stop) 的 mutants。"""
        proj, files = _write_project(tmp_path)
        pre = {"rc": 0, "tail": "", "elapsed": 5.0}
        r = run_mutation(files[0], proj, budget=3,
                         tests_glob="res://tests/unit/save/",
                         precomputed_baseline=pre, mutant_slice=(0, 1))
        s = r["summary"]
        assert s["mutants"] == 1
        assert s["baseline_reused"] is True

    def test_slice_requires_precomputed_baseline(self, tmp_path, monkeypatch, passing_gut):
        """同文件并发 worker 无共享 baseline → 拒绝（worker 各跑 baseline 会
        N 倍放大启动税，且并发期间 baseline 耗时被竞态污染、timeout 锚失真）。"""
        proj, files = _write_project(tmp_path)
        with pytest.raises(ValueError):
            run_mutation(files[0], proj, budget=3, mutant_slice=(0, 1))

    def test_slice_union_equals_serial_verdicts(self, tmp_path, monkeypatch, passing_gut):
        """各分片判定拼起来 = 串行整跑的计数（SPEC-012 判定一致性）。"""
        proj, files = _write_project(tmp_path)
        pre = {"rc": 0, "tail": "", "elapsed": 5.0}
        serial = run_mutation(files[0], proj, budget=3,
                              tests_glob="res://tests/unit/save/",
                              precomputed_baseline=pre)
        merged = {"killed": 0, "survived": 0, "mutants": 0}
        for sl in [(0, 1), (1, 2), (2, 3)]:
            ch = run_mutation(files[0], proj, budget=3,
                              tests_glob="res://tests/unit/save/",
                              precomputed_baseline=pre, mutant_slice=sl)
            for k in merged:
                merged[k] += ch["summary"][k]
        for k in merged:
            assert merged[k] == serial["summary"][k]


class TestIntraFileJobs:
    def test_single_file_jobs_chunks(self, tmp_path, monkeypatch, passing_gut):
        """单文件 + jobs=4：mutants 切片分派（内存注入路径放开同文件并发）。"""
        proj, files = _write_project(tmp_path)
        monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
        monkeypatch.setattr(
            executor, "_run_baseline_snapshot",
            lambda root, g, t: {"rc": 0, "tail": "", "elapsed": 5.0})
        d = executor.run_mutation_files(
            [files[0]], proj, budget=3, timeout_s=60, dry_run=False,
            tests_glob="res://tests/unit/save/", jobs=4)
        # 分片按 min(jobs, mutant 总数) 切（toy 只有 3 mutants）；
        # 契约 mutant 计数合集等于 budget 上限
        assert d["jobs"] == 3
        assert sum(c["summary"]["mutants"]
                   for c in d["results"]) == 3
        assert all(c["summary"]["baseline_reused"] for c in d["results"])

    def test_jobs_one_stays_serial(self, tmp_path, monkeypatch, passing_gut):
        proj, files = _write_project(tmp_path)
        d = executor.run_mutation_files(
            [files[0]], proj, budget=2, timeout_s=60, dry_run=False,
            tests_glob="res://tests/unit/save/", jobs=1)
        assert d["jobs"] == 1
        assert len(d["results"]) == 1

    def test_single_file_auto_scan_with_jobs(self, tmp_path, monkeypatch, passing_gut):
        """未显式 --tests：父层推导 scoped glob → baseline 只跑一份。"""
        proj, files = _write_project(tmp_path)
        baselines = {"n": 0}
        monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
        monkeypatch.setattr(
            executor, "_run_baseline_snapshot",
            lambda root, g, t: {"rc": 0, "tail": "", "elapsed": 5.0})
        d = executor.run_mutation_files(
            [files[0]], proj, budget=2, timeout_s=60, dry_run=False,
            tests_glob=None, jobs=4)
        # fake 全 pass → mutant 存活 → ok=False 是正确判定；
        # 关键断言：父层 scoped glob 推导生效、baseline 零跑双份
        assert all(c["summary"]["scoped"] for c in d["results"])
        assert all(c["summary"]["baseline_reused"] for c in d["results"])
