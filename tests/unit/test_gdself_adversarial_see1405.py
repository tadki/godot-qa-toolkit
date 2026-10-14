"""SEE-1405 ③ hardener——gdself 对抗性补强（预设：默认实现是错的）。

Python 源不做 gqt mutation（SEE-1356 裁定），变异式判别力由本组用例承担。
必杀目标映射：
- 删身份键轨道 → 三轨同中用例（class_name/path/stem 缺一即红）
- 破坏大小写分轨 → class_name 大写变形 / autoload token 小写冒充
- 改截断常数或比较 → hub 恰 K=20 不截断且继续展开、K+1 截断保测试
- 删 strip_noise 转义分支 → \\" 提前终止字符串致类名词泄漏
- key fs 分支 / 大写 res:// 前缀 → 绝对路径与 res:// 目标同消费集
- 兜底抢先静态 → dynamic 混入静态命中即红
"""

from __future__ import annotations

from pathlib import Path

from godot_qa_toolkit.mutation.gdself import (
    _unresolved_reason,
    build_index,
    query_consumers,
)


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _mini_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "systems" / "item").mkdir(parents=True)
    (root / "tests" / "unit").mkdir(parents=True)
    (root / "project.godot").write_text(
        '[application]\nconfig/name="adv"\n', encoding="utf-8")
    _write(root, "systems/item/item_data.gd",
           "class_name ItemData\nextends RefCounted\n")
    _write(root, "systems/orphan.gd",
           "class_name Orphan\nextends RefCounted\n")
    return root


def _hub_project(tmp_path: Path, n_consumers: int) -> Path:
    root = tmp_path / "proj"
    (root / "systems").mkdir(parents=True)
    (root / "tests" / "unit").mkdir(parents=True)
    (root / "project.godot").write_text("[application]\n", encoding="utf-8")
    _write(root, "systems/bus.gd", "class_name EventBus\nextends Node\n")
    for i in range(n_consumers):
        _write(root, f"systems/consumer_{i}.gd",
               "extends Node\nvar _bus: EventBus\n")
    _write(root, "tests/unit/test_bus.gd", "var bus: EventBus\n")
    return root


def _query(root: Path, target: str, **kw) -> dict:
    return query_consumers(str(root), target, root / "tests", **kw)


def _hits(out: dict) -> set[str]:
    return {c["res"] for c in out["consumers"]}


# ---- 四信息源边界 -----------------------------------------------------------


