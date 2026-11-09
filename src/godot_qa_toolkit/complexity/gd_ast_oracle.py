"""Raw GDScript AST decision-point oracle for complexity parity (WP1a §SPEC-004).

radon records over gd2py output are known to be lossy (branching lambdas,
and/or, ternaries and asserts evaporate), so they cannot act as their own
truth source. This oracle counts decision points on the *original* GDScript
AST via gdtoolkit's parser and diffs it against the radon records:

- strict match: oracle CC == radon CC — the measurement is faithful;
- lambda_gap:   oracle CC > radon CC by exactly the decision points that live
                inside lambda subtrees — the known pre-existing undercount
                (final-plan §4.4), disclosed per function, never written off;
- mismatch:     any other difference — a second-layer conversion defect that
                must be reported, never forced through.

Calibration (§SPEC-004 mini-parity, verified against gd2py's emitter):
`if_branch`/`elif_branch`/`for_stmt`/`for_stmt_typed`/`while_stmt` each +1;
`match_stmt` itself +1 (gd2py emits every match, wildcard or not, as if/elif
plus one extra tail branch) and every `match_branch`/`guarded_match_branch` +1
— a `when` guard expression evaporates like any other condition, so the branch
is one decision point either way.
Not counted: `and`/`or`, ternary `test_expr`, lambda bodies, `assert` — gd2py
evaporates all of them, so counting them would misreport faithfulness.
"""

from __future__ import annotations

from lark import Tree
from lark.lexer import Token

from .sanitizer import sanitize_python_ir_identifiers

DECISION_POINT_NODES = frozenset(
    {
        "if_branch",
        "elif_branch",
        "for_stmt",
        "for_stmt_typed",
        "while_stmt",
        "match_stmt",
        "match_branch",
        "guarded_match_branch",
    }
)


def iter_function_defs(tree: Tree) -> list[Tree]:
    """All func_def subtrees. static_func_def is only a wrapper around func_def
    in the raw AST — passing through it is what makes static count exactly
    once (adding the two node types is the forbidden double-count)."""
    return [fd for _, fd in _iter_function_defs_with_paths(tree)]


def _iter_function_defs_with_paths(tree: Tree) -> list[tuple[str, Tree]]:
    """(lex_path, func_def) pairs, the path qualified by enclosing `class_def`
    names plus the function name (e.g. `Wrapper.same`). GDScript permits the
    same function name in different scopes, so the bare name alone is not a
    function identity."""
    found: list[tuple[str, Tree]] = []
    stack: list[tuple[Tree, tuple[str, ...]]] = [(tree, ())]
    while stack:
        node, class_chain = stack.pop()
        if not isinstance(node, Tree):
            continue
        if node.data == "class_def":
            cls_name = next(
                (
                    str(t)
                    for t in node.scan_values(lambda v: isinstance(v, Token))
                    if t.type == "NAME"
                ),
                None,
            )
            if cls_name is not None:
                class_chain = class_chain + (cls_name,)
        if node.data == "func_def":
            fname = function_name(node)
            chain = class_chain + ((fname,) if fname else ())
            found.append((".".join(chain), node))
        stack.extend((c, class_chain) for c in node.children)
    # stack-pop reverses sibling order; restore source order for stable rows
    found.reverse()
    return found


def function_name(func_def: Tree) -> str | None:
    header = func_def.children[0] if func_def.children else None
    if not isinstance(header, Tree):
        return None
    for token in header.scan_values(lambda v: isinstance(v, Token)):
        if token.type == "NAME":
            return str(token)
    return None


def count_decision_points(func_def: Tree) -> int:
    total = 0
    for subtree in func_def.iter_subtrees():
        if subtree.data in DECISION_POINT_NODES:
            total += 1
    return total


def count_lambda_decision_points(func_def: Tree) -> int:
    total = 0
    for subtree in func_def.iter_subtrees():
        if subtree.data != "lambda":
            continue
        for inner in subtree.iter_subtrees():
            if inner.data in DECISION_POINT_NODES:
                total += 1
    return total


def parity_report(gd_source: str) -> list[dict]:
    """Diff raw-AST oracle CC against radon CC for every function in the
    source. Radon records carry post-conversion line numbers that do not map
    back to GDScript lines, so functions are matched by bare name — but
    GDScript allows the same name in different lexical scopes, and gd2py
    flattens inner-class methods to top-level defs, so a name collision makes
    pairing ambiguous: every affected function is reported with kind
    `identity_mismatch` (radon_cc=None) instead of being folded or guessed
    (Final Review MEDIUM-1: no record loss, no fake match).

    The oracle parses the *original* GDScript source; the radon side converts
    the same raw source and sanitizes the Python IR exactly like the
    collector does, so parity measures the real pipeline. A function actually
    named `def` would surface as missing-in-radon (its IR name becomes
    `def_`) and fail loudly instead of silently mis-matching."""
    from gdtoolkit.gd2py import convert_code
    from gdtoolkit.parser import parser
    from radon.complexity import cc_visit
    from radon.visitors import Function

    tree = parser.parse(gd_source)
    oracle_by_name: dict[str, list[dict]] = {}
    for lex_path, func_def in _iter_function_defs_with_paths(tree):
        name = function_name(func_def)
        if name is None:
            continue
        oracle_by_name.setdefault(name, []).append(
            {
                "lex_path": lex_path,
                "oracle_cc": 1 + count_decision_points(func_def),
                "lambda_gap": count_lambda_decision_points(func_def),
            }
        )

    python_code = sanitize_python_ir_identifiers(convert_code(gd_source))
    radon_by_name: dict[str, list[int]] = {}
    for block in cc_visit(python_code):
        if isinstance(block, Function):
            radon_by_name.setdefault(block.name, []).append(int(block.complexity))

    report: list[dict] = []
    for name, rows in oracle_by_name.items():
        radon_ccs = radon_by_name.get(name, [])
        if len(rows) == 1 and len(radon_ccs) == 1:
            row = rows[0]
            radon_cc = radon_ccs[0]
            lam = row["lambda_gap"]
            if radon_cc == row["oracle_cc"]:
                kind = "match"
            elif row["oracle_cc"] > radon_cc and row["oracle_cc"] - radon_cc == lam:
                kind = "lambda_gap"
            else:
                kind = "mismatch"
            report.append(
                {
                    "name": name,
                    "lex_path": row["lex_path"],
                    "oracle_cc": row["oracle_cc"],
                    "radon_cc": radon_cc,
                    "lambda_gap": lam if kind == "lambda_gap" else 0,
                    "kind": kind,
                }
            )
        elif len(rows) == 1 and not radon_ccs:
            # 1:0 — no pairing ambiguity; this is the missing-in-radon lane
            # (e.g. a function literally named `def` renamed by the sanitizer)
            row = rows[0]
            report.append(
                {
                    "name": name,
                    "lex_path": row["lex_path"],
                    "oracle_cc": row["oracle_cc"],
                    "radon_cc": None,
                    "lambda_gap": 0,
                    "kind": "mismatch",
                }
            )
        else:
            # ambiguous identity (same name in several scopes, or radon
            # emitting more records than the AST has functions): never guess
            # the pairing, never drop a record
            for row in rows:
                report.append(
                    {
                        "name": name,
                        "lex_path": row["lex_path"],
                        "oracle_cc": row["oracle_cc"],
                        "radon_cc": None,
                        "lambda_gap": 0,
                        "kind": "identity_mismatch",
                    }
                )
    return report
