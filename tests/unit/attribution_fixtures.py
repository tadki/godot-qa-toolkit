"""SEE-1581 测试辅助：写入"新鲜"运行时归属映射（mutation 真值选面夹具）。

默认写入 `<project>/.qa-cache/attribution-map.json`（truth_select 的约定
默认位置）；conftest 已把 `GQT_ATTRIBUTION_MAP_MODE` 置 off（测试面只读），
新鲜映射仍会被正常消费。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def install_recipe(project_root) -> None:
    """让 canonical recipe 可构建（选面新鲜判据需要）。"""
    project_root = Path(project_root)
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

    空列表 = 干净的"零引用"（映射说无测试 → 零引用短路）。
    SEE-1587：/6 形态——行事实为主真值（每个 (identity, rel) 边合成一行
    事实 [2]），file/function 视图按派生口径随行事实一致。"""
    from godot_qa_toolkit.measurement.recipe import build_recipe, recipe_hash
    install_recipe(project_root)
    recipe, error = build_recipe(project_root)
    assert error is None, error
    raw = recipe_hash({k: v for k, v in recipe.items()
                       if k != "recipe_hash"})
    arm = {"test_to_files": {}, "script_to_files": {},
           "tests": 0, "scripts": 0, "files": 0, "pairs": 0,
           "function_pairs": 0, "test_to_functions": {},
           "script_to_functions": {}, "file_to_tests": {},
           "file_to_scripts": {}, "line_pairs": 0, "script_line_pairs": 0,
           "test_to_lines": {}, "script_to_lines": {}, "line_to_tests": {}}
    shas: dict[str, str] = {}
    instrumentable: dict[str, list[int]] = {}
    for rel, idents in target_tests.items():
        p = Path(project_root) / rel
        if p.is_file():
            shas[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        instrumentable[rel] = [2, 3]
        for ident in idents:
            arm["test_to_files"].setdefault(ident, []).append(rel)
            arm["file_to_tests"].setdefault(rel, []).append(ident)
            arm["test_to_lines"].setdefault(ident, {})[rel] = [2]
            arm["line_to_tests"].setdefault(rel, {})["2"] = sorted(
                set(arm["line_to_tests"].setdefault(rel, {}).get("2", []))
                | {ident})
            arm["line_pairs"] += 1
    arm["tests"] = len(arm["test_to_files"])
    arm["files"] = len(arm["file_to_tests"])
    arm["pairs"] = sum(len(v) for v in arm["test_to_files"].values())
    tests_face = {}
    tests_root = Path(project_root) / "tests"
    if tests_root.is_dir():
        for gd in sorted(tests_root.rglob("*.gd")):
            tests_face[str(gd.relative_to(project_root))] = hashlib.sha256(
                gd.read_bytes()).hexdigest()
    doc = {
        "schema_version": "test-attribution/6",
        "report_recipe_hash": raw,
        "recipe_hash_pristine": raw,
        "project_root": str(project_root),
        "identity": "res://script::test_name",
        "causal": {"enabled": False},
        "file_sha256": shas,
        "target_files": sorted(shas),
        "tests_face": tests_face,
        "instrumentable_lines": instrumentable,
        "arms": {"gut_full": arm},
        # SEE-1587 D3：真实 /6 产物携带执行完整性证据；零引用（pairs=0）
        # 属合法空面——probes_fired>0 证明测试确已执行。
        "arms_state": {"gut_full": {"state": "valid",
                                    "invalid_reason": None,
                                    "bundles": 1,
                                    "pairs": len(arm["test_to_files"]),
                                    "probes_fired": 640,
                                    "vacuous": False}},
        "notes": [],
    }
    out = Path(project_root) / ".qa-cache" / "attribution-map.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return str(out)


def line_facts_map(project_root: Path,
                   facts: dict[str, dict[str, list[int]]], *,
                   instrumentable: dict[str, list[int]] | None = None,
                   probes: int = 640,
                   path: str | Path | None = None) -> str:
    """SEE-1594 S2：可配置行事实映射（完整 res:// 身份 → {rel: 行集}）。

    instrumentable：rel → 可插桩行集（缺省 [2, 3]）；probes：执行证据。
    写合法 test-attribution/6 到指定路径（缺省约定默认位置），返回路径。"""
    from godot_qa_toolkit.measurement.recipe import build_recipe, recipe_hash
    project_root = Path(project_root)
    install_recipe(project_root)
    recipe, error = build_recipe(project_root)
    assert error is None, error
    raw = recipe_hash({k: v for k, v in recipe.items()
                       if k != "recipe_hash"})
    rels = sorted({r for by_rel in facts.values() for r in by_rel}
                  | set(instrumentable or {}))
    shas = {rel: hashlib.sha256((project_root / rel).read_bytes())
            .hexdigest() for rel in rels if (project_root / rel).is_file()}
    arm = {"test_to_files": {}, "script_to_files": {}, "tests": 0,
           "scripts": 0, "files": len(rels), "pairs": 0, "function_pairs": 0,
           "test_to_functions": {}, "script_to_functions": {},
           "file_to_tests": {rel: [] for rel in rels},
           "file_to_scripts": {rel: [] for rel in rels},
           "line_pairs": 0, "script_line_pairs": 0, "test_to_lines": {},
           "script_to_lines": {}, "line_to_tests": {}}
    for ident, by_rel in facts.items():
        for rel, lines in by_rel.items():
            arm["test_to_files"].setdefault(ident, []).append(rel)
            arm["file_to_tests"].setdefault(rel, [])
            if ident not in arm["file_to_tests"][rel]:
                arm["file_to_tests"][rel].append(ident)
            arm["test_to_lines"].setdefault(ident, {})[rel] = list(lines)
            arm["line_pairs"] += len(lines)
            for line in lines:
                bucket = arm["line_to_tests"].setdefault(rel, {}) \
                    .setdefault(str(line), [])
                if ident not in bucket:
                    bucket.append(ident)
    arm["tests"] = len(arm["test_to_files"])
    arm["pairs"] = sum(len(v) for v in arm["test_to_files"].values())
    doc = {
        "schema_version": "test-attribution/6",
        "report_recipe_hash": raw, "recipe_hash_pristine": raw,
        "project_root": str(project_root),
        "identity": "res://script::test_name",
        "causal": {"enabled": False},
        "file_sha256": shas,
        "target_files": sorted(shas),
        "tests_face": _tests_face_of(project_root),
        "instrumentable_lines": instrumentable or {rel: [2, 3]
                                                   for rel in rels},
        "arms": {"gut_full": arm},
        "arms_state": {"gut_full": {"state": "valid", "invalid_reason": None,
                                    "bundles": 1, "pairs": arm["pairs"],
                                    "probes_fired": probes, "vacuous": False}},
        "notes": [],
    }
    out = Path(path) if path is not None else (
        Path(project_root) / ".qa-cache" / "attribution-map.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return str(out)


def _tests_face_of(project_root: Path) -> dict[str, str]:
    tests_root = Path(project_root) / "tests"
    if not tests_root.is_dir():
        return {}
    return {str(p.relative_to(project_root)): hashlib.sha256(
        p.read_bytes()).hexdigest()
        for p in sorted(tests_root.rglob("*.gd"))}


def seed_projection(project_root: Path, target: Path,
                    facts: dict[str, dict[str, list[int]]], *,
                    scripts: list[str], instrumentable: dict[str, list[int]],
                    tests_glob: str = "res://tests/",
                    identity=None) -> str:
    """SEE-1594 S2：经生产写侧落 /2 派生缓存条目（绑定映射内容与执行
    证据；A04 后 prefilter store 层的唯一合法种子方式）。返回映射路径。"""
    from godot_qa_toolkit.mutation import coverlink, truth_select
    map_file = line_facts_map(project_root, facts,
                              instrumentable=instrumentable)
    doc = json.loads(Path(map_file).read_text(encoding="utf-8"))
    pristine = truth_select._fresh_recipe_hash(str(project_root))
    covered, why = coverlink.project_coverage_from_map(
        str(project_root), doc, str(target), scripts, tests_glob,
        identity=identity, current_recipe_pristine=pristine)
    assert covered is not None, why
    return map_file
