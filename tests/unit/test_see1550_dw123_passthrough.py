"""SEE-1550 D-W1/D-W3 修复轮——--worker-channel 全路径透传 + 披露面契约。

D-W1（Revy §SPEC-020 FAIL 实证）：executor 单文件自动退串行分支（SEE-1413，
位点 ≤ 默认 jobs 的最常用小目标路径）与 --jobs 1 串行快路径调用 run_mutation
漏传 worker_channel——旗标在最常用路径静默失效（冷签名零披露）。契约：
三路径（CLI 串行 / executor 自动退串行 / executor 并行分片）+ 零执行收尾
一律透传；CLI 旗标缺席时传 None（env GQT_MUTATION_WORKER 保持 A9 双通道
opt-in 语义），不得传 False 封死 env 通道。

D-W3：并行/合并面 summary passthrough 缺 exec_scheme（单文件披露在场、
分片合并后丢失）；worker 腿 reporter.record 硬编码 0.0s 计时失真。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import executor
from godot_qa_toolkit.mutation import worker_channel as wc
from godot_qa_toolkit.mutation.executor import (
    _merge_results,
    run_mutation_files,
)
from godot_qa_toolkit.mutation.worker_channel import run_worker_legs

TOY_GD = ("func add(a, b):\n\treturn a + b\n\n"
          "func mul(a, b):\n\treturn a * b\n\n"
          "func sub(a, b):\n\treturn a - b\n\n"
          "func neg(a):\n\treturn -a\n\n"
          "func div(a, b):\n\treturn a / b\n")


@pytest.fixture()
def toy_target(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    target = proj / "toy.gd"
    target.write_text(TOY_GD, encoding="utf-8")
    return str(proj), str(target)


def _fake_report(**summary):
    return {"tool": "mutation", "ok": True,
            "summary": {"file": "toy.gd", **summary}, "failures": []}


@pytest.fixture()
def run_mutation_spy(monkeypatch):
    calls: list[dict] = []

    def _spy(file_path, project_root, **kwargs):
        calls.append({"file": file_path, **kwargs})
        return _fake_report()

    monkeypatch.setattr(executor, "run_mutation", _spy)
    return calls


def _stub_env(monkeypatch):
    monkeypatch.setattr(executor, "_detect_binary_kind", lambda: "linux")
    monkeypatch.setattr(executor, "identity_pack",
                        lambda *a, **k: {"stub": True})
    monkeypatch.delenv(wc.ENV_WORKER, raising=False)


# -- D-W1：executor 自动退串行分支（位点 ≤ 默认 jobs 的最常用路径）-----------

def test_auto_fallback_passes_worker_channel(toy_target, run_mutation_spy,
                                             monkeypatch):
    _stub_env(monkeypatch)
    monkeypatch.setattr(executor, "default_jobs", lambda j: 999)
    proj, target = toy_target
    r = run_mutation_files([target], proj, budget=50, jobs=None,
                           tests_glob="res://tests/", worker_channel=True)
    assert len(run_mutation_spy) == 1
    assert run_mutation_spy[0]["worker_channel"] is True
    assert r["jobs"] == 1  # 退串行形态保持


def test_auto_fallback_flag_absent_maps_none_env_still_decides(
        toy_target, run_mutation_spy, monkeypatch):
    _stub_env(monkeypatch)
    monkeypatch.setattr(executor, "default_jobs", lambda j: 999)
    monkeypatch.setenv(wc.ENV_WORKER, "1")
    proj, target = toy_target
    run_mutation_files([target], proj, budget=50, jobs=None,
                       tests_glob="res://tests/", worker_channel=None)
    assert run_mutation_spy[0]["worker_channel"] is None
    assert run_mutation_spy[0]["dry_run"] is False


# -- D-W1：--jobs 1 串行快路径 ------------------------------------------------

def test_serial_fast_path_passes_worker_channel(toy_target, run_mutation_spy,
                                                monkeypatch):
    _stub_env(monkeypatch)
    proj, target = toy_target
    run_mutation_files([target], proj, budget=50, jobs=1,
                       tests_glob="res://tests/", worker_channel=True)
    assert run_mutation_spy[0]["worker_channel"] is True


# -- D-W1：零执行收尾（全 reused / budget=0 的串行收尾 run_mutation）---------

def test_zero_execution_wrapup_passes_worker_channel(
        toy_target, run_mutation_spy, monkeypatch):
    _stub_env(monkeypatch)
    proj, target = toy_target
    src = Path(target).read_text(encoding="utf-8")
    mutants = executor._collect_mutations(src, target)
    assert mutants, "toy fixture must yield mutation sites"
    monkeypatch.setattr(
        executor, "_precompute_baselines",
        lambda *a, **k: ({"res://tests/": {"rc": 0, "tail": "",
                                           "elapsed": 1.0}}, {}))
    monkeypatch.setattr(executor, "_apply_coverage_prefilter",
                        lambda plan, *a, **k: ([], list(plan.executed)))
    monkeypatch.setattr(executor, "budget_on_retry",
                        lambda pool, budget: ([], list(pool)))
    run_mutation_files([target], proj, budget=10, jobs=4,
                       tests_glob="res://tests/", worker_channel=True)
    assert len(run_mutation_spy) == 1
    call = run_mutation_spy[0]
    assert call["worker_channel"] is True
    assert call["precomputed_baseline"] is not None


# -- D-W1：CLI 旗标映射（缺席 → None 保 env 通道；在场 → True）---------------

def _dispatch_args(tmp_path, target, extra):
    from godot_qa_toolkit.cli import build_parser
    argv = ["mutation", "--project-root", str(tmp_path / "proj"),
            *extra, target]
    return build_parser().parse_args(argv)


def test_cli_flag_absent_maps_none(toy_target, monkeypatch):
    from godot_qa_toolkit.mutation import runner
    from godot_qa_toolkit.cli import _dispatch_mutation
    proj, target = toy_target
    captured: list[dict] = []

    def _spy(**kwargs):
        captured.append(kwargs)
        return _fake_report()

    monkeypatch.setattr(runner, "run_mutation", lambda *a, **k: _spy(**k))
    args = _dispatch_args(Path(proj), target, ["--jobs", "1"])
    _dispatch_mutation(args, "linux")
    assert captured and captured[0].get("worker_channel") is None


def test_cli_flag_present_maps_true(toy_target, monkeypatch):
    from godot_qa_toolkit.mutation import runner
    from godot_qa_toolkit.cli import _dispatch_mutation
    proj, target = toy_target
    captured: list[dict] = []

    def _spy(**kwargs):
        captured.append(kwargs)
        return _fake_report()

    monkeypatch.setattr(runner, "run_mutation", lambda *a, **k: _spy(**k))
    args = _dispatch_args(Path(proj), target,
                          ["--jobs", "1", "--worker-channel"])
    _dispatch_mutation(args, "linux")
    assert captured and captured[0].get("worker_channel") is True


def test_cli_multi_path_passes_worker_channel(toy_target, monkeypatch):
    import godot_qa_toolkit.cli as cli_mod
    from godot_qa_toolkit.mutation import executor
    proj, target = toy_target
    captured: list[dict] = []

    def _spy(*a, **kwargs):
        captured.append(kwargs)
        return {"tool": "mutation", "ok": True, "jobs": 1,
                "results": [_fake_report()]}

    monkeypatch.setattr(executor, "run_mutation_files", _spy)
    monkeypatch.setattr(cli_mod, "TimingStore", object, raising=False)
    args = _dispatch_args(Path(proj), target,
                          ["--worker-channel", "--budget", "1"])
    cli_mod._dispatch_mutation(args, "linux")
    assert captured and captured[0].get("worker_channel") is True


def test_cli_help_discloses_env_equivalent(capsys):
    from godot_qa_toolkit.cli import build_parser
    with pytest.raises(SystemExit) as ei:
        build_parser().parse_args(["mutation", "--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "GQT_MUTATION_WORKER" in out


# -- D-W3：并行合并面 exec_scheme 披露 ----------------------------------------

def _chunk_result(file, **extra):
    summary = {"file": file, "mutants": 2, "killed": 1, "survived": 1,
               "suspect": 0, "timeout": 0, "run_errors": 0,
               "invalid_mutants": 0, "jobs": 1, "ok": True,
               "kill_rate": 0.5, "budget": 50, "scoped": True,
               "baseline_reused": False, "failures": [], "resumed": 0}
    summary.update(extra)
    return {"tool": "mutation", "ok": True, "summary": summary,
            "failures": []}


def test_merge_surface_discloses_exec_scheme():
    chunks = [{"file": "a.gd", "total_sites": 2, "attention": None}]
    results = [_chunk_result("a.gd", exec_scheme="worker-reset/1",
                             inject_scheme="disk-shadow/1",
                             slot_mode="fresh")]
    merged = _merge_results(chunks, results, 2)
    file_summary = merged["results"][0]["summary"]
    assert file_summary.get("exec_scheme") == "worker-reset/1"
    assert file_summary.get("inject_scheme") == "disk-shadow/1"


# -- D-W3：worker 腿进度计时（0.0s 失真修正）----------------------------------

class _FakeSession:
    def __init__(self, *a, **k):
        self.dead = False
        self.dead_reason = ""
        self.legs = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        pass

    def run_leg(self, leg, *a, **k):
        import time
        time.sleep(0.01)
        self.legs += 1
        return wc.WorkerLegResult(leg=leg, ok=True, hash_ok=True,
                                  gut_ran=True, reload={"res://t.gd": 0},
                                  pass_count=1, fail_count=0,
                                  timings={"gut_ms": 10, "reload_ms": 1})

    def note_canary(self):
        pass

    def invalidate_window(self):
        return 0


def test_worker_legs_report_real_elapsed(monkeypatch):
    monkeypatch.setenv(wc.ENV_CANARY_EVERY, "99")
    monkeypatch.setattr(wc, "WorkerSession", _FakeSession)
    records: list[tuple] = []

    def _mutant(i):
        from types import SimpleNamespace
        return SimpleNamespace(stable_id=f"m{i}", kind="op",
                               original="a + b", mutated="a - b")

    mutants = [_mutant(0), _mutant(1)]
    slot = types_ns_slot()
    pairs, fatals = run_worker_legs(
        mutants, slot, "toy.gd", "res://toy.gd", ["res://toy.gd"],
        "linux", "res://tests/",
        inject_fn=lambda m: ("sha", None),
        cold_runner=lambda m, seq: {"verdict": "survived"},
        record_fn=lambda m, v, *rest: records.append((m, v, rest)),
        classify_fn=lambda m, leg: {"verdict": "survived"},
        leg_timeout_s=30)
    assert fatals == []
    assert len(records) == 2
    for _m, _v, rest in records:
        assert len(rest) == 1, "record_fn contract: (mutant, verdict, elapsed)"
        assert rest[0] > 0.0, f"elapsed must be real, got {rest[0]}"


def types_ns_slot():
    from types import SimpleNamespace
    return SimpleNamespace(root="/tmp/nonexistent-slot",
                           project_root="/tmp/nonexistent-proj")
