"""CRAP-Line formula (§SPEC-007).

`score = cc² × (1−c)³ + cc` with c the raw 0–1 statement-start-line coverage
ratio. No pre-rounded percentages: a rounded 100% would silently zero the
penalty term. No coverage (None) means the file has no measurable denominator
(vacuous) — that never yields a score.
"""

from __future__ import annotations

CRAP_LINE_VERSION = "crap-line/1"


def crap_line_score(cc: int, coverage: float | None) -> float | None:
    if coverage is None:
        return None
    c = min(1.0, max(0.0, coverage))
    return cc * cc * (1.0 - c) ** 3 + cc
