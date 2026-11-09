"""SEE-1546 WP1a unit tests: def-token sanitization, Class-block filtering,
and the raw-GDScript-AST decision-point parity oracle (§SPEC-001/002/004)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from godot_qa_toolkit.complexity.collector import (
    _collect_python_source,
    collect_file,
    collect_paths,
)
from godot_qa_toolkit.complexity.gd_ast_oracle import (
    count_decision_points,
    count_lambda_decision_points,
    function_name,
    iter_function_defs,
    parity_report,
)
from godot_qa_toolkit.complexity.sanitizer import (
    sanitize_python_keyword_identifiers,
)

KOL_ROOT = os.environ.get("KOL_ROOT")

SEVEN_VAR_DEF_FILES = [
    "systems/effect/effect_definition_factory.gd",
    "scenes/ui/sane/sane_effect_formatter.gd",
    "autoload/effect_manager.gd",
    "autoload/hardware_manager.gd",
    "autoload/hardware_multi_driver.gd",
    "autoload/sane_subsystem.gd",
    "autoload/shadowconsole_subsystem.gd",
]


# --------------------------------------------------------------------------
# §SPEC-001: token-level def sanitization
# --------------------------------------------------------------------------


def test_sanitize_renames_def_at_identifier_positions_only():
    src = (
        "func compute(def: int) -> int:\n"
        "\tvar result := def * 2\n"
        "\tif result > 0:\n"
        "\t\treturn result\n"
        "\treturn def - result\n"
    )
    out = sanitize_python_keyword_identifiers(src)
    assert "def_" in out
    assert "var def " not in out
    # renamed consistently at every identifier occurrence — semantics preserved
    assert out.count("def_") == 3


def test_sanitize_skips_strings_and_comments():
    src = (
        'func messages() -> String:\n'
        '\t# comment mentions def here\n'
        '\tvar s := "call def in string"\n'
        "\tvar t := 'another def inside quotes'\n"
        "\treturn s + t\n"
    )
    out = sanitize_python_keyword_identifiers(src)
    assert "call def in string" in out
    assert "another def inside quotes" in out
    assert "# comment mentions def here" in out
    # only real identifiers renamed (none here besides s/t/messages)
    assert "def_" not in out


def test_sanitize_handles_raw_and_multiline_strings():
    # GDScript raw strings have no escape sequences: the first quote closes
    # the literal, so `r"raw def stays"` must survive verbatim while a normal
    # multiline string keeps its `def` untouched too.
    src = (
        'func texts() -> void:\n'
        '\tvar r := r"raw def stays"\n'
        '\tvar doc := """\n'
        "multi line doc with def inside\n"
        '\t"""\n'
        "\tprint(r)\n"
        "\tprint(doc)\n"
    )
    out = sanitize_python_keyword_identifiers(src)
    assert 'r"raw def stays"' in out
    assert "multi line doc with def inside" in out


def test_sanitize_covers_func_named_def_and_leaves_else_alone():
    # `def` is a legal GDScript identifier, so `func def():` is legal GDScript;
    # its *name* must be sanitized while GDScript keywords (`else:`) and the
    # Python `def foo():` keyword position produced later by gd2py stay intact.
    src = (
        "func def(x: int) -> int:\n"
        "\tif x > 0:\n"
        "\t\treturn 1\n"
        "\telse:\n"
        "\t\treturn 0\n"
    )
    out = sanitize_python_keyword_identifiers(src)
    assert "func def_(x: int) -> int:" in out
    assert "else:" in out
    assert not out.startswith("func def(")


def test_sanitize_avoids_name_collisions():
    src = (
        "func f(def_: int, def: int) -> int:\n"
        "\treturn def + def_\n"
    )
    out = sanitize_python_keyword_identifiers(src)
    lines = out.splitlines()
    # pre-existing `def_` must not be reused as the sanitized name
    assert "func f(def_: int, def__: int) -> int:" in lines[0]
    assert "return def__ + def_" in lines[1]


