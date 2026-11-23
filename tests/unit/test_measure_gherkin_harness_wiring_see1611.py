"""SEE-1613 §SPEC-011/D20 → SEE-1624 单根收口 — measure gherkin 臂
harness 契约位语义。

SEE-1624 单根裁决：`res://addons/gqt_gherkin/harness.gd` 是 addon 单根
内的受控源文件（随 addon 入库），measure gherkin 臂对其只读——旧的
"执行期临时 cp 接线 + finally 移除"机制退役。本文件钉住新契约：
契约位在场断言（缺失 fail-loud）、臂全程零触碰、无双根/回退/自动复制。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from godot_qa_toolkit.measurement import arms
from godot_qa_toolkit.measurement.backup import MeasurementError


def _proj(tmp_path: Path, *, with_contract: bool = True) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "addons" / "gut").mkdir(parents=True)
    (root / "addons" / "gut" / "gut_cmdln.gd").write_text(
        "# gut\n", encoding="utf-8")
    if with_contract:
        dest = root / "addons" / "gqt_gherkin" / "harness.gd"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("extends RefCounted\n", encoding="utf-8")
    return root


def _stub_flow(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(arms, "run_preflight",
                        lambda root, **k: {"ok": True})
    monkeypatch.setattr(arms, "compile_keys_to_suite",
                        lambda project_root, keys, suite_dir: None)
    monkeypatch.setattr(arms, "replay_check",
                        lambda project_root, key, *, suite_dir=None: {})
    monkeypatch.setattr(arms, "_snapshot_main", lambda r: {})
    counter = {"n": 0}

    def fake_suite_dir(r):
        d = Path(tmp_path) / f"suite-{counter['n']}"
        counter["n"] += 1
        d.mkdir(parents=True, exist_ok=True)
        return d

    monkeypatch.setattr(arms, "create_suite_dir", fake_suite_dir)


# ---------- 契约位在场断言 ----------

def test_ensure_raises_when_contract_missing(tmp_path):
    root = _proj(tmp_path, with_contract=False)
    dest = root / "addons" / "gqt_gherkin" / "harness.gd"
    assert not dest.exists()
    with pytest.raises(MeasurementError, match="contract position missing"):
        arms._ensure_gherkin_harness(root)
    # 缺失即 fail-loud：绝不创建/复制任何文件（无自动接线）
    assert not dest.exists()


def test_ensure_is_noop_when_contract_present(tmp_path):
    root = _proj(tmp_path)
    dest = root / "addons" / "gqt_gherkin" / "harness.gd"
    before = dest.read_bytes()
    arms._ensure_gherkin_harness(root)
    assert dest.read_bytes() == before, "contract file must be untouched"


def test_no_legacy_source_copy_lane(tmp_path):
    """无双根/回退：旧布局残留（旧 addon 根）不提供任何回退来源。"""
    root = _proj(tmp_path, with_contract=False)
    legacy = root / "addons" / "gqt_gherkin_old" / "harness.gd"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("extends RefCounted\n# legacy\n", encoding="utf-8")
    with pytest.raises(MeasurementError):
        arms._ensure_gherkin_harness(root)
    assert not (root / "addons" / "gqt_gherkin" / "harness.gd").exists()


# ---------- 行为级：flow 全程零触碰契约位 ----------

def test_flow_keeps_contract_intact_across_compile_and_run(
        tmp_path, monkeypatch):
    root = _proj(tmp_path)
    dest = root / "addons" / "gqt_gherkin" / "harness.gd"
    before = dest.read_bytes()
    seen: dict = {}

    def fake_compile(project_root, keys, suite_dir):
        seen["during_compile"] = dest.read_bytes() == before

    def fake_run_arm(name, **kw):
        seen["during_run"] = dest.read_bytes() == before
        return {"arm": name, "state": "valid"}

    _stub_flow(monkeypatch, tmp_path)
    monkeypatch.setattr(arms, "compile_keys_to_suite", fake_compile)
    monkeypatch.setattr(arms, "run_arm", fake_run_arm)
    out = arms.run_gherkin_flow(
        root=root, recipe={"gherkin_face": {"features": [{"key": "k1"}]}},
        gherkin_keys=["k1"], gherkin_shards=1,
        arms_parallel=False, gherkin_adaptive=False, arm_kwargs={})
    assert out["state"] == "valid"
    assert seen["during_compile"] is True
    assert seen["during_run"] is True
    assert dest.read_bytes() == before, "contract must survive the flow"


def test_flow_contract_missing_is_invalid_named(tmp_path, monkeypatch):
    root = _proj(tmp_path, with_contract=False)
    _stub_flow(monkeypatch, tmp_path)
    out = arms.run_gherkin_flow(
        root=root, recipe={"gherkin_face": {"features": [{"key": "k1"}]}},
        gherkin_keys=["k1"], gherkin_shards=1,
        arms_parallel=False, gherkin_adaptive=False, arm_kwargs={})
    assert out["state"] == "invalid"
    assert "contract position missing" in (out.get("invalid_reason") or "")


def test_flow_never_copies_any_harness(tmp_path, monkeypatch):
    """退役钉：flow 路径上任何 copyfile 调用 = cp 接线回潮。"""
    root = _proj(tmp_path, with_contract=False)
    _stub_flow(monkeypatch, tmp_path)
    import shutil as _shutil
    copy_calls: list = []
    real_copy = _shutil.copyfile

    def spy_copy(src, dst, *a, **k):
        copy_calls.append((src, dst))
        return real_copy(src, dst, *a, **k)

    monkeypatch.setattr(arms.shutil, "copyfile", spy_copy)
    out = arms.run_gherkin_flow(
        root=root, recipe={"gherkin_face": {"features": [{"key": "k1"}]}},
        gherkin_keys=["k1"], gherkin_shards=1,
        arms_parallel=False, gherkin_adaptive=False, arm_kwargs={})
    assert out["state"] == "invalid"  # 契约位缺失 → invalid
    assert not copy_calls, "no copy lane may exist on the flow path"


# ---------- 共享分片（§SPEC-005/006 语义保留） ----------

def test_flow_uses_shared_suite_groups(tmp_path, monkeypatch):
    """恢复 gherkin_shards 共享分片——44 feature 已过
    gherkin-feature-state-isolation 原子化门（SEE-1619 C1），同进程共享
    suite 不再有跨 feature 残留。shards=共享组数（并发上限同步收敛到组
    数），扇出=len(groups)×reps。"""
    root = _proj(tmp_path)
    compiles: list[tuple[list, Path]] = []
    concurrency_seen: dict = {}

    def fake_compile(project_root, keys, suite_dir):
        compiles.append((list(keys), Path(suite_dir)))

    def fake_run_arm(name, **kw):
        concurrency_seen["value"] = kw.get("concurrency")
        concurrency_seen["groups"] = kw["gherkin"]["groups"]
        return {"arm": name, "state": "valid", "gut_ok": True,
                "arm_fail_pairs": set(), "arm_failures": set(),
                "new_pairs": set(),
                "test_failures": {"observed": [], "observed_pairs": [],
                                  "new": [], "new_pairs": [],
                                  "instrumentation_intolerant": []},
                "reconciliation": {"ok": True, "shards": []},
                "wall_s": 0.0}

    _stub_flow(monkeypatch, tmp_path)
    monkeypatch.setattr(arms, "compile_keys_to_suite", fake_compile)
    monkeypatch.setattr(arms, "run_arm", fake_run_arm)
    out = arms.run_gherkin_flow(
        root=root,
        recipe={"gherkin_face": {"features": [
            {"key": "k1"}, {"key": "k2"}, {"key": "k3"}]}},
        gherkin_keys=None, gherkin_shards=2,
        arms_parallel=False, gherkin_adaptive=False, arm_kwargs={})
    assert out["state"] == "valid"
    assert [keys for keys, _ in compiles] == [["k1", "k3"], ["k2"]]
    assert len({str(sd) for _, sd in compiles}) == 2
    assert concurrency_seen["value"] == 2
    assert len(concurrency_seen["groups"]) == 2
    assert out.get("process_fallbacks") == []


def test_run_arm_exception_propagates_with_suite_reclaim(
        tmp_path, monkeypatch):
    """run_arm 抛未列名异常时错误照常传播，且本次新建 suite 目录在
    finally 兜底清理中回收（异常路径零残留）。"""
    import pytest as _pytest

    root = _proj(tmp_path)

    def fake_run_arm(name, **kw):
        raise RuntimeError("injected from run_arm")

    suite_root = Path(tmp_path) / "gherkin" / "generated"
    suite_dir = suite_root / ".gqt-run-deadbeef1234"

    _stub_flow(monkeypatch, tmp_path)
    monkeypatch.setattr(arms, "create_suite_dir", lambda r: suite_dir)
    monkeypatch.setattr(arms, "run_arm", fake_run_arm)
    with _pytest.raises(RuntimeError):
        arms.run_gherkin_flow(
            root=root,
            recipe={"gherkin_face": {"features": [{"key": "k1"}]}},
            gherkin_keys=["k1"], gherkin_shards=1,
            arms_parallel=False, gherkin_adaptive=False, arm_kwargs={})
    assert not suite_dir.exists(), "suite dir residue on run_arm exception"
    dest = root / "addons" / "gqt_gherkin" / "harness.gd"
    assert dest.is_file(), "contract file must never be reclaimed"
