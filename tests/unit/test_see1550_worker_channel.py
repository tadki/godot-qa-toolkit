"""SEE-1550 §SPEC-020 ③——W 通道会话语义单测（引擎无关，假传输）。

覆盖：exec_scheme 路由隔离、worker 腿分类矩阵、会话致命 → respawn + 自
上次 canary 窗口整批作废 + 冷重跑（含 IGNORE 刷码残留形态）、canary 失配
处置、死会话后续腿走冷。真实引擎等价电池见 tests/unit/test_see1550_worker_battery.py。
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import worker_channel
from godot_qa_toolkit.mutation.identity import (
    COLD_SCHEME,
    WORKER_SCHEME,
    identity_pack,
    identity_sha1,
)
from godot_qa_toolkit.mutation.ops import Mutant
from godot_qa_toolkit.mutation.reporting import classify_worker_leg
from godot_qa_toolkit.mutation.worker_channel import (
    WorkerLegResult,
    WorkerSession,
    identity_for_target,
    resolve_exec_scheme,
    run_worker_legs,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE = str(REPO / "tests" / "fixtures" / "see1550_forms")


def _mutant(i=1):
    return Mutant(file="x.gd", line=2, column=10, operator="+",
                  original="+", mutated="-", kind="AOR", scope_id="f",
                  n=i, stable_id=f"x.gd#f#{i}")


def _leg(leg=0, fail=0, failing=None, hash_ok=True, gut_ran=True):
    return WorkerLegResult(leg=leg, hash_ok=hash_ok, gut_ran=gut_ran,
                           fail_count=fail,
                           failing_tests=set(failing or ()),
                           reload={}, ok=bool(hash_ok and gut_ran))


class FakeSession:
    """脚本化会话：legs 队列按序返回，耗尽后返回 ok 腿。"""

    def __init__(self, script):
        self.script = list(script)
        self.dead = False
        self.dead_reason = ""
        self.closed = 0
        self.started = 0
        self.canary_notes = 0

    def start(self):
        self.started += 1

    def close(self):
        self.closed += 1
        self.dead = False

    def run_leg(self, leg, target_res, sha, closure, tests_glob,
                scene_refresh=None):
        item = self.script.pop(0) if self.script else _leg(leg)
        if getattr(item, "fatal", False):
            self.dead = True
            self.dead_reason = item.fatal_reason
        return item

    def note_canary(self):
        self.canary_notes += 1


class TestSchemeResolution:
    def test_opt_in_worker_scheme_on_eligible_target(self):
        scheme, route = resolve_exec_scheme(
            FIXTURE, "res://forms/runtime_load.gd", worker_flag=True)
        assert scheme == WORKER_SCHEME
        assert route is not None and route.channel == "worker"

    def test_default_is_cold(self, monkeypatch):
        monkeypatch.delenv(worker_channel.ENV_WORKER, raising=False)
        scheme, _route = resolve_exec_scheme(
            FIXTURE, "res://forms/runtime_load.gd", worker_flag=None)
        assert scheme == COLD_SCHEME

    def test_cold_route_target_stays_cold_even_opted_in(self):
        scheme, route = resolve_exec_scheme(
            FIXTURE, "res://forms/preload_boot.gd", worker_flag=True)
        assert scheme == COLD_SCHEME
        assert route is not None and route.channel == "cold"

    def test_exec_scheme_enters_identity_key(self):
        cold = identity_pack(FIXTURE, "linux", godot_bin="/no/g")
        worker = dict(cold, exec_scheme=WORKER_SCHEME)
        assert identity_sha1(cold) != identity_sha1(worker), \
            "killed 证书按 scheme 隔离 → 身份 key 必须不同"

    def test_identity_for_target_overrides_scheme(self):
        base = identity_pack(FIXTURE, "linux", godot_bin="/no/g")
        ident, route, scheme = identity_for_target(
            base, FIXTURE, "res://forms/runtime_load.gd", worker_flag=True)
        assert scheme == WORKER_SCHEME
        assert ident["exec_scheme"] == WORKER_SCHEME
        assert base["exec_scheme"] == COLD_SCHEME  # 原 dict 不被改（不可变）


class TestClassifyWorkerLeg:
    def test_hash_mismatch_is_run_error(self):
        r = classify_worker_leg(_mutant(), _leg(hash_ok=False), set(),
                                False, None, FIXTURE, "src")
        assert r["verdict"] == "run_error"

    def test_no_gut_evidence_is_run_error(self):
        r = classify_worker_leg(_mutant(), _leg(gut_ran=False), set(),
                                False, None, FIXTURE, "src")
        assert r["verdict"] == "run_error"

    def test_clean_pass_survived(self):
        r = classify_worker_leg(_mutant(), _leg(fail=0), set(), False,
                                None, FIXTURE, "src")
        assert r["verdict"] == "survived"

    def test_new_failure_killed(self):
        r = classify_worker_leg(_mutant(), _leg(fail=1, failing={"test_x"}),
                                set(), False, None, FIXTURE, "src")
        assert r["verdict"] == "killed"

    def test_preexisting_only_survived_or_suspect(self):
        leg = _leg(fail=1, failing={"test_old"})
        assert classify_worker_leg(
            _mutant(), leg, {"test_old"}, False, None, FIXTURE,
            "src")["verdict"] == "survived"
        assert classify_worker_leg(
            _mutant(), leg, {"test_old"}, True, None, FIXTURE,
            "src")["verdict"] == "suspect"


def _run_legs(script, mutants=None, cold_verdicts=None, classify=None):
    """驱动 run_worker_legs（假会话 + 假冷跑）并返回侧录。"""
    mutants = mutants or [_mutant(i) for i in range(1, 5)]
    cold_calls: list[int] = []
    recorded: list[tuple] = []
    sessions: list[FakeSession] = []

    def _factory(*a, **k):
        s = FakeSession(script.pop(0) if script else [])
        sessions.append(s)
        return s

    def _cold(m, seq):
        cold_calls.append(seq)
        verdict = (cold_verdicts or {}).get(seq, "killed")
        return {"verdict": verdict, "reason": "cold", "id": m.stable_id}

    def _classify(m, leg):
        return {"verdict": (classify or {}).get(leg.leg, "killed"),
                "reason": "worker", "id": m.stable_id}

    def _record(m, verdict, elapsed_s=0.0):
        recorded.append((m.stable_id, verdict["verdict"]))

    orig = worker_channel.WorkerSession
    worker_channel.WorkerSession = _factory
    try:
        pairs, fatals = run_worker_legs(
            mutants, type("Slot", (), {"root": "/tmp/x",
                                       "project_root": FIXTURE})(),
            "calc.gd", "res://forms/runtime_load.gd", ("res://a.gd",),
            "linux", "res://tests/",
            inject_fn=lambda m: ("sha-" + m.stable_id, None),
            cold_runner=_cold, record_fn=_record, classify_fn=_classify,
            leg_timeout_s=10)
    finally:
        worker_channel.WorkerSession = orig
    return {"pairs": pairs, "fatals": fatals, "cold": cold_calls,
            "recorded": recorded, "sessions": sessions}


class TestSessionFatal:
    def test_fatal_invalidates_window_and_respawns(self, monkeypatch):
        monkeypatch.setenv(worker_channel.ENV_CANARY_EVERY, "99")
        fatal = WorkerLegResult(leg=1, fatal=True,
                                fatal_reason="reload rc=43 (parse) — "
                                             "IGNORE 刷码残留", ok=False)
        out = _run_legs(script=[[_leg(0), fatal]])
        assert out["fatals"], "致命必须披露"
        assert "parse" in out["fatals"][0]
        # 窗口整批作废：legs 0..1 全部冷重跑（cold 调用 0,1,1=本腿再跑）
        assert 0 in out["cold"] and 1 in out["cold"]
        assert len(out["sessions"]) >= 2, "respawn 必须发生"
        verdicts = dict(out["recorded"])
        assert verdicts["x.gd#f#1"] == "killed"  # 冷权威覆盖
        assert verdicts["x.gd#f#2"] == "killed"

    def test_respawn_continues_worker_in_new_session(self, monkeypatch):
        """定案：fatal → respawn + 窗口冷重跑后，**后续腿在新会话继续**
        worker（不是全冷降级）。"""
        monkeypatch.setenv(worker_channel.ENV_CANARY_EVERY, "99")
        fatal = WorkerLegResult(leg=0, fatal=True, fatal_reason="hash gate",
                                ok=False)
        out = _run_legs(script=[[fatal]], mutants=[_mutant(i) for i in (1, 2, 3)])
        assert len(out["sessions"]) >= 2, "respawn 必须发生"
        assert out["sessions"][0].closed >= 1
        # leg0 冷权威；leg1/2 走新会话的 worker 腿（classify=killed）
        verdicts = dict(out["recorded"])
        assert verdicts["x.gd#f#1"] == "killed"
        assert verdicts["x.gd#f#2"] == "killed"
        assert 0 in out["cold"]

    def test_repeated_session_failures_degrade_to_cold(self, monkeypatch):
        """防抖：respawn 连续失败（两连）→ 剩余腿全冷降级并披露（不静默）。"""
        monkeypatch.setenv(worker_channel.ENV_CANARY_EVERY, "99")
        fatal0 = WorkerLegResult(leg=0, fatal=True, fatal_reason="start failed",
                                 ok=False)
        fatal1 = WorkerLegResult(leg=1, fatal=True, fatal_reason="start failed",
                                 ok=False)
        out = _run_legs(script=[[fatal0], [fatal1]],
                        mutants=[_mutant(i) for i in (1, 2, 3)])
        assert len(out["fatals"]) >= 2
        # leg2 在两次失败后走冷降级
        assert 2 in out["cold"]

    def test_canary_mismatch_invalidates(self, monkeypatch):
        """canary 失配 = 通道资格否证（D-W2）：窗口改冷权威 + 剩余全部冷
        降级，不再 respawn 后继续 worker（未验证判定禁止出货）。"""
        monkeypatch.setenv(worker_channel.ENV_CANARY_EVERY, "1")
        out = _run_legs(
            script=[[_leg(0), _leg(1)]],
            mutants=[_mutant(1), _mutant(2)],
            cold_verdicts={0: "survived", 1: "survived"},
            classify={0: "killed", 1: "killed"})
        assert out["fatals"] and "canary mismatch" in out["fatals"][0]
        verdicts = dict(out["recorded"])
        assert verdicts["x.gd#f#1"] == "survived"  # 冷权威
        assert verdicts["x.gd#f#2"] == "survived"  # 冷降级
        assert 0 in out["cold"] and 1 in out["cold"]
        assert len(out["sessions"]) == 1 and out["sessions"][0].closed >= 1

    def test_canary_match_notes_window(self, monkeypatch):
        monkeypatch.setenv(worker_channel.ENV_CANARY_EVERY, "1")
        out = _run_legs(
            script=[[_leg(0), _leg(1)]],
            mutants=[_mutant(1), _mutant(2)],
            cold_verdicts={0: "killed", 1: "killed"},
            classify={0: "killed", 1: "killed"})
        assert not out["fatals"]
        assert out["sessions"][0].canary_notes == 2
        verdicts = dict(out["recorded"])
        assert verdicts["x.gd#f#1"] == "killed"

    def test_inject_failure_records_run_error_without_leg(self, monkeypatch):
        monkeypatch.setenv(worker_channel.ENV_CANARY_EVERY, "99")
        mutants = [_mutant(1)]
        recorded = []
        orig = worker_channel.WorkerSession
        worker_channel.WorkerSession = lambda *a, **k: FakeSession([])
        try:
            pairs, fatals = run_worker_legs(
                mutants, type("Slot", (), {"root": "/tmp/x",
                                           "project_root": FIXTURE})(),
                "calc.gd", "res://forms/runtime_load.gd", ("res://a.gd",),
                "linux", "res://tests/",
                inject_fn=lambda m: (None, {"verdict": "run_error",
                                            "reason": "disk inject failed",
                                            "id": m.stable_id}),
                cold_runner=lambda m, s: {"verdict": "killed", "id": m.stable_id},
                record_fn=lambda m, v, elapsed_s=0.0: recorded.append(v["verdict"]),
                classify_fn=lambda m, leg: {"verdict": "killed",
                                            "id": m.stable_id},
                leg_timeout_s=10)
        finally:
            worker_channel.WorkerSession = orig
        assert recorded == ["run_error"]
        assert pairs and not fatals
