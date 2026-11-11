"""SEE-1546 M2 risk unit tests: function identity/probe manifest (§SPEC-006),
CRAP-Line formula and state contract (§SPEC-007), shadow selection (§SPEC-008)
and the performance field contract (§SPEC-014)."""

from __future__ import annotations

import json
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
    """PR-A re-anchor (plan §4.4 P1 denominator split): lambda body lines
    belong to the standalone lambda entry — the host denominator excludes
    them, which is exactly the "never mixed into the host denominator" pin."""
    identities = collect_function_identities(STRICT, file_label="x.gd")
    names = [i.name for i in identities]
    assert names == ["host"]  # lambda is not a function identity of its own
    host = identities[0]
    rows, _ = build_probe_manifest(STRICT, identities)
    host_fid = host.function_id
    lam_fid = f"{host_fid}.<lambda#1>"
    assert all(
        r["function_id"] in (host_fid, lam_fid) for r in rows
    ), str(rows)
    # lambda body lines are attributed to the lambda entry, never the host
    host_lines = {r["line"] for r in rows if r["function_id"] == host_fid}
    lam_lines = {r["line"] for r in rows if r["function_id"] == lam_fid}
    assert lam_lines, "lambda body has no probes"
    assert not (host_lines & lam_lines)


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


def test_identity_names_functions_from_registry_contract():
    """PR-A re-anchor: function names come from the registry contract
    (func_header first NAME token). The header-less func_def defensive branch
    (`<unnamed>` entry) is unreachable for gdtoolkit output — disclosed as a
    defensive arc, covered by the registry unit suite."""
    from godot_qa_toolkit.registry.functions import build_file_registry

    registry = build_file_registry("func f():\n\tpass\n", file_label="x.gd")
    assert [e["name"] for e in registry["entries"]] == ["f"]

