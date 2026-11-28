"""SEE-1654 harden-R1 钉点②：gherkin 增量腿**成功路径**常驻钉。

冒烟矩阵 R12 的夹具（`install_recipe` 的 demo.feature：hp=10≠left=7）
在预检就失败，腿的成功分支（rc=0、执行证据在场、判定 green）在冒烟
矩阵上覆盖不到；既有常驻测试只从合成账喂判定函数（c3r27
`test_all_pass_leg_green`）或断言失败腿的账键（c3r25 N5 / 真机矩阵），
没有任何测试把「腿真实跑成功 → `executed_scripts` 真实提取在场 →
`_gherkin_rerun_all_passed` 判全过 → 判定 green」整条链钉住。
C3-R26/D2 引入的 `executed_scripts` 证据键是这条链的判据来源——若提取
回归成空集，真实通过的腿会被第五形态误判成保守红（假失败方向），
既有的失败方向钉（rc=0 零证据不得放绿）抓不住它。

- 单元：假 GUT 返回真实形态的成功运行流（res:// 头 + `[passed]` 行），
  `_gherkin_leg_execute` 产物携带截断前提取的 `executed_scripts` /
  空 `failing_tests`，且 `_gherkin_rerun_all_passed` 判 True。
- 真机（成功腿端到端）：通过型 harness 夹具 + 通过型 demo.feature →
  建图 → feature 加注释触发增量 → 腿真实执行 rc=0、`executed_scripts`
  在场、身份判定 green；恢复原文再增量 → 判定表逐位回基线（成功路径
  heal 与全量一致）。无真机按仓库惯例 skip。

红绿咬合：单元在 `2858dbb`（C3-R26/D2 之前——腿账无 `executed_scripts`
键）上为红；真机段同一断言在 `2858dbb` 上为红。
身份形态遵循决策 19：身份第二段由真实编译器 `_ident(scenario, 位次)`
派生（`gherkin://feature/demo::test_s_000_00`），不手写合成身份。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from godot_qa_toolkit.gherkin.compile import _ident
from tests.unit.gherkin_verdict_fixtures import (
    GEN_SCRIPT,
    PASS_SCENARIO,
    _gut_out,
    _test_name,
)


# ─────────────────────── 单元：成功腿的账形态与全过判定 ───────────────────────
def test_leg_success_account_carries_executed_scripts(tmp_path,
                                                      monkeypatch):
    """假 GUT 返回真实形态成功运行流 → `_gherkin_leg_execute` 产物
    `executed_scripts` 非空（截断前提取）、`failing_tests` 空集、rc=0；
    `_gherkin_rerun_all_passed` 判 True（2858dbb 上无 `executed_scripts`
    键、全过判定只看 rc——提取回归不可见 = 本测为红）。"""
    from godot_qa_toolkit.measurement import increment_runner as ir

    out = _gut_out([], [_test_name(PASS_SCENARIO)]) + "\nRun Summary\n"
    assert GEN_SCRIPT in out

    def fake_run_one_gut(root, argv, timeout_s, env=None, gate_root=None):
        return (SimpleNamespace(returncode=0, stdout=out, stderr=""), 0.1)

    monkeypatch.setattr(ir, "_run_one_gut", fake_run_one_gut)
    xdg: list[str] = []
    executions = ir._gherkin_leg_execute(
        tmp_path, "res://gherkin/generated/x", {}, {}, [],
        "uidhardenr1s0001", 60, xdg)
    assert executions, executions
    ex = executions[0]
    assert ex.get("rc") == 0, ex
    assert ex.get("executed_scripts") == [GEN_SCRIPT], ex
    assert ex.get("failing_tests") == [], ex
    assert ex.get("error") is None, ex

    from godot_qa_toolkit.measurement.increment import _gherkin_rerun_all_passed

    assert _gherkin_rerun_all_passed({"executions": executions}) is True


def test_leg_success_factory_account_is_all_passed_not_conservative():
    """守卫（不误伤真实通过）：共享夹具工厂造的 rc=0 真实形态账
    （`executed_scripts` 在场）判全过——第五形态只吃零证据账。"""
    from godot_qa_toolkit.measurement.increment import _gherkin_rerun_all_passed
    from tests.unit.gherkin_verdict_fixtures import _leg_account

    account = _leg_account([], [PASS_SCENARIO], rc=0)
    assert account["executed_scripts"], account
    assert _gherkin_rerun_all_passed({"executions": [account]}) is True


# ─────────────────────── 真机：成功腿端到端（heal 与全量一致） ───────────────────────
PASSING_HARNESS_GD = (
    "extends RefCounted\n"
    "\n\n"
    "func smoke_check() -> bool:\n"
    "\treturn true\n"
    "\n\n"
    "func reset() -> void:\n"
    "\tpass\n"
    "\n\n"
    "func init_case(_case: Dictionary) -> String:\n"
    "\treturn \"\"\n"
    "\n\n"
    "func run_step(_step: Dictionary, _case: Dictionary) -> String:\n"
    "\treturn \"\"\n"
    "\n\n"
    "func check_expectations(_case: Dictionary) -> String:\n"
    "\treturn \"\"\n"
    "\n\n"
    "func query_state(_slot: String) -> String:\n"
    "\treturn \"\"\n"
)

PASSING_FEATURE = (
    "Feature: F\n"
    "  Scenario Outline: s\n"
    "    Then hp is <left>\n"
    "    # types: <hp>:int, <left>:numeric\n"
    "    Examples:\n"
    "      | hp | left |\n"
    "      | 7  | 7    |\n"
)


def test_matrix_gherkin_leg_success_green_and_heals(tmp_path, monkeypatch):
    """真机成功腿：通过型 feature → 注释触发增量 → 腿真实执行 rc=0、
    `executed_scripts` 在场（R12 冒烟覆盖不到的成功分支）、身份判定
    green 不落保守红；恢复原文再增量 → 判定表逐位回基线。"""
    import test_c3r23_same_name_increment_see1648 as _v1

    root = _v1._build_same_name_project(tmp_path / "r1654-leg-ok")
    (root / "addons" / "gqt_gherkin" / "harness.gd").write_text(
        PASSING_HARNESS_GD, encoding="utf-8")
    feat = root / "addons" / "gqt_gherkin" / "features" / "demo.feature"
    feat.write_text(PASSING_FEATURE, encoding="utf-8")
    _v1._build_map(root, monkeypatch)
    map_file = root / ".qa-cache" / "attribution-map.json"
    doc = json.loads(map_file.read_text(encoding="utf-8"))
    # 决策 19：身份第二段 = 真实编译器命名（scenario "s" 位次 0 →
    # test_s_000 + 案例 00）——与腿的编译产物逐字符同形。
    ident = f"gherkin://feature/demo::{_ident('s', 0)}_00"
    # 补种 gherkin 臂：重跑比例帽 0.05，1/25 = 0.04 走真腿（同 c3r25 矩阵）
    f2l = {f"demo_filler_{i:02d}": {"calc_sign.gd": [3]}
           for i in range(24)}
    f2l["demo"] = {"calc_sign.gd": [3]}
    doc["arms"]["gherkin"] = {
        "feature_to_lines": f2l,
        "test_to_lines": {ident: {"calc_sign.gd": [3]}},
        "line_to_features": {"calc_sign.gd": {"3": [ident]}},
        "tests": 1, "pairs": 1,
    }
    doc.setdefault("test_verdicts", {})[ident] = {
        "verdict": "green", "classification": "pre_existing"}
    (doc.setdefault("arms_state", {}))["gherkin"] = {
        "state": "valid", "invalid_reason": None, "bundles": 1,
        "pairs": 1, "probes_fired": 10, "vacuous": False}
    map_file.write_text(json.dumps(doc, ensure_ascii=False),
                        encoding="utf-8")

    from godot_qa_toolkit.mutation.map_contract import refresh

    feat.write_text(feat.read_text(encoding="utf-8")
                    + "# harden-R1 success probe\n", encoding="utf-8")
    out = refresh(str(root), ["calc_sign.gd"], timeout_s=500)
    assert out.get("fresh") is True, out
    doc = json.loads(map_file.read_text(encoding="utf-8"))
    inc = doc.get("increment") or {}
    assert inc.get("mode") == "incremental", inc
    assert inc.get("gherkin_rerun_features") == ["demo"], inc
    gh_execs = inc.get("gherkin_leg_executions") or []
    assert gh_execs, inc
    ex = gh_execs[0]
    # 成功腿的账形态（2858dbb 上无 executed_scripts 键 = 本测为红）：
    assert ex.get("rc") == 0, ex
    assert ex.get("error") is None, ex
    assert ex.get("executed_scripts"), ex
    assert ex.get("failing_tests") == [], ex
    table = doc.get("test_verdicts") or {}
    assert table.get(ident, {}).get("verdict") == "green", \
        table.get(ident)
    baseline_table = json.loads(json.dumps(table), )

    # heal：恢复原文再增量 → 判定表逐位回基线（成功路径与全量一致）
    feat.write_text(PASSING_FEATURE, encoding="utf-8")
    out2 = refresh(str(root), ["calc_sign.gd"], timeout_s=500)
    assert out2.get("fresh") is True, out2
    doc2 = json.loads(map_file.read_text(encoding="utf-8"))
    inc2 = doc2.get("increment") or {}
    assert inc2.get("mode") == "incremental", inc2
    assert (doc2.get("test_verdicts") or {}) == baseline_table, \
        "heal roundtrip drifted from baseline verdict table"
