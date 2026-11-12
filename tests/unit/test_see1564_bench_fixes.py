"""SEE-1564 §SPEC-201 基准脚本修复：warm 真零执行复用 + kol 模式变量修复。

- bench_ab_see1550.py：warm 场景不再传 --mutate-all（真零执行复用，
  差分默认开启）；cold 场景语义不变。
- bench_worker_channel.py：kol 模式直接使用 --kol-copy 项目（不再引用
  未定义的 fixture）；fixture 模式行为不变；输出 JSON 形态不变。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent.parent
BENCH_AB = REPO / "benchmarks" / "bench_ab_see1550.py"
BENCH_WC = REPO / "benchmarks" / "bench_worker_channel.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- bench_ab_see1550 warm 场景 --------------------------------------------

def test_bench_ab_warm_scenario_is_true_reuse(monkeypatch, tmp_path):
    mod = _load("bench_ab_see1550", BENCH_AB)
    captured = {}

    def fake_run(src_root, proj, target_rel, args):
        captured["args"] = list(args)
        return {"ok": True}, 1.0

    monkeypatch.setattr(mod, "_run_mutation", fake_run)
    mod._run_scenario(tmp_path, tmp_path, "res://x.gd", "warm",
                      keep_cache=True, point_line=6)
    assert "--mutate-all" not in captured["args"], (
        "warm scenario must rely on default diff reuse (true zero-execution "
        "reuse), not force a full rerun")


def test_bench_ab_cold_scenario_keeps_full_run(monkeypatch, tmp_path):
    mod = _load("bench_ab_see1550", BENCH_AB)
    captured = {}

    def fake_run(src_root, proj, target_rel, args):
        captured["args"] = list(args)
        return {"ok": True}, 1.0

    monkeypatch.setattr(mod, "_run_mutation", fake_run)
    monkeypatch.setattr(mod, "_wipe_cache", lambda proj: None)
    mod._run_scenario(tmp_path, tmp_path, "res://x.gd", "cold",
                      keep_cache=False, point_line=6)
    assert "--mutate-all" in captured["args"]


# --- bench_worker_channel kol 模式 ------------------------------------------

def test_bench_track_prepared_proj_skips_fixture_prep(tmp_path, monkeypatch):
    mod = _load("bench_worker_channel", BENCH_WC)

    def no_prep(base, gut, workdir, tag):
        raise AssertionError("prepared_proj must bypass fixture preparation")

    monkeypatch.setattr(mod, "_prep_project", no_prep)
    monkeypatch.setattr(mod, "_run_round",
                        lambda root, target, ch, tg, t: (
                            None, 1.0, {"channel": ch, "wall_s": 1.0,
                                        "killed": 9, "mutants": 9}))
    monkeypatch.setattr(mod, "_instrumented_leg",
                        lambda root, target, tg: {"reload_ms": 1,
                                                  "gut_ms": 1})
    report, proj = mod.bench_track(None, None, str(tmp_path), rounds=1,
                                   target_res="res://x.gd", tests_glob=None,
                                   timeout_s=1, prepared_proj=str(tmp_path))
    assert set(report.keys()) == {
        "aa_cold_rounds_s", "aa_noise_band_s", "ab_interleave", "a_median_s",
        "b_median_s", "a_worst_s", "b_best_s", "multiplier_median",
        "per_mutant_breakdown", "speed_gate"}


def test_bench_wc_kol_mode_targets_kol_copy(monkeypatch, tmp_path):
    """kol 模式：prepared_proj=--kol-copy、target=--kol-target 的 res 路径，
    不再触碰 fixture 分支（未定义变量 NameError 的根因）。"""
    mod = _load("bench_worker_channel", BENCH_WC)
    captured = {}
    fake_report = {"aa_cold_rounds_s": [], "speed_gate": True}

    def fake_track(fixture, gut_dir, workdir, rounds, target_res,
                   tests_glob, timeout_s, prepared_proj=None):
        captured.update(fixture=fixture, target_res=target_res,
                        prepared_proj=prepared_proj, tests_glob=tests_glob)
        return fake_report, tmp_path

    monkeypatch.setattr(mod, "bench_track", fake_track)
    monkeypatch.setattr(sys, "argv",
                        ["bench", "--mode", "kol",
                         "--kol-copy", str(tmp_path / "kol"),
                         "--kol-target", "autoload/save_manager.gd",
                         "--outdir", str(tmp_path / "out")])
    rc = mod.main()
    assert rc == 0
    assert captured["prepared_proj"] == str(tmp_path / "kol")
    assert captured["target_res"] == "res://autoload/save_manager.gd"
    assert captured["fixture"] is None


def test_bench_wc_fixture_mode_unchanged(monkeypatch, tmp_path):
    mod = _load("bench_worker_channel", BENCH_WC)
    captured = {}

    def fake_track(fixture, gut_dir, workdir, rounds, target_res,
                   tests_glob, timeout_s, prepared_proj=None):
        captured.update(fixture=fixture, prepared_proj=prepared_proj,
                        target_res=target_res)
        return {"speed_gate": True}, tmp_path

    monkeypatch.setattr(mod, "bench_track", fake_track)
    monkeypatch.setattr(mod, "_prep_project",
                        lambda base, gut, workdir, tag: tmp_path / "proj")
    monkeypatch.setattr(sys, "argv",
                        ["bench", "--mode", "fixture", "--outdir",
                         str(tmp_path / "out")])
    rc = mod.main()
    assert rc == 0
    assert captured["prepared_proj"] is None
    assert captured["fixture"] is not None
    assert captured["target_res"] == "res://forms/runtime_load.gd"
