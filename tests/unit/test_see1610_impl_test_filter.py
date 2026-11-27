"""SEE-1610 §SPEC-IMPL/§SPEC-JSON — test 级精准筛选单测。

覆盖：开关解析（默认开 / env 关断 / flag 优先）、marker 解析、planning
chunk 携带组内 tests_allow、verdict 披露（冷通道 marker → test_face 键；
worker 腿 allow_info → 同键）、summary test_filter 披露计数派生。
"""

from __future__ import annotations

import pytest

import attribution_fixtures

from godot_qa_toolkit.mutation import killbail
from godot_qa_toolkit.mutation import truth_select as _ts  # noqa: F401 — 先导破环
from godot_qa_toolkit.mutation.truth_lines import line_identity_sets


# ---------------------------------------------------------- 开关解析


def test_resolve_test_filter_default_on(monkeypatch):
    monkeypatch.delenv(killbail.ENV_TEST_FILTER, raising=False)
    assert killbail.resolve_test_filter() is True
    assert killbail.test_filter_disabled_by(None) is None


def test_resolve_test_filter_env_off(monkeypatch):
    for v in ("0", "false", "no"):
        monkeypatch.setenv(killbail.ENV_TEST_FILTER, v)
        assert killbail.resolve_test_filter() is False
        assert killbail.test_filter_disabled_by(None) == "env"
    monkeypatch.setenv(killbail.ENV_TEST_FILTER, "1")
    assert killbail.resolve_test_filter() is True


def test_resolve_test_filter_flag_wins(monkeypatch):
    monkeypatch.setenv(killbail.ENV_TEST_FILTER, "0")
    assert killbail.resolve_test_filter(True) is True
    assert killbail.test_filter_disabled_by(False) == "flag"
    monkeypatch.setenv(killbail.ENV_TEST_FILTER, "1")
    assert killbail.resolve_test_filter(False) is False


# ---------------------------------------------------------- marker 解析


def test_parse_test_face_marker():
    tail = "noise\nGQT_TEST_FACE requested=52 kept=31 removed=21 whole=1\ntail"
    d = killbail.parse_test_face_marker(tail)
    assert d == {"requested": 52, "kept": 31, "removed": 21,
                 "whole_script_fallback": 1}
    assert killbail.parse_test_face_marker("no marker here") is None
    assert killbail.parse_test_face_marker("") is None


# ---------------------------------------------------------- allowlist 数据


def _doc(**kw):
    base = {"line_to_tests": {"src/x.gd": {
                "10": ["test_a.gd::t1", "test_b.gd::t2"],
                "12": ["test_a.gd::t1"]}},
            "script_to_lines": {"test_c.gd": {"src/x.gd": [10]}},
            "test_to_functions": {},
            "test_to_lines": {"test_a.gd::t1": {"src/x.gd": [10]},
                              "test_b.gd::t2": {"src/x.gd": [10]}},
            "unattributed_files": []}
    base.update(kw)
    return {"arms": {"gut_full": base}}


def test_line_identity_sets_split_lanes():
    idents, lane = line_identity_sets(_doc(), "src/x.gd", {10, 12})
    assert idents[10] == ["test_a.gd::t1", "test_b.gd::t2"]
    assert idents[12] == ["test_a.gd::t1"]
    assert lane == {"test_c.gd"}


def test_unattributed_listing_does_not_change_selection():
    """D1 回退：unattributed_files 命中目标不再改变选面——稀疏行无整文件
    面，有行事实的行照常严格行级（正例 + 反例）。"""
    doc = _doc(unattributed_files=["src/x.gd"])
    idents, lane = line_identity_sets(doc, "src/x.gd", {99})
    assert 99 not in idents
    assert lane == set()
    idents_dense, lane_dense = line_identity_sets(doc, "src/x.gd", {10})
    assert idents_dense == {10: ["test_a.gd::t1", "test_b.gd::t2"]}
    assert lane_dense == {"test_c.gd"}


def test_legacy_map_without_list_keeps_strict_lines():
    """名单键缺失（legacy/增量产物）同语义：行级身份照常，无整文件面。"""
    doc = _doc()
    doc["arms"]["gut_full"].pop("unattributed_files")
    idents, lane = line_identity_sets(doc, "src/x.gd", {10})
    assert idents == {10: ["test_a.gd::t1", "test_b.gd::t2"]}
    assert lane == {"test_c.gd"}


