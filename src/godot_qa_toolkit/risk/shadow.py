"""Minimal exploratory shadow orchestration and report assembly (§SPEC-008/009/010/014).

Diagnostic-only by construction: no CI wiring, no thresholds, no average
score, no dynamic cache. The measurement layer is injected so unit/BDD tests
stay Godot-free while the real run (§SPEC-009) uses the coverage runner on
the real KOL tree. Extra GUT rounds are limited to what §SPEC-009 prescribes
(core, full, one same-recipe re-run) plus one acceptance cost probe.
"""

from __future__ import annotations

import time
from pathlib import Path

from .contract import build_shadow_record, classify_file_state
from .identity import file_profile

PERFORMANCE_FIELDS = ("complexity", "coverage", "risk_static", "extra_gut_rounds")


def build_performance_block(
    *,
    complexity_runs: dict,
    coverage_runs: list[dict],
    risk_static: dict,
    extra_gut_rounds: int,
) -> dict:
    baseline = complexity_runs.get("baseline_median_s") or 0.0
    head = complexity_runs.get("head_median_s") or 0.0
    ratio = (head / baseline) if baseline > 0 else 0.0
    coverage_rows = []
    for row in coverage_runs:
        base = row.get("baseline_median_s") or 0.0
        hd = row.get("head_median_s") or 0.0
        coverage_rows.append(
            {**row, "head_median_ratio": (hd / base) if base > 0 else 0.0}
        )
    return {
        "complexity": {**complexity_runs, "head_median_ratio": ratio},
        "coverage": coverage_rows,
        "risk_static": dict(risk_static),
        "extra_gut_rounds": extra_gut_rounds,
    }


def _function_stats(profile: dict, hits: set[int]) -> list[dict]:
    """Per-function probe totals/hits from the identity manifest."""
    by_function: dict[str, dict] = {}
    for row in profile["probes"]:
        fid = row["function_id"]
        if fid is None:
            continue
        bucket = by_function.setdefault(fid, {"total": 0, "covered": 0})
        bucket["total"] += 1
        if row["line"] in hits:
            bucket["covered"] += 1
    return [
        {"function_id": fid, "total": b["total"], "covered": b["covered"]}
        for fid, b in by_function.items()
    ]


def _file_state(run: dict) -> str:
    summary = run.get("summary", {})
    return classify_file_state(
        executable_probes=int(summary.get("total_lines") or 0),
        probes_fired=int(summary.get("probes_fired") or 0),
        hits=int(summary.get("covered_lines") or 0),
        gut_ok=bool(run.get("ok")),
        run_error=bool(summary.get("run_error")),
        timeout=bool(summary.get("timeout")),
        zero_tests=bool(summary.get("zero_tests")),
    )


def _records_for_file(
    profile: dict,
    hits: set[int],
    *,
    cov_source: str,
    state: str,
) -> list[dict]:
    identities = {f["function_id"]: f for f in profile["functions"]}
    stats = {s["function_id"]: s for s in _function_stats(profile, hits)}
    records = []
    for fid, identity in identities.items():
        stat = stats.get(fid)
        if state != "scored" or stat is None:
            covered = total = None
            record_state = state if state != "scored" else "unknown"
        else:
            covered, total = stat["covered"], stat["total"]
            record_state = "scored"
        records.append(
            build_shadow_record(
                function_id=fid,
                name=identity["name"],
                lex_path=identity["lex_path"],
                start_line=identity["start_line"],
                end_line=identity["end_line"],
                cc=identity["cc"],
                covered=covered,
                total=total,
                cov_source=cov_source,
                state=record_state,
                recipe={"cov_source": cov_source, "semantics": "statement_start_line"},
                blind_spots=[
                    "path coverage not measured",
                    "assertion effectiveness not measured",
                ],
            )
        )
    return records


