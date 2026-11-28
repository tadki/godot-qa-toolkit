"""SEE-1654 harden-R1 变异硬化补钉：f09d5f0..d961ccd 修复面变异扫描
幸存位点的判别力补强（§SPEC-012 / 023）。

变异扫描（17 点手工语义变异，/tmp 真副本逐点注入还原）发现四个位点
在既有单元面 SURVIVED，本模块逐一补钉。夹具来源逐条说明（决策 19）：

- M6（D2 披露面）`test_d2_unattributable_disclosure_named`：不涉身份
  比对——执行账由真实 `_execution_record` 从真实 GUT 失败形态输出
  （`Failed to load script … Parse Error` 零测试执行）构造，判定走
  产品 `_rerun_error_disclosure`。红锚 `2858dbb`（披露分支尚不存在
  → 披露为空，本测红）。
- M8（D1 行事实双车道）`test_line_fact_touchers_both_lanes_basename_form`：
  arm 取自**真实建图产物**（真 Godot + 真实 `refresh`，见
  `_real_build_arm`；无真机/无 GUT addon 按仓库惯例 skip）。script
  车道是升级过渡/遗留形态——主流真实建图该车道为空（本模块探针与
  QA-R4 的 KOL 真图 0 条一致；见决策 16 与 c3r22_d4），故在真实
  建图产物上以真实宇宙成员补一行 script 车道事实：键 = 真实建图宇宙
  内真实存在的脚本 `res://tests/test_forms.gd`（script 路径命名空间，
  遗留形态不归一），rel = 同一真实建图的真实生产文件 rel，行事实键
  两侧同键。按名比对的两侧命名空间：左 = `_script_name_of(真实建图
  身份)`（test 车道身份脚本段 basename）/ `Path(script 键).name`
  （script 车道 basename 归一），右 = 同一 rel 的触达集成员。红锚
  `f09d5f0`（script 车道回写完整 `res://` 身份、未按 basename 归一；
  归一在 surface 首提交 `034b950` 落地，本测在其上红）。
- M10（N2 截断前提取 · gherkin 腿）`test_leg_failing_tests_extracted_
  from_full_output`：失败名 = 共享夹具工厂 `_test_name(FAIL_SCENARIO)`
  （真实编译器 `_ident(scenario, 位次)` + 案例 00 派生），套件目录
  取 `GEN_SCRIPT` 真实产物路径形态；保留 >4000 字符窗口外构造与截断
  前提取断言。红锚 `972dcc4`（腿账无 `failing_tests` 键、失败名只
  解析截断尾部 → 本测红）。
- M16（回写桥）`test_backfill_bridge_maps_compile_names_to_scenario_names`：
  失败名与 manifest 均取共享夹具工厂（真实解析器 `_parse_failing_tests`
  + 真实回写链 `_scenario_map_from_manifests`）；按名比对两侧：左 =
  腿账 `failing_tests`（编译测试名命名空间），右 = `scenario_of_test`
  桥映射出的 scenario 名。在迹产品史无此缺陷形态，红咬合由变异注入
  判定（M16 补钉后 KILLED），如实披露。
- 守卫 `test_real_compiler_derives_fixture_names`：真实编译器派生
  形态守卫（防夹具漂移）。
"""

from __future__ import annotations

import json
import shutil
import subprocess as sp
from pathlib import Path
from types import SimpleNamespace

import pytest

FORMS_TEMPLATE = Path(__file__).resolve().parents[1] / "fixtures" / \
    "see1550_forms"


def _gut_addon_src() -> "Path | None":
    """真实 GUT addon 来源（与 test_c3r23 同口径的自含实现）。"""
    import os

    for cand in (os.environ.get("GQT_FORMS_GUT_DIR", ""),
                 "/mnt/d/GodotProjects/king-of-likes/addons/gut",
                 str(Path.home() / "tools" / "KingOfLikes-Godot" / "addons"
                     / "gut")):
        p = Path(cand) if cand else None
        if p is not None and (p / "gut_cmdln.gd").is_file():
            return p
    return None


def _real_godot_bin() -> "str | None":
    """真机二进制（同 test_c3r23 口径：env 或 PATH，stub 拒收）。"""
    import os
    import shutil as _shutil

    cand = os.environ.get("GQT_GODOT_BIN") or _shutil.which("godot")
    if not cand or not Path(cand).is_file():
        return None
    try:
        r = sp.run([str(cand), "--version"], capture_output=True,
                   text=True, timeout=60)
    except (OSError, sp.TimeoutExpired):
        return None
    if "stub" in (r.stdout or ""):
        return None
    return str(cand)


