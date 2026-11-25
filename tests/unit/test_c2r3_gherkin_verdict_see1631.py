"""SEE-1631 C2-R3：gherkin 增量 verdict 语义完备性（§SPEC-028）的行为判据。

- gherkin_dropped 的 feature 身份从 verdict 表剔除；
- 重跑键按重跑腿全部执行账判定（多分片/多 rep 任一失败都算失败）；
- 失败或不一致不得维持旧值；红的分类与 §SPEC-017 车道一致；
- gut 侧：基线 pre_existing 重跑仍失败保持 pre_existing，新增失败才 new。
"""

from __future__ import annotations

from godot_qa_toolkit.measurement.increment import (
    _verdict_gherkin_rerun,
    _verdict_recompute_rerun,
)


def _gh(ident: str) -> "str | None":
    from godot_qa_toolkit.measurement.attribution import _gherkin_feature_of

    return _gherkin_feature_of(ident)


# --------------------------------------------------------------------------
# gherkin 侧
# --------------------------------------------------------------------------

def test_gherkin_dropped_features_removed_from_table():
    merged = {
        "gherkin://feature/alpha::s0_01": {"verdict": "green"},
        "gherkin://feature/beta::s0_01": {"verdict": "green"},
    }
    _verdict_gherkin_rerun(merged, {"executions": []},
                           gherkin_rerun=set(),
                           gherkin_dropped={"alpha"})
    assert "gherkin://feature/alpha::s0_01" not in merged
    assert "gherkin://feature/beta::s0_01" in merged


def test_gherkin_rerun_all_executions_pass_green():
    merged = {
        "gherkin://feature/beta::s0_01": {"verdict": "red",
                                          "classification": "new",
                                          "script": "res://x/test_beta.gd"},
    }
    fresh = {"executions": [{"rc": 0, "out": "ok"},
                            {"rc": 0, "out": "ok"}]}
    _verdict_gherkin_rerun(merged, fresh, gherkin_rerun={"beta"},
                           gherkin_dropped=set())
    assert merged["gherkin://feature/beta::s0_01"]["verdict"] == "green"


def test_gherkin_rerun_any_execution_failure_not_green():
    """多分片/多 rep 任一失败都算失败——不得维持/翻转为 green。"""
    merged = {
        "gherkin://feature/beta::s0_01": {"verdict": "green"},
    }
    fresh = {"executions": [{"rc": 0, "out": "ok"},
                            {"rc": 1, "out": "fail"}]}
    _verdict_gherkin_rerun(merged, fresh, gherkin_rerun={"beta"},
                           gherkin_dropped=set())
    assert merged["gherkin://feature/beta::s0_01"]["verdict"] == "red"


def test_gherkin_rerun_baseline_pre_existing_keeps_classification():
    merged = {
        "gherkin://feature/beta::s0_01": {
            "verdict": "red", "classification": "pre_existing",
            "script": "res://x/test_beta.gd"},
    }
    fresh = {"executions": [{"rc": 1, "out": "fail"}]}
    _verdict_gherkin_rerun(merged, fresh, gherkin_rerun={"beta"},
                           gherkin_dropped=set())
    entry = merged["gherkin://feature/beta::s0_01"]
    assert entry["verdict"] == "red"
    assert entry["classification"] == "pre_existing"


def test_gherkin_rerun_baseline_suspected_intolerant_keeps_classification():
    merged = {
        "gherkin://feature/beta::s0_01": {
            "verdict": "red", "classification": "suspected_intolerant",
            "script": "res://x/test_beta.gd"},
    }
    fresh = {"executions": [{"rc": 1, "out": "fail"}]}
    _verdict_gherkin_rerun(merged, fresh, gherkin_rerun={"beta"},
                           gherkin_dropped=set())
    entry = merged["gherkin://feature/beta::s0_01"]
    assert entry["verdict"] == "red"
    assert entry["classification"] == "suspected_intolerant"


def test_gherkin_rerun_baseline_green_failure_classified_new():
    merged = {
        "gherkin://feature/beta::s0_01": {"verdict": "green"},
    }
    fresh = {"executions": [{"rc": 1, "out": "fail"}]}
    _verdict_gherkin_rerun(merged, fresh, gherkin_rerun={"beta"},
                           gherkin_dropped=set())
    entry = merged["gherkin://feature/beta::s0_01"]
    assert entry["verdict"] == "red"
    assert entry["classification"] == "new"


# --------------------------------------------------------------------------
# gut 侧：基线分类不被静默改写
# --------------------------------------------------------------------------

def test_gut_pre_existing_rerun_still_fails_keeps_pre_existing():
    merged = {
        "test_red.gd::test_red_always_fails": {
            "verdict": "red", "classification": "pre_existing",
            "script": "res://tests/unit/test_red.gd"},
    }
    per_script = {"test_red.gd": (1, {"test_red_always_fails"})}
    _verdict_recompute_rerun(merged, per_script)
    entry = merged["test_red.gd::test_red_always_fails"]
    assert entry["verdict"] == "red"
    assert entry["classification"] == "pre_existing"


def test_gut_green_rerun_now_fails_classified_new():
    merged = {
        "test_a.gd::test_green": {"verdict": "green"},
    }
    per_script = {"test_a.gd": (1, {"test_green"})}
    _verdict_recompute_rerun(merged, per_script)
    entry = merged["test_a.gd::test_green"]
    assert entry["verdict"] == "red"
    assert entry["classification"] == "new"


def test_gut_rerun_passes_green():
    merged = {
        "test_a.gd::test_x": {"verdict": "red",
                              "classification": "pre_existing"},
    }
    per_script = {"test_a.gd": (0, set())}
    _verdict_recompute_rerun(merged, per_script)
    assert merged["test_a.gd::test_x"] == {"verdict": "green"}


def test_no_duplicate_verdict_function_definitions():
    """交付前自查：同名函数重复定义（C2-R2 缺口直接成因）不得复发。"""
    import ast
    from pathlib import Path

    src = Path("src/godot_qa_toolkit/measurement/increment.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    seen: dict[str, int] = {}
    dup = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in seen:
                dup.append(node.name)
            seen[node.name] = node.lineno
    assert not dup, f"重复定义的函数：{dup}"
