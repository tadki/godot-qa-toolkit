"""SEE-1589 B §SPEC-002：worker / warm farm / prewarm 逐轮机器可读披露。

判据（无解释静默冷通道 / 静默 farm 重建 = 红）：
- 每轮 summary 带 `worker_channel`：route、route_reasons、reason_counts、
  closure_size、decision、exec_scheme、override_refused；
- 每轮 summary 带 `farm`：mode/decision/reason/proof_binding/validation；
- 每轮 envelope 带 `prewarm`：decision/import_calls/reason；
- 显式 `--worker-channel` 被静态路由拒绝时必须 `override_refused=true`。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import executor
from godot_qa_toolkit.mutation.channel_router import (
    CHANNEL_COLD,
    CHANNEL_WORKER,
    REASON_CLOSURE_MEMBER_IN_AUTOLOAD,
    REASON_ELIGIBLE_WORKER,
    RouteDecision,
)
from godot_qa_toolkit.mutation.executor import run_mutation_files
from godot_qa_toolkit.mutation.identity import COLD_SCHEME, WORKER_SCHEME
from godot_qa_toolkit.mutation.worker_channel import build_worker_disclosure
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


class TestWorkerDisclosure:
    def test_cold_route_discloses_named_reasons(self, tmp_path):
        route = RouteDecision(
            target_res="res://utils/leaf.gd", channel=CHANNEL_COLD,
            reasons=(f"{REASON_CLOSURE_MEMBER_IN_AUTOLOAD}:"
                     "res://autoload/bus.gd",),
            closure=("res://autoload/bus.gd", "res://utils/leaf.gd"))
        d = build_worker_disclosure(str(tmp_path), "res://utils/leaf.gd",
                                    True, COLD_SCHEME, route)
        assert d["route"] == "cold"
        assert d["decision"] == "cold_route_excluded"
        assert f"{REASON_CLOSURE_MEMBER_IN_AUTOLOAD}:" \
               "res://autoload/bus.gd" in d["route_reasons"]
        assert d["reason_counts"] == {REASON_CLOSURE_MEMBER_IN_AUTOLOAD: 1}
        assert d["closure_size"] == 2
        assert d["exec_scheme"] == COLD_SCHEME
        assert d["override_refused"] is True, \
            "显式 --worker-channel 被路由拒绝必须披露"

    def test_worker_route_discloses_worker_decision(self, tmp_path):
        route = RouteDecision(target_res="res://lib/a.gd",
                              channel=CHANNEL_WORKER,
                              reasons=(REASON_ELIGIBLE_WORKER,),
                              closure=("res://lib/a.gd",))
        d = build_worker_disclosure(str(tmp_path), "res://lib/a.gd",
                                    None, WORKER_SCHEME, route)
        assert d["decision"] == "worker"
        assert d["reason"] == "worker_reset_session"
        assert d["override_refused"] is False

    def test_disabled_worker_still_named(self, tmp_path):
        d = build_worker_disclosure(str(tmp_path), "res://lib/a.gd",
                                    False, COLD_SCHEME, None)
        assert d["decision"] == "cold_worker_disabled"
        assert d["route"] == "not_evaluated"
        assert d["reason"]

    def test_missing_gut_direct_drive_is_named(self, tmp_path):
        proj = tmp_path / "nogut"
        proj.mkdir()
        route = RouteDecision(target_res="res://lib/a.gd",
                              channel=CHANNEL_WORKER,
                              reasons=(REASON_ELIGIBLE_WORKER,),
                              closure=("res://lib/a.gd",))
        d = build_worker_disclosure(str(proj), "res://lib/a.gd", None,
                                    COLD_SCHEME, route)
        assert d["decision"] == "cold_gut_direct_unavailable"
        assert d["reason"] == "gut_addon_missing"
        assert d["override_refused"] is False


class TestMutationRunDisclosures:
    def test_summary_carries_all_three_mechanism_blocks(
            self, tmp_path, monkeypatch):
        proj, _target = _write_project(tmp_path)
        _install_fakes(monkeypatch)
        env = run_mutation_files(["calc.gd"], proj, budget=4, jobs=2)
        summary = env["results"][0]["summary"]

        wc = summary["worker_channel"]
        assert wc["exec_scheme"] == summary["exec_scheme"]
        assert wc["decision"] in {
            "worker", "cold_route_excluded", "cold_worker_disabled",
            "cold_gut_direct_unavailable"}
        assert wc["reason"], "冷/热都必须有具名 reason"
        assert isinstance(wc["route_reasons"], list)

        farm = summary["farm"]
        assert farm["mode"] == summary["slot_mode"]
        assert farm["decision"] in {"created", "reused", "rebuilt"}
        assert farm["reason"]
        assert isinstance(farm["validation"], list)
        # 每次 farm 决定都必须能读回新鲜度链（禁止静默重建）
        assert summary["freshness"]

        pre = env["prewarm"]
        assert pre["decision"]
        assert pre["reason"]
        assert isinstance(pre["import_calls"], int)


class TestDegradedWorkerDisclosure:
    def test_session_fatal_is_machine_readable(self):
        from godot_qa_toolkit.mutation.worker_channel import (
            degraded_worker_disclosure,
        )
        base = {"decision": "worker", "reason": "worker_reset_session",
                "exec_scheme": WORKER_SCHEME}
        out = degraded_worker_disclosure(
            base, ["user:// tree drift at leg 1 (ref=16 files, now=16) — "
                   "inter-leg state pollution, session fatal"])
        assert out["decision"] == "worker_degraded_cold"
        assert out["reason"] == "user_state_drift"
        assert out["equivalence"] == "cold_rerun_all_remaining_legs"
        assert out["fatal_events"]

    def test_no_fatal_keeps_worker_decision(self):
        from godot_qa_toolkit.mutation.worker_channel import (
            degraded_worker_disclosure,
        )
        out = degraded_worker_disclosure({"decision": "worker"}, [])
        assert out["decision"] == "worker"
