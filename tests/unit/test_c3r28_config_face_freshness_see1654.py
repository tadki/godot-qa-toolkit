"""SEE-1654 C3-R28（D4 + L-A/B/C + S-2）：配置面可构建性变化的新鲜度判定。

D4：建图时配置面可构建、现在不可构建（删除 / 损坏 / 不可解析）→ 具名
过期（`recipe: … recipe unbuildable …` / `report_recipe_hash: … recipe
unbuildable …` 因素）并预言具名全量（refresh 经全量腿具名失败——配置面
缺失时无法重建），不得静默判 fresh。判别器是真实建图器写入的建图时
事实键 `recipe_buildable`（建图仅在 recipe 可构建时产出本文件；增量
产物沿基线继承）——建图时即不可构建的合成夹具（无该键）保持现状跳过
（不被迫每次全量）。S-2 同源扫描：`.gqt-scope.json` 损坏、gherkin 单根
布局破坏同样以 `recipe unbuildable` 具名；`.gqt-scope.json` 删除、
`project.godot` 删除走既有 pristine 哈希失配（本轮不改，清单见交付评论）。
L-A：`fresh` 响应不回显上一次的 `increment.reason`（reason 只在本次
涉及重建时在场）。
L-B：`res://` 路径文案不再被 `Path()` 折叠丢斜杠。
L-C：plan 的地图修复提示与 P1 门提示不再指向已不需要或补不了的
`--auto-prepare`（它只出现在真能补的问题上）。

夹具（决策 19 口径）：pristine / 注入哈希由当前树真实现算（与建图器
同源），复用 test_c3r19 的真实形态项目与新鲜地图工厂。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.unit.test_c3r19_map_contract_fixes_see1645 import (
    _bypass_entry_helpers,
    _fresh_doc,
    _make_project,
    _run_cli,
    _write_map,
)

MAP_REL = Path(".qa-cache") / "attribution-map.json"


def _clear_recipe_caches() -> None:
    """真实 CLI 每次调用是冷进程；单测同进程内 recipe 哈希带 lru_cache，
    树变更后清缓存以等价冷进程语义（建图 → 变更 → 再判定）。"""
    from godot_qa_toolkit.mutation.truth_select import (
        _current_report_recipe_hash_cached, _fresh_recipe_hash,
    )

    _fresh_recipe_hash.cache_clear()
    _current_report_recipe_hash_cached.cache_clear()


def _fresh_doc_with_build_fact(proj: Path) -> dict:
    """真实形态新鲜地图 + 建图时事实（真实建图器写入 `recipe_buildable`：
    build_recipe 成功是建图的前置条件，产出本文件的构建必然可构建）。"""
    doc = _fresh_doc(proj)
    doc["recipe_buildable"] = True
    return doc


def _status(proj: Path) -> dict:
    from godot_qa_toolkit.mutation.map_contract import status

    return status(str(proj), [])


# ─────────────────────────────── D4 ───────────────────────────────

def test_gutconfig_deleted_named_stale_and_full_prediction(tmp_path):
    """删除 `.gutconfig.json`：status 具名过期（recipe unbuildable 因素）
    且预言具名全量——不得静默 fresh（QA-R6 V-2 反例，099bbe6 上假新鲜）。"""
    proj = _make_project(tmp_path)
    _write_map(proj, _fresh_doc_with_build_fact(proj))
    assert _status(proj)["fresh"] is True
    (proj / ".gutconfig.json").unlink()
    _clear_recipe_caches()
    st = _status(proj)
    assert st["fresh"] is False, st
    assert any("recipe unbuildable" in f for f in st["factors"]), st
    assert st["would_refresh"] == "full", st


def test_gutconfig_unparseable_named_stale(tmp_path):
    """`.gutconfig.json` 损坏（不可解析）：同具名过期（S-2：不可构建
    形态与删除同源闭合）。"""
    proj = _make_project(tmp_path)
    _write_map(proj, _fresh_doc_with_build_fact(proj))
    (proj / ".gutconfig.json").write_text("{not json", encoding="utf-8")
    _clear_recipe_caches()
    st = _status(proj)
    assert st["fresh"] is False, st
    assert any("recipe unbuildable" in f for f in st["factors"]), st


def test_gutconfig_deleted_refresh_named_failure(tmp_path):
    """删除 `.gutconfig.json` 后 refresh：预言 full → 全量腿在 recipe
    构建处具名失败（配置面缺失无法重建），旧图字节不变——不得静默
    already_fresh。"""
    proj = _make_project(tmp_path)
    map_file = _write_map(proj, _fresh_doc_with_build_fact(proj))
    before = map_file.read_bytes()
    (proj / ".gutconfig.json").unlink()
    _clear_recipe_caches()
    from godot_qa_toolkit.mutation.map_contract import (
        MapContractError,
        refresh,
    )
    from godot_qa_toolkit.mutation.verdict_source import BaselineSourceError

    with pytest.raises((MapContractError, BaselineSourceError)) as ei:
        refresh(str(proj), [], timeout_s=60)
    assert "recipe" in str(ei.value) or "gutconfig" in str(ei.value)
    assert map_file.read_bytes() == before


def test_gutconfig_deleted_consumers_named_not_silent(tmp_path):
    """删除 `.gutconfig.json` 后共享新鲜度谓词（coverage / risk /
    mutation 基线同源）具名失败——不得静默消费配置面过期的地图。"""
    proj = _make_project(tmp_path)
    doc = _write_map(proj, _fresh_doc_with_build_fact(proj))
    (proj / ".gutconfig.json").unlink()
    _clear_recipe_caches()
    from godot_qa_toolkit.mutation.verdict_source import (
        BaselineSourceError,
        assert_map_fresh,
    )

    with pytest.raises(BaselineSourceError, match="recipe unbuildable"):
        assert_map_fresh(str(proj), json.loads(doc.read_text(
            encoding="utf-8")), [])


# ─────────────────────────── S-2 同类扫描 ───────────────────────────

def test_scope_corrupted_named_stale(tmp_path):
    """`.gqt-scope.json` 损坏（build_recipe 经 scope_summary 具名拒绝）
    → recipe 不可构建 → 与 D4 同源具名过期。"""
    proj = _make_project(tmp_path)
    _write_map(proj, _fresh_doc_with_build_fact(proj))
    (proj / ".gqt-scope.json").write_text('{"production": "bogus"',
                                         encoding="utf-8")
    _clear_recipe_caches()
    st = _status(proj)
    assert st["fresh"] is False, st
    assert any("recipe unbuildable" in f for f in st["factors"]), st


def test_gherkin_adapter_face_missing_named_stale(tmp_path):
    """gherkin 单根布局破坏（addon 目录缺失 = 适配源空集 → build_recipe
    具名拒绝）→ recipe 不可构建 → 与 D4 同源具名过期。"""
    proj = _make_project(tmp_path)
    _write_map(proj, _fresh_doc_with_build_fact(proj))
    import shutil

    shutil.rmtree(proj / "addons" / "gqt_gherkin")
    _clear_recipe_caches()
    st = _status(proj)
    assert st["fresh"] is False, st
    assert any("recipe unbuildable" in f for f in st["factors"]), st


def test_scope_deleted_stale_via_existing_hash_mismatch(tmp_path):
    """S-2 不修项佐证：`.gqt-scope.json` 删除且规则实际改变 recipe 面
    （`source_classification.rules_sha256` 进哈希）→ 既有 pristine 哈希
    失配已具名过期（非 None 跳过面，本轮不改）。"""
    proj = _make_project(tmp_path)
    (proj / ".gqt-scope.json").write_text(json.dumps({
        "production": {"include": [{"kind": "file", "value": "prod.gd"}],
                       "exclude": []},
        "tests": {"exclude": [{"kind": "dir", "value": "tests",
                               "reason": "c3r28 probe"}]}},
        ensure_ascii=False), encoding="utf-8")
    doc = _fresh_doc_with_build_fact(proj)
    # scope 规则进 recipe 指纹——基线哈希在删除前按带规则树现算
    from godot_qa_toolkit.mutation.truth_select import (
        _current_report_recipe_hash, _fresh_recipe_hash)

    root = str(proj.resolve())
    doc["recipe_hash_pristine"] = _fresh_recipe_hash(root)
    doc["report_recipe_hash"] = _current_report_recipe_hash(root)
    _write_map(proj, doc)
    assert _status(proj)["fresh"] is True
    (proj / ".gqt-scope.json").unlink()
    _clear_recipe_caches()
    st = _status(proj)
    assert st["fresh"] is False, st
    assert any(f.startswith("recipe:") for f in st["factors"]), st


def test_project_godot_deleted_stale_via_existing_hash_mismatch(tmp_path):
    """S-2 不修项佐证：`project.godot` 删除 → project_face 内容变化 →
    既有 pristine 哈希失配已具名过期（非 None 跳过面，本轮不改）。"""
    proj = _make_project(tmp_path)
    (proj / "project.godot").write_text("config_version=5\n",
                                        encoding="utf-8")
    doc = _fresh_doc_with_build_fact(proj)
    from godot_qa_toolkit.mutation.truth_select import (
        _current_report_recipe_hash, _fresh_recipe_hash)

    root = str(proj.resolve())
    doc["recipe_hash_pristine"] = _fresh_recipe_hash(root)
    doc["report_recipe_hash"] = _current_report_recipe_hash(root)
    _write_map(proj, doc)
    assert _status(proj)["fresh"] is True
    (proj / "project.godot").unlink()
    _clear_recipe_caches()
    st = _status(proj)
    assert st["fresh"] is False, st
    assert any(f.startswith("recipe:") for f in st["factors"]), st


def test_never_buildable_recipe_map_keeps_current_behavior(tmp_path):
    """建图时即不可构建（合成夹具形态：树无 `.gutconfig.json`、地图无
    `recipe_buildable` 事实键与 recipe 哈希记录）→ 保持现状跳过 pristine
    对账，不被迫每次全量（决策 9：默认行为不变）。"""
    proj = _make_project(tmp_path)
    doc = _fresh_doc_with_build_fact(proj)
    # 夹具形态（建图时即不可构建）：无建图事实、无 recipe 哈希记录
    doc.pop("recipe_buildable", None)
    doc.pop("recipe_hash_pristine", None)
    doc.pop("report_recipe_hash", None)
    _write_map(proj, doc)
    (proj / ".gutconfig.json").unlink()
    _clear_recipe_caches()
    st = _status(proj)
    assert st["fresh"] is True, st


# ─────────────────────────────── L-A ───────────────────────────────

def test_map_update_fresh_response_does_not_echo_stale_reason(
        tmp_path, monkeypatch):
    """L-A（B7）：`mode=fresh` 响应不携带 `reason`——上一次重建的台账
    `increment.reason` 不得回显进本次"什么都没做"的响应（reason 只在
    本次涉及重建时在场）。"""
    proj = _make_project(tmp_path)
    doc = _fresh_doc_with_build_fact(proj)
    # 真实全量重建产物形态：increment 台账带上一轮的回退原因（QA-R6 V-3）
    doc["increment"] = {"mode": "gherkin_rerun_surface_too_large_fallback",
                        "reason": "gherkin_rerun_surface_too_large_fallback"}
    _write_map(proj, doc)
    _bypass_entry_helpers(monkeypatch, proj)
    rc, out, err = _run_cli(["measure", "map-update",
                             "--project-root", str(proj)])
    assert rc == 0, err
    payload = json.loads(out)
    summary = payload.get("summary") or {}
    assert summary.get("mode") == "fresh", summary
    assert summary.get("already_fresh") is True, summary
    assert "reason" not in summary, summary


# ─────────────────────────────── L-B ───────────────────────────────

def test_res_path_factor_text_keeps_double_slash(tmp_path):
    """L-B（B10）：`res://` 路径经漂移分类后文案原样保留双斜杠——
    `Path()` 的 POSIX 折叠（`res://x` → `res:/x`）不再丢斜杠。"""
    proj = _make_project(tmp_path)
    doc = _fresh_doc_with_build_fact(proj)
    from godot_qa_toolkit.mutation.verdict_source import _map_drift

    changed, drift = _map_drift(doc, str(proj), ["res://notes.md"])
    assert "res://notes.md" in changed, (changed, drift)
    assert all("res:/notes.md" not in d for d in drift), drift


# ─────────────────────────────── L-C ───────────────────────────────

def test_plan_map_problem_hint_points_to_prepare_renewal(tmp_path):
    """L-C（B11①）：地图缺失问题的修复提示指向 prepare 自动续新 /
    `gqt measure map-update`——不再写"rerun with --auto-prepare to build
    it automatically"（无旗标即自动建图，文案与行为一致）。"""
    proj = _make_project(tmp_path)
    from godot_qa_toolkit.mutation.plancheck import static_plan

    plan = static_plan(str(proj), str(proj / "prod.gd"), None)
    problem = next(p for p in plan["problems"]
                   if p.get("code") == "attribution_map_missing_or_stale")
    fix = str(problem.get("fix") or "")
    assert "--auto-prepare" not in fix, fix
    assert "measure map-update" in fix, fix


def test_p1_gate_notice_tails_auto_prepare_to_covered_problems_only():
    """L-C（B11②）：P1 门提示按剩余问题的 `auto_prepare_covered` 具体化
    ——`--auto-prepare` 只出现在它真能补的问题上（如 `gutconfig_missing`
    补不了，提示不得指向它）。"""
    import argparse

    from godot_qa_toolkit.cli import _p1_gate_notice

    args = argparse.Namespace()
    uncovered = [{"code": "gutconfig_missing", "auto_prepare_covered": False}]
    notice = _p1_gate_notice(args, uncovered)
    assert "--auto-prepare" not in notice, notice
    covered = [{"code": "import_cache_missing", "auto_prepare_covered": True}]
    assert "--auto-prepare" in _p1_gate_notice(args, covered)
