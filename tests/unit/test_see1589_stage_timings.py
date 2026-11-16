"""SEE-1589 B §SPEC-008：stage_timings 可对账到墙钟。

判据：
- farm 制备（farm_provision）、路由静态扫描（route_scan）、编排规划
  （planning）进入阶段清单（KOL 旧缺口 = 20s 引用图扫描未归因）；
- 每轮 summary / envelope 带 `stage_reconciliation`：
  wall_s / attributed_s / unexplained_s 三项齐备且自洽；
- 读阶段分解不再低估真实耗时（attributed + unexplained == wall）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import executor
from godot_qa_toolkit.mutation.executor import run_mutation_files
from godot_qa_toolkit.mutation.stagetiming import (
    RECONCILE_STAGE_KEYS,
    STAGE_KEYS,
    reconcile,
)
import godot_qa_toolkit.mutation.runner as runner_mod

GD_TARGET = ("func add(a, b):\n"
             "\tif a > b:\n"
             "\t\treturn a - b\n"
             "\treturn a + b\n")


class _SerialPool:
    def __init__(self, max_workers=None):
        self.max_workers = max_workers

    def map(self, fn, iterable):
        return [fn(x) for x in iterable]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _write_project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "addons" / "gut").mkdir(parents=True, exist_ok=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    save = proj / "tests" / "unit" / "save"
    save.mkdir(parents=True, exist_ok=True)
    (save / "test_save.gd").write_text('preload("res://calc.gd")\n')
    target = proj / "calc.gd"
    target.write_text(GD_TARGET)
    return str(proj), str(target)


def _install_fakes(monkeypatch):
    monkeypatch.setattr(runner_mod, "_run_gut_on_project",
                        lambda root, timeout_s=60, tests_glob=None:
                            (1, "* test_killed_by_probe\n\t[Failed]\n"))
    monkeypatch.setattr(runner_mod, "_run_baseline",
                        lambda root, glob, timeout_s:
                            (0, "OK. All tests passed", 1.5))
    monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
    monkeypatch.setattr(executor, "_run_baseline_snapshot",
                        lambda *a, **k: {"rc": 0, "tail": "pass",
                                         "elapsed": 5.0})


def test_reconcile_arithmetic():
    disclosure = {"per_mutant": 10.0, "farm_provision": 2.0,
                  "route_scan": 1.0, "slot_sync": 15.0, "total": 20.0}
    out = reconcile(disclosure, 20.0)
    assert out["wall_s"] == 20.0
    assert out["attributed_s"] == 13.0  # slot_sync 不参与求和（超集窗口）
    assert out["unexplained_s"] == 7.0


def test_reconcile_never_negative_on_over_attribution():
    out = reconcile({"per_mutant": 25.0}, 20.0)
    assert out["unexplained_s"] == 0.0


def test_new_stage_keys_present():
    for key in ("farm_provision", "route_scan", "planning"):
        assert key in STAGE_KEYS
        if key != "planning":  # planning 只在父层，不并入分片
            assert key in RECONCILE_STAGE_KEYS


def test_run_summary_and_envelope_reconcile(tmp_path, monkeypatch):
    proj, _target = _write_project(tmp_path)
    _install_fakes(monkeypatch)
    env = run_mutation_files(["calc.gd"], proj, budget=4, jobs=2)
    summary = env["results"][0]["summary"]

    st = summary["stage_timings"]
    assert "farm_provision" in st and "route_scan" in st
    rec = summary["stage_reconciliation"]
    assert set(rec) >= {"wall_s", "attributed_s", "unexplained_s",
                        "over_attributed_s"}
    assert rec["wall_s"] > 0.0
    assert st["total"] > 0.0
    assert rec["unexplained_s"] >= 0.0
    assert rec["attributed_s"] >= 0.0

    er = env["stage_reconciliation"]
    assert set(er) >= {"wall_s", "attributed_s", "unexplained_s",
                       "prewarm_import_s", "prewarm_decision"}
    assert er["unexplained_s"] >= 0.0
