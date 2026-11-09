"""SEE-1546 M2 shadow scenarios on pytest-bdd (§SPEC-006/007/008).

Godot-free: a deterministic fake `measure` injects core/full/re-run hits so
CI never needs a real engine. Real-Godot evidence is collected separately
(§SPEC-009 run with the coverage runner on the KOL tree).
"""

from __future__ import annotations

import pytest
from pytest_bdd import given, scenarios, then, when

from godot_qa_toolkit.risk.contract import build_shadow_record, classify_file_state
from godot_qa_toolkit.risk.formula import crap_line_score
from godot_qa_toolkit.risk.identity import (
    build_probe_manifest,
    collect_function_identities,
    file_profile,
    t_census,
)
from godot_qa_toolkit.risk.shadow import run_shadow

from ._support import fail_step as _fail

scenarios("features/see1546_shadow.feature")

TWO_FUNCS = (
    "extends RefCounted\n"
    "\n"
    "func first() -> int:\n"
    "\treturn 1\n"
    "\n"
    "func second() -> int:\n"
    "\tif 1:\n"
    "\t\treturn 2\n"
    "\treturn 3\n"
)

LAMBDA_SRC = (
    "extends RefCounted\n"
    "\n"
    "func host(x: int) -> int:\n"
    "\tvar f := func(v: int) -> int:\n"
    "\t\tif v > 0:\n"
    "\t\t\treturn v\n"
    "\t\treturn 0\n"
    "\treturn f(x)\n"
)

INNER_SRC = (
    "extends RefCounted\n"
    "\n"
    "class Inner:\n"
    "\tfunc method() -> int:\n"
    "\t\treturn 1\n"
    "\n"
    "func top() -> int:\n"
    "\treturn 2\n"
)

VAR_DEF_SRC = "func f() -> void:\n\tvar def := 1\n\tprint(def)\n"


def _fake_measure(hits_by_line: list[int]):
    def measure(label: str, source: str, tests_glob: str | None) -> dict:
        del source, tests_glob
        hits = hits_by_line
        total_lines = len(TWO_FUNCS.splitlines())
        return {
            "ok": True,
            "summary": {
                "total_lines": 6,
                "covered_lines": len(hits),
                "probes_fired": 6,
                "hits_lines": hits,
                "wall_s": 0.01,
            },
        }

    return measure


@given("a set of eligible GDScript sources and one non-eligible source")
def given_eligible_set(bdd_state: dict) -> None:
    bdd_state["eligible"] = {"a.gd": TWO_FUNCS}
    bdd_state["negatives"] = {"neg.gd": LAMBDA_SRC}


@when("the shadow partitions the candidates and measures the eligible files")
def when_partition_measure(bdd_state: dict) -> None:
    bdd_state["report"] = run_shadow(
        eligible_sources=bdd_state["eligible"],
        negative_sources=bdd_state["negatives"],
        measure=_fake_measure([4, 5, 7, 8]),
    )


@then("every eligible file reports all of its functions as scored records")
def then_all_scored(bdd_state: dict) -> None:
    records = bdd_state["report"]["records"]
    if not records:
        _fail("no records produced", "")
    if any(r["score_status"] != "scored" for r in records):
        _fail("non-scored record in eligible file", str(records))
    if len({r["function_id"] for r in records}) != 2:
        _fail("not all functions participated", str(records))


@then("only the non-eligible source routes to a gap lane")
def then_only_negative_lane(bdd_state: dict) -> None:
    negatives = bdd_state["report"]["negatives"]
    if len(negatives) != 1 or negatives[0]["gap_lane"] is None:
        _fail("negative lane mismatch", str(negatives))
    if bdd_state["report"]["confidence"]["sample_files"] != 1:
        _fail("eligible count mismatch", str(bdd_state["report"]["confidence"]))


@given("lambda inner class and var def negative sources")
def given_negatives(bdd_state: dict) -> None:
    bdd_state["negatives"] = {
        "lambda.gd": LAMBDA_SRC,
        "inner.gd": INNER_SRC,
        "vardef.gd": VAR_DEF_SRC,
    }


