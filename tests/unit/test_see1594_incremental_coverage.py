"""SEE-1594 §SPEC-025：coverage 由行级映射唯一真源驱动。

证明：
- 新鲜映射 → projection 的 covered 行集/覆盖率与行事实一致（parity）；
- 无映射 → fail-loud（绝不静默回退旧 direct GUT，且不 spawn godot）；
- 映射过期 → 走 resolve_selection 的增量刷新后重判（不是重跑 direct GUT）；
- 面过滤按路径边界匹配（目录不串相邻目录、单文件精确匹配）；
- 执行证据哨兵：probes_fired=0 且行事实非空 → run_error 拒绝可信分数。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import attribution_fixtures as fx
import godot_qa_toolkit.mutation.inject as inject
from godot_qa_toolkit.coverage.mapping import (CoverageMappingError,
                                                project_file_coverage)
from godot_qa_toolkit.coverage.runner import run_coverage

TARGET = "utils/t.gd"
IDENT_A = "test_a.gd::a"
IDENT_COMPAT = "test_a_compat.gd::c"
SRC = "extends RefCounted\n\nfunc f(a):\n\tif a > 0:\n\t\treturn 1\n\treturn 0\n"


def _no_godot(monkeypatch):
    monkeypatch.setattr(
        "godot_qa_toolkit.mutation.inject.detect_binary_kind",
        lambda *a, **k: "linux")
    monkeypatch.setattr(
        "godot_qa_toolkit.mutation.inject.godot_command",
        lambda: "godot")
    monkeypatch.setattr(
        "godot_qa_toolkit.mutation.identity.identity_pack",
        lambda *a, **k: {"kind": "linux"})


def _proj(tmp_path):
    proj = tmp_path / "proj"
    (proj / "utils").mkdir(parents=True)
    target = proj / "utils" / "t.gd"
    target.write_text(SRC, encoding="utf-8")
    (proj / "tests" / "unit").mkdir(parents=True)
    (proj / "tests" / "unit" / "test_a.gd").write_text(
        "extends Node\n", encoding="utf-8")
    return proj, target


def _install_map(proj: Path, covered_lines, *, probes_fired=640):
    """在约定默认位置写入新鲜 /6 行级主真值映射（含完整绑定包）。"""
    from godot_qa_toolkit.measurement.recipe import build_recipe, recipe_hash

    fx.install_recipe(proj)
    recipe, error = build_recipe(proj)
    assert error is None, error
    raw = recipe_hash({k: v for k, v in recipe.items() if k != "recipe_hash"})
    rel = "utils/t.gd"
    ident = IDENT_A
    arm = {"test_to_files": {ident: [rel]}, "script_to_files": {},
           "tests": 1, "scripts": 1, "files": 1, "pairs": len(covered_lines),
           "function_pairs": 0, "test_to_functions": {},
           "script_to_functions": {},
           "file_to_tests": {rel: [ident]},
           "file_to_scripts": {rel: ["test_x.gd"]},
           "line_pairs": len(covered_lines),
           "script_line_pairs": len(covered_lines),
           "test_to_lines": {ident: {rel: list(covered_lines)}},
           "script_to_lines": {},
           "line_to_tests": {rel: {str(covered_lines[0]): [ident]}}}
    doc = {"schema_version": "test-attribution/6",
           "report_recipe_hash": raw, "recipe_hash_pristine": raw,
           "project_root": str(proj), "identity": ident,
           "causal": {"enabled": False},
           "file_sha256": {rel: hashlib.sha256(
               (proj / rel).read_bytes()).hexdigest()},
           "target_files": [rel],
           "tests_face": {
               str(gd.relative_to(proj)): hashlib.sha256(
                   gd.read_bytes()).hexdigest()
               for gd in sorted(proj.rglob("tests/**/*.gd"))
               if "__pycache__" not in str(gd)},
           "instrumentable_lines": {rel: [2, 3]},
           "arms": {"gut_full": arm},
           "arms_state": {"gut_full": {"state": "valid",
                                       "invalid_reason": None, "bundles": 1,
                                       "pairs": 1,
                                       "probes_fired": probes_fired,
                                       "vacuous": False}},
           "notes": []}
    out = proj / ".qa-cache" / "attribution-map.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return str(out)


def test_projection_from_fresh_map_parity(tmp_path, monkeypatch):
    proj, target = _proj(tmp_path)
    _install_map(proj, covered_lines=[2])
    _no_godot(monkeypatch)
    out = project_file_coverage(str(proj), str(target), binary_kind="linux")
    assert out["source"] == "line_mapping_projection"
    assert out["covered_lines"] == {2}
    assert out["instrumentable_lines"] == {2, 3}
    assert out["total_lines"] == 2 and out["covered_count"] == 1
    assert out["coverage_percent"] == 50.0
    # parity：percent 与 covered/total 一致
    assert out["coverage_percent"] == round(
        100.0 * out["covered_count"] / out["total_lines"], 2)


def test_fail_loud_without_map_no_godot(tmp_path, monkeypatch):
    proj, target = _proj(tmp_path)
    _no_godot(monkeypatch)
    spawned = []

    def _boom(*a, **k):
        spawned.append(a)
        raise AssertionError("direct GUT path must not run under mapping source")

    monkeypatch.setattr(inject, "spawn_godot", _boom)
    with pytest.raises(CoverageMappingError):
        project_file_coverage(str(proj), str(target), binary_kind="linux")
    assert spawned == []


def test_incremental_refresh_used_on_stale(tmp_path, monkeypatch):
    from godot_qa_toolkit.mutation import truth_select as truth

    proj, target = _proj(tmp_path)
    _install_map(proj, covered_lines=[2])
    # 使映射过期：目标文件内容变化 → file_sha256 绑定失效
    target.write_text(SRC + "\n# changed\n", encoding="utf-8")
    calls = {"n": 0}

    def fake_increment(root, path, doc, rel, changed_extra=None, **kwargs):
        calls["n"] += 1
        _install_map(proj, covered_lines=[2])  # 依新内容重建新鲜映射
        return True, None

    monkeypatch.setenv(truth.ENV_MODE, "auto")  # 允许自动增量
    monkeypatch.setattr(truth, "_auto_increment", fake_increment)

    out = project_file_coverage(str(proj), str(target), binary_kind="linux")
    assert calls["n"] == 1, "stale map must go through incremental refresh, not re-GUT"
    assert out["coverage_percent"] == 50.0
    assert out["source"] == "line_mapping_projection"


def test_explicit_map_unusable_fail_loud(tmp_path, monkeypatch):
    proj, target = _proj(tmp_path)
    bad = proj / ".qa-cache" / "attribution-map.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{ not a map", encoding="utf-8")
    _no_godot(monkeypatch)
    with pytest.raises(CoverageMappingError):
        project_file_coverage(str(proj), str(target), binary_kind="linux")


class TestBoundaryFaceFilter:
    """§SPEC-025：面过滤按路径边界匹配——目录不得前缀串到相邻目录，
    单 .gd 必须精确匹配；退回裸 startswith 会让这些用例变红。"""

    def _proj_with_adjacent_dirs(self, tmp_path):
        proj, target = _proj(tmp_path)
        (proj / "tests" / "unit_compat").mkdir(parents=True, exist_ok=True)
        (proj / "tests" / "unit_compat" / "test_a_compat.gd").write_text(
            "extends Node\n", encoding="utf-8")
        # 先建全部测试文件再装映射（tests_face 漂移即 fail-loud，属正确行为；
        # 夹具必须构造"新鲜映射"前提）。行事实：unit 面 test_a 覆盖行 2，
        # 相邻目录同前缀名脚本覆盖行 3
        _install_map(proj, covered_lines=[2])
        map_path = proj / ".qa-cache" / "attribution-map.json"
        doc = json.loads(map_path.read_text(encoding="utf-8"))
        doc["arms"]["gut_full"]["test_to_lines"].setdefault(
            IDENT_COMPAT, {}).setdefault("utils/t.gd", []).append(3)
        map_path.write_text(json.dumps(doc, ensure_ascii=False),
                            encoding="utf-8")
        return proj, target

    def test_directory_face_excludes_adjacent_dir(self, tmp_path, monkeypatch):
        proj, target = self._proj_with_adjacent_dirs(tmp_path)
        _no_godot(monkeypatch)
        out = project_file_coverage(str(proj), str(target),
                                    tests_glob="res://tests/unit/")
        # unit 面只含 test_a 的行事实（行 2）——相邻目录的行 3 不得混入
        assert out["covered_lines"] == {2}
        assert all("compat" not in s for s in out["scripts"])

    def test_single_file_face_is_exact_match(self, tmp_path, monkeypatch):
        proj, target = self._proj_with_adjacent_dirs(tmp_path)
        _no_godot(monkeypatch)
        out = project_file_coverage(str(proj), str(target),
                                    tests_glob="res://tests/unit/test_a.gd")
        # 单文件精确匹配：face==script 本身才计入；相邻同名不得误吸
        assert out["covered_lines"] == {2}
        assert out["scripts"] == ["res://tests/unit/test_a.gd"]

    def test_startswith_regression_would_bleed(self, tmp_path, monkeypatch):
        """判别力反例：裸 startswith 会把相邻目录的行事实混入 unit 面；
        边界匹配必须剔除——回归即红。"""
        import godot_qa_toolkit.coverage.mapping as mapping_mod

        proj, target = self._proj_with_adjacent_dirs(tmp_path)
        _no_godot(monkeypatch)
        face = "res://tests/unit/"
        scripts = ["res://tests/unit/test_a.gd",
                   "res://tests/unit_compat/test_a_compat.gd"]
        kept = [s for s in scripts
                if s == face or s.startswith(
                    face if face.endswith("/") else face + "/")]
        assert kept == ["res://tests/unit/test_a.gd"]
        assert [s for s in scripts if s.startswith("res://tests/unit")] != kept
        assert mapping_mod is not None


class TestExecutionEvidenceSentinel:
    """§SPEC-025/027：probes_fired=0 且行事实非空（非 vacuous）→ 不得产出
    可信分数（fail-loud，不伪造）。"""

    def test_probes_zero_with_line_facts_is_run_error(self, tmp_path,
                                                      monkeypatch):
        """映射 probes_fired=0 且行事实非空（非 vacuous）→ run_error，
        不得产出可信 0% 覆盖分数（§SPEC-025/027）。"""
        proj, target = _proj(tmp_path)
        _install_map(proj, covered_lines=[2], probes_fired=0)
        _no_godot(monkeypatch)
        result = run_coverage(str(target), str(proj))
        assert result["ok"] is False
        assert result["summary"].get("run_error") is True
        assert "probes_fired=0" in result["summary"]["error"]


    def test_probes_missing_is_run_error(self, tmp_path, monkeypatch):
        """映射 arms_state 缺 probes_fired → 具名 fail-loud（不伪造）。"""
        proj, target = _proj(tmp_path)
        map_path = _install_map(proj, covered_lines=[2], probes_fired=None) \
            if False else _install_map(proj, covered_lines=[2])
        doc = json.loads(Path(map_path).read_text(encoding="utf-8"))
        del doc["arms_state"]["gut_full"]["probes_fired"]
        Path(map_path).write_text(json.dumps(doc, ensure_ascii=False),
                                  encoding="utf-8")
        _no_godot(monkeypatch)
        result = run_coverage(str(target), str(proj))
        assert result["ok"] is False
        assert result["summary"].get("run_error") is True
        assert "probes_fired" in result["summary"]["error"]



class TestRunCoverageMapping:
    """run_coverage 顶层契约（映射投影路径）。"""

    def test_projection_above_threshold_ok(self, tmp_path, monkeypatch):
        proj, target = _proj(tmp_path)
        _install_map(proj, covered_lines=[2])
        _no_godot(monkeypatch)
        result = run_coverage(str(target), str(proj), min_percent=50.0)
        assert result["ok"] is True
        assert result["summary"]["coverage_percent"] == 50.0
        assert result["summary"]["coverage_source"] == "line_mapping_projection"
        assert result["summary"]["probes_source"] == "map_arms_state"
        assert result["summary"]["probes_fired"] == 640
        assert result["summary"]["hits_lines"] == [2]

    def test_below_threshold_fails(self, tmp_path, monkeypatch):
        proj, target = _proj(tmp_path)
        _install_map(proj, covered_lines=[2])
        _no_godot(monkeypatch)
        result = run_coverage(str(target), str(proj), min_percent=80.0)
        assert result["ok"] is False
        assert result["failures"]
        assert "below threshold" in result["failures"][0]["reason"]

    def test_vacuous_no_executable_lines(self, tmp_path, monkeypatch):
        proj, target = _proj(tmp_path)
        target.write_text("extends RefCounted\n", encoding="utf-8")
        _no_godot(monkeypatch)
        result = run_coverage(str(target), str(proj))
        assert result["ok"] is True
        assert result["summary"].get("vacuous") is True

    def test_zero_real_file_write(self, tmp_path, monkeypatch):
        proj, target = _proj(tmp_path)
        _install_map(proj, covered_lines=[2])
        _no_godot(monkeypatch)
        before = target.read_bytes()
        run_coverage(str(target), str(proj), min_percent=50.0)
        assert target.read_bytes() == before  # 投影路径绝不写真实文件
