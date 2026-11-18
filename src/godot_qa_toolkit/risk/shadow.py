"""Minimal exploratory shadow orchestration and report assembly (§SPEC-008/009/010/014).

Diagnostic-only by construction: no CI wiring, no thresholds, no average
score, no dynamic cache. The measurement layer is injected so unit/BDD tests
stay Godot-free while the real run (§SPEC-009) uses the coverage runner on
the real KOL tree. Extra GUT rounds are limited to what §SPEC-009 prescribes
(core, full, one same-recipe re-run) plus one acceptance cost probe.
"""

from __future__ import annotations
from ..digest import sha256_text

import json
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
    per_entry: dict[str, dict] | None = None,
) -> list[dict]:
    """PR-C (§SPEC-027): when `per_entry` (fid → covered/total/state from the
    evidence bundle) is given, records consume the bundle's per-entry T/I/C
    directly; the probe-line aggregation stays as the fallback lane."""
    identities = {f["function_id"]: f for f in profile["functions"]}
    stats = {s["function_id"]: s for s in _function_stats(profile, hits)}
    records = []
    for fid, identity in identities.items():
        entry = (per_entry or {}).get(fid)
        stat = stats.get(fid)
        if entry is not None:
            covered, total = entry["covered"], entry["total"]
            record_state = entry["state"]
        elif state != "scored" or stat is None:
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
                lambda_gap=identity.get("lambda_gap", 0),
                recipe={"cov_source": cov_source, "semantics": "statement_start_line"},
                blind_spots=[
                    "path coverage not measured",
                    "assertion effectiveness not measured",
                ],
            )
        )
    return records


def _per_entry_state(c_row: dict, i_row: dict, lambda_gap: int,
                     stale: bool) -> dict:
    """§8-4 车道链：stale → lambda_gap(cc_unmeasured) → total>0 scored →
    else unknown（无分母不臆造分数）。"""
    if stale:
        return {"covered": None, "total": None, "state": "stale"}
    covered = c_row.get("covered", 0)
    total = i_row.get("i_lines", 0)
    if lambda_gap > 0:
        # §8-4: 带分支 lambda 宿主的出分车道维持 cc_unmeasured（WP1c 前）
        return {"covered": None, "total": None, "state": "cc_unmeasured"}
    if total > 0:
        return {"covered": covered, "total": total, "state": "scored"}
    return {"covered": None, "total": None, "state": "unknown"}


def _derive_per_entry(
    bundle: dict,
    profile_functions: list[dict],
    source: str,
    registry: dict | None = None,
    probe_manifest: dict | None = None,
) -> tuple[dict[str, dict], bool]:
    """§SPEC-027 per-entry score_status derivation (§8-4 rules unchanged):
    stale = bundle target sha ≠ current source sha; cc_unmeasured = host
    lambda_gap > 0 (WP1c 前出分车道不变). Returns (per_entry, stale).

    `registry` is only the fallback function list when `profile_functions` is
    empty; the derivation reads only the bundle's C/I rows and the host
    lambda_gap — no manifest walk is needed here."""
    from ..registry.functions import build_file_registry

    current_sha = sha256_text(source)
    stale = bundle.get("target", {}).get("sha256") != current_sha
    if registry is None:
        registry = build_file_registry(source, file_label="")

    c_by_fid = {c["fid"]: c for c in bundle.get("C", [])}
    i_by_fid = {r["fid"]: r for r in bundle.get("I", [])}
    per_entry: dict[str, dict] = {}
    if not profile_functions:
        profile_functions = [
            {"function_id": e["fid"], "lambda_gap": e.get("lambda_gap", 0)}
            for e in registry.get("entries", []) if e.get("kind") == "func_def"
        ]
    for fn in profile_functions:
        fid = fn["function_id"]
        lambda_gap = fn.get("lambda_gap", 0)
        per_entry[fid] = _per_entry_state(
            c_by_fid.get(fid, {}), i_by_fid.get(fid, {}), lambda_gap, stale)
    return per_entry, stale

