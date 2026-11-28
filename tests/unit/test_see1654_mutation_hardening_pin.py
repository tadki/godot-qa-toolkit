"""SEE-1654 harden-R1 变异硬化补钉：f09d5f0..d961ccd 修复面变异扫描
幸存位点的判别力补强（§SPEC-012 / 023）。

变异扫描（17 点手工语义变异，/tmp 真副本逐点注入还原）发现四个位点
在既有单元面 SURVIVED，本模块逐一补钉：

- M6（D2 披露面）：`_rerun_error_disclosure` 的 unattributable 具名
  披露分支——既有断言只在真机矩阵测试（`test_matrix_parse_error_
  no_green_real_godot`），单元面无人钉；红锚 `2858dbb`（披露分支
  尚不存在 → 披露为空，本测红）。
- M8（D1 行事实双车道）：`_line_fact_touchers` 的 script 车道且
  **basename 归一形态**——红锚 `f09d5f0`（surface 起点：script 车道
  回写完整 `res://` 身份、未归一；归一在 surface 首提交 `034b950`
  落地，本测在其上红）。
- M10（N2 截断前提取 · gherkin 腿）：`_gherkin_leg_execute` 的
  `failing_tests` 必须从**完整**输出提取——既有 N2 单元钉只覆盖 GUT
  腿的 `_execution_record`（`test_c3r24…::test_n2_truncated_output_…`），
  腿账自己的提取无人钉；红锚 `972dcc4`（腿账无 `failing_tests` 键、
  失败名只从截断尾部解析，本测红）。
- M16（回写桥）：`_failing_scenarios_backfill` 的桥映射方向（编译
  测试名 → scenario 名）——既有面无人断言映射值本身；在迹产品史无
  此缺陷（桥自引入即正确），红咬合由变异注入判定（M16 补钉后
  KILLED），如实披露。

身份/账形态遵循决策 19：全部取自真实产物派生的共享夹具工厂
（`gherkin_verdict_fixtures`：真实 `_ident`、真实解析器、真实回写链）。
"""

from __future__ import annotations

from types import SimpleNamespace


# ─────────────── M6：D2 unattributable 具名披露（2858dbb 上红） ───────────────
def test_d2_unattributable_disclosure_named():
    """rc=0 且零执行证据的账 → `_rerun_error_disclosure` 必须出具名
    unattributable 条目（2858dbb 上无此分支 → 披露为空，本测红）；
    真实通过的账（脚本头在场）不误伤。"""
    from godot_qa_toolkit.measurement.increment import _rerun_error_disclosure
    from godot_qa_toolkit.measurement.increment_runner import _execution_record

    parse_error_out = ("Godot Engine v4.6.2.stable.official.\n"
                       "Failed to load script res://tests/x/test_a.gd with "
                       "error \"Parse Error: Unexpected end of expression\""
                       ".\n")
    rec = _execution_record("test_a.gd", 0, parse_error_out)
    assert rec["evidence"]["executed"] == [], rec["evidence"]
    entries = _rerun_error_disclosure([rec])
    assert any(
        e.get("script") == "test_a.gd"
        and "unattributable" in str(e.get("error"))
        and "zero execution evidence" in str(e.get("error"))
        for e in entries), entries

    ok_rec = _execution_record(
        "test_ok.gd", 0,
        "res://tests/x/test_ok.gd\n* test_pass\nRun Summary\n")
    assert _rerun_error_disclosure([ok_rec]) == []


# ─────────────── M8：行事实双车道 · basename 口径（2858dbb 上红） ───────────────
def test_line_fact_touchers_both_lanes_basename_form():
    """`_line_fact_touchers`：test 车道身份与 script 车道都按重跑面
    basename 口径出触达者（surface 起点 `f09d5f0` 上 script 车道回写
    完整 `res://` 身份、未按 basename 归一 → 本测红；归一在 surface
    首提交 `034b950` 落地，其后绿）。"""
    from godot_qa_toolkit.measurement.increment import _line_fact_touchers

    arm = {
        "test_to_lines": {"res://tests/x/test_user.gd::t":
                          {"prod.gd": [2]}},
        "script_to_lines": {"res://scripts/helper.gd": {"prod.gd": [1]}},
    }
    touch = _line_fact_touchers(arm, "prod.gd")
    assert touch == {"test_user.gd", "helper.gd"}, touch


