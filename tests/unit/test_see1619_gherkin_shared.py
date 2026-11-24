"""SEE-1619 §SPEC-005 共享分片幸存语义 + SEE-1631 §SPEC-017 退役映射。

历史注记（退役映射，Owner 2026-11-24 裁决）：本文件原为 SEE-1619 临时
具名降级（共享红 → 带归属插桩单例隔离重跑 → process_fallbacks 披露）的
反例组。§SPEC-017 退役该重跑——"是否同进程污染"的检查随 feature 全量
原子化失去意义；红 feature 改走未插桩对照（对照绿=疑似插桩不耐受车道、
对照红=既有失败吸收、取不到证据=臂保持 invalid）。原降级语义测试的映射：
- 全原子 process_fallbacks=[] → 保留（键恒空的契约仍在）；
- 隔离绿具名降级 / 双红不包装 / 并发上限（降级轮） / 合并序列化 /
  合并 remaining 判定 → 随降级轮退役，由 §SPEC-017 对照路由测试承接
  （对照路由的全部结果分类与状态重算在 test_c1r1_see1631.py）；
- 对账失败分组整组降级 → 对账失败保持臂 invalid（无降级轮可比）；
- 清理失败残留 → 保留（错误路径仍存在）。
"""

from __future__ import annotations

from pathlib import Path

from godot_qa_toolkit.measurement import arms


