"""SEE-1587 §SPEC-003 — 模板单源 / prepcache 归属粒度 / 增量行事实与
Coverage Store 同步判据。

原型教训回归守卫（SEE-1585）：
- 全量/增量模板构建必须单源（两路 fresh 构建字节一致）；
- prepcache 命中路径必须连带 block_map 读回（否则行集退化为块首行）；
- 增量刷新同步维护行事实与 Coverage Store——绑定漂移显式失效，不假过滤。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from godot_qa_toolkit.measurement import increment_runner
from godot_qa_toolkit.measurement import prepcache
from godot_qa_toolkit.measurement.increment import run_attribution_increment
from godot_qa_toolkit.measurement.instrument import _instrument_pair
from godot_qa_toolkit.mutation import coverlink

TARGET_SRC = """extends RefCounted


func work(n: int) -> int:
\tvar acc = 0
\tfor i in range(n):
\t\tacc += i
\treturn acc
"""

TARGET2 = """extends Node


func ready_hook() -> void:
\tset_process(false)
"""


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "addons" / "gut").mkdir(parents=True)
    (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
    (root / "project.godot").write_text(
        'config_version=5\n[application]\nconfig/name="see1587-incr"\n')
    (root / ".gutconfig.json").write_text(json.dumps(
        {"dirs": ["res://tests/"], "include_subdirs": True,
         "prefix": "test_"}))
    (root / "tests" / "unit").mkdir(parents=True)
    (root / "tests" / "unit" / "test_game.gd").write_text(
        "extends GutTest\nfunc test_ok() -> void:\n\tassert_true(true)\n")
    feats = root / "addons" / "gqt_gherkin" / "features"
    feats.mkdir(parents=True)
    (feats / "demo.feature").write_text(
        "Feature: F\n  Scenario Outline: s\n    Then hp is <left>\n"
        "    # types: <hp>:int, <left>:numeric\n    Examples:\n"
        "      | hp | left |\n      | 10 | 7    |\n")
    game = root / "src_game"
    game.mkdir()
    (game / "cell.gd").write_text(TARGET_SRC)
    (game / "node.gd").write_text(TARGET2)
    return root


def _recipe(project: Path, tmp_path: Path) -> dict:
    from godot_qa_toolkit.measurement.recipe import build_recipe
    from attribution_fixtures import install_recipe
    install_recipe(project)
    recipe, error = build_recipe(project)
    assert error is None, error
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(recipe), encoding="utf-8")
    return recipe


# ------------------------------------------------------------ 模板单源


def test_increment_fresh_template_is_byte_identical_to_orchestrator(tmp_path):
    """单源判据：增量 fresh 构建与全量 _instrument_pair 产出的 attr 模板
    字节一致（SEE-1585 教训：两段构建代码 + 同 algo 键 = 两种形态静默
    共存）。"""
    project = _project(tmp_path)
    recipe = _recipe(project, tmp_path)
    targets = sorted(str(p) for p in (project / "src_game").glob("*.gd"))
    templates = increment_runner.prepare_attr_templates(
        project, targets, recipe, False)
    assert templates
    for path_str in targets:
        rel = str(Path(path_str).resolve().relative_to(project))
        src = Path(path_str).read_text(encoding="utf-8")
        from godot_qa_toolkit.registry.functions import build_file_registry
        registry = build_file_registry(src, file_label=rel)
        built = _instrument_pair(rel, src, registry, block_compress=True,
                                 test_attribution=True,
                                 attribution_causal=False)
        assert templates[rel]["template"] == built["template_attr"]
        assert templates[rel]["probe_lines"] == built["probe_lines"]
        assert templates[rel]["block_map"] == built["block_map"]
        # hit 级形态：归属块覆盖全部探针
        assert templates[rel]["template"].count(".get_or_add(") \
            == len(templates[rel]["probe_lines"])


def test_algo_key_shared_between_full_and_increment_paths():
    """全量与增量路径的 algo 键同源（归属粒度编码进键）。"""
    from godot_qa_toolkit.measurement.instrument import _algo_used
    for causal in (False, True):
        assert increment_runner._algo_for(causal) \
            == _algo_used(prepcache.PROBE_ALGO_VERSION, True, causal)


def test_prep_cache_hit_readback_carries_block_map(tmp_path, monkeypatch):
    """缓存命中必须连带 block_map（原型教训：只取 template/probe_lines 会
    让行事实退化为块首行）。"""
    project = _project(tmp_path)
    recipe = _recipe(project, tmp_path)
    targets = sorted(str(p) for p in (project / "src_game").glob("*.gd"))
    sentinel = {
        "instrumented_template": "tpl GQTUUIDSENTINEL",
        "probe_lines": [4, 5],
        "registry": {"entries": []},
        "block_map": {4: [4, 5, 6], 5: [5]},
    }
    monkeypatch.setattr(prepcache, "load_entry", lambda *a, **k: sentinel)
    out = increment_runner.prepare_attr_templates(
        project, targets, recipe, False)
    rel = "src_game/cell.gd"
    assert out[rel]["template"] == "tpl GQTUUIDSENTINEL"
    assert out[rel]["block_map"] == {4: [4, 5, 6], 5: [5]}


def test_prep_cache_roundtrip_preserves_block_map(tmp_path):
    """真实 store_entry → load_entry 往返：block_map 完整读回。"""
    root = str(tmp_path)
    block_map = {4: [4, 5, 6], 7: [7]}
    prepcache.store_entry(
        root, rel="x.gd", src_sha256="s", recipe_hash="r",
        registry={"entries": []}, probe_manifest={},
        instrumented_template="tpl GQTUUIDSENTINEL", probe_lines=[4, 7],
        source_plain="src", block_map=block_map,
        algo=increment_runner._algo_for(False))
    entry = prepcache.load_entry(root, rel="x.gd", src_sha256="s",
                                 recipe_hash="r",
                                 algo=increment_runner._algo_for(False))
    assert entry is not None
    assert entry["block_map"] == block_map


# ------------------------------------------------------------ 增量行事实同步


def _baseline_doc(recipe_hash_val: str) -> dict:
    cell_lines = {"test_a.gd::ta": {"src_game/cell.gd": [5]},
                  "test_b.gd::tb": {"src_game/cell.gd": [5, 7],
                                    "src_game/node.gd": [4]}}
    return {
        "schema_version": "test-attribution/7",
        "test_verdicts": {},
        "report_recipe_hash": recipe_hash_val,
        "recipe_hash_pristine": "pristine-1",
        "project_root": "",
        "identity": "res://script::test_name",
        "arms": {"gut_full": {
            "test_to_files": {"test_a.gd::ta": ["src_game/cell.gd"],
                              "test_b.gd::tb": ["src_game/cell.gd",
                                                "src_game/node.gd"]},
            "test_to_functions": {}, "script_to_files": {},
            "script_to_functions": {},
            "file_to_tests": {}, "file_to_scripts": {},
            "test_to_lines": cell_lines,
            "script_to_lines": {},
            "line_to_tests": {"src_game/cell.gd": {
                "5": ["test_a.gd::ta", "test_b.gd::tb"],
                "7": ["test_b.gd::tb"]},
                "src_game/node.gd": {"4": ["test_b.gd::tb"]}},
        }},
        "arms_state": {"gut_full": {"state": "valid", "bundles": 2,
                                    "pairs": 3}},
        "file_sha256": {},
        "target_files": ["src_game/cell.gd", "src_game/node.gd"],
        "tests_face": {},
        "instrumentable_lines": {"src_game/cell.gd": [5, 6, 7, 8],
                                 "src_game/node.gd": [4]},
        "notes": [],
    }


def _fake_arm_factory(monkeypatch, fresh_lines):
    monkeypatch.setattr(
        increment_runner, "prepare_attr_templates",
        lambda root, target_files, recipe, causal: {})
    monkeypatch.setattr(
        "godot_qa_toolkit.measurement.increment._run_increment_arm",
        lambda root, targets, recipe, rerun, timeout_s, causal:
        (0.01, fresh_lines, [],
         {"src_game/cell.gd": {5: "src_game/cell.gd::work"}}))


def test_increment_maintains_line_facts_and_views(tmp_path, monkeypatch):
    """增量：重跑身份行事实整体替换、其余身份保留；派生视图与反查从合并
    后行事实单源重算。"""
    root = tmp_path / "tree"
    (root / "tests").mkdir(parents=True)
    (root / "src_game").mkdir()
    (root / "src_game" / "cell.gd").write_text(TARGET_SRC)
    (root / "src_game" / "node.gd").write_text(TARGET2)
    (root / "tests" / "test_a.gd").write_text("extends GutTest\n")
    from godot_qa_toolkit.measurement import increment as inc_mod
    monkeypatch.setattr(inc_mod, "_inject_toolchain", lambda root, r: r)
    fresh = {"test_to_lines": {"test_a.gd::ta": {
        "src_game/cell.gd": [5, 6, 7]}}, "script_to_lines": {}}
    _fake_arm_factory(monkeypatch, fresh)
    baseline = _baseline_doc("rh")
    baseline["project_root"] = str(root)
    face = {}
    for gd in sorted((root / "tests").rglob("*.gd")):
        face[str(gd.relative_to(root))] = hashlib.sha256(
            gd.read_bytes()).hexdigest()
    baseline["tests_face"] = face
    baseline["file_sha256"] = {
        "src_game/cell.gd": hashlib.sha256(
            (root / "src_game" / "cell.gd").read_bytes()).hexdigest()}
    base_path = tmp_path / "base.json"
    base_path.write_text(json.dumps(baseline), encoding="utf-8")
    out = tmp_path / "inc.json"
    report = run_attribution_increment(
        root, [str(root / "src_game" / "cell.gd"),
               str(root / "src_game" / "node.gd")],
        {"recipe_hash": "rh"}, base_path, ["tests/unit/test_a.gd"], out,
        rerun_full_face_ratio=1.0)
    assert report["mode"] == "incremental"
    doc = json.loads(out.read_text(encoding="utf-8"))
    arm = doc["arms"]["gut_full"]
    # 重跑身份替换：test_a 新行事实（含新覆盖行 6/7）；保留身份 test_b 原样
    assert arm["test_to_lines"]["test_a.gd::ta"] == {
        "src_game/cell.gd": [5, 6, 7]}
    assert arm["test_to_lines"]["test_b.gd::tb"] == {
        "src_game/cell.gd": [5, 7], "src_game/node.gd": [4]}
    # 反查从合并行事实重算：行 6 现在属于 test_a
    assert arm["line_to_tests"]["src_game/cell.gd"]["6"] == ["test_a.gd::ta"]
    assert sorted(arm["line_to_tests"]["src_game/cell.gd"]["5"]) == [
        "test_a.gd::ta", "test_b.gd::tb"]
    # 派生 file 视图与行事实一致
    assert arm["test_to_files"]["test_a.gd::ta"] == ["src_game/cell.gd"]
    assert doc["instrumentable_lines"]["src_game/cell.gd"] == [5, 6, 7, 8]


def test_increment_syncs_coverage_store_or_invalidates(tmp_path, monkeypatch):
    """Coverage Store 同步：绑定漂移 → 旧投影条目显式失效；新映射再投影
    覆盖写回；映射不新鲜 → 投影拒绝（回退不过滤）。"""
    root = tmp_path / "tree"
    (root / "tests").mkdir(parents=True)
    (root / "src_game").mkdir()
    (root / "src_game" / "cell.gd").write_text(TARGET_SRC)
    (root / "tests" / "test_a.gd").write_text("extends GutTest\n")
    from godot_qa_toolkit.measurement import increment as inc_mod
    monkeypatch.setattr(inc_mod, "_inject_toolchain", lambda root, r: r)
    fresh = {"test_to_lines": {"test_a.gd::ta": {
        "src_game/cell.gd": [5, 6]}}, "script_to_lines": {}}
    _fake_arm_factory(monkeypatch, fresh)
    baseline = _baseline_doc("rh")
    baseline["project_root"] = str(root)
    # S2.1 M2：tests_face 须登记事实脚本（裸名归属内检目录一致）
    baseline["tests_face"] = {"tests/test_a.gd": hashlib.sha256(
        (root / "tests" / "test_a.gd").read_bytes()).hexdigest()}
    baseline["file_sha256"] = {
        "src_game/cell.gd": hashlib.sha256(
            (root / "src_game" / "cell.gd").read_bytes()).hexdigest()}
    base_path = tmp_path / "base.json"
    base_path.write_text(json.dumps(baseline), encoding="utf-8")
    # 基线态：先投影一份 store 条目（按旧行事实 {5}）
    target = root / "src_game" / "cell.gd"
    covered, err = coverlink.project_coverage_from_map(
        str(root), baseline, str(target), ["res://tests/test_a.gd"],
        "res://.gqt-attr-select/abc/", current_recipe_pristine="pristine-1",
        tests_face=baseline["tests_face"])
    assert err is None and covered == {5}
    # 增量刷新
    out = tmp_path / "inc.json"
    report = run_attribution_increment(
        root, [str(target), str(root / "src_game" / "node.gd")],
        {"recipe_hash": "rh"}, base_path, ["tests/unit/test_a.gd"], out,
        rerun_full_face_ratio=1.0)
    assert report["mode"] == "incremental"
    doc = json.loads(out.read_text(encoding="utf-8"))
    # 旧条目：绑定仍匹配（源码与测试面未变，projection binding 是基线态）
    # ——基线绑定在增量后依旧自洽（本场景行事实变化但绑定不变），条目
    # 读回仍是旧值 {5}：消费侧必须以「映射当前行事实」为准再投影覆盖。
    lines, err = coverlink.load_projected_coverage(
        str(root), str(target), "res://.gqt-attr-select/abc/",
        current_recipe_pristine="pristine-1",
        tests_face=baseline["tests_face"], current_doc=baseline)
    assert err is None and lines == {5}
    # 增量后映射再投影：行集更新为 {5,6}，条目被覆盖写回
    covered, err = coverlink.project_coverage_from_map(
        str(root), doc, str(target), ["res://tests/test_a.gd"],
        "res://.gqt-attr-select/abc/", current_recipe_pristine="pristine-1",
        tests_face=baseline["tests_face"])
    assert err is None and covered == {5, 6}
    lines, err = coverlink.load_projected_coverage(
        str(root), str(target), "res://.gqt-attr-select/abc/",
        current_recipe_pristine="pristine-1",
        tests_face=baseline["tests_face"], current_doc=doc)
    assert err is None and lines == {5, 6}
    # 绑定漂移场景：测试面变更后（增量会同步 tests_face），旧绑定失配 →
    # 显式失效
    (root / "tests" / "test_a.gd").write_text(
        "extends GutTest\n# changed\n")
    covered, err = coverlink.project_coverage_from_map(
        str(root), doc, str(target), ["res://tests/test_a.gd"],
        "res://.gqt-attr-select/abc/", current_recipe_pristine="pristine-1",
        tests_face=None)
    assert covered is None and "tests face drift" in err
    # 无法安全投影（映射不新鲜：tests_face 漂移未合并）→ 回退不过滤
    lines, err = coverlink.load_projected_coverage(
        str(root), str(target), "res://.gqt-attr-select/abc/",
        current_recipe_pristine="pristine-1", tests_face=None)
    assert lines is None and "tests face drift" in err


def test_increment_fallback_artifact_is_line_primary(tmp_path, monkeypatch):
    """全脸回退路径（new_prod_file）产出 /6 行级主真值产物（走真实
    run_measurement——FakeGodot）。"""
    import subprocess as sp
    project = _project(tmp_path)
    recipe = _recipe(project, tmp_path)
    targets = sorted(str(p) for p in (project / "src_game").glob("*.gd"))
    from godot_qa_toolkit.measurement import increment as inc_mod
    monkeypatch.setattr(inc_mod, "_inject_toolchain", lambda root, r: r)
    baseline = _baseline_doc(recipe["recipe_hash"])
    baseline["project_root"] = str(project)
    base_path = tmp_path / "base.json"
    base_path.write_text(json.dumps(baseline), encoding="utf-8")
    new_prod = project / "src_game" / "brand_new.gd"
    new_prod.write_text("extends RefCounted\nfunc x() -> void:\n\tpass\n")
    out = tmp_path / "inc.json"

    class _FakeRun:
        real = sp.run

        def __call__(self, *a, **k):
            argv = a[0]
            if "gut_cmdln.gd" in " ".join(argv):
                import re
                env = k.get("env") or {}
                xdg = env.get("XDG_DATA_HOME")
                if xdg:
                    pg = (project / "project.godot").read_text("utf-8")
                    name = re.search(
                        r'config/name\s*=\s*"([^"]+)"', pg).group(1)
                    dest = (Path(xdg) / "godot" / "app_userdata" / name)
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest = project
                for gd in project.glob("src_game/*.gd"):
                    src = gd.read_text(encoding="utf-8")
                    probes = sorted(set(re.findall(
                        r"__qa_cov_probe_m_[0-9a-f]{8}\[(\d+)\] = 1", src)),
                        key=int)
                    for m in re.finditer(
                            r'qa-coverage-attr-([0-9a-f-]{8,36})-" \+ '
                            r'str\(OS\.get_process_id\(\)\) \+ '
                            r'"-([0-9a-f]{8})\.txt', src):
                        uuid, key8 = m.group(1), m.group(2)
                        fn = "".join(f"fn {i} 0\n" for i in probes)
                        (dest /
                         f"qa-coverage-attr-{uuid}-555-{key8}.txt").write_text(
                            f"test 0\ncnt 0 {len(probes)}\n{fn}"
                            "ph 0 0 0 0\n", encoding="utf-8")
                hook = project / "addons" / "gqt_cov_pre_hook.gd"
                if hook.is_file():
                    for m in re.finditer(
                            r'qa-cov-attr-tests-([0-9a-f-]{8,36})-',
                            hook.read_text(encoding="utf-8")):
                        (dest /
                         f"qa-cov-attr-tests-{m.group(1)}-555.txt").write_text(
                            "slot 0\ttest\tres://tests/unit/"
                            "test_game.gd::test_ok\n", encoding="utf-8")

                class R:
                    returncode = 0
                    stdout = ""
                    stderr = ""

                return R()
            return _FakeRun.real(*a, **k)

    monkeypatch.setattr(sp, "run", _FakeRun())
    report = run_attribution_increment(
        project, targets, recipe, base_path, ["src_game/brand_new.gd"], out)
    assert report["mode"] == "new_prod_file_fallback"
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["schema_version"] == "test-attribution/7"
    arm = doc["arms"]["gut_full"]
    assert arm["test_to_lines"]["res://tests/unit/test_game.gd::test_ok"]
    # SEE-1604 D1：new_prod_file_fallback 必须把 classes.prod_new 并入重建
    # targets——新文件入探针面（旧行为"新文件永不进图"即 QA D1 缺陷）。
    assert "src_game/brand_new.gd" in doc["instrumentable_lines"]
    assert doc["instrumentable_lines"]["src_game/cell.gd"]
    assert doc["increment"]["increments_since_full"] == 0


# ------------------------------------------------- 硬化补盲（变异存活点）
# 增量合并契约的删除/替换语义：既有集成用例只覆盖「同身份替换 + 保留」，
# 未覆盖「删除脚本删边」与「重跑脚本内方法改名后旧身份清除」——用
# `_merge_arm` 直接钉死两条语义（IN1 / IN2 注入存活点）。


def test_merge_arm_drops_deleted_script_line_facts():
    """删除脚本 → 其 test 车道与 script 车道行事实连同反查一并删除。"""
    from godot_qa_toolkit.measurement.increment import _merge_arm

    arm = {
        "test_to_lines": {"test_b.gd::tb": {"a.gd": [3]}},
        "script_to_lines": {"test_b.gd": {"a.gd": [3]}},
    }
    merged = _merge_arm(arm,
                        {"test_to_lines": {}, "script_to_lines": {}},
                        dropped={"test_b.gd"})
    assert merged["test_to_lines"] == {}
    assert merged["script_to_lines"] == {}
    assert merged["line_to_tests"] == {}


def test_merge_arm_replaces_rerun_script_stale_method_identity():
    """重跑脚本按脚本名整体替换：该脚本内被改名/删除的方法旧身份不得残留
    （否则留下陈旧行事实造成过选面）。"""
    from godot_qa_toolkit.measurement.increment import _merge_arm

    arm = {
        "test_to_lines": {"test_a.gd::old": {"a.gd": [9]}},
        "script_to_lines": {},
    }
    fresh = {
        "test_to_lines": {"test_a.gd::new": {"a.gd": [4]}},
        "test_to_files": {"test_a.gd::new": ["a.gd"]},
        "script_to_lines": {},
    }
    merged = _merge_arm(arm, fresh, dropped=set())
    assert merged["test_to_lines"] == {"test_a.gd::new": {"a.gd": [4]}}
    assert merged["line_to_tests"] == {"a.gd": {"4": ["test_a.gd::new"]}}
