"""SEE-1594 BCODE §SPEC-011 等价证据：digest 单点 helper 与旧内联口径同值。

- canonical_sha256 == sha256(json.dumps(sort_keys, compact separators))；
- recipe_hash（basis）与旧公式逐值相等（真实 recipe 实测）；
- sha256_file == 旧 read_bytes 口径（分块实现同值）；
- coverage.evidence.sha256_text 委派后与 digest.sha256_text 同值。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.digest import (
    canonical_json,
    canonical_sha256,
    sha256_file,
    sha256_text,
)


def test_canonical_sha256_equals_old_inline_formula():
    obj = {"b": [1, 2, {"z": "中文"}], "a": None, "n": 1.5}
    old = hashlib.sha256(json.dumps(
        obj, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    assert canonical_sha256(obj) == old
    assert sha256_text(canonical_json(obj)) == old


def test_sha256_text_equals_old_inline():
    text = "abc\ndef 中文"
    assert sha256_text(text) == hashlib.sha256(
        text.encode("utf-8")).hexdigest()


def test_sha256_file_equals_read_bytes(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"abc" * 400_000)  # 多块，验证分块实现同值
    assert sha256_file(p) == hashlib.sha256(p.read_bytes()).hexdigest()


def test_recipe_hash_equals_old_basis_formula(tmp_path):
    from godot_qa_toolkit.measurement.recipe import build_recipe
    tmp = tmp_path / "proj"
    (tmp / "addons" / "gut").mkdir(parents=True)
    (tmp / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    (tmp / "project.godot").write_text("config_version=5\n")
    (tmp / ".gutconfig.json").write_text(
        json.dumps({"dirs": ["res://tests/"]}))
    (tmp / "tests").mkdir()
    (tmp / "tests" / "test_a.gd").write_text(
        "extends GutTest\nfunc t():\n\tpass\n")
    feats = tmp / "addons" / "gqt_gherkin" / "features"
    feats.mkdir(parents=True)
    (feats / "demo.feature").write_text(
        "Feature: F\n  Scenario Outline: s\n    Then hp is <left>\n"
        "    # types: <hp>:int, <left>:numeric\n    Examples:\n"
        "      | hp | left |\n      | 10 | 7    |\n")
    from gherkin_fixtures import ensure_addon_metadata
    ensure_addon_metadata(tmp)
    recipe, err = build_recipe(tmp)
    assert err is None, err
    basis = {k: v for k, v in recipe.items()
             if k not in ("recipe_hash", "project_root")}
    old = hashlib.sha256(json.dumps(
        basis, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    assert recipe["recipe_hash"] == old


def test_evidence_sha256_text_delegates_to_digest():
    from godot_qa_toolkit.coverage.evidence import sha256_text as ev_text
    assert ev_text("payload") == sha256_text("payload")


def test_digest_is_single_implementation():
    """四类摘要惯用式实现只存于 digest.py（与 governance --mode duplicates
    同口径的单元级钉）。"""
    src = Path(__file__).resolve().parents[2] / "src" / "godot_qa_toolkit"
    import re as _re
    pat = _re.compile(r"hashlib\.sha256\(\s*[^()\n]*\.encode\(")
    sites = [p for p in src.rglob("*.py")
             if pat.search(p.read_text(encoding="utf-8"))]
    assert [p.name for p in sites] == ["digest.py"], sites
