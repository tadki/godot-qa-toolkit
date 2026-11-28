"""SEE-1651 C3-R26：QA-R3 退回项 D1/D2 的回归钉。

- D1（HIGH）：`_content_changed_rerun` 只查 `file_to_scripts`（script
  车道）——真实建图里 script 车道通常为空（script 级行事实只在测试
  窗口外命中；KOL 真图 `script_to_lines` 0 条、`test_to_lines` 208
  条），内容修改后触达测试照样不重跑、续新报假新鲜。触达反查改按
  **行事实双车道**（与 `prod_removed` 的 `_line_fact_touchers` 同源，
  test 车道 + script 车道，basename 归一）。
- D2（HIGH）：执行账第五形态——有账、`rc=0`、零执行证据（无脚本头、
  无失败对、无失败名；典型形态：GUT `Failed to load script … Parse
  error` 零测试执行）= 不可归因，此前按 rc==0 放绿（45 条既有绿继承、
  假 kill 方向）。补保守整脚本红 + 具名披露；gherkin 腿同口径（rc=0
  且零 scenario 执行证据不得放绿）。

单元红测试在 `2858dbb` 的产品代码上为红；真机段（真实 Godot）覆盖
主流形态（test 体内消费、真实 GUT 解析失败），无真机按仓库惯例 skip。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import test_c3r23_same_name_increment_see1648 as _v1

VALID_TRES = '[gd_resource type="Resource" format=3]\n\n[resource]\n'


def _face(rels: list) -> dict:
    import hashlib

    return {rel: hashlib.sha256(rel.encode()).hexdigest() for rel in rels}


# ───────────────── D1：content_changed 触达反查按行事实双车道 ─────────────────
def test_d1_content_change_rerun_uses_line_facts_both_lanes(tmp_path):
    """单元：script 车道为空（真实建图形态：KOL 真图 0:208）、test 车道
    有触达（test 体内消费的主流形态）→ 内容修改后 rerun 必含触达测试、
    `rerun_sources` 带 `content_prod:<gd>`（2858dbb 上只查
    file_to_scripts → rerun 空，本测为红）。"""
    import hashlib

    from godot_qa_toolkit.measurement.increment import _increment_decision

    root = tmp_path / "proj"
    (root / "tests" / "x").mkdir(parents=True)
    (root / "data.tres").write_text(VALID_TRES, encoding="utf-8")
    (root / "prod_user.gd").write_text(
        'extends RefCounted\nconst D = preload("res://data.tres")\n'
        "func value() -> int:\n\treturn 1\n", encoding="utf-8")
    baseline = {
        "schema_version": "test-attribution/7",
        "report_recipe_hash": "h-ok",
        "production_content_sha256": {
            "data.tres": hashlib.sha256(b"k=0\n").hexdigest()},
        "tests_face": {"tests/x/test_user.gd": "sha"},
        "arms": {"gut_full": {
            "test_to_files": {
                "res://tests/x/test_user.gd::t": ["prod_user.gd"]},
            "script_to_files": {},
            "test_to_lines": {
                "res://tests/x/test_user.gd::t": {"prod_user.gd": [2]}},
            # 真实建图形态：script 车道为空（触达都在测试窗口内）
            "script_to_lines": {},
            "file_to_scripts": {},
        }},
    }
    classes, invalidation, rerun, reason = _increment_decision(
        root, {"recipe_hash": "h-ok"}, baseline, ["data.tres"], 0, 5, 0.05)
    assert classes.get("content_changed") == ["data.tres"], classes
    assert "test_user.gd" in rerun, (rerun, invalidation["sources"])
    assert any("content_prod:prod_user.gd" in s
               for s in (invalidation["sources"].get("test_user.gd") or [])), \
        invalidation["sources"]


def test_matrix_content_change_test_body_form_real_godot(tmp_path,
                                                         monkeypatch):
    """真机（主流形态）：消费发生在 **test 体内**（script 车道为空）→
    改内容触发增量 → rerun 必含触达测试（D1 修复前 rerun 为空、假
    新鲜）。夹具断言基线 script 车道为空，不用 before_all 造形态。"""
    import shutil

    root = _v1._build_same_name_project(tmp_path / "r26-content")
    (root / "data.tres").write_text(VALID_TRES, encoding="utf-8")
    (root / "prod_user.gd").write_text(
        'extends RefCounted\nconst D = preload("res://data.tres")\n'
        "func value() -> int:\n\treturn 1\n", encoding="utf-8")
    (root / "tests" / "fill" / "test_data_user.gd").write_text(
        'extends GutTest\nconst US = preload("res://prod_user.gd")\n'
        "func test_user_value():\n"
        "\tassert_eq(US.new().value(), 1)\n", encoding="utf-8")
    scope = json.loads((root / ".gqt-scope.json")
                       .read_text(encoding="utf-8"))
    scope["production"]["include"].extend(
        [{"kind": "file", "value": "data.tres"},
         {"kind": "file", "value": "prod_user.gd"}])
    (root / ".gqt-scope.json").write_text(
        json.dumps(scope, ensure_ascii=False), encoding="utf-8")
    _v1._build_map(root, monkeypatch)
    map_file = root / ".qa-cache" / "attribution-map.json"
    doc = json.loads(map_file.read_text(encoding="utf-8"))
    arm = (doc.get("arms") or {}).get("gut_full") or {}
    # 夹具形态断言：基线 script 车道为空（真实建图主流形态）、test
    # 车道有触达——覆盖的正是 D1 缺的那条车道
    assert not (arm.get("script_to_lines") or {}), \
        sorted(arm.get("script_to_lines") or {})
    assert any("prod_user.gd" in (v or {})
               for v in (arm.get("test_to_lines") or {}).values()), \
        sorted((arm.get("test_to_lines") or {}))[:8]
    (root / "data.tres").write_text(
        VALID_TRES.replace("[resource]\n",
                           "[resource]\nk = 2\n"), encoding="utf-8")
    from godot_qa_toolkit.mutation.map_contract import refresh

    out = refresh(str(root), ["calc_sign.gd"], timeout_s=500)
    assert out.get("fresh") is True, out
    doc = json.loads(map_file.read_text(encoding="utf-8"))
    inc = doc.get("increment") or {}
    assert inc.get("mode") == "incremental", inc
    assert "test_data_user.gd" in (inc.get("rerun_scripts") or []), inc
    sources = inc.get("rerun_sources") or {}
    assert any("content_prod:prod_user.gd" in s
               for s in (sources.get("test_data_user.gd") or [])), sources


# ───────────────── D2：执行账第五形态（rc=0 + 零执行证据） ─────────────────
def test_d2_rc0_zero_execution_evidence_never_green():
    """单元（Revy 的注入账形态）：GUT `Failed to load script … Parse
    error` 零测试执行 → 执行账 rc=0、证据全空 → 该脚本全部身份保守红，
    不得继承绿（2858dbb 上按 rc==0 放绿 → 双身份 green，本测为红）。"""
    from godot_qa_toolkit.measurement.increment import _merged_test_verdicts
    from godot_qa_toolkit.measurement.increment_runner import \
        _execution_record

    a = "res://tests/x/test_a.gd::test_one"
    b = "res://tests/x/test_a.gd::test_two"
    out = ("Godot Engine v4.6.2.stable.official.\n"
           "Failed to load script res://tests/x/test_a.gd with error "
           "\"Parse Error: Unexpected end of expression\".\n")
    rec = _execution_record("test_a.gd", 0, out)
    assert rec["evidence"]["executed"] == [], rec["evidence"]
    assert rec["evidence"]["failing"] == [], rec["evidence"]
    baseline = {"test_verdicts": {
        a: {"verdict": "green"},
        b: {"verdict": "green"},
    }}
    merged = _merged_test_verdicts(
        baseline, [rec], {"test_a.gd"}, set(), set(),
        None, set(), set(),
        tests_face=_face(["tests/x/test_a.gd"]),
        arm_identities=[a, b], collisions=[])
    assert merged.get(a, {}).get("verdict") == "red", merged
    assert merged.get(b, {}).get("verdict") == "red", merged


def test_d2_gherkin_rc0_zero_evidence_never_green():
    """单元（两腿口径对齐）：gherkin 重跑腿 rc=0 且零 scenario 执行证据
    （executed_scripts 空集——GUT 空收集/解析失败形态）→ 不得按 rc==0
    放绿（2858dbb 上 _gherkin_rerun_all_passed 只看 rc → green，本测为
    红）。"""
    from godot_qa_toolkit.measurement.increment import _merged_test_verdicts

    ident = "gherkin://feature/demo::s::1"
    baseline = {"test_verdicts": {ident: {"verdict": "green"}}}
    fresh_gherkin = {"executions": [{
        "rc": 0, "failing_scenarios": [], "executed_scripts": [],
        "out": "Godot Engine v4.6.2.stable.official.\n"}]}
    merged = _merged_test_verdicts(
        baseline, [], set(), set(), set(),
        fresh_gherkin, {"demo"}, set(),
        tests_face=None, arm_identities=[], collisions=[])
    assert merged.get(ident, {}).get("verdict") == "red", merged


def test_d2_gut_rc0_with_headers_still_green_not_regressed():
    """守卫（不误伤真实通过）：rc=0 且脚本头在场（GUT 真实打印 res://
    头）→ 仍按逐测试判定放绿——第五形态只覆盖零执行证据。"""
    from godot_qa_toolkit.measurement.increment import _merged_test_verdicts
    from godot_qa_toolkit.measurement.increment_runner import \
        _execution_record

    a = "res://tests/x/test_ok.gd::test_pass"
    out = ("res://tests/x/test_ok.gd\n"
           "* test_pass\n"
           "Run Summary\n")
    rec = _execution_record("test_ok.gd", 0, out)
    baseline = {"test_verdicts": {a: {"verdict": "green"}}}
    merged = _merged_test_verdicts(
        baseline, [rec], {"test_ok.gd"}, set(), set(),
        None, set(), set(),
        tests_face=_face(["tests/x/test_ok.gd"]),
        arm_identities=[a], collisions=[])
    assert merged.get(a, {}).get("verdict") == "green", merged


def test_matrix_parse_error_no_green_real_godot(tmp_path, monkeypatch):
    """真机（D2 主流形态）：真实 GUT 对注入 GDScript 解析错误的脚本
    `Failed to load script` 零测试执行 → 刷新后该脚本身份不得继承绿、
    披露具名、续新成功且随后报 fresh（Revy 在 KOL 真图坐实的形态）。"""
    root = _v1._build_same_name_project(tmp_path / "r26-parse")
    _v1._build_map(root, monkeypatch)
    map_file = root / ".qa-cache" / "attribution-map.json"
    victim = root / "tests" / "fill" / "test_calc_00.gd"
    victim_ident = "res://tests/fill/test_calc_00.gd::test_add_00"
    before = json.loads(map_file.read_text(encoding="utf-8"))
    assert (before.get("test_verdicts") or {}).get(
        victim_ident, {}).get("verdict") == "green", victim_ident
    victim.write_text(
        victim.read_text(encoding="utf-8")
        + "\nfunc broken(:\n", encoding="utf-8")
    from godot_qa_toolkit.mutation.map_contract import refresh

    out = refresh(str(root), ["calc_sign.gd"], timeout_s=500)
    assert out.get("fresh") is True, out
    doc = json.loads(map_file.read_text(encoding="utf-8"))
    inc = doc.get("increment") or {}
    assert inc.get("mode") == "incremental", inc
    assert inc.get("rerun_scripts") == ["test_calc_00.gd"], inc
    table = doc.get("test_verdicts") or {}
    assert table.get(victim_ident, {}).get("verdict") == "red", \
        table.get(victim_ident)
    errors = inc.get("rerun_execution_errors") or []
    assert any("test_calc_00.gd" in str(e.get("script") or "")
               and "zero execution evidence" in str(e.get("error") or "")
               for e in errors), errors
    out2 = refresh(str(root), ["calc_sign.gd"], timeout_s=500)
    assert out2.get("fresh") is True, out2
