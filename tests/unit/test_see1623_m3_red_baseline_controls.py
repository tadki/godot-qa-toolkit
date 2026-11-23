"""SEE-1623 M3 — 红基线控制与披露行为钉。

根因（SEE-1622 Harti BUG-6/BUG-7 + BUG-1 披露子项）：
- `--check-baseline` 在 dry-run plan-only 早退形态零发生（env 在 serial
  路径才设置，早退不经过）；
- `--fail-on-red-baseline` 只设 measure 面 env，mutation 自身从未快速
  失败（红基线检出后 28 变异照常执行完）；
- unsupported 面披露 `applied=true` 与 face 自相矛盾（worker 直驱物理
  跳红测试时 face 仍标 unsupported_explicit_or_full）。

行为契约：check-baseline 在 plan-only 形态执行真实基线检查（未请求 =
not_checked）；fail-on-red-baseline 快速失败（与
break-on-baseline-failure 同通道）；baseline_skip 披露 face 与 applied
自洽（worker 物理跳 → face=worker_direct_drive；不支持面 applied=false）。
"""

from __future__ import annotations

from godot_qa_toolkit.cli import _p1_prepare_gate


class TestBaselineSkipDisclosureConsistency:
    def test_worker_skip_face_labels_channel(self):
        from godot_qa_toolkit.mutation.runner import \
            _baseline_skip_disclosure as d
        out = d(dirty=True, skip_applied=True, worker_host=True,
                bail_face="unsupported_explicit_or_full", requested=1)
        assert out["applied"] is True
        assert out["face"] == "worker_direct_drive"
        assert out["requested"] == 1

    def test_unsupported_face_not_applied(self):
        from godot_qa_toolkit.mutation.runner import \
            _baseline_skip_disclosure as d
        out = d(dirty=True, skip_applied=False, worker_host=False,
                bail_face="unsupported_explicit_or_full", requested=2)
        assert out["applied"] is False
        assert out["face"] == "unsupported_explicit_or_full"

    def test_cold_supported_face_applied(self):
        from godot_qa_toolkit.mutation.runner import \
            _baseline_skip_disclosure as d
        out = d(dirty=True, skip_applied=True, worker_host=False,
                bail_face="tests_face", requested=1)
        assert out["applied"] is True and out["face"] == "tests_face"


class TestM3DegradedDisclosure:
    """worker 直驱降级冷通道后的披露自洽（QA 观察 2）：不得出现
    face=worker_direct_drive 且 applied=true 而实际执行面为 cold。"""

    def test_degraded_to_cold_reports_effective_face(self):
        from godot_qa_toolkit.mutation.runner import \
            _baseline_skip_disclosure as d
        out = d(dirty=True, skip_applied=True, worker_host=True,
                bail_face="unsupported_explicit_or_full", requested=1,
                effective_face="unsupported_explicit_or_full",
                degrade_reason="parity_mismatch", cold_can_skip=False)
        assert out["face"] == "unsupported_explicit_or_full"
        assert out["planned_face"] == "worker_direct_drive"
        assert out["degraded"] is True
        assert out["degrade_reason"] == "parity_mismatch"
        assert out["applied"] is False

    def test_degraded_with_skip_capable_cold_reports_applied(self):
        from godot_qa_toolkit.mutation.runner import \
            _baseline_skip_disclosure as d
        out = d(dirty=True, skip_applied=True, worker_host=True,
                bail_face="tests_face", requested=2,
                effective_face="tests_face", degrade_reason="session_fatal",
                cold_can_skip=True)
        assert out["face"] == "tests_face"
        assert out["planned_face"] == "worker_direct_drive"
        assert out["degraded"] is True
        assert out["applied"] is True

    def test_degraded_state_detection(self):
        from godot_qa_toolkit.mutation.runner import \
            _worker_degraded_state as st
        assert st({}) == (False, None)
        assert st({"worker_channel": {"decision": "worker",
                                      "reason": "worker_reset_session"}}) \
            == (False, None)
        degraded, reason = st({"worker_channel": {
            "decision": "worker_degraded_cold", "reason": "parity_mismatch"}})
        assert degraded is True and reason == "parity_mismatch"
        degraded, reason = st({"worker_fatal_events": ["parity leg unavailable"]})
        assert degraded is True and "parity" in reason


class TestCheckBaselinePlanOnly:
    def test_not_requested_reports_not_checked(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GQT_MUTATION_CHECK_BASELINE", raising=False)
        from godot_qa_toolkit.cli import _plan_only_baseline_health
        h = _plan_only_baseline_health(
            type("A", (), {"project_root": str(tmp_path),
                           "timeout": 1})())
        assert h["status"] == "not_checked"
        assert h["reason"] == "check_baseline_not_requested"


class TestFailOnRedWiring:
    def test_fail_on_red_or_break_on_passed(self, tmp_path, monkeypatch):
        """--fail-on-red-baseline 与 break-on 通道合流（快速失败语义）：
        run_mutation 收到 break_on_baseline_failure=True。"""
        from godot_qa_toolkit import cli
        from godot_qa_toolkit.mutation import runner as runner_mod

        captured: dict = {}

        def spy_run_mutation(**k):
            captured["break_on"] = k.get("break_on_baseline_failure")
            return {"tool": "mutation", "ok": True, "summary": {},
                    "failures": []}

        monkeypatch.setattr(runner_mod, "run_mutation", spy_run_mutation)

        class _Args:
            files = [str(tmp_path / "calc.gd")]
            project_root = str(tmp_path)
            budget = 1
            timeout = 60
            dry_run = True
            tests = None
            jobs = None
            binary_kind = None
            timing_store = None
            mutation_warning = 50
            timeout_factor = 2.0
            mutate_all = False
            worker_channel = False
            select_mode = "full"
            break_on_baseline_failure = False
            fail_on_red_baseline = True
            kill_bail = None
            mutation = None
            lines = None
            reuse_coverage = True

        cli._run_mutation_serial(_Args(), "linux")
        assert captured["break_on"] is True

    def test_break_on_alone_still_passed(self, tmp_path, monkeypatch):
        from godot_qa_toolkit import cli
        from godot_qa_toolkit.mutation import runner as runner_mod

        captured: dict = {}

        def spy_run_mutation(**k):
            captured["break_on"] = k.get("break_on_baseline_failure")
            return {"tool": "mutation", "ok": True, "summary": {},
                    "failures": []}

        monkeypatch.setattr(runner_mod, "run_mutation", spy_run_mutation)

        class _Args:
            files = [str(tmp_path / "calc.gd")]
            project_root = str(tmp_path)
            budget = 1
            timeout = 60
            dry_run = True
            tests = None
            jobs = None
            binary_kind = None
            timing_store = None
            mutation_warning = 50
            timeout_factor = 2.0
            mutate_all = False
            worker_channel = False
            select_mode = "full"
            break_on_baseline_failure = True
            fail_on_red_baseline = False
            kill_bail = None
            mutation = None
            lines = None
            reuse_coverage = True

        cli._run_mutation_serial(_Args(), "linux")
        assert captured["break_on"] is True
