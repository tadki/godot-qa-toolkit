"""SEE-1546 WP3 PR-C — coverage-union/1 契约单测（§SPEC-036）。

判据先行（TDD RED）：有效性矩阵逐条（同源码 sha256、同 probe manifest 哈希、
两臂 run_uuid 不同、两臂 bundle 均 valid、整臂替换禁拼接、**禁平均**
coverage=|∪C|/total 反向探针钉死）；comparable 仅 recipe_hash 与 canonical
一致时 true；arm_record_from_bundle 从 coverage-evidence/2 bundle 归一化。
"""

from __future__ import annotations

import pytest

from godot_qa_toolkit.coverage.evidence import build_bundle
from godot_qa_toolkit.coverage.union import (
    UNION_VERSION,
    arm_record_from_bundle,
    union_file,
)

GD_SRC = """extends RefCounted


func alpha() -> int:
\tvar x = 1
\treturn x


func beta() -> int:
\treturn 3
"""


def _bundle(*, run_uuid: str, arm: str, hits: list[int],
            source: str = GD_SRC) -> dict:
    # hits 传 probe id（0 基）——经 manifest 映射为行号再进 bundle
    # （与 runner._read_hits 的 id→line 语义一致）
    from godot_qa_toolkit.registry.functions import build_file_registry
    from godot_qa_toolkit.registry.manifest import build_probe_manifest_v2
    registry = build_file_registry(source, file_label="res://src_game/cell.gd")
    manifest = build_probe_manifest_v2(source, registry)
    plan = [p["line"] for p in manifest["probe_plan"]]
    hit_lines = [plan[i] for i in hits if i < len(plan)]
    return build_bundle(
        run_uuid=run_uuid, file_path="res://src_game/cell.gd",
        gd_source=source, tests_glob=None,
        registry=registry, probe_manifest=manifest,
        hits_lines=sorted(hit_lines), probes_fired=len(hits),
        gut_ok=True, run_error=False, timeout=False, zero_tests=False,
        vacuous=False, arm=arm,
    )


def _arms(hits_a: list[int], hits_b: list[int],
          *, arm_b: str = "acceptance", source_b: str = GD_SRC,
          recipe_hash: str = "c" * 64) -> tuple[dict, dict]:
    a = arm_record_from_bundle(
        _bundle(run_uuid="uuid-a", arm="gut_full", hits=hits_a),
        recipe_hash=recipe_hash, wall_s=1.0)
    b = arm_record_from_bundle(
        _bundle(run_uuid="uuid-b", arm=arm_b, hits=hits_b,
                source=source_b),
        recipe_hash=recipe_hash, wall_s=2.0)
    return a, b


def test_union_version_and_happy_shape():
    a, b = _arms([0, 1, 2], [0])
    rec = union_file(a, b, canonical_recipe_hash="c" * 64)
    assert rec["union_version"] == UNION_VERSION
    assert rec["valid"] is True
    assert rec["invalid_reason"] is None
    assert rec["union"]["method"] == "line_union"
    assert [arm["arm"] for arm in rec["arms"]] == ["gut_full", "acceptance"]
    assert rec["arms"][0]["run_uuid"] == "uuid-a"
    assert rec["arms"][1]["run_uuid"] == "uuid-b"


def test_union_is_line_set_union_never_average():
    """禁平均反向探针：A 独中 1 行 + B 独中 1 行 → |∪C|=2；两臂平均
    (1/3+1/3)/2=33% 恒不等于 2/3——实现取平均即此 fixture 红。"""
    a, b = _arms([0], [1])
    rec = union_file(a, b, canonical_recipe_hash="c" * 64)
    assert rec["valid"] is True
    # 3 个探针行，∪C = {line0, line1} → 2/3（≈66.67，非平均 33.33）
    assert rec["union"]["covered"] == 2
    assert rec["union"]["total"] == 3
    ids = set()
    for row in rec["union"]["per_fid"]:
        ids |= set(row["covered_line_ids"])
    assert len(ids) == 2
    average_would_be = (1 / 3 + 1 / 3) / 2
    assert rec["union"]["covered"] / rec["union"]["total"] != pytest.approx(
        average_would_be)


