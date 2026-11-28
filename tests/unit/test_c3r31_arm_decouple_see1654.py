"""SEE-1654 C3-R31（决策 25 + D8 + S-4）：feature 臂与 GUT 臂解耦、
预言 = 实际动作的组合完备性（§SPEC-039 / §SPEC-040）。

S-4 矩阵（「因素 → status 预言 → refresh 实际动作」单一出处——因素面
与 status 共用 `_factor_report` / `_FORCED_FULL_FACTORS`，阶梯按同一份
判定分流；矩阵常驻化即本文件）：

- 缺 `feature_sha256` 键（±feature 漂移）→ 只重建 feature 臂（决策 25；
  reason 具名随台账，R13；业务 GUT 进程 0 由腿端预检先于业务扇出保证）
- 缺判定表（±feature 漂移）→ 两臂全量自愈（D8/§SPEC-040：结构性缺失
  不可继承，不得走增量后具名拒绝，R14b）
- recipe 失配 / 工具链面漂移 → 两臂全量（决策 7）
- feature/内容漂移 → 增量（D5）

决策 25（§SPEC-039）：增量腿失败（预检/编译/适配器/suite 一族）→
feature 臂记 invalid（`arms_state.gherkin` = invalid + 具名原因，
`arms.gherkin` 不出场、`test_verdicts` 无 `gherkin://` 行、GUT 臂原样
继承、`feature_sha256` 按当前盘上内容写入、私有 suite 目录零残留）；
腿成功则按本轮真实结果重算（invalid 不被永久钉死）。

身份形态夹具（决策 19）：gherkin 身份第二段由真实编译命名函数派生；
GUT 判定行复用 test_c3r19 的真实形态新鲜地图工厂。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# S-4：因素 → 预言 → 实际动作 组合矩阵（阶梯分流）
# ---------------------------------------------------------------------------

def _make_project(tmp_path: Path) -> Path:
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).parent))
    from test_c3r19_map_contract_fixes_see1645 import _make_project

    return _make_project(tmp_path)


def _fresh_doc(proj: Path) -> dict:
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).parent))
    from test_c3r19_map_contract_fixes_see1645 import _fresh_doc

    return _fresh_doc(proj)


def _write_map(proj: Path, doc: dict) -> Path:
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).parent))
    from test_c3r19_map_contract_fixes_see1645 import _write_map

    return _write_map(proj, doc)


def _ladder(monkeypatch: pytest.MonkeyPatch, proj: Path, doc: dict,
            files: "list[str]" = []) -> dict:
    """驱动续新阶梯（准备函数替换为记录器），返回 {动作: 参数}。"""
    import godot_qa_toolkit.mutation.map_contract as mc
    import godot_qa_toolkit.mutation.verdict_source as vs

    calls: dict = {"full": 0, "increment": 0, "increment_kwargs": {}}

    def fake_full(root, files_, path, timeout_s):
        calls["full"] += 1

    def fake_inc(doc_, root, path, changed, timeout_s,
                 gherkin_arm_full_reason=None):
        calls["increment"] += 1
        calls["increment_kwargs"] = {"changed": list(changed),
                                     "reason": gherkin_arm_full_reason}

    monkeypatch.setattr(vs, "_prepare_full_rebuild", fake_full)
    monkeypatch.setattr(vs, "_prepare_increment_renewal", fake_inc)
    from godot_qa_toolkit.mutation.verdict_source import _prepare_drift_state

    changed, drift, _ok = _prepare_drift_state(doc, str(proj), files)
    mc._renewal_ladder(doc, str(proj), str(
        proj / ".qa-cache" / "attribution-map.json"), files, changed, drift,
        timeout_s=60)
    return calls


def test_d8_missing_table_plus_feature_drift_routes_full(
        tmp_path, monkeypatch):
    """D8/R14b：缺判定表 + feature 面漂移同时在场 → 预言 full、实际动作
    = 两臂全量（不得走增量后具名拒绝）。`53a8b46` 上阶梯走增量分支
    （RED），决策 20：缺表不可继承属 GUT 侧因素。"""
    proj = _make_project(tmp_path)
    doc = _fresh_doc(proj)
    # 结构性缺失与 feature 漂移同时注入
    doc.pop("test_verdicts", None)
    feat = (proj / "addons" / "gqt_gherkin" / "features" / "demo.feature")
    feat.write_text(feat.read_text(encoding="utf-8") + "\n# drift\n",
                    encoding="utf-8")
    _write_map(proj, doc)
    from godot_qa_toolkit.mutation.map_contract import status

    st = status(str(proj), [])
    assert st.get("would_refresh") == "full", st
    calls = _ladder(monkeypatch, proj, doc)
    assert calls["full"] == 1, calls
    assert calls["increment"] == 0, calls


def test_missing_feature_key_routes_arm_level_even_with_drift(
        tmp_path, monkeypatch):
    """R13：缺 `feature_sha256` 键（含 feature 面同时有漂移）→ 只重建
    feature 臂（reason 具名），不得降级两臂全量。"""
    proj = _make_project(tmp_path)
    doc = _fresh_doc(proj)
    doc.pop("feature_sha256", None)
    feat = (proj / "addons" / "gqt_gherkin" / "features" / "demo.feature")
    feat.write_text(feat.read_text(encoding="utf-8") + "\n# drift\n",
                    encoding="utf-8")
    calls = _ladder(monkeypatch, proj, doc)
    assert calls["increment"] == 1, calls
    assert calls["increment_kwargs"]["reason"] == \
        "gherkin_face_missing_fingerprint_full", calls
    assert calls["full"] == 0, calls


def test_feature_content_drift_routes_increment(tmp_path, monkeypatch):
    """feature 内容漂移 → 增量（D5 既有语义不回退）。"""
    proj = _make_project(tmp_path)
    doc = _fresh_doc(proj)
    feat = (proj / "addons" / "gqt_gherkin" / "features" / "demo.feature")
    feat.write_text(feat.read_text(encoding="utf-8") + "\n# drift\n",
                    encoding="utf-8")
    calls = _ladder(monkeypatch, proj, doc)
    assert calls["increment"] == 1, calls
    assert calls["full"] == 0, calls


def test_recipe_mismatch_routes_full(tmp_path, monkeypatch):
    """recipe 失配（配置面）→ 两臂全量（决策 7；不因 feature 面干净而
    走增量）。"""
    proj = _make_project(tmp_path)
    doc = _fresh_doc(proj)
    doc["recipe_hash_pristine"] = "stale-not-real"
    calls = _ladder(monkeypatch, proj, doc)
    assert calls["full"] == 1, calls
    assert calls["increment"] == 0, calls


# ---------------------------------------------------------------------------
# 决策 25：增量腿失败 → feature 臂 invalid（产物形态 = 全量 invalid 臂）
# ---------------------------------------------------------------------------

def _invalid_arm_doc() -> dict:
    """腿失败语义的真实形态锚：腿调用返回 invalid 原因（行为面单独测）。"""
    return {"reason": ("feature arm rebuild failed: GherkinRunError: "
                       "gherkin preflight failed: V2 red")}


def test_leg_failure_family_returns_arm_invalid_reason(monkeypatch, tmp_path):
    """腿失败（GherkinRunError 族）→ 返回具名 invalid 原因（不抛出、
    不降级两臂全量）；其余异常原样抛出。"""
    import godot_qa_toolkit.measurement.increment as inc
    import godot_qa_toolkit.measurement.increment_runner as irr
    from godot_qa_toolkit.gherkin.run import GherkinRunError

    def red_leg(*_a, **_k):
        raise GherkinRunError("gherkin preflight failed: V2 red")

    monkeypatch.setattr(irr, "run_gherkin_increment_leg", red_leg)
    fresh, execs, gh_s, arm_invalid = inc._gherkin_leg_run(
        Path(tmp_path), {"demo"}, {}, {}, False, 60)
    assert fresh is None and execs == [] and gh_s == 0.0
    assert arm_invalid and "feature arm rebuild failed" in arm_invalid

    def boom(*_a, **_k):
        raise RuntimeError("programmer error")

    monkeypatch.setattr(irr, "run_gherkin_increment_leg", boom)
    with pytest.raises(RuntimeError):
        inc._gherkin_leg_run(Path(tmp_path), {"demo"}, {}, {}, False, 60)


def test_increment_artifact_invalid_arm_shape(tmp_path):
    """决策 25/§SPEC-039 产物形态：feature 臂 invalid（具名原因）、
    arms.gherkin 不出场、判定表无 gherkin:// 行、GUT 臂判定与
    arms_state 原样继承、feature_sha256 按当前盘上内容写入。"""
    proj = _make_project(tmp_path)
    baseline = _fresh_doc(proj)
    # 基线含 feature 臂行事实与 gherkin 判定行（真实形态：恢复 C3-R27
    # 轮验证过的身份构造——第二段为真实编译命名派生）
    from gherkin_verdict_fixtures import _ident_of, _test_name

    gh_ident = _ident_of(_test_name("50×50 最坏混合布局单秒稳态求解 p95 预算"))
    baseline["arms"]["gherkin"] = {
        "feature_to_lines": {"demo": {"prod.gd": [7]}},
        "test_to_lines": {gh_ident: {"prod.gd": [7]}}, "pairs": 1}
    baseline["arms_state"]["gherkin"] = {"state": "valid", "pairs": 1}
    baseline["test_verdicts"][gh_ident] = {"verdict": "green"}
    from godot_qa_toolkit.measurement.increment import (
        _build_increment_artifact,
    )

    classes = {"prod_changed": [], "prod_new": [], "prod_removed": [],
               "test_changed": [], "test_new": [], "test_removed": [],
               "feature_changed": ["demo"], "feature_new": [],
               "feature_removed": [], "unknown": []}
    artifact = _build_increment_artifact(
        baseline, dict(baseline["arms"]["gut_full"]),
        {"recipe_hash": baseline["report_recipe_hash"],
         "gherkin_face": {"feature_sha256": {"demo": "cur"}}},
        proj, classes, [], {"rerun": set(), "dropped": set(),
                            "gherkin_rerun": ["demo"], "gherkin_dropped": [],
                            "sources": {}},
        0, 1.0, [], merged_gherkin=None, gh_executions=[],
        merged_verdicts=dict(baseline["test_verdicts"]),
        gherkin_arm_full_reason="gherkin_rerun_surface_too_large_fallback",
        gherkin_arm_invalid_reason="feature arm rebuild failed: V2 red")
    arms = artifact.get("arms") or {}
    assert "gherkin" not in arms, sorted(arms)
    state = (artifact.get("arms_state") or {}).get("gherkin") or {}
    assert state.get("state") == "invalid", state
    assert state.get("invalid_reason") == \
        "feature arm rebuild failed: V2 red", state
    gh_rows = [k for k in (artifact.get("test_verdicts") or {})
               if str(k).startswith("gherkin://")]
    assert gh_rows == [], gh_rows
    gut_state = (artifact.get("arms_state") or {}).get("gut_full") or {}
    assert gut_state == (baseline.get("arms_state") or {}).get("gut_full")
    assert artifact.get("feature_sha256") == {"demo": "cur"}


def test_leg_success_recomputes_arm_state(tmp_path):
    """腿成功 → feature 臂按本轮真实结果重算（invalid 不被永久钉死）。"""
    proj = _make_project(tmp_path)
    baseline = _fresh_doc(proj)
    baseline["arms_state"]["gherkin"] = {"state": "invalid",
                                         "invalid_reason": "old failure"}
    from godot_qa_toolkit.measurement.increment import (
        _build_increment_artifact,
    )
    from gherkin_verdict_fixtures import _ident_of, _test_name

    gh_ident = _ident_of(_test_name("50×50 最坏混合布局单秒稳态求解 p95 预算"))
    fresh_gherkin = {"feature_to_lines": {"demo": {"prod.gd": [7]}},
                     "test_to_lines": {gh_ident: {"prod.gd": [7]}},
                     "pairs": 1}
    classes = {"prod_changed": [], "prod_new": [], "prod_removed": [],
               "test_changed": [], "test_new": [], "test_removed": [],
               "feature_changed": ["demo"], "feature_new": [],
               "feature_removed": [], "unknown": []}
    artifact = _build_increment_artifact(
        baseline, dict(baseline["arms"]["gut_full"]),
        {"recipe_hash": baseline["report_recipe_hash"],
         "gherkin_face": {"feature_sha256": {"demo": "cur"}}},
        proj, classes, [], {"rerun": set(), "dropped": set(),
                            "gherkin_rerun": ["demo"], "gherkin_dropped": [],
                            "sources": {}},
        0, 1.0, [], merged_gherkin=fresh_gherkin, gh_executions=[],
        merged_verdicts=dict(baseline["test_verdicts"]),
        gherkin_arm_full_reason="gherkin_rerun_surface_too_large_fallback")
    arms = artifact.get("arms") or {}
    assert arms.get("gherkin") == fresh_gherkin
    state = (artifact.get("arms_state") or {}).get("gherkin") or {}
    assert state.get("pairs") == 1, state
    assert state.get("state") != "invalid", state


# ---------------------------------------------------------------------------
# 零残留：compile_suite 失败自回收私有 suite 目录
# ---------------------------------------------------------------------------

def test_compile_suite_failure_cleans_private_suite(tmp_path, monkeypatch):
    """预检/编译失败 → `.gqt-run-*` 私有 suite 目录自回收（C3-R31：Atlas
    实测 5 个残留目录的根因——失败路径早于调用方 try/finally）。"""
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).parent))
    from gherkin_fixtures import make_addon_project
    from godot_qa_toolkit.gherkin.suite import SuiteError
    from godot_qa_toolkit.measurement import increment_runner as irr

    proj = make_addon_project(tmp_path / "proj")
    (proj / "addons" / "gqt_gherkin" / "features" / "demo.feature") \
        .write_text(
            "Feature: F\n  Scenario Outline: s\n    Then hp is <left>\n",
            encoding="utf-8")

    def boom(*_a, **_k):
        raise SuiteError("compile exploded")

    from godot_qa_toolkit.gherkin import suite as _suite_mod

    monkeypatch.setattr(_suite_mod, "compile_keys_to_suite", boom)
    with pytest.raises(SuiteError):
        irr._gherkin_leg_compile_suite(proj, ["demo"], "abcdef012345")
    leftover = sorted(p.name for p in (proj / "gherkin" / "generated")
                      .glob(".gqt-run-*"))
    assert leftover == [], leftover
