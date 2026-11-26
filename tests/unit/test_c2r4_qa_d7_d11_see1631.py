"""SEE-1631 C2-R4：QA D7/D8/D9/D10 + §SPEC-029/031 的行为判据。

D8  增量 verdict 重算逐测试/逐 scenario（通过身份不得随脚本/feature 级
    rc 整体标红；不可归因保守全红）；
D9  gherkin_dropped 无条件消费（只删不重跑轮次同样剔除）；
D7  cold/worker 车道失败 ⊆ 基线红 → 真实存活（beacon/降级冷腿升级通道；
    新失败仍 killed——kill 判定不削弱）；
D10/§SPEC-030 同名 basename 碰撞按完整脚本路径消歧 + 碰撞具名披露 +
    空 script 红键不入表；
§SPEC-031 分类清单（production/non_production/test_face + unclassified
    fail-loud + autoload 交叉校验）；
§SPEC-029 全集对账/新增生产检出/project.godot 与工具链进指纹/显式 map
    自刷/不可归因 → 具名全量。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from godot_qa_toolkit.measurement.increment import (
    _merged_test_verdicts,
    _verdict_gherkin_rerun,
    _verdict_recompute_rerun,
)
from godot_qa_toolkit.mutation.ops import Mutant


@pytest.fixture(autouse=True)
def _reset_runner_globals():
    """d7 冷车道单测断言全局缺省——真机冒烟文件的残留会翻转判定。"""
    yield
    from godot_qa_toolkit.mutation import runner as _runner_mod

    _runner_mod._BASELINE_RED_MAP = set()
    _runner_mod._FACE_SCRIPT_BY_BASENAME = {}
from godot_qa_toolkit.mutation.reporting import (
    _classify_mutant_run,
    _classify_worker_leg_impl,
)


def _mk_mutant() -> Mutant:
    return Mutant(file="/p/systems/calc.gd", line=9, column=18,
                  operator="AOR", original="+", mutated="-", kind="AOR")


# --------------------------------------------------------------------------
# D8：逐测试/逐 scenario 重算
# --------------------------------------------------------------------------

def test_d8_gut_rerun_per_test_not_whole_script():
    """重跑腿 rc=1、GUT 账 Failing=1：仅失败名标红，脚本内通过的测试
    保持 green（D8 根因回归面——rc!=0 不得整体标红）。"""
    merged = {
        "test_stats.gd::test_mean_basic": {"verdict": "green"},
        "test_stats.gd::test_mean_empty": {"verdict": "green"},
        "test_stats.gd::test_clamp_pct": {"verdict": "green"},
    }
    per_script = {"test_stats.gd": (1, {"test_mean_basic"})}
    _verdict_recompute_rerun(merged, per_script)
    assert merged["test_stats.gd::test_mean_basic"]["verdict"] == "red"
    assert merged["test_stats.gd::test_mean_basic"]["classification"] == "new"
    assert merged["test_stats.gd::test_mean_empty"] == {"verdict": "green"}
    assert merged["test_stats.gd::test_clamp_pct"] == {"verdict": "green"}


def test_d8_gut_rerun_unparseable_failure_conservative_red():
    """失败名空且 rc!=0（输出不可解析/崩溃）→ 保守整脚本红（不得放绿）。"""
    merged = {
        "test_a.gd::test_x": {"verdict": "green"},
        "test_a.gd::test_y": {"verdict": "green"},
    }
    _verdict_recompute_rerun(merged, {"test_a.gd": (1, set())})
    assert merged["test_a.gd::test_x"]["verdict"] == "red"
    assert merged["test_a.gd::test_y"]["verdict"] == "red"


def test_d8_gherkin_per_scenario_not_whole_feature():
    """重跑腿 rc=1 且 failing_scenarios 在场：仅失败 scenario 红，通过的
    scenario 保持 green（qa_extra 4 scenario 整体标红的 D8 回归面）。"""
    merged = {
        "gherkin://feature/qa_extra::even": {"verdict": "green"},
        "gherkin://feature/qa_extra::odd": {"verdict": "green"},
        "gherkin://feature/qa_extra::mean": {"verdict": "green"},
    }
    fresh = {"executions": [{"rc": 1, "out": "...",
                             "failing_scenarios": ["mean"]}]}
    _verdict_gherkin_rerun(merged, fresh, gherkin_rerun={"qa_extra"})
    assert merged["gherkin://feature/qa_extra::mean"]["verdict"] == "red"
    assert merged["gherkin://feature/qa_extra::even"] == {"verdict": "green"}
    assert merged["gherkin://feature/qa_extra::odd"] == {"verdict": "green"}


def test_d8_gherkin_unattributable_conservative_red():
    """旧形态账（failing_scenarios 缺席）且 rc!=0 → 保守整键红。"""
    merged = {
        "gherkin://feature/qa_extra::even": {"verdict": "green"},
    }
    fresh = {"executions": [{"rc": 1, "out": "..."}]}
    _verdict_gherkin_rerun(merged, fresh, gherkin_rerun={"qa_extra"})
    assert merged["gherkin://feature/qa_extra::even"]["verdict"] == "red"


# --------------------------------------------------------------------------
# D9：gherkin_dropped 无条件消费（见 C2-R3 文件迁移用例）
# --------------------------------------------------------------------------

def test_d9_dropped_purged_increment_shape():
    from godot_qa_toolkit.measurement.increment import _merged_test_verdicts

    baseline = {"test_verdicts": {
        "gherkin://feature/qa_gen_07::s0_01": {"verdict": "green"},
        "gherkin://feature/keep::s0_01": {"verdict": "green"},
    }}
    merged = _merged_test_verdicts(
        baseline, [], set(), set(), set(), None, set(),
        gherkin_dropped={"qa_gen_07"})
    assert "gherkin://feature/qa_gen_07::s0_01" not in merged
    assert "gherkin://feature/keep::s0_01" in merged


# --------------------------------------------------------------------------
# D7：失败 ⊆ 基线红 → 真实存活（两车道；kill 判定不削弱）
# --------------------------------------------------------------------------

def test_d7_cold_no_skip_primitive_red_subset_survived():
    """beacon/降级冷腿（无 skip 原语）：失败集 ⊆ 基线红 **且整面执行完**
    （Run Summary 在场）→ survived。"""
    tail = ("res://tests/unit/test_red.gd\n* test_red_always_fails\n"
            "[Failed] test_red_always_fails\nRan 2 tests.\n"
            "Run Summary\n")
    rec = _classify_mutant_run(
        _mk_mutant(), rc=1, tail=tail,
        baseline_failures={"test_red_always_fails"}, baseline_dirty=True)
    assert rec["verdict"] == "survived"
    assert "pre-existing red" in rec["reason"]
    assert rec["pre_existing_only"] is True


def test_d7_cold_no_full_face_evidence_stays_suspect():
    """反例（Leader 复核）：失败 ⊆ 基线红但无收尾汇总（腿中止——绿杀手
    可能没执行）→ 保持 suspect（不可判真实存活）。"""
    tail = ("res://tests/unit/test_red.gd\n* test_red_always_fails\n"
            "[Failed] test_red_always_fails\n")  # 无 Run Summary
    rec = _classify_mutant_run(
        _mk_mutant(), rc=1, tail=tail,
        baseline_failures={"test_red_always_fails"}, baseline_dirty=True)
    assert rec["verdict"] == "suspect"
    assert rec.get("pre_existing_only") is None


def test_d7_cold_new_failure_still_killed():
    tail = ("res://tests/unit/test_a.gd\n* test_new\n"
            "[Failed] test_new\n")
    rec = _classify_mutant_run(
        _mk_mutant(), rc=1, tail=tail,
        baseline_failures={"test_red_always_fails"}, baseline_dirty=True)
    assert rec["verdict"] == "killed"


def test_d7_worker_red_subset_without_evidence_stays_suspect():
    """worker 腿无整面证据（无 tail/无选面规模）→ 失败 ⊆ 基线红保持
    suspect（Leader 复核修正；跳过生效走 skip_red_info 具名通道）。"""
    class Leg:
        gut_ran = True
        fail_count = 1
        tests_ran = 2
        hash_ok = True
        failing_tests = {"test_red_always_fails"}
        failing_scripts = set()
        skip_red_info = None
        allow_info = None

    rec = _classify_worker_leg_impl(
        _mk_mutant(), Leg(),
        baseline_failures={"test_red_always_fails"}, baseline_dirty=True,
        ownership_fn=lambda *a, **k: None, project_root="/p",
        original_src="src")
    assert rec["verdict"] == "suspect"


# --------------------------------------------------------------------------
# D10/§SPEC-030：同名碰撞消歧 + 披露 + 空 script 键
# --------------------------------------------------------------------------

def _dup_arms() -> tuple[dict, dict]:
    gut = {
        "arm": "gut_full", "state": "valid",
        "baseline_failures_meta": {"pairs": [
            ["res://tests/nested/test_dup.gd", "test_same_name_probe"]]},
        "test_failures": {"observed": [], "observed_pairs": [], "new": [],
                          "new_pairs": [], "instrumentation_intolerant": []},
        "attribution": {"test_to_lines": {
            "res://tests/unit/test_dup.gd::test_same_name_probe":
                {"src/calc.gd": [9]},
            "res://tests/nested/test_dup.gd::test_same_name_probe":
                {"src/calc.gd": [32]},
        }},
    }
    return (gut,)


def test_d10_collision_disambiguated_by_full_path():
    from godot_qa_toolkit.measurement.attribution import _test_verdict_table

    table = _test_verdict_table(_dup_arms())
    unit = "res://tests/unit/test_dup.gd::test_same_name_probe"
    nested = "res://tests/nested/test_dup.gd::test_same_name_probe"
    assert table[unit] == {"verdict": "green"}, (
        "另一目录同名绿测不得并入红身份")
    assert table[nested]["verdict"] == "red"
    assert table[nested]["classification"] == "pre_existing"
    assert table[nested]["script"] == "res://tests/nested/test_dup.gd"


def test_d10_collision_disclosed_in_artifact_key():
    from godot_qa_toolkit.measurement.attribution import (
        _verdict_identity_collisions,
    )

    # 单候选（无碰撞）→ 披露为空
    solo = {
        "arm": "gut_full", "state": "valid",
        "baseline_failures_meta": {"pairs": [
            ["res://tests/unit/test_red.gd", "test_red_always_fails"]]},
        "test_failures": {"observed_pairs": [], "new_pairs": [],
                          "instrumentation_intolerant": []},
        "attribution": {"test_to_lines": {
            "res://tests/unit/test_red.gd::test_red_always_fails":
                {"src/calc.gd": [9]},
            "res://tests/unit/test_green.gd::test_green":
                {"src/calc.gd": [3]},
        }},
    }
    assert _verdict_identity_collisions((solo,)) == []


def test_d10_empty_script_red_key_not_emitted():
    """raw 空 script 红键（解析碎片）不入表、具名披露（不得默认记 new）。"""
    from godot_qa_toolkit.measurement.attribution import (
        _test_verdict_table, _verdict_identity_collisions,
    )

    arms = ({
        "arm": "gut_full", "state": "valid",
        "baseline_failures_meta": {"pairs": []},
        "test_failures": {"observed_pairs": ["::test_fragment"],
                          "new_pairs": [], "instrumentation_intolerant": []},
        "attribution": {"test_to_lines": {}},
    },)
    table = _test_verdict_table(arms)
    assert "::test_fragment" not in table
    assert all("test_fragment" not in k or k.startswith("res://")
               for k in table)
    reasons = [c["reason"] for c in _verdict_identity_collisions(arms)]
    assert "empty_script_red_key_disclosed_not_emitted" in reasons


def test_d10_red_entries_full_path_script_for_skip():
    """基线合成按表内完整 script 精确跳过（另一目录同名绿杀手不误跳）。"""
    import godot_qa_toolkit.mutation.verdict_source as vs

    doc = {
        "schema_version": "test-attribution/7",
        "arms": {}, "arms_state": {"gut_full": {"state": "valid"}},
        "test_verdicts": {
            "res://tests/unit/test_dup.gd::test_same_name_probe":
                {"verdict": "green"},
            "res://tests/nested/test_dup.gd::test_same_name_probe":
                {"verdict": "red", "classification": "pre_existing",
                 "script": "res://tests/nested/test_dup.gd"},
        },
    }
    reds = vs._red_entries(doc)
    assert len(reds) == 1
    (_ident, (_cls, script, name)) = next(iter(reds.items()))
    assert script == "res://tests/nested/test_dup.gd"
    assert name == "test_same_name_probe"


# --------------------------------------------------------------------------
# §SPEC-031：分类清单
# --------------------------------------------------------------------------

def _proj(tmp_path: "object") -> "object":
    """KOL 形态夹具（验收基准 = 手工盘点逐项相等）：
    生产 .gd 182（systems 74 / scenes 53 / autoload 22 / resources 21 /
    utils 12）；非生产 438（addons 436 + .dev 2）；测试 329（285
    test_*.gd + 44 features）；非测试 15（GUT 目录内 11 mock + tests/
    4 个 scripts——按 9 目录公共父 tests/ 口径）。"""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "project.godot").write_text("config_version=5\n")
    for d, n in (("systems", 74), ("scenes", 53), ("autoload", 22),
                 ("resources", 21), ("utils", 12)):
        (root / d).mkdir()
        for i in range(n):
            (root / d / f"f{i}.gd").write_text("extends Node\n")
    for name, files in (("gut", 164), ("godot_mcp", 250),
                        ("gqt_gherkin", 21), ("kol_dialog_autodismiss", 1)):
        d = root / "addons" / name
        d.mkdir(parents=True)
        (d / "plugin.cfg").write_text("[plugin]\n")
        for i in range(files):
            (d / f"f{i}.gd").write_text("extends Node\n")
    (root / ".dev" / "scripts").mkdir(parents=True)
    for i in range(2):
        (root / ".dev" / f"w{i}.gd").write_text("x=1\n")
    for k in range(9):
        (root / "tests" / f"area{k}").mkdir(parents=True)
    for i in range(285):
        (root / "tests" / f"area{i % 9}" / f"test_{i}.gd") \
            .write_text("extends GutTest\n")
    for i in range(11):
        (root / "tests" / f"area{i % 9}" / f"mock_{i}.gd") \
            .write_text("extends Node\n")
    (root / "tests" / "scripts").mkdir()
    for i in range(4):
        (root / "tests" / "scripts" / f"s{i}.gd").write_text(
            "extends Node\n")
    (root / ".gutconfig.json").write_text(json.dumps(
        {"dirs": [f"res://tests/area{k}" for k in range(9)],
         "include_subdirs": True, "prefix": "test_"}))
    (root / "addons" / "gqt_gherkin" / "features").mkdir(parents=True)
    for i in range(44):
        (root / "addons" / "gqt_gherkin" / "features" / f"feat_{i}.feature") \
            .write_text("Feature: x\n")
    return root


_KOL_INCLUDE = [{"kind": "dir", "value": "systems"},
                {"kind": "dir", "value": "scenes"},
                {"kind": "dir", "value": "autoload"},
                {"kind": "dir", "value": "resources"},
                {"kind": "dir", "value": "utils"}]


def test_spec031_kol_baseline_four_classes(tmp_path):
    """验收基准：按 KOL 文档化配置解析，四类与手工盘点逐项相等——
    生产 182 / 非生产 438 / 测试 329（285+44）/ 非测试 15（11+4）。"""
    from godot_qa_toolkit.measurement.classify import discover_scope

    root = _proj(tmp_path)
    (root / ".gqt-scope.json").write_text(json.dumps({
        "production": {"include": _KOL_INCLUDE},
        "tests": {"exclude": []}}))
    u = discover_scope(root)
    assert u["disclosure"]["counts"] == {
        "production": 182, "non_production": 438, "test": 329,
        "non_test": 15}
    assert len(u["gd"]) == 182
    assert u["disclosure"]["per_dir_counts"]["production"]["systems"] == 74


def test_spec031_tests_exclude_executes(tmp_path):
    """tests.exclude 真正生效：dir 条目从 gut dirs 生效面剔除、feature
    key 从测试类剔除（归非测试），具名披露。"""
    from godot_qa_toolkit.measurement.classify import discover_scope

    root = _proj(tmp_path)
    (root / ".gqt-scope.json").write_text(json.dumps({
        "production": {"include": _KOL_INCLUDE},
        "tests": {"exclude": [
            {"kind": "dir", "value": "tests/area0", "reason": "legacy"},
            {"kind": "feature", "value": "feat_0",
             "reason": "quarantined"}]}}))
    u = discover_scope(root)
    d = u["disclosure"]
    assert d["gut_dirs_effective"] == [f"tests/area{k}" for k in range(1, 9)]
    assert d["gut_dirs_excluded"] == ["tests/area0"]
    assert "feat_0" in d["features_excluded"]
    assert d["counts"]["test"] == 296


def test_spec031_default_unconfigured_named(tmp_path):
    """未配规则文件：通用默认（全部非隐藏=生产；gut dirs+features=测试）
    + "未配置范围规则"具名提示。"""
    from godot_qa_toolkit.measurement.classify import discover_scope

    root = _proj(tmp_path)
    u = discover_scope(root)
    assert u["disclosure"]["defaults_used"] is True
    assert u["disclosure"]["unconfigured_hint"]
    c = u["disclosure"]["counts"]
    # 非生产 = addons/gut 164 + addons/gqt_gherkin 21 + .dev 2（终稿
    # 默认：插件面与隐藏目录不算生产）
    assert c["non_production"] == 187
    assert c["non_test"] == 15


def test_spec031_production_granularities(tmp_path):
    """三种粒度：dir 递归 / file 单文件 / ext 类型；exclude 带 reason
    相减（file 可从 dir 白名单抠出）。"""
    from godot_qa_toolkit.measurement.classify import discover_scope

    root = _proj(tmp_path)
    (root / ".gqt-scope.json").write_text(json.dumps({
        "production": {
            "include": [{"kind": "dir", "value": "scenes"},
                        {"kind": "file", "value": "systems/f0.gd"},
                        {"kind": "ext", "value": "*.csv"}],
            "exclude": [{"kind": "dir", "value": "addons",
                         "reason": "editor_plugins"},
                        {"kind": "file", "value": "scenes/f1.gd",
                         "reason": "dead_code"}]}}))
    u = discover_scope(root)
    prod = set(u["production"])
    assert "res://scenes/f2.gd" in prod
    assert "res://scenes/f1.gd" not in prod  # file 粒度黑名单
    assert "res://systems/f0.gd" in prod  # file 粒度白名单
    assert "res://addons/gut/f0.gd" not in prod  # dir 粒度黑名单
    assert "res://data.csv" not in prod  # ext 白名单只覆盖声明的 csv？无 csv 文件
    assert u["disclosure"]["excluded_production_stats"] == {
        "dead_code (rule)": 1}


def test_spec031_bad_path_or_empty_whitelist_fail_loud(tmp_path):
    from godot_qa_toolkit.measurement.classify import (
        ScopeError, discover_scope, load_scope_rules,
    )

    root = _proj(tmp_path)
    (root / ".gqt-scope.json").write_text(json.dumps({
        "production": {"include": [{"kind": "dir", "value": "no_such_dir"}]}}))
    with pytest.raises(ScopeError):
        discover_scope(root)
    (root / ".gqt-scope.json").write_text(json.dumps({
        "production": {"include": []}}))
    with pytest.raises(ScopeError):
        load_scope_rules(root)
    (root / ".gqt-scope.json").write_text(json.dumps({
        "production": {"include": [{"kind": "dir", "value": "systems"}],
                       "exclude": [{"kind": "dir", "value": "x"}]}}))
    with pytest.raises(ScopeError):
        load_scope_rules(root)


def test_spec031_rules_file_in_recipe_hash(tmp_path):
    """规则文件进 recipe：内容变更 → recipe hash 翻转（#16 失配全量）。"""
    from godot_qa_toolkit.measurement.recipe import build_recipe

    root = _proj(tmp_path)
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent))
    from gherkin_fixtures import ensure_addon_metadata
    ensure_addon_metadata(root)
    (root / ".gqt-scope.json").write_text(json.dumps({
        "production": {"include": _KOL_INCLUDE}}))
    r1, err = build_recipe(root)
    assert err is None, err
    sc = r1["source_classification"]
    assert sc["rules_file_present"] is True
    h1 = r1["recipe_hash"]
    (root / ".gqt-scope.json").write_text(json.dumps({
        "production": {"include": [{"kind": "dir", "value": "systems"}]}}))
    r2, err2 = build_recipe(root)
    assert err2 is None
    assert r2["recipe_hash"] != h1


