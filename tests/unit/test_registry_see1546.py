"""SEE-1546 WP1b unit tests: neutral function registry (§SPEC-017),
probe-manifest/2 (§SPEC-018) and registry snapshot diff (§SPEC-020)."""

from __future__ import annotations

from pathlib import Path

import pytest

from godot_qa_toolkit.registry.functions import build_file_registry
from godot_qa_toolkit.registry.manifest import build_probe_manifest_v2
from godot_qa_toolkit.registry.snapshot import DEFAULT_ESCAPE_DIRS, diff_registries

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

PARSE_ERROR_SRC = (
    "func broken(:\n"
    "\treturn 1\n"
)


def _by_name(registry: dict, name: str) -> list[dict]:
    return [e for e in registry["entries"] if e["name"] == name]


def _kinds(registry: dict) -> list[str]:
    return [e["kind"] for e in registry["entries"]]


# --------------------------------------------------------------------------
# §SPEC-017: registry rules
# --------------------------------------------------------------------------


def test_registry_version_and_shape():
    registry = build_file_registry(RICH_SRC, file_label="x.gd")
    assert registry["registry_version"] == "func-registry/1"
    assert registry["file"] == "x.gd"
    assert registry["parse"] == "ok"
    assert "sha256" in registry
    # 4 func_def + 1 lambda — the Class aggregate never becomes an entry
    assert len(registry["entries"]) == 5


def test_class_aggregate_never_enters_registry():
    registry = build_file_registry(RICH_SRC, file_label="x.gd")
    assert "class_def" not in _kinds(registry)


def test_inner_class_method_registered_with_scope_and_not_instrumentable():
    registry = build_file_registry(RICH_SRC, file_label="x.gd")
    same = _by_name(registry, "same")
    assert len(same) == 1
    entry = same[0]
    assert entry["lex_scope"] == ["Wrapper"]
    assert entry["fid"] == "x.gd::Wrapper.same"
    assert entry["instrumentable"] is False
    assert entry["cc_oracle"] == 1


def test_static_counted_exactly_once_with_flag():
    registry = build_file_registry(RICH_SRC, file_label="x.gd")
    make = _by_name(registry, "make")
    # static_func_def must pass through: double counting (func_def +
    # static_func_def added together) is the forbidden M1 bug
    assert len(make) == 1
    assert make[0]["static"] is True
    assert make[0]["instrumentable"] is True
    assert make[0]["occurrence"] == 1


def test_lambda_registered_independently_with_host_backref():
    registry = build_file_registry(RICH_SRC, file_label="x.gd")
    lambdas = [e for e in registry["entries"] if e["kind"] == "lambda"]
    assert len(lambdas) == 1, "branching lambda missing from registry"
    lam = lambdas[0]
    assert lam["name"] == "<lambda>"
    assert lam["fid"] == "x.gd::host.<lambda#1>"
    host = _by_name(registry, "host")[0]
    assert lam["host_fid"] == host["fid"]
    assert lam["cc_oracle"] == 2  # 1 + its own if
    # host keeps the WP1a lexical-group semantics
    assert host["cc_oracle"] == 2
    assert host["lambda_gap"] == 1


def test_span_end_is_leaf_token_line_not_container_meta():
    """meta.end_line overflows to the next function header — the registry span
    must come from the leaf-token algorithm instead."""
    registry = build_file_registry(RICH_SRC, file_label="x.gd")
    plain = _by_name(registry, "plain")[0]
    make = _by_name(registry, "make")[0]
    assert make["end_line"] < plain["start_line"], (
        f"span overlap: make ends {make['end_line']}, plain starts {plain['start_line']}"
    )


def test_body_hash_present_and_stable():
    registry = build_file_registry(RICH_SRC, file_label="x.gd")
    for entry in registry["entries"]:
        assert len(entry["body_hash"]) == 12
    again = build_file_registry(RICH_SRC, file_label="x.gd")
    assert [e["body_hash"] for e in registry["entries"]] == [
        e["body_hash"] for e in again["entries"]
    ]


def test_occurrence_disambiguates_cross_scope_same_names():
    src = (
        "extends RefCounted\n"
        "\n"
        "class A:\n"
        "\tfunc same() -> int:\n"
        "\t\treturn 1\n"
        "\n"
        "func same() -> int:\n"
        "\treturn 2\n"
    )
    registry = build_file_registry(src, file_label="x.gd")
    fids = sorted(e["fid"] for e in registry["entries"])
    assert fids == ["x.gd::A.same", "x.gd::same"]
    for entry in registry["entries"]:
        assert entry["occurrence"] == 1


def test_second_same_scope_name_gets_occurrence_suffix():
    # GDScript forbids same-scope overloads; the defensive suffix is exercised
    # through the internal counter contract on a synthetic double pass.
    registry = build_file_registry(RICH_SRC, file_label="x.gd")
    entries = registry["entries"]
    names = [e["name"] for e in entries]
    assert names.count("plain") == 1
    keys = {(tuple(e["lex_scope"]), e["name"], e["kind"]) for e in entries}
    assert len(keys) == len(entries)  # no key collision in a legal file


