"""SEE-1546 WP3 PR-A hardener: canonical recipe builder + cmd_recipe error
lanes (§SPEC-034). Test-side only; product code untouched."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from godot_qa_toolkit import cli as cli_mod
from godot_qa_toolkit.cli import build_parser
from godot_qa_toolkit.canonical.recipe import (
    _GUTCONFIG,
    build_recipe,
    load_gutconfig,
    verify_recipe,
)

GUTCONFIG = {
    "dirs": ["res://tests/unit/", "res://tests/integration/"],
    "include_subdirs": True,
    "prefix": "test_",
}


@pytest.fixture
def kol_like(tmp_path: Path) -> Path:
    root = tmp_path / "kol"
    root.mkdir()
    (root / _GUTCONFIG).write_text(
        json.dumps(GUTCONFIG, indent=1), encoding="utf-8"
    )
    features = root / "acceptance" / "features"
    features.mkdir(parents=True)
    for i in range(3):
        (features / f"feature_{i}.feature").write_text(
            f"Feature: f{i}\n  Scenario: s{i}\n    Given a\n", encoding="utf-8"
        )
    return root


@pytest.fixture
def kol_like(tmp_path: Path) -> Path:
    root = tmp_path / "kol"
    root.mkdir()
    (root / _GUTCONFIG).write_text(
        json.dumps(GUTCONFIG, indent=1), encoding="utf-8"
    )
    features = root / "acceptance" / "features"
    features.mkdir(parents=True)
    for i in range(3):
        (features / f"feature_{i}.feature").write_text(
            f"Feature: f{i}\n  Scenario: s{i}\n    Given a\n", encoding="utf-8"
        )
    return root


def _write_harness(root: Path) -> None:
    h = root / "acceptance" / "harness"
    h.mkdir(parents=True, exist_ok=True)
    (h / "harness.gd").write_text("# harness\n", encoding="utf-8")


# --------------------------------------------------------------------------
# recipe.py defensive lanes
# --------------------------------------------------------------------------


def test_load_gutconfig_reports_parse_failure(tmp_path):
    (tmp_path / _GUTCONFIG).write_text("{not json", encoding="utf-8")
    config, error = load_gutconfig(tmp_path)
    assert config is None
    assert error is not None and "parse failed" in error


def test_load_gutconfig_reports_non_object(tmp_path):
    (tmp_path / _GUTCONFIG).write_text("[1, 2]", encoding="utf-8")
    config, error = load_gutconfig(tmp_path)
    assert config is None
    assert error is not None and "not a JSON object" in error


def test_build_recipe_rejects_exclusion_missing_scope_or_entry(kol_like):
    recipe, error = build_recipe(
        kol_like, exclusions=[{"scope": "", "entry": "x", "reason": "r"}]
    )
    assert recipe is None
    assert error is not None and "missing scope/entry" in error

    recipe, error = build_recipe(
        kol_like, exclusions=[{"scope": "gut_dir", "entry": "", "reason": "r"}]
    )
    assert recipe is None
    assert error is not None and "missing scope/entry" in error


def test_build_recipe_rejects_empty_gutconfig_dirs(kol_like):
    (kol_like / _GUTCONFIG).write_text(
        json.dumps({"dirs": []}), encoding="utf-8"
    )
    recipe, error = build_recipe(kol_like, exclusions=[])
    assert recipe is None
    assert error is not None and "empty GUT face" in error


def test_build_recipe_rejects_zero_features(kol_like):
    for f in (kol_like / "acceptance" / "features").glob("*.feature"):
        f.unlink()
    recipe, error = build_recipe(kol_like, exclusions=[])
    assert recipe is None
    assert error is not None and "no .feature files" in error


def test_build_recipe_hashes_missing_harness_as_missing(kol_like):
    recipe, error = build_recipe(kol_like, exclusions=[])
    assert error is None
    assert recipe["acceptance_face"]["harness"]["wiring_sha256"] == "missing"


def test_verify_recipe_rejects_version_mismatch(kol_like):
    recipe, error = build_recipe(kol_like, exclusions=[])
    assert error is None
    recipe["recipe_version"] = "canonical-recipe/0"
    ok, reason = verify_recipe(recipe)
    assert ok is False
    assert "recipe_version mismatch" in reason


def test_verify_recipe_rejects_reasonless_exclusion(kol_like):
    recipe, error = build_recipe(
        kol_like,
        exclusions=[{"scope": "gut_dir", "entry": "res://tests/old/",
                     "reason": "retired face"}],
    )
    assert error is None
    recipe["exclusions"][0]["reason"] = "   "
    ok, reason = verify_recipe(recipe)
    assert ok is False
    assert "exclusion without reason" in reason


def test_verify_recipe_rejects_hash_mismatch(kol_like):
    recipe, error = build_recipe(kol_like, exclusions=[])
    assert error is None
    recipe["gut_face"]["dirs"] = ["res://tampered/"]
    ok, reason = verify_recipe(recipe)
    assert ok is False
    assert "recipe_hash mismatch" in reason


# --------------------------------------------------------------------------
# cmd_recipe CLI lanes (in-process)
# --------------------------------------------------------------------------


def _parse(*argv):
    return build_parser().parse_args(list(argv))


def test_cmd_recipe_rejects_malformed_exclude(kol_like, capsys):
    target = kol_like
    args = _parse("recipe", "--project-root", str(target),
                  "--exclude", "not-three-parts")
    rc = cli_mod.cmd_recipe(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["ok"] is False and out["tool"] == "recipe"
    assert "scope:entry:reason" in out["summary"]["error"]


def test_cmd_recipe_build_error_is_structural(tmp_path, capsys):
    args = _parse("recipe", "--project-root", str(tmp_path))
    rc = cli_mod.cmd_recipe(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["ok"] is False and out["tool"] == "recipe"
    assert ".gutconfig.json not found" in out["summary"]["error"]


def test_cmd_recipe_success_writes_recipe_and_summary(tmp_path, kol_like, capsys):
    root = kol_like
    _write_harness(root)
    out_path = tmp_path / "recipe.json"
    args = _parse(
        "recipe", "--project-root", str(root),
        "--out", str(out_path),
        "--exclude", "gut_dir:tests/old/:retired face",
        "--exclude", "acceptance_feature:feature_2:quarantined",
    )
    rc = cli_mod.cmd_recipe(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] is True and out["tool"] == "recipe"
    assert out["summary"]["exclusions"] == 2
    assert out["summary"]["gut_dirs"] == 2
    assert out["summary"]["acceptance_features"] == 3
    recipe = json.loads(out_path.read_text(encoding="utf-8"))
    assert recipe["recipe_hash"] == out["summary"]["recipe_hash"]
    assert len(recipe["exclusions"]) == 2
    assert recipe["gut_face"]["excluded"][0]["pattern"] == "tests/old/"
    assert recipe["acceptance_face"]["excluded"][0]["key"] == "feature_2"
    # hash roundtrip on the landed file
    from godot_qa_toolkit.canonical.recipe import recipe_hash, verify_recipe
    assert recipe["recipe_hash"] == recipe_hash(recipe)
    ok, reason = verify_recipe(recipe)
    assert ok is True and reason is None




def test_gutconfig_scan_keys_are_carried_verbatim(kol_like):
    """GC2/GC3: include_subdirs and prefix are live scan keys — the recipe
    must carry the gutconfig's values verbatim (never pinned defaults)."""
    from godot_qa_toolkit.canonical.recipe import build_recipe

    recipe, error = build_recipe(kol_like, exclusions=[])
    assert error is None
    assert recipe["gut_face"]["include_subdirs"] is True
    assert recipe["gut_face"]["prefix"] == "test_"

    flipped = dict(GUTCONFIG, include_subdirs=False, prefix="spec_")
    (kol_like / _GUTCONFIG).write_text(
        json.dumps(flipped, indent=1), encoding="utf-8"
    )
    recipe, error = build_recipe(kol_like, exclusions=[])
    assert error is None
    assert recipe["gut_face"]["include_subdirs"] is False
    assert recipe["gut_face"]["prefix"] == "spec_"


