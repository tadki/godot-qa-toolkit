"""Complexity collector over gdtoolkit's parser + radon (SEE-1268 M1).

Uses gdtoolkit's gd2py conversion and radon's cc_visit programmatically
(the gdradon CLI has text-only output). gd2py preserves control-flow shape —
exactly what cyclomatic complexity needs — so function-level cc numbers match
what `gdradon cc` prints.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gdtoolkit.gd2py import convert_code
from radon.complexity import cc_visit


@dataclass(frozen=True)
class FunctionComplexity:
    file: str
    name: str
    complexity: int
    line: int


def collect_file(path: str | Path) -> tuple[list[FunctionComplexity], str | None]:
    """Collect one file. Returns (functions, error) — error is set when the
    file cannot be converted (gd2py chokes on GDScript identifiers that are
    Python keywords, e.g. `var def`). Callers decide how to weigh it."""
    try:
        src = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return [], f"unreadable: {e}"
    try:
        return _collect_source(src, file_name=str(path)), None
    except SyntaxError as e:
        return [], f"gd2py conversion failed: {e}"


def collect_paths(paths: list[str | Path]) -> tuple[list[FunctionComplexity], list[dict]]:
    """Collect complexity for every .gd file under the given paths.

    Returns (functions, unparseable) where unparseable lists files that could
    not be measured — a real signal (gate counts them as failures), but never
    a crash: one bad file must not hide the metrics of the rest.
    """
    results: list[FunctionComplexity] = []
    unparseable: list[dict] = []
    for root in paths:
        p = Path(root)
        files = sorted(p.rglob("*.gd")) if p.is_dir() else [p]
        for f in files:
            funcs, error = collect_file(f)
            if error is not None:
                unparseable.append({"file": str(f), "reason": error})
            results.extend(funcs)
    return results, unparseable


def _collect_source(src: str, file_name: str) -> list[FunctionComplexity]:
    python_code = convert_code(src)
    blocks = cc_visit(python_code)
    return [
        FunctionComplexity(
            file=file_name,
            name=b.name,
            complexity=int(b.complexity),
            line=int(b.lineno),
        )
        for b in blocks
    ]
