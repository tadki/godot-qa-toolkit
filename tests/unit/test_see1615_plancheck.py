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
    # SEE-1623 M7：信封归一——error/plan 只在 summary，failures 在场。
    assert r["ok"] is False
    assert "error" not in r and "plan" not in r
    assert r["summary"]["error"] == "plan_blocked"
    assert r["failures"]
    assert r["summary"]["plan"]["problems"]
    assert any(p["code"] == "preload_target_missing"
               for p in r["summary"]["plan"]["problems"])
    assert spawned == []


# §SPEC-PREREQ-MATRIX：分级矩阵、聚合、P1 默认停 + --auto-prepare

def test_problems_carry_prereq_matrix_fields(tmp_path, monkeypatch):
    """每问题带 level(P0-P3)/source/fix/auto_prepare_covered；
    同一阶段全部静态问题一次列全（不得修一个再报一个）。"""
    root = _healthy_project(tmp_path, monkeypatch)
    (root / "addons" / "gut" / "gut_cmdln.gd").unlink()
    (root / ".godot" / "uid_cache.bin").unlink()
    _write(root, "tests/unit/test_broken.gd",
           'extends GutTest\n'
           'const A = preload("res://addons/godot_mcp/m1.gd")\n'
           'const B = preload("res://addons/godot_mcp/m2.gd")\n')
    _write(root, "project.godot",
           'config_version=5\n\n[autoload]\n'
           'Game="*res://autoload/game.gd"\n'
           'Ghost="*res://autoload/ghost.gd"\n')
    plan = plancheck.static_plan(str(root))
    probs = plan["problems"]
    assert len(probs) >= 4  # autoload + preload + gut + import 一次列全
    for p in probs:
        assert p["level"] in ("P0", "P1", "P2", "P3")
        assert p["source"]
        assert p["fix"]
        assert isinstance(p["auto_prepare_covered"], bool)
    levels = {p["code"]: p["level"] for p in probs}
    assert levels["autoload_target_missing"] == "P0"
    assert levels["preload_target_missing"] == "P0"
    assert levels["gut_addon_missing"] == "P0"
    assert levels["import_cache_missing"] == "P1"


def test_map_missing_is_p1_with_cost_and_fix_command(tmp_path, monkeypatch):
    root = _healthy_project(tmp_path, monkeypatch)
    target = _write(root, "utils/calc.gd", "extends RefCounted\n")
    plan = plancheck.static_plan(str(root), str(target))
    m = [p for p in plan["problems"]
         if p["code"] == "attribution_map_missing_or_stale"]
    assert m and m[0]["level"] == "P1"
    assert m[0]["auto_prepare_covered"] is True
    assert "map-update" in m[0]["fix"]
    assert "estimated_cost" in m[0]["evidence"]


def test_cli_p1_stops_by_default_auto_prepare_continues(tmp_path, monkeypatch):
    """P1 默认停（prepare_required + 修复命令/成本）；--auto-prepare 继续。
    SEE-1623 M1 改约：本钉为默认面形态——显式 --tests 不再被 P1 门拦
    （用户接管测试面，映射前置不适用）。"""
    import argparse
    from godot_qa_toolkit import cli as cli_mod

    root = _healthy_project(tmp_path, monkeypatch)
    target = _write(root, "utils/calc.gd", "extends RefCounted\n")
    base = dict(project_root=str(root), files=[str(target)],
                tests=None, select_mode="line",
                dry_run=False, attribution_map=None)

    import io
    import json as _json
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_mod._plan_gate_mutation(argparse.Namespace(
            auto_prepare=False, **base))
    assert rc is not None and rc != 0
    stopped = _json.loads(buf.getvalue())
    # SEE-1623 M7：信封归一——error/plan 只在 summary（顶层键撤销）。
    assert stopped["summary"]["error"] == "prepare_required"
    assert "error" not in stopped and "plan" not in stopped
    assert set(stopped) == {"tool", "ok", "summary", "failures"}
    p1 = stopped["summary"]["problems"]
    assert p1 and all(p["level"] == "P1" for p in p1)
    assert any("map-update" in p["fix"] for p in p1)

    cont = cli_mod._plan_gate_mutation(argparse.Namespace(
        auto_prepare=True, **base))
    assert cont is None  # auto-prepare → 继续（既有惰性 prepare 补齐）


# §SPEC-ANCHOR-PREVIEW：map_preview_state 锚定 + stale 前缀


def test_map_preview_fresh_with_cwd_relative_target(tmp_path, monkeypatch):
    """CWD≠root 时相对目标锚定后 fresh 判定不误报 stale。"""
    from godot_qa_toolkit.mutation.truth_select import map_preview_state

    root = _healthy_project(tmp_path, monkeypatch)
    target = _write(root, "utils/calc.gd", "extends RefCounted\n")
    from attribution_fixtures import write_map
    write_map(root, {"utils/calc.gd": []})
    monkeypatch.chdir(tmp_path)  # CWD≠root
    st = map_preview_state(str(root), "utils/calc.gd")
    assert st["present"] is True and st["fresh"] is True
    assert st["reason"] == "map_fresh"
    # 绝对路径同判
    st_abs = map_preview_state(str(root), str(target))
    assert st_abs["fresh"] is True


def test_map_preview_stale_reason_prefix_named(tmp_path, monkeypatch):
    """LOW-L2：stale 前缀 map_stale:<detail>（原误为 map_fresh:detail）。"""
    from godot_qa_toolkit.mutation.truth_select import map_preview_state

    root = _healthy_project(tmp_path, monkeypatch)
    from attribution_fixtures import write_map
    write_map(root, {"utils/calc.gd": []})
    _write(root, "utils/calc.gd", "extends RefCounted\n# drift\n")
    monkeypatch.chdir(tmp_path)
    st = map_preview_state(str(root), "utils/calc.gd")
    assert st["fresh"] is False
    assert st["reason"].startswith("map_stale:")
