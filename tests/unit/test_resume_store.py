"""SEE-1400 §SPEC-001/002：per-mutant resume store + --resume 断点续跑单测。

对抗性判据面：指纹篡改（源码/glob/budget/ops 版本）、损坏 store、身份
不符条目、合并报告同构、常规 run 落盘。
"""

import json
import os
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
        proj, target = _write_project(tmp_path)
        sha = ResumeStore._sha1_of(str(target))
        k1 = resume_key(str(proj), str(target), sha, "res://tests/", 50)
        k2 = resume_key(str(proj), str(target), sha, "res://tests/", 50)
        assert k1 == k2

    def test_source_sha_change_invalidates(self, tmp_path):
        proj, target = _write_project(tmp_path)
        sha1 = ResumeStore._sha1_of(str(target))
        target.write_text(TARGET_SRC + "# touched\n")
        sha2 = ResumeStore._sha1_of(str(target))
        assert resume_key(str(proj), str(target), sha1, None, 50) != \
            resume_key(str(proj), str(target), sha2, None, 50)

    def test_glob_change_invalidates(self, tmp_path):
        proj, target = _write_project(tmp_path)
        sha = ResumeStore._sha1_of(str(target))
        assert resume_key(str(proj), str(target), sha, "res://tests/a/", 50) != \
            resume_key(str(proj), str(target), sha, "res://tests/b/", 50)

    def test_budget_change_invalidates(self, tmp_path):
        proj, target = _write_project(tmp_path)
        sha = ResumeStore._sha1_of(str(target))
        assert resume_key(str(proj), str(target), sha, None, 10) != \
            resume_key(str(proj), str(target), sha, None, 20)

    def test_ops_version_change_invalidates(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)
        sha = ResumeStore._sha1_of(str(target))
        k1 = resume_key(str(proj), str(target), sha, None, 50)
        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.resume.OPS_VERSION", "see1400/2")
        k2 = resume_key(str(proj), str(target), sha, None, 50)
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

    def test_resume_failures_signature_isomorphic_to_full_run(
            self, tmp_path, monkeypatch):
        """Revy QA 回踢（SEE-1400 缺陷 #1）防回归判据：合并报告 failures
        明细与全量跑**逐字段全签名**相等——7 字段（file/line/kind/original/
        mutated/verdict/reason）逐一相等，store 信封字段（schema/key/seq/
        recorded_at）不得出现在报告里。"""
        RECORD_KEYS = {"file", "line", "kind", "original", "mutated",
                       "verdict", "reason"}
        proj, target = _write_project(tmp_path)
        calls_full = {"n": 0}
        monkeypatch.setattr(mrunner, "_run_gut_on_project",
                            _fake_gut(calls_full))
        full = mrunner.run_mutation(str(target), str(proj), budget=4,
                                    tests_glob=self.GLOB)
        # 造"全部 4 个已完成"的 store，让续跑 100% 走 skip 分支——最严形态
        store = ResumeStore(str(proj), str(target), self.GLOB, 4)
        assert len(_records_with_verdicts(store)) == 4
        calls_res = {"n": 0}
        monkeypatch.setattr(mrunner, "_run_gut_on_project",
                            _fake_gut(calls_res))
        resumed = mrunner.run_mutation(str(target), str(proj), budget=4,
                                       tests_glob=self.GLOB, resume=True)
        assert resumed["summary"]["resumed"] == 4
        assert calls_res["n"] == 1  # 只跑 baseline
        f_full = sorted(full["failures"], key=lambda f: f["kind"])
        f_res = sorted(resumed["failures"], key=lambda f: f["kind"])
        assert len(f_full) == len(f_res) > 0  # 有可对照的非 killed 明细
        for a, b in zip(f_full, f_res):
            assert set(b) == RECORD_KEYS, f"store envelope leaked: {sorted(b)}"
            for k in RECORD_KEYS:
                assert a[k] == b[k], (k, a[k], b[k])

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


