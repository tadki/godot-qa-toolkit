"""Token-level identifier sanitization for the complexity collector (WP1a).

gd2py converts GDScript to Python one identifier at a time, so an identifier
named `def` is emitted verbatim as a Python assignment target (`def = ...`)
and radon's compile() rejects the whole file. We sanitize *before* conversion,
on the GDScript source, because GDScript has no `def` keyword: every `def`
token there is an identifier position, while the Python `def foo():` keyword
positions are produced by gd2py itself and never pass through this module.
Strings, comments and control flow are copied through untouched; the sanitized
text is an in-memory measurement projection only — no GDScript source on disk
is ever modified.

Narrow scope by spec (§SPEC-001): only the keyword `def` is sanitized. Other
Python-keyword collisions (e.g. `var lambda`) remain structural failures and
stay in the unparseable lane.
"""

from __future__ import annotations

_QUOTES = "\"'"
_TRIPLES = ("\"\"\"", "'''")


def _is_ident_start(c: str) -> bool:
    return c.isalpha() or c == "_"


def _is_ident_char(c: str) -> bool:
    return c.isalnum() or c == "_"


def _scan_ident(src: str, start: int) -> tuple[str, int]:
    i = start
    n = len(src)
    while i < n and _is_ident_char(src[i]):
        i += 1
    return src[start:i], i


def _scan_string(src: str, start: int) -> int:
    """Return the index just past the string literal opening at src[start].

    Handles ''' / \"\"\" triple quotes, backslash escapes, and the raw-string
    prefix (an identifier `r`/`R` immediately followed by a quote), where the
    backslash escapes nothing — treating it as an escape would run past the
    closing quote and misparse the rest of the file.
    """
    quote = src[start]
    n = len(src)
    raw = start > 0 and src[start - 1] in "rR"
    if src[start : start + 3] in _TRIPLES:
        end = src.find(quote * 3, start + 3)
        return n if end == -1 else end + 3
    i = start + 1
    while i < n:
        c = src[i]
        if c == "\\" and not raw:
            i += 2
            continue
        if c == quote:
            return i + 1
        i += 1
    return n


def _iter_identifier_spans(src: str) -> list[tuple[int, int, str]]:
    """Collect (start, end, name) spans of identifier tokens, skipping
    comments and string literals so their `def`s stay verbatim."""
    spans: list[tuple[int, int, str]] = []
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        if c == "#":
            nl = src.find("\n", i)
            i = n if nl == -1 else nl
        elif c in _QUOTES:
            i = _scan_string(src, i)
        elif _is_ident_start(c):
            name, end = _scan_ident(src, i)
            if name not in ("r", "R") or end >= n or src[end] not in _QUOTES:
                spans.append((i, end, name))
            i = end
        else:
            i += 1
    return spans


def _pick_replacement(existing: set[str], keyword: str) -> str:
    candidate = keyword + "_"
    while candidate in existing:
        candidate += "_"
    return candidate


def sanitize_python_keyword_identifiers(src: str, keyword: str = "def") -> str:
    """Rename identifier-position `keyword` tokens to a collision-free name.

    Two passes: collect identifiers first so the replacement name cannot
    collide with anything already present; then rewrite only the keyword
    occurrences. Line structure is untouched, so line numbers stay valid.
    """
    spans = _iter_identifier_spans(src)
    existing = {name for _, _, name in spans}
    if keyword not in existing:
        return src
    replacement = _pick_replacement(existing, keyword)
    pieces: list[str] = []
    last = 0
    for start, end, name in spans:
        if name != keyword:
            continue
        pieces.append(src[last:start])
        pieces.append(replacement)
        last = end
    pieces.append(src[last:])
    return "".join(pieces)