def test_spec031_text_content_same_pipeline(tmp_path):
    """文本内容文件（.tscn/.tres/csv）与生产 .gd 同一套发现（v8）。"""
    from godot_qa_toolkit.measurement.classify import discover_scope

    root = _proj(tmp_path)
    (root / "scenes2").mkdir()
    (root / "scenes2" / "main.tscn").write_text("[node]\n")
    (root / "data.csv").write_text("a,b\n")
    (root / ".gqt-scope.json").write_text(json.dumps({
        "production": {"include": [
            {"kind": "dir", "value": "systems"},
            {"kind": "dir", "value": "scenes2"},
            {"kind": "ext", "value": "*.csv"}]}}))
    u = discover_scope(root)
    assert "res://scenes2/main.tscn" in u["production"]
    assert "res://data.csv" in u["production"]


# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# §SPEC-029：全集对账 / 新增生产检出 / 指纹 / 显式自刷 / 不可归因全量
# --------------------------------------------------------------------------

def test_spec029_new_production_detected_as_stale(tmp_path):
    import godot_qa_toolkit.mutation.verdict_source as vs

    root = tmp_path / "p"
    (root / "systems").mkdir(parents=True)
    (root / "systems" / "calc.gd").write_text("extends Node\n")
    doc = {"file_sha256": {"systems/old.gd": "x"}}
    # old.gd 不存在 → removed 命名失败（全集对账含存在性）
    with pytest.raises(vs.BaselineSourceError) as ei:
        vs._assert_target_files(str(root), doc, [])
    assert "removed" in str(ei.value)
    # 新增生产 .gd 不在 map → 判不新鲜
    (root / "systems" / "calc.gd").write_text("extends Node\n")
    (root / "utils").mkdir()
    (root / "utils" / "u.gd").write_text("extends Node\n")
    import hashlib as _hl
    real = _hl.sha256((root / "systems" / "calc.gd").read_bytes()) \
        .hexdigest()
    doc2 = {"file_sha256": {"systems/calc.gd": real}}
    with pytest.raises(vs.BaselineSourceError) as ei2:
        vs._assert_target_files(str(root), doc2, [])
    assert "new universe file" in str(ei2.value)