def test_sanitizer_off_leaves_file_unparseable(tmp_path: Path):
    """Independent negative probe for §SPEC-001: raw gd2py+radon on the same
    source (sanitizer bypassed) fails — proving the fix is the sanitizer."""
    from gdtoolkit.gd2py import convert_code
    from radon.complexity import cc_visit

    src = (
        "func f() -> void:\n"
        "\tvar def := 5\n"
        "\tprint(def)\n"
    )
    with pytest.raises(SyntaxError):
        cc_visit(convert_code(src))


def test_var_def_file_recovers_all_functions(tmp_path: Path):
    src = (
        "extends RefCounted\n"
        "\n"
        "func compute(def: int) -> int:\n"
        "\tvar result := def * 2\n"
        "\tif result > 0:\n"
        "\t\treturn result\n"
        "\treturn def - result\n"
        "\n"
        "func use_def() -> int:\n"
        "\tvar def := 5\n"
        "\tvar dict := {\"key\": def}\n"
        "\tprint(dict[\"key\"] + def)\n"
        "\treturn def\n"
    )
    p = tmp_path / "with_var_def.gd"
    p.write_text(src, encoding="utf-8")
    funcs, error = collect_file(p)
    assert error is None
    names = {f.name for f in funcs}
    assert names == {"compute", "use_def"}


# --------------------------------------------------------------------------
# §SPEC-002: Class aggregate blocks never pose as functions
# --------------------------------------------------------------------------


INNER_CLASS_SRC = (
    "extends RefCounted\n"
    "\n"
    "class CatalogEntry:\n"
    "\tvar name: String = \"\"\n"
    "\n"
    "\tfunc label() -> String:\n"
    "\t\treturn name\n"
    "\n"
    "func make() -> CatalogEntry:\n"
    "\tvar e := CatalogEntry.new()\n"
    "\te.name = \"x\"\n"
    "\treturn e\n"
)


def test_class_aggregate_block_not_reported_as_function(tmp_path: Path):
    p = tmp_path / "with_inner_class.gd"
    p.write_text(INNER_CLASS_SRC, encoding="utf-8")
    funcs, error = collect_file(p)
    assert error is None
    names = [f.name for f in funcs]
    assert "CatalogEntry" not in names


def test_real_functions_not_lost_with_class_filter(tmp_path: Path):
    p = tmp_path / "with_inner_class.gd"
    p.write_text(INNER_CLASS_SRC, encoding="utf-8")
    funcs, error = collect_file(p)
    assert error is None
    names = {f.name for f in funcs}
    # inner-class methods and outer functions all survive
    assert names == {"label", "make"}


def test_class_filter_independent_negative_probe(tmp_path: Path):
    """Without the filter the aggregate block DID pose as a function record —
    proving the filter is the fix (the guard can go red)."""
    from gdtoolkit.gd2py import convert_code
    from radon.complexity import cc_visit
    from radon.visitors import Class

    blocks = cc_visit(convert_code(INNER_CLASS_SRC))
    class_names = [b.name for b in blocks if isinstance(b, Class)]
    assert class_names == ["CatalogEntry"]


def test_python_source_class_blocks_filtered(tmp_path: Path):
    src = (
        "class Foo:\n"
        "    def bar(self):\n"
        "        return 1\n"
        "\n"
        "def top():\n"
        "    return 2\n"
    )
    p = tmp_path / "mod.py"
    p.write_text(src, encoding="utf-8")
    funcs, error = _collect_python_source(src, file_name=str(p))
    assert error is None
    names = [f.name for f in funcs]
    assert "Foo" not in names
    assert sorted(names) == ["bar", "top"]


# --------------------------------------------------------------------------
# §SPEC-004: raw GDScript AST decision-point parity oracle
# --------------------------------------------------------------------------


BRANCHING_SRC = (
    "extends RefCounted\n"
    "\n"
    "func branching(x: int, arr: Array) -> int:\n"
    "\tvar total := 0\n"
    "\tif x > 0 and arr.size() > 0:\n"
    "\t\ttotal += 1\n"
    "\telif x == 0 or arr.is_empty():\n"
    "\t\ttotal += 2\n"
    "\telse:\n"
    "\t\ttotal += 3\n"
    "\tfor item in arr:\n"
    "\t\tif item != null:\n"
    "\t\t\ttotal += 1\n"
    "\twhile total > 10:\n"
    "\t\ttotal -= 1\n"
    "\tmatch x:\n"
    "\t\t1:\n"
    "\t\t\ttotal += 10\n"
    "\t\t_:\n"
    "\t\t\ttotal += 20\n"
    "\treturn total\n"
    "\n"
    "func guard_match(x: int) -> int:\n"
    "\tmatch x:\n"
    "\t\t1 when x > 0:\n"
    "\t\t\treturn 1\n"
    "\t\t_:\n"
    "\t\t\treturn 0\n"
    "\n"
    "func typed_for(data: Dictionary) -> int:\n"
    "\tvar n := 0\n"
    "\tfor key: StringName in data:\n"
    "\t\tn += 1\n"
    "\treturn n\n"
)


