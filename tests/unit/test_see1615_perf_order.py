"""SEE-1615 §SPEC-PERF1 — 历史杀手排序与 kill-bail chunk C 配置契约。

判据：击杀数降序、同分/无历史保持确定性原序（排序仅置换不改集合）；
历史来自 resume v2 store 的 killed_by 证书聚合（rel 过滤、损坏降级）；
排序开关与 C 均由 env 单点解析；C 注入宿主 cfg/req。
"""

from __future__ import annotations

import json

from godot_qa_toolkit.mutation import killbail


# ---------------------------------------------------- order_scripts_by_history


def test_history_orders_killers_first():
    scripts = ["a.gd", "b.gd", "c.gd", "d.gd"]
    ordered = killbail.order_scripts_by_history(
        scripts, {"c.gd": 5, "b.gd": 2})
    assert ordered == ["c.gd", "b.gd", "a.gd", "d.gd"]


def test_ties_and_missing_history_keep_original_order():
    scripts = ["z.gd", "m.gd", "a.gd"]
    # 全部无历史 → 原序（不是字典序，确定性由调用方清单保证）
    assert killbail.order_scripts_by_history(scripts, {}) == scripts
    # 同分 → 保持各自原序
    ordered = killbail.order_scripts_by_history(
        ["x.gd", "y.gd", "k.gd"], {"x.gd": 1, "y.gd": 1})
    assert ordered == ["x.gd", "y.gd", "k.gd"]


def test_ordering_permutes_without_changing_members():
    scripts = ["s1.gd", "s2.gd", "s3.gd", "s4.gd", "s5.gd"]
    ordered = killbail.order_scripts_by_history(
        scripts, {"s4.gd": 3, "s1.gd": 1})
    assert sorted(ordered) == sorted(scripts)


# ------------------------------------------------------------- kill_history


def _store(tmp_path, rel: str, entries: dict[str, dict]) -> None:
    import hashlib
    key = hashlib.sha1(f"{rel}|glob|ops|linux|id".encode()).hexdigest()
    d = tmp_path / ".qa-cache" / "gqt-mutation-resume-v2" / key
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({
        "schema": "see1420-resume/2", "file_key": key, "rel": rel,
        "tests_glob": "glob"}), encoding="utf-8")
    for sid, entry in entries.items():
        (d / f"{sid}.json").write_text(json.dumps(entry), encoding="utf-8")


def test_kill_history_aggregates_certificate_scripts(tmp_path):
    rel = "src_game/calc.gd"
    target = tmp_path / rel
    target.parent.mkdir()
    target.write_text("extends RefCounted\n")
    _store(tmp_path, rel, {
        "m1": {"id": "m1", "verdict": "killed", "killed_by": [
            {"test_name": "t_hot", "script_path": "res://tests/t_hot.gd"}]},
        "m2": {"id": "m2", "verdict": "killed", "killed_by": [
            {"test_name": "t_hot", "script_path": "res://tests/t_hot.gd"}]},
        "m3": {"id": "m3", "verdict": "killed", "killed_by": [
            {"test_name": "t_a", "script_path": "res://tests/t_a.gd"}]},
        "m4": {"id": "m4", "verdict": "survived"}})
    hist = killbail.kill_history(str(tmp_path), str(target))
    assert hist["res://tests/t_hot.gd"] == 2
    assert hist["res://tests/t_a.gd"] == 1


def test_kill_history_ignores_other_targets_and_corruption(tmp_path):
    rel = "src_game/calc.gd"
    target = tmp_path / rel
    target.parent.mkdir()
    target.write_text("extends RefCounted\n")
    _store(tmp_path, "other/file.gd", {
        "m1": {"id": "m1", "verdict": "killed", "killed_by": [
            {"test_name": "t", "script_path": "res://tests/t.gd"}]}})
    _store(tmp_path, rel, {"bad": "not-a-dict"})
    assert killbail.kill_history(str(tmp_path), str(target)) == {}


def test_kill_history_no_store_is_empty(tmp_path):
    target = tmp_path / "calc.gd"
    target.write_text("extends RefCounted\n")
    assert killbail.kill_history(str(tmp_path), str(target)) == {}