def _measure_eligible_file(
    label: str,
    source: str,
    measure,
    core_tests: str,
    full_tests: str | None,
) -> dict:
    """Run one eligible file through the §SPEC-009 recipe (core, full, one
    same-recipe re-run) and produce its records plus every report block it
    contributes. The caller owns all report accumulators."""
    static_t0 = time.monotonic()
    profile = file_profile(source, file_label=label)
    static_seconds = time.monotonic() - static_t0

    core = measure(label, source, core_tests)
    full = measure(label, source, full_tests)
    rerun = measure(label, source, full_tests)

    core_hits = set(core["summary"].get("hits_lines", ()))
    full_hits = set(full["summary"].get("hits_lines", ()))
    rerun_hits = set(rerun["summary"].get("hits_lines", ()))

    state = _file_state(full)
    records = _records_for_file(profile, full_hits, cov_source="gut_full", state=state)

    per_block = _function_stats(profile, full_hits)
    core_block = {s["function_id"]: s for s in _function_stats(profile, core_hits)}
    rerun_identical = full_hits == rerun_hits
    return {
        "records": records,
        "unknown": (
            {"file": label, "reason": state, "count": len(records)}
            if state != "scored"
            else None
        ),
        "core_full": {
            "file": label,
            "core_covered_lines": len(core_hits),
            "full_covered_lines": len(full_hits),
            "core_wall_s": core["summary"].get("wall_s"),
            "full_wall_s": full["summary"].get("wall_s"),
            "per_function_core_minus_full": [
                {
                    "function_id": s["function_id"],
                    "core_covered": core_block.get(s["function_id"], {}).get(
                        "covered", 0
                    ),
                    "full_covered": s["covered"],
                }
                for s in per_block
            ],
        },
        "noise": {
            "file": label,
            "rerun_identical": rerun_identical,
            "status": (
                "no_instability_observed" if rerun_identical else "unstable"
            ),
        },
        "file": {
            "file": label,
            "profile": profile,
            "static_seconds": static_seconds,
            "state": state,
        },
    }


def _report_document(
    *,
    ranking: list[dict],
    unknown: list[dict],
    core_full: list[dict],
    noise: list[dict],
    negatives: list[dict],
    per_file: list[dict],
    sample_files: int,
    negative_files: int,
) -> dict:
    """Freeze the §SPEC-010 report document. Key order and every literal here
    are the report JSON contract — consumers (jq gates, QA) pin them."""
    scored = [r for r in ranking if r["score_status"] == "scored"]
    scored.sort(
        key=lambda r: (r["crap_line"]["score"] or 0.0), reverse=True
    )
    total_records = len(ranking)
    unknown_count = sum(1 for r in ranking if r["score_status"] != "scored")
    return {
        "report": "shadow-report",
        "version": "shadow/1",
        "evidence_level": "exploratory",
        "diagnostic_only": True,
        "dynamic_cache": False,
        "metric": "crap_line",
        "metric_version": "crap-line/1",
        "unknown": {
            "count": unknown_count,
            "total": total_records,
            "share": (unknown_count / total_records) if total_records else 0.0,
            "reasons": unknown,
        },
        "ranking": {
            "top": [
                {
                    "function_id": r["function_id"],
                    "score": r["crap_line"]["score"],
                    "score_status": r["score_status"],
                    "cc": r["cc"],
                    "coverage": r["coverage"],
                }
                for r in scored
            ],
            "manual_named": [],
            "note": "2-3 files only; never extrapolate to the whole repo",
        },
        "core_full": core_full,
        "noise": noise,
        "acceptance_cost": {
            "samples": [],
            "extrapolation_assumption": (
                "one feature compile+run sample; linear scaling is assumed, "
                "not proven — full 36-feature cost is out of scope for M2"
            ),
        },
        "routing": {
            "tidy": sum(1 for r in scored if (r["coverage"] or 0.0) >= 0.8),
            "harti_safety_net": sum(
                1 for r in scored if (r["coverage"] or 0.0) < 0.5
            ),
            "tool_chain_gap": len(negatives) + unknown_count,
        },
        "confidence": {
            "sample_files": sample_files,
            "caveat": "2-3 files cannot be extrapolated to the whole repository",
            "negative_files": negative_files,
        },
        "negatives": negatives,
        "files": per_file,
        "records": ranking,
    }


def run_shadow(
    *,
    eligible_sources: dict[str, str],
    negative_sources: dict[str, str],
    measure,
    core_tests: str = "res://tests/unit/",
    full_tests: str | None = None,
) -> dict:
    """Assemble the exploratory shadow report.

    `measure(file_label, source, tests_glob) -> dict` returns the coverage
    runner contract plus a "hits_lines" set under summary (see
    `default_measure`); tests inject deterministic fakes.
    """
    ranking: list[dict] = []
    unknown: list[dict] = []
    core_full: list[dict] = []
    noise: list[dict] = []
    per_file: list[dict] = []

    for label, source in eligible_sources.items():
        outcome = _measure_eligible_file(
            label, source, measure, core_tests, full_tests
        )
        ranking.extend(outcome["records"])
        if outcome["unknown"] is not None:
            unknown.append(outcome["unknown"])
        core_full.append(outcome["core_full"])
        noise.append(outcome["noise"])
        per_file.append(outcome["file"])

    negatives = []
    for label, source in negative_sources.items():
        profile = file_profile(source, file_label=label)
        negatives.append(
            {
                "file": label,
                "gap_lane": profile["gap_lane"],
                "eligible": profile["eligible"],
                "reason": profile["gap_lane"],
            }
        )

    return _report_document(
        ranking=ranking,
        unknown=unknown,
        core_full=core_full,
        noise=noise,
        negatives=negatives,
        per_file=per_file,
        sample_files=len(eligible_sources),
        negative_files=len(negative_sources),
    )