class TestFourSourceBoundaries:
    def test_missing_project_godot_s2_inert_but_s4_works(self, tmp_path):
        root = _mini_project(tmp_path)
        (root / "project.godot").unlink()
        _write(root, "tests/unit/test_item.gd",
               'const D = preload("res://systems/item/item_data.gd")\n')
        idx = build_index(str(root))
        assert idx["autoloads"] == {}
        out = _query(root, "res://systems/item/item_data.gd")
        assert "res://tests/unit/test_item.gd" in _hits(out)

    def test_pg_without_autoload_section_no_autoload_identities(
            self, tmp_path):
        root = _mini_project(tmp_path)
        _write(root, "systems/effect_manager.gd", "extends Node\n")
        _write(root, "tests/unit/test_em.gd",
               'var mgr = get_node_or_null("EffectManager")\n')
        idx = build_index(str(root))
        assert idx["autoloads"] == {}
        out = _query(root, "res://systems/effect_manager.gd")
        assert out["consumers"] == []

    def test_autoload_section_scoped_to_next_section_header(self, tmp_path):
        root = _mini_project(tmp_path)
        _write(root, "autoload/em.gd", "extends Node\n")
        _write(root, "systems/fake.gd", "extends Node\n")
        (root / "project.godot").write_text(
            "[autoload]\n"
            'EffectManager="*res://autoload/em.gd"\n'
            "[editor_plugins]\n"
            'Fake="*res://systems/fake.gd"\n',
            encoding="utf-8")
        idx = build_index(str(root))
        assert set(idx["autoloads"]) == {"EffectManager"}

    def test_autoload_star_and_plain_entries_both_registered(self, tmp_path):
        root = _mini_project(tmp_path)
        _write(root, "autoload/em.gd", "extends Node\n")
        _write(root, "autoload/tm.gd", "extends Node\n")
        (root / "project.godot").write_text(
            "[autoload]\n"
            'A="*res://autoload/em.gd"\n'
            'B="res://autoload/tm.gd"\n',
            encoding="utf-8")
        idx = build_index(str(root))
        assert idx["autoloads"] == {
            "A": "res://autoload/em.gd",
            "B": "res://autoload/tm.gd",
        }

    def test_malformed_tscn_tolerated_tres_binding_consumed(self, tmp_path):
        root = _mini_project(tmp_path)
        _write(root, "scenes/bad.tscn",
               '[gd_scene format=3]\n'
               '[ext_resource type="Texture" path="res://icon.png" id="1"]\n'
               '[ext_resource type="Script" id="2"]\n'
               'broken path="no-res-here" id="3"\n')
        _write(root, "scenes/vm.tres",
               '[gd_resource type="Resource" format=3]\n'
               '[ext_resource type="Script" '
               'path="res://systems/item/item_data.gd" id="1"]\n')
        idx = build_index(str(root))
        assert idx["files"]["res://scenes/bad.tscn"]["lits"] == []
        assert "res://systems/item/item_data.gd" in \
            idx["files"]["res://scenes/vm.tres"]["lits"]
        out = _query(root, "res://systems/item/item_data.gd")
        kinds = {c["res"]: c["kind"] for c in out["consumers"]}
        assert kinds.get("res://scenes/vm.tres") == "asset"

    def test_non_res_non_uid_literal_still_recorded(self, tmp_path):
        root = _mini_project(tmp_path)
        _write(root, "systems/saver.gd",
               'func _save():\n\treturn load("user://save.cfg")\n')
        idx = build_index(str(root))
        info = idx["files"]["res://systems/saver.gd"]
        assert "user://save.cfg" in info["lits"]
        assert idx["uid_literals"] == 0

    def test_uid_literal_counted_but_not_a_path_key(self, tmp_path):
        root = _mini_project(tmp_path)
        _write(root, "systems/ref.gd", 'var s = load("uid://b98x")\n')
        idx = build_index(str(root))
        assert idx["files"]["res://systems/ref.gd"]["lits"] == []
        assert idx["uid_literals"] == 1

    def test_tool_dirs_and_git_excluded_from_index(self, tmp_path):
        root = _mini_project(tmp_path)
        _write(root, ".godot/imported/cached.gd", "class_name ItemData\n")
        _write(root, ".qa-cache/gqt-mutation-resume/k/0.gd",
               "class_name ItemData\n")
        _write(root, ".git/hooks/seed.gd", "class_name ItemData\n")
        idx = build_index(str(root))
        assert not any(k.startswith(("res://.godot/", "res://.qa-cache/"))
                       for k in idx["files"])
        assert not any(".git" in k for k in idx["files"])
        out = _query(root, "res://systems/item/item_data.gd")
        assert out["consumers"] == []

    def test_strip_noise_keeps_escaped_quote_inside_string(self, tmp_path):
        root = _mini_project(tmp_path)
        _write(root, "tests/unit/test_esc.gd",
               'var t = "use \\"ItemData\\" here"\n')
        out = _query(root, "res://systems/item/item_data.gd")
        assert "res://tests/unit/test_esc.gd" not in _hits(out)


# ---- 身份键大小写分轨 -------------------------------------------------------