def test_union_disjoint_arms_full_cover():
    a, b = _arms([0], [1, 2])
    rec = union_file(a, b, canonical_recipe_hash="c" * 64)
    assert rec["union"]["covered"] == 3


def test_source_sha256_mismatch_invalid():
    a, b = _arms([0], [0], source_b=GD_SRC + "\n\nfunc gamma() -> int:\n\treturn 4\n")
    rec = union_file(a, b)
    assert rec["valid"] is False
    assert "sha256" in rec["invalid_reason"]


def test_probe_manifest_mismatch_invalid():
    # 同源码但 B 臂 manifest 哈希漂移（重建痕迹）→ invalid
    a, b = _arms([0], [0])
    b["probe_manifest_sha256"] = "f" * 64
    rec = union_file(a, b)
    assert rec["valid"] is False
    assert "manifest" in rec["invalid_reason"]


def test_run_uuid_reuse_invalid():
    a, b = _arms([0], [1])
    b["run_uuid"] = a["run_uuid"]  # 复用/拼接痕迹
    rec = union_file(a, b)
    assert rec["valid"] is False
    assert "run_uuid" in rec["invalid_reason"]


def test_arm_invalid_state_blocks_union():
    bad = _bundle(run_uuid="uuid-b", arm="acceptance", hits=[])
    bad["sentinels"]["run_error"] = True
    b = arm_record_from_bundle(bad, recipe_hash="c" * 64)
    a, _ = _arms([0], [0])
    rec = union_file(a, b, canonical_recipe_hash="c" * 64)
    assert rec["valid"] is False
    assert "acceptance" in rec["invalid_reason"]


def test_duplicate_arm_names_invalid():
    a, b = _arms([0], [1], arm_b="gut_full")  # 两臂同名 = 拼接痕迹
    rec = union_file(a, b)
    assert rec["valid"] is False
    assert "arm" in rec["invalid_reason"]


def test_no_probe_fired_is_invalid_lane():
    zero = _bundle(run_uuid="uuid-b", arm="acceptance", hits=[])
    zero["sentinels"]["probes_fired"] = 0
    b = arm_record_from_bundle(zero, recipe_hash="c" * 64)
    assert b["state"] == "invalid"
    assert "probe" in b["state_reason"]
    a, _ = _arms([0], [0])
    rec = union_file(a, b)
    assert rec["valid"] is False


def test_comparable_only_when_recipe_hash_matches_canonical():
    a, b = _arms([0], [1], recipe_hash="d" * 64)
    rec = union_file(a, b, canonical_recipe_hash="c" * 64)
    assert rec["valid"] is True
    assert rec["comparable"] is False  # 子集配方 ≠ canonical → false
    rec2 = union_file(a, b, canonical_recipe_hash="d" * 64)
    assert rec2["comparable"] is True
    rec3 = union_file(a, b)  # 无 canonical 基准 → 无法宣称 comparable
    assert rec3["comparable"] is False


def test_per_fid_rows_carry_covered_line_ids():
    a, b = _arms([0, 1], [2])
    rec = union_file(a, b, canonical_recipe_hash="c" * 64)
    by_fid = {r["fid"]: r for r in rec["union"]["per_fid"]}
    alpha = by_fid.get("res://src_game/cell.gd::alpha") or {}
    assert alpha.get("covered") == 2
    beta = by_fid.get("res://src_game/cell.gd::beta") or {}
    assert beta.get("covered") == 1


def test_arm_record_fields_from_bundle():
    bundle = _bundle(run_uuid="uuid-a", arm="gut_full", hits=[0, 2])
    rec = arm_record_from_bundle(bundle, recipe_hash="c" * 64, wall_s=1.5)
    assert rec["arm"] == "gut_full"
    assert rec["run_uuid"] == "uuid-a"
    assert rec["state"] == "valid"
    assert rec["wall_s"] == 1.5
    assert rec["target_sha256"] == bundle["target"]["sha256"]
    assert rec["probes_total"] == 3
    assert len(rec["probe_manifest_sha256"]) == 64
    # alpha 覆盖 2 行（ids 0,2 → manifest 前 3 行属 alpha）
    cov = rec["C"]["res://src_game/cell.gd::alpha"]
    assert cov == bundle["C"][0]["covered_line_ids"]
