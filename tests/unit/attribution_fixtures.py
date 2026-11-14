"""SEE-1581 测试辅助：写入"新鲜"运行时归属映射（mutation 真值选面夹具）。

默认写入 `<project>/.qa-cache/attribution-map.json`（truth_select 的约定
默认位置）；conftest 已把 `GQT_ATTRIBUTION_MAP_MODE` 置 off（测试面只读），
新鲜映射仍会被正常消费。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def install_recipe(project_root: Path) -> None:
    """让 canonical recipe 可构建（选面新鲜判据需要）。"""
    (project_root / "tests").mkdir(parents=True, exist_ok=True)
    feats = project_root / "acceptance" / "features"
    feats.mkdir(parents=True, exist_ok=True)
    demo = feats / "demo.feature"
    if not demo.is_file():
        demo.write_text(
            "Feature: F\n  Scenario Outline: s\n    Then hp is <left>\n"
            "    # types: <hp>:int, <left>:numeric\n    Examples:\n"
            "      | hp | left |\n      | 10 | 7    |\n", encoding="utf-8")
    cfg = project_root / ".gutconfig.json"
    if not cfg.is_file():
        cfg.write_text(json.dumps({
            "dirs": ["res://tests/"], "include_subdirs": True,
            "prefix": "test_"}), encoding="utf-8")


def write_map(project_root: Path,
              target_tests: dict[str, list[str]]) -> str:
    """target_tests: 目标文件 rel → 触达它的测试身份列表（`脚本名::测试名`）。

    空列表 = 干净的"零引用"（映射说无测试 → 零引用短路）。"""
    from godot_qa_toolkit.canonical.recipe import build_recipe, recipe_hash
    install_recipe(project_root)
    recipe, error = build_recipe(project_root)
    assert error is None, error
    raw = recipe_hash({k: v for k, v in recipe.items()
                       if k != "recipe_hash"})
    arm = {"test_to_files": {}, "script_to_files": {},
           "tests": 0, "scripts": 0, "files": 0, "pairs": 0,
           "function_pairs": 0, "test_to_functions": {},
           "script_to_functions": {}, "file_to_tests": {},
           "file_to_scripts": {}}
    shas: dict[str, str] = {}
    for rel, idents in target_tests.items():
        p = Path(project_root) / rel
        if p.is_file():
            shas[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        for ident in idents:
            arm["test_to_files"].setdefault(ident, []).append(rel)
            arm["file_to_tests"].setdefault(rel, []).append(ident)
    tests_face = {}
    tests_root = Path(project_root) / "tests"
    if tests_root.is_dir():
        for gd in sorted(tests_root.rglob("*.gd")):
            tests_face[str(gd.relative_to(project_root))] = hashlib.sha256(
                gd.read_bytes()).hexdigest()
    doc = {
        "schema_version": "canonical-test-attribution/4",
        "report_recipe_hash": raw,
        "recipe_hash_pristine": raw,
        "project_root": str(project_root),
        "identity": "res://script::test_name",
        "causal": {"enabled": False},
        "file_sha256": shas,
        "target_files": sorted(shas),
        "tests_face": tests_face,
        "arms": {"gut_full": arm},
        "arms_state": {"gut_full": {"state": "valid",
                                    "invalid_reason": None,
                                    "bundles": 1,
                                    "pairs": len(arm["test_to_files"])}},
        "notes": [],
    }
    out = Path(project_root) / ".qa-cache" / "attribution-map.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return str(out)
