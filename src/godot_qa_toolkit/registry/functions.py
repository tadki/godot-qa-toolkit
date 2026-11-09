"""Callable traversal for the neutral function registry (§SPEC-017).

Identity key = normalized path + lexical class chain + name + same-class
occurrence order. static_func_def wrappers are passed through (a function is
counted exactly once, with a `static` flag), lambdas are registered as
standalone entries with a host back-reference, inner-class methods carry the
class chain and `instrumentable=False`, and class_def nodes only ever appear
inside `lex_scope` — the Class aggregate never becomes an entry. Spans end at
the mutation leaf-token max line (container meta.end_line overflows and is
never trusted); the mutation scope semantics (inner-class black box, lambda
folded into host) are deliberately NOT inherited.

Performance (§SPEC-022): the per-callable facts (decision points, host
lambda_gap, token line bounds, nested lambdas) are collected in ONE DFS pass
per function instead of four separate subtree walks — the lark parse of the
largest census file (steady_engine.gd, 635 lines) alone costs ~0.44s, so the
static layer only fits the 0.5s/file budget with single-pass traversal. The
counting semantics replicate the WP1a oracle counters node-for-node
(DECISION_POINT_NODES membership, lambdas descended for the host lexical
group); equivalence is pinned by the full registry/parity fixture set.

Import direction (design §4.3): this module touches only gdtoolkit AST and
the WP1a oracle constants — the probe-plan side lives in registry.manifest.
"""

from __future__ import annotations

import hashlib

from gdtoolkit.parser import parser as gdtoolkit_parser
from lark import Tree
from lark.lexer import Token

from ..complexity.gd_ast_oracle import DECISION_POINT_NODES
from ..mutation.scopemanifest import _max_leaf_line

REGISTRY_VERSION = "func-registry/1"


def _start_line(node: Tree) -> int:
    """First leaf-token line of the subtree, identical under both parse modes
    (gather_metadata True/False): with metadata the Tree.meta.line equals the
    first leaf token's lexer line, so callers may pin this anchor either way
    (hardener contract test_start_line_agrees_between_metadata_modes).

    Kept as the public span-start helper even though build_file_registry now
    derives line bounds inside its single-pass scan (§SPEC-022)."""
    best = 0
    stack = [node]
    while stack:
        current = stack.pop()
        meta = getattr(current, "meta", None)
        if meta is not None and not getattr(meta, "empty", True):
            line = getattr(meta, "line", 0) or 0
            if line and (not best or line < best):
                best = line
        for child in getattr(current, "children", []):
            if isinstance(child, Tree):
                stack.append(child)
            else:
                line = getattr(child, "line", 0) or 0
                if line and (not best or line < best):
                    best = line
    return best


def _first_name(node: Tree) -> str | None:
    """Class-name lookup (rare — at most one inner-class file in the census):
    whole-subtree scan keeps it robust against class_def header shapes."""
    for token in node.scan_values(lambda v: isinstance(v, Token)):
        if token.type == "NAME":
            return str(token)
    return None


def _func_name(func_node: Tree) -> str | None:
    """Function name = first NAME token in the func_header (children[0]) —
    O(header) instead of a whole-subtree scan (§SPEC-022 static layer)."""
    header = func_node.children[0] if func_node.children else None
    if not isinstance(header, Tree):
        return None
    for child in header.children:
        if isinstance(child, Token) and child.type == "NAME":
            return str(child)
    return None


def _body_hash(src_lines: list[str], start_line: int, end_line: int) -> str:
    chunk = "".join(src_lines[start_line - 1 : end_line])
    return hashlib.sha1(chunk.encode("utf-8")).hexdigest()[:12]


def _scan_dp_bounds(node: Tree, collect_lambdas: bool):
    """One DFS pass over a callable subtree: lexical-group decision points,
    token line bounds, and (optionally) the direct lambda nodes.

    The host count INCLUDES lambda subtree decision points (WP1a lexical
    group semantics: cc_oracle = 1 + every decision point textually inside
    the function, lambdas included); each lambda's own count is derived by a
    separate scan of its (small) subtree, replicating
    count_lambda_decision_points' per-lambda summation.
    """
    dp = 0
    lo = 0
    hi = 0
    lambdas: list[Tree] = []
    stack: list[Tree] = [node]
    while stack:
        current = stack.pop()
        if current.data in DECISION_POINT_NODES:
            dp += 1
        for child in getattr(current, "children", []):
            if isinstance(child, Tree):
                if collect_lambdas and child.data == "lambda":
                    lambdas.append(child)
                stack.append(child)
            else:
                line = getattr(child, "line", 0) or 0
                if line:
                    lo = line if not lo or line < lo else lo
                    hi = line if line > hi else hi
    return dp, lo, hi, lambdas


def _lambda_facts(lambda_node: Tree) -> tuple[int, int, int]:
    dp, lo, hi = _scan_dp_bounds(lambda_node, collect_lambdas=False)[:3]
    return dp, lo, hi


