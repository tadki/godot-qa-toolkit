"""SEE-1420 QA 回归：并行差分收尾注入（executor._inject_one）报告装配。

实机验收（§14-C）复现的缺陷：diff_active ∧ plan.reused 非空时
`s["diff_skipped"] = sorted(reused)` 引用未定义名字 → NameError、
stdout 零 JSON（违反 exit-1-必有-JSON 契约）。串行路径
（planner disclosures）与并行路径（executor 注入）必须逐键同构。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import executor
from godot_qa_toolkit.mutation.ops import _collect_mutations
from godot_qa_toolkit.mutation.resume2 import ResumeStoreV2
from godot_qa_toolkit.mutation.scopemanifest import build_manifest
from godot_qa_toolkit.timing import project_relative
import godot_qa_toolkit.mutation.runner as runner_mod
from godot_qa_toolkit.mutation.executor import run_mutation_files

GD_TARGET = ("func add(a, b):\n"
             "\tif a > b:\n"
             "\t\treturn a - b\n"
             "\treturn a + b\n")


def _write_project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "addons" / "gut").mkdir(parents=True, exist_ok=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    save = proj / "tests" / "unit" / "save"
    save.mkdir(parents=True, exist_ok=True)
    (save / "test_save.gd").write_text('preload("res://calc.gd")\n')
    target = proj / "calc.gd"
    target.write_text(GD_TARGET)
    return str(proj), str(target)


class _SerialPool:
    def __init__(self, max_workers=None):
        self.max_workers = max_workers

    def map(self, fn, iterable):
        return [fn(x) for x in iterable]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _mutants_of(target):
    src = Path(target).read_text(encoding="utf-8")
    return build_manifest(src, target, _collect_mutations(src, target))


def _seed_killed_prior(proj, target):
    """预置 v1→v2 store：add#1 killed（全签名/scope_hash 与当前源精确一致）。"""
    header_hash, _spans, mutants, scope_hashes = _mutants_of(target)
    m0 = mutants[0]
    store = ResumeStoreV2(proj, target, "res://tests/unit/save/")
    assert store.write_manifest(header_hash, scope_hashes,
                                store._sha1_text(
                                    Path(target).read_text(encoding="utf-8")),
                                project_relative(proj, target))
    assert store.record_entry({
        "id": m0.stable_id,
        "verdict": "killed",
        "reason": f"seed {m0.kind} killed",
        "line": m0.line, "column": m0.column, "kind": m0.kind,
        "original": m0.original, "mutated": m0.mutated,
        "scope_hash": scope_hashes[m0.scope_id],
        "timeout_streak": 0,
    })
    return m0.stable_id


@pytest.fixture()
def killing_gut(monkeypatch):
    """到达此处调用必然是 mutant 轮（baseline 由 executor 层快照拦截）——
    返回新失败集 → executed 位点全 killed。"""
    monkeypatch.setattr(
        runner_mod, "_run_gut_on_project",
        lambda root, timeout_s=60, tests_glob=None:
            (1, "* test_killed_by_probe\n\t[Failed]\n"))


def test_parallel_diff_with_reuse_builds_report(tmp_path, monkeypatch,
                                                killing_gut):
    """复用+执行并存的并行差分 run 必须产出完整 JSON 报告（回归 NameError）。"""
    proj, target = _write_project(tmp_path)
    reused_id = _seed_killed_prior(proj, target)
    monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
    monkeypatch.setattr(executor, "_run_baseline_snapshot",
                        lambda *a, **k: {"rc": 0, "tail": "pass",
                                         "elapsed": 5.0})

    result = run_mutation_files([target], proj, budget=10, timeout_s=60,
                                tests_glob="res://tests/unit/save/", jobs=4)

    r = result["results"][0]
    s = r["summary"]
    assert r["ok"] is True
    assert s["mutants"] == 9
    assert s["killed"] == 9
    assert s["resumed"] == 1
    assert s["retry_total"] == 8
    assert s["executed"] == 8
    assert s["diff_skipped"] == [reused_id]
    assert s["untouched_ranges"] == []
    json.dumps(r)  # 报告可整体序列化（契约消费方面）


def test_parallel_first_run_has_no_diff_disclosures(tmp_path, monkeypatch,
                                                    killing_gut):
    """无 manifest 首跑：diff_active=False → 不携带差分披露（与串行同构）。"""
    proj, target = _write_project(tmp_path)
    monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
    monkeypatch.setattr(executor, "_run_baseline_snapshot",
                        lambda *a, **k: {"rc": 0, "tail": "pass",
                                         "elapsed": 5.0})

    result = run_mutation_files([target], proj, budget=10, timeout_s=60,
                                tests_glob="res://tests/unit/save/", jobs=4)

    s = result["results"][0]["summary"]
    assert "diff_skipped" not in s
    assert "retry_total" not in s
    assert s["mutants"] == 9
