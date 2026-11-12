"""SEE-1564 §SPEC-207 D-1 回归钉子：worker 默认通道下 warm 真零执行复用。

缺陷（Revy 实测对照）：父层 manifest 以缺省 cold 身份落键、worker 记录以
worker-reset 身份落键 → file_key 分裂 → 下轮 plan_diff 无条目可复用
（第 2 轮 executed=5、diff_skipped=[]）。修复后父层与记录同通道同键：
同 fixture 连跑两轮（cache 不清），第二轮 diff_skipped 全命中、executed=0；
冷通道既有钉子不动（本文件不触碰 GQT_MUTATION_WORKER=0 语义，§3.6 证书
按 scheme 隔离不变）。

引擎无关替身面：worker 路由经 resolve_exec_scheme 强制 worker-reset；腿
执行以 runner._run_cold_legs 承接（真实经过的是身份解析、store2 建键、
记录落盘与父层 manifest 写入——钉的就是这套键机制）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import executor
from godot_qa_toolkit.mutation.executor import run_mutation_files
from godot_qa_toolkit.mutation.worker_channel import WORKER_SCHEME
import godot_qa_toolkit.mutation.runner as runner_mod
import godot_qa_toolkit.mutation.worker_channel as worker_channel_mod

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
    # 杀手测试可归因（TestOwnershipIndex 唯一命中）——证书真实签发与校验
    # 才能走通（否则证书门保守重跑，那是另一条语义，不是本钉子的对象）。
    (save / "test_save.gd").write_text(
        'preload("res://calc.gd")\n\n'
        "func test_killed_by_probe():\n\tpass\n")
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


def _install_worker_fakes(monkeypatch):
    """强制 worker-reset 路由 + 腿执行以冷腿承接（键机制全真实）。"""
    monkeypatch.setattr(worker_channel_mod, "resolve_exec_scheme",
                        lambda *a, **k: (WORKER_SCHEME, None))
    monkeypatch.setattr(runner_mod, "resolve_exec_scheme",
                        lambda *a, **k: (WORKER_SCHEME, None))

    def shim(path, project_root, original_src, mutants, tests_glob,
             baseline_failures, baseline_dirty, slot, auto_name,
             target_rel, target_res, kind, reporter, store2,
             prior_entries, scopes, ownership_fn, timings_out=None):
        raw = runner_mod._run_cold_legs(
            path, project_root, original_src, "", mutants, tests_glob,
            baseline_failures, baseline_dirty, slot, auto_name, target_rel,
            target_res, ownership_fn, reporter, store2, prior_entries, scopes)
        return raw, []

    monkeypatch.setattr(worker_channel_mod, "execute_worker_branch", shim)
    monkeypatch.setattr(runner_mod, "_run_gut_on_project",
                        lambda root, timeout_s=60, tests_glob=None:
                            (1, "* test_killed_by_probe\n\t[Failed]\n"))
    monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
    monkeypatch.setattr(executor, "_run_baseline_snapshot",
                        lambda *a, **k: {"rc": 0, "tail": "pass",
                                         "elapsed": 5.0})


def _store_key_dirs(proj):
    base = Path(proj) / ".qa-cache" / "gqt-mutation-resume-v2"
    return {p.name for p in base.iterdir() if p.is_dir()} \
        if base.is_dir() else set()


def test_warm_worker_second_round_full_reuse(tmp_path, monkeypatch):
    """同 fixture 连跑两轮、cache 只清一次：第 2 轮 diff_skipped 全命中、
    executed=0（Revy D-1 对照口径）。"""
    _install_worker_fakes(monkeypatch)
    proj, target = _write_project(tmp_path)
    src = Path(target).read_text(encoding="utf-8")
    n_sites = len(executor._collect_mutations(src, target))
    assert n_sites > 1

    r1 = run_mutation_files([target], proj, budget=10, timeout_s=60,
                            tests_glob="res://tests/unit/save/", jobs=2)
    s1 = r1["results"][0]["summary"]
    assert s1["mutants"] == n_sites and s1["killed"] == n_sites

    r2 = run_mutation_files([target], proj, budget=10, timeout_s=60,
                            tests_glob="res://tests/unit/save/", jobs=2)
    s2 = r2["results"][0]["summary"]
    assert s2["executed"] == 0, (
        f"second worker-channel round must fully reuse (D-1), got "
        f"executed={s2.get('executed')} diff_skipped={s2.get('diff_skipped')}")
    skipped = sorted(set(s2.get("diff_skipped") or []))
    assert len(skipped) == n_sites, (
        f"diff_skipped must hit all {n_sites} killed sites (D-1 全命中), "
        f"got {len(skipped)}")
    assert s2["mutants"] == n_sites and s2["killed"] == n_sites


def test_warm_worker_single_store_key(tmp_path, monkeypatch):
    """键分裂缺陷签名：worker 轮的记录与 manifest 必须同键（恰一个
    file_key 目录）；拆成 worker 条目键 + cold manifest 键即红。"""
    _install_worker_fakes(monkeypatch)
    proj, target = _write_project(tmp_path)
    run_mutation_files([target], proj, budget=10, timeout_s=60,
                       tests_glob="res://tests/unit/save/", jobs=2)
    keys = _store_key_dirs(proj)
    assert len(keys) == 1, (
        "records and manifest must share one file_key under the worker "
        f"channel (D-1 key split: {sorted(keys)})")
