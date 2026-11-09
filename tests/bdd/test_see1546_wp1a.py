"""SEE-1546 WP1a scenarios on pytest-bdd: def sanitization, Class-block
filtering, lambda-gap disclosure and the real-tree baseline re-anchor."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pytest_bdd import given, scenarios, then, when

from godot_qa_toolkit.complexity.collector import collect_file
from godot_qa_toolkit.complexity.gd_ast_oracle import parity_report
from godot_qa_toolkit.complexity.sanitizer import (
    sanitize_python_keyword_identifiers,
)

from ._support import fail_step as _fail

scenarios("features/see1546_wp1a.feature")

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

VAR_DEF_SRC = (
    "extends RefCounted\n"
    "\n"
    "func compute(def: int) -> int:\n"
    "\tvar result := def * 2\n"
    "\tif result > 0:\n"
    "\t\treturn result\n"
    "\treturn def - result\n"
    "\n"
    "func stash() -> int:\n"
    "\tvar def := 5\n"
    "\tprint(def)\n"
    "\treturn def\n"
)

VAR_DEF_WIDE_SRC = (
    "extends RefCounted\n"
    "\n"
    "var def_attr := 1\n"
    "\n"
    "func wide() -> int:\n"
    "\tvar def := 5\n"
    "\tdef += def_attr\n"
    "\tvar dict := {\"key\": def}\n"
    "\tdict[\"key\"] = dict[\"key\"] + def\n"
    "\treturn dict[\"key\"]\n"
)

STRING_COMMENT_SRC = (
    "func messages() -> String:\n"
    "\t# comment mentions def here\n"
    '\tvar s := "call def in string"\n'
    "\tvar t := 'another def inside quotes'\n"
    '\treturn s + t + "and def again"\n'
)

KEYWORD_POS_SRC = (
    "func def(x: int) -> int:\n"
    "\tif x > 0:\n"
    "\t\treturn 1\n"
    "\telif x == 0:\n"
    "\t\treturn 0\n"
    "\telse:\n"
    "\t\treturn -1\n"
)

INNER_CLASS_SRC = (
    "extends RefCounted\n"
    "\n"
    "class CatalogEntry:\n"
    '\tvar name: String = ""\n'
    "\n"
    "\tfunc label() -> String:\n"
    "\t\treturn name\n"
    "\n"
    "func make() -> CatalogEntry:\n"
    "\tvar e := CatalogEntry.new()\n"
    "\treturn e\n"
)

LAMBDA_HOST_SRC = (
    "extends RefCounted\n"
    "\n"
    "func host(x: int) -> int:\n"
    "\tvar f := func(v: int) -> int:\n"
    "\t\tif v > 0:\n"
    "\t\t\treturn v * 2\n"
    "\t\treturn -v\n"
    "\treturn f(x)\n"
    "\n"
    "func plain(x: int) -> int:\n"
    "\tif x > 0:\n"
    "\t\treturn 1\n"
    "\treturn 0\n"
)

NON_DEF_KEYWORD_SRC = (
    "func trouble() -> void:\n"
    "\tvar lambda := 1\n"
    "\tprint(lambda)\n"
)


@given("a GDScript file using def as a local variable name")
def given_var_def_file(tmp_path: Path, bdd_state: dict) -> None:
    (tmp_path / "with_var_def.gd").write_text(VAR_DEF_SRC, encoding="utf-8")


@given("a GDScript file with def only inside strings and comments")
def given_string_comment_file(tmp_path: Path, bdd_state: dict) -> None:
    (tmp_path / "strings.gd").write_text(STRING_COMMENT_SRC, encoding="utf-8")


@given("a GDScript file whose def tokens sit next to else and func keyword positions")
def given_keyword_position_file(tmp_path: Path, bdd_state: dict) -> None:
    (tmp_path / "keyword_pos.gd").write_text(KEYWORD_POS_SRC, encoding="utf-8")


@given("the raw gd2py pipeline without sanitization")
def given_raw_pipeline(bdd_state: dict) -> None:
    bdd_state["var_def_src"] = VAR_DEF_SRC


@given("a GDScript file with an inner class and outer functions")
def given_inner_class_file(tmp_path: Path, bdd_state: dict) -> None:
    (tmp_path / "inner_class.gd").write_text(INNER_CLASS_SRC, encoding="utf-8")


@given("the raw radon cc_visit output for a file with an inner class")
def given_raw_cc_visit(bdd_state: dict) -> None:
    bdd_state["inner_class_src"] = INNER_CLASS_SRC


@given("a GDScript file whose host function contains a branching lambda")
def given_lambda_host_file(tmp_path: Path, bdd_state: dict) -> None:
    (tmp_path / "lambda_host.gd").write_text(LAMBDA_HOST_SRC, encoding="utf-8")


@given("a GDScript file with a non-def Python keyword collision")
def given_non_def_collision_file(tmp_path: Path, bdd_state: dict) -> None:
    (tmp_path / "collision.gd").write_text(NON_DEF_KEYWORD_SRC, encoding="utf-8")
    (tmp_path / "sibling.gd").write_text(
        "func fine() -> int:\n\treturn 7\n", encoding="utf-8"
    )


@given("the real KOL worktree with systems scenes and autoload trees")
def given_kol_worktree(bdd_state: dict) -> None:
    if not KOL_ROOT:
        pytest.skip("KOL_ROOT not set — real-tree scenario skipped")


@when("the complexity collector processes the file")
def when_collector(tmp_path: Path, bdd_state: dict) -> None:
    files = sorted(tmp_path.glob("*.gd"))
    if not files:
        _fail("scenario forgot to write a fixture file", str(tmp_path))
    funcs: list = []
    errors: list = []
    for f in files:
        f_funcs, error = collect_file(f)
        funcs.extend(f_funcs)
        errors.append(error)
    bdd_state["funcs"] = funcs
    bdd_state["errors"] = errors
    bdd_state["root"] = tmp_path


@when("a file with var def is converted")
def when_raw_convert(bdd_state: dict) -> None:
    # gd2py's emit does not compile its output; the `def = ...` syntax error
    # only surfaces when radon's cc_visit compiles the converted source.
    from gdtoolkit.gd2py import convert_code
    from radon.complexity import cc_visit

    try:
        cc_visit(convert_code(bdd_state["var_def_src"]))
        bdd_state["raw_failed"] = False
    except SyntaxError:
        bdd_state["raw_failed"] = True


@when("the sanitizer builds the in-memory projection")
def when_sanitize(tmp_path: Path, bdd_state: dict) -> None:
    src = (tmp_path / "keyword_pos.gd").read_text(encoding="utf-8")
    bdd_state["sanitized"] = sanitize_python_keyword_identifiers(src)


@when("the unfiltered block list is inspected")
def when_unfiltered(bdd_state: dict) -> None:
    from gdtoolkit.gd2py import convert_code
    from radon.complexity import cc_visit
    from radon.visitors import Class

    blocks = cc_visit(convert_code(bdd_state["inner_class_src"]))
    bdd_state["class_names"] = [b.name for b in blocks if isinstance(b, Class)]


@when("the parity oracle diffs raw AST against radon records")
def when_parity(bdd_state: dict) -> None:
    bdd_state["parity"] = parity_report(LAMBDA_HOST_SRC)


@when("the complexity gate runs over the three trees")
def when_gate(bdd_state: dict) -> None:
    from godot_qa_toolkit.complexity.gate import run_gate

    dirs = [str(Path(KOL_ROOT) / d) for d in ("systems", "scenes", "autoload")]
    bdd_state["gate"] = run_gate(dirs)


@then("every named function is recovered with no error")
def then_recovered(bdd_state: dict) -> None:
    if bdd_state["errors"] != [None]:
        _fail("collector reported an error", str(bdd_state["errors"]))
    names = {f.name for f in bdd_state["funcs"]}
    if names != {"compute", "stash"}:
        _fail("recovered function set mismatch", str(names))


@then(
    "a second file relying on def in parameters attributes and dict keys is also recovered"
)
def then_wide_recovered(bdd_state: dict) -> None:
    wide = bdd_state["root"] / "wide.gd"
    wide.write_text(VAR_DEF_WIDE_SRC, encoding="utf-8")
    funcs, error = collect_file(wide)
    if error is not None:
        _fail("wide file still unparseable", str(error))
    names = {f.name for f in funcs}
    if names != {"wide"}:
        _fail("wide recovered function set mismatch", str(names))


@then("the file parses without error")
def then_parses(bdd_state: dict) -> None:
    if bdd_state["errors"] != [None]:
        _fail("expected clean parse", str(bdd_state["errors"]))
    if not bdd_state["funcs"]:
        _fail("no functions recovered", "")


@then("the sanitized projection leaves every string and comment verbatim")
def then_verbatim(bdd_state: dict) -> None:
    src = (bdd_state["root"] / "strings.gd").read_text(encoding="utf-8")
    out = sanitize_python_keyword_identifiers(src)
    for verbatim in (
        "# comment mentions def here",
        '"call def in string"',
        "'another def inside quotes'",
        '"and def again"',
    ):
        if verbatim not in out:
            _fail("verbatim fragment lost", verbatim)


@then("control flow tokens are untouched")
def then_control_flow(bdd_state: dict) -> None:
    for token in ("if x > 0:", "elif x == 0:", "else:", "return"):
        if token not in bdd_state["sanitized"]:
            _fail("control flow token disturbed", token)


@then("the function named def is renamed only at its name position")
def then_func_name_renamed(bdd_state: dict) -> None:
    out = bdd_state["sanitized"]
    if "func def_(x: int) -> int:" not in out:
        _fail("function name position not renamed", out.splitlines()[0])


@then("conversion fails with a syntax error proving the sanitizer is the fix")
def then_raw_fails(bdd_state: dict) -> None:
    if not bdd_state["raw_failed"]:
        _fail("raw pipeline unexpectedly succeeded", "")


@then("the inner class name is not reported as a function")
def then_no_class_pseudo(bdd_state: dict) -> None:
    names = [f.name for f in bdd_state["funcs"]]
    if "CatalogEntry" in names:
        _fail("class aggregate still posing as function", str(names))


@then("the inner class methods and outer functions are all reported")
def then_real_functions_kept(bdd_state: dict) -> None:
    names = {f.name for f in bdd_state["funcs"]}
    if names != {"label", "make"}:
        _fail("real function set mismatch", str(names))


@then("the aggregate class block is present proving the filter is load-bearing")
def then_aggregate_present(bdd_state: dict) -> None:
    if bdd_state["class_names"] != ["CatalogEntry"]:
        _fail("expected aggregate block in raw cc_visit", str(bdd_state["class_names"]))


@then("the host is reported in the lambda_gap lane with the exact gap size")
def then_lambda_gap_lane(bdd_state: dict) -> None:
    host = [r for r in bdd_state["parity"] if r["name"] == "host"]
    if len(host) != 1:
        _fail("host row missing", str(bdd_state["parity"]))
    row = host[0]
    if row["kind"] != "lambda_gap" or row["lambda_gap"] != 1:
        _fail("lambda gap not disclosed with exact size", str(row))


@then("lambda-free functions stay in the strict match lane")
def then_strict_lane(bdd_state: dict) -> None:
    plain = [r for r in bdd_state["parity"] if r["name"] == "plain"]
    if not plain or plain[0]["kind"] != "match":
        _fail("lambda-free function not in match lane", str(plain))


@then("the file stays unparseable with an explicit reason")
def then_isolated_unparseable(bdd_state: dict) -> None:
    errors = bdd_state["errors"]
    if not errors or errors[0] is None:
        _fail("collision file unexpectedly parsed", str(errors))
    funcs = [f.name for f in bdd_state["funcs"]]
    if "trouble" in funcs:
        _fail("failed file forced through", str(funcs))


@then("sibling files in the same run still report their functions")
def then_sibling_ok(bdd_state: dict) -> None:
    funcs = [f.name for f in bdd_state["funcs"]]
    if "fine" not in funcs:
        _fail("sibling function lost", str(funcs))


@then("exactly 1561 functions are reported with zero unparseable files")
def then_rebaseline(bdd_state: dict) -> None:
    summary = bdd_state["gate"]["summary"]
    if summary["functions"] != 1561 or summary["unparseable"] != 0:
        _fail(
            "baseline re-anchor mismatch",
            f"functions={summary['functions']} unparseable={summary['unparseable']}",
        )


@then("the seven var def files contribute exactly 170 recovered functions")
def then_170(bdd_state: dict) -> None:
    recovered = 0
    for rel in SEVEN_VAR_DEF_FILES:
        funcs, error = collect_file(Path(KOL_ROOT) / rel)
        if error is not None:
            _fail(f"{rel} still unparseable", str(error))
        recovered += len(funcs)
    if recovered != 170:
        _fail("recovered function total mismatch", str(recovered))


@then(
    "mini parity over the 170 functions shows no mismatch beyond the disclosed lambda gaps"
)
def then_mini_parity(bdd_state: dict) -> None:
    match = gap = mismatch = 0
    for rel in SEVEN_VAR_DEF_FILES:
        report = parity_report((Path(KOL_ROOT) / rel).read_text(encoding="utf-8"))
        for row in report:
            if row["kind"] == "match":
                match += 1
            elif row["kind"] == "lambda_gap":
                gap += 1
            else:
                mismatch += 1
    if mismatch != 0:
        _fail("unexplained parity mismatches", str(mismatch))
    if match != 167 or gap != 3:
        _fail(f"lane totals drifted: match={match} gap={gap}", "")