def test_spec029_project_godot_in_fingerprint(tmp_path):
    """`project_face` 进 recipe：project.godot 单变量变更 → 当前树
    recipe hash 与建图时翻转（recipe 失配 → 具名全量走既有阶梯）。"""
    from godot_qa_toolkit.measurement.recipe import (
        _project_face, recipe_hash,
    )

    root = tmp_path / "p"
    root.mkdir()
    (root / "project.godot").write_text('[autoload]\nX="*res://a.gd"\n')
    f1 = _project_face(root)
    assert f1["autoloads"] == ["X"]
    (root / "project.godot").write_text(
        '[autoload]\nX="*res://a.gd"\nY="*res://b.gd"\n')
    f2 = _project_face(root)
    assert f2["autoloads"] == ["X", "Y"]
    assert f1["sha256"] != f2["sha256"]
    # hash 面变化（recipe 基座同体，仅 project_face 差异）
    base = {"recipe_version": "t", "project_face": f1}
    h1 = recipe_hash(dict(base))
    base2 = {"recipe_version": "t", "project_face": f2}
    assert recipe_hash(base2) != h1


def test_spec029_toolchain_probe_cached(tmp_path):
    from godot_qa_toolkit.measurement import recipe as _recipe

    tc1 = _recipe._probe_toolchain(tmp_path)
    tc2 = _recipe._probe_toolchain(tmp_path)
    assert tc1 == tc2
    assert set(tc1) == {"godot_version", "gut_version", "gqt_version",
                        "ops_version"}


