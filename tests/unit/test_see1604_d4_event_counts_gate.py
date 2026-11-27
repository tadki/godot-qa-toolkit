"""SEE-1604 D4：`event_counts_stale` 在 mutation / coverage 消费侧只作
如实披露（Owner 决策 2：增量产物不可用的拒绝门作废，"事件计数未重测"
不得阻断消费）。

原拒绝门（显式消费侧具名拒绝 event_counts_stale）随定案 21 退役；
本文件改为钉住新契约：带 event_counts_stale=true 的增量产物照常被
mutation 选面与 coverage 选面消费（新鲜图零续新），不因披露位被拒。
"""

from __future__ import annotations

import json
from pathlib import Path

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


def test_event_stale_disclosure_map_consumed_at_mutation(tmp_path,
                                                         monkeypatch):
    """决策 2：event_counts_stale 只作披露——mutation 选面照常消费
    （runtime_mapping），不因披露位具名拒绝、零续新。"""
    monkeypatch.setenv(truth_select.ENV_MODE, "auto")
    proj = _project(tmp_path)
    _stale_event_map(proj)
    calls: list[str] = []
    monkeypatch.setattr(
        truth_select, "_resolve_renew",
        lambda *a, **k: calls.append("renew") or False)
    sel = truth_select.resolve_selection(proj, str(proj / "calc.gd"))
    assert sel["source"] == "runtime_mapping"
    assert calls == []          # 新鲜图零续新


def test_event_stale_disclosure_map_consumed_at_coverage(tmp_path,
                                                         monkeypatch):
    """决策 2：coverage 选面同样照常消费带披露位的增量产物。"""
    monkeypatch.setenv(truth_select.ENV_MODE, "auto")
    proj = _project(tmp_path)
    _stale_event_map(proj)
    sel = covmap._mapping_select(Path(proj), Path(proj / "calc.gd"), None)
    assert sel.get("source") == "runtime_mapping"
