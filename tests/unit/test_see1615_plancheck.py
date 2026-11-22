"""SEE-1615 §SPEC-PLANFIRST — plan 三段式第一段（静态穷查 ≤10s）。

判据：plan 只读、硬预算 ≤10s；problems[]（severity/evidence/fix）；
autoload 与 tests preload/load 断裂为 error（GitHub 克隆 8 分钟失败的
直接根因——缺 addon 在 10s 内具名）；gut addon/import/map 缺失为
warning（既有路径已具名/惰性构建）。默认 run 有 error 即停并列问题
（不跑任何重活）；dry-run 永不阻断只披露。
"""

from __future__ import annotations

import json

from godot_qa_toolkit.mutation import plancheck


def _write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _healthy_project(tmp_path, monkeypatch):
    """健康最小工程：autoload 存在、tests preload 存在、gut 在位。"""
    monkeypatch.setenv("GQT_GODOT_BIN", "/bin/sh")
    root = tmp_path / "proj"
    _write(root, "project.godot",
           'config_version=5\n\n[autoload]\nGame="*res://autoload/game.gd"\n')
    _write(root, "autoload/game.gd", "extends Node\n")
    _write(root, "tests/unit/test_a.gd",
           'extends GutTest\nconst G = preload("res://autoload/game.gd")\n')
    _write(root, "addons/gut/gut_cmdln.gd", "# stub\n")
    (root / ".godot").mkdir(parents=True)
    for n in ("global_script_class_cache.cfg", "uid_cache.bin", "imported"):
        (root / ".godot" / n).write_text("x", encoding="utf-8")
    return root


def test_healthy_project_has_no_errors(tmp_path, monkeypatch):
    root = _healthy_project(tmp_path, monkeypatch)
    plan = plancheck.static_plan(str(root))
    errs = [p for p in plan["problems"] if p["severity"] == "error"]
    assert errs == []
    assert plan["budget_s"] <= 10
    assert plan["elapsed_s"] <= plan["budget_s"]
    assert plan["estimated_costs"]  # 贵操作披露清单在场


def test_missing_addon_preload_is_named_error(tmp_path, monkeypatch):
    """GitHub 克隆根因形态：tests 引用缺失 addon（N 处）→ error 具名。"""
    root = _healthy_project(tmp_path, monkeypatch)
    _write(root, "tests/unit/test_lease.gd",
           'extends GutTest\n'
           'const A = preload("res://addons/godot_mcp/lease/ctl.gd")\n'
           'const B = preload("res://addons/godot_mcp/lease/ctl2.gd")\n')
    plan = plancheck.static_plan(str(root))
    errs = [p for p in plan["problems"] if p["severity"] == "error"]
    assert len(errs) == 1
    p = errs[0]
    assert p["code"] == "preload_target_missing"
    assert p["evidence"]["count"] == 2
    assert "godot_mcp" in json.dumps(p["evidence"], ensure_ascii=False)
    assert p["fix"]


def test_missing_autoload_target_is_named_error(tmp_path, monkeypatch):
    root = _healthy_project(tmp_path, monkeypatch)
    _write(root, "project.godot",
           'config_version=5\n\n[autoload]\n'
           'Game="*res://autoload/game.gd"\nGhost="*res://autoload/ghost.gd"\n')
    plan = plancheck.static_plan(str(root))
    errs = [p for p in plan["problems"] if p["severity"] == "error"]
    assert any(p["code"] == "autoload_target_missing"
               and "ghost.gd" in json.dumps(p["evidence"], ensure_ascii=False)
               for p in errs)


def test_missing_gut_addon_is_warning_not_blocker(tmp_path, monkeypatch):
    root = _healthy_project(tmp_path, monkeypatch)
    (root / "addons" / "gut" / "gut_cmdln.gd").unlink()
    plan = plancheck.static_plan(str(root))
    assert not [p for p in plan["problems"] if p["severity"] == "error"]
    assert any(p["code"] == "gut_addon_missing" and p["severity"] == "warning"
               for p in plan["problems"])


def test_plan_budget_is_bounded(tmp_path, monkeypatch):
    root = _healthy_project(tmp_path, monkeypatch)
    plan = plancheck.static_plan(str(root), budget_s=10.0)
    assert plan["elapsed_s"] <= 10.5
    assert plan["budget_s"] == 10.0


def test_run_blocks_on_plan_error_without_spawning(tmp_path, monkeypatch):
    """默认 run：plan error（preload 断裂）即停并列问题——不进入任何
    GUT/映射重活（GitHub 克隆 10s 级具名拒绝形态）。"""
    from godot_qa_toolkit.mutation import executor as ex
    from godot_qa_toolkit.mutation import inject

    root = _healthy_project(tmp_path, monkeypatch)
    _write(root, "tests/unit/test_lease.gd",
           'extends GutTest\n'
           'const A = preload("res://addons/godot_mcp/missing.gd")\n')
    spawned: list = []
    real_spawn = inject.spawn_godot
    monkeypatch.setattr(inject, "spawn_godot",
                        lambda *a, **k: spawned.append(1) or real_spawn(*a, **k))
    from godot_qa_toolkit.mutation import truth_select
    monkeypatch.setattr(truth_select, "ensure_map",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not build map")))
    r = ex.run_mutation_files([str(root / "autoload" / "game.gd")],
                              str(root), dry_run=False, select_mode="full")
    assert r["ok"] is False and r["error"] == "plan_blocked"
    assert r["plan"]["problems"]
    assert any(p["code"] == "preload_target_missing"
               for p in r["plan"]["problems"])
    assert spawned == []