def test_sparse_lines_get_no_fn_or_file_widening():
    """D1 回退：稀疏行不再 fn→file 扩面——test_to_functions 在册也不回退。"""
    doc = _doc(test_to_functions={"test_fn.gd::t9": ["src/x.gd::work"]})
    idents, lane = line_identity_sets(doc, "src/x.gd", {77, 78})
    assert 77 not in idents and 78 not in idents
    assert lane == set()


# ---------------------------------------------------------- verdict 披露


def test_cold_verdict_discloses_test_face():
    from godot_qa_toolkit.mutation import runhelp

    class _M:
        file = "src/x.gd"
        line = 3
        column = 4
        kind = "UOI"
        operator = "not"
        original = "x"
        mutated = "y"
        stable_id = "s#3#0"

    tail = ("GUT stream...\nGQT_TEST_FACE requested=4 kept=2 removed=2 "
            "whole=0\n* test_ok\n  [Passed]\n")
    verdict = runhelp._sm_classify(_M(), 0, tail, set(), False, "src", None,
                                   ".")
    assert verdict["test_face"] == {"requested": 4, "kept": 2, "removed": 2,
                                    "whole_script_fallback": 0}


def test_worker_leg_allow_info_becomes_test_face():
    from godot_qa_toolkit.mutation.worker_session import WorkerLegResult
    from godot_qa_toolkit.mutation.reporting import classify_worker_leg

    class _M:
        file = "src/x.gd"
        line = 3
        column = 4
        kind = "UOI"
        operator = "not"
        original = "x"
        mutated = "y"
        stable_id = "s#3#0"

    class _Own:
        def owner_of(self, name):
            return None

    leg = WorkerLegResult(leg=1, ok=True, gut_ran=True, fail_count=0)
    leg.allow_info = {"requested": 6, "kept": 6, "removed": 0, "whole": 2}
    verdict = classify_worker_leg(_M(), leg, set(), False,
                                  lambda: None, ".", "src")
    assert verdict["test_face"] == {"requested": 6, "kept": 6, "removed": 0,
                                    "whole_script_fallback": 2,
                                    "overlap_red": 0}


# ---------------------------------------------------------- summary 计数


def test_summary_test_filter_counts_from_active_allow(monkeypatch):
    import godot_qa_toolkit.mutation.runner as runner

    allow = [["test_a.gd", "t1"], ["test_a.gd", "t2"], ["test_c.gd", "*"]]
    monkeypatch.setattr(runner, "_ACTIVE_ALLOW_TESTS", allow)
    at = runner._ACTIVE_ALLOW_TESTS or []
    assert sum(1 for p in at if p[1] != "*") == 2
    assert sum(1 for p in at if p[1] == "*") == 1


# ------------------------------------------------- D3 --mutate-all 串行透传


def test_serial_fast_path_passes_mutate_all(monkeypatch):
    """SEE-1610 D3：--mutate-all 在 --jobs 1 串行快路径不得静默丢弃。"""
    from godot_qa_toolkit.mutation import executor

    captured = {}

    def fake_run_mutation(f, project_root, **kw):
        captured.update(kw)
        return {"ok": True, "summary": {}}

    monkeypatch.setattr(executor, "run_mutation", fake_run_mutation)
    executor._serial_fast_path(
        "f.gd", ".", 50, 600, False, None, "linux", None, 50, 2.0,
        True, {"file": "f.gd"}, mutate_all=True)
    assert captured.get("mutate_all") is True
    executor._serial_fast_path(
        "f.gd", ".", 50, 600, False, None, "linux", None, 50, 2.0,
        True, {"file": "f.gd"})
    assert captured.get("mutate_all") is False


# ------------------------------------------------- L1 对账闭合桶 + D2 机器面


def test_test_face_marker_v2_parses_accounting():
    tail = ("GQT_TEST_FACE requested=39 kept=38 removed=95 whole=2 "
            "matched=36 overlap_red=3")
    d = killbail.parse_test_face_marker(tail)
    assert d == {"requested": 39, "kept": 38, "removed": 95,
                 "whole_script_fallback": 2, "matched_pairs": 36,
                 "overlap_red": 3}
    old = killbail.parse_test_face_marker(
        "GQT_TEST_FACE requested=4 kept=2 removed=2 whole=0")
    assert old["requested"] == 4 and "overlap_red" not in old