def test_oracle_parity_on_decision_point_shapes(tmp_path: Path):
    """Every function in the fixture must agree: raw-AST oracle CC == radon CC
    (no lambda involved). and/or, ternary and assert are *not* decision points
    in this oracle because gd2py evaporates them before radon sees them."""
    p = tmp_path / "branching.gd"
    p.write_text(BRANCHING_SRC, encoding="utf-8")
    report = parity_report(BRANCHING_SRC)
    kinds = {row["name"]: row["kind"] for row in report}
    assert kinds == {
        "branching": "match",
        "guard_match": "match",
        "typed_for": "match",
    }
    funcs, error = collect_file(p)
    assert error is None
    by_name = {f.name: f.complexity for f in funcs}
    assert by_name["branching"] == 9  # 1 + if + elif + for + inner if + while + (1 + 2 match)
    assert by_name["guard_match"] == 4  # 1 + match_stmt + guarded branch + wildcard branch (guard expr evaporates)
    assert by_name["typed_for"] == 2  # 1 + for_stmt_typed


def test_static_functions_counted_once(tmp_path: Path):
    """static_func_def wraps func_def in the raw AST — the oracle must pass
    through the wrapper and count the function exactly once."""
    src = (
        "extends RefCounted\n"
        "\n"
        "static func make_default() -> RefCounted:\n"
        "\treturn RefCounted.new()\n"
        "\n"
        "func wrap() -> RefCounted:\n"
        "\treturn make_default()\n"
    )
    tree_count = 0
    from gdtoolkit.parser import parser

    for fd in iter_function_defs(parser.parse(src)):
        assert function_name(fd) in ("make_default", "wrap")
        assert count_decision_points(fd) >= 0
        tree_count += 1
    assert tree_count == 2
    report = parity_report(src)
    assert {row["name"]: row["kind"] for row in report} == {
        "make_default": "match",
        "wrap": "match",
    }


LAMBDA_HOST_SRC = (
    "extends RefCounted\n"
    "\n"
    "func host(x: int) -> int:\n"
    "\tvar f := func(v: int) -> int:\n"
    "\t\tif v > 0:\n"
    "\t\t\treturn v * 2\n"
    "\t\treturn -v\n"
    "\treturn f(x)\n"
)


def test_lambda_gap_fixture_reports_divergence_red():
    """Sentinel: the harness MUST report the branching-lambda divergence, not
    silently pass. If lambda evaporation is missed (e.g. oracle downgraded to
    radon), this test goes red."""
    report = parity_report(LAMBDA_HOST_SRC)
    host = [row for row in report if row["name"] == "host"]
    assert len(host) == 1
    row = host[0]
    assert row["kind"] == "lambda_gap"
    assert row["lambda_gap"] == 1
    assert row["oracle_cc"] == 2
    assert row["radon_cc"] == 1


def test_lambda_free_host_parity_unaffected():
    """A lambda-free function keeps strict parity — the gap lane is reserved
    for branching lambdas only, so the ledger can't be written off wholesale."""
    src = (
        "extends RefCounted\n"
        "\n"
        "func plain(x: int) -> int:\n"
        "\tif x > 0:\n"
        "\t\treturn 1\n"
        "\treturn 0\n"
    )
    report = parity_report(src)
    assert report[0]["kind"] == "match"
    assert report[0]["lambda_gap"] == 0


def test_lambda_decision_points_counted_in_lambda_subtree():
    from gdtoolkit.parser import parser

    fds = list(iter_function_defs(parser.parse(LAMBDA_HOST_SRC)))
    assert len(fds) == 1
    assert count_lambda_decision_points(fds[0]) == 1
    assert count_decision_points(fds[0]) == 1