def test_spec029_status_recipe_pristine_stale_and_fresh(tmp_path, monkeypatch):
    """默认路径 map 的新鲜度统一经 map 契约 status 判定（§SPEC-035/S2）：
    recipe pristine 失配 → fresh=False 且带 recipe 因素；一致 → fresh=True。"""
    import hashlib

    import godot_qa_toolkit.mutation.truth_select as ts
    from godot_qa_toolkit.mutation import map_contract as mc

    root = tmp_path / "p"
    root.mkdir()
    pg = root / "project.godot"
    pg.write_text("config_version=5\n")
    pg_sha = hashlib.sha256(pg.read_bytes()).hexdigest()
    path = Path(ts.default_map_path(str(root)))
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"schema_version": "test-attribution/7",
           "recipe_hash_pristine": "OLD",
           "file_sha256": {"project.godot": pg_sha},
           "tests_face": {}, "arms": {"gut_full": {}},
           "test_verdicts": {},
           "arms_state": {"gut_full": {"state": "valid"}}}
    path.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(ts, "_fresh_recipe_hash", lambda r: "NEW")
    st = mc.status(str(root), [])
    assert st["present"] is True and st["fresh"] is False
    assert any("recipe" in f for f in st["factors"])
    doc2 = {**doc, "recipe_hash_pristine": "NEW"}
    path.write_text(json.dumps(doc2), encoding="utf-8")
    st2 = mc.status(str(root), [])
    assert st2["fresh"] is True


