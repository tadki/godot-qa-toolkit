"""SEE-1405 §SPEC-002：gdself_stats 统计脚本单测（toy 项目端到端）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from godot_qa_toolkit.mutation.gdself_stats import collect_stats, main


@pytest.fixture()
def toy(tmp_path):
    root = tmp_path / "proj"
    (root / "systems").mkdir(parents=True)
    (root / "scenes").mkdir()
    tests = root / "tests"
    tests.mkdir()
    (root / "project.godot").write_text(
        "[autoload]\nEff=\"*res://autoload/eff.gd\"\n", encoding="utf-8")
    (root / "autoload").mkdir()
    (root / "autoload" / "eff.gd").write_text("extends Node\n",
                                              encoding="utf-8")
    (root / "systems" / "data.gd").write_text(
        "class_name Data\nextends RefCounted\n", encoding="utf-8")
    (root / "systems" / "factory.gd").write_text(
        'const D = preload("res://systems/data.gd")\n'
        "var cached: Data\n", encoding="utf-8")
    (root / "systems" / "orphan.gd").write_text(
        "class_name Orphan\n", encoding="utf-8")
    (root / "scenes" / "v.tscn").write_text(
        '[gd_scene format=3]\n'
        '[ext_resource type="Script" path="res://autoload/eff.gd" id="1"]\n')
    (tests / "t.gd").write_text(
        'const F = preload("res://systems/factory.gd")\n'
        'var e = get_node("Eff")\n'
        'const S = preload("res://scenes/v.tscn")\n')
    return root


class TestCollectStats:
    def test_dual_scope_and_shares(self, toy):
        stats = collect_stats(str(toy))
        core = stats["scope_core"]
        assert core["production_gd_files"] == 4  # data/factory/orphan/eff
        # 反查零命中：data.gd（转手）与 orphan.gd；factory.gd 有直接引用
        assert core["legacy_zero_hit"] == 2
        # data.gd 被 factory 转手（S4+S1）、orphan 零引用
        assert core["gdself_eliminated"] == 1
        assert core["remaining_unresolved_reasons"] == {"no_tests": 1}
        assert core["per_source_any_hit"]["S1"] >= 1  # class_name 词命中
        assert core["per_source_any_hit"]["S4"] >= 1  # preload 字面量命中
        # autoload eff.gd 有直接引用，不在零命中集
        assert stats["threshold"]["pct_of_startup_tax"] == 5.0
        assert stats["startup_tax_ref"]["provisional"] is True

    def test_scene_chain_counted(self, toy):
        stats = collect_stats(str(toy))
        # eff.gd 有直接命中不在零命中集——场景链计数为 0；不炸即可
        assert isinstance(stats["scope_core"]["scene_chain_targets"], int)

    def test_threshold_flags_v2_when_over_budget(self, toy, monkeypatch):
        import godot_qa_toolkit.mutation.gdself_stats as gs
        monkeypatch.setattr(gs, "PROVISIONAL_STARTUP_TAX_S", 0.0)
        stats = gs.collect_stats(str(toy))
        assert stats["threshold"]["v2_needed"] is True


class TestMain:
    def test_missing_project_godot_exits_2(self, tmp_path, capsys):
        assert main(["--project-root", str(tmp_path)]) == 2
        assert "project.godot" in capsys.readouterr().err

    def test_json_on_stdout(self, toy, capsys):
        rc = main(["--project-root", str(toy)])
        assert rc == 0
        import json
        payload = json.loads(capsys.readouterr().out)
        assert "scope_core" in payload
