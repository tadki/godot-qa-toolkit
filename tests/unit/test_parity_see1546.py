"""SEE-1546 WP1b parity unit tests: occurrence-order pairing, seven lanes and
the closure invariant (§SPEC-019)."""

from __future__ import annotations

import pytest

from godot_qa_toolkit.complexity.parity import (
    LANES,
    pair_file,
    parity_file,
)

PLAIN_SRC = (
    "extends RefCounted\n"
    "\n"
    "func one() -> int:\n"
    "\tif 1:\n"
    "\t\treturn 1\n"
    "\treturn 0\n"
    "\n"
    "func two() -> int:\n"
    "\treturn 2\n"
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

DEF_NAMED_SRC = (
    "extends RefCounted\n"
    "\n"
    "func def() -> int:\n"
    "\treturn 1\n"
)

PARSE_ERROR_SRC = "func broken(:\n\treturn 1\n"


def _lanes(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["lane"]] = counts.get(row["lane"], 0) + 1
    return counts


def test_plain_file_all_match():
    report = parity_file(PLAIN_SRC, file_label="x.gd")
    counts = _lanes(report["rows"])
    assert counts == {"match": 2}
    assert report["func_entries"] == 2
    assert report["lambda_entries"] == 0


def test_branching_lambda_host_and_lambda_disclosed_lanes():
    report = parity_file(LAMBDA_SRC, file_label="x.gd")
    counts = _lanes(report["rows"])
    assert counts.get("lambda_gap") == 1  # host undercounted by exactly its gap
    assert counts.get("lambda_disclosed") == 1  # expected blind spot, not error
    host = next(r for r in report["rows"] if r["name"] == "host")
    assert host["lambda_gap"] == 1
    assert host["radon_cc"] == 1
    assert host["cc_oracle"] == 2


def test_def_named_function_is_missing_in_radon():
    report = parity_file(DEF_NAMED_SRC, file_label="x.gd")
    counts = _lanes(report["rows"])
    assert counts.get("missing_in_radon") == 1
    row = report["rows"][0]
    assert row["name"] == "def"
    assert row["radon_cc"] is None


def test_parse_error_routes_to_cc_unmeasured():
    report = parity_file(PARSE_ERROR_SRC, file_label="bad.gd")
    counts = _lanes(report["rows"])
    assert counts.get("cc_unmeasured") == 1
    assert report["func_entries"] == 0


def test_lambda_gap_does_not_absorb_arbitrary_difference(monkeypatch):
    """oracle − radon must equal lambda_gap exactly for the gap lane; any other
    difference is a second-layer defect and lands in mismatch (fail-loud)."""
    report = parity_file(LAMBDA_SRC, file_label="x.gd")
    host = next(r for r in report["rows"] if r["name"] == "host")
    if host["lambda_gap"] == 1 and host["radon_cc"] == 1:
        # the real pipeline produces gap=1; a synthetic wider delta must NOT
        # be classified as lambda_gap — verify via the classifier directly
        from godot_qa_toolkit.complexity.parity import _lane_for

        assert _lane_for(2, 4, host["lambda_gap"]) == "mismatch"
        assert _lane_for(2, 1, host["lambda_gap"]) == "lambda_gap"


def test_mismatch_is_reported_not_swallowed(monkeypatch):
    """Reverse probe: a wrong implementation that never reports mismatch must
    fail this test (the mismatch lane stays load-bearing)."""
    from godot_qa_toolkit.complexity.parity import _lane_for

    assert _lane_for(5, 1, 0) == "mismatch"
    assert _lane_for(1, 5, 0) == "mismatch"


def test_closure_invariant_holds_on_fixtures():
    for src, label in (
        (PLAIN_SRC, "a.gd"),
        (LAMBDA_SRC, "b.gd"),
        (DEF_NAMED_SRC, "c.gd"),
    ):
        report = parity_file(src, file_label=label)
        lanes_total = sum(_lanes(report["rows"]).values())
        assert lanes_total == report["func_entries"] + report["lambda_entries"], (
            f"closure broken for {label}: lanes={lanes_total} "
            f"entries={report['func_entries']}+{report['lambda_entries']}"
        )


def test_lanes_cover_the_seven_lane_contract():
    assert set(LANES) == {
        "match",
        "lambda_gap",
        "lambda_disclosed",
        "missing_in_radon",
        "identity_mismatch",
        "mismatch",
        "cc_unmeasured",
    }


def test_occurrence_pairing_survives_cross_scope_same_names():
    """Two same-named functions in different scopes pair 1:1 by occurrence —
    the WP1a bare-name folding must not reappear here."""
    src = (
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
    report = parity_file(src, file_label="x.gd")
    counts = _lanes(report["rows"])
    assert counts == {"match": 2}
    fids = sorted(r["fid"] for r in report["rows"])
    assert fids == ["x.gd::A.same", "x.gd::same"]


def test_identity_mismatch_when_radon_count_differs(monkeypatch):
    """When radon yields *more* records than the registry for a name (pipeline
    anomaly — legal GDScript cannot produce same-scope overloads), the whole
    name group goes identity_mismatch — never folded, never guessed."""
    from types import SimpleNamespace
    from godot_qa_toolkit.complexity import parity as parity_mod

    def fake_radon(_src: str) -> list:
        # registry has one `one`, radon emits two — count divergence
        return [
            SimpleNamespace(name="one", lineno=3, complexity=1),
            SimpleNamespace(name="one", lineno=4, complexity=9),
            SimpleNamespace(name="two", lineno=8, complexity=1),
        ]

    monkeypatch.setattr(parity_mod, "_radon_records", fake_radon)
    report = parity_file(PLAIN_SRC, file_label="x.gd")
    counts = _lanes(report["rows"])
    assert counts.get("identity_mismatch") == 1
    assert counts.get("match") == 1
