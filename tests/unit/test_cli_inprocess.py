"""In-process CLI tests (SEE-1306 hardener, §SPEC-006/010).

The M1-era CLI tests exercise `python -m godot_qa_toolkit.cli` via
subprocess, so pytest-cov never sees inside `cli.py` (44%). These tests call
`build_parser().parse_args(...)` + the command function directly in-process,
closing that blind spot without replacing the subprocess suite (which pins
real interpreter behaviour: exit codes, stderr-only caller errors).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from godot_qa_toolkit import cli as cli_mod
from godot_qa_toolkit.cli import build_parser

REPO = Path(__file__).parent.parent.parent

FEATURE = """\
Feature: in-process
  Scenario: passes
    Given a domain step
"""


def _args(*argv: str) -> pytest.MonkeyPatch:
    return build_parser().parse_args(list(argv))


class TestLoadStepsModule:
    def test_loads_and_registers(self, tmp_path):
        steps = tmp_path / "steps_x.py"
        steps.write_text(
            "def register(r):\n"
            "    @r.step('a domain step', keyword='Given')\n"
            "    def _s(ctx):\n"
            "        ctx['hit'] = True\n",
            encoding="utf-8",
        )
        registry = cli_mod.Registry()
        cli_mod._load_steps_module(steps, registry)
        found = registry.find(_fake_step("Given", "a domain step"))
        assert found is not None

    def test_sibling_import_needs_dir_on_sys_path_only_during_exec(self, tmp_path):
        # Helper + domain module pair: the import only resolves while the
        # steps module executes; afterwards the sys.path entry is removed.
        (tmp_path / "_helper.py").write_text("TOKEN = 'ok'\n", encoding="utf-8")
        (tmp_path / "steps_pair.py").write_text(
            "import _helper\n"
            "def register(r):\n"
            "    @r.step('a domain step', keyword='Given')\n"
            "    def _s(ctx):\n"
            "        ctx['token'] = _helper.TOKEN\n",
            encoding="utf-8",
        )
        registry = cli_mod.Registry()
        cli_mod._load_steps_module(tmp_path / "steps_pair.py", registry)
        assert registry.find(_fake_step("Given", "a domain step")) is not None
        import sys

        assert str(tmp_path) not in sys.path

    def test_already_on_sys_path_is_not_removed(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.syspath_prepend(str(tmp_path))
        (tmp_path / "steps_dup.py").write_text(
            "def register(r):\n    pass\n", encoding="utf-8"
        )
        registry = cli_mod.Registry()
        cli_mod._load_steps_module(tmp_path / "steps_dup.py", registry)
        assert str(tmp_path) in sys.path  # not ours to remove

    def test_unbuildable_spec_raises_valueerror(self, tmp_path):
        # A directory passed as the module path yields spec=None.
        with pytest.raises(ValueError, match="cannot build import spec"):
            cli_mod._load_steps_module(tmp_path, cli_mod.Registry())


def _fake_step(keyword: str, text: str):
    from godot_qa_toolkit.gherkin.parser import Step

    return Step(keyword=keyword, text=text, line=1)


class TestCmdGherkinInProcess:
    def _feature(self, tmp_path: Path, text: str = FEATURE) -> Path:
        f = tmp_path / "in.feature"
        f.write_text(text, encoding="utf-8")
        return f

    def _steps(self, tmp_path: Path) -> Path:
        steps = tmp_path / "steps_ok.py"
        steps.write_text(
            "def register(r):\n"
            "    @r.step('a domain step', keyword='Given')\n"
            "    def _s(ctx):\n"
            "        pass\n",
            encoding="utf-8",
        )
        return steps

    def test_pass_path_emits_json_and_exit_zero(self, tmp_path, capsys):
        feature = self._feature(tmp_path)
        args = _args("gherkin", str(feature), "--steps", str(self._steps(tmp_path)),
                     "--timeout", "10")
        rc = cli_mod.cmd_gherkin(args)
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["ok"] is True
        assert out["summary"]["passed"] == 1

    def test_syntax_error_emits_feature_null_contract(self, tmp_path, capsys):
        feature = self._feature(tmp_path, "Scenario: orphan\n")
        args = _args("gherkin", str(feature))
        rc = cli_mod.cmd_gherkin(args)
        out = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert out["feature"] is None
        assert out["summary"] == {"total": 0, "passed": 0, "failed": 1}
        assert ":1:" in out["failures"][0]["reason"]

    def test_directory_steps_aggregate_in_process(self, tmp_path, capsys):
        lib = tmp_path / "kol"
        lib.mkdir()
        (lib / "_shared.py").write_text("X = 1\n", encoding="utf-8")
        (lib / "steps_ok.py").write_text(
            "def register(r):\n"
            "    @r.step('a domain step', keyword='Given')\n"
            "    def _s(ctx):\n"
            "        pass\n",
            encoding="utf-8",
        )
        args = _args("gherkin", str(self._feature(tmp_path)), "--steps", str(lib))
        rc = cli_mod.cmd_gherkin(args)
        assert rc == 0
        assert json.loads(capsys.readouterr().out)["ok"] is True

    def test_empty_directory_is_caller_error(self, tmp_path, capsys):
        empty = tmp_path / "empty"
        empty.mkdir()
        args = _args("gherkin", str(self._feature(tmp_path)), "--steps", str(empty))
        rc = cli_mod.cmd_gherkin(args)
        captured = capsys.readouterr()
        assert rc == 2
        assert "no steps modules" in captured.err
        assert captured.out == ""

    def test_missing_steps_is_caller_error(self, tmp_path, capsys):
        args = _args("gherkin", str(self._feature(tmp_path)),
                     "--steps", str(tmp_path / "nope"))
        rc = cli_mod.cmd_gherkin(args)
        captured = capsys.readouterr()
        assert rc == 2
        assert "cannot load steps module" in captured.err

    def test_steps_file_without_register_is_caller_error(self, tmp_path, capsys):
        # A steps module that cannot even satisfy its contract is an
        # environment/caller problem (exit 2), never a fake test verdict.
        bad = tmp_path / "steps_bad.py"
        bad.write_text("X = 1\n", encoding="utf-8")
        args = _args("gherkin", str(self._feature(tmp_path)), "--steps", str(bad))
        rc = cli_mod.cmd_gherkin(args)
        captured = capsys.readouterr()
        assert rc == 2
        assert "cannot load steps module" in captured.err

    def test_steps_file_with_missing_import_is_caller_error(self, tmp_path, capsys):
        # Mirrors the no-import-cache scenario (SEE-1306 D2): the steps module
        # raises ImportError at exec time — environment not ready, not a red
        # run. CI must see exit 2, not a false regression.
        bad = tmp_path / "steps_dep.py"
        bad.write_text("import no_such_module_xyz\n", encoding="utf-8")
        args = _args("gherkin", str(self._feature(tmp_path)), "--steps", str(bad))
        rc = cli_mod.cmd_gherkin(args)
        captured = capsys.readouterr()
        assert rc == 2
        assert "cannot load steps module" in captured.err
        assert captured.out == ""

    def test_failing_step_emits_failure_with_context(self, tmp_path, capsys):
        steps = tmp_path / "steps_fail.py"
        steps.write_text(
            "from godot_qa_toolkit.gherkin.runner import StepFailure\n"
            "def register(r):\n"
            "    @r.step('a domain step', keyword='Given')\n"
            "    def _s(ctx):\n"
            "        raise StepFailure('boom', detail='why', stderr_tail='tail')\n",
            encoding="utf-8",
        )
        args = _args("gherkin", str(self._feature(tmp_path).with_name("in.feature")),
                     "--steps", str(steps), "--timeout", "10")
        rc = cli_mod.cmd_gherkin(args)
        out = json.loads(capsys.readouterr().out)
        assert rc == 1
        f = out["failures"][0]
        assert "StepFailure: boom" in f["reason"]
        assert f["detail"] == "why"
        assert f["stderr_tail"] == "tail"

    def test_timeout_flag_reaches_the_runner(self, tmp_path, capsys):
        steps = tmp_path / "steps_slow.py"
        steps.write_text(
            "import time\n"
            "def register(r):\n"
            "    @r.step('a domain step', keyword='Given')\n"
            "    def _s(ctx):\n"
            "        time.sleep(5)\n",
            encoding="utf-8",
        )
        args = _args("gherkin", str(self._feature(tmp_path)),
                     "--steps", str(steps), "--timeout", "1")
        rc = cli_mod.cmd_gherkin(args)
        out = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert out["failures"][0]["reason"].startswith("ScenarioTimeout")


class TestOtherSubcommandsInProcess:
    def test_complexity_ok_path(self, capsys):
        fixture = REPO / "tests" / "fixtures" / "complex.gd"
        args = _args("complexity", "--warn", "10", "--max", "15", str(fixture))
        rc = cli_mod.cmd_complexity(args)
        assert rc == 0
        assert json.loads(capsys.readouterr().out)["ok"] is True

    def test_emit_returns_one_on_failure(self, capsys):
        fixture = REPO / "tests" / "fixtures" / "complex.gd"
        args = _args("complexity", "--max", "1", str(fixture))
        rc = cli_mod.cmd_complexity(args)
        assert rc == 1
        assert json.loads(capsys.readouterr().out)["ok"] is False

    def test_mutation_missing_file_reports_error_in_contract(self, capsys):
        args = _args("mutation", "/nonexistent/x.gd", "--project-root", "/tmp")
        rc = cli_mod.cmd_mutation(args)
        out = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert out["ok"] is False

    def test_coverage_missing_file_reports_error_in_contract(self, capsys):
        args = _args("coverage", "/nonexistent/x.gd", "--project-root", "/tmp")
        rc = cli_mod.cmd_coverage(args)
        out = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert out["ok"] is False

    def test_main_dispatches_and_returns_exit_code(self, capsys):
        fixture = REPO / "tests" / "fixtures" / "complex.gd"
        assert cli_mod.main(["complexity", "--warn", "10", "--max", "15", str(fixture)]) == 0
        assert cli_mod.main(["complexity", "--max", "1", str(fixture)]) == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
