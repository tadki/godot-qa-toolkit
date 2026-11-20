"""SEE-1610 §SPEC-IMPL/§SPEC-JSON — test 级精准筛选单测。

覆盖：开关解析（默认开 / env 关断 / flag 优先）、marker 解析、planning
chunk 携带组内 tests_allow、verdict 披露（冷通道 marker → test_face 键；
worker 腿 allow_info → 同键）、summary test_filter 披露计数派生。
"""

from __future__ import annotations

import pytest

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
    idents, lane, scope = line_identity_sets(_doc(), "src/x.gd", {10, 12})
    assert idents[10] == ["test_a.gd::t1", "test_b.gd::t2"]
    assert idents[12] == ["test_a.gd::t1"]
    assert lane == {"test_c.gd"}
    assert scope == "line"


def test_closure_whole_file_when_unattributed_listed():
    """D1：窗口归因有损文件 → 整文件面（allow 含文件级全部测试）。"""
    doc = _doc(unattributed_files=["src/x.gd"])
    idents, lane, scope = line_identity_sets(doc, "src/x.gd", {99})
    assert scope == "file"
    assert "test_a.gd::t1" in idents[99] and "test_b.gd::t2" in idents[99]
    assert lane == {"test_c.gd"}


def test_closure_whole_file_when_list_absent_legacy_map():
    """名单键缺失（legacy/增量产物）→ 无法证伪归因完整 → 整文件面。"""
    doc = _doc()
    doc["arms"]["gut_full"].pop("unattributed_files")
    _idents, _lane, scope = line_identity_sets(doc, "src/x.gd", {10})
    assert scope == "file"


def test_closure_sparse_line_fn_then_file_ladder():
    """稀疏行（无行事实）：fn 级 ∪；fn 边也空 → 文件级。"""
    doc = _doc(test_to_functions={"test_fn.gd::t9": ["src/x.gd::work"]})
    idents, _lane, scope = line_identity_sets(
        doc, "src/x.gd", {77}, registry={77: "src/x.gd::work"})
    assert scope == "line"
    assert "test_fn.gd::t9" in idents[77]
    # fn 边空 → 文件级回退
    idents2, _l2, _s2 = line_identity_sets(
        doc, "src/x.gd", {78}, registry={78: "src/x.gd::other"})
    assert "test_a.gd::t1" in idents2[78]


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
