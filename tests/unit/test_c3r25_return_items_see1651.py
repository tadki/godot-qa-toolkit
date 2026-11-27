"""SEE-1651 C3-R25：复核退回项 N5–N8 的回归钉。

- N5（HIGH）：gherkin 增量腿成功路径——`_gherkin_leg_execute` 与
  `_failing_scenarios_backfill` 里的 `from .gutrun import` 是错误相对
  导入（gutrun 在 `mutation/` 包，measurement 包内无此模块），GUT 跑
  完即 ModuleNotFoundError，feature 改动 / 新增的续新必崩（C2-R4 起的
  backfill 一条 + C3-R24 N2 的一条）。用假 `_run_one_gut` 把两函数跑到
  GUT 返回之后，不依赖真 Godot。
- N6（HIGH）：重跑执行异常（rc 缺失 / error 在场）此前被折成 rc=0 →
  无失败名时按 rc==0 判绿——既有红身份被洗白成 green（假 kill 方向）。
  保守方向：异常账整脚本红并具名披露；gherkin 腿同口径（腿失败而
  failing_scenarios 为空集 = 不可归因 → 保守红，不得逐 scenario 放绿）。
- N7（HIGH）：`_content_changed_rerun` 把 `file_to_scripts` 的值（完整
  res:// 脚本身份）直接并进重跑面——`_increment_decision` 按 basename
  的 known 闸过滤后整批静默丢弃，非脚本内容修改的触达测试不重跑。
  重跑面是「选哪些脚本来跑」入口，按 basename 归一（决策 16）。
- N8（LOW）：`_canonical_verdict_dedup` / `_disclose_unresolvable_legacy`
  不跳过 `gherkin://` 身份——每次增量给每个 gherkin 判定身份记一条
  `legacy_basename_identity_kept_no_canonical_path` 失实披露。

单元红测试在 2f7788f 的产品代码上为红；真机段（真实 Godot）覆盖
N5 §SPEC-016 与 N7 内容车道的端到端成立，无真机按仓库惯例 skip。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ───────────────── N5：gherkin 增量腿成功路径（§SPEC-016） ─────────────────
def test_n5_gherkin_leg_success_path_survives_gut_return(tmp_path,
                                                         monkeypatch):
    """单元（零真机）：假 `_run_one_gut` + 跳过 validator 预检，把
    `run_gherkin_increment_leg` 跑到 GUT 返回之后——2f7788f 上
    `from .gutrun import` 在 measurement 包内解析失败
    （ModuleNotFoundError）→ 红；修后成功路径完整走通，执行账带
    `failing_tests` / `failing_scenarios`。"""
    import sys as _sys
    import types

    _sys.path.insert(0, str(Path(__file__).parent))

    from godot_qa_toolkit.measurement import increment_runner as ir
    from gherkin_fixtures import make_addon_project

    def fake_run_one_gut(root, argv, timeout_s, env=None, gate_root=None):
        return (types.SimpleNamespace(returncode=0, stdout="Run Summary\n",
                                      stderr=""), 0.1)

    monkeypatch.setattr(ir, "_run_one_gut", fake_run_one_gut)
    import godot_qa_toolkit.gherkin.addon as gaddon

    monkeypatch.setattr(gaddon, "run_validator_preflight",
                        lambda root: None)
    root = make_addon_project(tmp_path / "proj")
    fresh, executions = ir.run_gherkin_increment_leg(
        root, ["demo"], {}, {"recipe_hash": "x"}, False,
        "uidc3r25n50001", 60)
    assert executions, executions
    ex = executions[0]
    assert "failing_tests" in ex, ex
    assert ex.get("failing_scenarios") is not None, ex
    assert fresh is not None


def test_n5_gherkin_leg_execute_returns_failing_tests(tmp_path,
                                                      monkeypatch):
    """单元（零真机）：`_gherkin_leg_execute` 本体——假 GUT 返回带失败
    标记的输出 → 2f7788f 上在截断前提取处 ModuleNotFoundError；修后
    记录携带截断前解析的 `failing_tests`。"""
    import types

    from godot_qa_toolkit.measurement import increment_runner as ir

    out = ("res://gherkin/generated/x/gen_1.gd\n"
           "* test_s\n[Failed] test_s\n"
           "Run Summary\n- test_s\n[Failed] test_s\n")

    def fake_run_one_gut(root, argv, timeout_s, env=None, gate_root=None):
        return (types.SimpleNamespace(returncode=1, stdout=out, stderr=""),
                0.1)

    monkeypatch.setattr(ir, "_run_one_gut", fake_run_one_gut)
    root = tmp_path / "proj"
    root.mkdir()
    xdg: list[str] = []
    executions = ir._gherkin_leg_execute(
        root, "res://gherkin/generated/x", {}, {}, [], "uidc3r25n50002",
        60, xdg)
    assert executions, executions
    assert executions[0].get("failing_tests") == ["test_s"], executions


def test_matrix_feature_change_increment_real_godot(tmp_path, monkeypatch):
    """真机（§SPEC-016）：同名夹具建图 → 基线补种最小 gherkin 臂
    （stub 校验器预检可通过——真实 harness 的建图臂需要 KOL 全量运行
    时，不在合成夹具范围；腿的编译 / 回放 / 校验 / GUT 执行全链真机
    真实）→ 只改 feature 内容 → 续新增量成功、gherkin 重跑腿真实
    执行、执行账带 failing_tests / failing_scenarios（N5 修复前这条腿
    在 GUT 返回后必崩）。"""
    import test_c3r23_same_name_increment_see1648 as _v1

    root = _v1._build_same_name_project(tmp_path / "r25-feat")
    _v1._build_map(root, monkeypatch)
    map_file = root / ".qa-cache" / "attribution-map.json"
    doc = json.loads(map_file.read_text(encoding="utf-8"))
    ident = "gherkin://feature/demo::s::1"
    doc["arms"]["gherkin"] = {
        "feature_to_lines": {"demo": {"calc_sign.gd": [3]}},
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
    feat = root / "addons" / "gqt_gherkin" / "features" / "demo.feature"
    feat.write_text(
        feat.read_text(encoding="utf-8") + "# touched by C3-R25\n",
        encoding="utf-8")
    from godot_qa_toolkit.mutation.map_contract import refresh

    out = refresh(str(root), ["calc_sign.gd"], timeout_s=500)
    assert out.get("fresh") is True, out
    doc = json.loads(map_file.read_text(encoding="utf-8"))
    inc = doc.get("increment") or {}
    assert inc.get("mode") == "incremental", inc
    assert inc.get("gherkin_rerun_features") == ["demo"], inc
    gh_execs = inc.get("gherkin_leg_executions") or []
    assert gh_execs, inc
    assert "failing_tests" in gh_execs[0], gh_execs[0]
    assert gh_execs[0].get("failing_scenarios") is not None, gh_execs[0]


# ───────────────── N6：执行异常不得产出任何 green（§SPEC-038⑥ 同族） ─────────────────
def test_n6_execution_error_record_never_produces_green():
    """单元：基线含既有红身份，重跑执行账给一条
    `{"rc": None, "error": "GUT timed out …"}` → 该脚本全部身份保守红，
    不得出现 green（2f7788f 上 rc None 被折成 0 → 双身份全绿，本测为
    红）。"""
    from godot_qa_toolkit.measurement.increment import _merged_test_verdicts

    a = "res://tests/x/test_a.gd::test_r"
    b = "res://tests/x/test_a.gd::test_g"
    baseline = {"test_verdicts": {
        a: {"verdict": "red", "classification": "pre_existing",
            "script": "res://tests/x/test_a.gd"},
        b: {"verdict": "green"},
    }}
    merged = _merged_test_verdicts(
        baseline,
        [{"script": "test_a.gd", "rc": None, "out": "",
          "error": "GUT timed out after 180s "
                   "(args: -gselect=test_a.gd)"}],
        {"test_a.gd"}, set(), set(),
        None, set(), set(),
        tests_face={"tests/x/test_a.gd": "sha"},
        arm_identities=[a, b], collisions=[])
    assert merged is not None
    assert merged.get(a, {}).get("verdict") == "red", merged
    assert merged.get(b, {}).get("verdict") == "red", merged


def test_n6_gherkin_failed_leg_empty_failing_set_conservative_red():
    """单元（执行账失败语义两腿对齐）：gherkin 重跑腿失败
    （rc != 0）而 `failing_scenarios` 是空集（失败名不可归因）→ 保守
    整键红，不得逐 scenario 放绿（2f7788f 上空集按名判定 → 绿，本测为
    红）。"""
    from godot_qa_toolkit.measurement.increment import _merged_test_verdicts

    ident = "gherkin://feature/demo::s::1"
    baseline = {"test_verdicts": {ident: {"verdict": "green"}}}
    fresh_gherkin = {"executions": [{"rc": 1, "failing_scenarios": [],
                                      "out": "Run Summary\n"}]}
    merged = _merged_test_verdicts(
        baseline, [], set(), set(), set(),
        fresh_gherkin, {"demo"}, set(),
        tests_face=None, arm_identities=[], collisions=[])
    assert merged.get(ident, {}).get("verdict") == "red", merged


# ───────────────── N7：内容修改车道的重跑面身份口径 ─────────────────
def test_n7_content_change_rerun_face_includes_gd_toucher_tests(tmp_path):
    """单元：基线臂 `file_to_scripts` 用 res:// 完整身份，非脚本内容
    修改（被 prod gd preload）→ `_increment_decision` 的 rerun 必含
    触达消费 gd 的测试脚本（basename）、`rerun_sources` 带
    `content_prod:<gd>`（2f7788f 上完整路径被 basename known 闸静默
    丢弃 → 本测为红）。"""
    import hashlib

    from godot_qa_toolkit.measurement.increment import _increment_decision

    root = tmp_path / "proj"
    (root / "tests" / "x").mkdir(parents=True)
    content = root / "data.tres"
    content.write_text("k=1\n", encoding="utf-8")
    (root / "prod_user.gd").write_text(
        'extends RefCounted\nconst D = preload("res://data.tres")\n',
        encoding="utf-8")
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
            "script_to_lines": {},
            "file_to_scripts": {
                "prod_user.gd": ["res://tests/x/test_user.gd"]},
        }},
    }
    classes, invalidation, rerun, reason = _increment_decision(
        root, {"recipe_hash": "h-ok"}, baseline, ["data.tres"], 0, 5, 0.05)
    assert classes.get("content_changed") == ["data.tres"], classes
    assert "test_user.gd" in rerun, (rerun, invalidation["sources"])
    assert any("content_prod:prod_user.gd" in s
               for s in (invalidation["sources"].get("test_user.gd") or [])), \
        invalidation["sources"]


def test_matrix_content_change_real_godot(tmp_path, monkeypatch):
    """真机：内容文件被 prod gd preload、prod gd 被测试触达 → 改内容
    触发增量，`rerun_scripts` 必含触达测试（重跑面 = 静态直连消费者
    ∪ 触达消费 gd 的测试，因素表第 1 行）。"""
    import test_c3r23_same_name_increment_see1648 as _v1

    root = _v1._build_same_name_project(tmp_path / "r25-content")
    (root / "data.tres").write_text("k=1\n", encoding="utf-8")
    (root / "prod_user.gd").write_text(
        'extends RefCounted\nconst D = preload("res://data.tres")\n'
        "func value() -> int:\n\treturn 1\n", encoding="utf-8")
    (root / "tests" / "fill" / "test_data_user.gd").write_text(
        'extends GutTest\nconst U = preload("res://prod_user.gd")\n'
        "func test_user_value():\n\tassert_eq(U.value(), 1)\n",
        encoding="utf-8")
    scope = json.loads((root / ".gqt-scope.json")
                       .read_text(encoding="utf-8"))
    scope["production"]["include"].extend(
        [{"kind": "file", "value": "data.tres"},
         {"kind": "file", "value": "prod_user.gd"}])
    (root / ".gqt-scope.json").write_text(
        json.dumps(scope, ensure_ascii=False), encoding="utf-8")
    _v1._build_map(root, monkeypatch)
    map_file = root / ".qa-cache" / "attribution-map.json"
    (root / "data.tres").write_text("k=2\n", encoding="utf-8")
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


# ───────────────── N8：gherkin 身份不进旧 basename 披露 ─────────────────
def test_n8_gherkin_identities_not_disclosed_as_legacy_basename():
    """单元：判定表含 gherkin 身份 → 增量合并的 collisions 不得给它们
    记 `legacy_basename_identity_kept_no_canonical_path`（2f7788f 上每
    个 gherkin 身份一条失实披露 → 本测为红）。"""
    from godot_qa_toolkit.measurement.increment import _merged_test_verdicts

    ident = "gherkin://feature/demo::s::1"
    baseline = {"test_verdicts": {
        ident: {"verdict": "green", "classification": "pre_existing"},
        "res://tests/x/test_a.gd::t": {"verdict": "green"},
    }}
    collisions: list = []
    merged = _merged_test_verdicts(
        baseline, [], set(), set(), set(),
        None, set(), set(),
        tests_face={"tests/x/test_a.gd": "sha"},
        arm_identities=["res://tests/x/test_a.gd::t"],
        collisions=collisions)
    assert merged.get(ident, {}).get("verdict") == "green", merged
    assert collisions == [], collisions
