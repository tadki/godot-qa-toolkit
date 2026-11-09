"""CRAP-Line record contract and file state machine (§SPEC-007).

A record is diagnostic-only: it carries its own disclosure fields so no
consumer can mistake statement-start-line coverage for path coverage or test
effectiveness. unknown/vacuous/run-error states never produce a score; the
score is a ranking aid for tidy, never a quality KPI or CI gate.
"""

from __future__ import annotations

from .formula import CRAP_LINE_VERSION, crap_line_score

SCORE_STATUS_VALUES = frozenset(
    {
        "scored",
        "true_zero_coverage",  # reserved: certified lane only, never in M2
        "unknown",
        "vacuous",
        "cc_unmeasured",
        "unsupported",
        "partial_instrumentation",
        "identity_mismatch",
        "run_error",
        "suite_invalid",
        "zero_tests",
        "timeout",
        "stale",
        "provenance_mismatch",
        "unstable",
        "no_instability_observed",
        "excluded",
    }
)

_SCORELESS = SCORE_STATUS_VALUES - {"scored", "no_instability_observed"}


def classify_file_state(
    *,
    executable_probes: int,
    probes_fired: int,
    hits: int,
    gut_ok: bool,
    run_error: bool,
    timeout: bool,
    zero_tests: bool = False,
) -> str:
    if timeout:
        return "timeout"
    if run_error:
        return "run_error"
    if zero_tests:
        return "zero_tests"
    if not gut_ok:
        return "suite_invalid"
    if executable_probes == 0:
        return "vacuous"
    if probes_fired == 0:
        return "run_error"
    if hits == 0:
        # probes fired but nothing was ever hit: measurement gap, not a real 0
        return "unknown"
    return "scored"


def build_shadow_record(
    *,
    function_id: str,
    name: str,
    lex_path: str,
    start_line: int,
    end_line: int,
    cc: int,
    covered: int | None,
    total: int | None,
    cov_source: str,
    state: str = "scored",
    recipe: dict | None = None,
    provenance: dict | None = None,
    blind_spots: list[str] | None = None,
    lambda_gap: int = 0,
) -> dict:
    coverage = None
    if total is not None and total > 0 and covered is not None:
        coverage = covered / total
    score = crap_line_score(cc, coverage) if state == "scored" else None
    status = state
    return {
        "function_id": function_id,
        "name": name,
        "lex_path": lex_path,
        "location": {"start_line": start_line, "end_line": end_line},
        "cc": cc,
        "covered": covered,
        "total": total,
        "coverage": coverage,
        "score_status": status,
        "cov_source": cov_source,
        "recipe": recipe or {},
        "provenance": provenance or {},
        "blind_spots": list(blind_spots or []),
        "lambda_gap": lambda_gap,
        "evidence_level": "exploratory",
        "coverage_semantics": "statement_start_line",
        "path_coverage": "not_measured",
        "assertion_effectiveness": "not_measured",
        "crap_line": {
            "version": CRAP_LINE_VERSION,
            "cc": cc,
            "covered": covered,
            "total": total,
            "coverage": coverage,
            "score": score,
            "score_status": status,
            "coverage_semantics": "statement_start_line",
            "path_coverage": "not_measured",
            "assertion_effectiveness": "not_measured",
            "blind_spots": list(blind_spots or []),
            "evidence_level": "exploratory",
        },
    }