def test_spec029_unattributable_falls_back_full(tmp_path, monkeypatch):
    """增量续新遇 `unknown_changed_paths`（不可归因非 .gd）→ 具名全量
    重建（不作为用户可见错误）。"""
    import godot_qa_toolkit.mutation.verdict_source as vs

    calls = {"full": 0}

    class _IncErr(Exception):
        pass

    def _fake_increment(*a, **k):
        raise _IncErr("unknown_changed_paths: 1 changed path(s) "
                      "cannot be classified")

    monkeypatch.setattr(vs, "_prepare_full_rebuild",
                        lambda *a, **k: calls.__setitem__("full", 1))
    # run_attribution_increment / IncrementError 以函数内延迟 import 引用
    # ——patch 源模块属性（函数级 from-import 每次重读）。
    import godot_qa_toolkit.measurement.increment as _inc
    monkeypatch.setattr(_inc, "run_attribution_increment", _fake_increment)
    monkeypatch.setattr(_inc, "IncrementError", _IncErr)
    monkeypatch.setattr("godot_qa_toolkit.measurement.recipe.build_recipe",
                        lambda root: ({"recipe_hash": "h"}, None))
    tgt = tmp_path / "systems" / "calc.gd"
    tgt.parent.mkdir(parents=True, exist_ok=True)
    tgt.write_text("extends Node\n")
    # C3-R14：基线必须带 `test_verdicts`（§SPEC-025 表守恒入口拒绝
    # 缺表基线——本测主题是 unknown 回退，fixture 补齐守恒前提）。
    vs._prepare_increment_renewal({"target_files": ["systems/calc.gd"],
                                   "test_verdicts": {}},
                                  str(tmp_path), "/x/map.json",
                                  ["some.bin"], 600)
    assert calls["full"] == 1


