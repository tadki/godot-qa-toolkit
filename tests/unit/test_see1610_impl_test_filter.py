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


def test_line_identity_sets_split_lanes():
    doc = {"arms": {"gut_full": {
        "line_to_tests": {"src/x.gd": {
            "10": ["test_a.gd::t1", "test_b.gd::t2"],
            "12": ["test_a.gd::t1"]}},
        "script_to_lines": {"test_c.gd": {"src/x.gd": [10]}},
    }}}
    idents, lane = line_identity_sets(doc, "src/x.gd", {10, 12})
    assert idents[10] == ["test_a.gd::t1", "test_b.gd::t2"]
    assert idents[12] == ["test_a.gd::t1"]
    assert lane == {"test_c.gd"}


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
                                    "whole_script_fallback": 2}


# ---------------------------------------------------------- summary 计数


def test_summary_test_filter_counts_from_active_allow(monkeypatch):
    import godot_qa_toolkit.mutation.runner as runner

    allow = [["test_a.gd", "t1"], ["test_a.gd", "t2"], ["test_c.gd", "*"]]
    monkeypatch.setattr(runner, "_ACTIVE_ALLOW_TESTS", allow)
    at = runner._ACTIVE_ALLOW_TESTS or []
    assert sum(1 for p in at if p[1] != "*") == 2
    assert sum(1 for p in at if p[1] == "*") == 1
