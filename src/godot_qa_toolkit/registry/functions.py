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

Import direction (design §4.3): this module touches only gdtoolkit AST and
the WP1a oracle counters — the probe-plan side lives in registry.manifest.
"""

from __future__ import annotations

import hashlib
import re

from gdtoolkit.parser import parser as gdtoolkit_parser
from lark import Tree
from lark.lexer import Token

from ..complexity.gd_ast_oracle import (
    count_decision_points,
    count_lambda_decision_points,
)
from ..mutation.scopemanifest import _max_leaf_line

REGISTRY_VERSION = "func-registry/1"


def _first_name(node: Tree) -> str | None:
    for token in node.scan_values(lambda v: isinstance(v, Token)):
        if token.type == "NAME":
            return str(token)
    return None


def _start_line(node: Tree) -> int:
    """First leaf-token line of the subtree. Tokens carry their own line from
    the lexer, so the registry parse can skip gather_metadata entirely —
    with-metadata parsing was 8.6s of the 13.9s parity budget (§SPEC-022)."""
    best = 0
    stack = [node]
    while stack:
        current = stack.pop()
        meta = getattr(current, "meta", None)
        if meta is not None and not getattr(meta, "empty", True):
            line = getattr(meta, "line", 0) or 0
            if line:
                best = best or line
        for child in getattr(current, "children", []):
            if isinstance(child, Tree):
                stack.append(child)
            else:
                line = getattr(child, "line", 0) or 0
                if line and (not best or line < best):
                    best = line
    return best


def _body_hash(src_lines: list[str], start_line: int, end_line: int) -> str:
    chunk = "".join(src_lines[start_line - 1 : end_line])
    return hashlib.sha1(chunk.encode("utf-8")).hexdigest()[:12]


def _span(node: Tree) -> tuple[int, int]:
    return (_start_line(node), _max_leaf_line(node))


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
        start, end = _span(lambda_node)
        hash_text = _body_hash(src_lines, start, end)
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
                "body_hash": hash_text,
                "cc_oracle": 1 + count_decision_points(lambda_node),
                "lambda_gap": 0,
            }
        )

    def _register_func(func_node: Tree, scope: tuple, static: bool,
                       instrumentable: bool) -> None:
        name = _first_name(func_node) or "<unnamed>"
        key = (scope, name, "func_def")
        occurrence[key] = occurrence.get(key, 0) + 1
        fid = _occurrence_fid(file_label, scope, name, occurrence[key])
        start, end = _span(func_node)
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
                "cc_oracle": 1 + count_decision_points(func_node),
                "lambda_gap": count_lambda_decision_points(func_node),
            }
        )
        for sub in func_node.iter_subtrees():
            if sub.data == "lambda":
                _register_lambda(sub, fid, scope)

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
