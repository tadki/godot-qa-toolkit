"""SEE-1594 §SPEC-025：coverage 由行级映射唯一真源驱动。

证明：
- 新鲜映射 → projection 的 covered 行集/覆盖率与行事实一致（parity）；
- 无映射 → fail-loud（绝不静默回退旧 direct GUT，且不 spawn godot）；
- 映射过期 → 走 resolve_selection 的增量刷新后重判（不是重跑 direct GUT）。
"""

from __future__ import annotations

import pytest

import attribution_fixtures as fx
import godot_qa_toolkit.mutation.inject as inject
from godot_qa_toolkit.coverage.mapping import (CoverageMappingError,
                                                project_file_coverage)

TARGET = "utils/t.gd"
IDENT = "test_x.gd::test_a"
SRC = ("extends RefCounted\n\n"
       "func f(a):\n\tif a > 0:\n\t\treturn 1\n\treturn 0\n")


def _proj(tmp_path):
    proj = tmp_path / "proj"
    (proj / "utils").mkdir(parents=True)
    (proj / "utils" / "t.gd").write_text(SRC, encoding="utf-8")
    (proj / "tests").mkdir()
    (proj / "tests" / "test_x.gd").write_text("extends Node\n", encoding="utf-8")
    return proj


def test_projection_from_fresh_map_parity(tmp_path):
    proj = _proj(tmp_path)
    fx.write_map(proj, {TARGET: [IDENT]})
    out = project_file_coverage(str(proj), TARGET, binary_kind="linux")
    assert out["source"] == "line_mapping_projection"
    # 夹具：instrumentable=[2,3]，行事实覆盖 [2] → covered={2}
    assert out["covered_lines"] == {2}
    assert out["instrumentable_lines"] == {2, 3}
    assert out["total_lines"] == 2 and out["covered_count"] == 1
    assert out["coverage_percent"] == 50.0
    # parity：percent 与 covered/total 一致
    assert out["coverage_percent"] == round(
        100.0 * out["covered_count"] / out["total_lines"], 2)


def test_fail_loud_without_map_no_gut(tmp_path, monkeypatch):
    proj = _proj(tmp_path)  # 不写映射
    spawned = []

    def _boom(*a, **k):
        spawned.append(a)
        raise AssertionError("direct GUT path must not run under mapping source")

    monkeypatch.setattr(inject, "spawn_godot", _boom)
    monkeypatch.setattr(inject, "godot_command", lambda: "godot")
    with pytest.raises(CoverageMappingError):
        project_file_coverage(str(proj), TARGET, binary_kind="linux")
    assert spawned == []


def test_incremental_refresh_used_on_stale(tmp_path, monkeypatch):
    proj = _proj(tmp_path)
    fx.write_map(proj, {TARGET: [IDENT]})
    # 使映射过期：目标文件内容变化 → file_sha256 绑定失效
    (proj / "utils" / "t.gd").write_text(SRC + "\n# changed\n", encoding="utf-8")

    from godot_qa_toolkit.mutation import truth_select as truth
    calls = {"n": 0}

    def fake_increment(root, path, doc, rel, changed_extra=None, **kwargs):
        calls["n"] += 1
        fx.write_map(proj, {TARGET: [IDENT]})  # 依新内容重建新鲜映射
        return True, None

    monkeypatch.setenv(truth.ENV_MODE, "auto")  # 允许自动增量
    monkeypatch.setattr(truth, "_auto_increment", fake_increment)

    out = project_file_coverage(str(proj), TARGET, binary_kind="linux")
    assert calls["n"] == 1, "stale map must go through incremental refresh, not re-GUT"
    assert out["coverage_percent"] == 50.0
    assert out["source"] == "line_mapping_projection"


def test_explicit_map_unusable_fail_loud(tmp_path, monkeypatch):
    proj = _proj(tmp_path)
    bad = proj / ".qa-cache" / "attribution-map.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{ not a map", encoding="utf-8")
    with pytest.raises(CoverageMappingError):
        project_file_coverage(str(proj), TARGET, binary_kind="linux")
