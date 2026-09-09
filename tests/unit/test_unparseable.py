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


def test_lark_parse_error_captured_not_crashed(tmp_path):
    # `func hit(var def: int)` makes gd2py's lark parser throw an
    # UnexpectedToken — the FAIL-1 regression: it must surface as a JSON
    # structural failure with path:line, never a raw traceback crash.
    bad = tmp_path / "hit.gd"
    bad.write_text("extends Node\n\nfunc hit(var def: int) -> void:\n\tprint(def)\n")

    result = run_gate([str(tmp_path)])
    assert result["ok"] is False
    assert result["summary"]["unparseable"] == 1
    assert result["summary"]["functions"] == 0
    f = result["failures"][0]
    assert f["file"] == str(bad)
    assert "gd2py conversion failed" in f["reason"]
    assert "Unexpected" in f["reason"] or "LarkError" in f["reason"]
