"""Full per-function CC parity over the neutral registry (§SPEC-019).

Oracle = registry func_def entries (raw-AST decision points — radon records
are never the oracle, §SPEC-004 discipline). Radon records come from the same
convert+sanitize pipeline as the collector, so parity measures the real
pipeline. Pairing is per bare name by occurrence order: equal counts pair 1:1
(both sides enumerate inner-class methods; radon's cc_visit output order is
NOT source order once classes are present — fixture-verified — so the radon
side is sorted by lineno before pairing), and any count divergence sends the
whole name group to identity_mismatch — never folded, never guessed.
`closure_ok` is the machine-checkable invariant that every registry entry
landed in exactly one lane.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..registry.functions import build_file_registry
from .sanitizer import sanitize_python_ir_identifiers

PARITY_VERSION = "parity/1"
LANES = (
    "match",
    "lambda_gap",
    "lambda_disclosed",
    "missing_in_radon",
    "identity_mismatch",
    "mismatch",
    "cc_unmeasured",
)


def _row(fid, name, lane, cc_oracle=None, radon_cc=None, lambda_gap=None,
         pairing=None) -> dict:
    """One parity/1 row — the seven-key shape every lane emits."""
    return {
        "fid": fid,
        "name": name,
        "lane": lane,
        "cc_oracle": cc_oracle,
        "radon_cc": radon_cc,
        "lambda_gap": lambda_gap,
        "pairing": pairing,
    }


def _lane_for(cc_oracle: int, radon_cc: int | None,
              lambda_gap: int) -> str:
    if radon_cc is None:
        return "missing_in_radon"
    if radon_cc == cc_oracle:
        return "match"
    if cc_oracle > radon_cc and cc_oracle - radon_cc == lambda_gap:
        return "lambda_gap"
    return "mismatch"


def _radon_records(gd_source: str) -> list:
    """Radon Function records from the collector's exact pipeline."""
    from gdtoolkit.gd2py import convert_code
    from radon.complexity import cc_visit
    from radon.visitors import Function

    python_code = sanitize_python_ir_identifiers(convert_code(gd_source))
    return [
        b for b in cc_visit(python_code) if isinstance(b, Function)
    ]


def _lambda_rows(lambda_entries: list[dict]) -> list[dict]:
    return [
        _row(lam["fid"], lam["name"], "lambda_disclosed",
             cc_oracle=lam["cc_oracle"], lambda_gap=0, pairing="lambda")
        for lam in lambda_entries
    ]


def _named_group_rows(name: str, entries: list[dict],
                      radon_ccs: list[int]) -> list[dict]:
    """Occurrence pairing for one bare-name group; count divergence fails
    loud for the whole group instead of folding or guessing."""
    if len(entries) == len(radon_ccs):
        return [
            _row(
                entry["fid"],
                name,
                _lane_for(entry["cc_oracle"], radon_cc, entry["lambda_gap"]),
                cc_oracle=entry["cc_oracle"],
                radon_cc=radon_cc,
                lambda_gap=entry["lambda_gap"],
                pairing="occurrence",
            )
            for entry, radon_cc in zip(entries, radon_ccs)
        ]
    if not radon_ccs and len(entries) == 1:
        # 1:0 — e.g. a function literally named `def` sanitized to `def_`
        # before radon sees it: fail-loud missing lane. A whole group missing
        # (N:0, N>1) stays in identity_mismatch — pipeline anomaly, not a
        # known rename pattern.
        entry = entries[0]
        return [
            _row(entry["fid"], name, "missing_in_radon",
                 cc_oracle=entry["cc_oracle"], lambda_gap=entry["lambda_gap"])
        ]
    return [
        _row(entry["fid"], name, "identity_mismatch",
             cc_oracle=entry["cc_oracle"], lambda_gap=entry["lambda_gap"])
        for entry in entries
    ]


def pair_file(gd_source: str, file_label: str,
              registry: dict | None = None) -> dict:
    """Build the per-file parity rows and lane counts (parity/1)."""
    if registry is None:
        registry = build_file_registry(gd_source, file_label)
    if registry["parse"] != "ok":
        return {
            "file": file_label,
            "parse": "error",
            "reason": registry.get("reason"),
            "rows": [
                _row(f"{file_label}::<unparseable>", "<unparseable>",
                     "cc_unmeasured")
            ],
            "func_entries": 0,
            "lambda_entries": 0,
        }

    radon = _radon_records(gd_source)
    radon_by_name: dict[str, list[int]] = {}
    # radon's cc_visit output order does NOT follow source order once classes
    # are present (fixture-verified) — sorting by lineno restores the source
    # order that the occurrence pairing relies on.
    for block in sorted(radon, key=lambda b: b.lineno):
        radon_by_name.setdefault(block.name, []).append(int(block.complexity))

    func_entries = [e for e in registry["entries"] if e["kind"] == "func_def"]
    lambda_entries = [e for e in registry["entries"] if e["kind"] == "lambda"]

    by_name: dict[str, list[dict]] = {}
    for entry in func_entries:
        by_name.setdefault(entry["name"], []).append(entry)

    rows = _lambda_rows(lambda_entries)
    for name, entries in by_name.items():
        rows.extend(_named_group_rows(name, entries, radon_by_name.get(name, [])))

    return {
        "file": file_label,
        "parse": "ok",
        "rows": rows,
        "func_entries": len(func_entries),
        "lambda_entries": len(lambda_entries),
    }


