"""SEE-1589 §SPEC-006（A-M3）：单文件 `--jobs 1` 与单文件 summary 契约一致。

判据：单文件输出恒为 flat（顶层 `tool/ok/summary/failures`），不得因
`--jobs 1` 套用多文件 `results[]` 信封；`jobs` 可从 summary 读到。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import godot_qa_toolkit.cli as cli_mod
from godot_qa_toolkit.cli import build_parser
from godot_qa_toolkit.mutation import executor


def _args(tmp_path, *extra):
    (tmp_path / "calc.gd").write_text("func add(a, b):\n\treturn a + b\n")
    return build_parser().parse_args(
        ["mutation", str(tmp_path / "calc.gd"),
         "--project-root", str(tmp_path), *extra])


def _fake_envelope(jobs=1):
    return {
        "tool": "mutation", "ok": True, "jobs": jobs,
        "stage_timings": {"total": 1.0},
        "results": [{
            "tool": "mutation", "ok": True,
            "summary": {"file": "calc.gd", "mutants": 1, "killed": 1,
                        "jobs": jobs},
            "failures": [],
        }],
    }


def test_jobs_1_single_file_is_flat(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "run_mutation_files",
                        lambda *a, **k: _fake_envelope(1))
    args = _args(tmp_path, "--jobs", "1")
    out = cli_mod._dispatch_mutation(args, "linux")
    assert "results" not in out, f"单文件 --jobs 1 仍嵌套 results[]: {out}"
    assert out["summary"]["jobs"] == 1
    assert set(out) >= {"tool", "ok", "summary", "failures"}


def test_jobs_omitted_single_file_remains_flat(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "run_mutation_files",
                        lambda *a, **k: _fake_envelope(4))
    out = cli_mod._dispatch_mutation(_args(tmp_path), "linux")
    assert "results" not in out
    assert out["summary"]["jobs"] == 4


def test_multi_file_keeps_results_envelope(tmp_path, monkeypatch):
    (tmp_path / "b.gd").write_text("func b():\n\treturn 1\n")
    monkeypatch.setattr(executor, "run_mutation_files",
                        lambda *a, **k: _fake_envelope(2))
    args = build_parser().parse_args(
        ["mutation", str(tmp_path / "calc.gd"), str(tmp_path / "b.gd"),
         "--project-root", str(tmp_path), "--jobs", "1"])
    out = cli_mod._dispatch_mutation(args, "linux")
    assert "results" in out, "多文件仍须保留 results[]"
