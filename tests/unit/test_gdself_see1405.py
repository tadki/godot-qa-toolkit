"""SEE-1405 §SPEC-003：gdself 正向索引单测（四信息源/身份键/闭包/兜底/降级）。

fixture 拓扑（toy project）：
- autoload/effect_manager.gd   S2：注册名 EffectManager（node 名 ≠ stem）
- systems/item/item_data.gd    S1：class_name ItemData（纯数据类转手链）
- systems/item/item_factory.gd S4：preload item_data.gd 字面量
- systems/item/item_view.gd    S3：被 scenes/item_view.tscn ext_resource 绑定
- systems/fx/fire.gd           仅被动态插值 load 命中（目录兜底）
- systems/bus/event_bus.gd     hub（25 个生产消费者，扇出 > K）
- systems/orphan.gd            全项目零引用（no_tests / uid_literal 分类）
"""

from __future__ import annotations

from pathlib import Path

from godot_qa_toolkit.mutation.gdself import (
    build_index,
    query_consumers,
)

SCENE = """[gd_scene load_steps=2 format=3]
[ext_resource type="Script" path="res://systems/item/item_view.gd" id="1"]
[node name="ItemView" type="Node2D"]
script = ExtResource("1")
"""


def _toy_project(tmp_path: Path, with_uid_test: bool = True,
                 hub_consumers: int = 25) -> Path:
    root = tmp_path / "proj"
    (root / "autoload").mkdir(parents=True)
    (root / "systems" / "item").mkdir(parents=True)
    (root / "systems" / "fx").mkdir(parents=True)
    (root / "systems" / "bus").mkdir(parents=True)
    (root / "scenes").mkdir(parents=True)
    tests = root / "tests" / "unit"
    tests.mkdir(parents=True)

    (root / "project.godot").write_text(
        "[application]\n"
        "[autoload]\n"
        'EffectManager="*res://autoload/effect_manager.gd"\n',
        encoding="utf-8")
    (root / "autoload" / "effect_manager.gd").write_text(
        "extends Node\n", encoding="utf-8")
    (root / "systems" / "item" / "item_data.gd").write_text(
        "class_name ItemData\nextends RefCounted\n", encoding="utf-8")
    (root / "systems" / "item" / "item_factory.gd").write_text(
        'const ItemData = preload("res://systems/item/item_data.gd")\n'
        "class_name ItemFactory\n",
        encoding="utf-8")
    (root / "systems" / "item" / "item_view.gd").write_text(
        "class_name ItemView\nextends Node2D\n", encoding="utf-8")
    (root / "scenes" / "item_view.tscn").write_text(SCENE, encoding="utf-8")
    (root / "systems" / "fx" / "fx_loader.gd").write_text(
        "class_name FxLoader\n"
        'func _mk(id):\n\treturn load("res://systems/fx/%s.gd" % id)\n',
        encoding="utf-8")
    (root / "systems" / "fx" / "fire.gd").write_text(
        "extends RefCounted\n", encoding="utf-8")
    (root / "systems" / "bus" / "event_bus.gd").write_text(
        "class_name EventBus\nextends Node\n", encoding="utf-8")
    for i in range(hub_consumers):
        (root / "systems" / f"consumer_{i}.gd").write_text(
            f"extends Node\nvar _bus: EventBus\n# hub consumer {i}\n",
            encoding="utf-8")
    (root / "systems" / "orphan.gd").write_text(
        "class_name Orphan\nextends RefCounted\n", encoding="utf-8")

    (tests / "test_item.gd").write_text(
        'const F = preload("res://systems/item/item_factory.gd")\n'
        "var data: ItemData\n"
        'var em = get_node_or_null("EffectManager")\n',
        encoding="utf-8")
    (tests / "test_scene.gd").write_text(
        'const S = preload("res://scenes/item_view.tscn")\n',
        encoding="utf-8")
    (tests / "test_fx.gd").write_text(
        'const L = preload("res://systems/fx/fx_loader.gd")\n',
        encoding="utf-8")
    (tests / "test_bus.gd").write_text(
        "var bus: EventBus\n", encoding="utf-8")
    if with_uid_test:
        (tests / "test_uid.gd").write_text(
            'var r = load("uid://abc123")\n', encoding="utf-8")
    return root