@when("the shadow profiles them without consuming a GUT round")
def when_profile_negatives(bdd_state: dict) -> None:
    def boom(*_args, **_kwargs) -> dict:  # pragma: no cover - must not fire
        raise AssertionError("negative files must not be measured")

    bdd_state["report"] = run_shadow(
        eligible_sources={},
        negative_sources=bdd_state["negatives"],
        measure=boom,
    )


@then("each negative lands in its own gap lane")
def then_each_lane(bdd_state: dict) -> None:
    lanes = {n["file"]: n["gap_lane"] for n in bdd_state["report"]["negatives"]}
    expected = {
        "lambda.gd": "lambda_decision_gap",
        "inner.gd": "partial_instrumentation",
        "vardef.gd": "sanitized_ir_projection",
    }
    if lanes != expected:
        _fail("gap lanes mismatch", str(lanes))


@then("none of them is counted as an eligible measured file")
def then_none_eligible(bdd_state: dict) -> None:
    report = bdd_state["report"]
    if report["confidence"]["sample_files"] != 0:
        _fail("negatives counted as eligible", str(report["confidence"]))


@given("an eligible source whose measurement fires probes with no function hit")
def given_zero_hit(bdd_state: dict) -> None:
    bdd_state["eligible"] = {"zero.gd": TWO_FUNCS}


@when("the shadow assembles its records")
def when_assemble_zero(bdd_state: dict) -> None:
    bdd_state["report"] = run_shadow(
        eligible_sources=bdd_state["eligible"],
        negative_sources={},
        measure=_fake_measure([]),
    )


@then("the file state is unknown")
def then_state_unknown(bdd_state: dict) -> None:
    states = {f["file"]: f["state"] for f in bdd_state["report"]["files"]}
    if states != {"zero.gd": "unknown"}:
        _fail("state is not unknown", str(states))


@then("no record is scored or marked true zero")
def then_no_scored(bdd_state: dict) -> None:
    records = bdd_state["report"]["records"]
    if any(r["score_status"] in ("scored", "true_zero_coverage") for r in records):
        _fail("zero-hit file produced a score", str(records))


@given("the crap-line/1 formula")
def given_formula(bdd_state: dict) -> None:
    bdd_state["formula"] = crap_line_score


@when("cc is 6 with coverage 0 0.5 and 1")
def when_cc6(bdd_state: dict) -> None:
    bdd_state["scores"] = [bdd_state["formula"](6, c) for c in (0.0, 0.5, 1.0)]


@then("the scores are exactly 42 10.5 and 6")
def then_scores(bdd_state: dict) -> None:
    if bdd_state["scores"] != pytest.approx([42.0, 10.5, 6.0]):
        _fail("formula boundary mismatch", str(bdd_state["scores"]))


@given("a scored record from the shadow")
def given_record(bdd_state: dict) -> None:
    bdd_state["record"] = build_shadow_record(
        function_id="x.gd::f",
        name="f",
        lex_path="f",
        start_line=1,
        end_line=3,
        cc=2,
        covered=1,
        total=2,
        cov_source="gut_full",
    )


@when("its disclosure fields are inspected")
def when_inspect(bdd_state: dict) -> None:
    bdd_state["disclosure"] = bdd_state["record"]


@then("coverage semantics is statement start line")
def then_semantics(bdd_state: dict) -> None:
    record = bdd_state["disclosure"]
    if record["coverage_semantics"] != "statement_start_line":
        _fail("coverage semantics wrong", str(record["coverage_semantics"]))
    if record["crap_line"]["coverage_semantics"] != "statement_start_line":
        _fail("inner semantics wrong", "")


@then("path coverage and assertion effectiveness are explicitly not measured")
def then_not_measured(bdd_state: dict) -> None:
    record = bdd_state["disclosure"]
    for key in ("path_coverage", "assertion_effectiveness"):
        if record[key] != "not_measured":
            _fail("missing not_measured disclosure", key)


