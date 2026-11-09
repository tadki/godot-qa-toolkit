"""Token-level identifier sanitization on the CONVERTED Python IR (WP1a).

gd2py converts GDScript to Python one identifier at a time, so an identifier
named `def` is emitted verbatim as a Python assignment target (`def = ...`)
and radon's compile() rejects the whole file. Per §SPEC-001 the sanitization
happens on gd2py's *output* — the Python IR — not on the GDScript source:
Python's tokenizer already isolates string/comment tokens, and the keyword
vs identifier ambiguity that motivated source-side scanning does not exist
here because the Python grammar admits exactly one shape for a definition
head (`def` immediately followed by a NAME) and nothing else in an identifier
position. The sanitized text is an in-memory measurement projection only —
neither the GDScript source nor the IR is ever written back to disk.

Narrow scope by spec: only the keyword `def` is sanitized. Other
Python-keyword collisions (e.g. `var lambda` emitted as `lambda = 1`) remain
structural failures and stay in the unparseable lane.
"""

from __future__ import annotations

import io
import token as _token
import tokenize

# Tokens that can sit between a NAME and the token that reveals its role
# (e.g. line continuations, blank lines) carry no position semantics.
_ROLE_OPAQUE = (
    _token.NL,
    _token.NEWLINE,
    _token.COMMENT,
    _token.INDENT,
    _token.DEDENT,
    _token.ENDMARKER,
)


def _pick_replacement(names: set[str], keyword: str) -> str:
    candidate = keyword + "_"
    while candidate in names:
        candidate += "_"
    return candidate


def sanitize_python_ir_identifiers(python_code: str, keyword: str = "def") -> str:
    """Rename identifier-position `keyword` tokens in Python source.

    A `def` NAME token is a definition-head keyword exactly when the next
    role-bearing token is another NAME (`def foo(`); every other follower
    (`=`, `.`, `)`, `:`, end of line, ...) is an identifier position. Line
    structure is untouched, so radon line numbers stay valid.
    """
    tokens = list(tokenize.generate_tokens(io.StringIO(python_code).readline))
    names = {t.string for t in tokens if t.type == _token.NAME}
    if keyword not in names:
        return python_code
    replacement = _pick_replacement(names, keyword)

    spans: list[tuple[int, int]] = []
    for index, tok in enumerate(tokens):
        if tok.type != _token.NAME or tok.string != keyword:
            continue
        nxt = next(
            (t for t in tokens[index + 1 :] if t.type not in _ROLE_OPAQUE), None
        )
        if nxt is not None and nxt.type == _token.NAME:
            continue  # `def foo(` definition head — keyword position
        spans.append(tok.start)

    if not spans:
        return python_code
    lines = python_code.splitlines(keepends=True)
    for row, col in spans:
        line = lines[row - 1]
        if line[col : col + len(keyword)] != keyword:
            # tokenize positions are authoritative; a mismatch means the
            # input was mutated between tokenize and rewrite — fail loudly.
            raise ValueError(
                f"sanitizer rewrite missed '{keyword}' at line {row} col {col}: "
                f"{line!r}"
            )
        lines[row - 1] = line[:col] + replacement + line[col + len(keyword) :]
    return "".join(lines)
