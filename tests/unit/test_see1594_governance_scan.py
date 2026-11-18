"""SEE-1594 §SPEC-027/S7：治理扫描器最小反例（扫描面命中 / allowlist
生效 / stale allowlist / 单点违例 / 消费链断言）。

扫描器本体 = tests/integration/check_see1594_governance.py（独立 stdlib
实现，此处以合成树直接驱动其函数）。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

_SCANNER = (Path(__file__).resolve().parents[1] / "integration"
            / "check_see1594_governance.py")


def _load_scanner():
    spec = importlib.util.spec_from_file_location("see1594_governance",
                                                  _SCANNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "tests" / "unit").mkdir(parents=True)
    (root / "src" / "pkg" / "mod.py").write_text(
        "def apply_coverage_prefilter():\n    pass\n")
    return root


class TestRetiredScan:
    def test_hit_detected(self, tree):
        mod = _load_scanner()
        (tree / "src" / "pkg" / "legacy.py").write_text(
            "from x import derive_tests_glob  # 回潮\n")
        mod.ALLOWLIST["derive_tests_glob"] = []
        violations, allowlisted = mod.scan_retired(tree)
        assert any(v["pattern"] == "derive_tests_glob"
                   and "legacy.py" in v["file"] for v in violations)

    def test_allowlist_named_entry_absorbs_hit(self, tree):
        mod = _load_scanner()
        (tree / "src" / "pkg" / "legacy.py").write_text(
            "from x import derive_tests_glob  # 退役声明注释\n")
        mod.ALLOWLIST["derive_tests_glob"] = [
            ("src/pkg/legacy.py", "退役声明注释（具名豁免）")]
        violations, allowlisted = mod.scan_retired(tree)
        assert not [v for v in violations
                    if v.get("pattern") == "derive_tests_glob"]
        assert any(a["pattern"] == "derive_tests_glob" for a in allowlisted)

    def test_stale_allowlist_is_violation(self, tree):
        """allowlist 条目零命中 = 养死条目 → 自身违规（不许全局豁免）。"""
        mod = _load_scanner()
        mod.ALLOWLIST["structural_ownership"] = [
            ("src/pkg/never_written.py", "不存在的豁免")]
        violations, _ = mod.scan_retired(tree)
        assert any(v.get("kind") == "stale_allowlist"
                   and v["pattern"] == "structural_ownership"
                   and "never_written.py" in v["entry"] for v in violations)

    def test_clean_tree_ok(self, tree):
        mod = _load_scanner()
        for p in mod.ALLOWLIST:
            mod.ALLOWLIST[p] = []
        violations, allowlisted = mod.scan_retired(tree)
        assert violations == []
        assert allowlisted == []


class TestSinglePointScan:
    def test_second_definition_is_violation(self, tree):
        mod = _load_scanner()
        (tree / "src" / "pkg" / "copy.py").write_text(
            "def apply_coverage_prefilter():\n    pass\n")
        violations = mod.scan_single_point(tree)
        assert any(v["symbol"] == "apply_coverage_prefilter"
                   and len(v["sites"]) == 2 for v in violations)

    def test_single_definition_expected_file_ok(self, tree):
        mod = _load_scanner()
        # mod.py 已含唯一定义；期望文件子串设为实际所在
        mod.SINGLE_POINT_DEFS = {"apply_coverage_prefilter": "pkg/mod.py"}
        assert mod.scan_single_point(tree) == []


class TestDualTruthScan:
    def test_consumer_chain_anchor_reference_ok(self, tree):
        mod = _load_scanner()
        (tree / "src" / "pkg" / "cli.py").write_text(
            "def cmd_measure_coverage(args):\n"
            "    from x import run_coverage\n"
            "    return run_coverage(args)\n")
        mod.CONSUMER_CHAIN = {"measure_coverage": [
            ("src/pkg/cli.py", "cmd_measure_coverage", "run_coverage")]}
        evidence, violations = mod.scan_dual_truth(tree)
        assert violations == []
        assert evidence["consumer_chain"][0]["line"] == 2

    def test_missing_reference_is_violation(self, tree):
        mod = _load_scanner()
        (tree / "src" / "pkg" / "cli.py").write_text(
            "def cmd_measure_coverage(args):\n"
            "    return legacy_direct_answer(args)\n")
        mod.CONSUMER_CHAIN = {"measure_coverage": [
            ("src/pkg/cli.py", "cmd_measure_coverage", "run_coverage")]}
        _evidence, violations = mod.scan_dual_truth(tree)
        assert any("does not reference" in v["error"] for v in violations)