# ---- build_index：四信息源 ---------------------------------------------------


class TestBuildIndex:
    def test_s2_autoload_registry_parsed(self, tmp_path):
        root = _toy_project(tmp_path)
        idx = build_index(str(root))
        assert idx["autoloads"] == {
            "EffectManager": "res://autoload/effect_manager.gd"}

    def test_s1_class_name_captured(self, tmp_path):
        root = _toy_project(tmp_path)
        idx = build_index(str(root))
        info = idx["files"]["res://systems/item/item_data.gd"]
        assert info["class_name"] == "ItemData"

    def test_s4_literal_extracted(self, tmp_path):
        root = _toy_project(tmp_path)
        idx = build_index(str(root))
        info = idx["files"]["res://tests/unit/test_item.gd"]
        assert "res://systems/item/item_factory.gd" in \
            [p.casefold() for p in info["lits"]]

    def test_s3_scene_binding_extracted(self, tmp_path):
        root = _toy_project(tmp_path)
        idx = build_index(str(root))
        info = idx["files"]["res://scenes/item_view.tscn"]
        assert "res://systems/item/item_view.gd" in \
            [p.casefold() for p in info["lits"]]

    def test_dynamic_interpolation_dir_prefix(self, tmp_path):
        root = _toy_project(tmp_path)
        idx = build_index(str(root))
        info = idx["files"]["res://systems/fx/fx_loader.gd"]
        assert info["dyn_dirs"] == ["res://systems/fx/"]

    def test_uid_literals_counted(self, tmp_path):
        root = _toy_project(tmp_path, with_uid_test=True)
        idx = build_index(str(root))
        assert idx["uid_literals"] >= 1

    def test_strip_noise_comment_and_string_not_identity_hits(self, tmp_path):
        root = _toy_project(tmp_path)
        idx = build_index(str(root))
        (root / "tests" / "unit" / "test_noisy.gd").write_text(
            '# class_name ItemData 出现在注释\n'
            'var s = "ItemData in string"\n',
            encoding="utf-8")
        idx2 = build_index(str(root))
        out = query_consumers(str(root), "res://systems/item/item_data.gd",
                              root / "tests")
        noisy = "res://tests/unit/test_noisy.gd"
        hit_files = {c["res"] for c in out["consumers"]}
        assert noisy not in hit_files
        assert idx2 is not None  # 重建索引包含新文件（零缓存语义）

    def test_v1_builds_fresh_every_call_no_cache(self, tmp_path):
        root = _toy_project(tmp_path)
        i1 = build_index(str(root))
        (root / "systems" / "late.gd").write_text("class_name Late\n",
                                                  encoding="utf-8")
        i2 = build_index(str(root))
        assert "res://systems/late.gd" in i2["files"]
        assert "res://systems/late.gd" not in i1["files"]


# ---- 消费者查询：四源命中与闭包 ---------------------------------------------


