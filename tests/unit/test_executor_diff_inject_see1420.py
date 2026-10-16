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


def test_parallel_truncation_executed_excludes_static_invalid(
        tmp_path, monkeypatch):
    """O2 并行缝回归（SEE-1420 qa 复验实机 FAIL 实证）：截断披露的
    executed 不得回填计划面（min(total_sites, budget)）——worker 层已按
    invalid 预检覆写，父层逐分片求和。实机 20 vs mutants 19 漂移即此缝。"""
    src = ("func one(a):\n"
           "\treturn a + 1\n"
           "\n"
           "func two(a):\n"
           "\treturn a + 2\n")
    proj, target = _write_project(tmp_path)
    Path(target).write_text(src, encoding="utf-8")
    monkeypatch.setattr(runner_mod, "_run_gut_on_project",
                        lambda root, timeout_s=60, tests_glob=None:
                            (1, "* test_killed_by_probe\n\t[Failed]\n"))

    from godot_qa_toolkit.mutation import reporting as _rep
    orig_split = _rep._split_invalid_mutants

    def split_with_one_invalid(src_text, mutants):
        # 按 stable_id 精确失效一个位点（one#1）——分片 worker 各自调用
        # split，按序号搬运会 每个 chunk 各产一个 invalid。
        valid, invalid = orig_split(src_text, mutants)
        target = next((m for m in valid
                       if m.stable_id.endswith("#one#1")), None)
        if target is not None:
            valid = [m for m in valid if m is not target]
            invalid = invalid + [target]
        return valid, invalid

    monkeypatch.setattr(_rep, "_split_invalid_mutants", split_with_one_invalid)
    monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
    monkeypatch.setattr(executor, "_run_baseline_snapshot",
                        lambda *a, **k: {"rc": 0, "tail": "pass",
                                         "elapsed": 5.0})

    result = run_mutation_files([target], proj, budget=4, timeout_s=60,
                                tests_glob="res://tests/unit/save/", jobs=2)

    s = result["results"][0]["summary"]
    assert s["invalid_mutants"] == 1
    # 截断面 4 位点、1 个静态 invalid → 实际 GUT 执行 3
    assert s["executed"] == 3, (
        "truncation-disclosed executed must count real GUT runs only "
        f"(plan-face leak: {s['executed']})")


def test_parallel_reuse_coverage_keeps_non_instrumentable_sites(
        tmp_path, monkeypatch):
    """D3 并行缝回归（SEE-1420 qa 复验 FAIL 实证）：executor 侧预检的
    uncovered 行换新锚、pool 行仍 covered-only → 不可插桩行位点被静默
    丢弃（不执行也不标记）。修复后：执行面 = covered ∨ 不可插桩。"""
    proj, target = _write_project(tmp_path)
    monkeypatch.setattr(runner_mod, "_run_gut_on_project",
                        lambda root, timeout_s=60, tests_glob=None:
                            (1, "* test_killed_by_probe\n\t[Failed]\n"))
    monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
    monkeypatch.setattr(executor, "_run_baseline_snapshot",
                        lambda *a, **k: {"rc": 0, "tail": "pass",
                                         "elapsed": 5.0})

    from godot_qa_toolkit.mutation.coverlink import save_coverage
    # 位点行 2/3/4；2、3 covered 且可插桩；行 4 不可插桩（探针面外）
    assert save_coverage({2, 3}, 4, proj, target,
                         "res://tests/unit/save/",
                         instrumentable_lines={2, 3})

    result = run_mutation_files([target], proj, budget=10, timeout_s=60,
                                tests_glob="res://tests/unit/save/",
                                jobs=2, reuse_coverage=True)

    r = result["results"][0]
    s = r["summary"]
    unc = [f for f in r.get("failures", []) if f.get("verdict") == "uncovered"]
    assert unc == [], "no site may be marked uncovered in this fixture"
    assert s["mutants"] == 9, (
        "non-instrumentable-line sites must stay in the execution face "
        f"(covered-only pool leak: {s['mutants']} of 9)")
    assert s["killed"] == 9


def test_zero_executed_reuses_precomputed_baseline(tmp_path, monkeypatch):
    """§14-B 回归：全复用零执行时不得再实测第二轮 baseline（GUT 调用 1≠2）。

    实机缺陷：executor 零执行回退 run_mutation 未注入父层已预跑的共享
    baseline 快照 → run_mutation「永远实测」再跑全套 GUT 一次。
    """
    proj, target = _write_project(tmp_path)
    header_hash, _spans, mutants, scope_hashes = _mutants_of(target)
    store = ResumeStoreV2(proj, target, "res://tests/unit/save/")
    src = Path(target).read_text(encoding="utf-8")
    assert store.write_manifest(header_hash, scope_hashes,
                                store._sha1_text(src), "calc.gd")
    for m in mutants:
        assert store.record_entry({
            "id": m.stable_id, "verdict": "killed", "reason": "seed",
            "line": m.line, "column": m.column, "kind": m.kind,
            "original": m.original, "mutated": m.mutated,
            "scope_hash": scope_hashes[m.scope_id], "timeout_streak": 0,
        })

    calls = {"gut": 0}

    def counting_gut(root, timeout_s=60, tests_glob=None):
        calls["gut"] += 1
        return (1, "* test_killed_by_probe\n\t[Failed]\n")

    monkeypatch.setattr(runner_mod, "_run_gut_on_project", counting_gut)
    monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
    monkeypatch.setattr(executor, "_run_baseline_snapshot",
                        lambda *a, **k: {"rc": 0, "tail": "pass",
                                         "elapsed": 5.0})

    result = run_mutation_files([target], proj, budget=10, timeout_s=60,
                                tests_glob="res://tests/unit/save/", jobs=4)

    r = result["results"][0]
    assert r["ok"] is True
    assert r["summary"]["executed"] == 0
    assert r["summary"]["resumed"] == 9
    assert calls["gut"] == 0, (
        "zero-executed fallback must reuse the precomputed baseline — "
        f"got {calls['gut']} extra baseline GUT runs (§14-B: GUT==1)")
