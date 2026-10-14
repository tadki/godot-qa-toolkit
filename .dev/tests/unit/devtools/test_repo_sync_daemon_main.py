"""Real-behavior tests for repo_sync_daemon.main() loop (SEE-1406 ③ D1 rework).

Drives the REAL daemon main() — importing repo_sync_daemon and stubbing only
the clock and the subprocess boundary (run_one_cycle) — because the previous
TestExponentialBackoff / TestCircuitBreaker classes re-derived the loop formulas
inside the test bodies and never called main(), so mutants M5 (backoff doubling
removed, daemon:261) and M6 (transient counted toward breaker, daemon:233)
survived the full suite. These tests kill both anchors.
"""

import json
import logging
import sys

import pytest

import repo_sync_daemon as daemon_mod


class FakeClock:
    """Deterministic time.time/time.sleep replacement: sleep advances the clock."""

    def __init__(self):
        self.t = 0.0
        self.sleep_calls = []

    def time(self):
        return self.t

    def sleep(self, seconds):
        self.sleep_calls.append(seconds)
        self.t += seconds


@pytest.fixture
def daemon_env(tmp_path, monkeypatch):
    """Config file + module-level stubs so real main() runs without side effects."""
    log_dir = tmp_path / "logs"
    config = {
        "repo_path": str(tmp_path / "repo"),
        "remote": "origin",
        "branch": "master",
        "log_dir": str(log_dir),
        "state_file": str(tmp_path / "state" / "state.json"),
        "pid_file": str(tmp_path / "daemon.pid"),
        "daemon_poll_interval_seconds": 1,
        "daemon_max_consecutive_failures": 3,
        "daemon_backoff_max_seconds": 10,
        "subprocess_timeout_seconds": 7,
    }
    config_path = tmp_path / "repo_sync_config.json"
    import json

    config_path.write_text(json.dumps(config))

    clock = FakeClock()
    monkeypatch.setattr(daemon_mod, "time", clock)
    monkeypatch.setattr(daemon_mod.signal, "signal", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["repo_sync_daemon.py", "--config", str(config_path)])
    monkeypatch.setattr(daemon_mod, "_running", True)

    cycles: list[float] = []
    stub = {
        "results": [],  # list of (ok, error_category) returned per cycle
        "stop_after": None,  # set _running=False after this many cycles
    }

    def fake_run_one_cycle(sync_script, config, config_path, logger, timeout=None):
        cycles.append(clock.t)
        idx = stub["calls"]
        ok, category = stub["results"][idx]
        if stub["stop_after"] is not None and idx + 1 >= stub["stop_after"]:
            daemon_mod._running = False
        return ok, category

    stub["calls"] = 0

    def counting_stub(*args, **kwargs):
        result = fake_run_one_cycle(*args, **kwargs)
        stub["calls"] += 1
        return result

    monkeypatch.setattr(daemon_mod, "run_one_cycle", counting_stub)
    return {
        "config": config,
        "config_path": config_path,
        "clock": clock,
        "cycles": cycles,
        "stub": stub,
        "log_dir": log_dir,
    }


