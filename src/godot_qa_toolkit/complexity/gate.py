"""Complexity gate: machine pass/fail verdict over collected metrics (SEE-1268 M1).

P0' ruling: thresholds are machine-decidable. Defaults — fail above
max_complexity, warn-only above warn_complexity — are overridable so the Team
Lead tunes the "度" per scenario without touching tool code.
"""

from __future__ import annotations

from dataclasses import dataclass

from .collector import FunctionComplexity, collect_paths

DEFAULT_WARN_COMPLEXITY = 10  # radon rank B boundary
DEFAULT_MAX_COMPLEXITY = 15   # radon rank C boundary — hard fail


@dataclass(frozen=True)
class GateConfig:
    warn_complexity: int = DEFAULT_WARN_COMPLEXITY
    max_complexity: int = DEFAULT_MAX_COMPLEXITY

    def __post_init__(self) -> None:
        if self.max_complexity < self.warn_complexity:
            raise ValueError(
                f"max_complexity ({self.max_complexity}) must be >= "
                f"warn_complexity ({self.warn_complexity})"
            )


def run_gate(paths: list[str], config: GateConfig | None = None) -> dict:
    cfg = config or GateConfig()
    functions, unparseable = collect_paths(paths)

    violations = [
        {
            "file": f.file,
            "name": f.name,
            "line": f.line,
            "complexity": f.complexity,
            "threshold": cfg.max_complexity,
        }
        for f in functions
        if f.complexity > cfg.max_complexity
    ]
    warnings = [
        {
            "file": f.file,
            "name": f.name,
            "line": f.line,
            "complexity": f.complexity,
            "threshold": cfg.warn_complexity,
        }
        for f in functions
        if cfg.warn_complexity < f.complexity <= cfg.max_complexity
    ]

    return {
        "tool": "complexity",
        "ok": not violations and not unparseable,
        "config": {"warn_complexity": cfg.warn_complexity, "max_complexity": cfg.max_complexity},
        "summary": {
            "functions": len(functions),
            "max_seen": max((f.complexity for f in functions), default=0),
            "violations": len(violations),
            "warnings": len(warnings),
            "unparseable": len(unparseable),
        },
        "failures": violations + unparseable,
        "warnings": warnings,
    }
