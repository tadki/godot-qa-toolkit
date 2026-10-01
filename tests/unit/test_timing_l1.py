"""SEE-1356 L1 — timing store + timeout budget negotiation tests.

Covers §SPEC-L1-01 (预算公式四分支) / §SPEC-L1-02 (store 键/TTL/git_rev;
缓存命中仍实测 baseline) / §SPEC-L1-03 的报告字段与 suggested_action。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from godot_qa_toolkit.timing import (
    BUDGET_CAP_S,
    BOOTSTRAP_TIMEOUT_S,
    TTL_DAYS,
    TimingStore,
    resolve_budget,
    store_key,
    suggested_action,
    timeout_scale,
    bootstrap_timeout_s,
)


# ---- §SPEC-L1-01: 预算公式四分支 ---------------------------------------------

def test_branch_explicit_with_history_raises_floor():
    # explicit 40 + last 108s → derived 108*2+1=217 → max(40, 217) = 217；
    # 预算由 store 锚决定 → 归因 "store"
    assert resolve_budget(40, 108.0) == (217, "store")


def test_branch_explicit_without_history_uses_bootstrap_floor():
    # explicit 40 + bootstrap 180 → derived 361，bootstrap 锚决定 → "bootstrap"
    assert resolve_budget(40, None) == (361, "bootstrap")


def test_branch_no_explicit_with_history_capped():
    # last 400s → derived 801 → capped at 600; no extra floor
    assert resolve_budget(None, 400.0) == (BUDGET_CAP_S, "store")


def test_branch_no_explicit_no_history_bootstrap():
    assert resolve_budget(None, None) == (361, "bootstrap")


def test_explicit_not_truncated_by_cap():
    budget, source = resolve_budget(900, 108.0)
    assert budget == 900
    assert source == "explicit"


def test_low_cost_history_no_artificial_floor():
    # 有历史不设额外 floor：last 5s → derived 11（不拍脑袋抬回 180/361）
    assert resolve_budget(None, 5.0) == (11, "store")


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("GQT_TIMEOUT_SCALE", "3.0")
    monkeypatch.setenv("GQT_BOOTSTRAP_TIMEOUT_S", "60")
    assert timeout_scale() == 3.0
    assert bootstrap_timeout_s() == 60
    # explicit 缺省 + bootstrap 60 → derived 60*3+1 = 181
    assert resolve_budget(None, None)[0] == 181


def test_env_invalid_values_fall_back(monkeypatch):
    monkeypatch.setenv("GQT_TIMEOUT_SCALE", "not-a-number")
    monkeypatch.setenv("GQT_BOOTSTRAP_TIMEOUT_S", "-5")
    assert timeout_scale() == 2.0
    assert bootstrap_timeout_s() == BOOTSTRAP_TIMEOUT_S


def test_suggested_action_names_the_flag():
    assert suggested_action(600) == "rerun with --timeout 600"


# ---- §SPEC-L1-02: store 键 / TTL / git_rev -----------------------------------

@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "addons" / "gut").mkdir(parents=True)
    (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
    return root


def test_store_key_binds_realpath_kind_glob(tmp_path):
    target = tmp_path / "a.gd"
    target.write_text("func a():\n\treturn 1\n")
    k1 = store_key(str(target), "mutation", "res://tests/x/")
    k2 = store_key(str(target), "mutation", None)
    k3 = store_key(str(target.resolve()), "mutation", "res://tests/x/")
    assert k1 != k2
    assert k1 == k3  # realpath 归一
    assert len(k1) == 40  # sha1


def test_record_and_read_roundtrip(project, tmp_path, monkeypatch):
    target = project / "src.gd"
    target.write_text("func a():\n\treturn 1\n")
    monkeypatch.chdir(project)  # git rev: 无仓库 → None 一致
    store = TimingStore(str(project), str(target), "mutation", None, now=time.time())
    assert store.read() is None  # 无历史
    assert store.record(108.5) is True
    hist = store.read()
    assert hist["last"] == 108.5
    assert hist["samples_n"] == 1
    assert hist["p90"] == 108.5


def test_store_ttl_expiry(project, monkeypatch):
    target = project / "src.gd"
    target.write_text("func a():\n\treturn 1\n")
    monkeypatch.chdir(project)
    now = time.time()
    store = TimingStore(str(project), str(target), "mutation", None, now=now)
    store.record(100.0)
    # 8 天后 → TTL 失效 = 无历史
    aged = TimingStore(str(project), str(target), "mutation", None, now=now + 8 * 86400)
    assert aged.read() is None


def test_store_git_rev_invalidation(project, tmp_path, monkeypatch):
    target = project / "src.gd"
    target.write_text("func a():\n\treturn 1\n")
    monkeypatch.chdir(project)
    # 无 git 仓库：rev=None；伪造一条 rev=X 的记录 → 读侧 None != "X" → 失效
    store = TimingStore(str(project), str(target), "mutation", None, now=time.time())
    store.record(100.0)
    doc = json.loads(store.entry_path.read_text(encoding="utf-8"))
    doc["git_rev"] = "deadbeef"
    store.entry_path.write_text(json.dumps(doc), encoding="utf-8")
    assert store.read() is None


def test_store_tail10_and_p90(project, monkeypatch):
    target = project / "src.gd"
    target.write_text("func a():\n\treturn 1\n")
    monkeypatch.chdir(project)
    store = TimingStore(str(project), str(target), "mutation", None, now=time.time())
    for i in range(1, 13):
        store.record(float(i))
    hist = store.read()
    assert hist["samples_n"] == 10  # tail-10
    assert hist["samples"] == [3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0]
    assert hist["p90"] == pytest.approx(11.0)  # nearest-rank p90 of 10 samples
    assert hist["last"] == 12.0


def test_store_corrupt_entry_is_no_history(project, monkeypatch):
    target = project / "src.gd"
    target.write_text("func a():\n\treturn 1\n")
    monkeypatch.chdir(project)
    store = TimingStore(str(project), str(target), "mutation", None, now=time.time())
    store.store_dir.mkdir(parents=True, exist_ok=True)
    store.entry_path.write_text("{not json", encoding="utf-8")
    assert store.read() is None


def test_store_self_ignoring(project, monkeypatch):
    target = project / "src.gd"
    target.write_text("func a():\n\treturn 1\n")
    monkeypatch.chdir(project)
    store = TimingStore(str(project), str(target), "mutation", None, now=time.time())
    store.record(1.0)
    assert (store.store_dir / ".gitignore").read_text(encoding="utf-8").strip() == "*"


def test_gitignore_written_once_not_on_every_record(project, monkeypatch):
    # LOW2 (Final Review): the self-ignoring .gitignore is a one-time
    # directory-initialization action — record() must NOT rewrite it on the
    # hot path (mtime stays fixed across repeated records).
    target = project / "src.gd"
    target.write_text("func a():\n\treturn 1\n")
    monkeypatch.chdir(project)
    store = TimingStore(str(project), str(target), "mutation", None, now=time.time())
    store.record(1.0)
    gi = store.store_dir / ".gitignore"
    mtime_1 = gi.stat().st_mtime_ns
    for i in range(2, 6):
        store.record(float(i))
    assert gi.read_text(encoding="utf-8").strip() == "*"  # semantics unchanged
    assert gi.stat().st_mtime_ns == mtime_1  # untouched by hot path
    # A pre-existing .gitignore is never second-guessed or overwritten.
    gi.write_text("# operator-managed\n", encoding="utf-8")
    store.record(6.0)
    assert gi.read_text(encoding="utf-8").strip() == "# operator-managed"


# ---- §SPEC-L1-02 判据场景：缓存命中喂锚，baseline 仍实测 ----------------------

def _mutation_fixture(project, monkeypatch):
    from godot_qa_toolkit.mutation import runner as mut

    target = project / "calc.gd"
    target.write_text("func add(a, b):\n\treturn a + b\n")
    calls = {"gut_runs": 0}

    def fake_run_gut(project_root, timeout_s=60, tests_glob=None):
        calls["gut_runs"] += 1
        calls["last_timeout"] = timeout_s
        time.sleep(0.01)  # 实测耗时 > 0 —— baseline 永远真实跑
        return 0, ""

    monkeypatch.setattr(mut, "_run_gut_on_project", fake_run_gut)
    return mut, target, calls


def test_cache_hit_feeds_anchor_and_baseline_still_measured(project, monkeypatch):
    mut, target, calls = _mutation_fixture(project, monkeypatch)
    monkeypatch.chdir(project)
    store = TimingStore(str(project), str(target), "mutation", "res://tests/", now=time.time())
    store.record(108.0)

    result = mut.run_mutation(str(target), str(project), budget=5, timeout_s=60,
                              tests_glob="res://tests/", timing_store=store)
    # 缓存锚生效：首轮 baseline 用协商后预算（2×108+1=217，explicit 只抬下限）
    assert calls["gut_runs"] >= 1
    assert calls["last_timeout"] == 217
    # baseline 实测仍执行（缓存只喂预算锚，对照基准不得来自缓存）
    assert result["summary"]["baseline_reused"] is False
    # 报告字段（§SPEC-L1-03）
    assert result["summary"]["timing_source"] == "store"
    assert result["summary"]["samples_n"] == 2  # 预存 1 + 本轮实测 1
    assert result["summary"]["baseline_p90"] is not None


def test_no_history_bootstrap_and_sample_recorded(project, monkeypatch):
    mut, target, calls = _mutation_fixture(project, monkeypatch)
    monkeypatch.chdir(project)
    store = TimingStore(str(project), str(target), "mutation", "res://tests/", now=time.time())
    result = mut.run_mutation(str(target), str(project), budget=5, timeout_s=60,
                              tests_glob="res://tests/", timing_store=store)
    assert result["summary"]["timing_source"] == "bootstrap"
    assert calls["last_timeout"] == 361
    assert result["summary"]["samples_n"] == 1  # 本轮实测采样落盘
    assert store.read()["last"] > 0.0


def test_explicit_timeout_source_and_floor(project, monkeypatch):
    mut, target, calls = _mutation_fixture(project, monkeypatch)
    monkeypatch.chdir(project)
    store = TimingStore(str(project), str(target), "mutation", "res://tests/", now=time.time())
    store.record(10.0)  # 低耗历史：derived 21 < explicit 60 → 60
    result = mut.run_mutation(str(target), str(project), budget=5, timeout_s=60,
                              tests_glob="res://tests/", timing_store=store)
    assert result["summary"]["timing_source"] == "explicit"
    assert calls["last_timeout"] == 60


def test_dry_run_skips_store_io(project, monkeypatch):
    mut, target, calls = _mutation_fixture(project, monkeypatch)
    monkeypatch.chdir(project)
    store = TimingStore(str(project), str(target), "mutation", "res://tests/", now=time.time())
    store.record(108.0)
    result = mut.run_mutation(str(target), str(project), budget=5, timeout_s=60,
                              dry_run=True, tests_glob="res://tests/",
                              timing_store=store)
    assert result["summary"]["dry_run"] is True
    assert store.read()["samples_n"] == 1  # 零写入：采样数未变


def test_baseline_timeout_run_error_carries_suggested_action(project, monkeypatch):
    import subprocess

    mut, target, calls = _mutation_fixture(project, monkeypatch)
    monkeypatch.chdir(project)

    def fake_timeout(project_root, timeout_s=60, tests_glob=None):
        raise subprocess.TimeoutExpired(cmd="godot", timeout=timeout_s)

    monkeypatch.setattr(mut, "_run_gut_on_project", fake_timeout)
    result = mut.run_mutation(str(target), str(project), budget=5, timeout_s=60,
                              tests_glob="res://tests/")
    assert result["summary"]["run_error"] is True
    assert result["summary"]["suggested_action"] == "rerun with --timeout 600"


# ---- SEE-1356 hardener: 分支边界补缺（每条注明所杀变异类别） ------------------

def test_git_rev_probe_failure_is_no_history(project, monkeypatch):
    # kills: `_git_rev` 的 except (OSError, SubprocessError) 分支删除——
    # git 探测失败必须降级 rev=None，而不是把预算锚变成崩溃源。
    import subprocess

    target = project / "src.gd"
    target.write_text("func a():\n\treturn 1\n")
    monkeypatch.chdir(project)

    def boom(*a, **kw):
        raise OSError("git binary vanished")

    monkeypatch.setattr("godot_qa_toolkit.timing.subprocess.run", boom)
    store = TimingStore(str(project), str(target), "mutation", None, now=time.time())
    assert store.record(42.0) is True
    doc = json.loads(store.entry_path.read_text(encoding="utf-8"))
    assert doc["git_rev"] is None
    assert store.read()["last"] == 42.0  # rev None == None → 有效


def test_store_empty_or_nonnumeric_samples_is_no_history(project, monkeypatch):
    # kills: `if not samples: return None` 分支删除（空 samples 会把
    # samples[-1] 变 IndexError）与非数值元素过滤删除（字符串样本会炸 float）。
    target = project / "src.gd"
    target.write_text("func a():\n\treturn 1\n")
    monkeypatch.chdir(project)
    store = TimingStore(str(project), str(target), "mutation", None, now=time.time())
    store.store_dir.mkdir(parents=True, exist_ok=True)
    store.entry_path.write_text(json.dumps({"updated_at": time.time(), "git_rev": None, "samples": []}), encoding="utf-8")
    assert store.read() is None  # 空 samples = 无历史
    store.entry_path.write_text(json.dumps({"updated_at": time.time(), "git_rev": None, "samples": ["x", 7, True]}), encoding="utf-8")
    hist = store.read()
    assert hist["samples"] == [7.0, 1.0]  # 非数值剔除（True 被 int 接受）
    assert hist["last"] == 1.0


def test_store_unreadable_entry_is_no_history(project, monkeypatch):
    # kills: `read()` 的 OSError 捕获分支（损坏文件已在别例覆盖 ValueError；
    # 权限拒绝走 OSError —— 读失败必须诚实降级为无历史）。
    target = project / "src.gd"
    target.write_text("func a():\n\treturn 1\n")
    monkeypatch.chdir(project)
    store = TimingStore(str(project), str(target), "mutation", None, now=time.time())
    store.store_dir.mkdir(parents=True, exist_ok=True)
    store.entry_path.write_text("{}", encoding="utf-8")
    store.entry_path.chmod(0o000)
    try:
        assert store.read() is None
    finally:
        store.entry_path.chmod(0o644)


def test_record_write_failure_returns_false(project, monkeypatch):
    # kills: `record()` 的 except OSError → False 分支删除——写失败必须
    # 静默返回 False（预算锚是增强，不是契约），不能炸掉 mutation 轮。
    target = project / "src.gd"
    target.write_text("func a():\n\treturn 1\n")
    monkeypatch.chdir(project)
    store = TimingStore(str(project), str(target), "mutation", None, now=time.time())
    cache_dir = Path(store.project_root) / ".qa-cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "gqt-timing").write_text("not a dir", encoding="utf-8")
    assert store.record(1.0) is False


def test_p90_empty_is_zero():
    # kills: `_p90([])` 早退分支删除（空样本集会 min/max 出 IndexError）。
    from godot_qa_toolkit.timing import _p90

    assert _p90([]) == 0.0
    assert _p90([5.0]) == 5.0  # 单样本：nearest-rank 收敛到自身


def test_ttl_boundary_exact_is_valid_plus_one_is_expired(project, monkeypatch):
    # kills: `age_s > TTL_DAYS * 86400` 严格性变异（>= 会在整 7 天边界
    # 误杀有效历史；<= 会在边界保留过期历史）。
    target = project / "src.gd"
    target.write_text("func a():\n\treturn 1\n")
    monkeypatch.chdir(project)
    now = time.time()
    store = TimingStore(str(project), str(target), "mutation", None, now=now)
    store.record(100.0)
    exact = TimingStore(str(project), str(target), "mutation", None, now=now + TTL_DAYS * 86400)
    assert exact.read() is not None  # age == TTL → 仍有效
    expired = TimingStore(str(project), str(target), "mutation", None, now=now + TTL_DAYS * 86400 + 1)
    assert expired.read() is None  # age == TTL + 1s → 失效


def test_explicit_equal_to_derived_ties_to_anchor_source():
    # kills: `explicit > derived` 严格性变异（>= 会把相等情形错误归因
    # "explicit"——归因给"最终决定预算的那一方"的契约被破坏）。
    budget, source = resolve_budget(101, 50.0)  # derived = 50*2+1 = 101 == explicit
    assert (budget, source) == (101, "store")


def test_env_scale_and_bootstrap_boundary_values(monkeypatch):
    # kills: 双 env 的 `v > 0` 边界（0 与负值必须回退默认）与
    # int(float()) 转换分支（浮点字符串向下取整）。
    monkeypatch.setenv("GQT_TIMEOUT_SCALE", "0")
    monkeypatch.setenv("GQT_BOOTSTRAP_TIMEOUT_S", "0")
    assert timeout_scale() == 2.0
    assert bootstrap_timeout_s() == BOOTSTRAP_TIMEOUT_S
    monkeypatch.setenv("GQT_TIMEOUT_SCALE", "-3")
    monkeypatch.setenv("GQT_BOOTSTRAP_TIMEOUT_S", "3.7")
    assert timeout_scale() == 2.0
    assert bootstrap_timeout_s() == 3  # int(float("3.7")) 向下取整


def test_concurrent_record_keeps_entry_parseable(project, monkeypatch):
    # 并发写（生产形态 = 跨进程）：pid-suffixed tmp + os.replace 原子发布。
    # kills: tmp 后缀删除（跨进程并发写互相踩踏产生撕裂文件）与
    # replace→直接 write 变异——并发下每次读取都必须是完整合法 JSON。
    import os
    import subprocess
    import sys
    from pathlib import Path as _Path

    real_src = _Path(__file__).resolve().parents[2] / "src"

    target = project / "src.gd"
    target.write_text("func a():\n\treturn 1\n")
    monkeypatch.chdir(project)
    store = TimingStore(str(project), str(target), "mutation", None, now=time.time())
    assert (real_src / "godot_qa_toolkit" / "timing.py").exists()
    env = {**os.environ, "PYTHONPATH": str(real_src)}
    worker = (
        "import sys;from godot_qa_toolkit.timing import TimingStore;"
        f"TimingStore({str(project)!r}, {str(target)!r}, 'mutation', None, now={time.time()!r})"
        ".record(float(sys.argv[1]))"
    )
    procs = [subprocess.Popen([sys.executable, "-c", worker, str(i)], env=env) for i in range(8)]
    for p_ in procs:
        assert p_.wait(timeout=30) == 0
    hist = store.read()
    assert hist is not None  # 撕裂文件会让 read() 直接 None
    assert 1 <= hist["samples_n"] <= 10
    assert hist["last"] in hist["samples"]