@then("the evidence level is exploratory")
def then_evidence(bdd_state: dict) -> None:
    if bdd_state["disclosure"]["evidence_level"] != "exploratory":
        _fail("evidence level is not exploratory", "")


@given("a source with no executable statements")
def given_vacuous(bdd_state: dict) -> None:
    bdd_state["vacuous_state"] = classify_file_state(
        executable_probes=0, probes_fired=0, hits=0,
        gut_ok=True, run_error=False, timeout=False,
    )


@when("the shadow classifies and records it")
def when_record_vacuous(bdd_state: dict) -> None:
    bdd_state["vacuous_record"] = build_shadow_record(
        function_id="v.gd::empty",
        name="empty",
        lex_path="empty",
        start_line=1,
        end_line=2,
        cc=1,
        covered=None,
        total=None,
        cov_source="gut_full",
        state=bdd_state["vacuous_state"],
    )


@then("the state is vacuous")
def then_vacuous(bdd_state: dict) -> None:
    if bdd_state["vacuous_state"] != "vacuous":
        _fail("state is not vacuous", bdd_state["vacuous_state"])


@then("coverage and score are null")
def then_null(bdd_state: dict) -> None:
    record = bdd_state["vacuous_record"]
    if record["coverage"] is not None or record["crap_line"]["score"] is not None:
        _fail("vacuous record produced numbers", str(record["crap_line"]))
    if record["crap_line"]["coverage"] is not None:
        _fail("vacuous crap_line coverage not null", "")


@given("a GDScript source with several functions")
def given_identity_source(bdd_state: dict) -> None:
    bdd_state["identity_src"] = TWO_FUNCS


@when("the identity layer builds the probe manifest")
def when_identity(bdd_state: dict) -> None:
    identities = collect_function_identities(
        bdd_state["identity_src"], file_label="x.gd"
    )
    rows, manifest = build_probe_manifest(bdd_state["identity_src"], identities)
    bdd_state["identities"] = identities
    bdd_state["probe_rows"] = rows
    bdd_state["manifest"] = manifest


@then("every probe maps to exactly one function")
def then_probe_map(bdd_state: dict) -> None:
    rows = bdd_state["probe_rows"]
    if not rows:
        _fail("empty probe manifest", "")
    if any(r["function_id"] is None or r["owner_ambiguous"] for r in rows):
        _fail("ambiguous or unowned probe", str(rows))


@then("the census T equals the instrumentation set I")
def then_t_equals_i(bdd_state: dict) -> None:
    t_set = t_census(bdd_state["identities"])
    i_set = {r["line"] for r in bdd_state["probe_rows"]}
    if t_set != i_set:
        _fail("T != I", f"T={sorted(t_set)} I={sorted(i_set)}")


@given("a GDScript source with an inner class method")
def given_inner(bdd_state: dict) -> None:
    bdd_state["inner_src"] = INNER_SRC


@when("the instrumentation census is compared per function")
def when_inner_census(bdd_state: dict) -> None:
    profile = file_profile(INNER_SRC, file_label="inner.gd")
    bdd_state["inner_profile"] = profile


@then("the inner class body has no probe")
def then_inner_no_probe(bdd_state: dict) -> None:
    profile = bdd_state["inner_profile"]
    inner = next(f for f in profile["functions"] if f["name"] == "method")
    probe_lines = {row["line"] for row in profile["probes"]}
    body = set(range(inner["start_line"] + 1, inner["end_line"] + 1))
    if probe_lines & body:
        _fail("inner class body was instrumented", str(sorted(probe_lines & body)))


@then("the file is not eligible")
def then_inner_not_eligible(bdd_state: dict) -> None:
    if bdd_state["inner_profile"]["eligible"] is not False:
        _fail("inner-class file marked eligible", "")
    if bdd_state["inner_profile"]["gap_lane"] != "partial_instrumentation":
        _fail("gap lane wrong", str(bdd_state["inner_profile"]["gap_lane"]))