def test_gutconfig_config_sha256_ties_bytes_to_recipe(kol_like):
    """H2: the gutconfig file's own sha256 is hashed into the recipe —
    a constant digest would break the byte-level provenance chain."""
    import hashlib

    from godot_qa_toolkit.canonical.recipe import build_recipe

    cfg_path = kol_like / _GUTCONFIG
    expected = hashlib.sha256(cfg_path.read_bytes()).hexdigest()
    recipe, error = build_recipe(kol_like, exclusions=[])
    assert error is None
    assert recipe["gut_face"]["config_sha256"] == expected


def test_cmd_recipe_rejects_illegal_scope_fail_loud(kol_like, capsys):
    """§SPEC-041 target 1 (CLI-side anchor): an illegal exclusion scope (e.g.
    `gut_dirs`) passed through cmd_recipe's REAL path must produce ok=false
    with an enumeration hint — never silently accepted into the recipe."""
    args = _parse(
        "recipe", "--project-root", str(kol_like),
        "--exclude", "gut_dirs:res://tests/x/**:reason",
    )
    rc = cli_mod.cmd_recipe(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["ok"] is False and out["tool"] == "recipe"
    assert "illegal exclusion scope" in out["summary"]["error"]
    assert "must be one of" in out["summary"]["error"]


def test_cmd_recipe_legal_scope_still_accepted(kol_like, tmp_path, capsys):
    """§SPEC-041 companion: the legal-scope success path is unaffected by the
    enumeration gate."""
    out_path = tmp_path / "recipe.json"
    args = _parse(
        "recipe", "--project-root", str(kol_like),
        "--exclude", "gut_dir:tests/old/:retired face",
        "--out", str(out_path),
    )
    rc = cli_mod.cmd_recipe(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] is True
    recipe = json.loads(out_path.read_text(encoding="utf-8"))
    assert len(recipe["exclusions"]) == 1
    assert recipe["exclusions"][0]["scope"] == "gut_dir"
