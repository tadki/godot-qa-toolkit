"""CLI `--steps` directory/package loading (SEE-1306 BL-8, §SPEC-006).

The library form of the KOL step-defs is a directory of per-domain modules
(`steps_economy.py`, `steps_save.py`, ...). The M1 loader used
`spec_from_file_location` without `submodule_search_locations`, which cannot
import a package and cannot aggregate a directory — so a library-shaped
`--steps` failed with exit 2. These tests pin the new contract.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent.parent

FEATURE = """\
Feature: steps loader
  Scenario: aggregated
    Given a domain step
"""


def _run_cli(*argv: str, cwd: Path) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, "-m", "godot_qa_toolkit.cli", *argv],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )
    # Caller errors (exit 2) print on stderr without a JSON contract, matching
    # the M1 convention; only verdict paths emit JSON on stdout.
    if proc.returncode == 2:
        return 2, {"stderr": proc.stderr}
    start = proc.stdout.index("{")
    return proc.returncode, json.loads(proc.stdout[start:])


def _write_feature(tmp_path: Path) -> Path:
    feature = tmp_path / "dir.feature"
    feature.write_text(FEATURE, encoding="utf-8")
    return feature


def _write_steps(dir_path: Path, name: str, body: str) -> None:
    (dir_path / name).write_text(body, encoding="utf-8")


class TestDirectorySteps:
    def test_directory_aggregates_every_domain_module(self, tmp_path):
        steps = tmp_path / "kol"
        steps.mkdir()
        _write_steps(
            steps,
            "steps_economy.py",
            "def register(registry):\n"
            "    @registry.step('a domain step', keyword='Given')\n"
            "    def _s(ctx):\n"
            "        ctx['economy'] = True\n",
        )
        _write_steps(
            steps,
            "steps_save.py",
            "def register(registry):\n"
            "    pass\n",
        )
        rc, out = _run_cli(
            "gherkin", str(_write_feature(tmp_path)), "--steps", str(steps), cwd=REPO / "src"
        )
        assert rc == 0, out
        assert out["ok"] is True

    def test_private_helpers_are_skipped(self, tmp_path):
        steps = tmp_path / "kol"
        steps.mkdir()
        _write_steps(
            steps,
            "steps_economy.py",
            "def register(registry):\n"
            "    @registry.step('a domain step', keyword='Given')\n"
            "    def _s(ctx):\n"
            "        pass\n",
        )
        # A shared helper is not a steps module: it must not be auto-registered
        # (importing it is fine, calling register() on it is not the contract).
        _write_steps(steps, "_bridge.py", "SHARED = 1\n")
        rc, out = _run_cli(
            "gherkin", str(_write_feature(tmp_path)), "--steps", str(steps), cwd=REPO / "src"
        )
        assert rc == 0, out

    def test_directory_without_register_module_is_a_caller_error(self, tmp_path):
        steps = tmp_path / "empty"
        steps.mkdir()
        rc, out = _run_cli(
            "gherkin", str(_write_feature(tmp_path)), "--steps", str(steps), cwd=REPO / "src"
        )
        assert rc == 2
        assert "no steps modules" in out["stderr"]

    def test_missing_steps_path_is_a_caller_error(self, tmp_path):
        rc, out = _run_cli(
            "gherkin",
            str(_write_feature(tmp_path)),
            "--steps",
            str(tmp_path / "nope"),
            cwd=REPO / "src",
        )
        assert rc == 2
        assert "cannot load steps module" in out["stderr"]

    def test_single_file_still_works(self, tmp_path):
        steps = tmp_path / "single.py"
        _write_steps(
            tmp_path,
            "single.py",
            "def register(registry):\n"
            "    @registry.step('a domain step', keyword='Given')\n"
            "    def _s(ctx):\n"
            "        pass\n",
        )
        rc, out = _run_cli(
            "gherkin", str(_write_feature(tmp_path)), "--steps", str(steps), cwd=REPO / "src"
        )
        assert rc == 0, out

    def test_feature_level_scenario_timeout_flag(self, tmp_path):
        steps = tmp_path / "slow.py"
        _write_steps(
            tmp_path,
            "slow.py",
            "import time\n"
            "def register(registry):\n"
            "    @registry.step('a domain step', keyword='Given')\n"
            "    def _s(ctx):\n"
            "        time.sleep(5)\n",
        )
        rc, out = _run_cli(
            "gherkin",
            str(_write_feature(tmp_path)),
            "--steps",
            str(steps),
            "--timeout",
            "1",
            cwd=REPO / "src",
        )
        assert rc == 1
        assert out["failures"][0]["reason"].startswith("ScenarioTimeout")
