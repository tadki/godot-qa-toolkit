"""SEE-1613 §SPEC-011/D20 — measure acceptance 臂 harness 契约位接线。

CI accept 流水线在跑 gherkin 前会把项目源 `acceptance/harness/harness.gd`
幂等接线到契约位 `res://addons/gqt_accept/harness.gd`（该契约位被
.gitignore 忽略、不入库）。measure 的 acceptance 臂此前直接编译+跑 GUT，
跳过接线步 → 生成测试 before_all/`_make_harness` 全部 fail_test
("acceptance harness unavailable") → 全量 acceptance 面 177 红。

修复：acceptance 臂执行期临时接线，finally 移除（measure 既有
「生产树零残留」契约；已存在契约位时保持幂等不动）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from godot_qa_toolkit.measurement import arms


def _proj(tmp_path: Path, *, with_source: bool = True,
          with_contract: bool = False) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    if with_source:
        src = root / "acceptance" / "harness" / "harness.gd"
        src.parent.mkdir(parents=True)
        src.write_text("extends RefCounted\n", encoding="utf-8")
    if with_contract:
        dest = root / "addons" / "gqt_accept" / "harness.gd"
        dest.parent.mkdir(parents=True)
        dest.write_text("extends RefCounted\n# pre-wired\n", encoding="utf-8")
    return root


def test_ensure_wires_missing_contract_then_cleanup_removes(tmp_path):
    root = _proj(tmp_path)
    dest = root / "addons" / "gqt_accept" / "harness.gd"
    assert not dest.exists()

    wired = arms._ensure_acceptance_harness(root)
    assert wired == dest
    assert dest.is_file()
    assert dest.read_text(encoding="utf-8") == "extends RefCounted\n"

    arms._cleanup_acceptance_harness(wired)
    assert not dest.exists()
    # 空目录一并清理（生产树零残留）
    assert not (root / "addons" / "gqt_accept").exists()


def test_ensure_is_idempotent_when_contract_already_present(tmp_path):
    root = _proj(tmp_path, with_contract=True)
    dest = root / "addons" / "gqt_accept" / "harness.gd"
    before = dest.read_text(encoding="utf-8")

    assert arms._ensure_acceptance_harness(root) is None
    assert dest.read_text(encoding="utf-8") == before
    arms._cleanup_acceptance_harness(None)
    assert dest.is_file(), "pre-existing contract must never be removed"


def test_ensure_skips_when_project_has_no_harness_source(tmp_path):
    root = _proj(tmp_path, with_source=False)
    assert arms._ensure_acceptance_harness(root) is None
    assert not (root / "addons" / "gqt_accept").exists()


def test_acceptance_flow_cleans_contract_on_success(tmp_path, monkeypatch):
    """行为级：run_acceptance_flow 成功路径返回后契约位不得残留。"""
    root = _proj(tmp_path)
    dest = root / "addons" / "gqt_accept" / "harness.gd"
    seen: dict = {}

    def fake_compile(project_root, keys, suite_dir):
        seen["wired_during_compile"] = dest.is_file()

    def fake_replay(project_root, key, *, suite_dir=None):
        return {}

    def fake_run_arm(name, *, root, recipe, acceptance, **kw):
        seen["wired_during_run"] = dest.is_file()
        return {"arm": name, "state": "valid"}

    monkeypatch.setattr(arms, "compile_keys_to_suite", fake_compile)
    monkeypatch.setattr(arms, "replay_check", fake_replay)
    monkeypatch.setattr(arms, "run_arm", fake_run_arm)
    monkeypatch.setattr(arms, "_snapshot_main", lambda r: {})
    def fake_suite_dir(r):
        d = Path(tmp_path / "suite")
        d.mkdir(exist_ok=True)
        return d

    monkeypatch.setattr(arms, "create_suite_dir", fake_suite_dir)
    out = arms.run_acceptance_flow(
        root=root, recipe={"acceptance_face": {"features": [{"key": "k1"}]}},
        acceptance_keys=["k1"], acceptance_shards=1,
        arms_parallel=False, acceptance_adaptive=False, arm_kwargs={})
    assert out["state"] == "valid"
    assert seen["wired_during_compile"] is True
    assert seen["wired_during_run"] is True
    assert not dest.exists(), "contract must be removed after the arm"


def test_acceptance_flow_cleans_contract_on_suite_error(tmp_path, monkeypatch):
    """行为级：编译异常分支也不得残留契约位（finally 语义）。"""
    from godot_qa_toolkit.gherkin.suite import SuiteError

    root = _proj(tmp_path)
    dest = root / "addons" / "gqt_accept" / "harness.gd"

    def boom(project_root, keys, suite_dir):
        raise SuiteError("injected")

    monkeypatch.setattr(arms, "compile_keys_to_suite", boom)
    monkeypatch.setattr(arms, "_snapshot_main", lambda r: {})
    def fake_suite_dir(r):
        d = Path(tmp_path / "suite")
        d.mkdir(exist_ok=True)
        return d

    monkeypatch.setattr(arms, "create_suite_dir", fake_suite_dir)
    out = arms.run_acceptance_flow(
        root=root, recipe={"acceptance_face": {"features": [{"key": "k1"}]}},
        acceptance_keys=["k1"], acceptance_shards=1,
        arms_parallel=False, acceptance_adaptive=False, arm_kwargs={})
    assert out["state"] == "invalid"
    assert not dest.exists(), "contract must be removed on suite_error"


def test_acceptance_flow_uses_shared_suite_groups(tmp_path, monkeypatch):
    """§SPEC-005（SEE-1619 C2）：恢复 acceptance_shards 共享分片——44
    feature 已过 acceptance-feature-state-isolation 原子化门（SEE-1619
    C1/C1-R2），同进程共享 suite 不再有跨 feature 残留。shards=共享组数
    （并发上限同步收敛到组数），扇出=len(groups)×reps。"""
    root = _proj(tmp_path)
    compiles: list[tuple[list, Path]] = []
    concurrency_seen: dict = {}

    def fake_compile(project_root, keys, suite_dir):
        compiles.append((list(keys), Path(suite_dir)))

    def fake_replay(project_root, key, *, suite_dir=None):
        return {}

    def fake_run_arm(name, *, root, recipe, acceptance, **kw):
        concurrency_seen["value"] = kw.get("concurrency")
        concurrency_seen["groups"] = acceptance["groups"]

        class FakeArm:
            pass
        return {"arm": name, "state": "valid", "gut_ok": True,
                "arm_fail_pairs": set(), "arm_failures": set(),
                "new_pairs": set(),
                "test_failures": {"observed": [], "observed_pairs": [],
                                  "new": [], "new_pairs": [],
                                  "instrumentation_intolerant": []},
                "reconciliation": {"ok": True, "shards": []},
                "wall_s": 0.0}

    def fake_suite_dir(r):
        d = Path(tmp_path) / f"suite{len(compiles)}"
        d.mkdir(exist_ok=True)
        return d

    monkeypatch.setattr(arms, "compile_keys_to_suite", fake_compile)
    monkeypatch.setattr(arms, "replay_check", fake_replay)
    monkeypatch.setattr(arms, "run_arm", fake_run_arm)
    monkeypatch.setattr(arms, "_snapshot_main", lambda r: {})
    monkeypatch.setattr(arms, "create_suite_dir", fake_suite_dir)
    out = arms.run_acceptance_flow(
        root=root,
        recipe={"acceptance_face": {"features": [
            {"key": "k1"}, {"key": "k2"}, {"key": "k3"}]}},
        acceptance_keys=None, acceptance_shards=2,
        arms_parallel=False, acceptance_adaptive=False, arm_kwargs={})
    assert out["state"] == "valid"
    # 共享分片：2 组（k1,k3 / k2），共享组 keys>1，suite 目录数=组数
    assert [keys for keys, _ in compiles] == [["k1", "k3"], ["k2"]]
    assert len({str(sd) for _, sd in compiles}) == 2
    assert all(len(keys) > 1 for keys, _ in compiles
               if len(keys) == max(len(k) for k, _ in compiles))
    assert concurrency_seen["value"] == 2
    assert len(concurrency_seen["groups"]) == 2
    # 全原子：process_fallbacks 为空数组
    assert out.get("process_fallbacks") == []


def _wire_flow_fakes(monkeypatch, tmp_path, root, dest, state="valid"):
    """成功路径共用的 fake 装配。"""
    counter = {"n": 0}

    def fake_compile(project_root, keys, suite_dir):
        counter["n"] += 1

    def fake_replay(project_root, key, *, suite_dir=None):
        return {}

    def fake_suite_dir(r):
        d = Path(tmp_path) / f"s{counter['n']}"
        d.mkdir(exist_ok=True)
        counter["n"] += 1
        return d

    monkeypatch.setattr(arms, "compile_keys_to_suite", fake_compile)
    monkeypatch.setattr(arms, "replay_check", fake_replay)
    monkeypatch.setattr(arms, "run_arm",
                        lambda name, **kw: {"arm": name, "state": state})
    monkeypatch.setattr(arms, "_snapshot_main", lambda r: {})
    monkeypatch.setattr(arms, "create_suite_dir", fake_suite_dir)
    return dest


def test_cleanup_failure_downgrades_result_to_invalid(tmp_path, monkeypatch):
    """D21：unlink 失败不得静默——本次 acceptance 结果必须 invalid 并
    披露残留（不得带残留报 valid）。"""
    root = _proj(tmp_path)
    dest = root / "addons" / "gqt_accept" / "harness.gd"
    _wire_flow_fakes(monkeypatch, tmp_path, root, dest)
    real_unlink = Path.unlink

    def flaky_unlink(self, *a, **k):
        if self == dest:
            raise OSError("injected unlink failure")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    out = arms.run_acceptance_flow(
        root=root, recipe={"acceptance_face": {"features": [{"key": "k1"}]}},
        acceptance_keys=["k1"], acceptance_shards=1,
        arms_parallel=False, acceptance_adaptive=False, arm_kwargs={})
    assert out["state"] == "invalid", "residue must not report valid"
    assert "residue" in (out.get("invalid_reason") or "")


def test_copy_failure_yields_invalid_and_no_partial_residue(tmp_path,
                                                           monkeypatch):
    """D21：mkdir/copyfile 中途失败不得裸抛——结果 invalid 且本次部分
    产物被回收（树内无残留）。"""
    root = _proj(tmp_path)
    dest = root / "addons" / "gqt_accept" / "harness.gd"
    _wire_flow_fakes(monkeypatch, tmp_path, root, dest)

    def boom_copy(src, dst, *a, **k):
        raise OSError("injected copy failure")

    monkeypatch.setattr(arms.shutil, "copyfile", boom_copy)
    out = arms.run_acceptance_flow(
        root=root, recipe={"acceptance_face": {"features": [{"key": "k1"}]}},
        acceptance_keys=["k1"], acceptance_shards=1,
        arms_parallel=False, acceptance_adaptive=False, arm_kwargs={})
    assert out["state"] == "invalid"
    assert not dest.exists(), "partial copy must be reclaimed"
    assert not (root / "addons" / "gqt_accept").exists()


def test_run_arm_exception_propagates_without_residue(tmp_path, monkeypatch):
    """D22：run_arm 抛未列名异常时——错误传播 + 本次临时接线零残留
    （修复前 run_arm 在清理边界外，异常路径必留残留）。"""
    import pytest as _pytest

    root = _proj(tmp_path)
    dest = root / "addons" / "gqt_accept" / "harness.gd"

    def fake_run_arm(name, **kw):
        raise RuntimeError("injected from run_arm")

    suite_root = Path(tmp_path) / "acceptance" / "generated"
    suite_dir = suite_root / ".gqt-run-deadbeef1234"

    def fake_suite_dir(r):
        suite_dir.mkdir(parents=True, exist_ok=True)
        return suite_dir

    _wire_flow_fakes(monkeypatch, tmp_path, root, dest)
    monkeypatch.setattr(arms, "create_suite_dir", fake_suite_dir)
    monkeypatch.setattr(arms, "run_arm", fake_run_arm)
    with _pytest.raises(RuntimeError):
        arms.run_acceptance_flow(
            root=root,
            recipe={"acceptance_face": {"features": [{"key": "k1"}]}},
            acceptance_keys=["k1"], acceptance_shards=1,
            arms_parallel=False, acceptance_adaptive=False, arm_kwargs={})
    assert not dest.exists(), "unexpected-exception path must reclaim wiring"
    assert not (root / "addons" / "gqt_accept").exists()
    # D23：suite 目录与 harness 同属本次新建产物——异常路径一并回收
    assert not suite_dir.exists(), "suite dir residue on run_arm exception"
