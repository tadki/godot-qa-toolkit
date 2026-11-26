"""SEE-1581 hardener — 变异硬化补钉（真值选面 / 增量 / 归属车道 / fn 分组）。

本文件只补「注入变异未被现有判据抓到」的缺口，判据仍全部走机器执行：
每个用例对应一处手工注入变异（在 `/tmp` 真副本内注入、pytest 判定），
断言直接钉住变异前后的可观察差异，不做实现内部复述。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from attribution_fixtures import write_map
from godot_qa_toolkit.measurement import increment as inc_mod
from godot_qa_toolkit.measurement import increment_runner as runner_mod
from godot_qa_toolkit.measurement import prepcache
from godot_qa_toolkit.measurement.shadowio import (
    _accumulate_attr_lines,
    _new_attr_rec,
)
from godot_qa_toolkit.measurement.attribution import (
    _arms_state,
    _tests_face_map,
    build_attribution_map,
)
from godot_qa_toolkit.mutation import executor, truth_select
from godot_qa_toolkit.mutation.ops import Mutant
from godot_qa_toolkit.mutation.runner import _auto_scan_tests_glob_meta

TARGET = "func add(a, b):\n\treturn a + b\n"


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "tests" / "unit").mkdir(parents=True)
    (proj / "tests" / "unit" / "test_hit.gd").write_text(
        "extends GutTest\nfunc test_ok() -> void:\n\tpass\n")
    (proj / "calc.gd").write_text(TARGET)
    return proj


def _map_doc(proj: Path) -> dict:
    return truth_select.load_map(
        proj / ".qa-cache" / "attribution-map.json")


# ------------------------------------------------------------ truth_select


def test_refresh_allows_dropped_scripts(tmp_path):
    """守恒校验的允许变化面：dropped_scripts 中的脚本消失不算丢身份。"""
    proj = _project(tmp_path)
    write_map(proj, {"calc.gd": ["test_hit.gd::test_ok"]})
    before = _map_doc(proj)
    before["arms"]["gut_full"]["test_to_files"][
        "test_other.gd::test_y"] = ["calc.gd"]
    after = json.loads(json.dumps(before))
    after["arms"]["gut_full"]["test_to_files"].pop("test_other.gd::test_y")
    after["increment"] = {"rerun_scripts": [], "dropped_scripts":
                          ["test_other.gd"]}
    truth_select.validate_refresh(before, after)  # 不得抛


def test_refresh_requires_script_level_identity(tmp_path):
    """身份比较以脚本名归一：`a.gd::t1` vs `a.gd::t2` 是同一身份。"""
    doc = {"arms": {"gut_full": {"test_to_files": {
        "res://tests/a.gd::t1": ["x.gd"]}}}}
    assert truth_select._script_names(doc) == {"res://tests/a.gd"}


def test_stale_recipe_hash_fails_loud(tmp_path):
    proj = _project(tmp_path)
    write_map(proj, {"calc.gd": ["test_hit.gd::test_ok"]})
    path = proj / ".qa-cache" / "attribution-map.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["recipe_hash_pristine"] = "0" * 64
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(truth_select.MutationSelectionError) as ei:
        truth_select.resolve_selection(proj, str(proj / "calc.gd"))
    assert "recipe_hash_stale" in ei.value.reason


def test_removed_test_script_marks_tests_face_stale(tmp_path):
    proj = _project(tmp_path)
    write_map(proj, {"calc.gd": ["test_hit.gd::test_ok"]})
    (proj / "tests" / "unit" / "test_hit.gd").unlink()
    with pytest.raises(truth_select.MutationSelectionError) as ei:
        truth_select.resolve_selection(proj, str(proj / "calc.gd"))
    assert "tests_face_drift" in ei.value.reason


def test_ambiguous_test_names_fail_loud(tmp_path):
    proj = _project(tmp_path)
    (proj / "tests" / "other").mkdir(parents=True)
    (proj / "tests" / "other" / "test_hit.gd").write_text(
        "extends GutTest\nfunc test_ok() -> void:\n\tpass\n")
    write_map(proj, {"calc.gd": ["test_hit.gd::test_ok"]})
    with pytest.raises(truth_select.MutationSelectionError) as ei:
        truth_select.resolve_selection(proj, str(proj / "calc.gd"))
    assert ei.value.reason == "ambiguous_test_names"


def test_map_path_prefers_explicit_over_env(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    explicit = write_map(proj, {"calc.gd": ["test_hit.gd::test_ok"]})
    other = tmp_path / "other.json"
    other.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(truth_select.ENV_MAP, str(other))
    assert truth_select.map_path(proj, explicit) == explicit
    # 显式参数不存在 → None（不回退环境变量之外的猜测）
    assert truth_select.map_path(proj, str(tmp_path / "missing.json")) is None


def test_selection_dir_keyed_by_script_set(tmp_path):
    proj = _project(tmp_path)
    a = truth_select.materialize_selection(
        str(proj), ["res://tests/unit/test_hit.gd"])
    b = truth_select.materialize_selection(
        str(proj), ["res://tests/unit/test_hit.gd",
                    "res://tests/unit/test_second.gd"])
    again = truth_select.materialize_selection(
        str(proj), ["res://tests/unit/test_hit.gd"])
    assert a != b and a == again           # 内容哈希命名 + 幂等复用


def test_selection_skips_non_res_scripts(tmp_path):
    proj = _project(tmp_path)
    sel = truth_select.materialize_selection(
        str(proj), ["res://tests/unit/test_hit.gd", "/abs/foreign.gd"])
    d = proj / sel.removeprefix("res://")
    assert sorted(p.name for p in d.iterdir()) == ["test_hit.gd"]


def test_runtime_ownership_rejects_ambiguous_test_name(tmp_path):
    proj = _project(tmp_path)
    (proj / "tests" / "unit" / "a.gd").write_text("extends GutTest\n")
    (proj / "tests" / "unit" / "b.gd").write_text("extends GutTest\n")
    doc = {"arms": {"gut_full": {
        "test_to_files": {"a.gd::test_ok": ["calc.gd"],
                          "b.gd::test_ok": ["calc.gd"]},
        "script_to_files": {}}}}
    owner = truth_select.RuntimeOwnership(str(proj), doc)
    assert owner.owner_of("test_ok") is None


def test_runtime_ownership_rejects_multi_path_script(tmp_path):
    proj = _project(tmp_path)
    (proj / "tests" / "other").mkdir(parents=True)
    (proj / "tests" / "other" / "test_hit.gd").write_text(
        "extends GutTest\n", encoding="utf-8")
    doc = {"arms": {"gut_full": {
        "test_to_files": {"test_hit.gd::test_ok": ["calc.gd"]},
        "script_to_files": {}}}}
    owner = truth_select.RuntimeOwnership(str(proj), doc)
    assert owner.owner_of("test_ok") is None


def test_ownership_returns_none_when_binding_missing(tmp_path, capsys):
    """A09：映射在但缺 file_sha256 绑定 → None（证书不产出，保守重跑）
    + 具名披露；绝不静态兜底。"""
    proj = _project(tmp_path)
    path = write_map(proj, {"calc.gd": ["test_hit.gd::test_ok"]})
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    doc["file_sha256"] = {}
    Path(path).write_text(json.dumps(doc), encoding="utf-8")
    owner = truth_select.ownership_for(str(proj), str(proj / "calc.gd"))
    assert owner is None
    err = capsys.readouterr().err
    # 绑定缺失走 resolve 异常路径（file_binding_missing_or_stale）→
    # 具名 resolution failed 披露；无映射则走 no-fresh 披露——两者均为
    # 具名保守披露，绝不静态兜底
    assert "conservative rerun" in err
    assert ("no fresh runtime mapping" in err
            or "resolution failed" in err)


def test_function_selection_passes_through_zero_ref(tmp_path):
    proj = _project(tmp_path)
    write_map(proj, {"calc.gd": []})
    sel = truth_select.resolve_selection(proj, str(proj / "calc.gd"))
    fn = truth_select.resolve_function_selection(
        proj, str(proj / "calc.gd"), "calc.gd::add", file_selection=sel)
    assert fn is sel and fn["zero_ref"] is True


def test_function_selection_respects_unavailable_file_selection(tmp_path):
    proj = _project(tmp_path)
    unavailable = {"source": "full_face", "reason": "no_attribution_map",
                   "zero_ref": False, "tests": [], "tests_glob": None,
                   "mapping": None}
    fn = truth_select.resolve_function_selection(
        proj, str(proj / "calc.gd"), "calc.gd::add",
        file_selection=unavailable, strict=False)
    assert fn is unavailable


def test_ledger_wall_total_sums_events():
    truth_select.reset_ledger()
    truth_select._record({"action": "increment", "wall_s": 1.25})
    truth_select._record({"action": "regenerate", "wall_s": 2.5})
    assert truth_select.ledger()["wall_s_total"] == 3.75
    truth_select.reset_ledger()


# --------------------------------------------------------------- increment


def test_fallback_reason_precedence_and_calibration():
    empty = {"test_changed": [], "test_new": [], "test_removed": [],
             "prod_changed": [], "prod_new": [], "unknown": []}
    assert inc_mod._fallback_reason(False, empty, 0, 5, 0.0, 0.5) == \
        "hash_mismatch_fallback"
    assert inc_mod._fallback_reason(True, empty, 5, 5, 0.0, 0.5) == \
        "calibration_due_fallback"
    assert inc_mod._fallback_reason(True, empty, 4, 5, 0.5, 0.5) == \
        "incremental"
    assert inc_mod._fallback_reason(True, empty, 0, 5, 0.51, 0.5) == \
        "rerun_surface_too_large_fallback"


def test_invalidation_includes_direct_static_consumers(tmp_path):
    """静态候选：tests/ 下 depth==1 直连消费者必须进失效面。"""
    root = tmp_path / "proj"
    (root / "tests" / "unit").mkdir(parents=True)
    (root / "src_game").mkdir()
    (root / "src_game" / "node.gd").write_text("extends Node\n")
    (root / "tests" / "unit" / "test_static.gd").write_text(
        'extends GutTest\nconst N = preload("res://src_game/node.gd")\n')
    baseline = {"arms": {"gut_full": {
        "test_to_files": {}, "test_to_functions": {},
        "script_to_files": {}, "script_to_functions": {},
        "file_to_tests": {"src_game/node.gd": []}, "file_to_scripts": {}}}}
    classes = {"test_changed": [], "test_new": [], "test_removed": [],
               "prod_changed": ["src_game/node.gd"], "prod_new": [],
               "unknown": []}
    inv = inc_mod.compute_invalidation(root, baseline, classes, index=None)
    assert "test_static.gd" in inv["rerun"]
    assert any(s.startswith("static:") for s in inv["sources"]["test_static.gd"])


def test_merged_tests_face_updates_and_drops(tmp_path):
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_new.gd").write_text("# new\n")
    face = inc_mod._merged_tests_face(
        root, {"tests_face": {"tests/test_gone.gd": "old",
                              "tests/test_new.gd": "old"}},
        {"test_changed": [], "test_new": ["tests/test_new.gd"],
         "test_removed": ["tests/test_gone.gd"]})
    assert "tests/test_gone.gd" not in face
    assert face["tests/test_new.gd"] != "old"


def test_merged_file_sha_rebinds_changed_prod(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "cell.gd").write_text("extends RefCounted\n")
    shas = inc_mod._merged_file_sha(
        root, {"file_sha256": {"src/cell.gd": "stale"}},
        {"prod_changed": ["src/cell.gd"], "prod_new": []})
    assert shas["src/cell.gd"] != "stale"


def test_increment_artifact_counts_since_full(tmp_path):
    art = inc_mod._build_increment_artifact(
        {"report_recipe_hash": "h", "recipe_hash_pristine": "p",
         "arms_state": {}, "notes": []},
        {"tests": 0, "scripts": 0, "files": 0, "pairs": 0,
         "function_pairs": 0},
        {"recipe_hash": "h"}, tmp_path,
        {"test_changed": [], "test_new": [], "test_removed": [],
         "prod_changed": [], "prod_new": [], "unknown": []},
        [], {"dropped": set(), "sources": {}}, 3, 0.1)
    assert art["increment"]["increments_since_full"] == 4


def _increment_project(tmp_path: Path) -> tuple[Path, dict, list[str], Path]:
    """最小增量工程 + 基线产物（不触碰 godot：_inject_toolchain 由用例打桩）。"""
    root = tmp_path / "proj"
    (root / "tests" / "unit").mkdir(parents=True)
    (root / "src_game").mkdir()
    (root / "src_game" / "node.gd").write_text("extends Node\n")
    (root / "tests" / "unit" / "test_known.gd").write_text(
        "extends GutTest\n")
    (root / ".gutconfig.json").write_text(json.dumps(
        {"dirs": ["res://tests/"], "include_subdirs": True,
         "prefix": "test_"}))
    feats = root / "addons" / "gqt_gherkin" / "features"
    feats.mkdir(parents=True)
    (feats / "demo.feature").write_text(
        "Feature: F\n  Scenario Outline: s\n    Then hp is <left>\n"
        "    # types: <hp>:int, <left>:numeric\n    Examples:\n"
        "      | hp | left |\n      | 10 | 7    |\n")
    from gherkin_fixtures import ensure_addon_metadata
    ensure_addon_metadata(root)
    from godot_qa_toolkit.measurement.recipe import build_recipe

    recipe, error = build_recipe(root)
    assert error is None, error
    baseline = {
        "schema_version": "test-attribution/7",
        "test_verdicts": {},
        "report_recipe_hash": recipe["recipe_hash"],
        "recipe_hash_pristine": recipe["recipe_hash"],
        "arms": {"gut_full": {
            "test_to_files": {"test_known.gd::t": ["src_game/node.gd"]},
            "test_to_functions": {"test_known.gd::t": ["src_game/node.gd::x"]},
            "script_to_files": {}, "script_to_functions": {},
            "file_to_tests": {"src_game/node.gd": ["test_known.gd::t"]},
            "file_to_scripts": {},
            "test_to_lines": {"test_known.gd::t": {"src_game/node.gd": [2]}},
            "script_to_lines": {},
            "line_to_tests": {"src_game/node.gd": {"2": ["test_known.gd::t"]}},
        }},
        "arms_state": {"gut_full": {"state": "valid", "bundles": 1,
                                    "pairs": 1}},
        "instrumentable_lines": {"src_game/node.gd": [2]},
        "increment": {"increments_since_full": 3},
    }
    base_path = tmp_path / "base.json"
    base_path.write_text(json.dumps(baseline), encoding="utf-8")
    targets = [str(root / "src_game" / "node.gd")]
    return root, recipe, targets, base_path


def _fake_arm(monkeypatch):
    monkeypatch.setattr(inc_mod, "_inject_toolchain", lambda root, r: r)
    monkeypatch.setattr(
        inc_mod, "_run_increment_arm",
        lambda root, targets, recipe, rerun, timeout_s, causal:
        (0.01,
         {"test_to_lines": {"test_known.gd::t": {"src_game/node.gd": [2]}},
          "script_to_lines": {}},
         [],
         {"src_game/node.gd": {2: "src_game/node.gd::x"}}))


def test_increment_artifact_advances_calibration_counter(tmp_path,
                                                         monkeypatch):
    root, recipe, targets, base_path = _increment_project(tmp_path)
    _fake_arm(monkeypatch)
    out = tmp_path / "inc.json"
    report = inc_mod.run_attribution_increment(
        root, targets, recipe, base_path, ["tests/unit/test_known.gd"], out,
        rerun_full_face_ratio=1.0)
    assert report["mode"] == "incremental"
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["increment"]["increments_since_full"] == 4


def test_increment_rerun_excludes_scripts_outside_baseline(tmp_path,
                                                           monkeypatch):
    root, recipe, targets, base_path = _increment_project(tmp_path)
    _fake_arm(monkeypatch)
    monkeypatch.setattr(
        inc_mod, "compute_invalidation",
        lambda root, baseline, classes, index: {
            "rerun": {"test_known.gd", "ghost.gd"}, "dropped": set(),
            "sources": {}, "explicit_rerun": set()})
    out = tmp_path / "inc2.json"
    report = inc_mod.run_attribution_increment(
        root, targets, recipe, base_path, ["tests/unit/test_known.gd"], out,
        rerun_full_face_ratio=1.0)
    assert report["rerun_scripts"] == ["test_known.gd"]


def test_increment_filters_static_candidates_outside_baseline(tmp_path):
    """静态候选可能指向基线外脚本：known 闸必须过滤（不凭空圈定）。"""
    proj = tmp_path / "proj"
    (proj / "tests" / "unit").mkdir(parents=True)
    (proj / "src_game").mkdir()
    (proj / "src_game" / "node.gd").write_text("extends Node\n")
    (proj / "tests" / "unit" / "test_known.gd").write_text(
        "extends GutTest\n")
    (proj / "tests" / "unit" / "test_late.gd").write_text(
        'extends GutTest\nconst N = preload("res://src_game/node.gd")\n')
    baseline = {"arms": {"gut_full": {
        "test_to_files": {"test_known.gd::t": ["src_game/node.gd"]},
        "test_to_functions": {}, "script_to_files": {},
        "script_to_functions": {},
        "file_to_tests": {"src_game/node.gd": ["test_known.gd::t"]},
        "file_to_scripts": {}}}}
    classes = inc_mod.classify_changes(
        proj, ["src_game/node.gd"], baseline)
    inv = inc_mod.compute_invalidation(proj, baseline, classes, index=None)
    assert "test_late.gd" in inv["rerun"]
    known = {"test_known.gd"}
    rerun = {n for n in inv["rerun"] if n in known}
    assert "test_late.gd" not in rerun


# --------------------------------------------------------- increment_runner


def _gd_project(tmp_path: Path) -> tuple[Path, list[str]]:
    root = tmp_path / "proj"
    (root / "addons" / "gut").mkdir(parents=True)
    (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
    (root / "project.godot").write_text(
        'config_version=5\n[application]\nconfig/name="incr-hard"\n')
    (root / ".gutconfig.json").write_text(json.dumps(
        {"dirs": ["res://tests/"], "include_subdirs": True,
         "prefix": "test_"}))
    (root / "tests" / "unit").mkdir(parents=True)
    (root / "tests" / "unit" / "test_game.gd").write_text(
        "extends GutTest\nfunc test_ok() -> void:\n\tpass\n")
    feats = root / "addons" / "gqt_gherkin" / "features"
    feats.mkdir(parents=True)
    (feats / "demo.feature").write_text(
        "Feature: F\n  Scenario Outline: s\n    Then hp is <left>\n"
        "    # types: <hp>:int, <left>:numeric\n    Examples:\n"
        "      | hp | left |\n      | 10 | 7    |\n")
    game = root / "src_game"
    game.mkdir()
    (game / "cell.gd").write_text(
        "extends Node\n\n\nfunc later() -> void:\n\tcall_deferred(\"_noop\")\n")
    from gherkin_fixtures import ensure_addon_metadata
    ensure_addon_metadata(root)
    from godot_qa_toolkit.measurement.recipe import build_recipe

    recipe, error = build_recipe(root)
    assert error is None, error
    return root, sorted(str(p) for p in (root / "src_game").glob("*.gd"))


def test_attr_templates_causal_only_rewrites_when_asked(tmp_path):
    root, targets = _gd_project(tmp_path)
    from godot_qa_toolkit.measurement.recipe import build_recipe
    recipe, _ = build_recipe(root)
    plain = runner_mod.prepare_attr_templates(root, targets, recipe, False)
    causal = runner_mod.prepare_attr_templates(root, targets, recipe, True)
    rel = next(iter(plain))
    assert "__qgta_" not in plain[rel]["template"]
    assert "__qgta_" in causal[rel]["template"]


def test_execute_rerun_causal_gates_ctx_and_preserves_source(tmp_path):
    root, targets = _gd_project(tmp_path)
    from godot_qa_toolkit.measurement.recipe import build_recipe
    recipe, _ = build_recipe(root)
    originals = {p: Path(p).read_text(encoding="utf-8")
                 for p in targets}
    seen: list[dict] = []

    def fake_run(project_root, gut_args, timeout_s, env=None, gate_root=None):
        seen.append({
            "ctx": (Path(project_root) / "addons" / "gqt_cov_ctx.gd").is_file(),
            "flusher": (Path(project_root) / "addons" /
                        "gqt_cov_flusher.gd").read_text(encoding="utf-8"),
            "src": {p: Path(p).read_text(encoding="utf-8") for p in targets},
        })

        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R(), 0.0

    orig = runner_mod._run_one_gut
    runner_mod._run_one_gut = fake_run
    try:
        for causal in (False, True):
            templates = runner_mod.prepare_attr_templates(
                root, targets, recipe, causal)
            results, xdg = runner_mod.execute_rerun_scripts(
                root, ["res://tests/"], ["test_game.gd"], 60, templates,
                causal, "uuid-hard")
            for x in xdg:
                shutil.rmtree(x, ignore_errors=True)
            assert [r["rc"] for r in results] == [0]
    finally:
        runner_mod._run_one_gut = orig
    assert seen[0]["ctx"] is False and seen[1]["ctx"] is True
    for entry in seen:
        assert "gqt_cov_attr" in entry["flusher"]
    for p, text in originals.items():
        assert Path(p).read_text(encoding="utf-8") == text


def test_execute_rerun_reports_script_failure(tmp_path):
    root, targets = _gd_project(tmp_path)
    from godot_qa_toolkit.measurement.recipe import build_recipe
    recipe, _ = build_recipe(root)
    templates = runner_mod.prepare_attr_templates(root, targets, recipe,
                                                 False)

    def boom(*a, **k):
        raise RuntimeError("gut exploded")

    orig = runner_mod._run_one_gut
    runner_mod._run_one_gut = boom
    try:
        results, _xdg = runner_mod.execute_rerun_scripts(
            root, ["res://tests/"], ["test_game.gd"], 60, templates,
            False, "uuid-fail")
    finally:
        runner_mod._run_one_gut = orig
    assert results[0]["rc"] is None
    assert "gut exploded" in (results[0]["error"] or "")


def test_attr_templates_use_prep_cache(tmp_path, monkeypatch):
    root, targets = _gd_project(tmp_path)
    from godot_qa_toolkit.measurement.recipe import build_recipe
    recipe, _ = build_recipe(root)
    sentinel = {"instrumented_template": "CACHED_TEMPLATE",
                "probe_lines": [9], "registry": {"entries": []}}
    monkeypatch.setattr(prepcache, "load_entry", lambda *a, **k: sentinel)
    out = runner_mod.prepare_attr_templates(root, targets, recipe, False)
    rel = next(iter(out))
    assert out[rel]["template"] == "CACHED_TEMPLATE"


# ------------------------------------------------------------ orchestrator


def _rec(seen, cnt, fn=None, unattr=None, phases=(0, 0, 0, 0)):
    return {"seen": set(seen), "cnt": dict(cnt),
            "fn": {k: set(v) for k, v in (fn or {}).items()},
            "unattr_counts": dict(unattr or {}), "phases": list(phases)}


def test_test_lane_wins_over_conflicting_script_slot():
    # 槽位 0 同时出现在两张注册表：行事实只归 test 车道（test 优先，
    # 不按车道猜测重复归属）
    attr = {"x.gd": {1: _rec({0}, {0: 5}, fn={0: {0}})}}
    m = build_attribution_map(
        "gut_full", attr,
        ({1: {0: "res://tests/a.gd::t"}}, {1: {0: "res://tests/b.gd"}}),
        probe_lines_by_rel={"x.gd": [3]},
        block_map_by_rel={"x.gd": {3: [3, 4]}},
        fid_by_line_by_rel={"x.gd": {}})
    assert m["test_to_lines"] == {"res://tests/a.gd::t": {"x.gd": [3, 4]}}
    assert m["script_to_lines"] == {}
    assert m["test_to_files"] == {"res://tests/a.gd::t": ["x.gd"]}
    assert m["script_to_files"] == {}


def test_hotspot_limit_is_enforced():
    attr = {"x.gd": {1: _rec({}, {}, unattr={0: 3, 1: 1})}}
    m = build_attribution_map("gut_full", attr, ({1: {}}, {1: {}}),
                              hotspot_limit=1)
    assert len(m["unattributed_hotspots"]) == 1
    assert m["unattributed_hotspots"][0]["probe_id"] == 0


def test_empty_lanes_rate_is_none_not_zero():
    m = build_attribution_map("gut_full", {}, ({}, {}))
    assert m["lanes"]["test"]["rate"] is None
    assert m["unattributed_phases"]["bootstrap"]["rate"] is None


def test_fn_edge_without_resolved_fid_is_not_guessed():
    # 行事实覆盖的行在 fid 行映射中无归属：函数视图不猜（行事实仍在）
    attr = {"x.gd": {1: _rec({0}, {0: 2}, fn={0: {0}})}}
    m = build_attribution_map(
        "gut_full", attr,
        ({1: {0: "res://tests/a.gd::t"}}, {1: {}}),
        probe_lines_by_rel={"x.gd": [3]},
        block_map_by_rel={"x.gd": {3: [3]}},
        fid_by_line_by_rel={"x.gd": {}})
    assert m["test_to_lines"] == {"res://tests/a.gd::t": {"x.gd": [3]}}
    assert m["test_to_functions"] == {}
    # probe 越界同样不猜：探针 manifest 外的 fn 对丢弃
    attr2 = {"x.gd": {1: _rec({0}, {0: 2}, fn={99: {0}})}}
    m2 = build_attribution_map(
        "gut_full", attr2,
        ({1: {0: "res://tests/a.gd::t"}}, {1: {}}),
        probe_lines_by_rel={"x.gd": [3]},
        block_map_by_rel={"x.gd": {3: [3]}},
        fid_by_line_by_rel={"x.gd": {3: "x.gd::f"}})
    assert m2["test_to_lines"] == {}
    assert m2["test_to_functions"] == {}


def test_arms_state_pairs_count_reflects_edges():
    states = _arms_state(({
        "arm": "gut_full", "state": "valid", "bundles": 1,
        "attribution": {"test_to_files": {"a.gd::t": ["x"], "b.gd::u": ["y"]}},
    },))
    assert states["gut_full"]["pairs"] == 2


def test_attr_lines_accumulate_same_slot_and_probe():
    rec = _new_attr_rec()
    _accumulate_attr_lines(rec, "cnt 3 2\ncnt 3 5\n")
    assert rec["cnt"][3] == 7
    _accumulate_attr_lines(rec, "unattr 4 2\nunattr 4 3\n")
    assert rec["unattr_counts"][4] == 5
    _accumulate_attr_lines(rec, "ph 1 0 0 0\nph 2 0 0 0\n")
    assert rec["phases"][0] == 3


def test_tests_face_map_hashes_test_scripts(tmp_path):
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_a.gd").write_text("# a\n")
    face = _tests_face_map(root)
    assert list(face) == ["tests/test_a.gd"]
    assert len(face["tests/test_a.gd"]) == 64


# ---------------------------------------------------------- fn grouping layer


def _fn_project(tmp_path: Path) -> tuple[Path, Path]:
    proj = _project(tmp_path)
    (proj / "tests" / "unit" / "test_second.gd").write_text(
        "extends GutTest\nfunc test_ok() -> void:\n\tpass\n")
    (proj / "calc.gd").write_text(
        "func add(a, b):\n\tif a > b:\n\t\treturn a - b\n\treturn a + b\n"
        "\n\nfunc mul(a, b):\n\treturn a * b\n")
    write_map(proj, {"calc.gd": ["test_hit.gd::test_ok",
                                 "test_second.gd::test_ok"]})
    path = proj / ".qa-cache" / "attribution-map.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["arms"]["gut_full"]["test_to_functions"] = {
        "test_hit.gd::test_ok": ["calc.gd::add"],
        "test_second.gd::test_ok": ["calc.gd::mul"],
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    return proj, proj / "calc.gd"


def _mutant(path: Path, line: int, sid: str) -> Mutant:
    return Mutant(file=str(path), line=line, column=0, operator="ROR",
                  original=">", mutated="<", kind="ROR", stable_id=sid)


def test_fn_group_chunks_split_by_function(tmp_path):
    proj, target = _fn_project(tmp_path)
    sel = truth_select.resolve_selection(proj, str(target))
    src = target.read_text(encoding="utf-8")
    chunks, meta = executor._fn_group_chunks(
        str(target), str(proj), sel["tests_glob"], None,
        [_mutant(target, 2, "m-add"), _mutant(target, 8, "m-mul")],
        src, None, None, 60, 10, False, None,
        run_id="r1", identity=None, worker_channel=None,
        selection_meta=sel, total_sites=2)
    assert len(chunks) == 2 and len(meta) == 2
    by_fn = {m["function_id"]: m for m in meta}
    assert set(by_fn) == {"calc.gd::add", "calc.gd::mul"}
    assert by_fn["calc.gd::add"]["source"] == "function_mapping"
    # 每组只带自己的变异体，且 glob 是映射选面而非文件级并集
    globs = {}
    for ch in chunks:
        assert len(ch["retry_ids"]) == 1
        globs[ch["retry_ids"][0]] = ch["tests_glob"]
    assert globs["m-add"] != globs["m-mul"]
    assert all(g and g.startswith("res://.gqt-attr-select/")
               for g in globs.values())
    assert all(g != sel["tests_glob"] for g in globs.values())


def test_fn_group_module_scope_mutant_falls_back_to_file_glob(tmp_path):
    proj, target = _fn_project(tmp_path)
    sel = truth_select.resolve_selection(proj, str(target))
    src = target.read_text(encoding="utf-8") + "\nconst X = 1\n"
    chunks, meta = executor._fn_group_chunks(
        str(target), str(proj), sel["tests_glob"], None,
        [_mutant(target, 11, "m-module")],
        src, None, None, 60, 10, False, None,
        run_id="r2", identity=None, worker_channel=None,
        selection_meta=sel, total_sites=1)
    assert meta[0]["source"] == "file_fallback"
    assert meta[0]["reason"] == "module_scope"
    assert chunks[0]["tests_glob"] == sel["tests_glob"]


def test_fid_of_line_respects_both_bounds():
    registry = {"entries": [
        {"fid": "a.gd::f", "instrumentable": True,
         "start_line": 2, "end_line": 4},
        {"fid": "a.gd::ghost", "instrumentable": False,
         "start_line": 6, "end_line": 8},
    ]}
    assert executor._fid_of_line(registry, 3) == "a.gd::f"
    assert executor._fid_of_line(registry, 5) is None
    assert executor._fid_of_line(registry, 7) is None


def test_chunk_selection_disclosure_reaches_serial_summary():
    results = [{"summary": {"file": "a.gd"}}]
    chunks = [{"file": "a.gd", "selection_mode": "fn",
               "selection_meta": {"source": "runtime_mapping",
                                  "tests": ["res://tests/a.gd"]}}]
    executor._attach_chunk_selection(results, chunks)
    assert results[0]["summary"]["test_selection"]["source"] == \
        "runtime_mapping"
    assert results[0]["summary"]["selection_mode"] == "fn"


def test_zero_ref_terminal_requires_sites(tmp_path):
    target = tmp_path / "calc.gd"
    target.write_text(TARGET)
    assert executor._zero_ref_terminal(
        str(target), 0, {"zero_ref": True}) is None


def test_serial_gate_never_shortcuts_fn_mode(tmp_path, monkeypatch):
    """fn 级分组的 per-group glob 依赖编排分片；串行快路径不得截走。"""
    proj = _project(tmp_path)
    (proj / "calc.gd").write_text(
        "func add(a, b):\n\tif a > b:\n\t\treturn a - b\n\treturn a + b\n")
    write_map(proj, {"calc.gd": ["test_hit.gd::test_ok"]})
    called: list[str] = []
    monkeypatch.setattr(executor, "run_mutation",
                        lambda *a, **k: called.append("serial") or {
                            "ok": True, "summary": {}})
    ret, glob, meta = executor._single_file_serial_gate(
        [str(proj / "calc.gd")], str(proj), budget=50, timeout_s=60,
        tests_glob=None, jobs=10, dry_run=False,
        binary_kind=None, timing_store=None, mutation_warning=None,
        timeout_factor=None, mutate_all=False, worker_channel=None,
        select_mode="fn")
    assert ret is None and glob and meta["source"] == "runtime_mapping"
    assert called == []


def test_killed_certificate_discloses_ownership_source(tmp_path):
    """kill 证书必须带归属来源两态披露（runtime_mapping ≠ structural）。"""
    from godot_qa_toolkit.mutation.runner import _attach_killed_certificate
    proj = _project(tmp_path)
    killer = proj / "tests" / "unit" / "test_hit.gd"

    class _Owner:
        source = "runtime_mapping"

        def owner_of(self, name):
            return "res://tests/unit/test_hit.gd"

    verdict: dict = {"verdict": "killed"}
    tail = "* test_ok\n\t[Failed]\n"
    _attach_killed_certificate(verdict, tail, set(), lambda: _Owner(),
                               str(proj))
    assert verdict["killed_by"][0]["ownership_source"] == "runtime_mapping"
    assert verdict["killed_by"][0]["script_path"] == \
        "res://tests/unit/test_hit.gd"
    assert killer.is_file()


def test_killed_certificate_absent_without_owner(tmp_path):
    from godot_qa_toolkit.mutation.runner import _attach_killed_certificate
    proj = _project(tmp_path)
    verdict: dict = {"verdict": "killed"}
    _attach_killed_certificate(verdict, "* test_ok\n\t[Failed]\n", set(),
                               lambda: None, str(proj))
    assert "killed_by" not in verdict


def test_zero_ref_stderr_disclosure(tmp_path, capsys):
    proj = _project(tmp_path)
    write_map(proj, {"calc.gd": []})
    glob, meta = _auto_scan_tests_glob_meta(
        str(proj / "calc.gd"), str(proj), select_mode="file")
    assert glob is None and meta["zero_ref"] is True
    assert "says no test touches" in capsys.readouterr().err
