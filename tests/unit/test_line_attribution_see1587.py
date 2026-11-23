"""SEE-1587 §SPEC-002 — line-primary Attribution 新 schema 判据。

覆盖：
- hit 级插桩形态（全部块入口 probe 携带归属块；fn 入口限制退役）；
- algo 键编码归属粒度（attr-line/1；fn 级旧条目绝不互相命中）；
- test-attribution/6：行事实主真值 + line→tests 反查 + 派生 file/function
  视图；旧 /5 schema fail-loud 无兼容别名；
- 同轮插桩 Coverage Store 投影（see1420-coverage/2）：按 mutation 实际
  测试集合做行集并集，绑定包失效即显式拒用，绝不假过滤。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from godot_qa_toolkit.coverage.runner import _instrument_blocks
from godot_qa_toolkit.measurement import prepcache
from godot_qa_toolkit.measurement.shadowio import (
    _read_attr_merged,
)
from godot_qa_toolkit.measurement.attribution import (
    ATTRIBUTION_SCHEMA,
    build_attribution_map,
)
from godot_qa_toolkit.measurement.instrument import (
    _algo_used,
    _instrument_pair,
)
from godot_qa_toolkit.measurement.orchestrator import (
    file_key8,
    run_measurement,
)
from godot_qa_toolkit.mutation import coverlink
from godot_qa_toolkit.mutation import truth_select
from godot_qa_toolkit.mutation.truth_select import MutationSelectionError

TARGET_SRC = """extends RefCounted