def test_test_face_aggregate_all_verdict_classes():
    """D2：killed/survived/timeout/run_error/suspect 记录均可聚合对账。"""
    from godot_qa_toolkit.mutation.reporting import _emit_test_face_aggregate

    results = [
        {"verdict": "killed", "id": "a", "killed_by": ["t"],
         "test_face": {"requested": 4, "kept": 3, "removed": 1,
                       "whole_script_fallback": 0, "overlap_red": 0}},
        {"verdict": "survived",
         "test_face": {"requested": 4, "kept": 4, "removed": 0,
                       "whole_script_fallback": 0, "overlap_red": 0}},
        {"verdict": "timeout",
         "test_face": {"marker_missing": True}},
        {"verdict": "run_error",
         "test_face": {"marker_missing": True}},
        {"verdict": "suspect",
         "test_face": {"requested": 4, "kept": 4, "removed": 0,
                       "whole_script_fallback": 1, "overlap_red": 2}},
    ]
    summary = {}
    _emit_test_face_aggregate(summary, results)
    tf = summary["test_face"]
    assert tf["applied_legs"] == 5 and tf["marker_missing_legs"] == 2
    assert tf["kept"] == 11 and tf["removed"] == 1
    assert tf["whole_script_fallback"] == 1 and tf["overlap_red"] == 2
    assert tf["by_verdict"] == {"killed": 1, "survived": 1, "timeout": 1,
                                "run_error": 1, "suspect": 1}


def test_killed_certificate_and_store_carry_test_face():
    """D2：killed 证书与 resume store 条目均携带 test_face。"""
    from godot_qa_toolkit.mutation.reporting import _emit_killed_certificates
    from godot_qa_toolkit.mutation.diffengine import make_entry

    tf = {"requested": 4, "kept": 3, "removed": 1,
          "whole_script_fallback": 0, "overlap_red": 0}
    results = [{"verdict": "killed", "id": "a", "killed_by": ["t"],
                "test_face": tf}]
    summary = {}
    _emit_killed_certificates(summary, results)
    assert summary["killed_by_certificates"][0]["test_face"] == tf

    class _M:
        stable_id = "s"; scope_id = "f.gd"; line = 1; column = 1
        kind = "K"; original = "x"; mutated = "y"

    entry = make_entry(_M(), {"verdict": "killed", "test_face": tf},
                       "killed", 0, {}, 0.0)
    assert entry["test_face"] == tf


# ============================================ hardener 补强（SEE-1610 变异硬化）
# 以下用例补齐「透传/披露」判别面缺口：原 test_summary_test_filter_counts_
# from_active_allow 只对注入的列表自重述，未触达生产代码；selection→chunk→
# cfg→worker 的 allowlist 落点亦无断言。每条对应用户侧变异点（见硬化报告
# 变异矩阵 RN-*/PL-*/TS-*/DD-*/WS-*）。


def test_test_filter_disclosure_counts_and_scope(monkeypatch):
    """summary.test_filter 披露：identities/script_lane 计数与 scope 同源
    （D1 回退后 scope 恒 "line"）。"""
    import godot_qa_toolkit.mutation.runner as runner

    monkeypatch.delenv(killbail.ENV_TEST_FILTER, raising=False)
    allow = [["test_a.gd", "t1"], ["test_b.gd", "t2"], ["test_c.gd", "*"]]
    monkeypatch.setattr(runner, "_ACTIVE_ALLOW_SCOPE", "line")
    d = runner._test_filter_disclosure(allow)
    assert d["enabled"] is True and d["disabled_by"] is None
    assert d["applied"] is True
    assert d["allow_identities"] == 2
    assert d["script_lane_fallback"] == 1
    assert d["scope"] == "line"
    # 未下发（None）→ applied False、scope 归空、计数为 0
    e = runner._test_filter_disclosure(None)
    assert e["applied"] is False and e["scope"] is None
    assert e["allow_identities"] == 0 and e["script_lane_fallback"] == 0


