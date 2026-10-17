"""Pure Core 架构守卫 gate（SEE-1426 P1，§SPEC-1426-01）。

沿 complexity gate 模式：collector 产出违规事实，gate 归并为统一 JSON 契约
{tool, ok, summary, failures}。ok = 无违规且无 unparseable（unmeasured =
fail signal，绝不静默跳过——complexity 同款纪律）。
"""

from __future__ import annotations

from .collector import ArchViolation, collect_paths


def run_arch_gate(paths: list[str]) -> dict:
    violations, unparseable = collect_paths(paths)
    failures = (
        [{"file": v.file, "line": v.line, "rule": v.rule, "message": v.message}
         for v in violations]
        + unparseable
    )
    by_rule: dict[str, int] = {}
    for v in violations:
        by_rule[v.rule] = by_rule.get(v.rule, 0) + 1
    return {
        "tool": "arch",
        "ok": not failures,
        "summary": {
            "files": len({v.file for v in violations}) ,
            "violations": len(violations),
            "unparseable": len(unparseable),
            "by_rule": by_rule,
        },
        "failures": failures,
    }
