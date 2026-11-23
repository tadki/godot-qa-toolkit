"""SEE-1623 M7 — mutation 异常形态信封契约行为钉。

根因（SEE-1622 Bachi BUG-13 + Harti 障碍6）：plan_only/prepare_required/
plan_blocked 等异常形态顶层缺 `failures[]`、多出顶层 `plan`/`error` 键
——README/contract 承诺 stdout 恒为 `{tool, ok, summary, failures}`，
自动化按契约解析会漏判。

行为契约（三形态归一）：plan 只在 summary；顶层键集恰为
`{tool, ok, summary, failures}`；failures 恒在场（ok=true 空 list，
error 级问题逐条 reason）。
"""

from __future__ import annotations

import argparse

import godot_qa_toolkit.cli as cli
from godot_qa_toolkit.mutation import executor as executor_mod


def _capture(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(cli, "_emit",
                        lambda result: captured.update(result) or 1)
    return captured


class TestEnvelopeForms:
    def test_prepare_required_flat_envelope(self, tmp_path, monkeypatch):
        captured = _capture(monkeypatch)
        target = tmp_path / "calc.gd"
        target.write_text("extends RefCounted\n")
        args = argparse.Namespace(
            files=[str(target)], project_root=str(tmp_path), dry_run=False,
            auto_prepare=False, select_mode="line", tests=None,
            attribution_map=None)
        cli._p1_prepare_gate(args, {"problems": [
            {"severity": "warning", "code": "attribution_map_missing_or_stale",
             "level": "P1", "evidence": {}, "fix": "x",
             "auto_prepare_covered": True}]})
        assert set(captured) == {"tool", "ok", "summary", "failures"}
        assert captured["failures"]
        assert "plan" not in captured
        assert captured["summary"]["plan"]["problems"]

    def test_plan_blocked_flat_envelope(self, tmp_path, monkeypatch):
        captured = _capture(monkeypatch)
        args = argparse.Namespace(
            project_root=str(tmp_path),
            files=[str(tmp_path / "calc.gd")],
            tests="res://tests/nope/",
            select_mode="line", dry_run=False, attribution_map=None,
            auto_prepare=False)
        cli._plan_gate_mutation(args)
        assert set(captured) == {"tool", "ok", "summary", "failures"}
        assert captured["ok"] is False
        assert "plan" not in captured
        assert captured["summary"]["error"] == "plan_blocked"
        assert any("tests_glob_target_missing" in f["reason"]
                   for f in captured["failures"])

    def test_dry_run_plan_only_flat_envelope(self, tmp_path, monkeypatch):
        captured = _capture(monkeypatch)
        root = tmp_path / "proj"
        root.mkdir()
        (root / "project.godot").write_text(
            'config_version=5\n[application]\nconfig/name="m7"\n')
        (root / "calc.gd").write_text(
            "extends RefCounted\n\nfunc add(a, b):\n\treturn a + b\n")
        args = argparse.Namespace(
            project_root=str(root), files=[str(root / "calc.gd")],
            tests=None, select_mode="line", dry_run=True,
            attribution_map=None, auto_prepare=False, timeout=600)
        cli._plan_gate_mutation(args)
        assert set(captured) == {"tool", "ok", "summary", "failures"}
        assert captured["ok"] is True
        assert captured["failures"] == []
        assert "plan" not in captured
        assert captured["summary"]["selection_mode"] == "plan_only"


class TestExecutorPlanBlockedEnvelope:
    def test_multi_file_plan_blocked_flat(self, tmp_path, monkeypatch):
        """executor 多文件 plan_blocked：plan 只在 summary，failures 在场
        （旧顶层 plan/error 键撤销）。"""
        from godot_qa_toolkit.mutation.plancheck import static_plan

        root = tmp_path / "proj"
        root.mkdir()
        (root / "project.godot").write_text(
            'config_version=5\n[application]\nconfig/name="m7"\n')
        (root / "a.gd").write_text("extends RefCounted\n")
        (root / "b.gd").write_text("extends RefCounted\n")
        r = executor_mod.run_mutation_files(
            [str(root / "a.gd"), str(root / "b.gd")], str(root),
            tests_glob="res://tests/nope/", select_mode="full")
        assert r["ok"] is False
        assert "plan" not in r
        assert "error" not in r
        assert set(r) >= {"tool", "ok", "summary", "failures", "results"}
        assert r["summary"]["error"] == "plan_blocked"
        assert r["failures"]
        assert all(f["summary"]["error"] == "plan_blocked"
                   for f in r["results"])
