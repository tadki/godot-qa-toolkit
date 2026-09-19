"""SEE-1321 §SPEC-016：executor 报告完备性对抗用例（Final Review MEDIUM-1）。

缺陷：`_build_chunks` 对 `_collect_mutations` 抛异常的文件（不可解析 GDScript）
静默 `total = 0` 丢弃——该文件从合并报告完全消失，顶层 `ok` 可为 true，
与单文件路径 `_setup_error_result` 契约行为分叉；全零变异时零 chunk 早退
契约只含 `files[0]` 一个文件的摘要。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import executor
import godot_qa_toolkit.mutation.runner as runner_mod

GD_TARGET = "func add(a, b):\n\treturn a + b\n"
GD_NO_OPS = "extends Node\n\nfunc noop():\n\tpass\n"
GD_UNPARSEABLE = "func broken(:\n\treturn ???\n"


def _write_project(tmp_path, contents):
    proj = tmp_path / "proj"
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    (proj / "tests" / "unit" / "save").mkdir(parents=True)
    (proj / "tests" / "unit" / "save" / "test_save.gd").write_text(
        'preload("res://calc.gd")\n')
    paths = []
    for name, body in contents:
        p = proj / name
        p.write_text(body)
        paths.append(str(p))
    return str(proj), paths


class _SerialPool:
    def __init__(self, max_workers=None):
        self.max_workers = max_workers

    def map(self, fn, iterable):
        return [fn(x) for x in iterable]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _seal(monkeypatch):
    monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
    monkeypatch.setattr(executor, "prewarm_import", lambda root: None)
    monkeypatch.setattr(
        runner_mod, "_run_gut_on_project",
        lambda root, timeout_s=60, tests_glob=None: (0, "pass"))
    monkeypatch.setattr(
        executor, "_run_baseline_snapshot",
        lambda root, g, t: {"rc": 0, "tail": "", "elapsed": 5.0})


class TestSpec016UnparseableFile:
    def test_unparseable_file_produces_error_contract(self, tmp_path, monkeypatch):
        """不可解析文件不得从报告消失——error 契约进 results，顶层 ok=False。"""
        _seal(monkeypatch)
        proj, paths = _write_project(
            tmp_path, [("calc.gd", GD_TARGET), ("broken.gd", GD_UNPARSEABLE)])
        d = executor.run_mutation_files(
            paths, proj, budget=3, timeout_s=60, dry_run=False,
            tests_glob="res://tests/unit/save/", jobs=4)
        assert d["ok"] is False, \
            "unparseable file must surface as failure, not vanish"
        per_file = {r["summary"].get("file") for r in d["results"]}
        assert any(f and f.endswith("broken.gd") for f in per_file), \
            f"broken.gd missing from results: {per_file}"
        broken = [r for r in d["results"]
                  if r["summary"].get("file", "").endswith("broken.gd")][0]
        assert broken["ok"] is False
        assert any("setup failed" in f.get("reason", "") or
                   "error" in f.get("reason", "").lower()
                   for f in broken.get("failures", []))

    def test_collect_exception_not_swallowed_into_zero(self, tmp_path, monkeypatch):
        """_collect_mutations 异常路径不得静默——broken 文件必须留痕（哪怕只
        断言 results 中出现 error 形态）。"""
        _seal(monkeypatch)
        proj, paths = _write_project(
            tmp_path, [("calc.gd", GD_TARGET), ("broken.gd", GD_UNPARSEABLE)])
        d = executor.run_mutation_files(
            paths, proj, budget=3, timeout_s=60, dry_run=False,
            tests_glob="res://tests/unit/save/", jobs=4)
        summary = str(d)
        assert "broken.gd" in summary
        assert d["ok"] is False


class TestSpec016AllZeroMutants:
    def test_all_files_zero_mutants_yields_per_file_records(self, tmp_path, monkeypatch):
        """全部文件零变异：契约必须逐文件出 no_sites 记录（不得只留 files[0]）。"""
        _seal(monkeypatch)
        proj, paths = _write_project(
            tmp_path, [("calc.gd", GD_NO_OPS), ("other.gd", GD_NO_OPS)])
        d = executor.run_mutation_files(
            paths, proj, budget=3, timeout_s=60, dry_run=False,
            tests_glob="res://tests/unit/save/", jobs=4)
        files_in_results = {r["summary"].get("file") for r in d["results"]}
        assert len(d["results"]) == 2, \
            f"expected per-file no_sites records for both files: {files_in_results}"
        assert all(r["summary"].get("mutants") == 0 for r in d["results"])


class TestLow3StemWordBoundary:
    def test_mock_prefixed_stem_not_matched(self, tmp_path):
        """mock_save_manager.gd 的内容不得命中 save_manager（LOW-3 全词匹配）。"""
        from godot_qa_toolkit.mutation.testscan import derive_tests_glob
        tests = tmp_path / "tests"
        d = tests / "unit" / "mocks"
        d.mkdir(parents=True)
        (d / "test_mock_save_manager.gd").write_text(
            "var mock_save_manager = 1\n")
        assert derive_tests_glob("res://scripts/save_manager.gd", tests) is None


class TestLow4WslenvAppend:
    def test_existing_wslenv_preserved(self, tmp_path, monkeypatch):
        from godot_qa_toolkit.mutation.inject import mutant_env
        import os
        monkeypatch.setenv("WSLENV", "USER_FLAG/w")
        env, _cfg = mutant_env("res://calc.gd", "x", str(tmp_path), 0)
        assert "USER_FLAG/w" in env["WSLENV"]
        assert "GQT_MUTATION_CFG/w" in env["WSLENV"]


class TestLow5PrewarmDegrade:
    def test_prewarm_timeout_does_not_break_contract(self, tmp_path, monkeypatch):
        """预热超时不许逃逸——降级告警继续（LOW-5）。"""
        import subprocess as sp
        proj, paths = _write_project(
            tmp_path, [("calc.gd", GD_TARGET)])
        (Path(proj) / "project.godot").write_text("; stub")

        def fake_run(*a, **k):
            raise sp.TimeoutExpired(cmd="godot", timeout=300)

        monkeypatch.setattr(sp, "run", fake_run)
        monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
        monkeypatch.setattr(
            executor, "_run_baseline_snapshot",
            lambda root, g, t: {"rc": 0, "tail": "", "elapsed": 5.0})
        monkeypatch.setattr(
            runner_mod, "_run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None: (0, "pass"))
        d = executor.run_mutation_files(
            paths, proj, budget=2, timeout_s=60, dry_run=False,
            tests_glob="res://tests/unit/save/", jobs=2)
        assert d["tool"] == "mutation"
        assert d["results"]


class TestLow6MultiFileErrorShape:
    def test_missing_files_produce_error_contract_per_file(self, tmp_path, capsys):
        """多文件路径含不存在文件：每个缺失文件以 error 契约进 results、
        顶层 ok=False——不得丢文件也不得破坏统一 JSON 契约（LOW-6+MEDIUM-1
        收敛语义：缺失文件与解析异常同属收集失败）。"""
        from godot_qa_toolkit.cli import main
        rc = main(["mutation", str(tmp_path / "missing1.gd"),
                   str(tmp_path / "missing2.gd"), "--project-root",
                   str(tmp_path)])
        assert rc == 1
        import json as _json
        out = _json.loads(capsys.readouterr().out)
        assert out["ok"] is False
        files_in_results = {r["summary"].get("file") for r in out["results"]}
        assert files_in_results == {
            str(tmp_path / "missing1.gd"), str(tmp_path / "missing2.gd")}
        assert all("error" in r["summary"] for r in out["results"])