def _measure_measurements(label: str, source: str, measure,
                          core_tests: str, full_tests: "str | None") -> tuple:
    """§SPEC-009 配方三连测：core / full / 同配方复跑。返回 (profile,
    core, full, rerun)。"""
    profile = file_profile(source, file_label=label)
    core = measure(label, source, core_tests)
    full = measure(label, source, full_tests)
    rerun = measure(label, source, full_tests)
    return profile, core, full, rerun


def _measure_records_lane(profile: dict, full, state: str,
                          source: str) -> list[dict]:
    """记录车道（§SPEC-027 PR-C）：bundle 在场消费 per-entry T/I/C 并派生
    score_status（stale/cc_unmeasured，见 _derive_per_entry），否则
    hits_lines 聚合回退。"""
    full_hits = set(full["summary"].get("hits_lines", ()))
    bundle = full.get("bundle")
    if bundle is not None:
        per_entry, _ = _derive_per_entry(
            bundle, profile["functions"], source
        )
        return _records_for_file(
            profile, full_hits, cov_source="gut_full", state=state,
            per_entry=per_entry,
        )
    return _records_for_file(
        profile, full_hits, cov_source="gut_full", state=state
    )


def _measure_core_full_block(profile: dict, label: str, core, full) -> dict:
    """core_full 报告块（覆盖行数/墙钟/逐函数 core−full 差）。"""
    core_hits = set(core["summary"].get("hits_lines", ()))
    full_hits = set(full["summary"].get("hits_lines", ()))
    per_block = _function_stats(profile, full_hits)
    core_block = {s["function_id"]: s
                  for s in _function_stats(profile, core_hits)}
    return {
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
    }


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
    profile, core, full, rerun = _measure_measurements(
        label, source, measure, core_tests, full_tests)
    static_seconds = time.monotonic() - static_t0

    full_hits = set(full["summary"].get("hits_lines", ()))
    rerun_hits = set(rerun["summary"].get("hits_lines", ()))
    state = _file_state(full)
    records = _measure_records_lane(profile, full, state, source)
    rerun_identical = full_hits == rerun_hits
    return {
        "records": records,
        "unknown": (
            {"file": label, "reason": state, "count": len(records)}
            if state != "scored"
            else None
        ),
        "core_full": _measure_core_full_block(profile, label, core, full),
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

def _report_ranking_blocks(ranking: list[dict]) -> tuple:
    """排序切片 + unknown 统计（score 降序为报告契约）。"""
    scored = [r for r in ranking if r["score_status"] == "scored"]
    scored.sort(key=lambda r: (r["crap_line"]["score"] or 0.0), reverse=True)
    total_records = len(ranking)
    unknown_count = sum(1 for r in ranking if r["score_status"] != "scored")
    return scored, total_records, unknown_count


def _report_ranking_block(scored: list[dict]) -> dict:
    """ranking 报告块（top 投影 + 手动命名占位 + 外推警告）。"""
    return {
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
    scored, total_records, unknown_count = _report_ranking_blocks(ranking)
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
        "ranking": _report_ranking_block(scored),
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

def _shadow_collect(eligible_sources: dict[str, str], measure,
                    core_tests: str,
                    full_tests: "str | None") -> tuple:
    """逐 eligible 文件跑 §SPEC-009 配方并聚合各报告块。"""
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
    return ranking, unknown, core_full, noise, per_file


def _shadow_negatives(negative_sources: dict[str, str]) -> list[dict]:
    """负例 → 测量缺口车道行（gap_lane/eligible/reason 披露）。"""
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
    return negatives


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
    `default_measure`); tests inject deterministic fakes. """
    ranking, unknown, core_full, noise, per_file = _shadow_collect(
        eligible_sources, measure, core_tests, full_tests)
    negatives = _shadow_negatives(negative_sources)
    return _report_document(
        ranking=ranking, unknown=unknown, core_full=core_full, noise=noise,
        negatives=negatives, per_file=per_file,
        sample_files=len(eligible_sources),
        negative_files=len(negative_sources))
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
            "complexity", {"baseline_median_s": 0.0, "head_median_s": 0.0}),
        coverage_runs=perf_in.get("coverage", []),
        risk_static=_risk_static_block(report),
        extra_gut_rounds=0,
    )
    if perf_in.get("acceptance_cost"):
        report["acceptance_cost"].update(perf_in["acceptance_cost"])
    return report


def _measure_once(label: str, source: str, tests_glob: "str | None",
                  project_root: str, evidence_dir, evidence_out) -> dict:
    """单次 coverage run + wall_s 入账 + bundle 快照差定位（fail-loud：
    evidence 消失/多义绝不返回无证据 measure 结果，D-WP2C-1 裁决）。"""
    from ..coverage.runner import run_coverage

    # §SPEC-030 / D-WP2C-1: snapshot the evidence dir's bundle set before
    # the run — summary carries no run_uuid (§SPEC-026 stdout freeze), so
    # the adapter locates this run's bundle by snapshot diff instead.
    bundles_before = _evidence_bundle_names(evidence_dir)
    start = time.monotonic()
    result = run_coverage(
        file_path=str(Path(project_root) / label),
        project_root=project_root,
        min_percent=0.0,
        tests_glob=tests_glob,
        evidence_out=evidence_out,
    )
    wall = time.monotonic() - start
    summary = dict(result.get("summary", {}))
    summary["wall_s"] = round(wall, 3)
    out = {"ok": result.get("ok"), "summary": summary}
    if evidence_dir is not None:
        new_bundles = sorted(
            _evidence_bundle_names(evidence_dir) - bundles_before
        )
        if len(new_bundles) == 1:
            bundle_path = evidence_dir / new_bundles[0]
            out["bundle"] = json.loads(
                bundle_path.read_text(encoding="utf-8")
            )
        else:
            # fail-loud (D-WP2C-1 ruling): silent bundle loss is exactly
            # the defect class this fix eliminates — never return a
            # measure result whose evidence vanished or is ambiguous
            raise RuntimeError(
                f"evidence bundle for {label!r}: expected exactly 1 new "
                f"bundle in {evidence_dir}, found {len(new_bundles)} "
                f"(new: {sorted(new_bundles)}; run ok={out['ok']}, "
                f"run_error={summary.get('run_error')})"
            )
    return out


def default_measure(project_root: str, evidence_out=None):
    """Real measurement adapter: one coverage run per (file, tests_glob).

    PR-C (§SPEC-027): with `evidence_out`, each run produces a
    coverage-evidence/2 bundle and the adapter returns its per-entry T/I/C in
    the `bundle` key — the shadow consumes bundles instead of hits_lines.
    Bundles are reused as explicit outputs; no extra GUT rounds are added
    (§SPEC-027 extra_gut_rounds==0)."""
    evidence_dir = Path(evidence_out) if evidence_out else None
    if evidence_dir is not None:
        evidence_dir.mkdir(parents=True, exist_ok=True)

    def measure(label: str, source: str, tests_glob: str | None) -> dict:
        return _measure_once(label, source, tests_glob, project_root,
                             evidence_dir,
                             str(evidence_dir) if evidence_dir else None)

    return measure

def _evidence_bundle_names(evidence_dir: Path | None) -> set[str]:
    """Names of bundle files (coverage-evidence/2) in the evidence dir —
    `*.record.json` run records are lifecycle state, not bundles."""
    if evidence_dir is None or not evidence_dir.is_dir():
        return set()
    return {
        p.name for p in evidence_dir.glob("*.json")
        if not p.name.endswith(".record.json")
    }
