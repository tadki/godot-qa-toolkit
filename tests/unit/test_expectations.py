"""Unit tests for gherkin expectations DSL (SEE-1317 §SPEC-014, TDD)."""

from __future__ import annotations

import pytest

from godot_qa_toolkit.gherkin.expectations import (
    assert_between,
    assert_compare,
    assert_in,
    assert_length,
    assert_not_in,
    assert_signal_payload_field,
)
from godot_qa_toolkit.gherkin.runner import StepFailure


class TestAssertCompare:
    def test_numeric_gt_pass(self):
        assert_compare(5.0, ">", 3.0)

    def test_numeric_gte_pass(self):
        assert_compare(3.0, ">=", 3.0)

    def test_numeric_lt_fail(self):
        with pytest.raises(StepFailure) as excinfo:
            assert_compare(5.0, "<", 3.0, label="timer")
        assert "timer" in str(excinfo.value.detail or excinfo.value.reason)

    def test_numeric_eq(self):
        assert_compare(2, "==", 2)

    def test_numeric_ne(self):
        assert_compare(2, "!=", 3)

    def test_string_number_coercion(self):
        assert_compare("5.0", ">=", 5.0)

    def test_string_equality(self):
        assert_compare("general", "==", "general")

    def test_unknown_op_is_caller_error(self):
        with pytest.raises(ValueError):
            assert_compare(1, "in", 1)


class TestAssertMembership:
    def test_in_pass(self):
        assert_in("general", ["general", "reflection", "component", "special"])

    def test_in_fail(self):
        with pytest.raises(StepFailure):
            assert_in("mystery", ["general", "special"])

    def test_not_in_pass(self):
        assert_not_in("mystery", ["general"])

    def test_not_in_fail(self):
        with pytest.raises(StepFailure):
            assert_not_in("general", ["general", "special"])


class TestAssertBetween:
    def test_inclusive_edges(self):
        assert_between(0.0, 0.0, 10.0)
        assert_between(10.0, 0.0, 10.0)

    def test_exclusive_interior(self):
        assert_between(5.0, 0.0, 10.0, inclusive=False)

    def test_out_of_range(self):
        with pytest.raises(StepFailure):
            assert_between(11.0, 0.0, 10.0)

    def test_non_numeric(self):
        with pytest.raises(StepFailure):
            assert_between("abc", 0.0, 10.0)


class TestAssertLength:
    def test_list(self):
        assert_length([1, 2, 3], 3)

    def test_dict(self):
        assert_length({"a": 1, "b": 2}, 2)

    def test_mismatch(self):
        with pytest.raises(StepFailure):
            assert_length([1], 2)

    def test_no_len(self):
        with pytest.raises(StepFailure):
            assert_length(42, 1)


class TestAssertSignalPayloadField:
    def test_pass(self):
        assert_signal_payload_field({"reason": "insufficient_funds"}, "reason", "insufficient_funds")

    def test_field_missing(self):
        with pytest.raises(StepFailure) as excinfo:
            assert_signal_payload_field({"a": 1}, "reason", "x")
        assert "absent" in str(excinfo.value.detail or "")

    def test_field_mismatch(self):
        with pytest.raises(StepFailure):
            assert_signal_payload_field({"reason": "vendor_unavailable"}, "reason", "insufficient_funds")

    def test_non_dict(self):
        with pytest.raises(StepFailure):
            assert_signal_payload_field([1, 2], "reason", "x")
