"""Neutral GDScript AST helpers shared across layers (SEE-1546 WP2 PR-B).

`max_leaf_line` was extracted from mutation/scopemanifest.py so registry /
coverage consumers stop importing a mutation-private symbol. Semantics are
unchanged: container nodes' meta.end_line overflows to the file tail, so only
leaf-token lines and data-node start lines are trusted.
"""

from __future__ import annotations


def max_leaf_line(node) -> int:
    """Max leaf-token line in the subtree — the only trustworthy span end.

    gdtoolkit container nodes (func_def/class_def/root) report a meta.end_line
    that overflows to the file end (measured: a 4-line func reports 6) — a
    container's end is never trusted; only leaf-token lines and data-node
    start lines are usable.
    """
    best = 0
    meta = getattr(node, "meta", None)
    if meta is not None and not meta.empty:
        best = max(best, getattr(meta, "line", 0) or 0)
    for child in getattr(node, "children", []):
        if hasattr(child, "data"):
            best = max(best, max_leaf_line(child))
        else:
            best = max(best, getattr(child, "line", 0) or 0)
    return best
