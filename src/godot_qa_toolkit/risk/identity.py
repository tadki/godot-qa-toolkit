"""Function identity, probe→function mapping and instrumentation census (§SPEC-006),
PR-A re-anchor (§SPEC-024): this module is now a *registry consumer*.

Identity collection delegates to `registry.functions.build_file_registry`
(fid = path + class chain + name + occurrence; leaf-token spans; standalone
lambda entries) and line ownership delegates to
`registry.manifest.build_probe_manifest_v2` (build-time fid materialization —
no downstream line-range inference). The private coverage.runner imports of
M2 are gone (§SPEC-024 debt gate): the instrumentation line set and per-entry
T census arrive through probe-manifest/2, whose collector constants remain the
single source of truth inside registry.manifest.

Eligibility stays in the risk domain (a measurement policy, not a neutral
fact): gap lanes and the eligible verdict are aggregated here from the
registry/manifest facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gdtoolkit.gd2py import convert_code

from ..complexity.sanitizer import sanitize_python_ir_identifiers
from ..registry.functions import build_file_registry
from ..registry.manifest import build_probe_manifest_v2


@dataclass(frozen=True)
class FunctionIdentity:
    function_id: str
    name: str
    lex_path: str
    start_line: int
    end_line: int
    cc: int
    kind: str
    body_hash: str
    t_lines: tuple[int, ...] = field(default=())


def collect_function_identities(gd_source: str, file_label: str) -> list[FunctionIdentity]:
    """func_def identities from the registry (lambda entries are exposed
    separately via file_profile; §SPEC-024 incremental disclosure). Per-entry
    T line ids are backfilled from probe-manifest/2's per-function census."""
    registry = build_file_registry(gd_source, file_label)
    manifest = build_probe_manifest_v2(gd_source, registry)
    per_function = {p["fid"]: p for p in manifest["per_function"]}
    identities: list[FunctionIdentity] = []
    for entry in registry["entries"]:
        if entry["kind"] != "func_def":
            continue
        pf = per_function.get(entry["fid"], {})
        identities.append(
            FunctionIdentity(
                function_id=entry["fid"],
                name=entry["name"],
                lex_path=".".join(entry["lex_scope"] + [entry["name"]]),
                start_line=entry["start_line"],
                end_line=entry["end_line"],
                cc=entry["cc_oracle"],
                kind=entry["kind"],
                body_hash=entry["body_hash"],
                t_lines=tuple(pf.get("t_line_ids", [])),
            )
        )
    return identities


def t_census(identities: list[FunctionIdentity]) -> set[int]:
    lines: set[int] = set()
    for identity in identities:
        lines.update(identity.t_lines)
    return lines


def _probe_rows(manifest: dict) -> list[dict]:
    """probe_plan rows in the M2 assertion shape: `function_id` aliases the
    materialized fid; `owner_ambiguous` reads the build-time ownership flag
    (unowned lines keep M2 semantics — fid None, ambiguous False)."""
    return [
        {
            "probe_id": probe["probe_id"],
            "line": probe["line"],
            "function_id": probe["fid"],
            "owner_ambiguous": probe.get("ownership") == "ambiguous",
            "owner_kind": probe["owner_kind"],
        }
        for probe in manifest["probe_plan"]
    ]


def build_probe_manifest(
    gd_source: str, identities: list[dict] | None = None,
    file_label: str | None = None,
) -> tuple[list[dict], list[int]]:
    """Adapter over probe-manifest/2 returning the M2-shaped (rows, manifest)
    pair so §SPEC-006 consumers keep their assertion shape.

    `rows` add `function_id` (from the materialized fid) and
    `owner_ambiguous` (probe-manifest/2 ownership == ambiguous) per line;
    unowned lines keep M2 semantics (fid None, ambiguous False).
    """
    label = file_label
    if not label and identities:
        first = identities[0]
        fid = getattr(first, "function_id", "") or first.get("function_id", "")
        label = str(fid).split("::", 1)[0] or "unknown.gd"
    registry = build_file_registry(gd_source, file_label=label)
    manifest = build_probe_manifest_v2(gd_source, registry)
    lines = [p["line"] for p in manifest["probe_plan"]]
    return _probe_rows(manifest), lines


def _uses_sanitized_projection(gd_source: str) -> bool:
    try:
        python_code = convert_code(gd_source)
    except Exception:
        return False
    return sanitize_python_ir_identifiers(python_code) != python_code


GAP_LANE_LAMBDA = "lambda_decision_gap"
GAP_LANE_INNER = "partial_instrumentation"
GAP_LANE_VAR_DEF = "sanitized_ir_projection"


def file_profile(gd_source: str, file_label: str) -> dict:
    """Aggregate registry + probe-manifest/2 facts into the risk eligibility
    profile (§SPEC-024). File-level `t_equal_i`/`owners_complete` degrade to
    per-entry aggregation; lambda and inner-class registry entries surface in
    the profile as incremental disclosure (§SPEC-024 disclosure gate)."""
    registry = build_file_registry(gd_source, file_label)
    manifest = build_probe_manifest_v2(gd_source, registry)
    census = manifest["census"]
    per_function = manifest["per_function"]

    rows = _probe_rows(manifest)

    func_entries = [e for e in registry["entries"] if e["kind"] == "func_def"]
    lambda_entries = [e for e in registry["entries"] if e["kind"] == "lambda"]

    functions = [
        {
            "function_id": e["fid"],
            "name": e["name"],
            "lex_path": ".".join(e["lex_scope"] + [e["name"]]),
            "start_line": e["start_line"],
            "end_line": e["end_line"],
            "cc": e["cc_oracle"],
            "kind": e["kind"],
            "body_hash": e["body_hash"],
            # §SPEC-027 derivation input: host lambda_gap > 0 → cc_unmeasured
            "lambda_gap": e["lambda_gap"],
        }
        for e in func_entries
    ]

    # per-entry aggregation (§SPEC-024: file-level facts degrade to entries);
    # inner-class rows keep t_equal_i=False, so a partial-instrumentation
    # file stays ineligible by construction. Ambiguous ownership is an
    # ownership defect (owners_complete), not a T/I mismatch.
    t_total = sum(p["t_lines"] for p in per_function)
    i_total = sum(p["i_lines"] for p in per_function)
    t_equal_i = all(p["t_equal_i"] for p in per_function) and not census.get(
        "unowned"
    )
    owners_complete = census["owners_complete"]

    has_lambda = bool(lambda_entries)
    has_inner_class = bool(registry.get("inner_classes"))

    gap_lane = None
    if has_lambda:
        gap_lane = GAP_LANE_LAMBDA
    elif has_inner_class:
        gap_lane = GAP_LANE_INNER
    elif _uses_sanitized_projection(gd_source):
        gap_lane = GAP_LANE_VAR_DEF

    eligible = (
        gap_lane is None
        and bool(func_entries)
        and t_equal_i
        and owners_complete
    )
    return {
        "file": file_label,
        "functions": functions,
        "lambda_entries": lambda_entries,
        "probes": rows,
        "manifest": [p["line"] for p in manifest["probe_plan"]],
        "census_gaps": manifest["census_gaps"],
        "per_function": per_function,
        "census": census,
        "t_total": t_total,
        "i_total": i_total,
        "t_equal_i": t_equal_i,
        "owners_complete": owners_complete,
        "has_lambda": has_lambda,
        "has_inner_class": has_inner_class,
        "eligible": eligible,
        "gap_lane": gap_lane,
    }