# --------------------------------------------------------------------------
# §SPEC-004 (isolation): second-layer conversion failure stays unparseable
# --------------------------------------------------------------------------


def test_second_layer_failure_isolated_as_unparseable(tmp_path: Path):
    """Sanitization covers `def` only (WP1a narrow scope). A different
    Python-keyword collision (`var lambda`) must stay a *reported* structural
    failure — never forced through (§SPEC-004 cc_unmeasured lane)."""
    src = (
        "func trouble() -> void:\n"
        "\tvar lambda := 1\n"
        "\tprint(lambda)\n"
    )
    p = tmp_path / "trouble.gd"
    p.write_text(src, encoding="utf-8")
    funcs, error = collect_file(p)
    assert funcs == []
    assert error is not None
    assert "radon visit failed" in error


def test_collect_paths_reports_unparseable_without_crash(tmp_path: Path):
    good = tmp_path / "good.gd"
    good.write_text("func ok() -> int:\n\treturn 1\n", encoding="utf-8")
    bad = tmp_path / "bad.gd"
    bad.write_text(
        "func trouble() -> void:\n\tvar lambda := 1\n\tprint(lambda)\n",
        encoding="utf-8",
    )
    funcs, unparseable = collect_paths([str(tmp_path)])
    assert [f.name for f in funcs] == ["ok"]
    assert len(unparseable) == 1
    assert unparseable[0]["file"] == str(bad)
    assert "reason" in unparseable[0]


# --------------------------------------------------------------------------
# §SPEC-003: baseline re-anchor over the real KOL worktree (needs KOL_ROOT)
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    not KOL_ROOT, reason="KOL_ROOT not set — real-tree reconciliation skipped"
)
def test_seven_files_mini_parity_all_recovered():
    """Mini parity over all 170 recovered functions of the 7 var-def files:
    strict oracle==radon for lambda-free functions, disclosed lambda_gap for
    branching-lambda hosts, zero unexplained mismatches."""
    total = match = gap = mismatch = 0
    for rel in SEVEN_VAR_DEF_FILES:
        path = Path(KOL_ROOT) / rel
        funcs, error = collect_file(path)
        assert error is None, f"{rel} still unparseable: {error}"
        report = parity_report(path.read_text(encoding="utf-8"))
        radon = {f.name: f.complexity for f in funcs}
        assert len(report) == len(funcs) == len(radon), rel
        for row in report:
            total += 1
            assert radon[row["name"]] == row["radon_cc"], rel
            if row["kind"] == "match":
                match += 1
            elif row["kind"] == "lambda_gap":
                gap += 1
            else:
                mismatch += 1
    assert total == 170
    assert mismatch == 0
    assert gap == 3
    assert match == 167


@pytest.mark.skipif(
    not KOL_ROOT, reason="KOL_ROOT not set — real-tree reconciliation skipped"
)
def test_seven_files_recover_170_functions_and_rebaseline():
    """Baseline side-effects of §SPEC-003: the 7 files contribute 170 functions,
    the systems/scenes/autoload totals re-anchor to 1561/0, and the lambda
    decision gap stays visible (not written off) in the parity oracle."""
    from godot_qa_toolkit.complexity.gate import run_gate

    dirs = [str(Path(KOL_ROOT) / d) for d in ("systems", "scenes", "autoload")]
    result = run_gate(dirs)
    assert result["summary"]["functions"] == 1561
    assert result["summary"]["unparseable"] == 0

    recovered = 0
    for rel in SEVEN_VAR_DEF_FILES:
        funcs, error = collect_file(Path(KOL_ROOT) / rel)
        assert error is None, rel
        recovered += len(funcs)
    assert recovered == 170

    # the lambda gap lane survives the fix: branching-lambda hosts keep their
    # disclosed gap (cc_unmeasured for lambda hosts is NOT silently lifted)
    sane = parity_report((Path(KOL_ROOT) / "autoload/sane_subsystem.gd").read_text(encoding="utf-8"))
    gaps = [row for row in sane if row["kind"] == "lambda_gap"]
    assert gaps, "lambda decision gap must stay disclosed, not written off"