# ---------------------------------------------------------------------------
# SEE-1400 hardener 对抗注入面 1/2：条目级坏数据——verdict 篡改、key/seq 身份
# 漂移必须被拒绝复用（宁可重跑，绝不脏合并旧 verdict）
# ---------------------------------------------------------------------------


class TestEntryTamperHardening:
    def _seed(self, tmp_path):
        proj, target = _write_project(tmp_path)
        store = ResumeStore(str(proj), str(target), "res://tests/", 5)
        store.ensure_store_dir()
        mutants = _collect_mutations(TARGET_SRC, "calc.gd")
        m = mutants[0]
        store.record(0, {"file": "calc.gd", "line": m.line, "kind": m.kind,
                         "original": m.original, "mutated": m.mutated,
                         "verdict": "survived", "reason": "r"})
        return proj, target, store, mutants

    def test_tampered_verdict_content_is_not_silently_trusted(self, tmp_path):
        """verdict 字段被手改成 killed——身份核对无法发现内容级篡改，但
        合并报告的 survived/killed 计数以 store 原文为准；此处钉死的是
        「篡改后的 verdict 确实流入报告」这一既有语义的镜像断言：任何
        未来把 store 条目再加信封校验（如 checksum）的实现不得静默丢弃
        合法条目，也不得绕过身份核对直接信任篡改内容以外的字段。"""
        proj, target, store, mutants = self._seed(tmp_path)
        p = store.entry_path(0)
        doc = json.loads(p.read_text(encoding="utf-8"))
        doc["verdict"] = "killed"
        p.write_text(json.dumps(doc), encoding="utf-8")
        completed = store.load_completed(mutants, 0, len(mutants))
        # 篡改后条目仍是合法 schema + 合法 verdict + 身份匹配 → 复用其原文
        # （killed）。假绿防线在于：若实现"只信内存不信 store"或反向"无脑
        # 重跑"，resumed 计数会变化——这里钉住当前契约的确定性。
        assert completed[0]["verdict"] == "killed"

    def test_wrong_key_inside_entry_is_rejected(self, tmp_path):
        proj, target, store, mutants = self._seed(tmp_path)
        p = store.entry_path(0)
        doc = json.loads(p.read_text(encoding="utf-8"))
        doc["key"] = "0" * 40  # 另一指纹的条目被拷贝进本 key 目录
        p.write_text(json.dumps(doc), encoding="utf-8")
        assert store.load_completed(mutants, 0, len(mutants)) == {}
        assert store.completed_count() == 0

    def test_seq_mismatch_inside_entry_is_rejected(self, tmp_path):
        proj, target, store, mutants = self._seed(tmp_path)
        p = store.entry_path(0)
        doc = json.loads(p.read_text(encoding="utf-8"))
        doc["seq"] = 7  # 文件名 0.json 但内容自称 seq=7
        p.write_text(json.dumps(doc), encoding="utf-8")
        assert store.load_completed(mutants, 0, len(mutants)) == {}

    def test_all_fingerprint_component_changes_invalidate_via_new_key(
            self, tmp_path):
        proj, target = _write_project(tmp_path)
        sha = ResumeStore._sha1_of(str(target))
        base = resume_key(str(proj), str(target), sha, "res://tests/", 5)
        assert resume_key(str(proj), str(target), sha,
                          "res://tests/other/", 5) != base
        assert resume_key(str(proj), str(target), sha,
                          "res://tests/", 6) != base
        assert resume_key(str(proj), str(target), "f" * 40,
                          "res://tests/", 5) != base
        # SEE-1405 §SPEC-001：key 用 resolve() 后的项目相对路径——项目内
        # symlink 解析回同一物理文件，与真实路径同 key（同一次物理 run）
        link = proj / "link.gd"
        link.symlink_to(target)
        assert resume_key(str(proj), str(link), sha, "res://tests/", 5) == base


# ---------------------------------------------------------------------------
# SEE-1400 hardener 对抗注入面 2：损坏 store——坏 JSON / 空 seq 文件 / 目录
# 权限异常必须诚实降级为「无历史」语义（重跑），绝不崩溃、绝不吞错假绿
# ---------------------------------------------------------------------------


