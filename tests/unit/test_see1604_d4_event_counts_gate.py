"""SEE-1604 D4：`event_counts_stale` 拒绝门在 mutation / coverage 消费侧生效。

缺陷（QA D4）：门唯一读取点（cli `_attribution_usable`）只检查本轮自产物，增量
产物永不带 increment 块 → 死分支。显式增量产物（increment.event_counts_stale
=true）被 mutation / coverage 以 fresh_runtime_mapping 零拒绝消费。
判据：显式消费侧具名拒绝（event_counts_stale），零续新。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from godot_qa_toolkit.mutation import truth_select
from godot_qa_toolkit.coverage import mapping as covmap
from attribution_fixtures import write_map

TARGET = "func add(a, b):\n\treturn a + b\n"


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "tests" / "unit").mkdir(parents=True)
    (proj / "tests" / "unit" / "test_hit.gd").write_text(
        "extends GutTest\nfunc test_ok() -> void:\n\tpass\n")
    (proj / "calc.gd").write_text(TARGET)
    return proj


def _stale_event_map(proj: Path) -> str:
    """新鲜映射但 increment.event_counts_stale=true（真实增量产物形态）。"""
    map_path = write_map(proj, {"calc.gd": ["test_hit.gd::test_ok"]})
    doc = json.loads(Path(map_path).read_text(encoding="utf-8"))
    doc["increment"] = {"mode": "incremental", "event_counts_stale": True}
    Path(map_path).write_text(json.dumps(doc), encoding="utf-8")
    return map_path


def test_explicit_event_stale_map_rejected_at_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv(truth_select.ENV_MODE, "auto")
    proj = _project(tmp_path)
    map_path = _stale_event_map(proj)
    monkeypatch.setenv(truth_select.ENV_MAP, str(map_path))
    calls: list[str] = []
    monkeypatch.setattr(
        truth_select, "_resolve_renew",
        lambda *a, **k: calls.append("renew") or False)
    with pytest.raises(truth_select.MutationSelectionError) as ei:
        truth_select.resolve_selection(proj, str(proj / "calc.gd"))
    assert "event_counts_stale" in ei.value.reason
    assert calls == []          # 拒绝先于任何续新（零 GUT）


def test_explicit_event_stale_map_rejected_at_coverage(tmp_path, monkeypatch):
    monkeypatch.setenv(truth_select.ENV_MODE, "auto")
    proj = _project(tmp_path)
    map_path = _stale_event_map(proj)
    monkeypatch.setenv(truth_select.ENV_MAP, str(map_path))
    with pytest.raises(covmap.CoverageMappingError) as ei:
        covmap._mapping_select(Path(proj), Path(proj / "calc.gd"), None)
    assert ei.value.reason == "event_counts_stale"