def test_f2_gut_dir_order_independent(tmp_path):
    """C3-R2 项4：gutconfig 同时列父目录与子目录、include_subdirs=false
    ——归类与列举顺序无关（父目录在前不得把子目录 test_*.gd 短路）。"""
    from godot_qa_toolkit.measurement.classify import discover_scope

    root = tmp_path / "proj"
    root.mkdir()
    (root / "project.godot").write_text("config_version=5\n")
    (root / "prod.gd").write_text("extends Node\n")
    (root / "tests" / "sub").mkdir(parents=True)
    (root / "tests" / "sub" / "test_x.gd").write_text("extends GutTest\n")
    (root / ".gutconfig.json").write_text(json.dumps({
        "dirs": ["res://tests", "res://tests/sub"],
        "include_subdirs": False, "prefix": "test_"}))
    # 顺序一：父目录在前
    (root / ".gqt-scope.json").write_text(json.dumps({
        "production": {"include": [{"kind": "dir", "value": "tests/sub"},
                                   {"kind": "file", "value": "prod.gd"}]}}))
    u1 = discover_scope(root)
    # include_subdirs=false + 父目录 res://tests → sub/test_x 非直属；
    # 子目录 res://tests/sub 直属命中 → test（顺序无关）
    assert "res://tests/sub/test_x.gd" in u1["test"], u1["test"]
    assert "res://prod.gd" in u1["production"]
    # 顺序二：子目录在前
    (root / ".gutconfig.json").write_text(json.dumps({
        "dirs": ["res://tests/sub", "res://tests"],
        "include_subdirs": False, "prefix": "test_"}))
    u2 = discover_scope(root)
    assert "res://tests/sub/test_x.gd" in u2["test"]
