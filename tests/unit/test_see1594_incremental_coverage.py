"""SEE-1594 §SPEC-025：coverage 由行级映射唯一真源驱动。

证明：
- 新鲜映射 → projection 的 covered 行集/覆盖率与行事实一致（parity）；
- 无映射 → fail-loud（绝不静默回退旧 direct GUT，且不 spawn godot）；
- 映射过期 → 走 resolve_selection 的增量刷新后重判（不是重跑 direct GUT）；
- 面过滤按路径边界匹配（目录不串相邻目录、单文件精确匹配）；
- 执行证据哨兵：probes_fired=0 且行事实非空 → run_error 拒绝可信分数；
- 畸形 arms_state / instrumentable_lines → 具名 run_error，不裸 traceback。
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


def _install_map(proj: Path, covered_lines, *, probes_fired=640,
                 arms_state=None):
    """在约定默认位置写入 /6 行级主真值映射（含完整绑定包；arms_state
    可注入畸形形态供哨兵测试）。"""
    from godot_qa_toolkit.measurement.recipe import build_recipe, recipe_hash

    fx_install = fx
    from godot_qa_toolkit.measurement import recipe as _recipe_mod

    fx_install.install_recipe(proj)
    recipe, error = build_recipe(proj)
    assert error is None, error
    raw = recipe_hash({k: v for k, v in recipe.items() if k != "recipe_hash"})
    rel = "utils/t.gd"
    ident = IDENT_A
    if arms_state is None:
        arms_state = {"gut_full": {"state": "valid", "invalid_reason": None,
                                   "bundles": 1, "pairs": 1,
                                   "probes_fired": probes_fired,
                                   "vacuous": False}}
    arm = {"test_to_files": {ident: [rel]}, "script_to_files": {},
           "tests": 1, "scripts": 1, "files": 1,
           "pairs": len(covered_lines),
           "function_pairs": 0, "test_to_functions": {},
           "script_to_functions": {},
           "file_to_tests": {rel: [ident]},
           "file_to_scripts": {rel: ["test_x.gd"]},
           "line_pairs": len(covered_lines),
           "script_line_pairs": len(covered_lines),
           "test_to_lines": {ident: {rel: list(covered_lines)}},
           "script_to_lines": {},
           "line_to_tests": {rel: {str(covered_lines[0]): [ident]}}}
    tests_face = {
        str(gd.relative_to(proj)): hashlib.sha256(
            gd.read_bytes()).hexdigest()
        for gd in sorted(proj.rglob("tests/**/*.gd"))
    }
    doc = {"schema_version": "test-attribution/7",
           "report_recipe_hash": raw, "recipe_hash_pristine": raw,
           "project_root": str(proj), "identity": ident,
           "causal": {"enabled": False},
           "file_sha256": {rel: hashlib.sha256(
               (proj / rel).read_bytes()).hexdigest()},
           "target_files": [rel],
           "tests_face": tests_face,
           "instrumentable_lines": {rel: [2, 3]},
           "arms": {"gut_full": arm},
           "arms_state": arms_state,
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
    target.write_text(SRC + "\n# changed\n", encoding="utf-8")
    calls = {"n": 0}

    def fake_increment(root, path, doc, rel, changed_extra=None, **kwargs):
        calls["n"] += 1
        _install_map(proj, covered_lines=[2])
        return True, None

    monkeypatch.setenv(truth.ENV_MODE, "auto")
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
        # 行事实：unit 面的测试覆盖行 2；相邻目录同前缀名脚本覆盖行 3
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
        assert out["covered_lines"] == {2}
        assert all("compat" not in s for s in out["scripts"])

    def test_single_file_face_is_exact_match(self, tmp_path, monkeypatch):
        proj, target = self._proj_with_adjacent_dirs(tmp_path)
        _no_godot(monkeypatch)
        import json as _json
        map_path = proj / ".qa-cache" / "attribution-map.json"
        doc = _json.loads(map_path.read_text(encoding="utf-8"))
        ident_c = IDENT_COMPAT
        doc["arms"]["gut_full"]["test_to_lines"].setdefault(
            ident_c, {}).setdefault("utils/t.gd", []).append(3)
        map_path.write_text(_json.dumps(doc, ensure_ascii=False),
                            encoding="utf-8")
        out = project_file_coverage(
            str(proj), str(target), tests_glob="res://tests/unit/test_a.gd")
        assert out["covered_lines"] == {2}
        assert out["scripts"] == ["res://tests/unit/test_a.gd"]

    def test_startswith_regression_would_bleed(self, tmp_path, monkeypatch):
        """生产回归判别：驱动 project_file_coverage——unit_compat 的行事实
        （行 3）不得混入 unit 面（covered={2}）；回归到裸 startswith 时
        covered 变 {2,3}，本用例即红。"""
        proj, target = self._proj_with_adjacent_dirs(tmp_path)
        _no_godot(monkeypatch)
        from godot_qa_toolkit.coverage.mapping import project_file_coverage
        out = project_file_coverage(str(proj), str(target),
                                    tests_glob="res://tests/unit/")
        assert out["covered_lines"] == {2}
        assert out["coverage_percent"] == 50.0


class TestExecutionEvidenceSentinel:
    """§SPEC-025/027：probes_fired=0 且行事实非空（非 vacuous）→ 不得产出
    可信分数（run_error 域 JSON + 失败 evidence，不伪造）。"""

    def test_probes_zero_with_line_facts_is_run_error(self, tmp_path,
                                                      monkeypatch):
        proj, target = _proj(tmp_path)
        _install_map(proj, covered_lines=[2], probes_fired=0)
        _no_godot(monkeypatch)
        result = run_coverage(str(target), str(proj))
        assert result["ok"] is False
        assert result["summary"].get("run_error") is True
        assert "probes_fired=0" in result["summary"]["error"]

    def test_probes_missing_is_run_error(self, tmp_path, monkeypatch):
        proj, target = _proj(tmp_path)
        map_path = _install_map(proj, covered_lines=[2])
        doc = json.loads(Path(map_path).read_text(encoding="utf-8"))
        del doc["arms_state"]["gut_full"]["probes_fired"]
        Path(map_path).write_text(json.dumps(doc, ensure_ascii=False),
                                  encoding="utf-8")
        _no_godot(monkeypatch)
        result = run_coverage(str(target), str(proj))
        assert result["ok"] is False
        assert result["summary"].get("run_error") is True
        assert "probes_fired" in result["summary"]["error"]


class TestMalformedArmsState:
    """§SPEC-025/027：畸形 arms_state（顶层非 dict、probes/pairs 非法）→
    具名 run_error + 失败 evidence，绝不裸 traceback。"""

    def _write_map_with_arms(self, proj, arms_state):
        import hashlib

        from godot_qa_toolkit.measurement.recipe import (build_recipe,
                                                          recipe_hash)
        import attribution_fixtures as fx

        fx.install_recipe(proj)
        recipe, error = build_recipe(proj)
        assert error is None, error
        raw = recipe_hash({k: v for k, v in recipe.items()
                           if k != "recipe_hash"})
        rel = "utils/t.gd"
        ident = IDENT_A
        tests_face = {
            str(gd.relative_to(proj)): hashlib.sha256(
                gd.read_bytes()).hexdigest()
            for gd in sorted(proj.rglob("tests/**/*.gd"))
        }
        doc = {"schema_version": "test-attribution/7",
               "report_recipe_hash": raw, "recipe_hash_pristine": raw,
               "project_root": str(proj), "identity": ident,
               "causal": {"enabled": False},
               "file_sha256": {rel: hashlib.sha256(
                   (proj / rel).read_bytes()).hexdigest()},
               "target_files": [rel],
               "tests_face": tests_face,
               "instrumentable_lines": {rel: [2, 3]},
               "arms": {"gut_full": {
                   "test_to_files": {ident: [rel]}, "script_to_files": {},
                   "tests": 1, "scripts": 1, "files": 1, "pairs": 1,
                   "function_pairs": 0, "test_to_functions": {},
                   "script_to_functions": {},
                   "file_to_tests": {rel: [ident]},
                   "file_to_scripts": {rel: ["test_x.gd"]},
                   "line_pairs": 1, "script_line_pairs": 1,
                   "test_to_lines": {ident: {rel: [2]}},
                   "script_to_lines": {},
                   "line_to_tests": {rel: {"2": [ident]}}}},
               "arms_state": arms_state,
               "notes": []}
        out = proj / ".qa-cache" / "attribution-map.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return str(out)

    def test_probes_fired_non_numeric_is_named_run_error(
            self, tmp_path, monkeypatch):
        proj, target = _proj(tmp_path)
        _no_godot(monkeypatch)
        self._write_map_with_arms(proj, {"gut_full": {
            "state": "valid", "invalid_reason": None, "bundles": 1,
            "pairs": 1, "probes_fired": "abc", "vacuous": False}})
        result = run_coverage(str(target), str(proj))
        assert result["ok"] is False
        assert result["summary"].get("run_error") is True
        assert ("malformed" in result["summary"]["error"]
                or "probes_fired" in result["summary"]["error"])

    def test_invalid_pairs_container_is_named_run_error(
            self, tmp_path, monkeypatch):
        proj, target = _proj(tmp_path)
        _no_godot(monkeypatch)
        self._write_map_with_arms(proj, {"gut_full": {
            "state": "valid", "invalid_reason": None,
            "bundles": {"x": 1},
            "pairs": 1, "probes_fired": 2, "vacuous": False}})
        result = run_coverage(str(target), str(proj))
        assert result["ok"] is False
        assert result["summary"].get("run_error") is True

    def test_arms_state_truthy_non_dict_is_named_run_error(
            self, tmp_path, monkeypatch):
        proj, target = _proj(tmp_path)
        _no_godot(monkeypatch)
        self._write_map_with_arms(proj, ["not", "a", "dict"])
        result = run_coverage(str(target), str(proj))
        assert result["ok"] is False
        assert result["summary"].get("run_error") is True


class TestMalformedInstrumentableLines:
    """§SPEC-025/027：map 顶层 instrumentable_lines 为 truthy 非 dict（如
    list）→ 具名 run_error/域错误，绝不裸 traceback。"""

    def test_top_level_truthy_non_dict_is_named_run_error(
            self, tmp_path, monkeypatch):
        proj, target = _proj(tmp_path)
        map_path = _install_map(proj, covered_lines=[2])
        doc = json.loads(Path(map_path).read_text(encoding="utf-8"))
        doc["instrumentable_lines"] = [2, 3]
        Path(map_path).write_text(json.dumps(doc, ensure_ascii=False),
                                  encoding="utf-8")
        _no_godot(monkeypatch)
        result = run_coverage(str(target), str(proj))
        assert result["ok"] is False
        assert result["summary"].get("run_error") is True
        # 具名域错误钉：错误必须出自顶层类型检查本体；投影兜底 AttributeError
        # 转发的 projection_failed 不满足（钉住 §SPEC-025/027 域命名）。
        assert "instrumentable_lines top-level malformed" in \
            result["summary"]["error"]


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
        assert target.read_bytes() == before