def _proj(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "addons" / "gut").mkdir(parents=True)
    (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
    (root / "addons" / "gqt_gherkin").mkdir(parents=True)
    (root / "addons" / "gqt_gherkin" / "harness.gd").write_text(
        "extends RefCounted\n")
    return root


def _script(key: str) -> str:
    return (f"res://gherkin/generated/.gqt-run-x/generated/"
            f"test_{key}.gd")


def _make_fake_run_arm(shared_red: set[str], comparison: str = "green"):
    """单轮（共享）run_arm 桩：shared_red 里的 key 产出红对；对照腿由
    调用方另行 stub `_gherkin_uninstrumented_comparison`。comparison 决定
    桩注入的对照判定（green/red/no_evidence）。"""
    calls: list[dict] = []

    def fake(name: str, *, gherkin=None, **kw):
        calls.append({"groups": gherkin["groups"],
                      "concurrency": kw.get("concurrency")})
        pairs: list[tuple[str, str]] = []
        for g in gherkin["groups"]:
            for k in g["keys"]:
                if k in shared_red:
                    pairs.append((_script(k), "test_bad"))
        ok_pairs = sorted(f"{s}::{t}" for s, t in pairs)
        rec_shards = [{"ok": True, "keys": list(g["keys"])}
                      for g in gherkin["groups"]]
        return {"arm": name,
                "state": "invalid" if pairs else "valid",
                "invalid_reason": "new_failures" if pairs else None,
                "gut_ok": not pairs,
                "wall_s": 1.0, "run_uuid": "uuid-0",
                "arm_fail_pairs": set(pairs),
                "arm_failures": {t for _, t in pairs},
                "new_pairs": set(pairs),
                "test_failures": {
                    "observed": sorted({t for _, t in pairs}),
                    "observed_pairs": ok_pairs,
                    "new": sorted({t for _, t in pairs}),
                    "new_pairs": ok_pairs,
                    "instrumentation_intolerant": []},
                "reconciliation": {"ok": True, "shards": rec_shards},
                "bundles_data": {}}

    return fake, calls


def _wire(monkeypatch, tmp_path: Path, fake_run_arm) -> None:
    counter = {"n": 0}

    def fake_compile(project_root, keys, suite_dir):
        # SEE-1631 sync：_compile_into 读 manifests 捕获身份
        return {"keys": list(keys), "suite_dir": str(suite_dir),
                "manifests": {k: {"attribution": []} for k in keys}}

    def fake_replay(project_root, key, *, suite_dir=None):
        return {}

    def fake_suite_dir(r):
        counter["n"] += 1
        d = (Path(r) / "gherkin" / "generated" /
             f".gqt-run-{counter['n']}")
        d.mkdir(parents=True, exist_ok=True)
        # 所有权标记内容 = 目录名（绑定身份契约）
        (d / ".gqt-owner").write_text(d.name + "\n", encoding="utf-8")
        return d

    monkeypatch.setattr(arms, "compile_keys_to_suite", fake_compile)
    monkeypatch.setattr(arms, "replay_check", fake_replay)
    monkeypatch.setattr(arms, "run_arm", fake_run_arm)
    monkeypatch.setattr(arms, "_snapshot_main", lambda r: {})
    monkeypatch.setattr(arms, "create_suite_dir", fake_suite_dir)
    # SEE-1624：flow 入口预检不在本组测试语义内（stub 两阶段：夹具无
    # addon schema、且不得拉真 Godot）；预检语义有专项测试覆盖。
    monkeypatch.setattr(arms, "compile_preflight",
                        lambda root, **k: {"ok": True})
    monkeypatch.setattr(arms, "run_validator_preflight",
                        lambda root, **k: {"ok": True})


KEYS = ["k1", "k2", "k3", "k4"]


def _flow(root: Path, shards: int = 2) -> dict:
    return arms.run_gherkin_flow(
        root=root,
        recipe={"gherkin_face": {"features": [
            {"key": k} for k in KEYS]}},
        gherkin_keys=None, gherkin_shards=shards,
        arms_parallel=False, gherkin_adaptive=False, arm_kwargs={})


# ---------- 共享分片幸存语义 ----------

def test_all_atomic_single_shared_round_empty_fallbacks(
        tmp_path, monkeypatch):
    root = _proj(tmp_path)
    fake, calls = _make_fake_run_arm(set())
    _wire(monkeypatch, tmp_path, fake)
    out = _flow(root, shards=2)
    assert out["process_fallbacks"] == []
    assert out["state"] == "valid" and out["gut_ok"] is True
    # 单轮共享：仅一次 run_arm（对照腿仅在红时发生）
    assert len(calls) == 1
    # 共享组 keys>1：2 组（k1,k3 / k2,k4）
    assert [sorted(g["keys"]) for g in calls[0]["groups"]] == [
        ["k1", "k3"], ["k2", "k4"]]


def test_fanout_groups_equal_shards(tmp_path, monkeypatch):
    root = _proj(tmp_path)
    fake, calls = _make_fake_run_arm(set())
    _wire(monkeypatch, tmp_path, fake)
    _flow(root, shards=4)
    assert len(calls[0]["groups"]) == 4
    assert calls[0]["concurrency"] == 4


def test_flow_never_touches_harness_contract_position(
        tmp_path, monkeypatch):
    """cp 接线退役：flow 全程不得创建/删除 harness 契约位——任何对契约位
    的 unlink 即旧接线回潮，本钉变红。"""
    root = _proj(tmp_path)
    fake, _calls = _make_fake_run_arm(set())
    dest = root / "addons" / "gqt_gherkin" / "harness.gd"
    before = dest.read_bytes()
    _wire(monkeypatch, tmp_path, fake)
    real_unlink = Path.unlink
    unlink_targets: list[Path] = []

    def spy_unlink(self, *a, **k):
        unlink_targets.append(self)
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", spy_unlink)
    out = _flow(root, shards=2)
    assert out["state"] == "valid"
    assert dest.read_bytes() == before
    assert dest not in unlink_targets
    assert out.get("process_fallbacks") == []


def test_early_failure_path_carries_process_fallbacks(tmp_path, monkeypatch):
    """早失败路径（suite_error）返回字典同样带 process_fallbacks 键（空
    数组）——成功与 invalid 形状一致，消费方不可能在错误路径 KeyError。"""
    root = _proj(tmp_path)

    def boom(project_root, keys, suite_dir):
        from godot_qa_toolkit.gherkin.suite import SuiteError
        raise SuiteError("injected compile failure")

    counter = {"n": 0}

    def fake_replay(project_root, key, *, suite_dir=None):
        return {}

    def fake_suite_dir(r):
        counter["n"] += 1
        d = (Path(r) / "gherkin" / "generated" /
             f".gqt-run-{counter['n']}")
        d.mkdir(parents=True, exist_ok=True)
        (d / ".gqt-owner").write_text(d.name + "\n", encoding="utf-8")
        return d

    monkeypatch.setattr(arms, "compile_keys_to_suite", boom)
    monkeypatch.setattr(arms, "replay_check", fake_replay)
    monkeypatch.setattr(arms, "_snapshot_main", lambda r: {})
    monkeypatch.setattr(arms, "create_suite_dir", fake_suite_dir)
    monkeypatch.setattr(arms, "compile_preflight",
                        lambda root, **k: {"ok": True})
    monkeypatch.setattr(arms, "run_validator_preflight",
                        lambda root, **k: {"ok": True})
    out = arms.run_gherkin_flow(
        root=root,
        recipe={"gherkin_face": {"features": [
            {"key": k} for k in KEYS]}},
        gherkin_keys=None, gherkin_shards=2,
        arms_parallel=False, gherkin_adaptive=False, arm_kwargs={})
    assert out["state"] == "invalid"
    assert "process_fallbacks" in out, "早失败路径必须带该键"
    assert out["process_fallbacks"] == []


def test_error_path_suite_cleanup_failure_not_valid(tmp_path, monkeypatch):
    """错误路径 suite 清理失败 = 残留 = 具名 suite_residue，不得静默。"""
    root = _proj(tmp_path)

    def boom(project_root, keys, suite_dir):
        from godot_qa_toolkit.gherkin.suite import SuiteError
        raise SuiteError("injected compile failure")

    counter = {"n": 0}

    def fake_replay(project_root, key, *, suite_dir=None):
        return {}

    def fake_suite_dir(r):
        counter["n"] += 1
        d = (Path(r) / "gherkin" / "generated" /
             f".gqt-run-{counter['n']}")
        d.mkdir(parents=True, exist_ok=True)
        (d / ".gqt-owner").write_text(d.name + "\n", encoding="utf-8")
        return d

    real_cleanup = arms._cleanup_suite_dir

    def flaky_cleanup(sd, project_root):
        if ".gqt-run-1" in str(sd):
            raise OSError("injected cleanup failure")
        return real_cleanup(sd, project_root)

    monkeypatch.setattr(arms, "compile_keys_to_suite", boom)
    monkeypatch.setattr(arms, "replay_check", fake_replay)
    monkeypatch.setattr(arms, "_snapshot_main", lambda r: {})
    monkeypatch.setattr(arms, "create_suite_dir", fake_suite_dir)
    monkeypatch.setattr(arms, "compile_preflight",
                        lambda root, **k: {"ok": True})
    monkeypatch.setattr(arms, "run_validator_preflight",
                        lambda root, **k: {"ok": True})
    monkeypatch.setattr(arms, "_cleanup_suite_dir", flaky_cleanup)
    out = arms.run_gherkin_flow(
        root=root,
        recipe={"gherkin_face": {"features": [
            {"key": k} for k in KEYS]}},
        gherkin_keys=None, gherkin_shards=2,
        arms_parallel=False, gherkin_adaptive=False, arm_kwargs={})
    assert out["state"] == "invalid"
    assert "suite_residue" in str(out.get("invalid_reason"))
    assert out["process_fallbacks"] == []


# ---------- §SPEC-017 对照腿（插桩单例重跑退役后的红处理） ----------

def test_shared_red_comparison_green_arm_valid_no_second_round(
        tmp_path, monkeypatch):
    """§SPEC-017：共享轮红 → 对照绿（疑似插桩不耐受）→ 臂保持 valid、
    无第二轮 run_arm（插桩单例重跑退役）、对账未执行如实标注。"""
    root = _proj(tmp_path)
    fake, calls = _make_fake_run_arm({"k2"}, comparison="green")
    _wire(monkeypatch, tmp_path, fake)
    comparisons: list[tuple[list[str], int]] = []
    orig_cmp = arms._gherkin_uninstrumented_comparison

    def spy_cmp(root_p, failed, timeout_s):
        comparisons.append((list(failed), timeout_s))
        return {"green": set(failed), "red": set(), "no_evidence": set(),
                "executions": [{"key": k, "rc": 0} for k in failed]}

    monkeypatch.setattr(arms, "_gherkin_uninstrumented_comparison", spy_cmp)
    out = _flow(root, shards=2)
    assert len(calls) == 1, "不得再有第二轮（插桩单例重跑已退役）"
    assert comparisons and sorted(comparisons[0][0]) == ["k2"]
    assert out["state"] == "valid"
    assert out["process_fallbacks"] == []
    rec = out.get("reconciliation") or {}
    assert rec.get("executed") is False and rec.get("ok") is None
    tf = out["test_failures"]
    assert tf["instrumentation_intolerant"] and tf["new"] == []
    assert out["gherkin_comparison"]["green"] == ["k2"]


def test_shared_red_comparison_red_absorbed(tmp_path, monkeypatch):
    """§SPEC-017：对照红 = 既有失败（具名吸收），臂保持 valid。"""
    root = _proj(tmp_path)
    fake, _calls = _make_fake_run_arm({"k2"}, comparison="red")
    _wire(monkeypatch, tmp_path, fake)
    monkeypatch.setattr(
        arms, "_gherkin_uninstrumented_comparison",
        lambda root_p, failed, timeout_s: {
            "green": set(), "red": set(failed), "no_evidence": set(),
            "executions": [{"key": k, "rc": 1} for k in failed]})
    out = _flow(root, shards=2)
    assert out["state"] == "valid"
    tf = out["test_failures"]
    assert tf["pre_existing_failures"] and tf["new"] == []


def test_shared_red_comparison_no_evidence_stays_invalid(
        tmp_path, monkeypatch):
    """§SPEC-017：对照取不到证据 → 臂保持 invalid（不静默转绿）。"""
    root = _proj(tmp_path)
    fake, _calls = _make_fake_run_arm({"k2"}, comparison="no_evidence")
    _wire(monkeypatch, tmp_path, fake)
    monkeypatch.setattr(
        arms, "_gherkin_uninstrumented_comparison",
        lambda root_p, failed, timeout_s: {
            "green": set(), "red": set(), "no_evidence": set(failed),
            "executions": [{"key": k, "rc": 3} for k in failed]})
    out = _flow(root, shards=2)
    assert out["state"] == "invalid"
    assert out["invalid_reason"] == "new_failures"
    assert out["process_fallbacks"] == []


def test_comparison_json_serializable(tmp_path, monkeypatch):
    """对照路由后的臂文档必须 JSON 可序列化（C3-R2 教训映射：路由不得
    把 set 形态留在 test_failures/顶层）。"""
    import json as _json
    root = _proj(tmp_path)
    fake, _calls = _make_fake_run_arm({"k2"}, comparison="green")
    _wire(monkeypatch, tmp_path, fake)
    monkeypatch.setattr(
        arms, "_gherkin_uninstrumented_comparison",
        lambda root_p, failed, timeout_s: {
            "green": set(failed), "red": set(), "no_evidence": set(),
            "executions": [{"key": k, "rc": 0} for k in failed]})
    out = _flow(root, shards=2)
    dumped = _json.dumps(out, ensure_ascii=False, indent=1)
    assert "instrumentation_intolerant" in dumped
    for key in ("arm_fail_pairs", "arm_failures", "new_pairs"):
        assert not isinstance(out.get(key), (set, frozenset)), key
    tf = out["test_failures"]
    for key in ("new_pairs", "observed_pairs"):
        assert not isinstance(tf.get(key), (set, frozenset)), key
