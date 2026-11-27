"""SEE-1651 C3-R24：增量层身份口径统一（§SPEC-038，决策 16）。

C3-R23 复核退回项 N1–N4 的回归钉（判据 §SPEC-038 场景矩阵）：

- N1（HIGH）：删除车道按**完整路径身份**——被删身份的边与判定剔除，
  幸存者（含同名幸存者）逐位不变；`validate_refresh` 的守恒豁免按完整
  路径匹配，规范身份下同名豁免不得掩盖幸存者丢失；升级过渡（旧
  basename 形态基线）删除后判定表单一形态、幸存者判定保留、无法消歧
  旧条目具名披露（场景 ②③⑤）。

单元红测试在 972dcc4 的产品代码上为红（各用例 docstring 标明红因）；
真机段（真实 Godot + GUT）覆盖场景 ②③④⑥ 的端到端成立，无真机时
按仓库既有惯例 skip。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import test_c3r23_same_name_increment_see1648 as _v1

A_IDENT = _v1.A_IDENT
B_IDENT = _v1.B_IDENT
TEST_NAME = "test_sign_positive"
LEGACY_IDENT = f"test_green.gd::{TEST_NAME}"


def _face(rels: list[str]) -> dict:
    import hashlib

    return {rel: hashlib.sha256(rel.encode()).hexdigest() for rel in rels}


def _canonical_baseline() -> dict:
    return {"test_verdicts": {
        A_IDENT: {"verdict": "green"},
        B_IDENT: {"verdict": "red", "classification": "pre_existing",
                  "script": "res://tests/b/test_green.gd"},
    }}


def _legacy_baseline() -> dict:
    return {"test_verdicts": {LEGACY_IDENT: {"verdict": "green"}}}


def _arms_state() -> dict:
    return {"gut_full": {"state": "valid", "invalid_reason": None,
                         "bundles": 1, "pairs": 2,
                         "probes_fired": 640, "vacuous": False}}


# ───────────────── N1：删除车道按完整路径身份（§SPEC-038 ②③⑤） ─────────────────
def test_n1_delete_one_same_name_verdict_survivor_intact():
    """②判定车道：同名两脚本删其一（删 a）→ 被删身份剔除、幸存者判定
    逐位不变（972dcc4 上 dropped 按 basename → 双身份全空，本测为红）。"""
    from godot_qa_toolkit.measurement.increment import _merged_test_verdicts

    merged = _merged_test_verdicts(
        _canonical_baseline(), [], set(), {"tests/a/test_green.gd"}, set(),
        None, set(), set(),
        tests_face=_face(["tests/b/test_green.gd"]),
        arm_identities=[A_IDENT, B_IDENT], collisions=[])
    assert merged is not None
    assert merged.get(B_IDENT, {}).get("verdict") == "red", merged
    assert merged.get(B_IDENT, {}).get("classification") == "pre_existing", \
        merged
    assert merged.get(B_IDENT, {}).get("script") == \
        "res://tests/b/test_green.gd", merged
    assert A_IDENT not in merged, sorted(merged)


def test_n1_delete_one_same_name_view_edges_survivor_intact():
    """②边车道：删除按完整路径只清被删身份的边，同名幸存者边原样保留
    （972dcc4 上按 basename 清边 → 双身份边全清；rel 形态不匹配 → 谁都
    不清——两方向都破坏守恒，本测为红）。"""
    from godot_qa_toolkit.measurement.increment import _merge_arm

    line = {"calc_sign.gd": [3, 4]}
    arm = {"test_to_lines": {A_IDENT: dict(line), B_IDENT: dict(line)},
           "script_to_lines": {}}
    merged = _merge_arm(arm, {}, {"tests/a/test_green.gd"})
    got = merged.get("test_to_lines") or {}
    assert got.get(B_IDENT) == line, sorted(got)
    assert A_IDENT not in got, sorted(got)


def test_n1_normal_delete_drops_exact_identity_and_keeps_others():
    """③普通删除（无同名）：被删身份的判定剔除、其余身份不受影响
    （与同名删除同一完整路径口径）。"""
    from godot_qa_toolkit.measurement.increment import _merged_test_verdicts

    only = "res://tests/fill/test_calc_00.gd::test_add_00"
    baseline = {"test_verdicts": {
        only: {"verdict": "green"},
        A_IDENT: {"verdict": "green"},
        B_IDENT: {"verdict": "red", "classification": "pre_existing",
                  "script": "res://tests/b/test_green.gd"},
    }}
    merged = _merged_test_verdicts(
        baseline, [], set(), {"tests/fill/test_calc_00.gd"}, set(),
        None, set(), set(),
        tests_face=_face(["tests/a/test_green.gd", "tests/b/test_green.gd"]),
        arm_identities=[only, A_IDENT, B_IDENT], collisions=[])
    assert only not in merged, sorted(merged)
    assert merged.get(A_IDENT, {}).get("verdict") == "green", merged
    assert merged.get(B_IDENT, {}).get("verdict") == "red", merged


def test_n1_validate_refresh_accepts_full_path_deletion():
    """③守恒校验：规范身份基线删除脚本后续新——dropped 完整路径必须
    能豁免被删身份，正常删除不得被判 refresh_lost_identities 拒绝
    （972dcc4 上 basename 减不掉完整路径身份集 → 拒绝，本测为红）。"""
    from godot_qa_toolkit.mutation.truth_select import validate_refresh

    before = {"schema_version": "test-attribution/7",
              "arms": {"gut_full": {"test_to_files": {A_IDENT: ["prod.gd"],
                                                      B_IDENT: ["prod.gd"]}}},
              "arms_state": _arms_state(),
              "test_verdicts": {}}
    after = {"schema_version": "test-attribution/7",
             "arms": {"gut_full": {"test_to_files": {B_IDENT: ["prod.gd"]}}},
             "arms_state": _arms_state(),
             "test_verdicts": {},
             "increment": {"rerun_scripts": [],
                           "dropped_scripts": ["tests/a/test_green.gd"]}}
    validate_refresh(before, after)


def test_n1_validate_refresh_still_rejects_survivor_loss():
    """守恒豁免的负向钉：同名删除后**幸存者**身份也丢（合并缺陷）→
    仍必须拒绝——完整路径豁免不得外溢到同名幸存者（修后必须保持红）。"""
    from godot_qa_toolkit.mutation.truth_select import (
        MutationSelectionError, validate_refresh,
    )

    before = {"schema_version": "test-attribution/7",
              "arms": {"gut_full": {"test_to_files": {A_IDENT: ["prod.gd"],
                                                      B_IDENT: ["prod.gd"]}}},
              "arms_state": _arms_state(),
              "test_verdicts": {}}
    after = {"schema_version": "test-attribution/7",
             "arms": {"gut_full": {"test_to_files": {}}},
             "arms_state": _arms_state(),
             "test_verdicts": {},
             "increment": {"rerun_scripts": [],
                           "dropped_scripts": ["tests/a/test_green.gd"]}}
    with pytest.raises(MutationSelectionError) as ei:
        validate_refresh(before, after)
    assert "refresh_lost_identities" in str(ei.value), str(ei.value)


def test_n1_upgrade_transition_delete_unique_survivor_canonicalized():
    """⑤升级过渡（旧 basename 形态基线）删其一：唯一幸存者 → 判定表
    改写规范形态、幸存者判定保留、改写具名披露（972dcc4 上按 basename
    整条删除 → 判定丢失，本测为红）。"""
    from godot_qa_toolkit.measurement.increment import _merged_test_verdicts

    collisions: list = []
    merged = _merged_test_verdicts(
        _legacy_baseline(), [], set(), {"tests/a/test_green.gd"}, set(),
        None, set(), set(),
        tests_face=_face(["tests/b/test_green.gd"]),
        arm_identities=[LEGACY_IDENT], collisions=collisions)
    canon = f"res://tests/b/test_green.gd::{TEST_NAME}"
    assert merged.get(canon, {}).get("verdict") == "green", merged
    assert LEGACY_IDENT not in merged, sorted(merged)
    assert any(c.get("reason") ==
               "legacy_basename_identity_canonicalized_after_delete"
               for c in collisions), collisions


def test_n1_upgrade_transition_delete_ambiguous_survivors_disclosed():
    """⑤无法消歧（同名多目录删其一，仍有多个幸存者）→ 旧条目保留 +
    具名披露，不得静默丢弃或猜改（972dcc4 上按 basename 整条删除，
    本测为红）。"""
    from godot_qa_toolkit.measurement.increment import _merged_test_verdicts

    collisions: list = []
    merged = _merged_test_verdicts(
        _legacy_baseline(), [], set(), {"tests/a/test_green.gd"}, set(),
        None, set(), set(),
        tests_face=_face(["tests/b/test_green.gd", "tests/c/test_green.gd"]),
        arm_identities=[LEGACY_IDENT], collisions=collisions)
    assert merged.get(LEGACY_IDENT, {}).get("verdict") == "green", merged
    assert any(c.get("reason") ==
               "legacy_basename_identity_ambiguous_after_delete"
               for c in collisions), collisions
    assert not any(c.get("reason") ==
                   "legacy_basename_identity_canonicalized_after_delete"
                   for c in collisions), collisions


def test_n1_upgrade_transition_delete_whole_family_dropped():
    """⑤整族删除（同名两脚本全删）→ 旧 basename 条目剔除（无幸存者
    可保留；配合守恒校验的 basename 豁免不被误拒）。"""
    from godot_qa_toolkit.measurement.increment import _merged_test_verdicts

    collisions: list = []
    merged = _merged_test_verdicts(
        _legacy_baseline(), [], set(),
        {"tests/a/test_green.gd", "tests/b/test_green.gd"}, set(),
        None, set(), set(),
        tests_face=_face([]),
        arm_identities=[LEGACY_IDENT], collisions=collisions)
    assert LEGACY_IDENT not in merged, sorted(merged)


def test_n1_upgrade_transition_delete_via_refresh_flow(tmp_path):
    """⑤端到端（真实 refresh 流、零 GUT 腿）：真实哈希的旧 basename
    形态基线 → 删其一 → refresh 增量 → 判定表规范单一形态、幸存者
    判定保留、dropped 记完整路径、守恒校验随流通过。"""
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).parent))
    from attribution_fixtures import write_map

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "prod.gd").write_text(
        "extends Node\nfunc calc(x: int) -> int:\n\treturn x + 1\n",
        encoding="utf-8")
    (proj / "tests" / "a").mkdir(parents=True)
    (proj / "tests" / "b").mkdir(parents=True)
    body = ('extends GutTest\nconst C = preload("res://prod.gd")\n'
            "func test_sign_positive():\n\tassert_eq(C.calc(1), 2)\n")
    (proj / "tests" / "a" / "test_green.gd").write_text(body,
                                                        encoding="utf-8")
    (proj / "tests" / "b" / "test_green.gd").write_text(body,
                                                        encoding="utf-8")
    write_map(proj, {"prod.gd": [A_IDENT, B_IDENT]})
    map_file = proj / ".qa-cache" / "attribution-map.json"
    doc = json.loads(map_file.read_text(encoding="utf-8"))

    # 升级过渡基线：把同名两身份折叠为旧 basename 形态（其余结构不动）
    def legacy(ident: str) -> str:
        sc, sep, nm = ident.partition("::")
        if sep and sc in ("res://tests/a/test_green.gd",
                          "res://tests/b/test_green.gd"):
            return LEGACY_IDENT
        return ident

    table = {LEGACY_IDENT: {"verdict": "green"}}
    doc["test_verdicts"] = table
    arm = doc["arms"]["gut_full"]
    arm["test_to_files"] = {LEGACY_IDENT: sorted({
        r for rs in arm["test_to_files"].values() for r in rs})}
    arm["test_to_lines"] = {LEGACY_IDENT: dict(
        next(iter(arm["test_to_lines"].values())))}
    arm["file_to_tests"] = {rel: [LEGACY_IDENT]
                            for rel in arm["file_to_tests"]}
    arm["line_to_tests"] = {rel: {ln: [LEGACY_IDENT]
                                  for ln in buckets}
                            for rel, buckets in
                            arm["line_to_tests"].items()}
    map_file.write_text(json.dumps(doc, ensure_ascii=False),
                        encoding="utf-8")

    os.remove(proj / "tests" / "a" / "test_green.gd")
    from godot_qa_toolkit.mutation.map_contract import refresh

    out = refresh(str(proj), ["prod.gd"], timeout_s=120)
    assert out.get("fresh") is True, out
    doc2 = json.loads(map_file.read_text(encoding="utf-8"))
    inc = doc2.get("increment") or {}
    assert inc.get("mode") == "incremental", inc
    assert inc.get("dropped_scripts") == ["tests/a/test_green.gd"], inc
    table2 = doc2.get("test_verdicts") or {}
    canon = f"res://tests/b/test_green.gd::{TEST_NAME}"
    assert table2.get(canon, {}).get("verdict") == "green", table2
    assert LEGACY_IDENT not in table2, sorted(table2)
    collisions = doc2.get("identity_collisions") or []
    assert any(c.get("reason") ==
               "legacy_basename_identity_canonicalized_after_delete"
               for c in collisions), collisions


# ───────────────── 真机段：§SPEC-038 ②③④⑥ 端到端（无真机 skip） ─────────────────
def test_matrix_delete_same_name_a_real_godot(tmp_path, monkeypatch):
    """②真机：建图 → 删 a/ → refresh 增量 → 被删身份剔除、幸存者判定
    与边逐位不变、dropped 记完整路径、skip-red 只命中真红。"""
    root = _v1._build_same_name_project(tmp_path / "case-a")
    _v1._build_map(root, monkeypatch)
    map_file = root / ".qa-cache" / "attribution-map.json"
    before = json.loads(map_file.read_text(encoding="utf-8"))
    os.remove(root / "tests" / "a" / "test_green.gd")
    from godot_qa_toolkit.mutation.map_contract import refresh

    out = refresh(str(root), ["calc_sign.gd"], timeout_s=500)
    assert out.get("fresh") is True, out
    doc = json.loads(map_file.read_text(encoding="utf-8"))
    inc = doc.get("increment") or {}
    assert inc.get("mode") == "incremental", inc
    assert inc.get("dropped_scripts") == ["tests/a/test_green.gd"], inc
    table_before = before.get("test_verdicts") or {}
    table = doc.get("test_verdicts") or {}
    assert A_IDENT not in table, sorted(table)
    # 幸存者逐位不变
    assert table.get(B_IDENT) == table_before.get(B_IDENT), \
        (table_before.get(B_IDENT), table.get(B_IDENT))
    t2l_before = ((before.get("arms") or {}).get("gut_full") or {}) \
        .get("test_to_lines") or {}
    t2l = ((doc.get("arms") or {}).get("gut_full") or {}) \
        .get("test_to_lines") or {}
    assert A_IDENT not in t2l, sorted(t2l)
    assert t2l.get(B_IDENT) == t2l_before.get(B_IDENT), B_IDENT
    # map 源 skip-red 只命中真红（b 的既有失败），零假 kill
    from godot_qa_toolkit.mutation.verdict_source import map_baseline_snapshots

    baselines, _failures = map_baseline_snapshots(
        ["calc_sign.gd"], str(root), None)
    snap = baselines[None]
    assert snap.get("red_pairs") == \
        [("res://tests/b/test_green.gd", TEST_NAME)], snap.get("red_pairs")
    # 续新后入口直接可用：status 判新鲜
    from godot_qa_toolkit.mutation.map_contract import status

    st = status(str(root), ["calc_sign.gd"])
    assert st.get("fresh") is True, st.get("factors")


def test_matrix_delete_same_name_b_real_godot(tmp_path, monkeypatch):
    """②另一侧：删 b/ → 同口径成立（a 幸存者保留绿判定）。"""
    root = _v1._build_same_name_project(tmp_path / "case-b")
    _v1._build_map(root, monkeypatch)
    map_file = root / ".qa-cache" / "attribution-map.json"
    before = json.loads(map_file.read_text(encoding="utf-8"))
    os.remove(root / "tests" / "b" / "test_green.gd")
    from godot_qa_toolkit.mutation.map_contract import refresh

    out = refresh(str(root), ["calc_sign.gd"], timeout_s=500)
    assert out.get("fresh") is True, out
    doc = json.loads(map_file.read_text(encoding="utf-8"))
    inc = doc.get("increment") or {}
    assert inc.get("mode") == "incremental", inc
    assert inc.get("dropped_scripts") == ["tests/b/test_green.gd"], inc
    table_before = before.get("test_verdicts") or {}
    table = doc.get("test_verdicts") or {}
    assert B_IDENT not in table, sorted(table)
    assert table.get(A_IDENT) == table_before.get(A_IDENT), \
        (table_before.get(A_IDENT), table.get(A_IDENT))
    snap = None
    from godot_qa_toolkit.mutation.verdict_source import map_baseline_snapshots

    baselines, _failures = map_baseline_snapshots(
        ["calc_sign.gd"], str(root), None)
    snap = baselines[None]
    assert snap.get("red_pairs") == [], snap.get("red_pairs")


def test_matrix_normal_delete_real_godot(tmp_path, monkeypatch):
    """③真机：普通删除（无同名，filler 脚本）→ 增量成功、被删身份
    剔除、其余身份判定保留（N1 修复前 validate_refresh 会把普通删除
    误判 refresh_lost_identities 拒绝续新）。"""
    root = _v1._build_same_name_project(tmp_path / "case-c")
    _v1._build_map(root, monkeypatch)
    map_file = root / ".qa-cache" / "attribution-map.json"
    before = json.loads(map_file.read_text(encoding="utf-8"))
    victim = "res://tests/fill/test_calc_00.gd::test_add_00"
    assert victim in (before.get("test_verdicts") or {}), \
        sorted((before.get("test_verdicts") or {}))[:8]
    os.remove(root / "tests" / "fill" / "test_calc_00.gd")
    from godot_qa_toolkit.mutation.map_contract import refresh

    out = refresh(str(root), ["calc_sign.gd"], timeout_s=500)
    assert out.get("fresh") is True, out
    doc = json.loads(map_file.read_text(encoding="utf-8"))
    inc = doc.get("increment") or {}
    assert inc.get("mode") == "incremental", inc
    assert inc.get("dropped_scripts") == ["tests/fill/test_calc_00.gd"], inc
    table = doc.get("test_verdicts") or {}
    assert victim not in table, sorted(table)
    assert table.get(A_IDENT, {}).get("verdict") == "green", table.get(A_IDENT)
    assert table.get(B_IDENT, {}).get("verdict") == "red", table.get(B_IDENT)


def test_matrix_add_same_name_real_godot(tmp_path, monkeypatch):
    """④真机：新增同名脚本 → 增量 → 三个身份都在、无重复形态。"""
    root = _v1._build_same_name_project(tmp_path / "case-d")
    _v1._build_map(root, monkeypatch)
    map_file = root / ".qa-cache" / "attribution-map.json"
    (root / "tests" / "c").mkdir(parents=True)
    (root / "tests" / "c" / "test_green.gd").write_text(
        'extends GutTest\nconst C = preload("res://calc_sign.gd")\n'
        "func test_sign_positive():\n\tassert_eq(C.new().sign(5), 1)\n",
        encoding="utf-8")
    from godot_qa_toolkit.mutation.map_contract import refresh

    out = refresh(str(root), ["calc_sign.gd"], timeout_s=500)
    assert out.get("fresh") is True, out
    doc = json.loads(map_file.read_text(encoding="utf-8"))
    inc = doc.get("increment") or {}
    assert inc.get("mode") == "incremental", inc
    table = doc.get("test_verdicts") or {}
    c_ident = "res://tests/c/test_green.gd::test_sign_positive"
    assert table.get(c_ident, {}).get("verdict") == "green", table.get(c_ident)
    assert table.get(A_IDENT, {}).get("verdict") == "green", table.get(A_IDENT)
    assert table.get(B_IDENT, {}).get("verdict") == "red", table.get(B_IDENT)
    # 无重复形态：旧 basename 形态键不得出现
    assert LEGACY_IDENT not in table, sorted(table)
