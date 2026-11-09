"""SEE-1546 WP1b scenarios on pytest-bdd: neutral registry rules,
probe-manifest/2 build-time materialization, parity lanes/closure and the
scope-escape diff (§SPEC-017/018/019/020)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_bdd import given, scenarios, then, when

from godot_qa_toolkit.complexity.parity import parity_file
from godot_qa_toolkit.registry.functions import build_file_registry
from godot_qa_toolkit.registry.manifest import build_probe_manifest_v2
from godot_qa_toolkit.registry.snapshot import DEFAULT_ESCAPE_DIRS, diff_registries

from ._support import fail_step as _fail

scenarios("features/see1546_wp1b.feature")

RICH_SRC = (
    "extends RefCounted\n"
    "\n"
    "class Wrapper:\n"
    "\tfunc same() -> int:\n"
    "\t\treturn 1\n"
    "\n"
    "static func make() -> RefCounted:\n"
    "\treturn RefCounted.new()\n"
    "\n"
    "func host(x: int) -> int:\n"
    "\tvar f := func(v: int) -> int:\n"
    "\t\tif v > 0:\n"
    "\t\t\treturn v\n"
    "\t\treturn 0\n"
    "\treturn f(x)\n"
    "\n"
    "func plain() -> int:\n"
    "\treturn 7\n"
)

CROSS_SCOPE_SRC = (
    "extends RefCounted\n"
    "\n"
    "class A:\n"
    "\tfunc same() -> int:\n"
    "\t\tif 1:\n"
    "\t\t\treturn 1\n"
    "\t\treturn 0\n"
    "\n"
    "func same() -> int:\n"
    "\treturn 2\n"
)


@given("a GDScript source with static inner class lambda and plain functions")
def given_rich_source(bdd_state: dict) -> None:
    bdd_state["registry"] = build_file_registry(RICH_SRC, file_label="x.gd")


@when("the file registry is built")
def when_build_registry(bdd_state: dict) -> None:
    if "registry" not in bdd_state:
        bdd_state["registry"] = build_file_registry(RICH_SRC, file_label="x.gd")
    bdd_state["entries"] = bdd_state["registry"]["entries"]


@then("static inner class lambda and plain entries all exist")
def then_all_shapes(bdd_state: dict) -> None:
    names = {e["name"] for e in bdd_state["entries"]}
    if not {"same", "make", "host", "plain", "<lambda>"} <= names:
        _fail("callable shapes missing from registry", str(names))


@then("the class aggregate never becomes an entry")
def then_no_class_entry(bdd_state: dict) -> None:
    if any(e["kind"] == "class_def" for e in bdd_state["entries"]):
        _fail("class aggregate entered the registry", "")


@given("the same GDScript source")
def given_same_source(bdd_state: dict) -> None:
    bdd_state["registry"] = build_file_registry(RICH_SRC, file_label="x.gd")


@when("the registry invariants are checked")
def when_check_invariants(bdd_state: dict) -> None:
    bdd_state["entries"] = bdd_state["registry"]["entries"]


@then("static functions are counted exactly once")
def then_static_once(bdd_state: dict) -> None:
    make = [e for e in bdd_state["entries"] if e["name"] == "make"]
    if len(make) != 1 or not make[0]["static"]:
        _fail("static double-count or flag missing", str(make))


@then("the branching lambda has its own entry with a host backref")
def then_lambda_entry(bdd_state: dict) -> None:
    lam = [e for e in bdd_state["entries"] if e["kind"] == "lambda"]
    if len(lam) != 1:
        _fail("lambda entry missing", str(len(lam)))
    host = next(e for e in bdd_state["entries"] if e["name"] == "host")
    if lam[0]["host_fid"] != host["fid"]:
        _fail("lambda host backref wrong", lam[0]["host_fid"])


@then("no entry carries the kind class_def")
def then_no_class_kind(bdd_state: dict) -> None:
    if any(e["kind"] == "class_def" for e in bdd_state["entries"]):
        _fail("class_def kind found", "")


@given("the registry for the GDScript source")
def given_registry_for_manifest(bdd_state: dict) -> None:
    bdd_state["registry"] = build_file_registry(RICH_SRC, file_label="x.gd")


@when("the probe manifest v2 is built")
def when_build_manifest(bdd_state: dict) -> None:
    bdd_state["manifest"] = build_probe_manifest_v2(
        RICH_SRC, bdd_state["registry"]
    )


@then("every probe carries a non-null fid and owner kind")
def then_probes_materialized(bdd_state: dict) -> None:
    for probe in bdd_state["manifest"]["probe_plan"]:
        if probe["fid"] is None or probe["owner_kind"] is None:
            _fail("probe without materialized ownership", str(probe))


@then("the probe order matches the instrumentation manifest")
def then_probe_order(bdd_state: dict) -> None:
    from godot_qa_toolkit.coverage.runner import _collect_executable_lines

    instrumented = [l.line for l in _collect_executable_lines(RICH_SRC)]
    planned = [p["line"] for p in bdd_state["manifest"]["probe_plan"]]
    if planned != instrumented:
        _fail("probe order diverges from instrumentation", f"{planned} vs {instrumented}")


@given("an inner class method in the registry")
def given_inner_class(bdd_state: dict) -> None:
    bdd_state["registry"] = build_file_registry(RICH_SRC, file_label="x.gd")


@when("the probe manifest census is assembled")
def when_census(bdd_state: dict) -> None:
    bdd_state["manifest"] = build_probe_manifest_v2(
        RICH_SRC, bdd_state["registry"]
    )


@then(
    "the inner class method lands in census gaps with partial instrumentation"
)
def then_inner_gap(bdd_state: dict) -> None:
    gaps = [g for g in bdd_state["manifest"]["census_gaps"]
            if g["fid"] == "x.gd::Wrapper.same"]
    if not gaps or gaps[0]["reason"] != "class_def_pruned":
        _fail("inner class census gap missing", str(bdd_state["manifest"]["census_gaps"]))
    per = {p["fid"]: p for p in bdd_state["manifest"]["per_function"]}
    if per["x.gd::Wrapper.same"].get("state_hint") != "partial_instrumentation":
        _fail("state hint missing", str(per.get("x.gd::Wrapper.same")))


@then("the sibling entries keep their own probe plan and t equals i")
def then_siblings_ok(bdd_state: dict) -> None:
    per = {p["fid"]: p for p in bdd_state["manifest"]["per_function"]}
    plain = per.get("x.gd::plain")
    if not plain or not plain["t_equal_i"] or plain["i_lines"] == 0:
        _fail("sibling poisoned by inner-class gap", str(plain))


@given("a host function containing a branching lambda")
def given_host_lambda(bdd_state: dict) -> None:
    bdd_state["registry"] = build_file_registry(RICH_SRC, file_label="x.gd")


@when("the per function decomposition is assembled")
def when_per_function(bdd_state: dict) -> None:
    bdd_state["manifest"] = build_probe_manifest_v2(
        RICH_SRC, bdd_state["registry"]
    )


@then("the host and lambda T line sets are disjoint")
def then_disjoint(bdd_state: dict) -> None:
    per = {p["fid"]: p for p in bdd_state["manifest"]["per_function"]}
    host_t = set(per["x.gd::host"]["t_line_ids"])
    lam_t = set(per["x.gd::host.<lambda#1>"]["t_line_ids"])
    if host_t & lam_t:
        _fail("host T swallowed lambda lines", str(sorted(host_t & lam_t)))


@given("two same-named functions in different lexical scopes")
def given_cross_scope(bdd_state: dict) -> None:
    bdd_state["src"] = CROSS_SCOPE_SRC


@when("the parity rows are produced")
def when_parity(bdd_state: dict) -> None:
    bdd_state["parity"] = parity_file(bdd_state["src"], file_label="x.gd")


@then("both pair by occurrence and land in the match lane")
def then_pairing(bdd_state: dict) -> None:
    rows = bdd_state["parity"]["rows"]
    if len(rows) != 2 or any(r["lane"] != "match" for r in rows):
        _fail("occurrence pairing broken", str(rows))
    if any(r["pairing"] != "occurrence" for r in rows):
        _fail("pairing mode not occurrence", str(rows))


@given(
    "parity rows over fixtures with match lambda_gap lambda_disclosed and missing lanes"
)
def given_parity_rows(bdd_state: dict) -> None:
    lambda_src = (
        "extends RefCounted\n"
        "\n"
        "func host(x: int) -> int:\n"
        "\tvar f := func(v: int) -> int:\n"
        "\t\tif v > 0:\n"
        "\t\t\treturn v\n"
        "\t\treturn 0\n"
        "\treturn f(x)\n"
    )
    def_src = "extends RefCounted\n\nfunc def() -> int:\n\treturn 1\n"
    plain = parity_file("extends RefCounted\n\nfunc f() -> int:\n\treturn 1\n",
                        file_label="p.gd")
    lam = parity_file(lambda_src, file_label="l.gd")
    dfn = parity_file(def_src, file_label="d.gd")
    bdd_state["rows"] = plain["rows"] + lam["rows"] + dfn["rows"]
    bdd_state["func_entries"] = (
        plain["func_entries"] + lam["func_entries"] + dfn["func_entries"]
    )
    bdd_state["lambda_entries"] = (
        plain["lambda_entries"] + lam["lambda_entries"] + dfn["lambda_entries"]
    )


@when("the lane totals are summed")
def when_lane_totals(bdd_state: dict) -> None:
    bdd_state["lane_total"] = len(bdd_state["rows"])


@then("the closure invariant equals func entries plus lambda entries")
def then_closure(bdd_state: dict) -> None:
    expected = bdd_state["func_entries"] + bdd_state["lambda_entries"]
    if bdd_state["lane_total"] != expected:
        _fail(
            "closure invariant broken",
            f"lanes={bdd_state['lane_total']} expected={expected}",
        )


@given("added entries covering all three escape rules")
def given_escape_entries(bdd_state: dict) -> None:
    def entry(fid: str, body_hash: str, *, kind: str = "func_def",
              lex_scope: tuple = ()) -> dict:
        return {
            "fid": fid, "name": fid.rsplit(".", 1)[-1], "kind": kind,
            "lex_scope": list(lex_scope), "occurrence": 1, "static": False,
            "instrumentable": kind != "lambda", "start_line": 1,
            "end_line": 2, "body_hash": body_hash, "cc_oracle": 1,
            "lambda_gap": 0,
        }

    bdd_state["old"] = {"files": {"f.gd": {"entries": []}}}
    bdd_state["new"] = {
        "files": {
            "f.gd": {
                "entries": [
                    entry("mods/f.gd::plain_add", "h1"),
                    entry("addons/f.gd::in_addons", "h2"),
                    entry("mods/g.gd::new_lambda", "h3", kind="lambda"),
                    entry("mods/g.gd::IntoCls.moved", "h4", lex_scope=("IntoCls",)),
                ]
            }
        }
    }


@when("the registry diff is computed")
def when_diff(bdd_state: dict) -> None:
    bdd_state["diff"] = diff_registries(
        bdd_state["old"], bdd_state["new"], escape_dirs=DEFAULT_ESCAPE_DIRS
    )


@then("every escape row is marked with its rule")
def then_escape_rows(bdd_state: dict) -> None:
    rows = {r["fid"]: r for r in bdd_state["diff"]["rows"]}
    expected_rules = {
        "mods/f.gd::plain_add": None,
        "addons/f.gd::in_addons": "escape_dir",
        "mods/g.gd::new_lambda": "lambda_kind",
        "mods/g.gd::IntoCls.moved": "moved_into_inner_class",
    }
    for fid, rule in expected_rules.items():
        row = rows[fid]
        if row.get("scope_escape") != (rule is not None):
            _fail(f"escape flag wrong for {fid}", str(row))
        if rule is not None and row.get("escape_rule") != rule:
            _fail(f"escape rule wrong for {fid}", str(row.get("escape_rule")))


@then("countable added equals added minus scope escape")
def then_governance_net(bdd_state: dict) -> None:
    summary = bdd_state["diff"]["summary"]
    if summary["governance_net"]["countable_added"] != (
        summary["added"] - summary["scope_escape"]
    ):
        _fail("governance net arithmetic broken", str(summary))
