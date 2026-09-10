"""CLI-level tests for mutation + coverage subcommands (SEE-1268 M2)."""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent.parent
SRC = REPO / "src"


def _run_cli(*argv: str) -> tuple[int, dict]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)
    proc = subprocess.run(
        [sys.executable, "-m", "godot_qa_toolkit.cli", *argv],
        capture_output=True,
        text=True,
        cwd=str(SRC),
        env=env,
    )
    start = proc.stdout.index("{")
    return proc.returncode, json.loads(proc.stdout[start:])


class TestMutationCli:
    def test_missing_file_returns_json_failure(self):
        rc, out = _run_cli("mutation", "/nonexistent/x.gd", "--project-root", "/tmp")
        assert rc == 1
        assert out["ok"] is False
        assert "not found" in out["failures"][0]["reason"]

    def test_no_sites_ok(self, tmp_path):
        proj = tmp_path / "proj"
        (proj / "addons" / "gut").mkdir(parents=True)
        (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
        target = proj / "noop.gd"
        target.write_text("extends Node\nfunc f():\n\tpass\n")
        rc, out = _run_cli("mutation", str(target), "--project-root", str(proj))
        assert rc == 0
        assert out["ok"] is True


class TestCoverageCli:
    def test_missing_file_returns_json_failure(self):
        rc, out = _run_cli("coverage", "/nonexistent/x.gd", "--project-root", "/tmp")
        assert rc == 1
        assert out["ok"] is False

    def test_contract_shape(self, tmp_path, monkeypatch):
        # CLI-level contract verified via run_coverage in unit tests; here just
        # check the parser accepts the flags.
        import argparse
        from godot_qa_toolkit.cli import build_parser

        parser = build_parser()
        args = parser.parse_args([
            "coverage", "x.gd", "--project-root", "/p",
            "--min-percent", "75", "--timeout", "90",
        ])
        assert args.command == "coverage"
        assert args.min_percent == 75
        assert args.timeout == 90
