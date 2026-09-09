"""Tests: unparseable files are structural failures, not crashes (SEE-1268)."""

from pathlib import Path

from godot_qa_toolkit.complexity.gate import run_gate


def test_unparseable_file_counts_as_failure(tmp_path):
    # `def` is a Python keyword — gd2py cannot convert this file.
    bad = tmp_path / "bad.gd"
    bad.write_text("extends Node\n\nfunc f() -> void:\n\tvar def := 1\n\tprint(def)\n")
    good = tmp_path / "good.gd"
    good.write_text("extends Node\n\nfunc g() -> int:\n\treturn 1\n")

    result = run_gate([str(tmp_path)])
    assert result["ok"] is False
    assert result["summary"]["unparseable"] == 1
    assert result["summary"]["functions"] == 1  # good.gd still measured
    assert any(f["file"] == str(bad) for f in result["failures"])