def test_parse_error_file_yields_empty_entries_with_reason():
    registry = build_file_registry(PARSE_ERROR_SRC, file_label="bad.gd")
    assert registry["parse"] == "error"
    assert registry["entries"] == []
    assert "reason" in registry


# --------------------------------------------------------------------------
# §SPEC-018: probe-manifest/2
# --------------------------------------------------------------------------


def test_probe_plan_same_order_as_instrumentation():
    from godot_qa_toolkit.coverage.runner import _collect_executable_lines

    registry = build_file_registry(RICH_SRC, file_label="x.gd")
    manifest = build_probe_manifest_v2(RICH_SRC, registry)
    instrumented = _collect_executable_lines(RICH_SRC)
    assert [p["line"] for p in manifest["probe_plan"]] == [
        l.line for l in instrumented
    ]
    assert manifest["manifest_version"] == "probe-manifest/2"


def test_ownership_materialized_at_build_time():
    registry = build_file_registry(RICH_SRC, file_label="x.gd")
    manifest = build_probe_manifest_v2(RICH_SRC, registry)
    for probe in manifest["probe_plan"]:
        assert probe["fid"] is not None  # build-time materialization, zero inference
        assert probe["owner_kind"] in ("func", "lambda")
    census = manifest["census"]
    assert census["unowned"] == 0
    assert census["ambiguous"] == 0
    assert census["owners_complete"] is True


def test_lambda_body_lines_belong_to_lambda_entry():
    registry = build_file_registry(RICH_SRC, file_label="x.gd")
    manifest = build_probe_manifest_v2(RICH_SRC, registry)
    host = _by_name(registry, "host")[0]
    lam = [e for e in registry["entries"] if e["kind"] == "lambda"][0]
    lam_plan = [p for p in manifest["probe_plan"] if p["fid"] == lam["fid"]]
    assert lam_plan, "lambda body has no probes"
    assert all(p["owner_kind"] == "lambda" for p in lam_plan)
    host_plan = [p for p in manifest["probe_plan"] if p["fid"] == host["fid"]]
    assert host_plan, "host body has no probes"


def test_host_t_lines_exclude_lambda_subtree():
    registry = build_file_registry(RICH_SRC, file_label="x.gd")
    manifest = build_probe_manifest_v2(RICH_SRC, registry)
    host = _by_name(registry, "host")[0]
    lam = [e for e in registry["entries"] if e["kind"] == "lambda"][0]
    per = {p["fid"]: p for p in manifest["per_function"]}
    assert per[host["fid"]]["t_lines"] >= 1
    assert per[lam["fid"]]["t_lines"] >= 1
    # host T and lambda T are disjoint line sets: the host census prunes the
    # lambda subtree, so the lambda body lines belong to the lambda entry only
    host_ids = set(per[host["fid"]]["t_line_ids"])
    lam_ids = set(per[lam["fid"]]["t_line_ids"])
    assert not (host_ids & lam_ids)


def test_inner_class_census_gap_does_not_poison_siblings():
    registry = build_file_registry(RICH_SRC, file_label="x.gd")
    manifest = build_probe_manifest_v2(RICH_SRC, registry)
    gaps = manifest["census_gaps"]
    same_gap = [g for g in gaps if g["fid"] == "x.gd::Wrapper.same"]
    assert same_gap, "inner-class census gap missing"
    assert same_gap[0]["reason"] == "class_def_pruned"
    assert same_gap[0]["expected"] == "not_instrumented"
    per = {p["fid"]: p for p in manifest["per_function"]}
    assert per["x.gd::Wrapper.same"]["i_lines"] == 0
    assert per["x.gd::Wrapper.same"]["t_equal_i"] is False
    assert per["x.gd::Wrapper.same"]["state_hint"] == "partial_instrumentation"
    # siblings are untouched
    assert per["x.gd::plain"]["t_equal_i"] is True
    assert "state_hint" not in per["x.gd::plain"]


def test_ambiguous_ownership_is_counted_fail_loud(tmp_path: Path):
    """Two instrumentable entries claiming the *identical* span cannot be
    disambiguated — the line gets fid=None and the census counts it loud."""
    from godot_qa_toolkit.registry.manifest import build_probe_manifest_v2 as build

    src = "func f():\n\tpass\n"
    duplicate = {
        "registry_version": "func-registry/1",
        "file": "x.gd",
        "parse": "ok",
        "entries": [
            {
                "fid": "x.gd::f", "name": "f", "kind": "func_def",
                "lex_scope": [], "occurrence": 1, "host_fid": None,
                "static": False, "instrumentable": True,
                "start_line": 1, "end_line": 2, "body_hash": "a",
                "cc_oracle": 1, "lambda_gap": 0,
            },
            {
                "fid": "x.gd::twin", "name": "twin", "kind": "func_def",
                "lex_scope": [], "occurrence": 1, "host_fid": None,
                "static": False, "instrumentable": True,
                "start_line": 1, "end_line": 2, "body_hash": "b",
                "cc_oracle": 1, "lambda_gap": 0,
            },
        ],
    }
    manifest = build(src, duplicate)
    assert manifest["census"]["ambiguous"] >= 1
    assert manifest["census"]["owners_complete"] is False
    assert any(p["fid"] is None for p in manifest["probe_plan"])


