"""Registry snapshot diff with scope-escape marking (§SPEC-020).

Diff semantics (`func-registry-diff/1`): unchanged / edited / added / removed
by fid; a removed∩added pair with an identical body_hash is an
`identity_changed_candidate` — a *machine candidate* only, `human_final=true`
so it can never auto-count as governance gain. Escape rules (L1 ruling):
target kind=lambda, non-empty lex_scope (moved into an inner class), or a
path inside the default escape dirs (`acceptance/generated/`, `.godot/`,
`addons/`). Escape rows are counted separately and never count toward the
governance net.
"""

from __future__ import annotations

import json
from pathlib import Path

DIFF_VERSION = "func-registry-diff/1"
DEFAULT_ESCAPE_DIRS = ("acceptance/generated/", ".godot/", "addons/")


def _in_escape_dir(fid: str, escape_dirs: tuple[str, ...]) -> bool:
    rel = fid.split("::", 1)[0].replace("\\", "/")
    if not rel.startswith("./"):
        rel = rel.lstrip("/")
    return any(rel.startswith(d) for d in escape_dirs)


def _escape_rules(row_kind: str, lex_scope: list, fid: str,
                  escape_dirs: tuple[str, ...]) -> list[str]:
    rules: list[str] = []
    if row_kind == "lambda":
        rules.append("lambda_kind")
    if lex_scope:
        rules.append("moved_into_inner_class")
    if _in_escape_dir(fid, escape_dirs):
        rules.append("escape_dir")
    return rules


def _by_fid(snapshot: dict) -> dict:
    return {
        e["fid"]: e
        for file_reg in (snapshot.get("files") or {}).values()
        for e in file_reg.get("entries", [])
    }


def _identity_candidates(
    old_by_fid: dict, new_by_fid: dict,
    added_fids: list[str], removed_fids: list[str],
) -> dict[str, str]:
    """removed∩added pairs with identical body_hash (rename / move). Machine
    candidates only — pairing is greedy by first occurrence and every match
    is flagged human_final=true by the caller."""
    candidates: dict[str, str] = {}
    for removed in removed_fids:
        removed_hash = old_by_fid[removed]["body_hash"]
        match = next(
            (
                added
                for added in added_fids
                if new_by_fid[added]["body_hash"] == removed_hash
            ),
            None,
        )
        if match is not None:
            candidates[removed] = match
    return candidates


def _mark_escapes(rows: list[dict], new_by_fid: dict,
                  escape_dirs: tuple[str, ...]) -> None:
    """Escape marking: added or candidate rows only (L1 rules)."""
    for row in rows:
        if row["status"] in ("added", "identity_changed_candidate"):
            entry = new_by_fid[row["fid"]]
            rules = _escape_rules(entry["kind"], entry["lex_scope"],
                                  row["fid"], escape_dirs)
            if rules:
                row["scope_escape"] = True
                row["escape_rule"] = rules[0]
                row["escape_rules"] = rules


def _summarize_rows(rows: list[dict]) -> dict:
    added_rows = [r for r in rows if r["status"] == "added"]
    escape_added = sum(1 for r in added_rows if r.get("scope_escape"))
    removed_rows = [r for r in rows
                    if r["status"] in ("removed", "removed_identity_candidate")]
    candidate_rows = [
        r for r in rows if r["status"] == "identity_changed_candidate"
    ]
    return {
        "added": len(added_rows),
        "removed": len(removed_rows),
        "edited": sum(1 for r in rows if r["status"] == "edited"),
        "identity_changed_candidates": len(candidate_rows),
        "scope_escape": escape_added,
        "scope_escape_candidates": sum(
            1 for r in candidate_rows if r.get("scope_escape")
        ),
        "governance_net": {
            "countable_added": len(added_rows) - escape_added,
            "escape_added": escape_added,
        },
    }


def diff_registries(old: dict, new: dict,
                    escape_dirs: tuple[str, ...] = DEFAULT_ESCAPE_DIRS) -> dict:
    """Compare two multi-file registry snapshots by fid."""
    old_by_fid = _by_fid(old)
    new_by_fid = _by_fid(new)

    rows: list[dict] = []
    added_fids: list[str] = []
    for fid, entry in new_by_fid.items():
        if fid in old_by_fid:
            if old_by_fid[fid]["body_hash"] == entry["body_hash"]:
                rows.append({"fid": fid, "status": "unchanged",
                             "scope_escape": False})
            else:
                rows.append(
                    {
                        "fid": fid,
                        "status": "edited",
                        "body_hash": {
                            "old": old_by_fid[fid]["body_hash"],
                            "new": entry["body_hash"],
                        },
                        "scope_escape": False,
                    }
                )
        else:
            added_fids.append(fid)
            rows.append({"fid": fid, "status": "added", "scope_escape": False})

    removed_fids: list[str] = []
    for fid in old_by_fid:
        if fid not in new_by_fid:
            removed_fids.append(fid)
            rows.append({"fid": fid, "status": "removed", "scope_escape": False})

    candidates = _identity_candidates(
        old_by_fid, new_by_fid, added_fids, removed_fids
    )
    for removed, added in candidates.items():
        for row in rows:
            if row["fid"] == removed and row["status"] == "removed":
                row["status"] = "removed_identity_candidate"
                row["new_fid"] = added
            elif row["fid"] == added and row["status"] == "added":
                row["status"] = "identity_changed_candidate"
                row["evidence"] = "body_hash_identical"
                row["human_final"] = True

    _mark_escapes(rows, new_by_fid, escape_dirs)
    # unified JSON contract keys: `ok` means "command executed completely"
    # (plan §4.1) — lane anomalies are data, not exit-code semantics
    return {
        "tool": "registry",
        "ok": True,
        "diff_version": DIFF_VERSION,
        "rows": rows,
        "summary": _summarize_rows(rows),
        "failures": [],
    }


def load_snapshot(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_snapshot(snapshot: dict, path: str | Path) -> None:
    Path(path).write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