class TestDaemonMainRealLoop:
    """SPEC-011: daemon main() loop real behaviors via the imported module."""

    def test_daemon_main_backoff_interval_doubles_and_caps(
        self, daemon_env, monkeypatch
    ):
        """Persistent failures double the poll interval each cycle, capped at
        daemon_backoff_max_seconds (kills M5: min(current_interval*2, max))."""
        env = daemon_env
        # max_failures=3 in this env's config; raise it so the breaker does not
        # fire and the backoff sequence is observable across many cycles.
        env["config"]["daemon_max_consecutive_failures"] = 99
        env["config_path"].write_text(json.dumps(env["config"]))
        env["stub"]["results"] = [(False, "persistent")] * 20
        env["stub"]["stop_after"] = 7  # 7 failing cycles then signal stop
        monkeypatch.setattr(daemon_mod, "send_alert", lambda *a, **k: (True, "ok"))

        rc = daemon_mod.main()
        assert rc == 0

        gaps = [b - a for a, b in zip(env["cycles"], env["cycles"][1:])]
        # The first gap uses the poll interval only when the cycle ran at the
        # pre-loop instant (cycle 0 runs before any sleep); the daemon sleeps the
        # *backed-off* interval after each failure, so gaps after cycle 0 are
        # 2, 4, 8, 10, 10, ... and the very first sleep is also 2. Cycles are
        # recorded at their start times: cycle0 t=0, cycle1 t=2, cycle2 t=6...
        # poll=1, backoff_max=10 → gaps must be 2, 4, 8, 10, 10, 10 (capped)
        assert gaps == [2, 4, 8, 10, 10, 10]

    def test_daemon_main_circuit_breaker_stops_at_threshold_and_sends_alert(
        self, daemon_env, monkeypatch
    ):
        """daemon_max_consecutive_failures consecutive persistent failures stop
        main() after exactly that many cycles, sending one alert (kills M6 side
        effects on the persistent path and proves send_alert wiring)."""
        env = daemon_env
        env["stub"]["results"] = [(False, "persistent")] * 20
        env["stub"]["stop_after"] = None  # loop must terminate via breaker
        alerts = []
        monkeypatch.setattr(
            daemon_mod, "send_alert", lambda config, msg: alerts.append(msg) or (True, "ok")
        )

        rc = daemon_mod.main()
        assert rc == 0
        assert len(env["cycles"]) == 3, "main() must stop after max_failures cycles"
        assert len(alerts) == 1
        assert "Circuit breaker" in alerts[0]

    def test_daemon_main_transient_error_category_not_counted_toward_breaker(
        self, daemon_env, monkeypatch
    ):
        """transient error_category failures never increment the breaker counter
        and never back off (kills M6: skip_count = error_category == "transient").
        (also SPEC-004 anchor: error_category classification drives counting)"""
        env = daemon_env
        env["stub"]["results"] = [(False, "transient")] * 20
        env["stub"]["stop_after"] = 6
        alerts = []
        monkeypatch.setattr(
            daemon_mod, "send_alert", lambda config, msg: alerts.append(msg) or (True, "ok")
        )

        rc = daemon_mod.main()
        assert rc == 0
        # 6 transient cycles — breaker (3) never fires
        assert len(env["cycles"]) == 6
        assert alerts == []
        # no backoff: every gap stays at the base poll interval
        gaps = [b - a for a, b in zip(env["cycles"], env["cycles"][1:])]
        assert gaps == [1, 1, 1, 1, 1]

    def test_daemon_main_success_resets_backoff_and_breaker(
        self, daemon_env, monkeypatch
    ):
        """A success between persistent failures resets both the interval and the
        consecutive-failure counter (real loop, not a re-derived formula)."""
        env = daemon_env
        env["stub"]["results"] = [
            (False, "persistent"),
            (False, "persistent"),
            (True, ""),
            (False, "persistent"),
            (False, "persistent"),
            (False, "persistent"),
        ]
        env["stub"]["stop_after"] = None  # breaker must stop it after cycle 6
        monkeypatch.setattr(daemon_mod, "send_alert", lambda *a, **k: (True, "ok"))

        rc = daemon_mod.main()
        assert rc == 0
        # 2 fail (interval 1→2→4) + success (reset to 1) + 3 fail (2→4, breaker)
        assert len(env["cycles"]) == 6
        gaps = [b - a for a, b in zip(env["cycles"], env["cycles"][1:])]
        assert gaps == [2, 4, 1, 2, 4]

    def test_daemon_main_backoff_max_from_config_is_honored(
        self, daemon_env, monkeypatch
    ):
        """daemon_backoff_max_seconds caps the interval even with many failures."""
        env = daemon_env
        env["config"]["daemon_max_consecutive_failures"] = 99
        env["config_path"].write_text(json.dumps(env["config"]))
        env["stub"]["results"] = [(False, "persistent")] * 20
        env["stub"]["stop_after"] = 6
        monkeypatch.setattr(daemon_mod, "send_alert", lambda *a, **k: (True, "ok"))

        rc = daemon_mod.main()
        assert rc == 0
        gaps = [b - a for a, b in zip(env["cycles"], env["cycles"][1:])]
        assert max(gaps) <= env["config"]["daemon_backoff_max_seconds"]
        assert gaps[-1] == env["config"]["daemon_backoff_max_seconds"]
