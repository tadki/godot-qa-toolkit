"""SEE-1564 §SPEC-202 stage_timings 分段计时仪表：字段存在 + 取值合理。

stdout 契约只加不改：现有字段语义不变；阶段清单 = test_scan/prewarm/
baseline/per_mutant/worker_boot/slot_sync/prefilter/total；RSS 峰值 =
self VmHWM（mb）；磁盘增量 = .qa-cache + ext4 农场树 before/after（run 级）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import executor
from godot_qa_toolkit.mutation.executor import run_mutation_files
from godot_qa_toolkit.mutation.runner import run_mutation
from godot_qa_toolkit.mutation.stagetiming import STAGE_KEYS
import godot_qa_toolkit.mutation.runner as runner_mod

GD_TARGET = ("func add(a, b):\n"
             "\tif a > b:\n"
             "\t\treturn a - b\n"
             "\treturn a + b\n")

EXISTING_SUMMARY_KEYS = ("file", "mutants", "killed", "survived", "suspect",
                         "timeout", "run_errors", "invalid_mutants",
                         "kill_rate", "budget", "scoped",
                         "baseline_reused", "adaptive_timeout_s",
                         "inject_scheme")


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


class _SerialPool:
    def __init__(self, max_workers=None):
        self.max_workers = max_workers

    def map(self, fn, iterable):
        return [fn(x) for x in iterable]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install_fakes(monkeypatch):
    # baseline 与 mutant 腿分离假体：baseline 干净通过（零 pre-existing
    # 失败），mutant 腿恒失败 → 差集 kill 判定全部 killed。
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


def _assert_stage_shape(st: dict):
    assert set(st.keys()) == set(STAGE_KEYS)
    for key in STAGE_KEYS:
        assert isinstance(st[key], (int, float)) and st[key] >= 0.0, key
    assert st["total"] > 0.0


def test_unit_summary_stage_timings_and_rss(tmp_path, monkeypatch):
    _install_fakes(monkeypatch)
    proj, target = _write_project(tmp_path)
    result = run_mutation(target, proj, budget=10, timeout_s=60,
                          tests_glob="res://tests/unit/save/")
    s = result["summary"]
    for key in EXISTING_SUMMARY_KEYS:  # 只加不改：既有键全部在场
        assert key in s, key
    _assert_stage_shape(s["stage_timings"])
    assert isinstance(s["rss_peak_mb"], (int, float)) and s["rss_peak_mb"] > 0
    assert result["ok"] is True


def test_envelope_stage_timings_rss_disk_delta(tmp_path, monkeypatch):
    _install_fakes(monkeypatch)
    proj, target = _write_project(tmp_path)
    result = run_mutation_files([target], proj, budget=10, timeout_s=60,
                                tests_glob="res://tests/unit/save/", jobs=4)
    env_st = result["stage_timings"]
    _assert_stage_shape(env_st)
    assert isinstance(result["rss_peak_mb"], (int, float))
    assert result["rss_peak_mb"] > 0
    assert isinstance(result["disk_delta_bytes"], int)
    assert result["disk_delta_bytes"] >= 0
    # 每文件聚合 summary 同样携带（并行合并路径）
    _assert_stage_shape(result["results"][0]["summary"]["stage_timings"])


def test_stage_timings_present_on_warm_full_reuse(tmp_path, monkeypatch):
    """warm 全复用（零执行）：阶段面仍在场——per_mutant=0、baseline=锚耗时。"""
    _install_fakes(monkeypatch)
    proj, target = _write_project(tmp_path)
    run_mutation_files([target], proj, budget=10, timeout_s=60,
                       tests_glob="res://tests/unit/save/", jobs=4)
    # 第二轮：差分全复用（killed 条目带真实证书才可复用——fake GUT 杀手证书
    # 由 classify 路径签发，首轮已落盘）
    result = run_mutation_files([target], proj, budget=10, timeout_s=60,
                                tests_glob="res://tests/unit/save/", jobs=4)
    st = result["results"][0]["summary"]["stage_timings"]
    _assert_stage_shape(st)
