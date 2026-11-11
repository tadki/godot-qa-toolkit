"""CRAP-Line formula (§SPEC-007).

`score = cc² × (1−c)³ + cc` with c the raw 0–1 statement-start-line coverage
ratio. No pre-rounded percentages: a rounded 100% would silently zero the
penalty term. No coverage (None) means the file has no measurable denominator
(vacuous) — that never yields a score.
"""

from __future__ import annotations

CRAP_LINE_VERSION = "crap-line/1"


def crap_line_score(cc: int, coverage: float | None) -> float | None:
    """§SPEC-053：非法覆盖率（越界/NaN）必须显式拒绝，不得静默 clamp——
    clamp 会把坏的测量伪装成合法分数。"""
    if coverage is None:
        return None
    c = float(coverage)
    if c != c or c < 0.0 or c > 1.0:  # NaN 或越界
        raise ValueError(
            f"illegal coverage ratio {coverage!r} (expected 0.0–1.0, "
            f"cc={cc})")
    return cc * cc * (1.0 - c) ** 3 + cc
