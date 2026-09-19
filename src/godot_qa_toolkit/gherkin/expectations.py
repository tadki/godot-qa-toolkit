"""Expectation predicate helpers for step-defs (SEE-1317 §SPEC-014).

Extends the gherkin runner's verdict layer with the comparison and
membership predicates the vendor feature demands (arrival-timer sign,
vendor-type membership, numeric range, signal payload discrimination).
The existing ``_kol_common.expect_value`` covers only ``==``/approximate
equality; encoding "arrival_timer > 0" or "vendor_type in [...]" in a
scenario would otherwise force a bespoke closure per step and scatter
the same pattern across domain modules.

Contract preserved: every predicate raises ``StepFailure`` on mismatch
and returns ``None`` on pass — exactly the discipline the runner already
consumes. Pure predicates (no I/O, no globals) so step-defs stay read-only
consumers of the probe outcome.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .runner import StepFailure


def assert_compare(actual: Any, op: str, expected: Any, *, label: str = "value") -> None:
    """Raise StepFailure unless ``actual <op> expected``.

    Supported ops: ``<``, ``<=``, ``==``, ``!=``, ``>=``, ``>``. Numeric
    coercion is applied to both operands when both are int/float/str-that-
    parses-as-float; otherwise a plain Python comparison is used (works for
    strings/bools). An unknown op is a caller error, not a verdict.
    """
    a, e = actual, expected
    if isinstance(a, str) and isinstance(e, (int, float)):
        try:
            a = float(a)
        except ValueError:
            pass
    if isinstance(e, str) and isinstance(a, (int, float)):
        try:
            e = float(e)
        except ValueError:
            pass
    ops = {
        "<": lambda: a < e,
        "<=": lambda: a <= e,
        "==": lambda: a == e,
        "!=": lambda: a != e,
        ">=": lambda: a >= e,
        ">": lambda: a > e,
    }
    if op not in ops:
        raise ValueError(f"assert_compare: unknown op {op!r}")
    if not ops[op]():
        raise StepFailure(
            reason=f"{label} comparison failed",
            detail=f"expected {label} {op} {expected!r}, got {actual!r}",
        )


def assert_in(actual: Any, members: Iterable[Any], *, label: str = "value") -> None:
    """Raise StepFailure unless ``actual`` is a member of ``members``."""
    member_list = list(members)
    if actual not in member_list:
        raise StepFailure(
            reason=f"{label} membership failed",
            detail=f"expected {label} in {member_list!r}, got {actual!r}",
        )


def assert_not_in(actual: Any, members: Iterable[Any], *, label: str = "value") -> None:
    """Raise StepFailure if ``actual`` is a member of ``members``."""
    member_list = list(members)
    if actual in member_list:
        raise StepFailure(
            reason=f"{label} exclusion failed",
            detail=f"expected {label} not in {member_list!r}, got {actual!r}",
        )


def assert_between(
    actual: Any, lo: float, hi: float, *, label: str = "value", inclusive: bool = True
) -> None:
    """Raise StepFailure unless ``lo <= actual <= hi`` (or strict when not inclusive)."""
    try:
        a = float(actual)
    except (TypeError, ValueError) as exc:
        raise StepFailure(
            reason=f"{label} not numeric",
            detail=f"cannot compare {actual!r} to [{lo!r}, {hi!r}]",
        ) from exc
    if inclusive:
        ok = lo <= a <= hi
        op_lo, op_hi = "<=", "<="
    else:
        ok = lo < a < hi
        op_lo, op_hi = "<", "<"
    if not ok:
        raise StepFailure(
            reason=f"{label} out of range",
            detail=f"expected {lo!r} {op_lo} {label} {op_hi} {hi!r}, got {actual!r}",
        )


def assert_length(actual: Any, expected: int, *, label: str = "collection") -> None:
    """Raise StepFailure unless ``len(actual) == expected``."""
    try:
        n = len(actual)
    except TypeError as exc:
        raise StepFailure(
            reason=f"{label} has no length",
            detail=f"cannot take len() of {type(actual).__name__}",
        ) from exc
    if n != expected:
        raise StepFailure(
            reason=f"{label} length mismatch",
            detail=f"expected len({label}) == {expected}, got {n}",
        )


def assert_signal_payload_field(
    payload: Any, field: str, expected: Any, *, label: str = "signal payload"
) -> None:
    """Raise StepFailure unless ``payload[field] == expected``.

    The vendor domain needs to discriminate ``vendor_purchase_failed``
    reasons (``insufficient_funds`` / ``vendor_unavailable`` / ...); a
    count-only ``expect_signal`` cannot. Payload shape is a dict produced
    by the probe bridge; missing key is a mismatch, not a crash.
    """
    if not isinstance(payload, dict):
        raise StepFailure(
            reason=f"{label} is not a dict",
            detail=f"got {type(payload).__name__} for {label}",
        )
    if field not in payload:
        raise StepFailure(
            reason=f"{label} missing field",
            detail=f"field {field!r} absent from {payload!r}",
        )
    actual = payload[field]
    if actual != expected:
        raise StepFailure(
            reason=f"{label} field mismatch",
            detail=f"expected {label}.{field} == {expected!r}, got {actual!r}",
        )
