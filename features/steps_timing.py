"""SEE-1356 L1 gherkin steps — in-process budget-negotiation scenario."""

from __future__ import annotations

import subprocess
import time

from godot_qa_toolkit.timing import TimingStore
from godot_qa_toolkit.gherkin.runner import Registry, StepFailure


def register(registry: Registry) -> None:
    state: dict[str, object] = {}

    @registry.step("a mutation target with a cached baseline sample of {seconds} seconds")
    def _given_target(_p: dict[str, str], seconds: str) -> None:
        import tempfile
        from pathlib import Path

        root = Path(tempfile.mkdtemp(prefix="see1356-timing-"))
        (root / "addons" / "gut").mkdir(parents=True)
        (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
        target = root / "calc.gd"
        target.write_text("func add(a, b):\n\treturn a + b\n")
        store = TimingStore(str(root), str(target), "mutation", "res://tests/",
                            now=time.time())
        assert store.record(float(seconds)) is True
        state["root"] = str(root)
        state["target"] = str(target)
        state["store"] = store
        state["seconds"] = float(seconds)

    @registry.step("the mutation run resolves its timeout budget from the timing store")
    def _when_run(_p: dict[str, str]) -> None:
        from godot_qa_toolkit.mutation import runner as mut

        calls = {"runs": 0, "last_timeout": None}

        def fake_run_gut(project_root, timeout_s=60, tests_glob=None):
            calls["runs"] += 1
            calls["last_timeout"] = timeout_s
            time.sleep(0.01)  # baseline 实测耗时 > 0
            return 0, ""

        orig = mut._run_gut_on_project
        mut._run_gut_on_project = fake_run_gut
        try:
            state["result"] = mut.run_mutation(
                str(state["target"]), str(state["root"]), budget=5,
                timeout_s=60, tests_glob="res://tests/",
                timing_store=state["store"])
        finally:
            mut._run_gut_on_project = orig
        state["calls"] = calls

    @registry.step("the budget anchor comes from the cache")
    def _then_anchor(_p: dict[str, str]) -> None:
        expected = int(float(state["seconds"]) * 2.0) + 1
        if state["calls"]["last_timeout"] != expected:
            raise StepFailure(
                reason="budget anchor did not come from the cache",
                detail=f"expected baseline timeout {expected}, "
                       f"got {state['calls']['last_timeout']}")
        result = state["result"]
        if result["summary"]["timing_source"] != "store":
            raise StepFailure(reason="timing_source not store",
                              detail=str(result["summary"]))

    @registry.step("the baseline GUT run still executes as the kill-diff control")
    def _then_baseline_measured(_p: dict[str, str]) -> None:
        if state["calls"]["runs"] < 1:
            raise StepFailure(reason="baseline GUT never ran (cache must feed "
                                     "the anchor only, never the baseline)")
        if state["result"]["summary"]["baseline_reused"] is not False:
            raise StepFailure(reason="baseline_reused is not False — the "
                                     "control run was skipped")
        if state["result"]["summary"]["samples_n"] < 2:
            raise StepFailure(reason="this run's measured sample was not recorded",
                              detail=str(state["result"]["summary"]))