class TestConsumerQuery:
    def _out(self, root, target, **kw):
        return query_consumers(str(root), target, root / "tests", **kw)

    def test_s1_class_name_identity_hit(self, tmp_path):
        root = _toy_project(tmp_path)
        out = self._out(root, "res://systems/item/item_factory.gd")
        hits = {c["res"] for c in out["consumers"]}
        assert "res://tests/unit/test_item.gd" in hits  # ItemData 词命中→闭包
        assert any("class_name" in c["sources"] or "path" in c["sources"]
                   for c in out["consumers"])

    def test_s2_autoload_token_hit(self, tmp_path):
        root = _toy_project(tmp_path)
        out = self._out(root, "res://autoload/effect_manager.gd")
        hits = {c["res"] for c in out["consumers"]}
        assert "res://tests/unit/test_item.gd" in hits
        item = next(c for c in out["consumers"]
                    if c["res"].endswith("test_item.gd"))
        assert "autoload" in item["sources"]

    def test_s3_scene_chain_within_depth2(self, tmp_path):
        root = _toy_project(tmp_path)
        out = self._out(root, "res://systems/item/item_view.gd")
        hits = {c["res"] for c in out["consumers"]}
        assert "res://tests/unit/test_scene.gd" in hits
        scene = next(c for c in out["consumers"]
                     if c["res"].endswith(".tscn"))
        assert "path" in scene["sources"]  # 经资产节点中转

    def test_depth1_config_misses_scene_chain(self, tmp_path):
        root = _toy_project(tmp_path)
        out = self._out(root, "res://systems/item/item_view.gd", max_depth=1)
        hits = {c["res"] for c in out["consumers"]}
        assert "res://tests/unit/test_scene.gd" not in hits

    def test_hub_fanout_truncated_but_tests_kept(self, tmp_path):
        root = _toy_project(tmp_path)
        out = self._out(root, "res://systems/bus/event_bus.gd",
                        hub_fanout=20)
        hits = {c["res"] for c in out["consumers"]}
        assert "res://tests/unit/test_bus.gd" in hits  # 终端命中不丢
        assert out["unresolved"]["hub_truncated"] >= 1

    def test_dynamic_dir_fallback_conservative(self, tmp_path):
        root = _toy_project(tmp_path)
        out = self._out(root, "res://systems/fx/fire.gd")
        hits = {c["res"] for c in out["consumers"]}
        assert "res://tests/unit/test_fx.gd" in hits  # fire←fx_loader←test
        assert out["unresolved"]["dynamic_hits"] >= 1

    def test_unresolved_reason_uid_literal(self, tmp_path):
        root = _toy_project(tmp_path)
        out = self._out(root, "res://systems/orphan.gd")
        assert out["consumers"] == []
        assert out["unresolved"]["reason"] == "uid_literal"

    def test_unresolved_reason_no_tests(self, tmp_path):
        root = _toy_project(tmp_path, with_uid_test=False)
        out = self._out(root, "res://systems/orphan.gd")
        assert out["consumers"] == []
        assert out["unresolved"]["reason"] == "no_tests"


# ---- 身份键语义 -------------------------------------------------------------


class TestIdentitySemantics:
    def test_class_name_case_sensitive(self, tmp_path):
        root = _toy_project(tmp_path)
        (root / "tests" / "unit" / "test_wrongcase.gd").write_text(
            "var d: itemdata\n", encoding="utf-8")
        out = query_consumers(str(root),
                              "res://systems/item/item_data.gd",
                              root / "tests")
        hits = {c["res"] for c in out["consumers"]}
        assert "res://tests/unit/test_wrongcase.gd" not in hits

    def test_path_literal_casefold_match(self, tmp_path):
        root = _toy_project(tmp_path)
        (root / "tests" / "unit" / "test_upper.gd").write_text(
            'const D = preload("RES://SYSTEMS/ITEM/ITEM_DATA.GD")\n',
            encoding="utf-8")
        out = query_consumers(str(root),
                              "res://systems/item/item_data.gd",
                              root / "tests")
        hits = {c["res"] for c in out["consumers"]}
        assert "res://tests/unit/test_upper.gd" in hits

    def test_stem_word_match_is_casefold(self, tmp_path):
        root = _toy_project(tmp_path)
        (root / "tests" / "unit" / "test_stem.gd").write_text(
            "var orphan_ref = Orphan\n", encoding="utf-8")
        out = query_consumers(str(root), "res://systems/orphan.gd",
                              root / "tests")
        hits = {c["res"] for c in out["consumers"]}
        assert "res://tests/unit/test_stem.gd" in hits