func work(n: int) -> int:
\tvar acc = 0
\tfor i in range(n):
\t\tacc += i
\treturn acc
"""


# ------------------------------------------------------------ hit 级插桩形态


def _registry():
    return {"entries": [
        {"fid": "x.gd::work", "instrumentable": True,
         "start_line": 4, "end_line": 8},
    ]}


def test_instrument_pair_emits_hit_level_attr_template():
    """SEE-1587：attr 模板全部块入口 probe 携带归属块（hit 级边覆盖整个
    探针面）；plain 模板零归属块。"""
    from godot_qa_toolkit.coverage.runner import _instrument_blocks
    built = _instrument_pair(
        "x.gd", TARGET_SRC, _registry(), block_compress=True,
        test_attribution=True, attribution_causal=False)
    assert "parse_error" not in built
    plain, attr = built["template"], built["template_attr"]
    probe_lines, block_map = built["probe_lines"], built["block_map"]
    assert probe_lines and block_map
    # plain 模板无归属块；attr 模板归属块数 == 探针数（全 probe 携带）
    assert "gqt_cov_slot" not in plain
    assert attr.count('Engine.get_meta("gqt_cov_slot", 0)') \
        == len(probe_lines)
    # 每个归属块都记 (probe, slot) fctx 边（fn 级数据布局）
    assert attr.count(".get_or_add(") == len(probe_lines)


def test_algo_key_encodes_attribution_granularity():
    assert _algo_used("blocks/6", True) == "blocks/6-attr-line/1"
    assert _algo_used("blocks/6", True, causal=True) \
        == "blocks/6-attr-line/1-causal"
    assert _algo_used("blocks/6", False) == "blocks/6"


def test_old_granularity_cache_entries_never_hit_new_key(tmp_path):
    """fn 级旧条目（attr/6）与 hit 级新键（attr-line/1）绝不互相命中。"""
    root = str(tmp_path)
    kwargs = dict(rel="x.gd", src_sha256="s", recipe_hash="r",
                  registry={"entries": []}, probe_manifest={},
                  instrumented_template="tpl GQTUUIDSENTINEL",
                  probe_lines=[1], source_plain="src", block_map={})
    old_tag = _algo_used(prepcache.PROBE_ALGO_VERSION, True).replace(
        "attr-line/1", "attr/6")
    prepcache.store_entry(root, algo=old_tag, **kwargs)
    # 旧键条目在盘，但新键查找必须 miss（形态不同不互认）
    assert prepcache.load_entry(root, rel="x.gd", src_sha256="s",
                                recipe_hash="r",
                                algo=_algo_used(
                                    prepcache.PROBE_ALGO_VERSION,
                                    True)) is None
    assert prepcache.load_entry(root, rel="x.gd", src_sha256="s",
                                recipe_hash="r", algo=old_tag) is not None


# ------------------------------------------------------------ schema fail-loud


def _doc_with_schema(schema: str) -> dict:
    return {"schema_version": schema, "arms": {"gut_full": {}},
            "arms_state": {}}


def test_load_map_rejects_old_schema_fail_loud(tmp_path):
    p = tmp_path / "map.json"
    p.write_text(json.dumps(_doc_with_schema("test-attribution/5")),
                 encoding="utf-8")
    with pytest.raises(Exception, match="schema"):
        truth_select.load_map(p)


def test_validate_refresh_rejects_old_schema():
    with pytest.raises(MutationSelectionError):
        truth_select.validate_refresh(
            _doc_with_schema(ATTRIBUTION_SCHEMA),
            _doc_with_schema("test-attribution/5"))


def test_increment_baseline_rejects_old_schema(tmp_path):
    from godot_qa_toolkit.measurement.increment import IncrementError, \
        load_baseline
    p = tmp_path / "b.json"
    p.write_text(json.dumps(_doc_with_schema("test-attribution/5")),
                 encoding="utf-8")
    with pytest.raises(IncrementError, match="schema"):
        load_baseline(p)


def test_schema_constants_aligned():
    from godot_qa_toolkit.measurement.increment import ATTRIBUTION_SCHEMA \
        as INC_SCHEMA
    assert ATTRIBUTION_SCHEMA == "test-attribution/6"
    assert INC_SCHEMA == ATTRIBUTION_SCHEMA
    assert truth_select.SCHEMA_VERSION == ATTRIBUTION_SCHEMA


# ------------------------------------------------------------ 行事实主真值


def _rec(seen, cnt, fn, unattr_counts, phases):
    return {"seen": set(seen), "cnt": dict(cnt),
            "fn": {k: set(v) for k, v in fn.items()},
            "unattr_counts": dict(unattr_counts), "phases": list(phases)}


def test_line_facts_reverse_index_and_block_expansion():
    """hit 级 sink：(probe, slot) 边展开为块级行事实；两测试命中同一块 →
    line→tests 反查双身份；file/function 视图为派生投影。"""
    attr_by_rel = {"src/a.gd": {7: _rec(
        {0, 1}, {0: 4, 1: 2}, {0: {0, 1}, 3: {1}}, {}, [0, 0, 0, 0])}}
    registries = (
        {7: {0: "res://tests/t1.gd::ta", 1: "res://tests/t2.gd::tb"}}, {})
    m = build_attribution_map(
        "gut_full", attr_by_rel, registries,
        probe_lines_by_rel={"src/a.gd": [4, 5, 6, 7]},
        block_map_by_rel={"src/a.gd": {4: [4, 5], 5: [5, 6], 7: [7]}},
        fid_by_line_by_rel={"src/a.gd": {4: "f", 5: "f", 6: "f", 7: "g"}})
    # probe 0 → 行 4 → 块 [4,5]：t1/t2 共同行事实；probe 3 → 行 7 → [7]：tb
    assert m["test_to_lines"] == {
        "res://tests/t1.gd::ta": {"src/a.gd": [4, 5]},
        "res://tests/t2.gd::tb": {"src/a.gd": [4, 5, 7]},
    }
    assert m["line_to_tests"]["src/a.gd"] == {
        "4": ["res://tests/t1.gd::ta", "res://tests/t2.gd::tb"],
        "5": ["res://tests/t1.gd::ta", "res://tests/t2.gd::tb"],
        "7": ["res://tests/t2.gd::tb"],
    }
    # 派生视图：file/function 由行事实投影
    assert m["test_to_files"]["res://tests/t1.gd::ta"] == ["src/a.gd"]
    assert sorted(m["test_to_functions"]["res://tests/t1.gd::ta"]) == ["f"]
    assert sorted(m["test_to_functions"]["res://tests/t2.gd::tb"]) == ["f", "g"]
    assert m["line_pairs"] == 5
    # 车道事件统计独立于行事实（cnt 口径）
    assert m["attributed_hit_events"] == 6


def test_hit_mode_sink_reads_through_existing_reader(tmp_path):
    """产品 finalizer 的 sink 形态（fn 行）在 hit 级数据下读回不变——
    行事实提取与既有 reader 同一路径。"""
    import shutil
    import tempfile
    xdg = tempfile.mkdtemp(prefix="gqt-see1587-sink-")
    root = tmp_path / "proj"
    root.mkdir()
    (root / "project.godot").write_text(
        'config_version=5\n[application]\nconfig/name="see1587"\n')
    try:
        dest = (Path(xdg) / "godot" / "app_userdata" / "see1587")
        dest.mkdir(parents=True)
        key8 = file_key8("x.gd")
        (dest / f"qa-coverage-attr-u1-999-{key8}.txt").write_text(
            "test 0\ncnt 0 3\nfn 0 0\nfn 1 0\nfn 2 0\nph 0 0 0 0\n",
            encoding="utf-8")
        merged = _read_attr_merged(root, "u1", "x.gd", [xdg])
        rec = merged[999]
        assert rec["fn"] == {0: {0}, 1: {0}, 2: {0}}
    finally:
        shutil.rmtree(xdg, ignore_errors=True)


# ------------------------------------------------------------ 端到端 /6 产物


def _project(tmp_path: Path) -> Path:
    import subprocess as sp
    root = tmp_path / "proj"
    (root / "addons" / "gut").mkdir(parents=True)
    (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
    (root / "project.godot").write_text(
        'config_version=5\n[application]\nconfig/name="see1587-fixture"\n')
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
    return root


class _FakeGodot:
    """写 hits 与 attr sink（hit 级：全部 probe 的 fn 边）+ registry。"""

    def __init__(self, project_root: Path):
        import subprocess as sp
        self.real_run = sp.run
        self.root = project_root
        self.returncode = 0

    def __call__(self, *a, **k):
        import re
        import subprocess as sp
        argv = a[0]
        cmd = " ".join(argv)
        if "gut_cmdln.gd" not in cmd:
            return self.real_run(*a, **k)
        env = k.get("env") or {}
        xdg = env.get("XDG_DATA_HOME")
        if xdg:
            pg = (self.root / "project.godot").read_text(encoding="utf-8")
            proj = re.search(r'config/name\s*=\s*"([^"]+)"', pg).group(1)
            dest = Path(xdg) / "godot" / "app_userdata" / proj
            dest.mkdir(parents=True, exist_ok=True)
        else:
            dest = self.root
        for gd in self.root.glob("src_game/*.gd"):
            src = gd.read_text(encoding="utf-8")
            probe_ids = sorted(set(re.findall(
                r"__qa_cov_probe_m_[0-9a-f]{8}\[(\d+)\] = 1", src)),
                key=int)
            for m in re.finditer(
                    r'qa-coverage-hits-([0-9a-f-]{8,36})-" \+ '
                    r'str\(OS\.get_process_id\(\)\) \+ "-([0-9a-f]{8})\.txt',
                    src):
                uuid, key8 = m.group(1), m.group(2)
                (dest / f"qa-coverage-hits-{uuid}-777-{key8}.txt").write_text(
                    "".join(f"{i}\n" for i in probe_ids), encoding="utf-8")
            for m in re.finditer(
                    r'qa-coverage-attr-([0-9a-f-]{8,36})-" \+ '
                    r'str\(OS\.get_process_id\(\)\) \+ "-([0-9a-f]{8})\.txt',
                    src):
                uuid, key8 = m.group(1), m.group(2)
                # hit 级：每个 probe 都写 fn 边（去重形态，与 finalizer 一致）
                fn_lines = "".join(f"fn {i} 0\n" for i in probe_ids)
                (dest / f"qa-coverage-attr-{uuid}-777-{key8}.txt").write_text(
                    f"test 0\ncnt 0 {len(probe_ids) * 2}\n{fn_lines}"
                    "ph 0 0 0 0\n", encoding="utf-8")
        hook = self.root / "addons" / "gqt_cov_pre_hook.gd"
        if hook.is_file():
            for m in re.finditer(
                    r'qa-cov-attr-tests-([0-9a-f-]{8,36})-',
                    hook.read_text(encoding="utf-8")):
                uuid = m.group(1)
                (dest / f"qa-cov-attr-tests-{uuid}-777.txt").write_text(
                    "slot 0\ttest\tres://tests/unit/test_game.gd::test_ok\n",
                    encoding="utf-8")

        class R:
            def __init__(self, rc):
                self.returncode = rc
                self.stdout = ""
                self.stderr = ""

        return R(self.returncode)


def test_run_emits_line_primary_artifact(tmp_path, monkeypatch):
    import subprocess as sp
    project = _project(tmp_path)
    from gherkin_fixtures import ensure_addon_metadata
    ensure_addon_metadata(project)
    from godot_qa_toolkit.measurement.recipe import build_recipe

    recipe, error = build_recipe(project)
    assert error is None, error
    targets = sorted(str(p) for p in (project / "src_game").glob("*.gd"))
    monkeypatch.setattr(sp, "run", _FakeGodot(project))
    artifact_path = tmp_path / "line.attr.json"
    report = run_measurement(project, targets, recipe,
                           test_attribution=True,
                           attribution_out=str(artifact_path))
    doc = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert doc["schema_version"] == ATTRIBUTION_SCHEMA
    arm = doc["arms"]["gut_full"]
    # 行事实 + 反查 + 探针面全在产物里；行集 ⊆ 探针面
    lines = arm["test_to_lines"]["res://tests/unit/test_game.gd::test_ok"][
        "src_game/cell.gd"]
    assert lines
    surface = set(doc["instrumentable_lines"]["src_game/cell.gd"])
    assert set(lines) <= surface
    rev = arm["line_to_tests"]["src_game/cell.gd"]
    assert {int(k) for k, v in rev.items() if v} >= set(lines)
    # 派生视图与行事实一致
    assert arm["test_to_files"][
        "res://tests/unit/test_game.gd::test_ok"] == ["src_game/cell.gd"]
    # 报告 summary 携带 line_pairs
    assert report["test_attribution"]["arms"]["gut_full"]["line_pairs"] > 0


# ------------------------------------------------------------ Coverage 投影


def _write_line_primary_doc(tmp_path: Path, project: Path, rel: str,
                            src: str, line_map: dict) -> dict:
    """构造与磁盘绑定的 /6 文档：file_sha256/tests_face 现算，行事实给
    test_a → line_map、test_b → 附加行。"""
    face = {}
    tests = project / "tests"
    if tests.is_dir():
        for gd in sorted(tests.rglob("*.gd")):
            face[str(gd.relative_to(project))] = hashlib.sha256(
                gd.read_bytes()).hexdigest()
    arm = {
        "test_to_files": {"test_a.gd::ta": [rel], "test_b.gd::tb": [rel]},
        "test_to_functions": {}, "script_to_files": {},
        "script_to_functions": {}, "file_to_tests": {rel: [
            "test_a.gd::ta", "test_b.gd::tb"]}, "file_to_scripts": {},
        "test_to_lines": {
            "test_a.gd::ta": {rel: sorted(line_map["a"])},
            "test_b.gd::tb": {rel: sorted(line_map["b"])},
        },
        "script_to_lines": {},
        "line_to_tests": {rel: {str(ln): ["test_a.gd::ta"]
                                for ln in line_map["a"]}},
    }
    return {
        "schema_version": "test-attribution/6",
        "report_recipe_hash": "rh",
        "recipe_hash_pristine": "pristine-1",
        "project_root": str(project),
        "identity": "res://script::test_name",
        "file_sha256": {rel: hashlib.sha256(src.encode()).hexdigest()},
        "target_files": [rel],
        "tests_face": face,
        "instrumentable_lines": {rel: sorted(
            set(line_map["a"]) | set(line_map["b"]) | {99})},
        "arms": {"gut_full": arm},
        "arms_state": {"gut_full": {"state": "valid", "bundles": 1,
                                    "pairs": 2}},
        "notes": [],
    }


def test_projection_unions_selected_tests_and_persists(tmp_path):
    project = tmp_path / "proj"
    (project / "tests").mkdir(parents=True)
    (project / "tests" / "test_a.gd").write_text("extends GutTest\n")
    # S2.1 M2：裸名归属内检目录一致——face 必须登记全部事实脚本
    (project / "tests" / "test_b.gd").write_text("extends GutTest\n")
    target = project / "src" / "a.gd"
    target.parent.mkdir(parents=True)
    target.write_text(TARGET_SRC)
    doc = _write_line_primary_doc(tmp_path, project, "src/a.gd", TARGET_SRC,
                                  {"a": {5, 6}, "b": {6, 7}})
    covered, err = coverlink.project_coverage_from_map(
        str(project), doc, str(target), ["res://tests/test_a.gd"],
        "res://.gqt-attr-select/abc/", current_recipe_pristine="pristine-1")
    assert err is None
    # 只并集 test_a 的行事实，∩ 探针面（行 99 不在事实里自然被排除）
    assert covered == {5, 6}
    # /2 条目落盘：schema + 绑定包 + exact tests_glob
    entry_path = coverlink.coverage_cache_path(
        str(project), str(target), "res://.gqt-attr-select/abc/")
    raw = json.loads(entry_path.read_text(encoding="utf-8"))
    assert raw["schema"] == coverlink.SCHEMA_COVERAGE_PROJECTION
    assert raw["tests_glob"] == "res://.gqt-attr-select/abc/"
    assert raw["projection"]["source"] == coverlink.PROJECTION_SOURCE
    assert raw["projection"]["map_recipe_pristine"] == "pristine-1"
    assert raw["instrumentable_lines"] == [5, 6, 7, 99]
    # 读回：绑定全匹配 → 同一行集（A05：绑定对当前映射内容校验）
    lines, err = coverlink.load_projected_coverage(
        str(project), str(target), "res://.gqt-attr-select/abc/",
        current_recipe_pristine="pristine-1", current_doc=doc)
    assert err is None and lines == {5, 6}
    # 双测试并集
    covered2, err = coverlink.project_coverage_from_map(
        str(project), doc, str(target),
        ["res://tests/test_a.gd", "res://tests/test_b.gd"],
        "res://.gqt-attr-select/def/", current_recipe_pristine="pristine-1")
    assert err is None and covered2 == {5, 6, 7}


def test_projection_rejects_unsafe_bindings(tmp_path):
    project = tmp_path / "proj"
    (project / "tests").mkdir(parents=True)
    (project / "tests" / "test_a.gd").write_text("extends GutTest\n")
    target = project / "src" / "a.gd"
    target.parent.mkdir(parents=True)
    target.write_text(TARGET_SRC)
    doc = _write_line_primary_doc(tmp_path, project, "src/a.gd", TARGET_SRC,
                                  {"a": {5}, "b": {6}})
    glob = "res://.gqt-attr-select/abc/"
    # recipe drift
    covered, err = coverlink.project_coverage_from_map(
        str(project), doc, str(target), ["res://tests/test_a.gd"], glob,
        current_recipe_pristine="pristine-2")
    assert covered is None and "recipe drift" in err
    # map schema 非 /6
    bad = dict(doc)
    bad["schema_version"] = "test-attribution/5"
    covered, err = coverlink.project_coverage_from_map(
        str(project), bad, str(target), ["res://tests/test_a.gd"], glob,
        current_recipe_pristine="pristine-1")
    assert covered is None and "schema" in err
    # 臂无效
    bad = json.loads(json.dumps(doc))
    bad["arms_state"] = {"gut_full": {"state": "run_error", "bundles": 0,
                                      "pairs": 0}}
    covered, err = coverlink.project_coverage_from_map(
        str(project), bad, str(target), ["res://tests/test_a.gd"], glob,
        current_recipe_pristine="pristine-1")
    assert covered is None and "arms invalid" in err
    # 目标文件内容变更 → file 绑定失配
    target.write_text(TARGET_SRC.replace("\treturn acc",
                                         "\treturn acc + 0"))
    covered, err = coverlink.project_coverage_from_map(
        str(project), doc, str(target), ["res://tests/test_a.gd"], glob,
        current_recipe_pristine="pristine-1")
    assert covered is None and "file binding stale" in err
    target.write_text(TARGET_SRC)
    # 测试面漂移
    (project / "tests" / "test_a.gd").write_text(
        "extends GutTest\n# edited\n")
    covered, err = coverlink.project_coverage_from_map(
        str(project), doc, str(target), ["res://tests/test_a.gd"], glob,
        current_recipe_pristine="pristine-1")
    assert covered is None and "tests face drift" in err


def test_projection_read_invalidates_on_drift(tmp_path):
    """增量后绑定漂移 → 显式失效（不假过滤）；新映射再投影 → 覆盖写回。"""
    project = tmp_path / "proj"
    (project / "tests").mkdir(parents=True)
    (project / "tests" / "test_a.gd").write_text("extends GutTest\n")
    target = project / "src" / "a.gd"
    target.parent.mkdir(parents=True)
    target.write_text(TARGET_SRC)
    doc = _write_line_primary_doc(tmp_path, project, "src/a.gd", TARGET_SRC,
                                  {"a": {5}, "b": {6}})
    glob = "res://.gqt-attr-select/abc/"
    covered, err = coverlink.project_coverage_from_map(
        str(project), doc, str(target), ["res://tests/test_a.gd"], glob,
        current_recipe_pristine="pristine-1")
    assert err is None
    # 测试面变更（增量合并会把 tests_face 更新——此处直接改盘上测试文件
    # 模拟漂移后的树）
    (project / "tests" / "test_b.gd").write_text("extends GutTest\n")
    lines, err = coverlink.load_projected_coverage(
        str(project), str(target), glob,
        current_recipe_pristine="pristine-1")
    assert lines is None and "tests face drift" in err
    # 模拟增量后的新映射（tests_face 已含 test_b、行事实更新）→ 再投影
    doc2 = _write_line_primary_doc(tmp_path, project, "src/a.gd", TARGET_SRC,
                                   {"a": {5, 7}, "b": {6}})
    face = dict(doc2["tests_face"])
    face["tests/test_b.gd"] = hashlib.sha256(
        (project / "tests" / "test_b.gd").read_bytes()).hexdigest()
    doc2["tests_face"] = face
    covered, err = coverlink.project_coverage_from_map(
        str(project), doc2, str(target), ["res://tests/test_a.gd"], glob,
        current_recipe_pristine="pristine-1")
    assert err is None and covered == {5, 7}
    lines, err = coverlink.load_projected_coverage(
        str(project), str(target), glob,
        current_recipe_pristine="pristine-1", current_doc=doc2)
    assert err is None and lines == {5, 7}


def test_projection_loader_never_consumes_manual_entries(tmp_path):
    """SEE-1594 A04：已退役 /1 手动 schema 的盘上残留条目对投影消费面
    不可见（schema 分流拒绝；写侧已不再产出该形态）。"""
    project = tmp_path / "proj"
    (project / "tests").mkdir(parents=True)
    target = project / "src" / "a.gd"
    target.parent.mkdir(parents=True)
    target.write_text(TARGET_SRC)
    glob = "res://tests/"
    entry = {"schema": coverlink.SCHEMA_COVERAGE, "rel": "src/a.gd",
             "tests_glob": glob, "binary_kind": "cold",
             "source_sha1": hashlib.sha1(
                 target.read_bytes()).hexdigest(),
             "total_lines": 3, "covered_lines": [5],
             "coverage_percent": 33.33, "recorded_at": 0.0}
    path = coverlink.coverage_cache_path(str(project), str(target), glob)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entry), encoding="utf-8")
    lines, err = coverlink.load_projected_coverage(
        str(project), str(target), glob)
    assert lines is None and "manual measurement entry" in err


# ------------------------------------------------- 硬化补盲（变异存活点）
# 以下判据封闭 SEE-1587 hardener 轮注入变异的存活点（仅测试侧补强）：
# - CL2：projection covered ∩ 探针可插桩面（不可插桩行不得进入覆盖集）
# - CL3：projection 并集必须含 script 车道行事实（不止 test 车道）
# - CL5：load 侧绑定包 map_file_sha256 失配显式拒绝（即便 source_sha1 仍匹配）
# - CL6：load 侧身份包漂移显式拒绝（不假过滤）


def test_projection_intersects_covered_with_instrumentable(tmp_path):
    """行事实中被投影的 covered 行必须 ∩ 探针可插桩面——不可插桩行不得
    进入 projection 覆盖集（D3 过滤锚 = covered ∧ instrumentable）。"""
    project = tmp_path / "proj"
    (project / "tests").mkdir(parents=True)
    (project / "tests" / "test_a.gd").write_text("extends GutTest\n")
    target = project / "src" / "a.gd"
    target.parent.mkdir(parents=True)
    target.write_text(TARGET_SRC)
    doc = _write_line_primary_doc(tmp_path, project, "src/a.gd", TARGET_SRC,
                                  {"a": {5, 6}, "b": {6}})
    rel = "src/a.gd"
    # test_a 的行事实多出可插桩面之外的行 42 → 必须被 ∩ 掉
    doc["arms"]["gut_full"]["test_to_lines"]["test_a.gd::ta"][rel] = [5, 6, 42]
    doc["instrumentable_lines"][rel] = [5, 6]
    covered, err = coverlink.project_coverage_from_map(
        str(project), doc, str(target), ["res://tests/test_a.gd"],
        "res://.gqt-attr-select/abc/", current_recipe_pristine="pristine-1")
    assert err is None and covered == {5, 6}


def test_projection_unions_script_lane_line_facts(tmp_path):
    """投影并集必须含 script 车道行事实（before_all 等脚本级执行同样是
    该脚本的测试面），不得只剩 test 车道。"""
    project = tmp_path / "proj"
    (project / "tests").mkdir(parents=True)
    (project / "tests" / "test_a.gd").write_text("extends GutTest\n")
    target = project / "src" / "a.gd"
    target.parent.mkdir(parents=True)
    target.write_text(TARGET_SRC)
    doc = _write_line_primary_doc(tmp_path, project, "src/a.gd", TARGET_SRC,
                                  {"a": {5}, "b": {6}})
    rel = "src/a.gd"
    doc["arms"]["gut_full"]["script_to_lines"] = {"test_a.gd": {rel: [7]}}
    doc["instrumentable_lines"][rel] = [5, 6, 7]
    covered, err = coverlink.project_coverage_from_map(
        str(project), doc, str(target), ["res://tests/test_a.gd"],
        "res://.gqt-attr-select/abc/", current_recipe_pristine="pristine-1")
    assert err is None and covered == {5, 7}


def test_projection_loader_rejects_binding_target_sha_mismatch(tmp_path):
    """绑定包 map_file_sha256 失配必须显式拒绝——即便缓存条目自身
    source_sha1 仍与当前目标一致（binding 绑定的是映射快照，防御分层）。"""
    project = tmp_path / "proj"
    (project / "tests").mkdir(parents=True)
    (project / "tests" / "test_a.gd").write_text("extends GutTest\n")
    target = project / "src" / "a.gd"
    target.parent.mkdir(parents=True)
    target.write_text(TARGET_SRC)
    doc = _write_line_primary_doc(tmp_path, project, "src/a.gd", TARGET_SRC,
                                  {"a": {5}, "b": {6}})
    glob = "res://.gqt-attr-select/abc/"
    covered, err = coverlink.project_coverage_from_map(
        str(project), doc, str(target), ["res://tests/test_a.gd"], glob,
        current_recipe_pristine="pristine-1")
    assert err is None
    entry_path = coverlink.coverage_cache_path(str(project), str(target), glob)
    raw = json.loads(entry_path.read_text(encoding="utf-8"))
    # 只篡改 binding 的目标 sha；source_sha1 仍与当前文件一致
    raw["projection"]["map_file_sha256"] = "0" * 64
    entry_path.write_text(json.dumps(raw), encoding="utf-8")
    lines, err = coverlink.load_projected_coverage(
        str(project), str(target), glob,
        current_recipe_pristine="pristine-1")
    assert lines is None and "target changed since map" in err


def test_projection_loader_rejects_identity_drift(tmp_path):
    """投影 /2 条目身份包漂移 → 显式拒绝（不假过滤）；同身份命中。"""
    project = tmp_path / "proj"
    (project / "tests").mkdir(parents=True)
    (project / "tests" / "test_a.gd").write_text("extends GutTest\n")
    target = project / "src" / "a.gd"
    target.parent.mkdir(parents=True)
    target.write_text(TARGET_SRC)
    doc = _write_line_primary_doc(tmp_path, project, "src/a.gd", TARGET_SRC,
                                  {"a": {5}, "b": {6}})
    glob = "res://.gqt-attr-select/abc/"
    ident = {"inject_scheme": "x", "ops_version": "1",
             "gut_addon_hash": "h1"}
    covered, err = coverlink.project_coverage_from_map(
        str(project), doc, str(target), ["res://tests/test_a.gd"], glob,
        current_recipe_pristine="pristine-1", identity=ident)
    assert err is None
    # 同身份命中
    lines, err = coverlink.load_projected_coverage(
        str(project), str(target), glob, identity=ident,
        current_recipe_pristine="pristine-1", current_doc=doc)
    assert err is None and lines == {5}
    # 身份漂移（gut_addon_hash 变）→ 显式失效
    lines, err = coverlink.load_projected_coverage(
        str(project), str(target), glob,
        identity={**ident, "gut_addon_hash": "h2"},
        current_recipe_pristine="pristine-1")
    assert lines is None and "identity drift" in err
