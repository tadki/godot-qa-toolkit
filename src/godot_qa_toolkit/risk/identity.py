"""Function identity, probe→function mapping and instrumentation census (§SPEC-006).

Identity is anchored on the raw GDScript AST (gdtoolkit), never on radon
records: lex_path (class chain + name), span, decision-point cc and a body
hash. The probe manifest reuses the coverage runner's executable-line set
(the instrumentation `I`) and compares it with an independently derived
per-function statement census (`T`) — class_def bodies are pruned by the
instrumenter, so inner-class files expose that gap as `t_equal_i=False`
instead of pretending full measurement.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from gdtoolkit.parser import parser as gdtoolkit_parser
from lark import Tree

from ..complexity.gd_ast_oracle import (
    _iter_function_defs_with_paths,
    count_decision_points,
    function_name,
)
from ..complexity.sanitizer import sanitize_python_ir_identifiers
from ..coverage.runner import (
    _EXECUTABLE_NODE_NAMES,
    _collect_executable_lines,
    _node_line,
    _paren_depths,
)


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


def _body_hash(src: str, node: Tree) -> str:
    start = getattr(node.meta, "start_pos", None)
    end = getattr(node.meta, "end_pos", None)
    chunk = src[start:end] if start is not None and end is not None else ""
    return hashlib.sha1(chunk.encode("utf-8")).hexdigest()[:12]


def _function_statement_lines(node: Tree, depths: list[int]) -> tuple[int, ...]:
    """Per-function statement census (T side): a *separate* traversal from the
    global instrumentation collector, restricted to the func_def subtree."""
    found: list[int] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.data in _EXECUTABLE_NODE_NAMES:
            ln = _node_line(current)
            if ln and ln <= len(depths) and depths[ln - 1] == 0:
                found.append(ln)
        stack.extend(c for c in getattr(current, "children", []) if hasattr(c, "data"))
    return tuple(sorted(set(found)))


def collect_function_identities(gd_source: str, file_label: str) -> list[FunctionIdentity]:
    tree = gdtoolkit_parser.parse(gd_source, gather_metadata=True)
    depths = _paren_depths(gd_source)
    identities: list[FunctionIdentity] = []
    for lex_path, func_def in _iter_function_defs_with_paths(tree):
        name = function_name(func_def)
        if name is None:
            continue
        meta = func_def.meta
        identities.append(
            FunctionIdentity(
                function_id=f"{file_label}::{lex_path}",
                name=name,
                lex_path=lex_path,
                start_line=int(getattr(meta, "line", 0) or 0),
                end_line=int(getattr(meta, "end_line", 0) or 0),
                cc=1 + count_decision_points(func_def),
                kind=str(func_def.data),
                body_hash=_body_hash(gd_source, func_def),
                t_lines=_function_statement_lines(func_def, depths),
            )
        )
    return identities


def t_census(identities: list[FunctionIdentity]) -> set[int]:
    lines: set[int] = set()
    for identity in identities:
        lines.update(identity.t_lines)
    return lines


def _owner(identities: list[FunctionIdentity], line: int) -> tuple[str | None, bool]:
    """Line → function_id by the innermost span (GDScript has no nested
    functions; the looser end_line occasionally touches the next header line,
    so the latest start wins). True ambiguity is reported, never guessed."""
    candidates = [
        i for i in identities if i.start_line < line <= i.end_line
    ]
    if not candidates:
        return None, False
    if len(candidates) > 1:
        starts = {i.start_line for i in candidates}
        if len(starts) > 1:
            latest = max(candidates, key=lambda i: i.start_line)
            return latest.function_id, False
        return None, True
    return candidates[0].function_id, False


def build_probe_manifest(
    gd_source: str, identities: list[FunctionIdentity]
) -> tuple[list[dict], list[int]]:
    """probe_id → {line, function_id} rows plus the probe_id→line manifest
    (same ordering as the coverage runner's manifest)."""
    executable_lines = _collect_executable_lines(gd_source)
    manifest = [entry.line for entry in executable_lines]
    rows: list[dict] = []
    for probe_id, line in enumerate(manifest):
        function_id, ambiguous = _owner(identities, line)
        rows.append(
            {
                "probe_id": probe_id,
                "line": line,
                "function_id": function_id,
                "owner_ambiguous": ambiguous,
            }
        )
    return rows, manifest


def _has_lambda(tree: Tree) -> bool:
    return any(sub.data == "lambda" for sub in tree.iter_subtrees())


def _has_inner_class(tree: Tree) -> bool:
    return any(sub.data == "class_def" for sub in tree.iter_subtrees())


def _uses_sanitized_projection(gd_source: str) -> bool:
    from gdtoolkit.gd2py import convert_code

    try:
        python_code = convert_code(gd_source)
    except Exception:
        return False
    return sanitize_python_ir_identifiers(python_code) != python_code


GAP_LANE_LAMBDA = "lambda_decision_gap"
GAP_LANE_INNER = "partial_instrumentation"
GAP_LANE_VAR_DEF = "sanitized_ir_projection"


def file_profile(gd_source: str, file_label: str) -> dict:
    tree = gdtoolkit_parser.parse(gd_source, gather_metadata=True)
    identities = collect_function_identities(gd_source, file_label)
    rows, manifest = build_probe_manifest(gd_source, identities)
    t_set = t_census(identities)
    i_set = {row["line"] for row in rows}
    has_lambda = _has_lambda(tree)
    has_inner = _has_inner_class(tree)
    t_equal_i = t_set == i_set
    owners_complete = all(
        row["function_id"] is not None and not row["owner_ambiguous"] for row in rows
    )

    gap_lane = None
    if has_lambda:
        gap_lane = GAP_LANE_LAMBDA
    elif has_inner:
        gap_lane = GAP_LANE_INNER
    elif _uses_sanitized_projection(gd_source):
        gap_lane = GAP_LANE_VAR_DEF

    eligible = (
        gap_lane is None
        and bool(identities)
        and t_equal_i
        and owners_complete
    )
    return {
        "file": file_label,
        "functions": [
            {
                "function_id": i.function_id,
                "name": i.name,
                "lex_path": i.lex_path,
                "start_line": i.start_line,
                "end_line": i.end_line,
                "cc": i.cc,
                "kind": i.kind,
                "body_hash": i.body_hash,
            }
            for i in identities
        ],
        "probes": rows,
        "manifest": manifest,
        "t_total": len(t_set),
        "i_total": len(i_set),
        "t_equal_i": t_equal_i,
        "owners_complete": owners_complete,
        "has_lambda": has_lambda,
        "has_inner_class": has_inner,
        "eligible": eligible,
        "gap_lane": gap_lane,
    }