def _occurrence_fid(file_label: str, scope: tuple[str, ...], name: str,
                    order: int) -> str:
    scope_part = "".join(f"{s}." for s in scope)
    base = f"{scope_part}{name}"
    return f"{file_label}::{base}" + (f"#{order}" if order > 1 else "")


def build_file_registry(gd_source: str, file_label: str) -> dict:
    """Build the `func-registry/1` snapshot for one file.

    Parse failures produce `entries=[]` with `parse:"error"` and a reason —
    the file routes to the cc_unmeasured ledger (WP1a discipline), never a
    crash.
    """
    snapshot: dict = {
        "registry_version": REGISTRY_VERSION,
        "file": file_label,
        "sha256": hashlib.sha256(gd_source.encode("utf-8")).hexdigest(),
        "parse": "ok",
        "entries": [],
    }
    # gather_metadata=False: the registry only needs leaf-token lines, and
    # metadata assembly dominated the parse cost (§SPEC-022 budget)
    try:
        tree = gdtoolkit_parser.parse(gd_source, gather_metadata=False)
    except Exception as e:
        snapshot["parse"] = "error"
        snapshot["reason"] = f"{type(e).__name__}: {e}"
        return snapshot

    src_lines = gd_source.splitlines(keepends=True)
    entries: list[dict] = []
    occurrence: dict[tuple, int] = {}
    lambda_order: dict[str, int] = {}
    inner_classes: list[str] = []

    def _register_lambda(lambda_node: Tree, host_fid: str, scope: tuple) -> None:
        k = lambda_order.get(host_fid, 0) + 1
        lambda_order[host_fid] = k
        lam_dp, start, end = _lambda_facts(lambda_node)
        entries.append(
            {
                "fid": f"{host_fid}.<lambda#{k}>",
                "name": "<lambda>",
                "kind": "lambda",
                "lex_scope": list(scope),
                "occurrence": k,
                "host_fid": host_fid,
                "static": False,
                "instrumentable": True,
                "start_line": start,
                "end_line": end,
                "body_hash": _body_hash(src_lines, start, end),
                "cc_oracle": 1 + lam_dp,
                "lambda_gap": 0,
            }
        )

    def _register_func(func_node: Tree, scope: tuple, static: bool,
                       instrumentable: bool) -> None:
        name = _func_name(func_node) or "<unnamed>"
        key = (scope, name, "func_def")
        occurrence[key] = occurrence.get(key, 0) + 1
        fid = _occurrence_fid(file_label, scope, name, occurrence[key])
        host_dp, start, end, lambdas = _scan_dp_bounds(
            func_node, collect_lambdas=True
        )
        # <lambda#k> by source order of each lambda's start line
        lam_facts = [
            (lam_node, *_lambda_facts(lam_node)) for lam_node in lambdas
        ]  # (node, dp, start, end)
        lam_facts.sort(key=lambda t: t[2])
        lambda_gap = sum(t[1] for t in lam_facts)
        entries.append(
            {
                "fid": fid,
                "name": name,
                "kind": "func_def",
                "lex_scope": list(scope),
                "occurrence": occurrence[key],
                "host_fid": None,
                "static": static,
                "instrumentable": instrumentable,
                "start_line": start,
                "end_line": end,
                "body_hash": _body_hash(src_lines, start, end),
                "cc_oracle": 1 + host_dp,
                "lambda_gap": lambda_gap,
            }
        )
        lambda_order[fid] = 0
        for lam_node, _dp, _lo, _hi in lam_facts:
            _register_lambda(lam_node, fid, scope)

    def _walk(node: Tree, scope: tuple, instrumentable: bool) -> None:
        for child in getattr(node, "children", []):
            if not isinstance(child, Tree):
                continue
            if child.data == "static_func_def":
                inner = next(
                    (c for c in child.children
                     if isinstance(c, Tree) and c.data == "func_def"),
                    None,
                )
                if inner is not None:
                    _register_func(inner, scope, static=True,
                                   instrumentable=instrumentable)
            elif child.data == "func_def":
                _register_func(child, scope, static=False,
                               instrumentable=instrumentable)
            elif child.data == "class_def":
                cls_name = _first_name(child)
                if cls_name is not None:
                    inner_classes.append(cls_name)
                    # inner-class methods: extended scope and
                    # instrumentable=False (the coverage instrumenter prunes
                    # class_def bodies — SEE-1367 runner evidence)
                    _walk(child, scope + (cls_name,), instrumentable=False)
            elif child.data == "lambda":
                # module-level lambdas: standalone, no host (0 occurrences in
                # the current census — placeholder per plan §4.4)
                _register_lambda(child, f"{file_label}::", scope)
            else:
                _walk(child, scope, instrumentable)

    _walk(tree, (), instrumentable=True)
    entries.sort(key=lambda e: (e["start_line"], e["fid"]))
    snapshot["entries"] = entries
    snapshot["inner_classes"] = inner_classes
    return snapshot
