"""SEE-1576 D2 修复钉测试（§SPEC-609 R-4 / §SPEC-610 R-5）。

R-4：差分复用路径 `_inject_reused` 的 failures[] 必须与常规路径
（reporting._mutation_report `verdict != "killed"` 过滤）同口径——killed
复用记录只经 summary 计数 + diff_skipped 披露，不再回放进 failures[]。

R-5：相对路径目标锚定 `--project-root`（绝对路径直过）——mutation 多文件
检查点 / run_mutation / coverage run_coverage 三处同形态检查点同口径，
路径解析不依赖调用方 CWD。
"""

import hashlib
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
import godot_qa_toolkit.coverage.runner as coverage_runner_mod
from godot_qa_toolkit.mutation.executor import run_mutation_files
from godot_qa_toolkit.mutation.runner import run_mutation
from godot_qa_toolkit.coverage.runner import run_coverage

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


def _real_cert(proj):
    from godot_qa_toolkit.mutation.identity import deps_closure_sha1
    script_res = "res://tests/unit/save/test_save.gd"
    fs = Path(proj) / "tests/unit/save/test_save.gd"
    return [{
        "test_name": "test_x", "script_path": script_res,
        "script_sha1": hashlib.sha1(fs.read_bytes()).hexdigest(),
        "deps_sha1": deps_closure_sha1(str(proj), script_res),
    }]


def _seed_store(proj, target):
    from godot_qa_toolkit.mutation.identity import identity_pack
    from godot_qa_toolkit.mutation.inject import detect_binary_kind
    ident = identity_pack(str(proj), detect_binary_kind())
    return ResumeStoreV2(proj, target, "res://tests/unit/save/",
                         binary_kind=detect_binary_kind(), identity=ident)


def _seed_prior(proj, target, verdict, index=0):
    """预置 v2 store 条目：killed / suspect 均属「复用跳过」档（survived 永远重跑）。"""
    header_hash, _spans, mutants, scope_hashes = _mutants_of(target)
    m0 = mutants[index]
    store = _seed_store(proj, target)
    assert store.write_manifest(header_hash, scope_hashes,
                                store._sha1_text(
                                    Path(target).read_text(encoding="utf-8")),
                                project_relative(proj, target))
    entry = {
        "id": m0.stable_id,
        "verdict": verdict,
        "reason": f"seed {m0.kind} {verdict}",
        "line": m0.line, "column": m0.column, "kind": m0.kind,
        "original": m0.original, "mutated": m0.mutated,
        "scope_hash": scope_hashes[m0.scope_id],
        "timeout_streak": 0,
    }
    if verdict == "killed":
        entry["killed_by"] = _real_cert(proj)
    assert store.record_entry(entry)
    return m0.stable_id


@pytest.fixture()
def killing_gut(monkeypatch):
    monkeypatch.setattr(
        runner_mod, "_run_gut_on_project",
        lambda root, timeout_s=60, tests_glob=None:
            (1, "* test_killed_by_probe\n\t[Failed]\n"))


def _run_parallel(proj, target, monkeypatch, budget=10):
    monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
    monkeypatch.setattr(executor, "_run_baseline_snapshot",
                        lambda *a, **k: {"rc": 0, "tail": "pass",
                                         "elapsed": 5.0})
    return run_mutation_files([target], proj, budget=budget, timeout_s=60,
                              tests_glob="res://tests/unit/save/", jobs=4)


# ---------------------------------------------------------------- R-4 pins

def test_r4_reused_killed_not_in_failures(tmp_path, monkeypatch, killing_gut):
    """§SPEC-609：killed 复用记录不得回放进 failures[]（与常规路径同口径）。"""
    proj, target = _write_project(tmp_path)
    reused_id = _seed_prior(proj, target, "killed")
    result = _run_parallel(proj, target, monkeypatch)

    r = result["results"][0]
    s = r["summary"]
    killed_failures = [f for f in r.get("failures", [])
                       if f.get("verdict") == "killed"]
    assert killed_failures == [], (
        f"killed 复用条目被回放进 failures[]: {killed_failures}")
    # 计数与披露语义不变：killed 仍计入 summary，复用 id 仍由 diff_skipped 呈现
    assert s["killed"] == 9
    assert s["mutants"] == 9
    assert s["resumed"] == 1
    assert s["diff_skipped"] == [reused_id]
    json.dumps(r)  # 报告整体可序列化


def test_r4_reused_suspect_stays_in_failures(tmp_path, monkeypatch, killing_gut):
    """§SPEC-609：非 killed 的复用条目（suspect 档）仍须进 failures[]。"""
    proj, target = _write_project(tmp_path)
    reused_id = _seed_prior(proj, target, "suspect")
    result = _run_parallel(proj, target, monkeypatch)

    r = result["results"][0]
    s = r["summary"]
    suspect_failures = [f for f in r.get("failures", [])
                        if f.get("verdict") == "suspect"]
    assert len(suspect_failures) == 1, (
        f"suspect 复用条目丢失于 failures[]: {r.get('failures', [])}")
    assert s["suspect"] == 1
    assert s["resumed"] == 1
    assert s["diff_skipped"] == [reused_id]


