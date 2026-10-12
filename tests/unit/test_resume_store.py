"""SEE-1400 §SPEC-001/002：per-mutant resume store + --resume 断点续跑单测。

对抗性判据面：指纹篡改（源码/glob/budget/ops 版本）、损坏 store、身份
不符条目、合并报告同构、常规 run 落盘。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import runner as mrunner
from godot_qa_toolkit.mutation.ops import OPS_VERSION, _collect_mutations
from godot_qa_toolkit.mutation.resume import (
    FINAL_VERDICTS,
    ResumeStore,
    resume_key,
)
from godot_qa_toolkit.mutation.testscan import derive_tests_glob

# 6 个变异点（ROR < 3 枚 + AOR + 3 枚）——budget=4 必然截断，seq 空间稳定。
TARGET_SRC = (
    "func add(a, b):\n"
    "\tif a < b:\n"
    "\t\treturn a + b\n"
    "\treturn b\n"
)


def _write_project(tmp_path, src=TARGET_SRC):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    target = proj / "calc.gd"
    target.write_text(src)
    return proj, target


def _fake_gut(calls, kill_every=2):
    """确定性 fake：call 序号（1-based，baseline=1）整除 kill_every → killed。

    call 计数在 resume 重跑中从 baseline 重新起算——已完成条目按 seq 跳过
    后，剩余 mutant 的相对 call 序与全量跑同位（baseline 永远实测）。
    """

    def fake(root, timeout_s=60, tests_glob=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return (0, "baseline ok")
        if calls["n"] % kill_every == 0:
            return (1, "test_x [Failed]")
        return (0, "all passed")

    return fake


def _records_with_verdicts(store):
    out = {}
    for p in sorted(store.store_dir.glob("*.json")):
        raw = json.loads(p.read_text(encoding="utf-8"))
        out[int(p.stem)] = raw["verdict"]
    return out


# ---------------------------------------------------------------------------
# 指纹 key（防脏合并）
# ---------------------------------------------------------------------------


class TestResumeKey:
    def test_same_inputs_same_key(self, tmp_path):
        _, target = _write_project(tmp_path)
        sha = ResumeStore._sha1_of(str(target))
        k1 = resume_key(str(target), sha, "res://tests/", 50)
        k2 = resume_key(str(target), sha, "res://tests/", 50)
        assert k1 == k2

    def test_source_sha_change_invalidates(self, tmp_path):
        _, target = _write_project(tmp_path)
        sha1 = ResumeStore._sha1_of(str(target))
        target.write_text(TARGET_SRC + "# touched\n")
        sha2 = ResumeStore._sha1_of(str(target))
        assert resume_key(str(target), sha1, None, 50) != \
            resume_key(str(target), sha2, None, 50)

    def test_glob_change_invalidates(self, tmp_path):
        _, target = _write_project(tmp_path)
        sha = ResumeStore._sha1_of(str(target))
        assert resume_key(str(target), sha, "res://tests/a/", 50) != \
            resume_key(str(target), sha, "res://tests/b/", 50)

    def test_budget_change_invalidates(self, tmp_path):
        _, target = _write_project(tmp_path)
        sha = ResumeStore._sha1_of(str(target))
        assert resume_key(str(target), sha, None, 10) != \
            resume_key(str(target), sha, None, 20)

    def test_ops_version_change_invalidates(self, tmp_path, monkeypatch):
        _, target = _write_project(tmp_path)
        sha = ResumeStore._sha1_of(str(target))
        k1 = resume_key(str(target), sha, None, 50)
        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.resume.OPS_VERSION", "see1400/2")
        k2 = resume_key(str(target), sha, None, 50)
        assert k1 != k2
        assert OPS_VERSION != "see1400/2"  # 常量本体未被污染


# ---------------------------------------------------------------------------
# store 读写与诚实降级
# ---------------------------------------------------------------------------


class TestStoreReadWrite:
    def _store(self, tmp_path, src=TARGET_SRC):
        proj, target = _write_project(tmp_path, src)
        store = ResumeStore(str(proj), str(target), "res://tests/", 50)
        return proj, target, store

    def test_record_and_gitignore(self, tmp_path):
        _, _, store = self._store(tmp_path)
        assert store.record(0, {"verdict": "killed", "reason": "r"}) is True
        assert store.entry_path(0).is_file()
        assert store.completed_count() == 1  # 合法条目计入
        gitignore = store.store_dir / ".gitignore"
        assert gitignore.read_text(encoding="utf-8") == "*\n"

    def test_load_completed_identity_match(self, tmp_path):
        _, _, store = self._store(tmp_path)
        mutants = _collect_mutations(TARGET_SRC, "calc.gd")
        store.record(0, {"file": "calc.gd", "line": mutants[0].line,
                         "kind": mutants[0].kind,
                         "original": mutants[0].original,
                         "mutated": mutants[0].mutated,
                         "verdict": "killed", "reason": "r"})
        done = store.load_completed(mutants, 0, len(mutants))
        assert list(done) == [0]
        assert done[0]["verdict"] == "killed"

    def test_identity_mismatch_not_reused(self, tmp_path):
        _, _, store = self._store(tmp_path)
        mutants = _collect_mutations(TARGET_SRC, "calc.gd")
        store.record(0, {"file": "calc.gd", "line": mutants[0].line,
                         "kind": mutants[0].kind,
                         "original": mutants[0].original,
                         "mutated": "TAMPERED",  # 身份不符
                         "verdict": "killed", "reason": "r"})
        assert store.load_completed(mutants, 0, len(mutants)) == {}

    def test_corrupt_entry_degrades(self, tmp_path):
        _, _, store = self._store(tmp_path)
        store.ensure_store_dir()
        store.entry_path(0).write_text("{not json", encoding="utf-8")
        mutants = _collect_mutations(TARGET_SRC, "calc.gd")
        assert store.load_completed(mutants, 0, len(mutants)) == {}
        assert store.completed_count() == 0

    def test_bad_verdict_and_schema_degrade(self, tmp_path):
        _, _, store = self._store(tmp_path)
        store.ensure_store_dir()
        store.entry_path(0).write_text(
            json.dumps({"schema": "other/1", "verdict": "killed"}),
            encoding="utf-8")
        store.entry_path(1).write_text(
            json.dumps({"schema": "see1400-resume/1", "verdict": "bogus"}),
            encoding="utf-8")
        mutants = _collect_mutations(TARGET_SRC, "calc.gd")
        assert store.load_completed(mutants, 0, len(mutants)) == {}

    def test_slice_bounds_respected(self, tmp_path):
        _, _, store = self._store(tmp_path)
        mutants = _collect_mutations(TARGET_SRC, "calc.gd")
        store.record(1, {"verdict": "killed", "reason": "r",
                         "kind": mutants[1].kind, "line": mutants[1].line,
                         "original": mutants[1].original,
                         "mutated": mutants[1].mutated})
        # [0,1) 不含 seq 1
        assert store.load_completed(mutants, 0, 1) == {}
        assert 1 in store.load_completed(mutants, 1, len(mutants))

    def test_final_verdicts_cover_six_states(self):
        assert FINAL_VERDICTS == frozenset({
            "killed", "survived", "suspect", "timeout", "run_error",
            "invalid_mutant"})


class TestStoreDegradation:
    """诚实降级面：源文件不可读、写盘失败、目录读失败均不抛出。"""

    def test_unreadable_target_yields_stable_key(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        store = ResumeStore(str(proj), str(proj / "missing.gd"), None, 50)
        assert store.source_sha1 == "unreadable"
        assert len(store.key) == 40  # sha1 形态稳定

    def test_record_write_failure_warns_once(self, tmp_path, monkeypatch,
                                             capsys):
        proj, target = _write_project(tmp_path)
        store = ResumeStore(str(proj), str(target), None, 50)
        monkeypatch.setattr(ResumeStore, "ensure_store_dir",
                            lambda self: (_ for _ in ()).throw(
                                OSError("disk full")))
        assert store.record(0, {"verdict": "killed"}) is False
        assert store.record(1, {"verdict": "killed"}) is False
        err = capsys.readouterr().err
        assert err.count("resume store write failed") == 1  # 每实例只告警一次

    def test_completed_count_oserror_is_zero(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)
        store = ResumeStore(str(proj), str(target), None, 50)
        store.record(0, {"verdict": "killed", "reason": "r"})
        monkeypatch.setattr(Path, "glob",
                            lambda self, *a, **k: (_ for _ in ()).throw(
                                OSError("fs gone")))
        assert store.completed_count() == 0

    def test_valid_entry_file_read_failure_is_false(self, tmp_path,
                                                    monkeypatch):
        proj, target = _write_project(tmp_path)
        store = ResumeStore(str(proj), str(target), None, 50)
        store.record(0, {"verdict": "killed", "reason": "r"})
        entry = store.entry_path(0)
        monkeypatch.setattr(Path, "read_text",
                            lambda self, *a, **k: (_ for _ in ()).throw(
                                OSError("vanished")))
        assert store._valid_entry_file(entry) is False

    def test_load_completed_negative_start_clamped(self, tmp_path):
        proj, target = _write_project(tmp_path)
        store = ResumeStore(str(proj), str(target), None, 50)
        mutants = _collect_mutations(TARGET_SRC, "calc.gd")
        assert store.load_completed(mutants, -3, 2) == {}


# ---------------------------------------------------------------------------
# run_mutation 集成：落盘 / 中断 / 续跑合并
# ---------------------------------------------------------------------------


class TestRunMutationResume:
    GLOB = "res://tests/"  # 显式 glob——与 run 内自建 store 的 key 成分对齐

    def test_plain_run_persists_every_verdict(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)
        calls = {"n": 0}
        monkeypatch.setattr(mrunner, "_run_gut_on_project", _fake_gut(calls))
        r = mrunner.run_mutation(str(target), str(proj), budget=3,
                                 tests_glob=self.GLOB)
        assert r["ok"] is False  # 有 survived
        assert "resumed" not in r["summary"]  # 常规 run 不带 resume 字段
        store = ResumeStore(str(proj), str(target), self.GLOB, 3)
        assert len(_records_with_verdicts(store)) == 3

    def test_interrupt_keeps_completed_verdicts(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)
        calls = {"n": 0}

        def fake(root, timeout_s=60, tests_glob=None):
            calls["n"] += 1
            if calls["n"] == 4:  # baseline + 2 mutant 后中断第 3 个
                raise KeyboardInterrupt
            return (0, "all passed")

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake)
        with pytest.raises(KeyboardInterrupt):
            mrunner.run_mutation(str(target), str(proj), budget=4,
                                 tests_glob=self.GLOB)
        store = ResumeStore(str(proj), str(target), self.GLOB, 4)
        assert len(_records_with_verdicts(store)) == 2  # 已完成的 2 个保留可查

    def test_resume_skips_and_merges_isomorphic_report(
            self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)
        # 全量跑的参照 JSON（同 fake 确定性）
        calls_full = {"n": 0}
        monkeypatch.setattr(mrunner, "_run_gut_on_project",
                            _fake_gut(calls_full))
        full = mrunner.run_mutation(str(target), str(proj), budget=4,
                                    tests_glob=self.GLOB)
        # 清掉刚才 plain run 的落盘，重造"中断 2 个后剩 2 个"的局面
        store = ResumeStore(str(proj), str(target), self.GLOB, 4)
        all_verdicts = _records_with_verdicts(store)
        for p in store.store_dir.glob("*.json"):
            p.unlink()
        kept = {}
        for seq, v in all_verdicts.items():
            kept[seq] = v
            if len(kept) == 2:
                break
        for seq, v in kept.items():
            mutants = _collect_mutations(TARGET_SRC, "calc.gd")
            store.record(seq, {
                "file": "calc.gd", "line": mutants[seq].line,
                "kind": mutants[seq].kind, "original": mutants[seq].original,
                "mutated": mutants[seq].mutated, "verdict": v, "reason": "r"})
        # 续跑：同 fake 确定性 → 剩余 mutant 的 verdict 与全量跑一致
        calls_res = {"n": 0}
        monkeypatch.setattr(mrunner, "_run_gut_on_project",
                            _fake_gut(calls_res))
        resumed = mrunner.run_mutation(str(target), str(proj), budget=4,
                                       tests_glob=self.GLOB, resume=True)
        assert resumed["summary"]["resumed"] == 2
        for k in ("mutants", "killed", "survived", "timeout", "run_errors",
                  "invalid_mutants", "kill_rate"):
            assert resumed["summary"][k] == full["summary"][k], k
        assert resumed["ok"] == full["ok"]
        fail_key = lambda fs: sorted((f["kind"], f["original"], f["mutated"])
                                     for f in fs)  # noqa: E731
        assert fail_key(resumed["failures"]) == fail_key(full["failures"])
        # 全量跑 GUT 轮次 = baseline+4；续跑只补剩余
        assert calls_full["n"] == 5
        assert calls_res["n"] == 3

    def test_resume_without_store_equals_plain_run(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)
        calls = {"n": 0}
        monkeypatch.setattr(mrunner, "_run_gut_on_project", _fake_gut(calls))
        r = mrunner.run_mutation(str(target), str(proj), budget=3,
                                 tests_glob=self.GLOB, resume=True)
        assert "resumed" in r["summary"]
        assert r["summary"]["resumed"] == 0
        assert calls["n"] == 4  # baseline + 3，无跳过

    def test_resume_after_source_change_reruns_all(self, tmp_path,
                                                   monkeypatch):
        proj, target = _write_project(tmp_path)
        calls = {"n": 0}
        monkeypatch.setattr(mrunner, "_run_gut_on_project", _fake_gut(calls))
        mrunner.run_mutation(str(target), str(proj), budget=3,
                             tests_glob=self.GLOB)
        # 指纹篡改：源码变化 → 换 key → 旧条目全部作废重跑
        target.write_text(TARGET_SRC + "\n# touched\n")
        calls2 = {"n": 0}
        monkeypatch.setattr(mrunner, "_run_gut_on_project", _fake_gut(calls2))
        r = mrunner.run_mutation(str(target), str(proj), budget=3,
                                 tests_glob=self.GLOB, resume=True)
        assert r["summary"]["resumed"] == 0
        assert calls2["n"] == 4  # 全部重跑


# ---------------------------------------------------------------------------
# auto-scope 与 resume 的协同（§SPEC-005 通道语义不回退）
# ---------------------------------------------------------------------------


def test_auto_scope_single_file_yields_gd_glob(tmp_path):
    tests = tmp_path / "tests" / "unit"
    tests.mkdir(parents=True)
    (tests / "test_calc.gd").write_text(
        'extends RefCounted\nconst C = preload("res://calc.gd")\n')
    glob = derive_tests_glob("res://calc.gd", tmp_path / "tests")
    assert glob is not None and glob.endswith(".gd")
    assert glob == "res://tests/unit/test_calc.gd"


# CLI --resume 参数面注册测试在 test_cli.py（-k cli 判据通道）。
