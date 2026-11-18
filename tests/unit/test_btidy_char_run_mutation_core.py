"""BTIDY.1 特征化测试：`mutation.runner._run_mutation_core` 关键 seam。

钉住：(1) setup 解析后的 identity 必须透传到 plan_execution（历史回归点：
误传原始 None 形参）；(2) dry_run 早退返回估算；(3) 零位点早退返回
no_sites；(4) 零执行零复用返回 no_sites。拆分后断言不变。
"""

from __future__ import annotations

from pathlib import Path

from godot_qa_toolkit.mutation import runner as core

PACK = {"inject_scheme": "disk-farm/1", "exec_scheme": "cold-shadow/1"}


def _ctx(tmp_path: Path, *, mutants: int = 1) -> dict:
    target = tmp_path / "x.gd"
    target.write_text("extends Node\n")
    return {
        "path": target, "original_src": "extends Node\n",
        "tests_glob": "res://tests/", "selection_meta": {},
        "identity": dict(PACK), "mutants": [object()] * mutants,
        "header_hash": "h", "scope_hashes": {}, "total_sites": mutants,
        "attention": None, "kind": "linux",
    }


def _wire(monkeypatch, tmp_path, *, mutants=1):
    ctx = _ctx(tmp_path, mutants=mutants)
    monkeypatch.setattr(core, "_core_setup", lambda *a, **k: (None, ctx))
    monkeypatch.setattr(
        core, "_core_caliper",
        lambda project_root, path, **k: (k["selection_meta"], k["tests_glob"]))
    return ctx


def test_core_passes_resolved_identity_to_plan(tmp_path, monkeypatch):
    _wire(monkeypatch, tmp_path)
    captured = {}

    def fake_plan(*a, **k):
        captured.update(k)
        return ({}, [], {}, [], [], [])  # 零执行零复用 → no_sites

    monkeypatch.setattr(core, "plan_execution", fake_plan)
    r = core._run_mutation_core(str(tmp_path / "x.gd"), str(tmp_path),
                                identity=None)
    # 历史回归点：plan 收到的是 setup 解析后的 pack，而非原始 None
    assert captured["identity"] == PACK
    assert r["summary"]["mutants"] == 0


def test_core_dry_run_early_return(tmp_path, monkeypatch):
    _wire(monkeypatch, tmp_path)
    sentinel = {"tool": "mutation", "ok": True,
                "summary": {"dry": True}, "failures": []}
    monkeypatch.setattr(core, "_core_dry_run", lambda *a, **k: sentinel)
    r = core._run_mutation_core(str(tmp_path / "x.gd"), str(tmp_path),
                                dry_run=True, identity=None)
    assert r is sentinel


def test_core_zero_mutants_early_return(tmp_path, monkeypatch):
    _wire(monkeypatch, tmp_path, mutants=0)
    r = core._run_mutation_core(str(tmp_path / "x.gd"), str(tmp_path),
                                identity=None)
    assert r["ok"] is True and r["summary"]["mutants"] == 0
