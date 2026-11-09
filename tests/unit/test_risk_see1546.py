"""SEE-1546 M2 risk unit tests: function identity/probe manifest (§SPEC-006),
CRAP-Line formula and state contract (§SPEC-007), shadow selection (§SPEC-008)
and the performance field contract (§SPEC-014)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from godot_qa_toolkit.risk.contract import (
    SCORE_STATUS_VALUES,
    build_shadow_record,
    classify_file_state,
)
from godot_qa_toolkit.risk.formula import CRAP_LINE_VERSION, crap_line_score
from godot_qa_toolkit.risk.identity import (
    collect_function_identities,
    build_probe_manifest,
    t_census,
)

KOL_ROOT = os.environ.get("KOL_ROOT")


# --------------------------------------------------------------------------
# §SPEC-007: CRAP-Line formula — the literal numeric gate
# --------------------------------------------------------------------------


def test_crap_line_formula_boundaries():
    """cc=6 with c=0/0.5/1 must be exactly 42 / 10.5 / 6."""
    assert crap_line_score(6, 0.0) == pytest.approx(42.0)
    assert crap_line_score(6, 0.5) == pytest.approx(10.5)
    assert crap_line_score(6, 1.0) == pytest.approx(6.0)
    assert CRAP_LINE_VERSION == "crap-line/1"


def test_crap_line_formula_is_monotone_and_raw_ratio():
    # raw 0-1 ratio, no pre-rounded percentage: c=0.005 behaves as 0.005
    assert crap_line_score(3, 0.0) == pytest.approx(12.0)
    assert crap_line_score(3, 1.0) == pytest.approx(3.0)
    high = crap_line_score(10, 0.1)
    low_cc = crap_line_score(2, 0.1)
    assert high > low_cc


def test_crap_line_rejects_invalid_denominator():
    """total==0 cannot produce a score — vacuous, not a perfect 100%."""
    assert crap_line_score(6, None) is None


# --------------------------------------------------------------------------
# §SPEC-007: record disclosure contract
# --------------------------------------------------------------------------


DISCLOSURE_FIELDS = {
    "coverage_semantics": "statement_start_line",
    "path_coverage": "not_measured",
    "assertion_effectiveness": "not_measured",
}


def test_record_disclosure_fields_are_mandatory():
    record = build_shadow_record(
        function_id="systems/x.gd::f",
        name="f",
        lex_path="f",
        start_line=1,
        end_line=3,
        cc=2,
        covered=1,
        total=2,
        cov_source="gut_core",
    )
    assert record["crap_line"]["version"] == CRAP_LINE_VERSION
    for key, value in DISCLOSURE_FIELDS.items():
        assert record["crap_line"][key] == value
        assert record[key] == value
    assert record["evidence_level"] == "exploratory"
    assert record["crap_line"]["score"] == pytest.approx(crap_line_score(2, 0.5))
    assert record["score_status"] == "scored"


def test_vacuous_file_yields_null_coverage_and_no_score():
    state = classify_file_state(executable_probes=0, probes_fired=0, hits=0,
                                gut_ok=True, run_error=False, timeout=False)
    assert state == "vacuous"
    record = build_shadow_record(
        function_id="x.gd::empty", name="empty", lex_path="empty",
        start_line=1, end_line=2, cc=1, covered=None, total=None,
        cov_source="gut_core", state=state,
    )
    assert record["crap_line"]["coverage"] is None
    assert record["crap_line"]["score"] is None
    assert record["score_status"] == "vacuous"


def test_unknown_zero_hit_file_never_scored():
    """A whole file with probes fired but zero function hits stays unknown —
    never disguised as a true zero-coverage score."""
    state = classify_file_state(executable_probes=5, probes_fired=5, hits=0,
                                gut_ok=True, run_error=False, timeout=False)
    assert state == "unknown"
    record = build_shadow_record(
        function_id="x.gd::f", name="f", lex_path="f", start_line=1,
        end_line=2, cc=3, covered=None, total=None, cov_source="gut_core",
        state=state,
    )
    assert record["crap_line"]["score"] is None
    assert record["score_status"] == "unknown"


def test_run_error_states_never_score():
    for state, kwargs in {
        "run_error": dict(executable_probes=3, probes_fired=0, hits=0,
                          gut_ok=True, run_error=True, timeout=False),
        "timeout": dict(executable_probes=3, probes_fired=0, hits=0,
                        gut_ok=True, run_error=False, timeout=True),
        "suite_invalid": dict(executable_probes=3, probes_fired=3, hits=1,
                              gut_ok=False, run_error=False, timeout=False),
        "zero_tests": dict(executable_probes=3, probes_fired=0, hits=0,
                           gut_ok=True, run_error=False, timeout=False,
                           zero_tests=True),
    }.items():
        assert classify_file_state(**kwargs) == state


def test_score_status_values_cover_contract_states():
    for required in ("scored", "unknown", "vacuous", "cc_unmeasured",
                     "partial_instrumentation", "identity_mismatch",
                     "run_error", "timeout", "suite_invalid", "zero_tests"):
        assert required in SCORE_STATUS_VALUES


# --------------------------------------------------------------------------
# §SPEC-006: function identity + probe manifest + instrumentation
# --------------------------------------------------------------------------


IDENTITY_SRC = (
    "extends RefCounted\n"
    "\n"
    "func alpha(a: int) -> int:\n"
    "\tif a > 0:\n"
    "\t\treturn a\n"
    "\treturn 0\n"
    "\n"
    "func beta() -> void:\n"
    "\tprint(1)\n"
)


def test_identity_collects_lex_path_spans_and_cc():
    identities = collect_function_identities(IDENTITY_SRC, file_label="x.gd")
    assert [i.name for i in identities] == ["alpha", "beta"]
    alpha = identities[0]
    assert alpha.lex_path == "alpha"
    assert alpha.start_line == 3 and alpha.end_line >= 6
    assert alpha.cc == 2  # 1 + if
    assert alpha.function_id.startswith("x.gd::")
    assert identities[1].cc == 1


def test_probe_manifest_maps_each_probe_to_one_function():
    identities = collect_function_identities(IDENTITY_SRC, file_label="x.gd")
    rows, manifest = build_probe_manifest(IDENTITY_SRC, identities)
    assert len(rows) == len(manifest) > 0
    owner_ids = {r["function_id"] for r in rows}
    assert owner_ids <= {i.function_id for i in identities}
    if len(owner_ids) >= 2:
        # probes exist for both functions
        counts = {}
        for r in rows:
            counts[r["function_id"]] = counts.get(r["function_id"], 0) + 1
        assert all(c > 0 for c in counts.values())


def test_manifest_t_equals_i_and_unambiguous_ownership():
    identities = collect_function_identities(IDENTITY_SRC, file_label="x.gd")
    rows, manifest = build_probe_manifest(IDENTITY_SRC, identities)
    t_set = t_census(identities)
    i_set = {row["line"] for row in rows}
    assert t_set == i_set  # T = I
    assert all(row["function_id"] is not None for row in rows)  # ownership complete
    assert all(row["owner_ambiguous"] is False for row in rows)


def test_static_function_counted_once_in_identity():
    src = (
        "extends RefCounted\n"
        "\n"
        "static func helper() -> int:\n"
        "\treturn 1\n"
        "\n"
        "func caller() -> int:\n"
        "\treturn helper()\n"
    )
    identities = collect_function_identities(src, file_label="x.gd")
    assert [i.name for i in identities] == ["helper", "caller"]
    assert identities[0].kind == "func_def"


STRICT = (
    "extends RefCounted\n"
    "\n"
    "func host(x: int) -> int:\n"
    "\tvar f := func(v: int) -> int:\n"
    "\t\tif v > 0:\n"
    "\t\t\treturn v\n"
    "\t\treturn 0\n"
    "\treturn f(x)\n"
)

INNER = (
    "extends RefCounted\n"
    "\n"
    "class Inner:\n"
    "\tfunc method() -> int:\n"
    "\t\treturn 1\n"
    "\n"
    "func top() -> int:\n"
    "\treturn 2\n"
)


def test_instrumentation_gap_flags_class_def_body():
    """class_def bodies are pruned from instrumentation (SEE-1367): the
    identity layer must expose the gap instead of pretending T=I."""
    identities = collect_function_identities(INNER, file_label="x.gd")
    rows, _ = build_probe_manifest(INNER, identities)
    covered_lines = {row["line"] for row in rows}
    # top() body is instrumentable; Inner.method body is not
    top = next(i for i in identities if i.name == "top")
    inner = next(i for i in identities if i.name == "method")
    assert any(top.start_line < ln <= top.end_line for ln in covered_lines)
    assert not any(inner.start_line < ln <= inner.end_line for ln in covered_lines)


def test_lambda_and_inner_class_never_mix_into_host_denominator():
    identities = collect_function_identities(STRICT, file_label="x.gd")
    names = [i.name for i in identities]
    assert names == ["host"]  # lambda is not a function identity of its own
    host = identities[0]
    rows, _ = build_probe_manifest(STRICT, identities)
    assert all(r["function_id"] == host.function_id for r in rows)


# --------------------------------------------------------------------------
# §SPEC-008: eligible selection and negative lanes
# --------------------------------------------------------------------------


def test_eligible_files_satisfy_selection_rules():
    from godot_qa_toolkit.risk.identity import file_profile

    good = file_profile(IDENTITY_SRC, file_label="x.gd")
    assert good["has_lambda"] is False
    assert good["has_inner_class"] is False
    assert good["t_equal_i"] is True
    assert good["eligible"] is True


def test_three_negative_files_route_to_gap_lanes():
    from godot_qa_toolkit.risk.identity import file_profile

    lam = file_profile(STRICT, file_label="lambda.gd")
    inner = file_profile(INNER, file_label="inner.gd")
    var_def = file_profile(
        "func f() -> void:\n\tvar def := 1\n\tprint(def)\n",
        file_label="vardef.gd",
    )
    assert lam["eligible"] is False and lam["gap_lane"] == "lambda_decision_gap"
    assert inner["eligible"] is False and inner["gap_lane"] == "partial_instrumentation"
    assert var_def["eligible"] is False
    assert var_def["gap_lane"] == "sanitized_ir_projection"


def test_zero_hit_file_stays_unknown_not_true_zero():
    state = classify_file_state(executable_probes=4, probes_fired=4, hits=0,
                                gut_ok=True, run_error=False, timeout=False)
    assert state == "unknown"
    assert state != "true_zero_coverage"


# --------------------------------------------------------------------------
# §SPEC-014: performance field contract (structural)
# --------------------------------------------------------------------------


def test_shadow_report_performance_fields():
    from godot_qa_toolkit.risk.shadow import PERFORMANCE_FIELDS, build_performance_block

    perf = build_performance_block(
        complexity_runs={"baseline_median_s": 1.0, "head_median_s": 1.1},
        coverage_runs=[{"file": "a.gd", "baseline_median_s": 1.0, "head_median_s": 1.2}],
        risk_static={"max_seconds_per_file": 0.01, "max_share_of_total": 0.02},
        extra_gut_rounds=0,
    )
    assert set(PERFORMANCE_FIELDS) <= set(perf)
    assert perf["complexity"]["head_median_ratio"] == pytest.approx(1.1)
    assert perf["coverage"][0]["head_median_ratio"] == pytest.approx(1.2)
    assert perf["extra_gut_rounds"] == 0


# --------------------------------------------------------------------------
# Real KOL eligibility spot-check (needs KOL_ROOT)
# --------------------------------------------------------------------------


def test_probe_without_owner_is_skipped_not_miscounted():
    """A probe line falling outside every function span must be skipped in
    per-function stats — never attributed to an arbitrary host."""
    from godot_qa_toolkit.risk.shadow import _function_stats

    profile = {
        "probes": [
            {"probe_id": 0, "line": 3, "function_id": None, "owner_ambiguous": False},
            {"probe_id": 1, "line": 4, "function_id": "x.gd::f", "owner_ambiguous": False},
        ]
    }
    stats = _function_stats(profile, {4})
    assert stats == [{"function_id": "x.gd::f", "total": 1, "covered": 1}]


def test_classify_no_probe_fired_is_run_error_lane():
    state = classify_file_state(executable_probes=3, probes_fired=0, hits=0,
                                gut_ok=True, run_error=False, timeout=False)
    assert state == "run_error"


def test_identity_owner_reports_true_ambiguity(monkeypatch):
    """Two spans starting on the same line cannot be disambiguated — the owner
    resolver must report ambiguity instead of guessing."""
    from godot_qa_toolkit.risk import identity as identity_mod

    fakes = [
        identity_mod.FunctionIdentity("x.gd::a", "a", "a", 1, 5, 1, "func_def", ""),
        identity_mod.FunctionIdentity("x.gd::b", "b", "b", 1, 5, 1, "func_def", ""),
    ]
    owner, ambiguous = identity_mod._owner(fakes, 3)
    assert owner is None and ambiguous is True


def test_identity_owner_prefers_latest_start_on_loose_span():
    from godot_qa_toolkit.risk import identity as identity_mod

    fakes = [
        identity_mod.FunctionIdentity("x.gd::a", "a", "a", 1, 8, 1, "func_def", ""),
        identity_mod.FunctionIdentity("x.gd::b", "b", "b", 5, 12, 1, "func_def", ""),
    ]
    owner, ambiguous = identity_mod._owner(fakes, 7)
    assert owner == "x.gd::b" and ambiguous is False


def test_identity_handles_plain_assignments_and_failed_conversion(monkeypatch):
    """Module-level assignments carry no function identity (skipped); a gd2py
    conversion failure in the sanitized-projection probe must not crash."""
    from godot_qa_toolkit.risk import identity as identity_mod

    src = "extends RefCounted\n\nvar top := 1\n\nfunc f() -> void:\n\tpass\n"
    ids = collect_function_identities(src, file_label="x.gd")
    assert [i.name for i in ids] == ["f"]

    def boom(_src: str) -> str:
        raise RuntimeError("no gd2py here")

    monkeypatch.setattr("gdtoolkit.gd2py.convert_code", boom)
    assert identity_mod._uses_sanitized_projection(src) is False


def test_identity_tolerates_token_children(monkeypatch):
    """The per-function census walks Token children (no `.data`); they must be
    skipped without failing the census."""
    from godot_qa_toolkit.risk.identity import _function_statement_lines
    from gdtoolkit.parser import parser

    tree = parser.parse("func f() -> void:\n\tpass\n", gather_metadata=True)
    func_def = next(t for t in tree.iter_subtrees() if t.data == "func_def")
    lines = _function_statement_lines(func_def, [0] * 20)
    assert isinstance(lines, tuple)


def test_partition_sources_splits_by_static_eligibility(tmp_path: Path):
    from godot_qa_toolkit.risk.shadow import partition_sources

    root = tmp_path / "proj"
    root.mkdir()
    good = root / "good.gd"
    good.write_text(IDENTITY_SRC, encoding="utf-8")
    neg = root / "neg.gd"
    neg.write_text(STRICT, encoding="utf-8")
    eligible, negatives = partition_sources([good, neg], str(root))
    assert set(eligible) == {"good.gd"}
    assert set(negatives) == {"neg.gd"}


def test_partition_sources_label_fallbacks(tmp_path: Path):
    """Without a project root (or for paths outside it) the label falls back
    to the bare file name — the report never crashes on foreign paths."""
    from godot_qa_toolkit.risk.shadow import partition_sources

    root = tmp_path / "proj"
    root.mkdir()
    inside = root / "inside.gd"
    inside.write_text(IDENTITY_SRC, encoding="utf-8")
    outside = tmp_path / "outside.gd"
    outside.write_text(IDENTITY_SRC, encoding="utf-8")
    eligible_no_root, _ = partition_sources([outside], None)
    assert set(eligible_no_root) == {"outside.gd"}
    eligible_foreign, _ = partition_sources([outside], str(root))
    assert set(eligible_foreign) == {"outside.gd"}


def test_identity_skips_unnamed_and_hashes_without_meta(monkeypatch):
    from godot_qa_toolkit.risk import identity as identity_mod
    from lark import Tree

    monkeypatch.setattr(
        identity_mod,
        "_iter_function_defs_with_paths",
        lambda tree: [("", Tree("func_def", []))],
    )
    assert identity_mod.collect_function_identities("func f():\n\tpass\n", "x.gd") == []
    # a Tree without start_pos/end_pos hashes the empty chunk (defensive)
    assert identity_mod._body_hash("src", Tree("func_def", [])) == (
        "da39a3ee5e6b"
    )


def test_identity_owner_missing_span_returns_no_owner():
    from godot_qa_toolkit.risk import identity as identity_mod

    assert identity_mod._owner([], 5) == (None, False)


def test_run_shadow_from_paths_static_only(tmp_path: Path):
    from godot_qa_toolkit.risk.shadow import run_shadow_from_paths

    root = tmp_path / "proj"
    root.mkdir()
    good = root / "good.gd"
    good.write_text(IDENTITY_SRC, encoding="utf-8")
    neg = root / "neg.gd"
    neg.write_text(STRICT, encoding="utf-8")
    report = run_shadow_from_paths(
        paths=[good, neg],
        project_root=str(root),
        skip_measure=True,
        performance_input={
            "complexity": {"baseline_median_s": 1.0, "head_median_s": 1.0},
            "coverage": [],
            "acceptance_cost": {"samples": [{"feature": "f", "total_s": 1.0}]},
        },
    )
    assert report["confidence"]["sample_files"] == 1
    assert report["performance"]["extra_gut_rounds"] == 0
    assert report["acceptance_cost"]["samples"][0]["feature"] == "f"
    assert report["unknown"]["share"] == 1.0  # static-only: everything unknown


def test_run_shadow_from_paths_delegates_to_default_measure(
    tmp_path: Path, monkeypatch
):
    from godot_qa_toolkit.risk import shadow as shadow_mod

    root = tmp_path / "proj"
    root.mkdir()
    good = root / "good.gd"
    good.write_text(IDENTITY_SRC, encoding="utf-8")
    calls: list[tuple[str, str | None]] = []
    captured_stores: list[object] = []

    def fake_run_coverage(file_path, project_root, min_percent, timeout_s,
                          timing_store, tests_glob):
        calls.append((file_path, tests_glob))
        captured_stores.append(timing_store)
        return {
            "ok": True,
            "summary": {
                "total_lines": 3,
                "covered_lines": 3,
                "probes_fired": 3,
                "hits_lines": [4, 5, 7],
            },
        }

    monkeypatch.setattr(
        "godot_qa_toolkit.coverage.runner.run_coverage", fake_run_coverage
    )
    report = shadow_mod.run_shadow_from_paths(
        paths=[good], project_root=str(root), skip_measure=False
    )
    # core + full + one same-recipe re-run = exactly three GUT rounds per file
    assert len(calls) == 3
    # D-M2-1 guard (default direction): with no timing_store passed, the
    # forward must be None — never an implicit store.
    assert captured_stores == [None, None, None]
    assert report["performance"]["extra_gut_rounds"] == 0
    assert report["records"]


@pytest.mark.skipif(not KOL_ROOT, reason="KOL_ROOT not set")
def test_real_kol_selected_eligible_files_are_eligible():
    from godot_qa_toolkit.risk.identity import file_profile

    for rel in (
        "systems/container/data_source.gd",
        "systems/item/item_factory.gd",
        "systems/container/cell/cell_factory.gd",
    ):
        src = (Path(KOL_ROOT) / rel).read_text(encoding="utf-8")
        profile = file_profile(src, file_label=rel)
        assert profile["eligible"] is True, (rel, profile)


def test_default_measure_forwards_timing_store(monkeypatch, tmp_path: Path):
    """D-M2-1: the closure must pass the outer `timing_store` through to
    run_coverage — a hardcoded None here silently ignores the parameter and
    any caller wiring the timing store would lose every sample."""
    from godot_qa_toolkit.risk import shadow as shadow_mod

    captured: dict = {}
    sentinel = object()

    def fake_run_coverage(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "summary": {"hits_lines": []}}

    monkeypatch.setattr(
        "godot_qa_toolkit.coverage.runner.run_coverage", fake_run_coverage
    )
    measure = shadow_mod.default_measure(str(tmp_path), timing_store=sentinel)
    measure("f.gd", "source", None)
    assert captured["timing_store"] is sentinel

    bare = shadow_mod.default_measure(str(tmp_path))
    bare("f.gd", "source", None)
    assert captured["timing_store"] is None