class TestCaseSplitTracks:
    def test_class_name_uppercase_variant_not_a_hit(self, tmp_path):
        root = _mini_project(tmp_path)
        _write(root, "tests/unit/test_up.gd", "var d: ITEMDATA\n")
        out = _query(root, "res://systems/item/item_data.gd")
        assert "res://tests/unit/test_up.gd" not in _hits(out)

    def test_class_name_digit_suffix_word_not_a_hit(self, tmp_path):
        root = _mini_project(tmp_path)
        _write(root, "tests/unit/test_v2.gd", "var d: ItemData2\n")
        out = _query(root, "res://systems/item/item_data.gd")
        assert "res://tests/unit/test_v2.gd" not in _hits(out)

    def test_autoload_token_is_case_sensitive(self, tmp_path):
        root = _mini_project(tmp_path)
        _write(root, "autoload/effect_manager.gd", "extends Node\n")
        (root / "project.godot").write_text(
            "[application]\n[autoload]\n"
            'EffectManager="*res://autoload/effect_manager.gd"\n',
            encoding="utf-8")
        _write(root, "tests/unit/test_exact.gd",
               'var em = get_node("EffectManager")\n')
        _write(root, "tests/unit/test_lower.gd",
               'var em2 = get_node("effectmanager")\n')
        out = _query(root, "res://autoload/effect_manager.gd")
        by = {c["res"]: c for c in out["consumers"]}
        assert set(by) == {"res://tests/unit/test_exact.gd"}
        assert by["res://tests/unit/test_exact.gd"]["sources"] == \
            ["autoload"]

    def test_query_side_res_prefix_casefolded(self, tmp_path):
        root = _mini_project(tmp_path)
        _write(root, "tests/unit/test_o.gd", "var orphan = Orphan\n")
        lower = _query(root, "res://systems/orphan.gd")
        upper = _query(root, "RES://SYSTEMS/ORPHAN.GD")
        assert upper["consumers"] == lower["consumers"]
        assert upper["target"] == "res://systems/orphan.gd"

    def test_fs_path_target_same_consumers_as_res_form(self, tmp_path):
        root = _mini_project(tmp_path)
        _write(root, "tests/unit/test_o.gd", "var orphan = Orphan\n")
        fs = _query(root, str(root / "systems" / "orphan.gd"))
        res = _query(root, "res://systems/orphan.gd")
        assert fs["target"] == "res://systems/orphan.gd"
        assert fs["consumers"] == res["consumers"]

    def test_single_consumer_hits_three_tracks_at_once(self, tmp_path):
        root = _mini_project(tmp_path)
        _write(root, "tests/unit/test_multi.gd",
               'const D = preload("res://systems/item/item_data.gd")\n'
               "var item_data = null\nvar cache: ItemData\n")
        out = _query(root, "res://systems/item/item_data.gd")
        assert out["consumers"][0]["sources"] == \
            ["class_name", "path", "stem"]


# ---- 闭包深度 / hub 扇出截断边界 --------------------------------------------