# ---------------------------------------------------------------- R-5 pins

def test_r5_mutation_relative_target_anchored_to_project_root(
        tmp_path, monkeypatch, killing_gut):
    """§SPEC-610：仓外 CWD + 相对路径 + --project-root 的多文件检查点跑通。"""
    proj, _target = _write_project(tmp_path)
    monkeypatch.chdir(tmp_path)  # CWD != project_root
    monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
    monkeypatch.setattr(executor, "_run_baseline_snapshot",
                        lambda *a, **k: {"rc": 0, "tail": "pass",
                                         "elapsed": 5.0})
    result = run_mutation_files(["calc.gd"], proj, budget=10, timeout_s=60,
                                tests_glob="res://tests/unit/save/", jobs=4)
    assert result["results"][0]["summary"]["file"].endswith("calc.gd")


def test_r5_run_mutation_relative_dry_run_anchored(tmp_path, monkeypatch):
    """§SPEC-610：run_mutation 串行入口对相对路径同口径锚定（dry-run 验证）。"""
    proj, _target = _write_project(tmp_path)
    monkeypatch.chdir(tmp_path)  # CWD != project_root
    result = run_mutation("calc.gd", proj, dry_run=True)
    assert result["summary"]["file"].endswith("calc.gd")


class _GutReached(RuntimeError):
    pass


def _gut_reached_patch(monkeypatch):
    def _boom(*a, **k):
        raise _GutReached("gut-reached")

    monkeypatch.setattr(coverage_runner_mod, "_run_gut_for_coverage", _boom)


def test_r5_coverage_relative_target_anchored(tmp_path, monkeypatch):
    """§SPEC-610：coverage/runner.py 同形态检查点保持同一口径（相对路径）。"""
    proj, _target = _write_project(tmp_path)
    _gut_reached_patch(monkeypatch)
    monkeypatch.chdir(tmp_path)  # CWD != project_root
    # 通过 is_file 检查即到达 GUT 调用点（修复前抛 FileNotFoundError）
    with pytest.raises(_GutReached):
        run_coverage("calc.gd", proj)


def test_r5_coverage_absolute_target_unaffected(tmp_path, monkeypatch):
    """§SPEC-610：coverage 绝对路径直过（锚定只作用于相对路径）。"""
    proj, target = _write_project(tmp_path)
    _gut_reached_patch(monkeypatch)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(_GutReached):
        run_coverage(target, proj)


def test_r5_absolute_target_unaffected(tmp_path, monkeypatch):
    """§SPEC-610：绝对路径直过（锚定只作用于相对路径）。"""
    proj, target = _write_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = run_mutation(target, proj, dry_run=True)
    assert result["summary"]["file"] == target


# ------------------------------------- not-found 防御分支钉（D2-hardener 补）

def test_r5_mutation_notfound_fail_loud_with_anchored_path(
        tmp_path, monkeypatch):
    """D2-hardener：mutation 串行入口 not-found 防御分支必须 fail-loud，
    且错误消息附锚定后绝对路径（tidy 登记的既有覆盖缺口，见
    mutation/runner.py 的 target not found raise）。"""
    proj, _target = _write_project(tmp_path)
    monkeypatch.chdir(tmp_path)  # CWD != project_root，目标在两处均不存在
    with pytest.raises(FileNotFoundError) as excinfo:
        run_mutation("nope/missing.gd", proj, dry_run=True)
    msg = str(excinfo.value)
    assert "mutation target not found" in msg, (
        f"not-found 分支未按语义消息 fail-loud: {msg}")
    anchored = str(Path(proj) / "nope" / "missing.gd")
    assert anchored in msg, (
        f"错误消息缺锚定后路径（fail-loud 排障语义丢失）: {msg}")


def test_r5_coverage_notfound_fail_loud_with_anchored_path(
        tmp_path, monkeypatch):
    """D2-hardener：coverage 入口 not-found 防御分支必须 fail-loud（抛
    FileNotFoundError 而非静默走 §SPEC-028 结构化错误档），且消息附锚定
    后绝对路径（tidy 登记的既有覆盖缺口，见 coverage/runner.py 的
    target not found raise）。"""
    proj, _target = _write_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError) as excinfo:
        run_coverage("nope/missing.gd", proj)
    msg = str(excinfo.value)
    assert "coverage target not found" in msg, (
        f"not-found 分支未按语义消息 fail-loud: {msg}")
    anchored = str(Path(proj) / "nope" / "missing.gd")
    assert anchored in msg, (
        f"错误消息缺锚定后路径（fail-loud 排障语义丢失）: {msg}")