# ─────────────── M10：gherkin 腿失败名截断前提取（972dcc4 上红） ───────────────
def test_leg_failing_tests_extracted_from_full_output(tmp_path,
                                                      monkeypatch):
    """失败名只在截断窗口之外可见（>4000 字符、标记行在窗口外）→
    `_gherkin_leg_execute` 的 `failing_tests` 仍非空（必须解析完整
    输出；972dcc4 上腿账无 `failing_tests` 键、失败名只解析截断尾部
    → 本测红）。"""
    from godot_qa_toolkit.measurement import increment_runner as ir
    from tests.unit.gherkin_verdict_fixtures import GEN_SCRIPT

    name = "test_window_out_fail"
    filler = "\n".join(
        f"output line {i:04d} padding padding padding" for i in range(300))
    out = (f"{GEN_SCRIPT}\n* {name}\n[Failed] {name}\n"
           f"{filler}\nRun Summary\n")
    assert len(out) > 4000, len(out)
    assert name not in out[-4000:], \
        "fixture must hide the failure marker beyond the truncation window"

    def fake_run_one_gut(root, argv, timeout_s, env=None, gate_root=None):
        return (SimpleNamespace(returncode=1, stdout=out, stderr=""), 0.1)

    monkeypatch.setattr(ir, "_run_one_gut", fake_run_one_gut)
    xdg: list[str] = []
    executions = ir._gherkin_leg_execute(
        tmp_path, "res://gherkin/generated/x", {}, {}, [],
        "uidhardenr1m0010", 60, xdg)
    assert executions, executions
    assert executions[0].get("failing_tests") == [name], executions[0]


# ─────────────── M16：回写桥方向（变异咬合；无在迹产品缺陷） ───────────────
def test_backfill_bridge_maps_compile_names_to_scenario_names():
    """`_failing_scenarios_backfill`：`failing_scenarios` 必须是 scenario
    名（经真实 `_scenario_map_from_manifests` 桥），不得直接装编译测试名
    （M16 变异 = 桥反转）。在迹产品史无此缺陷形态，红咬合由变异注入
    判定（如实披露）。"""
    from godot_qa_toolkit.measurement.increment_runner import (
        _failing_scenarios_backfill,
        _scenario_map_from_manifests,
    )
    from tests.unit.gherkin_verdict_fixtures import (
        FAIL_SCENARIO,
        FEATURE,
        GEN_SCRIPT,
        _test_name,
    )

    fail_test = _test_name(FAIL_SCENARIO)
    manifest = {"manifests": {FEATURE: {"attribution": [
        {"test_name": fail_test, "scenario": FAIL_SCENARIO}]}}}
    scenario_of_test = _scenario_map_from_manifests(manifest, [FEATURE])
    ex = {"rc": 1,
          "out": (f"{GEN_SCRIPT}\n* {fail_test}\n[Failed] {fail_test}\n"
                  "Run Summary\n"),
          "failing_tests": [fail_test]}
    _failing_scenarios_backfill([ex], scenario_of_test)
    assert ex["failing_scenarios"] == [FAIL_SCENARIO], ex


# 决策 19 锚：编译测试名确由真实编译器派生（防夹具漂移的形态守卫）。
def test_real_compiler_derives_fixture_names():
    from godot_qa_toolkit.gherkin.compile import _ident
    from tests.unit.gherkin_verdict_fixtures import (
        FAIL_SCENARIO,
        _test_name,
    )

    assert _test_name(FAIL_SCENARIO).startswith(
        _ident(FAIL_SCENARIO, 0)), _test_name(FAIL_SCENARIO)
