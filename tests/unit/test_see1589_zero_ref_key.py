"""SEE-1589 §SPEC-009（A-L2）：零引用短路键与文档/实测一致。

判据：零引用短路轮在 `summary.zero_ref_short_circuit` 明确为 true（与
`test_selection.zero_ref` 双披露），串行门与并行 settled 路径同形。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import executor
from godot_qa_toolkit.mutation.executor import run_mutation_files
from godot_qa_toolkit.mutation.testscan import zero_ref_short_circuit_result

GD_TARGET = ("func add(a, b):\n"
             "\tif a > b:\n"
             "\t\treturn a - b\n"
             "\treturn a + b\n")


def test_result_helper_carries_key():
    s = zero_ref_short_circuit_result("/x/calc.gd")["summary"]
    assert s["zero_ref_short_circuit"] is True


def _write_project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "addons" / "gut").mkdir(parents=True, exist_ok=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    (proj / "tests" / "unit").mkdir(parents=True, exist_ok=True)
    (proj / "tests" / "unit" / "test_other.gd").write_text("extends GutTest\n")
    target = proj / "calc.gd"
    target.write_text(GD_TARGET)
    return str(proj), str(target)


def _zero_ref_meta():
    return {"source": "runtime_mapping", "zero_ref": True, "tests": [],
            "tests_glob": None, "derived_glob": None, "mapping": "m.json",
            "reason": "mapping_says_no_tests"}


def test_serial_gate_zero_ref_summary_key(tmp_path, monkeypatch):
    proj, _target = _write_project(tmp_path)
    monkeypatch.setattr(executor, "_auto_scan_tests_glob_meta",
                        lambda *a, **k: (None, _zero_ref_meta()))
    env = run_mutation_files(["calc.gd"], proj, budget=2)
    summary = env["results"][0]["summary"]
    assert summary.get("zero_ref_short_circuit") is True, summary
    assert summary.get("test_selection", {}).get("zero_ref") is True


class _SerialPool:
    def __init__(self, max_workers=None):
        self.max_workers = max_workers

    def map(self, fn, iterable):
        return [fn(x) for x in iterable]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_non_zero_ref_run_has_no_key(tmp_path, monkeypatch):
    import godot_qa_toolkit.mutation.runner as runner_mod
    proj, _target = _write_project(tmp_path)
    monkeypatch.setattr(executor, "_auto_scan_tests_glob_meta",
                        lambda *a, **k: ("res://tests/unit/",
                                         {"source": "runtime_mapping",
                                          "zero_ref": False,
                                          "tests_glob": "res://tests/unit/"}))
    monkeypatch.setattr(runner_mod, "_run_gut_on_project",
                        lambda root, timeout_s=60, tests_glob=None:
                            (1, "* killed\n\t[Failed]\n"))
    monkeypatch.setattr(runner_mod, "_run_baseline",
                        lambda root, glob, timeout_s:
                            (0, "OK. All tests passed", 1.5))
    monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
    monkeypatch.setattr(executor, "_run_baseline_snapshot",
                        lambda *a, **k: {"rc": 0, "tail": "pass",
                                         "elapsed": 5.0})
    env = run_mutation_files(["calc.gd"], proj, budget=2,
                             select_mode="file")
    summary = env["results"][0]["summary"]
    assert "zero_ref_short_circuit" not in summary
    assert summary["mutants"] > 0


def test_no_mutation_sites_with_zero_ref_carries_key(tmp_path, monkeypatch):
    proj, _target = _write_project(tmp_path)
    (Path(proj) / "calc.gd").write_text("")  # 零变异位点
    import godot_qa_toolkit.mutation.runner as runner_mod
    for mod in (executor, runner_mod):
        monkeypatch.setattr(mod, "_auto_scan_tests_glob_meta",
                            lambda *a, **k: (None, _zero_ref_meta()))
    env = run_mutation_files(["calc.gd"], proj, budget=2)
    summary = env["results"][0]["summary"]
    assert summary["mutants"] == 0
    assert summary.get("zero_ref_short_circuit") is True, summary
    assert summary.get("short_circuit_reason") == "no_mutation_sites"