def parity_file(gd_source: str, file_label: str,
                registry: dict | None = None) -> dict:
    """pair_file + closure metadata for one file."""
    report = pair_file(gd_source, file_label, registry=registry)
    report["lanes"] = {
        lane: sum(1 for r in report["rows"] if r["lane"] == lane)
        for lane in LANES
    }
    return report


def _collect_tasks(paths: list[Path], project_root: str) -> list[tuple[str, str]]:
    """(abs_path, rel_label) worker tasks: recursive .gd walk under each path,
    deduplicated by resolved path (L3: the same three roots as the complexity
    gate)."""
    import glob

    tasks: list[tuple[str, str]] = []
    root = Path(project_root).resolve()
    seen: set[str] = set()
    for path in paths:
        p = Path(path)
        candidates = (
            sorted(glob.glob(str(p / "**" / "*.gd"), recursive=True))
            if p.is_dir()
            else [str(p)]
        )
        for file_path in candidates:
            resolved = str(Path(file_path).resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                rel = str(Path(file_path).resolve().relative_to(root))
            except ValueError:
                rel = Path(file_path).name
            tasks.append((file_path, rel))
    return tasks


def _run_tasks(tasks: list[tuple[str, str]]) -> list[tuple[str, dict, dict]]:
    """Fan tasks across a process pool — the §SPEC-022 budget (parity ≤ 1.5×
    the serial complexity scan) is only reachable with the AST face
    parallelized. Restricted environments fall back to serial."""
    from concurrent.futures import ProcessPoolExecutor
    from concurrent.futures.process import BrokenProcessPool

    workers = min(os.cpu_count() or 1, 8)
    if workers > 1 and len(tasks) > 1:
        try:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                return list(pool.map(_parity_file_task, tasks))
        except (OSError, PermissionError, BrokenProcessPool):
            # process-pool unavailable or a worker crashed at startup —
            # serial fallback keeps the run complete (obs-1)
            pass
    return [_parity_file_task(t) for t in tasks]


def _aggregate_results(
    results: list[tuple[str, dict, dict]],
) -> tuple[list[dict], dict[str, dict], dict]:
    """Fold per-file results into the parity/1 summary (closure invariant
    appended last), file reports sorted by label, and the registry snapshot
    transport for --registry-out."""
    file_reports: list[dict] = []
    snapshot_files: dict[str, dict] = {}
    summary = {
        "files": 0,
        "func_entries": 0,
        "lambda_entries": 0,
        "inner_class_method_entries": 0,
        "inner_class_files": 0,
        "lambda_gap_total": 0,
        "unparseable": 0,
        **{lane: 0 for lane in LANES},
    }
    for rel, report, registry in results:
        summary["files"] += 1
        summary["func_entries"] += report["func_entries"]
        summary["lambda_entries"] += report["lambda_entries"]
        if report["parse"] != "ok":
            summary["unparseable"] += 1
        for lane, count in report["lanes"].items():
            summary[lane] += count
        file_reports.append(report)
        summary["inner_class_method_entries"] += sum(
            1
            for e in registry["entries"]
            if e["kind"] == "func_def" and e["lex_scope"]
        )
        if registry.get("inner_classes"):
            summary["inner_class_files"] += 1
        # lane rows count hosts; the disclosed gap magnitude is summed
        # separately (plan §4.4 counts lambda occurrences, not hosts)
        summary["lambda_gap_total"] += sum(
            e["lambda_gap"] for e in registry["entries"] if e["kind"] == "func_def"
        )
        snapshot_files[rel] = registry

    lanes_total = sum(summary[lane] for lane in LANES)
    summary["closure_ok"] = lanes_total == (
        summary["func_entries"] + summary["lambda_entries"]
    )
    file_reports.sort(key=lambda r: r["file"])
    return file_reports, snapshot_files, summary


def _parity_file_task(task: tuple[str, str]) -> tuple[str, dict, dict]:
    """Worker: (abs_path, rel_label) → (label, parity report, registry).

    Module-level pure function so parity_paths can fan out across a process
    pool — the AST face (lark parse + oracle counters) and the radon face are
    independent per file.
    """
    path, label = task
    gd_source = Path(path).read_text(encoding="utf-8")
    registry = build_file_registry(gd_source, file_label=label)
    report = parity_file(gd_source, file_label=label, registry=registry)
    return label, report, registry


def parity_paths(paths: list[Path], project_root: str) -> dict:
    """Run parity over recursive .gd files under each path (L3: the same
    three roots as the complexity gate)."""
    file_reports, snapshot_files, summary = _aggregate_results(
        _run_tasks(_collect_tasks(paths, project_root))
    )
    return {
        "tool": "parity",
        "ok": True,
        "report_version": PARITY_VERSION,
        "diagnostic_only": True,
        "summary": summary,
        "files": file_reports,
        "_snapshot_files": snapshot_files,
    }