def _real_build_arm(tmp_path, monkeypatch):
    """真实建图（决策 19）：最小真实项目 → 真实 Godot import → 真实
    `refresh` → 返回 (root, arm, doc)。身份与行事实全部来自真实建图
    产物；无真机/无 GUT addon 按仓库惯例 skip。"""
    gut_src = _gut_addon_src()
    if gut_src is None:
        pytest.skip("real GUT addon unavailable")
    godot = _real_godot_bin()
    if godot is None:
        pytest.skip("real godot binary unavailable")
    monkeypatch.setenv("GQT_GODOT_BIN", godot)
    monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
    monkeypatch.setenv("GQT_AUTO_IMPORT", "1")
    root = tmp_path / "m8proj"
    shutil.copytree(FORMS_TEMPLATE, root)
    shutil.copytree(gut_src, root / "addons" / "gut")
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).parent))
    from attribution_fixtures import install_recipe
    from gherkin_fixtures import ensure_addon_metadata

    install_recipe(root)
    ensure_addon_metadata(root)
    (root / "calc_sign.gd").write_text(
        "extends RefCounted\nfunc sign(x: int) -> int:\n"
        "\tif x > 0:\n\t\treturn 1\n\treturn -1\n", encoding="utf-8")
    (root / "tests" / "a").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "a" / "test_green.gd").write_text(
        'extends GutTest\nconst C = preload("res://calc_sign.gd")\n'
        "func test_sign_positive():\n"
        "\tassert_eq(C.new().sign(5), 1)\n", encoding="utf-8")
    (root / ".gqt-scope.json").write_text(json.dumps(
        {"production": {"include": [
            {"kind": "dir", "value": "forms"},
            {"kind": "file", "value": "calc_sign.gd"}]}}),
        encoding="utf-8")
    sp.run([godot, "--headless", "--import", "--path", str(root)],
           capture_output=True, timeout=550)
    from godot_qa_toolkit.mutation.map_contract import refresh

    out = refresh(str(root), ["calc_sign.gd"], timeout_s=500)
    assert out.get("fresh") is True, out
    doc = json.loads((root / ".qa-cache" / "attribution-map.json")
                     .read_text(encoding="utf-8"))
    return root, doc["arms"]["gut_full"], doc


# ─────────────── M6：D2 unattributable 具名披露（2858dbb 上红） ───────────────
def test_d2_unattributable_disclosure_named():
    """rc=0 且零执行证据的账 → `_rerun_error_disclosure` 必须出具名
    unattributable 条目（2858dbb 上无此分支 → 披露为空，本测红）；
    真实通过的账（脚本头在场）不误伤。不涉身份比对：执行账由真实
    `_execution_record` 从真实 GUT 失败形态输出构造。"""
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


# ─────────────── M8：行事实双车道 · basename 口径（f09d5f0 上红） ───────────────
def test_line_fact_touchers_both_lanes_basename_form(tmp_path,
                                                     monkeypatch):
    """`_line_fact_touchers`：test 车道身份与 script 车道都按重跑面
    basename 口径出触达者。夹具 = 真实建图产物（决策 19，见模块
    docstring 的命名空间说明）；script 车道按升级过渡/遗留形态在真实
    产物上补一行真实宇宙成员（主流真实建图该车道为空）。红锚
    `f09d5f0`（script 车道回写完整 `res://` 身份未归一）。"""
    from godot_qa_toolkit.measurement.increment import _line_fact_touchers

    root, arm, _doc = _real_build_arm(tmp_path, monkeypatch)
    ident, by_rel = next((k, v) for k, v in
                         (arm.get("test_to_lines") or {}).items() if v)
    rel = sorted(by_rel)[0]
    # test 车道（纯真实建图产物）：左 = 真实身份脚本段 basename，
    # 右 = 同一 rel 的触达集
    touch = _line_fact_touchers(arm, rel)
    assert Path(ident.partition("::")[0]).name in touch, (ident, touch)
    # script 车道（升级过渡/遗留形态，真实宇宙成员）：键 = 真实建图
    # 宇宙内真实存在的脚本 res:// 完整路径（遗留形态不归一），右 =
    # 同一 rel；产品口径按 basename 归一
    script_res = "res://tests/test_forms.gd"
    assert (root / "tests" / "test_forms.gd").is_file(), script_res
    aug = dict(arm)
    aug["script_to_lines"] = {script_res: {rel: [1]}}
    touch_aug = _line_fact_touchers(aug, rel)
    assert Path(script_res).name in touch_aug, touch_aug
    assert Path(script_res).name not in touch, touch


# ─────────────── M10：gherkin 腿失败名截断前提取（972dcc4 上红） ───────────────
def test_leg_failing_tests_extracted_from_full_output(tmp_path,
                                                      monkeypatch):
    """失败名只在截断窗口之外可见（>4000 字符、标记行在窗口外）→
    `_gherkin_leg_execute` 的 `failing_tests` 仍非空（必须解析完整
    输出；972dcc4 上腿账无 `failing_tests` 键、失败名只解析截断尾部
    → 本测红）。失败名与套件路径均为真实产物派生（共享夹具工厂：
    真实编译器 `_ident` + `GEN_SCRIPT`）。"""
    from godot_qa_toolkit.measurement import increment_runner as ir
    from tests.unit.gherkin_verdict_fixtures import (
        FAIL_SCENARIO,
        GEN_SCRIPT,
        _test_name,
    )

    name = _test_name(FAIL_SCENARIO)
    gen_res = GEN_SCRIPT.rsplit("/", 1)[0]
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
        tmp_path, gen_res, {}, {}, [], "uidhardenr1m0010", 60, xdg)
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
        _SCENARIO_INDEX,
        _test_name,
    )

    assert _test_name(FAIL_SCENARIO).startswith(
        _ident(FAIL_SCENARIO, _SCENARIO_INDEX[FAIL_SCENARIO])), \
        _test_name(FAIL_SCENARIO)