def test_run_mutation_core_assembles_allow_from_selection_meta(monkeypatch):
    """selection_meta.tests_allow → _ACTIVE_ALLOW_TESTS/[script,name] 组装；
    chunk 无 scope 键（D1 回退）→ runner 默认 scope="line"。"""
    import godot_qa_toolkit.mutation.runner as runner

    monkeypatch.delenv(killbail.ENV_TEST_FILTER, raising=False)
    meta = {"tests_allow": {"identities": ["a.gd::t1", "b.gd::t2"],
                            "script_lane": ["c.gd"]}}
    ctx = {"path": "x.gd", "original_src": "", "identity": {},
           "mutants": [], "scope_hashes": {}, "total_sites": 0,
           "header_hash": "h"}
    monkeypatch.setattr(runner, "_core_setup", lambda *a, **k: (None, ctx))
    monkeypatch.setattr(runner, "_core_caliper_and_early",
                        lambda *a, **k: (None, "glob", meta))
    monkeypatch.setattr(runner, "_core_plan", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_no_sites_result", lambda p, s: {"ok": True})
    monkeypatch.setattr(runner, "_ACTIVE_ALLOW_TESTS", None)
    monkeypatch.setattr(runner, "_ACTIVE_ALLOW_SCOPE", None)
    assert runner._run_mutation_core("x.gd", ".", selection_meta=meta) == {
        "ok": True}
    assert runner._ACTIVE_ALLOW_TESTS == [
        ["a.gd", "t1"], ["b.gd", "t2"], ["c.gd", "*"]]
    assert runner._ACTIVE_ALLOW_SCOPE == "line"


def test_write_direct_cfg_carries_allow_tests(tmp_path):
    """cold 直驱 cfg 必须原样承载 allow_tests（[script,name]/[script,*]）。"""
    import json
    from godot_qa_toolkit.mutation import directdrive

    allow = [["a.gd", "t1"], ["b.gd", "*"]]
    p = directdrive.write_direct_cfg(str(tmp_path), "res://x.gd", "sha",
                                     "Auto", ["res://tests/a.gd"], [],
                                     allow_tests=allow)
    assert json.loads(p.read_text(encoding="utf-8"))["allow_tests"] == allow
    p2 = directdrive.write_direct_cfg(str(tmp_path), "res://x.gd", "sha",
                                      "Auto", [], [])
    assert json.loads(p2.read_text(encoding="utf-8"))["allow_tests"] == []


def test_worker_session_sends_and_reads_allow_tests(tmp_path, monkeypatch):
    """worker 腿 req 下发 allow_tests 且 resp.allow → allow_info 回读。"""
    import json
    from godot_qa_toolkit.mutation import worker_session as ws

    slot = tmp_path / "slot"
    (slot / ws.WORKER_DIRNAME).mkdir(parents=True)
    sess = ws.WorkerSession(str(slot), str(tmp_path), "linux")
    monkeypatch.setattr(sess, "start", lambda: None)
    sess._log_path = tmp_path / "worker-log.txt"  # 缺席 → OSError → 空失败集
    captured = {}

    def fake_await(resp_path, leg):
        captured["req"] = json.loads(
            (slot / ws.WORKER_DIRNAME / "req.json").read_text(encoding="utf-8"))
        return {"leg": leg, "hash_ok": True, "gut_ran": True, "fail": 0,
                "pass": 1, "reload": {},
                "allow": {"requested": 6, "kept": 6, "removed": 0, "whole": 2,
                          "matched_pairs": 6, "overlap_red": 0}}

    monkeypatch.setattr(sess, "_await_resp", fake_await)
    allow = [["a.gd", "t1"], ["b.gd", "*"]]
    r = sess.run_leg(1, "res://x.gd", "sha", [], None, allow_tests=allow)
    assert captured["req"]["allow_tests"] == allow
    assert r.allow_info == {"requested": 6, "kept": 6, "removed": 0,
                            "whole": 2, "matched_pairs": 6, "overlap_red": 0}


# ------------------------------------------------- selection→chunk allow 组装

_SRC_LOOP = ("extends RefCounted\n\nfunc add(a, b):\n\treturn a + b\n\n"
             "func sub(a, b):\n\treturn a - b\n\nfunc mul(a, b):\n"
             "\treturn a * b\n\nfunc div(a, b):\n\treturn a / b\n")


def _fixture_project(tmp_path):
    from attribution_fixtures import install_recipe
    proj = tmp_path / "proj"
    (proj / "tests" / "unit").mkdir(parents=True)
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub\n")
    for name, fn in (("test_a.gd", "t1"), ("test_b.gd", "t2")):
        (proj / "tests" / "unit" / name).write_text(
            f"extends GutTest\nfunc {fn}() -> void:\n\tpass\n", encoding="utf-8")
    target = proj / "calc.gd"
    target.write_text(_SRC_LOOP, encoding="utf-8")
    install_recipe(proj)
    return proj, target


def _feature_sha_1610(proj):
    """feature 指纹按目录实算（无 features 目录 → 空表，契约形状仍成立）。"""
    import hashlib

    feats = proj / "addons" / "gqt_gherkin" / "features"
    if not feats.is_dir():
        return {}
    return {p.stem.replace("-", "_"):
            hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(feats.glob("*.feature"))}


def _write_line_map_1610(proj, target):
    """行事实 /6 映射：4/7→test_a、7 兼 test_b、10→test_b；script 车道
    test_b 触达行 10；unattributed_files 含目标仅作选面不变性对照
    （D1 回退：名单不再触发整文件面，无 scope 键）。"""
    import hashlib
    import json
    from godot_qa_toolkit.measurement.recipe import build_recipe, recipe_hash

    recipe, err = build_recipe(proj)
    assert err is None, err
    raw = recipe_hash({k: v for k, v in recipe.items() if k != "recipe_hash"})
    pristine = attribution_fixtures.pristine_recipe_hash(proj)
    rel = str(target.resolve().relative_to(proj.resolve()))
    line_to_tests = {4: ["test_a.gd::t1"],
                     7: ["test_a.gd::t1", "test_b.gd::t2"],
                     10: ["test_b.gd::t2"]}
    test_to_lines: dict[str, dict[str, list[int]]] = {}
    for ln, ids in line_to_tests.items():
        for ident in ids:
            test_to_lines.setdefault(ident, {}).setdefault(rel, []).append(ln)
    face = {str(p.relative_to(proj)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((proj / "tests").rglob("*.gd"))}
    arm = {
        "test_to_files": {i: [rel] for i in test_to_lines},
        "test_to_functions": {}, "script_to_files": {}, "script_to_functions": {},
        "file_to_tests": {rel: sorted(test_to_lines)}, "file_to_scripts": {},
        "test_to_lines": {i: {rel: sorted(v)} for i, v in test_to_lines.items()},
        "script_to_lines": {"test_b.gd": {rel: [10]}},
        "line_to_tests": {rel: {str(ln): sorted(ids)
                                for ln, ids in line_to_tests.items()}},
        "unattributed_files": [rel],
    }
    doc = {"schema_version": "test-attribution/7",
           "report_recipe_hash": raw, "recipe_hash_pristine": pristine,
           "project_root": str(proj), "identity": "res://script::test_name",
           "causal": {"enabled": False},
           "file_sha256": {rel: hashlib.sha256(
               target.read_bytes()).hexdigest()},
           "target_files": [rel], "tests_face": face,
           "instrumentable_lines": {rel: list(range(1, 14))},
           "arms": {"gut_full": arm},
           "arms_state": {"gut_full": {"state": "valid", "bundles": 1,
                                       "pairs": 1}},
           "notes": [],
           # C3-R20 迁移（B）：契约认可的地图带判定表与 feature 指纹
           "test_verdicts": {i: {"verdict": "green",
                                 "classification": "pre_existing"}
                             for i in test_to_lines},
           "feature_sha256": _feature_sha_1610(proj)}
    out = proj / ".qa-cache" / "attribution-map.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return str(out)


def test_line_selection_and_chunk_carry_tests_allow(tmp_path):
    """resolve（--lines 串行）与 planning chunk 同源组装 tests_allow：
    identities 并集、script 车道整脚本保留；严格行级——unattributed
    名单在场不改变选面、稀疏行只具名披露（D1 回退后无 scope 键）。"""
    from godot_qa_toolkit.mutation import truth_select
    from godot_qa_toolkit.mutation.executor import _line_group_chunks
    from godot_qa_toolkit.mutation.ops import _collect_mutations
    from godot_qa_toolkit.mutation.scopemanifest import build_manifest

    proj, target = _fixture_project(tmp_path)
    map_path = _write_line_map_1610(proj, target)
    # 1) resolve 侧：请求含稀疏行 5（无行事实）
    sel = truth_select.resolve_line_group_selection(
        proj, str(target), [4, 7, 10, 5])
    ta = sel["tests_allow"]
    assert set(ta) == {"identities", "script_lane"}
    assert set(ta["identities"]) == {"test_a.gd::t1", "test_b.gd::t2"}
    assert ta["script_lane"] == ["test_b.gd"]
    assert sel["no_mapped_tests"] == [5]
    # 2) planning chunk 侧
    src = target.read_text(encoding="utf-8")
    _, _, mutants, _ = build_manifest(src, str(target),
                                      _collect_mutations(src, str(target)))
    meta = {"source": "runtime_mapping",
            "tests": ["res://tests/unit/test_a.gd", "res://tests/unit/test_b.gd"],
            "mapping": {"path": map_path}}
    chunks, groups, _nm = _line_group_chunks(
        str(target), str(proj), "res://tests/", "linux", mutants, meta, None,
        2.0, 60, 50, False, None, run_id="r1", identity=None,
        worker_channel=None, total_sites=9)
    by_sig = {frozenset(g["signature"]): ch["selection_meta"]["tests_allow"]
              for ch, g in zip(chunks, groups)}
    a = by_sig[frozenset({"test_a.gd"})]
    b = by_sig[frozenset({"test_b.gd"})]
    ab = by_sig[frozenset({"test_a.gd", "test_b.gd"})]
    assert set(a) == {"identities", "script_lane"} == set(b) == set(ab)
    assert a["identities"] == ["test_a.gd::t1"]   # 组内行身份（行 4 仅 test_a）
    assert a["script_lane"] == []                 # test_a 组不触达 script 车道
    assert b["identities"] == ["test_b.gd::t2"]   # 行 10 仅 test_b
    assert b["script_lane"] == ["test_b.gd"]      # test_b 组命中 script 车道
    assert ab["identities"] == ["test_a.gd::t1", "test_b.gd::t2"]  # 行 7 混合组


def test_run_mutation_core_respects_off_switch(monkeypatch):
    """env 关断逃生门：开关 OFF 时即便 chunk 携带 tests_allow 也不下发。"""
    import godot_qa_toolkit.mutation.runner as runner

    monkeypatch.setenv(killbail.ENV_TEST_FILTER, "0")
    meta = {"tests_allow": {"identities": ["a.gd::t1"], "script_lane": [],
                            "scope": "line"}}
    ctx = {"path": "x.gd", "original_src": "", "identity": {},
           "mutants": [], "scope_hashes": {}, "total_sites": 0,
           "header_hash": "h"}
    monkeypatch.setattr(runner, "_core_setup", lambda *a, **k: (None, ctx))
    monkeypatch.setattr(runner, "_core_caliper_and_early",
                        lambda *a, **k: (None, "glob", meta))
    monkeypatch.setattr(runner, "_core_plan", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_no_sites_result", lambda p, s: {"ok": True})
    monkeypatch.setattr(runner, "_ACTIVE_ALLOW_TESTS", None)
    runner._run_mutation_core("x.gd", ".", selection_meta=meta)
    assert runner._ACTIVE_ALLOW_TESTS is None


class _Mut:
    file = "src/x.gd"; line = 3; column = 4; kind = "UOI"
    operator = "not"; original = "x"; mutated = "y"; stable_id = "s#3#0"


def test_worker_leg_marker_missing_fallback_when_allow_active(monkeypatch):
    """D2：过滤启用但 worker 腿无 allow_info → test_face marker_missing 兜底。"""
    import godot_qa_toolkit.mutation.runner as runner
    from godot_qa_toolkit.mutation.worker_session import WorkerLegResult
    from godot_qa_toolkit.mutation.reporting import classify_worker_leg

    monkeypatch.setattr(runner, "_ACTIVE_ALLOW_TESTS", [["a.gd", "t1"]])
    leg = WorkerLegResult(leg=1, ok=True, gut_ran=True, fail_count=0)
    verdict = classify_worker_leg(_Mut(), leg, set(), False,
                                  lambda: None, ".", "src")
    assert verdict["test_face"]["marker_missing"] is True


def test_cold_verdict_marker_missing_when_allow_active(monkeypatch):
    """D2：过滤启用但 cold 腿无 marker（timeout/注入失败）→ marker_missing。"""
    from godot_qa_toolkit.mutation import runhelp, runner

    monkeypatch.setattr(runner, "_ACTIVE_ALLOW_TESTS", [["a.gd", "t1"]])
    # 有 GUT 运行证据（过 L4 门）但无 GQT_TEST_FACE marker
    tail = "* test_ok\n  [Passed]\n"
    verdict = runhelp._sm_classify(_Mut(), 0, tail, set(), False,
                                   "src", None, ".")
    assert verdict["test_face"]["marker_missing"] is True
