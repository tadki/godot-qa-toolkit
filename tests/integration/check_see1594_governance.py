#!/usr/bin/env python3
"""SEE-1594 §SPEC-027 治理扫描（--mode semantics）：退役零残留 + 单点实现
+ 消费链单真源的结构化扫描。

- 退役符号/路径命中（src/docs/tests/.github/scripts 文本面）→ 非零退出
  并逐条列出位置；**allowlist 显式具名**（pattern + 文件子串 + 理由），
  每条 allowlist 必须实际命中至少一处（stale allowlist 同样报错），
  不存在全局豁免。
- 单点实现：prefilter 应用/摘要、基线解析/懒获取、tests-face 指纹的
  def 站点全局唯一。
- 消费链（§SPEC-025）：measure coverage / measure risk(risk-shadow) /
  mutation prefilter 三消费面全部经 /6 map 单真源（结构引用断言，
  file:line 证据随 JSON 落盘）。

签名：
    python tests/integration/check_see1594_governance.py \
        --mode semantics --out <json>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# 文本扫描面（相对仓库根；目录 = 递归，文件 = 单点）
SCAN_DIRS = ("src", "docs", "tests")
SCAN_FILES = (".github/workflows", "scripts")

# 退役符号/路径 pattern：命中即违规，除非命中 allowlist（具名条目）。
RETIRED_PATTERNS = {
    "flag_resume": r"--resume\b",
    "flag_reuse_coverage": r"--reuse-coverage",
    "derive_tests_glob": r"derive_tests_glob",
    "strength_glob": r"_strength_glob",
    "index_cache": r"_INDEX_CACHE",
    "structural_ownership": r"StructuralOwnership",
    "test_ownership_index": r"TestOwnershipIndex",
    "test_func_pattern": r"_TEST_FUNC_PATTERN",
    "disclose_v1_superseded": r"disclose_v1_superseded",
    "disclose_superseded": r"disclose_superseded",
    "coverage_store_v1": r"see1420-coverage/1",
    "sink_recovery_source": r"sink[- ]recovery|recovered sink|sink 复原",
}

# allowlist：显式具名（pattern → (文件子串, 理由)）。每条必须实际命中，
# 否则按 stale-allowlist 报错——不许全局豁免、不许养死条目。
# 扫描器自身（pattern 定义/allowlist 表/最小测试载体）逐 pattern 显式
# 具名豁免——扫描器文本必然包含被扫字样，属定义而非残留。
ALLOWLIST = {
    "flag_resume": [
        ("docs/contract.md", "历史台账/退役声明（A13 --resume retired 标注）"),
        ("docs/runbook/mutation.md", "退役标注行（retired SEE-1594 A13）"),
        ("docs/mutation-architecture.md",
         "退役标注（防 --resume 误复用的毒条目防护描述）"),
        ("src/godot_qa_toolkit/mutation/diffengine.py",
         "历史台账注释（v1 --resume 既有语义溯源）"),
        ("tests/unit/test_see1594_semantic_retirement.py",
         "退役防复发 pin"),
        ("tests/integration/check_see1594_governance.py",
         "扫描器自身的 pattern 定义与最小测试"),
        ("tests/unit/test_argv_samesource_see1405.py",
         "历史台账（SEE-1400 毒条目叙事，退役前行为背景）"),
        ("tests/unit/test_diff_default_budget_retry_see1420.py",
         "历史台账注释（差分默认开启语义描述）"),
    ],
    "flag_reuse_coverage": [
        ("docs/contract.md", "历史台账/退役声明（A13 retired 标注）"),
        ("docs/runbook/mutation.md", "退役标注行（retired SEE-1594 A13）"),
        ("docs/mutation-architecture.md",
         "退役标注（历史形态旗标已退役说明）"),
        ("src/godot_qa_toolkit/cli.py",
         "A13 退役说明 docstring（_reuse_coverage_flag 契约描述）"),
        ("tests/unit/test_coverage_prefilter_reuse_see1420.py",
         "历史台账 docstring（§SPEC-1420-12 定案标题引用旧旗标名）"),
        ("tests/unit/test_see1594_semantic_retirement.py",
         "退役防复发 pin"),
        ("tests/integration/check_see1594_governance.py",
         "扫描器自身的 pattern 定义与最小测试"),
    ],
    "derive_tests_glob": [
        ("src/godot_qa_toolkit/mutation/testscan.py",
         "A10 退役声明注释（静态选面链物理退役标注）"),
        ("tests/unit/test_see1594_semantic_retirement.py",
         "退役防复发 pin"),
        ("tests/integration/check_see1594_governance.py",
         "扫描器自身的 pattern 定义与最小测试"),
    ],
    "strength_glob": [
        ("src/godot_qa_toolkit/mutation/testscan.py", "A10 退役声明注释"),
        ("tests/unit/test_see1594_semantic_retirement.py",
         "退役防复发 pin"),
        ("tests/integration/check_see1594_governance.py",
         "扫描器自身的 pattern 定义与最小测试"),
    ],
    "index_cache": [
        ("src/godot_qa_toolkit/mutation/testscan.py", "A10 退役声明注释"),
        ("tests/unit/test_see1594_semantic_retirement.py",
         "退役防复发 pin"),
        ("tests/integration/check_see1594_governance.py",
         "扫描器自身的 pattern 定义与最小测试"),
    ],
    "structural_ownership": [
        ("tests/unit/test_see1594_semantic_retirement.py",
         "退役防复发 pin"),
        ("tests/integration/check_see1594_governance.py",
         "扫描器自身的 pattern 定义与最小测试"),
    ],
    "test_ownership_index": [
        ("tests/unit/test_see1594_semantic_retirement.py",
         "退役防复发 pin"),
        ("tests/integration/check_see1594_governance.py",
         "扫描器自身的 pattern 定义与最小测试"),
    ],
    "test_func_pattern": [
        ("tests/unit/test_see1594_semantic_retirement.py",
         "退役防复发 pin"),
        ("tests/integration/check_see1594_governance.py",
         "扫描器自身的 pattern 定义与最小测试"),
    ],
    "disclose_v1_superseded": [
        ("docs/contract.md", "历史台账（A14 退役声明：该披露面已删除）"),
        ("tests/unit/test_see1594_semantic_retirement.py",
         "退役防复发 pin"),
        ("tests/integration/check_see1594_governance.py",
         "扫描器自身的 pattern 定义与最小测试"),
    ],
    "disclose_superseded": [
        ("src/godot_qa_toolkit/mutation/ops.py",
         "ops 版本历史台账（当时的披露语义）"),
        ("tests/unit/test_see1550_collector_mdr.py",
         "历史台账注释（SEE-1550 当期语义）"),
        ("tests/unit/test_see1594_semantic_retirement.py",
         "退役防复发 pin"),
        ("tests/integration/check_see1594_governance.py",
         "扫描器自身的 pattern 定义与最小测试"),
    ],
    "coverage_store_v1": [
        ("src/godot_qa_toolkit/mutation/coverlink.py",
         "/1 拒绝守卫常量（盘上残留条目拒绝，零读写路径；A04）"),
        ("docs/contract.md", "历史台账（S2 /1 store 层退役声明）"),
        ("docs/runbook/measure.md", "退役声明（manual /1 store retired A04）"),
        ("docs/runbook/mutation.md",
         "退役声明（manual /1 store layer retired）"),
        ("tests/unit/test_mechanism_audit_see1587.py",
         "拒绝守卫常量 pin（assert SCHEMA_COVERAGE == /1）"),
        ("tests/unit/test_see1594_derived_cache_identity.py",
         "拒绝守卫反例（/1 条目必须被拒，非写入路径）"),
        ("tests/unit/test_see1594_semantic_retirement.py",
         "退役防复发 pin"),
        ("tests/integration/check_see1594_governance.py",
         "扫描器自身的 pattern 定义与最小测试"),
    ],
    "sink_recovery_source": [
        ("src/godot_qa_toolkit/coverage/runner.py",
         "sink-recovery 已退役声明注释（非覆盖来源）"),
        ("tests/unit/test_see1594_semantic_retirement.py",
         "退役防复发 pin"),
        ("tests/integration/check_see1594_governance.py",
         "扫描器自身的 pattern 定义与最小测试"),
    ],
}

# 单点实现（§SPEC-027 去重面）：符号 → 期望唯一 def 站点（文件子串）。
SINGLE_POINT_DEFS = {
    "apply_coverage_prefilter": "mutation/coverlink.py",
    "coverage_prefilter_summary": "mutation/coverlink.py",
    "_parse_failing_pairs": "measurement/orchestrator.py",
    "_acquire_lazy_baseline": "measurement/orchestrator.py",
    "current_tests_face": "mutation/coverlink.py",
    "_gutconfig_face_args": "measurement/orchestrator.py",
}

# 消费链断言（§SPEC-025）：文件 → 必须引用的符号（file:line 证据落 JSON）。
CONSUMER_CHAIN = {
    "measure_coverage": [
        ("src/godot_qa_toolkit/cli.py", "cmd_measure_coverage", "run_coverage"),
        ("src/godot_qa_toolkit/coverage/runner.py", "run_coverage",
         "project_file_coverage"),
    ],
    "measure_risk_shadow": [
        ("src/godot_qa_toolkit/risk/shadow.py", "default_measure",
         "run_coverage"),
    ],
    "mutation_prefilter": [
        ("src/godot_qa_toolkit/mutation/coverlink.py",
         "apply_coverage_prefilter", "resolve_coverage_for_prefilter"),
        ("src/godot_qa_toolkit/mutation/truth_select.py",
         "_current_tests_face", "current_tests_face"),
    ],
}


def _iter_scan_files(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    for d in SCAN_DIRS:
        base = repo_root / d
        if base.is_dir():
            files += [p for p in sorted(base.rglob("*"))
                      if p.is_file() and p.suffix in (".py", ".md", ".yml",
                                                      ".yaml", ".json",
                                                      ".sh")]
    for f in SCAN_FILES:
        base = repo_root / f
        if base.is_dir():
            files += [p for p in sorted(base.rglob("*")) if p.is_file()]
        elif base.is_file():
            files.append(base)
    return [p for p in files if "__pycache__" not in p.parts]


_SCANNER_SELF = [
    ("tests/integration/check_see1594_governance.py",
     "扫描器自身（pattern 定义/allowlist 表）"),
    ("tests/unit/test_see1594_governance_scan.py",
     "载体文本：扫描器最小反例的合成样例——随用例增删而变，免 stale 检查"),
]
# 载体文本条目：允许零命中（不参与 stale 检查）；其余条目零命中即违规。
for _p in ALLOWLIST:
    _have = {e[0] for e in ALLOWLIST[_p]}
    for _entry in _SCANNER_SELF:
        if _entry[0] not in _have:
            ALLOWLIST[_p].append(_entry)


def scan_retired(repo_root: Path) -> tuple[list[dict], list[dict]]:
    """返回 (violations, allowlisted)。allowlist 条目零命中 → 自身违规。"""
    violations: list[dict] = []
    allowlisted: list[dict] = []
    allow_hits: dict[str, set[int]] = {p: set() for p in ALLOWLIST}
    for path in _iter_scan_files(repo_root):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(repo_root).as_posix()
        for no, line in enumerate(lines, 1):
            for pname, pattern in RETIRED_PATTERNS.items():
                if not re.search(pattern, line):
                    continue
                entry = next((a for a in ALLOWLIST.get(pname, [])
                              if a[0] in rel), None)
                if entry is not None:
                    allow_hits[pname].add(ALLOWLIST[pname].index(entry))
                    allowlisted.append({
                        "pattern": pname, "file": rel, "line": no,
                        "allowlist_reason": entry[1],
                        "text": line.strip()[:160]})
                else:
                    violations.append({
                        "pattern": pname, "file": rel, "line": no,
                        "text": line.strip()[:160]})
    stale = [
        {"pattern": p, "kind": "stale_allowlist",
         "entry": ALLOWLIST[p][i][0],
         "reason": ALLOWLIST[p][i][1],
         "error": "allowlist entry matched nothing (stale)"}
        for p, hits in allow_hits.items() for i in range(len(ALLOWLIST[p]))
        if i not in hits
        and not ALLOWLIST[p][i][1].startswith("载体文本")]
    violations.extend(stale)
    return violations, allowlisted


def scan_single_point(repo_root: Path) -> list[dict]:
    """单点 def 站点：每个符号全局恰好一个 def，且落在期望文件。"""
    violations: list[dict] = []
    src = repo_root / "src"
    for name, expected in SINGLE_POINT_DEFS.items():
        sites = []
        for py in sorted(src.rglob("*.py")):
            try:
                lines = py.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for no, line in enumerate(lines, 1):
                if re.match(rf"def {re.escape(name)}\(", line):
                    sites.append((py.relative_to(repo_root).as_posix(), no))
        ok = len(sites) == 1 and expected in sites[0][0]
        violations.append({
            "symbol": name, "sites": sites, "expected_file": expected,
            "ok": ok})
    return [v for v in violations if not v["ok"]]


def scan_dual_truth(repo_root: Path) -> tuple[dict, list[dict]]:
    """消费链结构断言：三消费面经 /6 map 单真源（引用在场即证据）。"""
    evidence: dict = {"consumer_chain": [], "violations": []}
    for surface, chain in CONSUMER_CHAIN.items():
        for rel, anchor, must_reference in chain:
            p = repo_root / rel
            found = None
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
            except OSError:
                evidence["violations"].append(
                    {"surface": surface, "file": rel, "error": "missing"})
                continue
            in_anchor = False
            for no, line in enumerate(lines, 1):
                if re.match(rf"(def|class) {re.escape(anchor)}\b", line):
                    in_anchor = True
                elif in_anchor and re.match(r"\S", line) and \
                        not line.startswith((" ", "\t", ")", "#")):
                    break  # anchor 函数体结束
                if in_anchor and must_reference in line:
                    found = {"file": rel, "line": no,
                             "text": line.strip()[:140]}
                    break
            if found:
                evidence["consumer_chain"].append(
                    {"surface": surface, "anchor": anchor,
                     "references": must_reference, **found})
            else:
                evidence["violations"].append({
                    "surface": surface, "file": rel, "anchor": anchor,
                    "error": f"anchor body does not reference "
                             f"{must_reference}"})
    return evidence, evidence["violations"]


def run(mode: str, repo_root: Path | None = None) -> dict:
    root = repo_root or REPO_ROOT
    if mode != "semantics":
        return {"tool": "see1594.governance", "ok": False,
                "failures": [{"reason": f"unknown mode: {mode}"}]}
    retired, allowlisted = scan_retired(root)
    single_violations = scan_single_point(root)
    dual, dual_violations = scan_dual_truth(root)
    ok = not (retired or single_violations or dual_violations)
    return {
        "tool": "see1594.governance",
        "mode": mode,
        "ok": ok,
        "retired_violations": retired,
        "retired_allowlisted": allowlisted,
        "single_point_violations": single_violations,
        "consumer_chain": dual["consumer_chain"],
        "dual_truth_violations": dual_violations,
        "failures": (
            [{"reason": f"retired symbol residue: {v['pattern']} at "
                        f"{v.get('file', v.get('entry'))}"
                        f"{':' + str(v['line']) if 'line' in v else ''} — "
                        f"{v.get('text', v.get('error', ''))}"}
             for v in retired]
            + [{"reason": f"single-point violation: {v['symbol']} -> "
                          f"{v['sites']}"} for v in single_violations]
            + [{"reason": f"dual-truth violation: {v}"} for v in dual_violations]
            or []),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SEE-1594 §SPEC-027 governance scan")
    parser.add_argument("--mode", default="semantics",
                        choices=["semantics"])
    parser.add_argument("--out", required=True, help="JSON 报告输出路径")
    parser.add_argument("--repo-root", default=None,
                        help="仓库根（缺省=本脚本所在仓库）")
    args = parser.parse_args(argv)
    report = run(args.mode, Path(args.repo_root).resolve()
                 if args.repo_root else None)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(json.dumps({"tool": report["tool"], "ok": report["ok"],
                      "failures": report["failures"][:5]},
                     ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