def partition_sources(
    paths: list[Path], project_root: str | None = None
) -> tuple[dict[str, str], dict[str, str]]:
    """Split candidate files by static eligibility (§SPEC-008): eligible files
    are measured with all functions participating; the lambda / inner-class /
    var-def negatives route to their measurement-gap lanes and never consume
    a GUT round. Labels stay project-root-relative so the measurement adapter
    can resolve the real file path."""
    eligible: dict[str, str] = {}
    negatives: dict[str, str] = {}
    read_errors: list[str] = []
    root = Path(project_root).resolve() if project_root else None
    for path in paths:
        from ..source_io import read_gd_source

        source, read_error = read_gd_source(path)
        if read_error is not None:
            # §SPEC-028: unreadable files route to the read_error gap lane —
            # the scan continues for the remaining files, never crashes
            label = (
                str(path.resolve().relative_to(Path(project_root).resolve()))
                if project_root and path.resolve().is_relative_to(
                    Path(project_root).resolve())
                else path.name
            )
            negatives[label] = ""
            read_errors.append(label)
            continue
        if root is not None:
            try:
                label = str(path.resolve().relative_to(root))
            except ValueError:
                label = path.name
        else:
            label = path.name
        profile = file_profile(source, file_label=label)
        if profile["eligible"]:
            eligible[label] = source
        else:
            negatives[label] = source
    for label in read_errors:
        negatives.setdefault(label, "")
    return eligible, negatives


def _risk_static_block(report: dict) -> dict:
    """§SPEC-014 static-layer budget: per-file static seconds vs the measured
    (core+full) wall clock, both read from the assembled report's own rows."""
    static_max = max(
        (float(f.get("static_seconds", 0.0)) for f in report["files"]), default=0.0
    )
    measured_wall = sum(
        float(entry.get("full_wall_s") or 0.0) for entry in report["core_full"]
    ) + sum(
        float(entry.get("core_wall_s") or 0.0) for entry in report["core_full"]
    )
    return {
        "max_seconds_per_file": round(static_max, 4),
        "max_share_of_total": (
            round(static_max / (static_max + measured_wall), 4)
            if (static_max + measured_wall) > 0
            else 0.0
        ),
    }


def run_shadow_from_paths(
    *,
    paths: list[Path],
    project_root: str,
    core_tests: str = "res://tests/unit/",
    full_tests: str | None = None,
    measure=None,
    skip_measure: bool = False,
    performance_input: dict | None = None,
) -> dict:
    """CLI entry: partition paths, measure eligible files, assemble report."""
    eligible, negatives = partition_sources(paths, project_root)
    measure_fn = measure or default_measure(project_root)

    def _skip(_label: str, _source: str, _tests: str | None) -> dict:
        return {
            "ok": False,
            "summary": {
                "run_error": True,
                "error": "measurement skipped (static-only dry run)",
            },
        }

    report = run_shadow(
        eligible_sources=eligible,
        negative_sources=negatives,
        measure=_skip if skip_measure else measure_fn,
        core_tests=core_tests,
        full_tests=full_tests,
    )
    perf_in = performance_input or {}
    report["performance"] = build_performance_block(
        complexity_runs=perf_in.get(
            "complexity", {"baseline_median_s": 0.0, "head_median_s": 0.0}
        ),
        coverage_runs=perf_in.get("coverage", []),
        risk_static=_risk_static_block(report),
        extra_gut_rounds=0,
    )
    if perf_in.get("acceptance_cost"):
        report["acceptance_cost"].update(perf_in["acceptance_cost"])
    return report


def default_measure(project_root: str, timing_store=None):
    """Real measurement adapter: one coverage run per (file, tests_glob)."""

    def measure(label: str, source: str, tests_glob: str | None) -> dict:
        from ..coverage.runner import run_coverage

        start = time.monotonic()
        result = run_coverage(
            file_path=str(Path(project_root) / label),
            project_root=project_root,
            min_percent=0.0,
            timeout_s=300,
            timing_store=timing_store,
            tests_glob=tests_glob,
        )
        wall = time.monotonic() - start
        summary = dict(result.get("summary", {}))
        summary["wall_s"] = round(wall, 3)
        return {"ok": result.get("ok"), "summary": summary}

    return measure
