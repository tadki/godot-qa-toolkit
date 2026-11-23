"""SEE-1623 M8 — 失败对身份重复计数行为钉。

根因（SEE-1622 Refacty BUG-3）：同一失败测试以空 script 回显与全路径
归因两种形态各计一次——1 个红测报 `baseline_count=2`，meta.pairs 双写，
失败计数/门禁证据翻倍。

行为契约（canonical_failure_pairs 单一出处）：双形态并存只保留归因
形态；仅存裸名形态（Run Summary 截断）原样保留；基线计数与 pairs
披露按失败身份唯一。
"""

from __future__ import annotations

from godot_qa_toolkit.measurement.orchestrator import (
    _parse_baseline_failures,
    canonical_failure_pairs,
    _parse_failing_pairs,
)

_BOTH_FORMS = (
    "Godot Engine v4.4.1\n"
    "res://tests/unit/test_red.gd\n"
    "  * test_red_always_fails\n"
    "\t\ttest_red_always_fails [Failed]\n"
    "= Run Summary\n"
    "res://tests/unit/test_red.gd\n"
    "- test_red_always_fails\n"
    "    [Failed]: boom\n"
)


class TestCanonicalFailurePairs:
    def test_dual_form_collapses_to_attributed(self):
        pairs = {("res://tests/unit/test_red.gd", "test_red_always_fails"),
                 ("", "test_red_always_fails")}
        assert canonical_failure_pairs(pairs) == {
            ("res://tests/unit/test_red.gd", "test_red_always_fails")}

    def test_bare_only_form_preserved(self):
        pairs = {("", "test_unattributed")}
        assert canonical_failure_pairs(pairs) == {("", "test_unattributed")}

    def test_distinct_names_both_kept(self):
        pairs = {("", "test_a"), ("res://t/x.gd", "test_b")}
        assert canonical_failure_pairs(pairs) == pairs

    def test_parse_failing_pairs_sees_both_forms(self):
        """采集面保持双形态（门控判定面不变）——规范化只在计数/披露边界。"""
        parsed = _parse_failing_pairs(_BOTH_FORMS)
        assert parsed == {("res://tests/unit/test_red.gd",
                           "test_red_always_fails"),
                          ("", "test_red_always_fails")}


class TestBaselineIdentityCount:
    def test_explicit_baseline_dual_form_counted_once(self):
        pairs = _parse_baseline_failures({
            ("res://tests/unit/test_red.gd", "test_red_always_fails"),
            ("", "test_red_always_fails")})
        assert len(pairs) == 1
