"""probe-manifest/2: build-time fid materialization and per-callable T/I (§SPEC-018).

Ownership is resolved once, here, from the registry spans (innermost span
wins — a lambda span is strictly inside its host span, so lambda body lines
belong to the lambda entry). Consumers see only fids; nothing downstream
re-infers ownership from line ranges. T-side per-entry counts are derived
from the registry spans too, with the host census pruning lambda subtrees
(the lambda body belongs to the lambda entry). Inner-class entries are
pruned by the instrumenter, so their census rows land in `census_gaps` and
the entry itself carries `state_hint=partial_instrumentation` — sibling
entries in the same file are never poisoned.
"""

from __future__ import annotations

from gdtoolkit.parser import parser as gdtoolkit_parser
from lark import Tree

from ..coverage.runner import (
    _EXECUTABLE_NODE_NAMES,
    _collect_executable_lines,
    _node_line,
    _paren_depths,
)

MANIFEST_VERSION = "probe-manifest/2"


def _statement_lines(
    root: Tree, depths: list[int], skip_subtrees: tuple[str, ...] = ()
) -> tuple[int, ...]:
    """Statement census per entry: executable nodes at paren-depth 0 under
    `root`. Hosts pass skip_subtrees=("lambda",) — the lambda body belongs to
    the lambda entry; lambda entries traverse their own subtree directly."""
    found: list[int] = []
    stack = [root]
    while stack:
        current = stack.pop()
        if current.data in skip_subtrees:
            continue
        if current.data in _EXECUTABLE_NODE_NAMES:
            line = _node_line(current)
            if line and line <= len(depths) and depths[line - 1] == 0:
                found.append(line)
        stack.extend(
            c for c in getattr(current, "children", []) if isinstance(c, Tree)
        )
    return tuple(sorted(set(found)))


def _reparse_source(gd_source: str) -> tuple[Tree, list[int]]:
    tree = gdtoolkit_parser.parse(gd_source, gather_metadata=True)
    return tree, _paren_depths(gd_source)


def _node_by_span(tree: Tree, start: int) -> Tree | None:
    """Locate the AST node whose func_def/lambda meta.line == start (spans in
    the registry were derived from these nodes)."""
    for sub in tree.iter_subtrees():
        if sub.data in ("func_def", "lambda"):
            if int(getattr(sub.meta, "line", 0) or 0) == start:
                return sub
    return None


def _resolve_ownership(
    spans: list[tuple], plan_lines: list[int],
) -> tuple[list[dict], int, int, dict[str, int]]:
    probe_plan: list[dict] = []
    unowned = ambiguous = 0
    i_by_fid: dict[str, int] = {}
    for probe_id, line in enumerate(plan_lines):
        hits = [s for s in spans if s[0] < line <= s[1]]
        ownership = "owned"
        if not hits:
            unowned += 1
            ownership = "unowned"
            fid, kind = None, None
        else:
            # spans are pre-sorted innermost-first; lambda⊂host nesting is the
            # expected shape, so ambiguity only exists when the two innermost
            # spans are *identical* (same start and end, different fid)
            innermost = hits[0]
            if len(hits) > 1 and (hits[1][0], hits[1][1]) == (
                innermost[0],
                innermost[1],
            ):
                ambiguous += 1
                ownership = "ambiguous"
                fid, kind = None, None
            else:
                fid, kind = innermost[2], innermost[3]
                i_by_fid[fid] = i_by_fid.get(fid, 0) + 1
        probe_plan.append(
            {
                "probe_id": probe_id,
                "line": line,
                "fid": fid,
                # contract naming: func_def → func, lambda → lambda
                "owner_kind": "func" if kind == "func_def" else kind,
                # §SPEC-024: per-line ownership flag for consumers that must
                # distinguish unowned vs ambiguous without re-deriving spans
                "ownership": ownership,
            }
        )
    return probe_plan, unowned, ambiguous, i_by_fid


def _gap_rows(entry: dict, t_ids: tuple[int, ...]) -> list[dict]:
    return [
        {
            "line": line,
            "fid": entry["fid"],
            "owner_kind": "inner_class_method",
            "reason": "class_def_pruned",
            "expected": "not_instrumented",
        }
        for line in t_ids
    ]


def _partial_row(entry: dict, t_count: int) -> dict:
    return {
        "fid": entry["fid"],
        "t_lines": t_count,
        "i_lines": 0,
        "t_equal_i": False,
        "state_hint": "partial_instrumentation",
    }


def _measured_row(entry: dict, t_count: int, i_lines: int,
                  t_ids: tuple[int, ...]) -> dict:
    return {
        "fid": entry["fid"],
        "t_lines": t_count,
        "i_lines": i_lines,
        "t_equal_i": t_count == i_lines,
        "t_line_ids": list(t_ids),
    }


def build_probe_manifest_v2(gd_source: str, registry: dict) -> dict:
    """Assemble the `probe-manifest/2` contract for one file.

    Entries with `instrumentable=False` (inner-class methods, pruned by the
    instrumenter) contribute `census_gaps` rows instead of probe lines; every
    other entry keeps its own T/I decomposition and stays fully usable.
    """
    tree, depths = _reparse_source(gd_source)
    executable = _collect_executable_lines(gd_source)
    plan_lines = [entry.line for entry in executable]

    instrumentable_entries = [
        e for e in registry["entries"] if e["instrumentable"]
    ]
    # innermost span wins: (start, end] containment, smallest end then latest
    # start resolves the lambda-inside-host nesting without any guesswork
    spans = sorted(
        ((e["start_line"], e["end_line"], e["fid"], e["kind"])
         for e in instrumentable_entries),
        key=lambda s: (s[1] - s[0], -s[0]),
    )

    probe_plan, unowned, ambiguous, i_by_fid = _resolve_ownership(spans, plan_lines)

    census_gaps: list[dict] = []
    per_function: list[dict] = []
    for entry in registry["entries"]:
        ast_node = _node_by_span(tree, entry["start_line"])
        t_ids = (
            _statement_lines(
                ast_node,
                depths,
                skip_subtrees=("lambda",) if entry["kind"] != "lambda" else (),
            )
            if ast_node is not None
            else ()
        )
        if not entry["instrumentable"]:
            census_gaps.extend(_gap_rows(entry, t_ids))
            per_function.append(_partial_row(entry, len(t_ids)))
            continue
        i_lines = i_by_fid.get(entry["fid"], 0)
        per_function.append(_measured_row(entry, len(t_ids), i_lines, t_ids))

    return {
        "manifest_version": MANIFEST_VERSION,
        "file": registry["file"],
        "probe_plan": probe_plan,
        "census_gaps": census_gaps,
        "per_function": per_function,
        "census": {
            "probes_total": len(plan_lines),
            "unowned": unowned,
            "ambiguous": ambiguous,
            "owners_complete": unowned == 0 and ambiguous == 0,
        },
    }
