"""SEE-1413 BDD steps — fake GUT stub, zero real-Godot dependency."""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

from godot_qa_toolkit.gherkin.runner import Registry, StepFailure

REPO = Path(__file__).parent.parent

sys.path.insert(0, str(REPO / "src"))

from godot_qa_toolkit.mutation import executor  # noqa: E402
import godot_qa_toolkit.mutation.runner as runner_mod  # noqa: E402

GD_4_SITES = (
    "func f(a, b):\n"
    "\tif a > 0:\n"
    "\t\treturn a + b\n"
    "\treturn a - b and a * b\n"
)
GD_2_SITES = "func f(a, b):\n\treturn a + b\n"


class _SerialPool:
    def __init__(self, max_workers=None):
        self.max_workers = max_workers

    def map(self, fn, iterable):
        return [fn(x) for x in iterable]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _write_project(source: str) -> tuple[str, str]:
    import tempfile
    root = Path(tempfile.mkdtemp(prefix="see1413-bdd-"))
    (root / "addons" / "gut").mkdir(parents=True)
    (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
    save = root / "tests" / "unit" / "save"
    save.mkdir(parents=True)
    (save / "test_save.gd").write_text('preload("res://calc.gd")\n')
    target = root / "calc.gd"
    target.write_text(source)
    return str(root), str(target)


def register(registry: Registry) -> None:
    state: dict[str, object] = {}

    @registry.step("a mutation target with {count} mutation sites and a passing GUT stub")
    def _given_target(_p: dict[str, str], count: str) -> None:
        source = GD_4_SITES if int(count) >= 4 else GD_2_SITES
        root, target = _write_project(source)
        state["expected_mutants"] = int(count)
        executor._worker_pool = _SerialPool
        executor._run_baseline_snapshot = (  # type: ignore[method-assign]
            lambda r, g, t: {"rc": 0, "tail": "", "elapsed": 5.0})
        runner_mod._run_gut_on_project = (  # type: ignore[method-assign]
            lambda r, timeout_s=60, tests_glob=None: (0, "pass"))
        state["root"], state["target"] = root, target
        state["prewarm"] = {"n": 0}
        orig_prewarm = executor.prewarm_import
        executor.prewarm_import = lambda r: state["prewarm"].__setitem__(  # type: ignore[method-assign]
            "n", state["prewarm"]["n"] + 1)
        state["orig_prewarm"] = orig_prewarm

    @registry.step("gqt mutation runs the single file with jobs omitted")
    def _when_default(_p: dict[str, str]) -> None:
        from godot_qa_toolkit import cli as cli_mod
        args = cli_mod.build_parser().parse_args([
            "mutation", state["target"], "--project-root", state["root"],
            "--tests", "res://tests/unit/save/",
        ])
        output = io.StringIO()
        cpu_count = executor.os.cpu_count
        executor.os.cpu_count = lambda: 16
        try:
            with redirect_stdout(output):
                cli_mod.cmd_mutation(args)
        finally:
            executor.os.cpu_count = cpu_count
        state["result"] = cli_mod.json.loads(output.getvalue())

    @registry.step("gqt mutation runs the single file with jobs {n}")
    def _when_jobs(_p: dict[str, str], n: str) -> None:
        state["result"] = executor.run_mutation_files(
            [state["target"]], state["root"], budget=50, timeout_s=60,
            dry_run=False, tests_glob="res://tests/unit/save/", jobs=int(n))

    @registry.step("the mutation --jobs help text is rendered")
    def _when_help(_p: dict[str, str]) -> None:
        from godot_qa_toolkit.cli import build_parser
        choices = build_parser()._subparsers._group_actions[0].choices
        action = next(a for a in choices["mutation"]._actions if a.dest == "jobs")
        state["help_text"] = action.help

    @registry.step("the report top-level shape is flat with jobs greater than {n}")
    def _then_flat(_p: dict[str, str], n: str) -> None:
        d = state["result"]
        if set(d) != {"tool", "ok", "summary", "failures"}:
            raise StepFailure(f"top-level shape not flat: {sorted(d)}")
        jobs = d["summary"].get("jobs")
        if jobs is None or not jobs > int(n):
            raise StepFailure(f"summary.jobs={jobs} not > {n}")

    @registry.step("the flat summary keeps existing keys and adds jobs")
    def _then_keys(_p: dict[str, str]) -> None:
        summary = state["result"]["summary"]
        expected = {
            "file", "mutants", "killed", "survived", "suspect", "timeout",
            "run_errors", "invalid_mutants", "kill_rate", "budget",
            "adaptive_timeout_s", "scoped", "baseline_reused",
            "timing_source", "samples_n", "baseline_p90", "jobs",
        }
        if set(summary) != expected:
            raise StepFailure(f"summary keys changed: {sorted(set(summary))}")
        if summary["mutants"] != state["expected_mutants"]:
            raise StepFailure(
                f"merged mutants={summary['mutants']} != {state['expected_mutants']}")

    @registry.step("the report falls back to serial with jobs {n}")
    def _then_serial(_p: dict[str, str], n: str) -> None:
        d = state["result"]
        if d["jobs"] != int(n):
            raise StepFailure(f"expected serial jobs={n}, got {d['jobs']}")
        if d["results"][0]["summary"].get("jobs") != int(n):
            raise StepFailure("serial result summary.jobs mismatch")

    @registry.step("zero prewarm imports were performed")
    def _then_no_prewarm(_p: dict[str, str]) -> None:
        if state["prewarm"]["n"] != 0:
            raise StepFailure(f"prewarm ran {state['prewarm']['n']} time(s)")

    @registry.step("the report reports jobs {n} with a single result")
    def _then_single(_p: dict[str, str], n: str) -> None:
        d = state["result"]
        if d["jobs"] != int(n) or len(d["results"]) != 1:
            raise StepFailure(f"jobs={d['jobs']} results={len(d['results'])}")
        if d["results"][0]["summary"].get("jobs") != int(n):
            raise StepFailure("jobs=1 result summary.jobs mismatch")
        if state["prewarm"]["n"] != 0:
            raise StepFailure("jobs=1 unexpectedly prewarmed")

    @registry.step("the text mentions per-file AND per-mutant slicing")
    def _then_text_levels(_p: dict[str, str]) -> None:
        if "per-file AND per-mutant" not in state["help_text"]:
            raise StepFailure("help text missing per-file AND per-mutant")

    @registry.step("the text mentions single file support")
    def _then_text_single(_p: dict[str, str]) -> None:
        if "single file" not in state["help_text"]:
            raise StepFailure("help text missing single file note")