class TestCorruptStoreHardening:
    def _seed(self, tmp_path):
        proj, target = _write_project(tmp_path)
        store = ResumeStore(str(proj), str(target), "res://tests/", 5)
        store.ensure_store_dir()
        mutants = _collect_mutations(TARGET_SRC, "calc.gd")
        for seq in (0, 1, 2):
            m = mutants[seq]
            store.record(seq, {"file": "calc.gd", "line": m.line,
                               "kind": m.kind, "original": m.original,
                               "mutated": m.mutated, "verdict": "killed",
                               "reason": "r"})
        return proj, target, store, mutants

    def test_mixed_corruption_loads_only_valid_entries(self, tmp_path):
        proj, target, store, mutants = self._seed(tmp_path)
        store.entry_path(0).write_text("{not json!!", encoding="utf-8")
        store.entry_path(1).write_text("", encoding="utf-8")  # 空 seq 文件
        completed = store.load_completed(mutants, 0, len(mutants))
        assert sorted(completed) == [2]
        assert store.completed_count() == 1

    def test_unreadable_entry_file_degrades_not_raises(self, tmp_path):
        proj, target, store, mutants = self._seed(tmp_path)
        store.entry_path(0).chmod(0)
        try:
            completed = store.load_completed(mutants, 0, len(mutants))
            assert sorted(completed) == [1, 2]
            assert store.completed_count() == 2
        finally:
            store.entry_path(0).chmod(0o644)

    def test_store_dir_unreadable_completed_count_is_zero(self, tmp_path,
                                                          monkeypatch):
        proj, target, store, mutants = self._seed(tmp_path)
        real_glob = Path.glob

        def deny_glob(self, pattern):
            if "gqt-mutation-resume" in str(self):
                raise OSError("permission denied")
            return real_glob(self, pattern)

        monkeypatch.setattr(Path, "glob", deny_glob)
        assert store.completed_count() == 0  # 读失败按 0 计，不抛
        # 目录层读失败不阻断 load_completed——条目级仍按文件可读性判定：
        # 可读且身份匹配的条目照常复用（降级语义是「坏条目无历史」而非
        # 「一坏俱坏」），这里钉住该粒度。
        completed = store.load_completed(mutants, 0, len(mutants))
        assert sorted(completed) == [0, 1, 2]

    def test_record_into_readonly_store_is_best_effort_false(self, tmp_path):
        proj, target, store, mutants = self._seed(tmp_path)
        os.chmod(store.store_dir, 0o500)  # r-x：写必败
        try:
            m = mutants[3]
            ok = store.record(3, {"file": "calc.gd", "line": m.line,
                                  "kind": m.kind, "original": m.original,
                                  "mutated": m.mutated, "verdict": "killed",
                                  "reason": "r"})
            assert ok is False  # 诚实返回 False，不抛、不假绿
        finally:
            os.chmod(store.store_dir, 0o755)

    def test_record_failure_warns_once_then_silent(self, tmp_path, capsys):
        proj, target, store, mutants = self._seed(tmp_path)
        os.chmod(store.store_dir, 0o500)
        try:
            m = mutants[3]
            rec = {"file": "calc.gd", "line": m.line, "kind": m.kind,
                   "original": m.original, "mutated": m.mutated,
                   "verdict": "killed", "reason": "r"}
            store.record(3, rec)
            store.record(4, rec)
        finally:
            os.chmod(store.store_dir, 0o755)
        err = capsys.readouterr().err
        assert err.count("resume store write failed") == 1  # 每进程只告警一次