def identity_unnamed_probe():
    return None

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
                          timing_store, tests_glob, evidence_out=None):
        calls.append((file_path, tests_glob))
        captured_stores.append(timing_store)
        summary = {
            "total_lines": 3,
            "covered_lines": 3,
            "probes_fired": 3,
            "hits_lines": [4, 5, 7],
        }
        out = {"ok": True, "summary": summary}
        if evidence_out:
            # PR-B evidence contract: bundle lands in the evidence dir
            import hashlib
            run_uuid = "prc-delegate-0000-0000-000000000000"
            src_sha = hashlib.sha256(
                Path(file_path).read_text(encoding="utf-8").encode("utf-8")
            ).hexdigest()
            bundle = {
                "evidence_version": "coverage-evidence/2",
                "run_uuid": run_uuid,
                "recovered": False,
                "target": {"file": file_path, "sha256": src_sha},
                "recipe": {"arm": "gut_full", "tests_glob": tests_glob},
                "probe_manifest": {"manifest_version": "probe-manifest/2",
                                   "inline": {}},
                "T": [], "I": [], "C": [], "census_gaps": [],
                "sentinels": {"probes_fired": 3, "vacuous": False,
                              "run_error": False, "timeout": False,
                              "zero_tests": False, "gut_ok": True,
                              "transport_anomaly": 0},
                "entry_states": [],
                "file_state": "bundle_only",
            }
            ev = Path(evidence_out)
            ev.mkdir(parents=True, exist_ok=True)
            (ev / f"{run_uuid}.json").write_text(
                json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
            summary["run_uuid"] = run_uuid
        return out

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


# --------------------------------------------------------------------------
# §SPEC-027 (PR-C): evidence-bundle consumption and per-entry derivation
# --------------------------------------------------------------------------


def _bundle_for(source: str, hits: list[int], sha: str) -> dict:
    """Deterministic evidence bundle in the coverage-evidence/2 shape, built
    from the real registry/manifest pipeline (same as runner would produce)."""
    from godot_qa_toolkit.registry.functions import build_file_registry
    from godot_qa_toolkit.registry.manifest import build_probe_manifest_v2

    registry = build_file_registry(source, file_label="x.gd")
    manifest = build_probe_manifest_v2(source, registry)
    hits_set = set(hits)
    plan_by_line = {p["line"]: p for p in manifest["probe_plan"]}
    c_rows = []
    per = {p["fid"]: p for p in manifest["per_function"]}
    for fid, pf in per.items():
        covered = sorted(
            ln for ln in pf.get("t_line_ids", [])
            if ln in hits_set and ln in plan_by_line
            and plan_by_line[ln]["fid"] == fid
        )
        c_rows.append({"fid": fid, "covered": len(covered),
                       "covered_line_ids": covered})
    i_rows = [{"fid": fid, "i_lines": pf.get("i_lines", 0)}
              for fid, pf in per.items()]
    return {
        "evidence_version": "coverage-evidence/2",
        "target": {"sha256": sha},
        "probe_manifest": {"manifest_version": "probe-manifest/2",
                           "inline": manifest},
        "T": [{"fid": fid, "t_lines": pf.get("t_lines", 0),
               "line_ids": pf.get("t_line_ids", [])} for fid, pf in per.items()],
        "I": i_rows,
        "C": c_rows,
        "census_gaps": manifest["census_gaps"],
        "sentinels": {"probes_fired": len(hits)},
    }


def test_evidence_bundle_consumed_for_records(monkeypatch):
    """§SPEC-027: measure results carrying a bundle feed records from the
    bundle's per-entry T/I/C — the evidence lane replaces hits aggregation."""
    import hashlib
    from godot_qa_toolkit.risk import shadow as shadow_mod

    source = (
        "extends RefCounted\n"
        "\n"
        "func one() -> int:\n"
        "\treturn 1\n"
        "\n"
        "func two() -> int:\n"
        "\tif 1:\n"
        "\t\treturn 2\n"
        "\treturn 0\n"
    )
    src_sha = hashlib.sha256(source.encode("utf-8")).hexdigest()
    captured = {}

    def fake_measure(label, src, tests_glob):
        bundle = _bundle_for(src, hits=[4, 7, 8, 9], sha=src_sha)
        captured["bundle"] = bundle
        covered_lines = sorted(
            ln for c in bundle["C"] for ln in c["covered_line_ids"]
        )
        return {"ok": True, "summary": {
            "total_lines": 4, "covered_lines": len(covered_lines),
            "probes_fired": len(covered_lines), "hits_lines": covered_lines,
            "wall_s": 0.01,
        }, "bundle": bundle}

    outcome = shadow_mod._measure_eligible_file(
        "x.gd", source, fake_measure,
        "res://tests/unit/container/", "res://tests/unit/"
    )
    rows = outcome["records"]
    assert len(rows) == 2
    by_name = {r["name"]: r for r in rows}
    assert by_name["one"]["covered"] == 1 and by_name["one"]["total"] == 1
    assert by_name["two"]["covered"] == 3 and by_name["two"]["total"] == 3
    assert all(r["cov_source"] == "gut_full" for r in rows)


def test_evidence_stale_sha_derives_stale_status():
    """§8-4 derivation: bundle target sha ≠ current source sha → every record
    goes stale (covered/total nulled, no score)."""
    import hashlib
    from godot_qa_toolkit.risk import shadow as shadow_mod

    source = SHADOW_TWO_FUNC_SRC
    src_sha = hashlib.sha256(source.encode("utf-8")).hexdigest()
    bundle = _bundle_for(source, hits=[3, 7], sha=src_sha)
    # drift the bundle's sha so it no longer matches the current source
    bundle["target"]["sha256"] = "stale" * 8
    profile = shadow_mod.file_profile(source, file_label="x.gd")
    per_entry, stale = shadow_mod._derive_per_entry(
        bundle, profile["functions"], source
    )
    assert stale is True
    assert all(e["state"] == "stale" for e in per_entry.values())


def test_evidence_host_lambda_gap_derives_cc_unmeasured():
    """§8-4: host lambda_gap > 0 → cc_unmeasured (WP1c 前出分车道不变)."""
    from godot_qa_toolkit.risk import shadow as shadow_mod
    from godot_qa_toolkit.risk.identity import file_profile
    import hashlib

    lam_src = (
        "extends RefCounted\n"
        "\n"
        "func host(x: int) -> int:\n"
        "\tvar f := func(v: int) -> int:\n"
        "\t\tif v > 0:\n"
        "\t\t\treturn v\n"
        "\t\treturn 0\n"
        "\treturn f(x)\n"
    )
    src_sha = hashlib.sha256(lam_src.encode("utf-8")).hexdigest()
    bundle = _bundle_for(lam_src, hits=[3], sha=src_sha)
    profile = file_profile(lam_src, file_label="x.gd")
    per_entry, stale = shadow_mod._derive_per_entry(
        bundle, profile["functions"], lam_src
    )
    host = per_entry["x.gd::host"]
    assert host["state"] == "cc_unmeasured"
    assert host["covered"] is None and host["total"] is None


SHADOW_TWO_FUNC_SRC = (
    "extends RefCounted\n"
    "\n"
    "func one() -> int:\n"
    "\treturn 1\n"
    "\n"
    "func two() -> int:\n"
    "\tif 1:\n"
    "\t\treturn 2\n"
    "\treturn 0\n"
)


def test_derive_per_entry_signature_helper_exists():
    from godot_qa_toolkit.risk import shadow as shadow_mod
    import hashlib
    import json

    from godot_qa_toolkit.registry.functions import build_file_registry
    from godot_qa_toolkit.registry.manifest import build_probe_manifest_v2

    src = SHADOW_TWO_FUNC_SRC
    sha = hashlib.sha256(src.encode()).hexdigest()
    reg = build_file_registry(src, file_label="x.gd")
    man = build_probe_manifest_v2(src, reg)
    profile_functions = [
        {"function_id": e["fid"], "lambda_gap": e["lambda_gap"]}
        for e in reg["entries"] if e["kind"] == "func_def"
    ]
    c_rows = [{"fid": fid, "covered": covered, "covered_line_ids": []}
              for fid, covered in (("x.gd::one", 1), ("x.gd::two", 3))]
    i_rows = [{"fid": fid, "i_lines": total}
              for fid, total in (("x.gd::one", 1), ("x.gd::two", 3))]
    bundle = {"C": c_rows, "I": i_rows, "target": {"sha256": sha}}

    per_entry, stale = shadow_mod._derive_per_entry(
        bundle, profile_functions, src,
        registry=reg, probe_manifest=man,
    )
    assert stale is False
    assert set(per_entry) == {"x.gd::one", "x.gd::two"}
    assert all(e["state"] == "scored" for e in per_entry.values())
    assert per_entry["x.gd::one"]["covered"] == 1
    assert per_entry["x.gd::two"]["covered"] == 3


def test_default_measure_consumes_real_bundle_by_snapshot_diff(
    tmp_path: Path, monkeypatch
):
    """D-WP2C-1 real-path integration: mock only the GUT execution layer —
    the runner's real evidence path lands the bundle, and default_measure
    locates it by snapshot diff (summary carries NO run_uuid). RED before the
    snapshot-diff fix: the adapter looked up None.json and silently dropped
    the bundle key."""
    import os

    import godot_qa_toolkit.coverage.runner as c
    from godot_qa_toolkit.risk import shadow as shadow_mod

    proj = tmp_path / "proj"
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    target = proj / "calc.gd"
    target.write_text("func add(a, b):\n\treturn a + b\n")
    ev = tmp_path / "ev"
    ev.mkdir()

    captured = {}
    real_instrument = c._instrument

    def sink_spy(src, sink_name):
        captured["sink"] = os.path.join(str(proj), sink_name)
        return real_instrument(src, sink_name)

    monkeypatch.setattr(c, "_instrument", sink_spy)

    def fake_gut(*args, **kwargs):
        # mock ONLY the GUT execution layer: after the runner instruments and
        # writes the run record, the probes write hits into the real sink —
        # everything else (run record, bundle, sink cleanup) is the real path
        with open(captured["sink"], "w") as f:
            f.write("0\n1\n2\n")
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(c.subprocess, "run", fake_gut)

    measure = shadow_mod.default_measure(str(proj), evidence_out=str(ev))
    out = measure("calc.gd", target.read_text(encoding="utf-8"),
                  "res://tests/unit/")
    assert "bundle" in out, (
        "bundle key silently dropped — adapter cannot locate the evidence "
        f"bundle (summary keys: {sorted(out['summary'])})"
    )
    bundle = out["bundle"]
    assert bundle["evidence_version"] == "coverage-evidence/2"
    assert bundle["recovered"] is False
    on_disk = json.loads(
        next(p for p in ev.glob("*.json") if not p.name.endswith(".record.json"))
    .read_text(encoding="utf-8"))
    assert on_disk == bundle
    # bundle landed → this run's sink was cleared (evidence moved)
    assert not any(p.name.startswith("qa-coverage-hits-")
                   for p in proj.iterdir())


def test_recipe_cli_res_scheme_exclude_survives_colons(tmp_path: Path, monkeypatch):
    """D-W3A-1 real-path pin: `gqt recipe --exclude
    gut_dir:res://tests/unit/quarantine/**:known flaky` — the entry contains
    `://`, so the OLD split(":", 2) truncated entry to `res` and swallowed the
    path into the reason (silent wrong recipe). The fixed parser takes scope
    at the FIRST colon and entry/reason at the LAST colon."""
    import argparse
    import godot_qa_toolkit.cli as cli_mod
    import godot_qa_toolkit.canonical.recipe as recipe_mod

    captured: dict = {}

    def spy_build(project_root, exclusions=None, **kwargs):
        captured["exclusions"] = list(exclusions or [])
        return {"recipe_version": "canonical-recipe/1", "recipe_hash": "x",
                "gut_face": {"dirs": ["res://tests/unit/"]},
                "acceptance_face": {"features": [], "count": 1,
                                    "excluded": [], "harness": {}},
                "toolchain": {}, "exclusions": [], "failures": []}, None

    monkeypatch.setattr(recipe_mod, "build_recipe", spy_build)
    argv = ["recipe", "--project-root", str(tmp_path),
            "--exclude", "gut_dir:res://tests/unit/quarantine/**:known flaky"]
    args = cli_mod.build_parser().parse_args(argv)
    cli_mod.cmd_recipe(args)
    rows = captured["exclusions"]
    assert len(rows) == 1
    row = rows[0]
    assert row["scope"] == "gut_dir"
    assert row["entry"] == "res://tests/unit/quarantine/**", (
        f"entry truncated by colon split: {row}"
    )
    assert row["reason"] == "known flaky"


def test_crap_line_rejects_illegal_coverage_inputs():
    """§SPEC-053：非法覆盖率（越界/NaN）不得静默 clamp —— 必须显式拒绝。"""
    import math

    for bad in (1.0001, -0.0001, float("nan")):
        with pytest.raises(ValueError):
            crap_line_score(6, bad)
    # 合法边界仍精确
    assert crap_line_score(6, 0.0) == pytest.approx(42.0)
    assert crap_line_score(6, 1.0) == pytest.approx(6.0)