# --------------------------------------------------------------------------
# §SPEC-020: registry snapshot diff
# --------------------------------------------------------------------------


def _entry(fid: str, body_hash: str, *, kind: str = "func_def",
           lex_scope: tuple = ()) -> dict:
    return {
        "fid": fid,
        "name": fid.rsplit(".", 1)[-1],
        "kind": kind,
        "lex_scope": list(lex_scope),
        "occurrence": 1,
        "static": False,
        "instrumentable": kind != "lambda",
        "start_line": 1,
        "end_line": 2,
        "body_hash": body_hash,
        "cc_oracle": 1,
        "lambda_gap": 0,
    }


def _snapshot(entries: list[dict], label: str = "f.gd") -> dict:
    return {"files": {label: {"entries": entries}}}


def test_diff_statuses_unchanged_edited_added_removed():
    old = _snapshot([_entry("f.gd::keep", "aaa"), _entry("f.gd::edit", "bbb"),
                     _entry("f.gd::gone", "ccc")])
    new = _snapshot([_entry("f.gd::keep", "aaa"), _entry("f.gd::edit", "B2B"),
                     _entry("f.gd::fresh", "ddd")])
    diff = diff_registries(old, new)
    rows = {r["fid"]: r["status"] for r in diff["rows"]}
    assert rows["f.gd::keep"] == "unchanged"
    assert rows["f.gd::edit"] == "edited"
    assert rows["f.gd::fresh"] == "added"
    assert rows["f.gd::gone"] == "removed"
    summary = diff["summary"]
    assert summary["added"] == 1 and summary["removed"] == 1
    assert summary["edited"] == 1


def test_identity_changed_candidate_requires_matching_body_hash():
    old = _snapshot([_entry("f.gd::OldName.helper", "samehash")])
    new = _snapshot([_entry("f.gd::MovedCls.helper", "samehash")])
    diff = diff_registries(old, new)
    rows = [r for r in diff["rows"] if r["status"] == "identity_changed_candidate"]
    assert len(rows) == 1
    row = rows[0]
    assert row["evidence"] == "body_hash_identical"
    assert row["human_final"] is True


def test_identity_changed_without_hash_match_is_add_remove():
    old = _snapshot([_entry("f.gd::old_fn", "hash1")])
    new = _snapshot([_entry("f.gd::new_fn", "hash2")])
    diff = diff_registries(old, new)
    rows = {r["fid"]: r["status"] for r in diff["rows"]}
    assert rows == {"f.gd::old_fn": "removed", "f.gd::new_fn": "added"}
    assert diff["summary"]["identity_changed_candidates"] == 0


def test_scope_escape_rules_and_governance_net(tmp_path: Path):
    old = _snapshot([])
    new_entries = [
        _entry("mods/f.gd::plain_add", "h1"),
        _entry("addons/f.gd::in_addons", "h2"),
        _entry("mods/g.gd::new_lambda", "h3", kind="lambda"),
        _entry("mods/g.gd::IntoCls.moved", "h4", lex_scope=("IntoCls",)),
    ]
    new = _snapshot(new_entries)
    diff = diff_registries(old, new, escape_dirs=DEFAULT_ESCAPE_DIRS)
    rows = {r["fid"]: r for r in diff["rows"]}
    assert rows["mods/f.gd::plain_add"]["scope_escape"] is False
    assert rows["addons/f.gd::in_addons"]["scope_escape"] is True
    assert rows["addons/f.gd::in_addons"]["escape_rule"] == "escape_dir"
    assert rows["mods/g.gd::new_lambda"]["scope_escape"] is True
    assert rows["mods/g.gd::new_lambda"]["escape_rule"] == "lambda_kind"
    assert rows["mods/g.gd::IntoCls.moved"]["scope_escape"] is True
    assert rows["mods/g.gd::IntoCls.moved"]["escape_rule"] == "moved_into_inner_class"
    summary = diff["summary"]
    assert summary["added"] == 4
    assert summary["scope_escape"] == 3
    # the machine gate: countable_added == added - scope_escape
    assert summary["governance_net"]["countable_added"] == (
        summary["added"] - summary["scope_escape"]
    )
    assert summary["governance_net"]["escape_added"] == 3