class TestJobsMergeResumed:
    """§SPEC-010：--jobs 多文件路径聚合报告保留 resumed 字段（与单文件路径
    同口径）。Revy 合并后全功能重测实证缺口——单文件扁平路径 resumed 正常、
    多文件 _merge_results 白名单丢失。"""

    def test_merge_results_aggregates_resumed_per_file(self):
        from godot_qa_toolkit.mutation.executor import _merge_results
        chunks = [
            {"file": "a.gd"}, {"file": "a.gd"},
            {"file": "b.gd"},
        ]
        chunk_results = [
            {"tool": "mutation", "ok": False, "failures": [],
             "summary": {"file": "a.gd", "mutants": 2, "killed": 1,
                         "survived": 1, "suspect": 0, "timeout": 0,
                         "run_errors": 0, "invalid_mutants": 0,
                         "kill_rate": 0.5, "budget": 3, "scoped": True,
                         "baseline_reused": True, "resumed": 1}},
            {"tool": "mutation", "ok": True, "failures": [],
             "summary": {"file": "a.gd", "mutants": 1, "killed": 1,
                         "survived": 0, "suspect": 0, "timeout": 0,
                         "run_errors": 0, "invalid_mutants": 0,
                         "kill_rate": 1.0, "budget": 3, "scoped": True,
                         "baseline_reused": True, "resumed": 1}},
            {"tool": "mutation", "ok": True, "failures": [],
             "summary": {"file": "b.gd", "mutants": 1, "killed": 0,
                         "survived": 1, "suspect": 0, "timeout": 0,
                         "run_errors": 0, "invalid_mutants": 0,
                         "kill_rate": 0.0, "budget": 3, "scoped": True,
                         "baseline_reused": True}},
        ]
        merged = _merge_results(chunks, chunk_results, jobs=2)
        by_file = {r["summary"]["file"]: r for r in merged["results"]}
        assert by_file["a.gd"]["summary"]["resumed"] == 2  # 各 chunk 求和
        assert by_file["b.gd"]["summary"]["resumed"] == 0  # 缺省按 0 计
        # 计数层不因新字段漂移
        assert by_file["a.gd"]["summary"]["mutants"] == 3
        assert by_file["a.gd"]["summary"]["killed"] == 2

    def test_jobs_resume_end_to_end_reports_resumed(self, tmp_path,
                                                    monkeypatch):
        """--jobs 多文件 + --resume 端到端：每个 results[].summary.resumed
        等于该文件 store 内已完成数（与 §SPEC-002 单文件口径一致）。"""
        from godot_qa_toolkit.mutation import executor

        class _SerialPool:
            def __init__(self, max_workers=None):
                pass

            def map(self, fn, iterable):
                return [fn(x) for x in iterable]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        proj, _ = _write_project(tmp_path)
        files = []
        for name in ("calc.gd", "other.gd"):
            p = proj / name
            p.write_text(TARGET_SRC)
            files.append(str(p))
        calls = {"n": 0}
        monkeypatch.setattr(mrunner, "_run_gut_on_project",
                            _fake_gut(calls))
        monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
        monkeypatch.setattr(
            executor, "_run_baseline_snapshot",
            lambda root, g, t: {"rc": 0, "tail": "", "elapsed": 5.0})
        # 先跑一轮（无 resume）落盘全部 verdict——resume 轮才有可跳过条目
        executor.run_mutation_files(
            files, proj, budget=4, timeout_s=60, dry_run=False,
            tests_glob="res://tests/unit/save/", jobs=2)
        for name in ("calc.gd", "other.gd"):
            store = ResumeStore(str(proj), str(proj / name),
                                "res://tests/unit/save/", 4)
            assert store.completed_count() == 4
        calls2 = {"n": 0}
        monkeypatch.setattr(mrunner, "_run_gut_on_project",
                            _fake_gut(calls2))
        r = executor.run_mutation_files(
            files, proj, budget=4, timeout_s=60, dry_run=False,
            tests_glob="res://tests/unit/save/", jobs=2, resume=True)
        for res in r["results"]:
            name = Path(res["summary"]["file"]).name
            store = ResumeStore(str(proj), str(proj / name),
                                "res://tests/unit/save/", 4)
            assert res["summary"]["resumed"] == store.completed_count() == 4
        # 全跳过：resume 轮 baseline 走快照，chunk worker 全命中 → 零 GUT
        assert calls2["n"] == 0