# ---------------------------------------------------- env 解析（开关与 C）


def test_resolve_test_order_env(monkeypatch):
    # 默认 OFF：PERF1 实测（A/A≥3 + 交错 A/B≥3）收益不超噪声带——保留
    # 现状原序；机制经 env opt-in（GQT_MUTATION_TEST_ORDER=history）。
    monkeypatch.delenv(killbail.ENV_TEST_ORDER, raising=False)
    assert killbail.resolve_test_order() is False
    monkeypatch.setenv(killbail.ENV_TEST_ORDER, "history")
    assert killbail.resolve_test_order() is True
    for v in ("off", "0", "false", "no"):
        monkeypatch.setenv(killbail.ENV_TEST_ORDER, v)
        assert killbail.resolve_test_order() is False


def test_resolve_bail_chunk_env(monkeypatch):
    monkeypatch.delenv(killbail.ENV_BAIL_CHUNK, raising=False)
    assert killbail.resolve_bail_chunk() == killbail.DEFAULT_BAIL_CHUNK
    monkeypatch.setenv(killbail.ENV_BAIL_CHUNK, "8")
    assert killbail.resolve_bail_chunk() == 8
    for bad in ("0", "-3", "abc", ""):
        monkeypatch.setenv(killbail.ENV_BAIL_CHUNK, bad)
        assert killbail.resolve_bail_chunk() == killbail.DEFAULT_BAIL_CHUNK


def test_ordered_bail_scripts_env_off_is_passthrough(monkeypatch):
    """env 关断（默认，实测落定）：_ordered_bail_scripts 原样透传，
    不触发排序与历史读——排序机制只在 opt-in 时生效。"""
    from godot_qa_toolkit.mutation import runner as runner_mod

    monkeypatch.delenv(killbail.ENV_TEST_ORDER, raising=False)

    def _boom(*a, **k):
        raise AssertionError("ordering must not run when env off")

    monkeypatch.setattr(killbail, "order_scripts_by_history", _boom)
    monkeypatch.setattr(killbail, "kill_history", _boom)
    scripts = ["b.gd", "a.gd"]
    assert runner_mod._ordered_bail_scripts("/p", "t.gd", scripts) == scripts


def test_ordered_bail_scripts_env_on_orders_by_history(monkeypatch):
    """env opt-in：按历史击杀数降序（杀手优先，仅置换清单）。"""
    from godot_qa_toolkit.mutation import runner as runner_mod

    monkeypatch.setenv(killbail.ENV_TEST_ORDER, "history")
    monkeypatch.setattr(killbail, "kill_history",
                        lambda root, path: {"a.gd": 3})
    assert runner_mod._ordered_bail_scripts(
        "/p", "t.gd", ["b.gd", "a.gd"]) == ["a.gd", "b.gd"]


def test_write_direct_cfg_carries_bail_chunk(tmp_path, monkeypatch):
    import godot_qa_toolkit.mutation.directdrive as dd

    monkeypatch.setenv(killbail.ENV_BAIL_CHUNK, "16")
    p = dd.write_direct_cfg(str(tmp_path), "res://x.gd", "sha", "Auto",
                            ["res://tests/a.gd"], [])
    assert json.loads(p.read_text(encoding="utf-8"))["bail_chunk"] == 16


def test_worker_host_declares_chunk_param_contract():
    """宿主 GDScript 顺序执行原语必须接 chunk 形参（两宿主同源文本）。"""
    from godot_qa_toolkit.mutation import directdrive, worker_session

    def _norm(text: str) -> str:
        return " ".join(text.split())

    assert ("func _gqt_run_scripts_sequential(gut: Node, scripts: Array, "
            "baseline_failures: Array, per_script_timeout_ms: int, "
            "chunk: int) -> Dictionary:") in _norm(directdrive.SEQUENTIAL_RUNNER_GD)
    assert directdrive.SEQUENTIAL_RUNNER_GD in worker_session.WORKER_HOST_GD
