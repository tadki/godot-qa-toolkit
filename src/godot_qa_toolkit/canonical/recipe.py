"""Canonical full-recipe builder (canonical-recipe/1, §SPEC-034).

Read-only: scans `.gutconfig.json` (the live non-quarantine GUT face) and
`acceptance/features/**/*.feature` (per-feature sha256), folds harness and
toolchain versions into the hash, and normalizes the whole recipe (sorted
keys, compact separators) before sha256 — same input, same recipe_hash; any
byte change in any hashed input changes it. Exclusions REQUIRE a reason: an
empty reason makes the recipe illegal (plan §4.6 — exclusions enter the hash
with their justification).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

RECIPE_VERSION = "canonical-recipe/1"
_GUTCONFIG = ".gutconfig.json"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_gutconfig(project_root: Path) -> tuple[dict | None, str | None]:
    """Parse `.gutconfig.json`; returns (config, None) or (None, reason)."""
    cfg_path = project_root / _GUTCONFIG
    if not cfg_path.is_file():
        return None, f"{_GUTCONFIG} not found in project root"
    try:
        config = json.loads(cfg_path.read_text(encoding="utf-8"))
    except ValueError as e:
        return None, f"{_GUTCONFIG} parse failed: {e}"
    if not isinstance(config, dict):
        return None, f"{_GUTCONFIG} is not a JSON object"
    return config, None


def scan_acceptance_features(project_root: Path) -> tuple[list[dict], int, str | None]:
    """Scan acceptance/features/**/*.feature; returns (rows, count, error).

    Each row: {key (GUT 9.x stem convention), source, sha256}."""
    features_dir = project_root / "acceptance" / "features"
    if not features_dir.is_dir():
        return [], 0, "acceptance/features not found in project root"
    rows: list[dict] = []
    for path in sorted(features_dir.glob("**/*.feature")):
        key = path.stem
        rel = path.relative_to(project_root).as_posix()
        rows.append(
            {
                "key": key,
                "source": f"res://{rel}",
                "sha256": _sha256_file(path),
            }
        )
    return rows, len(rows), None


LEGAL_SCOPES = frozenset({"gut_dir", "acceptance_feature"})


def _legal_exclusions(exclusions: list[dict]) -> tuple[list[dict], str | None]:
    normalized: list[dict] = []
    for row in exclusions:
        scope = row.get("scope", "")
        entry_text = row.get("entry", "")
        reason = (row.get("reason") or "").strip()
        if not scope or not entry_text:
            return [], (
                f"exclusion missing scope/entry: {row!r}"
            )
        # §SPEC-041 L3: scope is an enum — a typo like `gut_dirs` would
        # silently exclude nothing; fail loud instead
        if scope not in LEGAL_SCOPES:
            return [], (
                f"illegal exclusion scope {scope!r} — "
                f"must be one of {sorted(LEGAL_SCOPES)}"
            )
        if not reason:
            # plan §4.6: an exclusion without a stated reason is an illegal
            # recipe — empty justification must fail loudly, never silently
            # shrink the measured face
            return [], (
                f"exclusion for {entry_text!r} has an empty reason — "
                "exclusions REQUIRE a justification (recipe is illegal "
                "without one)"
            )
        normalized.append(
            {"scope": scope, "entry": entry_text, "reason": reason}
        )
    return normalized, None


def build_recipe(
    project_root: str | Path,
    exclusions: list[dict] | None = None,
    *,
    gqt_version: str = "unknown",
    ops_version: str = "unknown",
    godot_version: str = "unknown",
    gut_version: str = "unknown",
) -> tuple[dict | None, str | None]:
    """Build the canonical-recipe/1 dict; returns (recipe, None) or
    (None, reason) when the project layout or an exclusion is illegal."""
    root = Path(project_root)
    exclusions = exclusions or []
    normalized_exclusions, error = _legal_exclusions(exclusions)
    if error is not None:
        return None, error

    gutconfig, error = load_gutconfig(root)
    if error is not None:
        return None, error

    dirs = gutconfig.get("dirs", [])
    if not dirs:
        return None, "gutconfig has no dirs — empty GUT face is not canonical"

    features, feature_count, error = scan_acceptance_features(root)
    if error is not None:
        return None, error
    if feature_count == 0:
        return None, "acceptance/features has no .feature files"

    harness = {
        "worker": "generated/accept_worker.gd",
        "ops_version": ops_version,
        "wiring_sha256": _sha256_file(
            root / "acceptance" / "harness" / "harness.gd"
        )
        if (root / "acceptance" / "harness" / "harness.gd").is_file()
        else "missing",
    }

    recipe = {
        "recipe_version": RECIPE_VERSION,
        "project_root": str(root),
        "gut_face": {
            "config_source": _GUTCONFIG,
            "config_sha256": _sha256_file(root / _GUTCONFIG),
            # the whole gutconfig (scan keys + exclusions原文) hashes into the
            # recipe — "换 recipe" is an explicit provenance event
            "gutconfig_raw": gutconfig,
            "dirs": dirs,
            "include_subdirs": bool(gutconfig.get("include_subdirs", False)),
            "prefix": gutconfig.get("prefix", "test_"),
            "excluded": [
                {
                    "pattern": x["entry"],
                    "reason": x["reason"],
                }
                for x in normalized_exclusions
                if x["scope"] == "gut_dir"
            ],
        },
        "acceptance_face": {
            "features": features,
            "count": feature_count,
            "excluded": [
                {
                    "key": x["entry"],
                    "reason": x["reason"],
                }
                for x in normalized_exclusions
                if x["scope"] == "acceptance_feature"
            ],
            "harness": harness,
        },
        "toolchain": {
            "gqt_version": gqt_version,
            "ops_version": ops_version,
            "godot_version": godot_version,
            "gut_version": gut_version,
        },
        "exclusions": normalized_exclusions,
    }
    # normalized JSON minus recipe_hash itself → sha256; sort_keys + compact
    # separators give "same input, same hash, any byte change changes it"
    recipe["recipe_hash"] = recipe_hash(recipe)
    return recipe, None


def recipe_hash(recipe: dict) -> str:
    """Recompute the hash over the recipe minus its own recipe_hash AND
    project_root — the checkout path is provenance, not a measured face
    (§SPEC-041 L2: same content at any path must hash identically). The
    canonical verification for loaded/recovered recipes."""
    basis = {k: v for k, v in recipe.items()
             if k not in ("recipe_hash", "project_root")}
    return _sha256_text(
        json.dumps(basis, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"))
    )


def verify_recipe(recipe: dict) -> tuple[bool, str | None]:
    """Verify a loaded recipe: version tag, exclusion reasons, hash match."""
    if recipe.get("recipe_version") != RECIPE_VERSION:
        return False, f"recipe_version mismatch: {recipe.get('recipe_version')}"
    for row in recipe.get("exclusions", []):
        if not (row.get("reason") or "").strip():
            return False, f"exclusion without reason: {row!r}"
    if recipe.get("recipe_hash") != recipe_hash(recipe):
        return False, "recipe_hash mismatch — recipe was modified after hashing"
    return True, None
