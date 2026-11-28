"""SEE-1587 §SPEC-008 — 机制审计机器判据（对抗姿态）。

把 §SPEC-011 处置矩阵的登记转成可回归报警的机器判据——证明「无未分类
旧双路径」：

- src 内旧 schema 字符串（test-attribution/5）零残留；
- 退役机制（fn 入口 probe `_function_entry_probes`、增量独立构建器
  `_build_fresh`）零定义零引用；
- prepcache algo 键编码归属粒度：旧 `attr/6` 条目绝不命中 `attr-line/1`；
- `/1` 手动 store 与 `/2` 投影 store 互不冒充（双向拒绝）；
- prefilter 消费面单源（planner/executor 只经 coverlink 统一链，不直连
  旧 load 函数）；
- 消费面与测试 fixture 全部迁到行级语义（/6）；
- 显式保留的兼容面显式非默认：`--select fn/file` 为显式校准/回退视图、
  `full` 唯一全脸审计、`measure coverage` /1 手动入口是唯一 /1 写者且
  不在默认 mutation 链上。

任一断言失败 = 旧双路径回归（不是现状打印）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "godot_qa_toolkit"
TESTS = Path(__file__).resolve().parent


def _src_texts() -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(SRC.rglob("*.py")):
        out[str(p.relative_to(SRC))] = p.read_text(encoding="utf-8")
    return out


# ------------------------------------------------------------ 旧字符串零残留


def test_src_has_zero_test_attribution_5_schema_strings():
    """旧 /5 schema 字符串在 src 内零残留（行级主真值唯一 schema）。"""
    bad = [path for path, text in _src_texts().items()
           if "test-attribution/5" in text]
    assert not bad, f"legacy schema string resurfaced in src: {bad}"


def test_retired_fn_entry_probe_machinery_absent():
    """fn 入口 probe 机制物理退役：零定义、零引用（含 docstring 代码引用）。"""
    hits = [f"{path}: {line.strip()[:120]}"
            for path, text in _src_texts().items()
            for line in text.splitlines()
            if "_function_entry_probes" in line]
    assert not hits, f"retired _function_entry_probes resurfaced: {hits[:8]}"


def test_retired_increment_fresh_builder_absent():
    """增量独立 attr 模板构建器退役：模板构建单源（_instrument_pair）。"""
    hits = [f"{path}: {line.strip()[:120]}"
            for path, text in _src_texts().items()
            for line in text.splitlines()
            if "_build_fresh" in line]
    assert not hits, f"retired _build_fresh resurfaced: {hits[:8]}"
    inc = (SRC / "measurement" / "increment_runner.py").read_text(
        encoding="utf-8")
    assert "_instrument_pair" in inc, (
        "increment fresh path must reuse the single-source builder "
        "_instrument_pair")


def test_hit_level_probe_surface_is_the_attribution_shape():
    """hit 级探针面在位：attr 模板由全部块入口 probe 携带归属块（若回归
    fn 入口限制，行事实退化为函数入口行——此处直接报警）。"""
    inst = (SRC / "measurement" / "instrument.py").read_text(
        encoding="utf-8")
    assert "frozenset(range(len(probe_lines)))" in inst, (
        "hit-level attribution surface regressed: _instrument_pair no "
        "longer carries attribution blocks on every block-entry probe")


# ------------------------------------------------------------ 常量对齐


def test_schema_constants_aligned_line_primary():
    from godot_qa_toolkit.measurement import increment as inc_mod
    from godot_qa_toolkit.measurement.attribution import (
        ATTRIBUTION_ALGO_TAG,
        ATTRIBUTION_SCHEMA,
    )
    from godot_qa_toolkit.mutation import coverlink, truth_select

    assert ATTRIBUTION_SCHEMA == "test-attribution/7"
    assert ATTRIBUTION_ALGO_TAG == "attr-line/1"
    assert truth_select.SCHEMA_VERSION == ATTRIBUTION_SCHEMA
    assert inc_mod.ATTRIBUTION_SCHEMA == ATTRIBUTION_SCHEMA
    assert coverlink.SCHEMA_COVERAGE == "see1420-coverage/1"
    assert coverlink.SCHEMA_COVERAGE_PROJECTION == "see1420-coverage/2"


# ------------------------------------------------------------ algo 键粒度


def test_algo_granularity_key_rejects_legacy_attr6_entries(tmp_path):
    """旧 `attr/6` 条目与 hit 级新键（attr-line/1）绝不互相命中——粒度
    编码进 algo 键（SEE-1585 原型教训的回归守卫）。"""
    from godot_qa_toolkit.measurement import prepcache
    from godot_qa_toolkit.measurement.instrument import _algo_used

    root = str(tmp_path)
    kwargs = dict(rel="x.gd", src_sha256="s", recipe_hash="r",
                  registry={"entries": []}, probe_manifest={},
                  instrumented_template="tpl GQTUUIDSENTINEL",
                  probe_lines=[1], source_plain="src", block_map={})
    legacy_tag = "blocks/6-attr/6"
    prepcache.store_entry(root, algo=legacy_tag, **kwargs)
    current = _algo_used(prepcache.PROBE_ALGO_VERSION, True)
    assert current.endswith("attr-line/1")
    assert prepcache.load_entry(root, rel="x.gd", src_sha256="s",
                                recipe_hash="r", algo=current) is None
    assert prepcache.load_entry(root, rel="x.gd", src_sha256="s",
                                recipe_hash="r", algo=legacy_tag) is not None


# ------------------------------------------------------------ store 互不冒充


def test_manual_store_and_projection_store_never_impersonate(tmp_path):
    """SEE-1594 A04：/1 写侧与读链已退役——写侧拒绝无绑定条目，盘上残留
    /1 schema 条目对投影消费面不可见（schema 分流拒绝）。"""
    import json as _json

    from godot_qa_toolkit.mutation import coverlink

    proj = tmp_path / "proj"
    target = proj / "a.gd"
    target.parent.mkdir(parents=True)
    target.write_text("func f(a):\n\treturn a\n")
    glob = "res://tests/"
    # 写侧：无 projection 绑定 = 拒绝（不再产出 /1 形态）
    try:
        coverlink.save_coverage({2}, 3, str(proj), str(target), glob,
                                instrumentable_lines={1, 2, 3})
        raise AssertionError("save_coverage must require a projection binding")
    except ValueError as e:
        assert "projection binding" in str(e)
    # 盘上手写 /1 残留条目 → 投影消费面 schema 分流拒绝
    entry = {"schema": coverlink.SCHEMA_COVERAGE, "rel": "a.gd",
             "tests_glob": glob, "binary_kind": "cold",
             "source_sha1": __import__("hashlib").sha1(
                 target.read_bytes()).hexdigest(),
             "total_lines": 3, "covered_lines": [2],
             "coverage_percent": 66.67, "recorded_at": 0.0}
    path = coverlink.coverage_cache_path(str(proj), str(target), glob)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(entry), encoding="utf-8")
    lines, err = coverlink.load_projected_coverage(
        str(proj), str(target), glob)
    assert lines is None and "manual measurement entry" in err
    # /2 写路径受控：无绑定 doc → 投影拒绝（不产出无绑定缓存）
    doc = {"schema_version": "test-attribution/7",
           "recipe_hash_pristine": "p", "file_sha256": {}, "tests_face": {},
           "instrumentable_lines": {}, "arms_state": {}, "arms": {}}
    covered, perr = coverlink.project_coverage_from_map(
        str(proj), doc, str(target), ["res://tests/t.gd"],
        "res://.gqt-attr-select/x/")
    assert covered is None  # 该 doc 无绑定/面，仅证明写入路径受控


# ------------------------------------------------------------ prefilter 单源


def test_prefilter_consumers_go_through_the_single_chain():
    """planner/executor 只经 coverlink 统一解析链做 prefilter——直连旧
    load 函数的旁路（双路径）在此报警。"""
    planner = (SRC / "mutation" / "planner.py").read_text(encoding="utf-8")
    # SEE-1594 BTIDY：executor 的 prefilter 消费簇物理迁移至 planning 模块
    # （零行为拆分）——统一链守卫随迁，消费文件断言不弱化。
    executor = (SRC / "mutation" / "planning.py").read_text(encoding="utf-8")
    for name, text in (("planner", planner), ("executor/planning", executor)):
        assert "resolve_coverage_for_prefilter" in text, (
            f"{name} must consume the unified prefilter chain")
        for legacy in ("load_reusable_coverage", "load_instrumentable_lines"):
            assert legacy not in text, (
                f"{name} bypasses the unified chain via {legacy}")
    # SEE-1594 A04：/1 读链零消费者——`load_reusable_coverage` /
    # `load_instrumentable_lines` 在 src/ 无任何残留（含 coverlink 自身）。
    loaders = [path for path, text in _src_texts().items()
               if "load_reusable_coverage(" in text
               or "load_instrumentable_lines(" in text]
    assert loaders == [], loaders


def test_measure_coverage_manual_entry_is_the_only_store1_writer():
    """SEE-1594 §SPEC-027 A04：`/1` 独立覆盖真源写者已退役——`measure
    coverage` 不再写 `/1`；唯一 `save_coverage(` 调用在 `mutation/coverlink`
    的**行事实投影**路径（`projection=` → `/2` 派生缓存），不是第二真源。"""
    writers = [path for path, text in _src_texts().items()
               if "save_coverage(" in text and "def save_coverage" not in text]
    # A04：`coverage/runner.py` 不再写 `/1`；唯一 `save_coverage(` 仅存于其
    # 定义文件 `mutation/coverlink.py`（投影路径写 `/2` 派生缓存，非第二真源），
    # 故外部写者列表为空。
    assert "coverage/runner.py" in _src_texts(), "runner module should remain"
    assert "coverage/runner.py" not in writers, writers
    assert writers == [], writers


# ------------------------------------------------------------ 消费面迁移


def test_test_side_fixtures_and_constants_migrated_to_schema6():
    """测试侧 fixture/常量全部迁到 /6（§SPEC-004 轮消费者迁移的回归守卫）。"""
    for name in ("attribution_fixtures.py", "test_select_dispatch_see1583.py",
                 "test_see1581_hardener_strengthen.py",
                 "test_canonical_attribution_see1581.py",
                 "test_canonical_increment_see1581.py"):
        text = (TESTS / name).read_text(encoding="utf-8")
        assert "test-attribution/5" not in text, (
            f"{name} still pins the legacy /5 schema")


def test_write_map_fixture_produces_line_primary_doc(tmp_path):
    """行为级复核：共享 fixture write_map 产出 /6 行级主真值文档（含行事实
    与反查），消费面（truth_select.load_map）可装载。"""
    from godot_qa_toolkit.mutation import truth_select
    from attribution_fixtures import write_map

    proj = tmp_path / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "test_a.gd").write_text("extends GutTest\n")
    (proj / "src").mkdir()
    (proj / "src" / "a.gd").write_text("func f():\n\treturn 1\n")
    write_map(proj, {"src/a.gd": ["test_a.gd::t1"]})
    doc = truth_select.load_map(proj / ".qa-cache" / "attribution-map.json")
    assert doc["schema_version"] == "test-attribution/7"
    arm = doc["arms"]["gut_full"]
    assert arm["test_to_lines"]["test_a.gd::t1"]["src/a.gd"] == [2]
    assert arm["line_to_tests"]["src/a.gd"]["2"] == ["test_a.gd::t1"]


def test_line_primary_arm_shape_is_not_the_fn_shape():
    """行级主真值形状守卫：映射含行事实/反查，派生视图由其投影——回归到
    「只有 file/function 边、无行事实」的 /5 形状在此报警。"""
    from godot_qa_toolkit.measurement.attribution import (
    build_attribution_map,
)

    attr = {"a.gd": {5: {"seen": {0}, "cnt": {0: 2},
                         "fn": {0: {0}}, "unattr_counts": {},
                         "phases": [0, 0, 0, 0]}}}
    registries = ({5: {0: "t.gd::t1"}}, {})
    m = build_attribution_map(
        "gut_full", attr, registries,
        probe_lines_by_rel={"a.gd": [3]},
        block_map_by_rel={"a.gd": {3: [3, 4]}},
        fid_by_line_by_rel={"a.gd": {3: "f", 4: "f"}})
    for key in ("test_to_lines", "script_to_lines", "line_to_tests",
                "line_pairs", "test_to_files", "test_to_functions"):
        assert key in m, f"line-primary arm shape regressed: missing {key}"
    assert m["test_to_lines"]["t.gd::t1"]["a.gd"] == [3, 4]


# ------------------------------------------------------------ 兼容面非默认


def test_calibration_and_audit_faces_are_explicit_not_default():
    """显式保留的兼容面必须显式非默认：--select 默认 line（fn/file 校准/
    回退、full 唯一全脸审计均为显式选择）。"""
    from godot_qa_toolkit.cli import build_parser
    from godot_qa_toolkit.mutation import runner

    args = build_parser().parse_args(
        ["mutation", "f.gd", "--project-root", "p"])
    assert args.select_mode == "line"
    action = next(a for a in
                  build_parser()._subparsers._group_actions[0]
                  .choices["mutation"]._actions if a.dest == "select_mode")
    assert set(action.choices) == {"line", "fn", "file", "full"}
    # full = 显式审计面（唯一全脸入口；非默认路径派生）
    glob, meta = runner._auto_scan_tests_glob_meta(
        "f.gd", "p", select_mode="full")
    assert glob is None and meta["source"] == "full_face" \
        and meta["reason"] == "explicit_full_face"
