"""Coverage evidence v2: run-record lifecycle, bundle construction, sink
recovery and explicit pruning (§SPEC-025/026).

- per-run UUID sink: `qa-coverage-hits-{run_uuid}.txt` — same-process,
  cross-process and CLI/risk-embedded runs are mutually isolated (fixes
  D-候选-1: the PID-based sink accumulated hits across sequential runs).
- run record: written atomically BEFORE GUT starts (`status=started`), so a
  crash leaves the UUID sink + a started record on disk — `--recover` re-reads
  the sink against the recorded probe manifest without re-running GUT.
- bundle (`coverage-evidence/2`): written after a complete run, then the
  sink is deleted (evidence has moved into the bundle). Failure paths keep
  the sink for recovery.
- `--prune-sinks` (W1 ruling): explicit cleanup of `qa-coverage-hits-*` files
  older than the retention period (default 7 days). user:// lives under the
  user's HOME — silent automatic sweeps are out of scope by ruling.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

from ..registry.functions import build_file_registry
from ..registry.manifest import build_probe_manifest_v2

EVIDENCE_VERSION = "coverage-evidence/2"
SINK_PREFIX = "qa-coverage-hits-"
RETENTION_DAYS = 7


def make_run_uuid() -> str:
    return str(uuid.uuid4())


def sink_name_for(run_uuid: str) -> str:
    return f"{SINK_PREFIX}{run_uuid}.txt"


def sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def evidence_dir_for(evidence_out: str | None) -> Path:
    return Path(evidence_out) if evidence_out else Path.cwd()


def write_run_record(evidence_dir: Path, record: dict) -> None:
    """Atomic write: recovery treats a malformed record as failed-start and
    reports it — it must never observe a half-written record as valid."""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    tmp = evidence_dir / f"{record['run_uuid']}.record.tmp"
    tmp.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(evidence_dir / f"{record['run_uuid']}.record.json")


def read_run_record(evidence_dir: Path, run_uuid: str) -> dict | None:
    record_path = evidence_dir / f"{run_uuid}.record.json"
    if not record_path.is_file():
        return None
    try:
        return json.loads(record_path.read_text(encoding="utf-8"))
    except ValueError:
        return {"run_uuid": run_uuid, "status": "record_corrupt"}


def update_run_record(evidence_dir: Path, run_uuid: str, **fields) -> None:
    record = read_run_record(evidence_dir, run_uuid) or {"run_uuid": run_uuid}
    record.update(fields)
    write_run_record(evidence_dir, record)


def build_run_record(
    *,
    run_uuid: str,
    file_path: str,
    gd_source: str,
    tests_glob: str | None,
    probe_manifest: dict,
) -> dict:
    return {
        "run_uuid": run_uuid,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "target": {
            "file": file_path,
            "sha256": sha256_text(gd_source),
        },
        "recipe": {
            "arm": "gut_core" if tests_glob else "gut_full",
            "tests_glob": tests_glob or "res://tests/",
        },
        "status": "started",
        "probe_manifest": probe_manifest,
    }


def build_bundle(
    *,
    run_uuid: str,
    file_path: str,
    gd_source: str,
    tests_glob: str | None,
    hits_lines: list[int],
    probes_fired: int,
    gut_ok: bool,
    run_error: bool,
    timeout: bool,
    zero_tests: bool,
    vacuous: bool,
    recovered: bool = False,
) -> dict:
    """Assemble the `coverage-evidence/2` bundle: probe-manifest/2 (T/I), the
    sink hits attributed to fids (C), run sentinels and per-entry states."""
    registry = build_file_registry(gd_source, file_label=file_path)
    manifest = build_probe_manifest_v2(gd_source, registry)
    line_to_plan = {p["line"]: p for p in manifest["probe_plan"]}
    hits = set(hits_lines)
    instrumentable_fids = {
        e["fid"] for e in registry["entries"] if e["instrumentable"]
    }

    t_rows: list[dict] = []
    i_rows: list[dict] = []
    c_rows: list[dict] = []
    entry_states: list[dict] = []
    per_function = {p["fid"]: p for p in manifest["per_function"]}
    for entry in registry["entries"]:
        fid = entry["fid"]
        pf = per_function.get(fid, {})
        t_ids = list(pf.get("t_line_ids", []))
        i_ids = sorted(
            p["line"] for p in manifest["probe_plan"] if p["fid"] == fid
        )
        covered_ids = sorted(line for line in i_ids if line in hits)
        t_rows.append(
            {
                "fid": fid,
                "kind": entry["kind"],
                "t_lines": len(t_ids),
                "line_ids": t_ids,
            }
        )
        i_rows.append({"fid": fid, "i_lines": len(i_ids)})
        c_rows.append(
            {
                "fid": fid,
                "covered": len(covered_ids),
                "covered_line_ids": covered_ids,
            }
        )
        if fid in instrumentable_fids:
            entry_states.append(
                {
                    "fid": fid,
                    "state": (
                        "measured"
                        if len(t_ids) == len(i_ids)
                        else "partial_instrumentation"
                    ),
                    "t_equal_i": len(t_ids) == len(i_ids),
                }
            )
        else:
            # inner-class method: pruned from instrumentation by design
            entry_states.append(
                {"fid": fid, "state": "partial_instrumentation",
                 "t_equal_i": False}
            )

    file_state = "vacuous" if not registry["entries"] else "bundle_only"

    return {
        "evidence_version": EVIDENCE_VERSION,
        "run_uuid": run_uuid,
        "recovered": recovered,
        "target": {"file": file_path, "sha256": sha256_text(gd_source)},
        "recipe": {
            "arm": "gut_core" if tests_glob else "gut_full",
            "tests_glob": tests_glob or "res://tests/",
        },
        "probe_manifest": {
            "manifest_version": "probe-manifest/2",
            "inline": manifest,
        },
        "T": t_rows,
        "I": i_rows,
        "C": c_rows,
        "census_gaps": manifest["census_gaps"],
        "sentinels": {
            "probes_fired": probes_fired,
            "vacuous": vacuous,
            "run_error": run_error,
            "timeout": timeout,
            "zero_tests": zero_tests,
            "gut_ok": gut_ok,
            "transport_anomaly": 0,
        },
        "entry_states": entry_states,
        "file_state": file_state,
    }


def write_bundle(evidence_dir: Path, bundle: dict) -> Path:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    out = evidence_dir / f"{bundle['run_uuid']}.json"
    out.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return out


def recover_bundle(evidence_dir: Path) -> dict:
    """Rebuild bundles for started-but-unfinished runs on disk, WITHOUT
    re-running GUT: the started record carries the probe manifest inline, so
    the UUID sink's raw probe ids can be re-attributed to fids exactly like
    the online path. The sink is kept (recovery only reads it)."""
    errors: list[str] = []
    recovered: list[dict] = []
    if not evidence_dir.is_dir():
        return {"recovered": recovered, "errors": ["evidence dir missing"]}

    for record_path in sorted(evidence_dir.glob("*.record.json")):
        # stem of "<uuid>.record.json" still carries the ".record" suffix —
        # reading by path (not by stem) avoids the double-suffix miss
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except ValueError:
            record = None
        if not isinstance(record, dict):
            errors.append(f"unreadable record: {record_path.name}")
            continue
        if record.get("status") == "bundle_written":
            continue
        run_uuid = record.get("run_uuid")
        manifest = record.get("probe_manifest") or {}
        plan = manifest.get("probe_plan", [])
        plan_by_id = {p["probe_id"]: p for p in plan}
        sink = _find_sink(evidence_dir, record, run_uuid)
        if sink is None:
            errors.append(
                f"run {run_uuid}: sink {SINK_PREFIX}{run_uuid}.txt missing — "
                "cannot recover without hits data"
            )
            continue
        valid_ids = set(plan_by_id)
        hits_raw = [
            int(ln.strip())
            for ln in Path(sink).read_text(encoding="utf-8").splitlines()
            if ln.strip().isdigit()
        ]
        hits_lines = sorted(
            {plan_by_id[pid]["line"] for pid in hits_raw if pid in valid_ids}
        )
        covered_by_fid: dict[str, list[int]] = {}
        for pid in hits_raw:
            if pid in valid_ids:
                covered_by_fid.setdefault(plan_by_id[pid]["fid"], []).append(
                    plan_by_id[pid]["line"]
                )
        per_function = {p["fid"]: p for p in manifest.get("per_function", [])}
        bundle = {
            "evidence_version": EVIDENCE_VERSION,
            "run_uuid": run_uuid,
            "recovered": True,
            "target": record.get("target", {}),
            "recipe": record.get("recipe", {}),
            "probe_manifest": {
                "manifest_version": "probe-manifest/2",
                "inline": manifest,
            },
            "T": [
                {
                    "fid": fid,
                    "t_lines": pf.get("t_lines", 0),
                    "line_ids": pf.get("t_line_ids", []),
                }
                for fid, pf in per_function.items()
            ],
            "I": [
                {"fid": fid, "i_lines": pf.get("i_lines", 0)}
                for fid, pf in per_function.items()
            ],
            "C": [
                {
                    "fid": fid,
                    "covered": len(sorted(set(lines))),
                    "covered_line_ids": sorted(set(lines)),
                }
                for fid, lines in covered_by_fid.items()
            ],
            "census_gaps": record.get("census_gaps", []),
            "sentinels": {
                "probes_fired": len([p for p in hits_raw if p in valid_ids]),
                "vacuous": False,
                "run_error": False,
                "timeout": False,
                "zero_tests": False,
                "gut_ok": True,
                "transport_anomaly": len(
                    [p for p in hits_raw if p not in valid_ids]
                ),
            },
            "entry_states": record.get("entry_states", []),
            "file_state": "recovered",
        }
        out = write_bundle(evidence_dir, bundle)
        update_run_record(evidence_dir, run_uuid, status="bundle_written")
        recovered.append({"run_uuid": run_uuid, "bundle": str(out)})
    return {"recovered": recovered, "errors": errors}


def _find_sink(evidence_dir: Path, record: dict, run_uuid: str) -> str | None:
    candidates = [record.get("sink_path"), str(evidence_dir / sink_name_for(run_uuid))]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def prune_sinks(project_root: str, retention_days: int = RETENTION_DAYS) -> dict:
    """Explicit cleanup of `qa-coverage-hits-*` files older than the
    retention period (W1 ruling: user:// lives under HOME — no implicit
    automatic sweeps). Returns what was deleted for the report."""
    from .runner import _resolve_user_sink_dir

    now = time.time()
    deleted: list[str] = []
    checked_dirs: list[Path] = []
    user_dir = _resolve_user_sink_dir(project_root)
    if user_dir:
        checked_dirs.append(Path(user_dir))
    checked_dirs.append(Path(project_root))
    for d in checked_dirs:
        if not d.is_dir():
            continue
        for f in d.glob(f"{SINK_PREFIX}*"):
            try:
                age_days = (now - f.stat().st_mtime) / 86400
                if age_days > retention_days:
                    f.unlink()
                    deleted.append(str(f))
            except OSError:
                continue
    return {
        "deleted": deleted,
        "retention_days": retention_days,
        "dirs_checked": [str(d) for d in checked_dirs],
    }


def clear_sink(sink_path: str | None, project_root: str,
               sink_name: str) -> None:
    """Delete this run's sink after the bundle landed (evidence moved)."""
    if sink_path and Path(sink_path).is_file():
        os.remove(sink_path)
    local = Path(project_root) / sink_name
    if local.is_file():
        local.unlink()