class TestClosureAndHubBoundary:
    def test_hub_batch_exactly_K_still_expands(self, tmp_path):
        root = _hub_project(tmp_path, n_consumers=19)
        _write(root, "systems/child.gd",
               'const C = preload("res://systems/consumer_0.gd")\n')
        out = _query(root, "res://systems/bus.gd", hub_fanout=20)
        assert "res://systems/child.gd" in _hits(out)
        assert out["unresolved"]["hub_truncated"] == 0

    def test_hub_batch_K_plus_1_truncates_but_keeps_tests(self, tmp_path):
        root = _hub_project(tmp_path, n_consumers=20)
        _write(root, "systems/child.gd",
               'const C = preload("res://systems/consumer_0.gd")\n')
        out = _query(root, "res://systems/bus.gd", hub_fanout=20)
        hits = _hits(out)
        assert "res://tests/unit/test_bus.gd" in hits
        assert "res://systems/child.gd" not in hits
        assert out["unresolved"]["hub_truncated"] >= 1

    def test_default_hub_fanout_is_20(self, tmp_path):
        root = _hub_project(tmp_path, n_consumers=21)
        out = _query(root, "res://systems/bus.gd")
        assert out["unresolved"]["hub_truncated"] >= 1

    def test_test_files_never_expand_even_within_depth(self, tmp_path):
        root = _mini_project(tmp_path)
        _write(root, "tests/unit/test_leaf.gd", "extends RefCounted\n")
        _write(root, "tests/unit/test_inner.gd",
               'const L = preload("res://tests/unit/test_leaf.gd")\n')
        _write(root, "systems/prod.gd",
               'const T = preload("res://tests/unit/test_inner.gd")\n')
        out = _query(root, "res://tests/unit/test_leaf.gd", max_depth=3)
        assert [c["res"] for c in out["consumers"]] == \
            ["res://tests/unit/test_inner.gd"]

    def test_reference_cycle_terminates_without_duplicates(self, tmp_path):
        root = _mini_project(tmp_path)
        _write(root, "systems/a.gd",
               'const B = preload("res://systems/b.gd")\n')
        _write(root, "systems/b.gd",
               'const A = preload("res://systems/a.gd")\n')
        _write(root, "tests/unit/test_a.gd",
               'const A = preload("res://systems/a.gd")\n')
        out = _query(root, "res://systems/a.gd", max_depth=4)
        assert sorted(_hits(out)) == [
            "res://systems/b.gd", "res://tests/unit/test_a.gd"]

    def test_depth_zero_walk_yields_no_consumers(self, tmp_path):
        root = _mini_project(tmp_path)
        _write(root, "tests/unit/test_o.gd", "var orphan = Orphan\n")
        out = _query(root, "res://systems/orphan.gd", max_depth=0)
        assert out["consumers"] == []
        assert out["unresolved"]["reason"] == "no_tests"


# ---- 插值兜底披露 -----------------------------------------------------------


class TestDynamicFallbackDisclosure:
    def test_root_level_interpolation_is_universal_consumer(self, tmp_path):
        root = _mini_project(tmp_path)
        _write(root, "tests/unit/test_dyn.gd",
               'func _mk(n):\n\treturn load("%s.gd" % n)\n')
        idx = build_index(str(root))
        dyn = idx["files"]["res://tests/unit/test_dyn.gd"]["dyn_dirs"]
        assert dyn == ["res://"]
        for target in ("res://systems/item/item_data.gd",
                       "res://systems/orphan.gd"):
            assert "res://tests/unit/test_dyn.gd" in _hits(
                _query(root, target))

    def test_static_hit_suppresses_dynamic_fallback(self, tmp_path):
        root = _mini_project(tmp_path)
        _write(root, "tests/unit/test_mixed.gd",
               'const D = preload("res://systems/item/item_data.gd")\n'
               'func _mk(n):\n\treturn load("%s.gd" % n)\n')
        out = _query(root, "res://systems/item/item_data.gd")
        assert out["consumers"][0]["sources"] == ["path"]
        assert out["unresolved"]["dynamic_hits"] == 0
        out2 = _query(root, "res://systems/orphan.gd")
        assert out2["consumers"][0]["sources"] == ["dynamic"]


# ---- unresolved 分类阶梯（防御性披露分支直测） ------------------------------


class TestUnresolvedReasonLadder:
    """分类阶梯唯一出处直测：consumers > hub > dynamic > uid > no_tests。

    hub/dynamic 两档经公开 API 不可达（hub_truncated / dynamic_hits > 0
    蕴含 consumers 非空）——防御性披露分支；直测防实现改动后静默失序。
    """

    def test_priority_ladder(self):
        idx = {"uid_literals": 2}
        key = "res://systems/orphan.gd"
        assert _unresolved_reason([], 1, 0, idx, key) == "hub_truncated"
        assert _unresolved_reason([], 0, 3, idx, key) == "dynamic_dir"
        assert _unresolved_reason([], 1, 2, idx, key) == "hub_truncated"
        assert _unresolved_reason([], 0, 0, idx, key) == "uid_literal"
        assert _unresolved_reason([], 0, 0, idx,
                                  "res://scenes/v.tscn") == "no_tests"
        assert _unresolved_reason([], 0, 0, {"uid_literals": 0},
                                  key) == "no_tests"
        assert _unresolved_reason([{"res": "x"}], 5, 5, idx, key) is None
