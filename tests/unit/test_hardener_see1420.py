"""SEE-1420 hardener 硬化——§SPEC-1420-05/06/08/10 对抗性边界复核
（hardener 子步骤：以"默认代码是错误的"预设找反例，不找确认证据）。

覆盖面（引用编号，命令判据见 issue description §SPEC-1420-05/06/08/10）：
- §05：resume v2 信封校验（file_key/schema/非 dict）、告警一次性、路径约束、
  tmp 无残渣、写失败告警一次、重复 id 确定性；
- §06：矩阵补格——suspect/invalid_mutant 复用档、run_error/unknown 重跑档、
  判定序（header > scope > 全签名 > verdict）、budget<=0 零执行、untouched
  区间合并/切分；
- §08：毒 run 守卫精确边界（5/10 毒、4/8 非毒）、空窗口、pinned×毒窗口、
  缺 streak 字段降级、survived 清 streak、毒守卫按窗口口径；
- §10：per-mutant 并发 worker 分片写竞态（manifest 索引层 + per-id 条目
  多线程并发 + 多 store 实例交错）后数据完整一致；pinned prior 下差分 vs
  --mutate-all verdict 一致；
- §04/D3：稳定 id 属性反例——注释/空行零位移 id 但全签名显式降级、新语句
  仅位移所在 scope、同名消歧漂移绝不陈旧复用、header 编辑不动 scope hash。
"""

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import runner as mrunner
from godot_qa_toolkit.mutation.diffengine import (
    apply_timeout_streaks,
    budget_on_retry,
    plan_diff,
    prior_timeout_streak,
    streak_for,
    untouched_ranges,
)
from godot_qa_toolkit.mutation.ops import Mutant, _collect_mutations
from godot_qa_toolkit.mutation.resume2 import SCHEMA_V2, ResumeStoreV2
from godot_qa_toolkit.mutation.scopemanifest import build_manifest

TARGET_SRC = ("func add(a, b):\n"
              "\treturn a + b\n")

TWO_FUNCS = ("extends Node\n"
             "\n"
             "const MAX = 3\n"
             "\n"
             "func add(a, b):\n"
             "\treturn a + b\n"
             "\n"
             "func sub(a, b):\n"
             "\treturn a - b\n")

MANIFEST = {"header_hash": "H", "scopes": {"func:add": "S1"}}
PRIOR = {"verdict": "killed", "reason": "r", "scope_hash": "S1",
         "line": 2, "column": 11, "kind": "AOR", "original": "+",
         "mutated": "-", "timeout_streak": 0}


def _write_project(tmp_path, src=TARGET_SRC):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    target = proj / "calc.gd"
    target.write_text(src)
    return proj, target


def _entry(i=1, verdict="killed"):
    return {"id": f"calc.gd#add#{i}", "scope_id": "add", "scope_hash": "S",
            "line": 2, "column": 11, "kind": "AOR", "original": "+",
            "mutated": "-", "verdict": verdict, "reason": "r",
            "timeout_streak": 0}


def _mutants(src=TWO_FUNCS, label="calc.gd"):
    _, _, tagged, _ = build_manifest(src, label,
                                     _collect_mutations(src, label))
    return tagged


def _state(src, label="calc.gd"):
    """完整差分状态：manifest 文档 + 全 killed 条目 + 位点 + scope 表。"""
    header, _, tagged, site_scopes = build_manifest(
        src, label, _collect_mutations(src, label))
    entries = {}
    for m in tagged:
        entries[m.stable_id] = {
            "id": m.stable_id, "scope_id": m.scope_id,
            "scope_hash": site_scopes.get(m.scope_id),
            "line": m.line, "column": m.column, "kind": m.kind,
            "original": m.original, "mutated": m.mutated,
            "verdict": "killed", "reason": "seeded", "timeout_streak": 0,
        }
    manifest = {"schema": SCHEMA_V2, "header_hash": header,
                "scopes": dict(site_scopes)}
    return manifest, entries, tagged, site_scopes, header


# ============================================================================
# §05 resume v2 —— 信封校验 / 告警 / 路径 / 原子写边界
# ============================================================================

class TestResumeV2EnvelopeAdversarial:
    def test_wrong_file_key_manifest_warns_and_degrades(self, tmp_path,
                                                        capsys):
        proj, target = _write_project(tmp_path)
        store = ResumeStoreV2(str(proj), str(target), "res://tests/")
        store.ensure_store_dir()
        store.manifest_path.write_text(
            json.dumps({"schema": SCHEMA_V2, "file_key": "forged"}),
            encoding="utf-8")
        assert store.load_manifest() is None
        assert "envelope mismatch" in capsys.readouterr().err

    def test_non_dict_manifest_degrades_with_warning(self, tmp_path, capsys):
        proj, target = _write_project(tmp_path)
        store = ResumeStoreV2(str(proj), str(target), "res://tests/")
        store.ensure_store_dir()
        store.manifest_path.write_text("[]", encoding="utf-8")
        assert store.load_manifest() is None
        assert "not an object" in capsys.readouterr().err

    def test_v1_schema_entry_rejected_with_drift_warning(self, tmp_path,
                                                         capsys):
        """v1 schema 条目混入 v2 目录（schema bump 残留）= 信封漂移。"""
        proj, target = _write_project(tmp_path)
        store = ResumeStoreV2(str(proj), str(target), "res://tests/")
        store.ensure_store_dir()
        p = store.entry_path("calc.gd#add#1")
        p.write_text(json.dumps({"schema": "see1400-resume/1",
                                 "file_key": store.key, **_entry()}),
                     encoding="utf-8")
        assert store.load_entries() == {}
        err = capsys.readouterr().err
        assert "1 envelope-drift and 0 poison entries rejected" in err

    def test_drift_and_poison_counts_disclosed_together(self, tmp_path,
                                                        capsys):
        proj, target = _write_project(tmp_path)
        store = ResumeStoreV2(str(proj), str(target), "res://tests/")
        store.ensure_store_dir()
        drift = {"schema": SCHEMA_V2, "file_key": "forged", **_entry()}
        (store.entry_path("calc.gd#add#1")
         .write_text(json.dumps(drift), encoding="utf-8"))
        poison = {"schema": SCHEMA_V2, "file_key": store.key,
                  **_entry(i=2, verdict="run_error"),
                  "reason": "memory injection failed: env missing"}
        (store.entry_path("calc.gd#add#2")
         .write_text(json.dumps(poison), encoding="utf-8"))
        assert store.load_entries() == {}
        err = capsys.readouterr().err
        assert "1 envelope-drift and 1 poison entries rejected" in err

    def test_non_string_id_rejected_as_drift(self, tmp_path, capsys):
        proj, target = _write_project(tmp_path)
        store = ResumeStoreV2(str(proj), str(target), "res://tests/")
        store.ensure_store_dir()
        bad = {"schema": SCHEMA_V2, "file_key": store.key,
               **{**_entry(), "id": 123}}
        (store.entry_path("calc.gd#add#1")
         .write_text(json.dumps(bad), encoding="utf-8"))
        assert store.load_entries() == {}
        assert "envelope-drift" in capsys.readouterr().err

    def test_unreadable_entries_warn_exactly_once(self, tmp_path, capsys):
        proj, target = _write_project(tmp_path)
        store = ResumeStoreV2(str(proj), str(target), "res://tests/")
        store.ensure_store_dir()
        for name in ("a.json", "b.json", "c.json"):
            (store.store_dir / name).write_bytes(b"\x00{corrupted")
        assert store.load_entries() == {}
        err = capsys.readouterr().err
        assert err.count("unreadable entry") == 1

    def test_entry_path_contained_for_adversarial_ids(self, tmp_path):
        """id 含 '/'、'..'、绝对路径成分 → 压平后绝不逃出 store_dir。"""
        proj, target = _write_project(tmp_path)
        store = ResumeStoreV2(str(proj), str(target), "res://tests/")
        for sid in ("a/b#c#1", "../../evil", "/abs/path/x",
                    "..\\..\\evil", "", "."):
            p = store.entry_path(sid)
            assert p.parent == store.store_dir
            assert "/" not in p.name and "\\" not in p.name

    def test_no_tmp_residue_after_record(self, tmp_path):
        proj, target = _write_project(tmp_path)
        store = ResumeStoreV2(str(proj), str(target), "res://tests/")
        for i in (1, 2, 3):
            assert store.record_entry(_entry(i=i))
        assert not list(store.store_dir.glob("*.tmp.*"))

    def test_duplicate_id_last_sorted_file_wins(self, tmp_path):
        """同 id 双文件（外部构造）→ 载入确定性：排序末位文件胜出。"""
        proj, target = _write_project(tmp_path)
        store = ResumeStoreV2(str(proj), str(target), "res://tests/")
        store.ensure_store_dir()
        doc = lambda v: json.dumps({"schema": SCHEMA_V2,  # noqa: E731
                                    "file_key": store.key,
                                    **_entry(verdict=v)})
        (store.entry_path("calc.gd#add#1")
         .write_text(doc("killed"), encoding="utf-8"))
        (store.store_dir / "zz-copy.json").write_text(doc("survived"),
                                                      encoding="utf-8")
        entries = store.load_entries()
        assert list(entries) == ["calc.gd#add#1"]
        assert entries["calc.gd#add#1"]["verdict"] == "survived"

    def test_write_failure_warns_exactly_once(self, tmp_path, capsys):
        proj, target = _write_project(tmp_path)
        (proj / ".qa-cache").write_text("occupied")
        store = ResumeStoreV2(str(proj), str(target), "res://tests/")
        assert store.record_entry(_entry()) is False
        assert store.record_entry(_entry(i=2)) is False
        err = capsys.readouterr().err
        assert err.count("resume v2 store write failed") == 1


# ============================================================================
# §06 差分跳过矩阵 —— 补格与判定序
# ============================================================================

class TestMatrixCompletion:
    def _plan(self, verdict, prior_extra=None):
        mutants = _mutants(TARGET_SRC)
        sid = mutants[0].stable_id
        prior = {sid: {**PRIOR, "verdict": verdict, **(prior_extra or {})}}
        return plan_diff("H", {"func:add": "S1"}, MANIFEST, mutants, prior), sid

    def test_suspect_reused(self):
        plan, sid = self._plan("suspect")
        assert plan.triggers[sid] == "reused:suspect"
        assert sid in plan.reused

    def test_invalid_mutant_reused(self):
        plan, sid = self._plan("invalid_mutant")
        assert plan.triggers[sid] == "reused:invalid_mutant"
        assert sid in plan.reused

    def test_run_error_retried(self):
        """run_error 属基础设施存疑——宁跑不脏（D4 第 3 档）。"""
        plan, sid = self._plan("run_error")
        assert plan.triggers[sid] == "retry:run_error"
        assert sid not in plan.reused

    def test_unknown_verdict_retried(self):
        """未知 verdict（旧 schema 残留）→ 绝不复用，显式披露触发名。"""
        plan, sid = self._plan("weird_legacy")
        assert plan.triggers[sid] == "retry:unknown_verdict:weird_legacy"
        assert sid not in plan.reused


class TestMatrixOrdering:
    """判定序反例：高危档必须压过本可复用的档位。"""

    def test_scope_change_beats_survived_prior(self):
        mutants = _mutants(TARGET_SRC)
        sid = mutants[0].stable_id
        prior = {sid: {**PRIOR, "verdict": "survived"}}
        plan = plan_diff("H", {"func:add": "S2"}, MANIFEST, mutants, prior)
        assert plan.triggers[sid] == "scope_changed"
        assert plan.reused == {}

    def test_header_change_beats_killed_prior(self):
        mutants = _mutants(TARGET_SRC)
        sid = mutants[0].stable_id
        prior = {sid: dict(PRIOR)}
        plan = plan_diff("H2", {"func:add": "S1"}, MANIFEST, mutants, prior)
        assert plan.triggers[sid] == "header_changed"
        assert plan.reused == {}

    def test_signature_drift_beats_survived_prior(self):
        """survived prior + 行漂移：降级重跑而非按 id 复用红档。"""
        mutants = _mutants(TARGET_SRC)
        sid = mutants[0].stable_id
        prior = {sid: {**PRIOR, "verdict": "survived", "line": 99}}
        plan = plan_diff("H", {"func:add": "S1"}, MANIFEST, mutants, prior)
        assert plan.triggers[sid] == "signature_drift"
        assert plan.reused == {}

    def test_scope_absent_from_old_manifest_is_scope_new(self):
        mutants = _mutants(TARGET_SRC)
        sid = mutants[0].stable_id
        plan = plan_diff("H", {"func:add": "S1"},
                         {"header_hash": "H", "scopes": {}},
                         mutants, {sid: dict(PRIOR)})
        assert plan.triggers[sid] == "scope_new"


class TestBudgetAndRanges:
    def test_budget_zero_and_negative_yield_all_untouched(self):
        executed = [SimpleNamespace(line=i) for i in (1, 2, 3)]
        for budget in (0, -1):
            final, untouched = budget_on_retry(executed, budget)
            assert final == []
            assert untouched == executed

    def test_untouched_ranges_merge_and_split(self):
        items = [SimpleNamespace(line=i) for i in (3, 1, 7, 2, 10, 9)]
        assert untouched_ranges(items) == [[1, 3], [7, 7], [9, 10]]
        assert untouched_ranges([]) == []


# ============================================================================
# §08 timeout_pinned —— 毒守卫精确边界与固定组合
# ============================================================================

def _m(i=1):
    return Mutant(file="x.gd", line=2, column=11, operator="+", original="+",
                  mutated="-", kind="AOR", scope_id="add", n=i,
                  stable_id=f"x.gd#func:add#{i}")


def _rec(verdict="timeout"):
    return {"file": "x.gd", "line": 2, "column": 11, "kind": "AOR",
            "operator": "+", "original": "+", "mutated": "-",
            "verdict": verdict, "reason": "GUT timed out", "line_text": "..."}


class TestPoisonGuardBoundaries:
    def _raw(self, n_timeout, n_killed, base=0):
        pairs = [(_m(base + i + 1), _rec("timeout")) for i in range(n_timeout)]
        pairs += [(_m(100 + base + j), _rec("killed"))
                  for j in range(n_killed)]
        return pairs

    def test_exact_half_ratio_5_of_10_is_poison(self):
        """边界精确定义：≥50% 且 ≥5 条——5/10 恰好落毒；毒窗口内 timeout
        不落 streak（无 prior → 保持 0）且无人 pinned。"""
        out = apply_timeout_streaks(self._raw(5, 5), {}, {"func:add": "S"}, 0.0)
        assert out.suspended is True
        assert out.pinned_ids == []
        timeouts = [e for e in out.entries if e["verdict"] == "timeout"]
        killed = [e for e in out.entries if e["verdict"] == "killed"]
        assert len(timeouts) == 5 and len(killed) == 5
        assert all(e["timeout_streak"] == 0 for e in timeouts)

    def test_four_of_eight_not_poison(self):
        """占比过半但条数不足 5 → 非毒（count 门在前）。"""
        out = apply_timeout_streaks(self._raw(4, 4), {}, {"func:add": "S"}, 0.0)
        assert out.suspended is False

    def test_empty_window_no_crash_and_not_poison(self):
        out = apply_timeout_streaks([], {}, {}, 0.0)
        assert out.suspended is False
        assert out.records == [] and out.entries == []
        assert out.pinned_ids == []

    def test_poison_window_keeps_pinned_pinned_with_held_streak(self):
        """毒窗口内 prior pinned 重判 timeout：保留原 streak，仍 pinned
        （毒窗口不提供任何解除证据）。"""
        priors = {"x.gd#func:add#1": {"verdict": "timeout_pinned",
                                 "timeout_streak": 2}}
        # 非毒窗口（1/10）：prior pinned 重判 timeout → streak 增长 2→3
        raw = [(_m(1), _rec("timeout"))]
        raw += [(_m(10 + j), _rec("killed")) for j in range(9)]
        out = apply_timeout_streaks(raw, priors, {"func:add": "S"}, 0.0)
        e1 = next(e for e in out.entries if e["id"] == "x.gd#func:add#1")
        assert (e1["verdict"], e1["timeout_streak"]) == ("timeout_pinned", 3)
        poison_raw = [(_m(1), _rec("timeout"))]
        poison_raw += [(_m(50 + j), _rec("killed")) for j in range(5)]
        poison_raw += [(_m(60 + j), _rec("timeout")) for j in range(4)]
        out2 = apply_timeout_streaks(poison_raw, priors, {"func:add": "S"}, 0.0)
        assert out2.suspended is True
        e1b = next(e for e in out2.entries if e["id"] == "x.gd#func:add#1")
        assert (e1b["verdict"], e1b["timeout_streak"]) == \
            ("timeout_pinned", 2)

    def test_prior_missing_streak_field_degrades_to_fresh_count(self):
        assert streak_for({"verdict": "timeout"}, "timeout") == \
            (1, "timeout")
        assert prior_timeout_streak({"verdict": "timeout"}) == 0

    def test_survived_prior_clears_stale_streak(self):
        prior = {"verdict": "survived", "timeout_streak": 9}
        assert streak_for(prior, "survived") == (0, "survived")
        assert prior_timeout_streak(prior) == 0

    def test_poison_guard_is_per_window(self):
        """毒守卫按调用窗口计（模块 docstring 披露口径）：两半窗口各自
        非毒，合并不毒化历史——钉死契约口径防实现漂移。"""
        wa = self._raw(3, 2)
        wb = self._raw(3, 2, base=200)
        assert apply_timeout_streaks(wa, {}, {"func:add": "S"}, 0.0).suspended \
            is False
        assert apply_timeout_streaks(wb, {}, {"func:add": "S"}, 0.0).suspended \
            is False
        assert apply_timeout_streaks(wa + wb, {}, {"func:add": "S"}, 0.0) \
            .suspended is True


# ============================================================================
# §10 per-mutant 并发 worker 分片写竞态 + pinned prior 一致性
# ============================================================================

class TestConcurrentShardWriteRace:
    def test_threads_records_and_manifest_concurrently(self, tmp_path):
        """多线程分片写 per-id 条目 + manifest 索引层并发重写 + 原子读：
        收尾后全部条目在位、manifest 合法、零 tmp 残渣。"""
        proj, target = _write_project(tmp_path)
        store = ResumeStoreV2(str(proj), str(target), "res://tests/")
        n_threads, per_thread = 8, 12
        errors: list = []
        # barrier 方数 = 全部参与线程（8 worker + manifest 写者 + 原子读者），
        # 主线程不参与——方数错配会让落败线程永久阻塞并拖死解释器退出。
        n_parties = n_threads + 2
        barrier = threading.Barrier(n_parties)
        timeout = 10

        def worker(t):
            try:
                barrier.wait(timeout)
                for i in range(per_thread):
                    ok = store.record_entry(
                        _entry(i=t * per_thread + i + 1))
                    assert ok is True
            except Exception as e:  # noqa: BLE001 — 线程内异常必须上浮
                errors.append(e)

        def manifest_writer():
            try:
                barrier.wait(timeout)
                for _ in range(25):
                    assert store.write_manifest("H", {"func:add": "S"},
                                                "SHA", "calc.gd")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def atomic_reader():
            """读侧只允许两种结果：FileNotFoundError 或完整 JSON——原子
            os.replace 保证读者永不看到半写内容。"""
            try:
                barrier.wait(timeout)
                for _ in range(40):
                    try:
                        raw = store.manifest_path.read_text(encoding="utf-8")
                    except FileNotFoundError:
                        continue
                    json.loads(raw)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,), daemon=True)
                   for t in range(n_threads)]
        threads += [threading.Thread(target=manifest_writer, daemon=True),
                    threading.Thread(target=atomic_reader, daemon=True)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert not any(t.is_alive() for t in threads), \
            "worker thread stuck — barrier/lock misuse"
        assert errors == []
        entries = store.load_entries()
        assert len(entries) == n_threads * per_thread
        assert all(e["verdict"] == "killed" for e in entries.values())
        man = store.load_manifest()
        assert man is not None and man["header_hash"] == "H"
        assert not list(store.store_dir.glob("*.tmp.*"))

    def test_distinct_store_instances_interleaved(self, tmp_path):
        """模拟多 worker 进程：4 个 store 实例（同 key）各写各 id 交错
        落盘 → 收尾全集可见。"""
        proj, target = _write_project(tmp_path)
        stores = [ResumeStoreV2(str(proj), str(target), "res://tests/",
                                binary_kind="linux", now=float(t))
                  for t in range(4)]
        for w, store in enumerate(stores):
            for i in range(6):
                assert store.record_entry(_entry(i=w * 6 + i + 1))
        merged = stores[0].load_entries()
        assert len(merged) == 24
        assert stores[3].load_entries() == merged


class TestDiffVsAllWithPinnedPrior:
    """§10 × §08 组合：pinned prior 在场时差分跑 vs --mutate-all 全量跑
    verdict 完全一致（streak 连续性不被 --mutate-all 重置）。"""

    def test_pinned_consistent_between_diff_and_mutate_all(
            self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path, TWO_FUNCS)

        def gut(root, timeout_s=60, tests_glob=None):
            return (0, "all passed")

        def mk_keyed():
            def keyed(m, *a, **k):
                verdict = "killed" if m.scope_id == "func:add" else "timeout"
                return {"file": m.file, "line": m.line, "column": m.column,
                        "kind": m.kind, "operator": m.operator,
                        "original": m.original, "mutated": m.mutated,
                        "verdict": verdict, "reason": "keyed",
                        "id": m.stable_id}
            return keyed

        monkeypatch.setattr(mrunner, "_run_gut_on_project", gut)
        monkeypatch.setattr(mrunner, "_run_single_mutant", mk_keyed())
        r1 = mrunner.run_mutation(str(target), str(proj), budget=10,
                                  tests_glob="res://tests/")
        assert r1["summary"]["killed"] == 3
        assert r1["summary"]["timeout"] == 3  # streak 1，未毒（3 < 5）
        monkeypatch.setattr(mrunner, "_run_single_mutant", mk_keyed())
        r2 = mrunner.run_mutation(str(target), str(proj), budget=10,
                                  tests_glob="res://tests/")
        assert r2["summary"]["timeout_pinned"] == 3  # streak 2 → pinned
        monkeypatch.setattr(mrunner, "_run_single_mutant", mk_keyed())
        r3 = mrunner.run_mutation(str(target), str(proj), budget=10,
                                  tests_glob="res://tests/",
                                  mutate_all=True)
        fkey = lambda fs: sorted(  # noqa: E731
            (f["id"], f["verdict"]) for f in fs)
        assert fkey(r2["failures"]) == fkey(r3["failures"])
        assert r3["summary"]["timeout_pinned"] == 3
        assert r3["summary"]["killed"] == 3
        assert r2["ok"] is True and r3["ok"] is True


# ============================================================================
# §04/D3 稳定 id —— 属性反例构造
# ============================================================================

class TestStableIdCounterexamples:
    def test_comment_insertion_zero_id_shift_but_explicit_degradation(
            self, capsys):
        """注释行整体位移行号：id 零位移（D3 属性）——但全签名含行号，
        差分必须显式降级重跑，绝不按 id 静默复用陈旧证据。"""
        s1 = _state(TWO_FUNCS)
        s2 = _state("# note\n" + TWO_FUNCS)
        assert [m.stable_id for m in s1[2]] == [m.stable_id for m in s2[2]]
        assert s1[3] == s2[3] and s1[4] == s2[4]
        plan = plan_diff(s2[4], s2[3], s1[0], s2[2], s1[1])
        assert plan.reused == {}
        assert set(plan.triggers.values()) == {"signature_drift"}
        assert len(plan.executed) == len(s2[2])
        capsys.readouterr()  # 噪声吸收：逐位点 stderr 告警属既有契约

    def test_blank_line_insertion_same_property(self):
        s1 = _state(TWO_FUNCS)
        s2 = _state("\n\n" + TWO_FUNCS)
        assert [m.stable_id for m in s1[2]] == [m.stable_id for m in s2[2]]
        plan = plan_diff(s2[4], s2[3], s1[0], s2[2], s1[1])
        assert plan.reused == {}
        assert set(plan.triggers.values()) == {"signature_drift"}

    def test_new_statement_shifts_only_host_scope(self):
        """在 sub 内插入新语句：add 全绿复用；sub 位点集变化且必重跑。"""
        src2 = TWO_FUNCS.replace("return a - b", "return a - b - 1")
        s1 = _state(TWO_FUNCS)
        s2 = _state(src2)
        add1 = sorted(m.stable_id for m in s1[2] if m.scope_id == "func:add")
        add2 = sorted(m.stable_id for m in s2[2] if m.scope_id == "func:add")
        assert add1 == add2
        sub1 = sorted(m.stable_id for m in s1[2] if m.scope_id == "func:sub")
        sub2 = sorted(m.stable_id for m in s2[2] if m.scope_id == "func:sub")
        assert sub1 != sub2  # 新语句改变 sub 的位点集（n 序或签名）
        plan = plan_diff(s2[4], s2[3], s1[0], s2[2], s1[1])
        reused_scopes = {sid.split("#")[1]
                         for sid in plan.reused}
        assert reused_scopes == {"func:add"}
        assert all(plan.triggers[m.stable_id] == "scope_changed"
                   for m in s2[2] if m.scope_id == "func:sub")

    def test_new_func_between_leaves_add_reused_sub_degrades(self):
        """A 与 B 之间插新 func：A 复用；C scope_new；B 行号位移 → 全签名
        显式降级（scope hash 不变但签名漂移）。"""
        src2 = TWO_FUNCS.replace(
            "func sub(a, b):",
            "func mid(a):\n\treturn a * 3\n\nfunc sub(a, b):")
        s1 = _state(TWO_FUNCS)
        s2 = _state(src2)
        plan = plan_diff(s2[4], s2[3], s1[0], s2[2], s1[1])
        for m in s2[2]:
            trigger = plan.triggers[m.stable_id]
            if m.scope_id == "func:add":
                assert trigger == "reused:killed"
            elif m.scope_id == "func:mid":
                assert trigger == "scope_new"
            else:
                assert trigger == "signature_drift", \
                    f"sub site {m.stable_id}: {trigger}"

    def test_identical_text_rename_no_stale_reuse(self):
        """同名同文本函数消歧漂移的最坏组合：改名后存活者的 scope_id/同名
        hash 与死者条目完全同域——只有全签名行号校验能拒复用。"""
        src1 = ("func add(a):\n\treturn a + 1\n\n"
                "func add(a):\n\treturn a + 1\n")
        s1 = _state(src1)
        assert any(m.scope_id == "func:add#2" for m in s1[2])
        src2 = src1.replace("func add(a):\n\treturn a + 1\n\nfunc add",
                            "func plus(a):\n\treturn a + 1\n\nfunc add", 1)
        s2 = _state(src2)
        assert any(m.scope_id == "func:plus" for m in s2[2])
        assert all(m.scope_id != "func:add#2" for m in s2[2])
        plan = plan_diff(s2[4], s2[3], s1[0], s2[2], s1[1])
        assert plan.reused == {}
        for m in s2[2]:
            expected = ("scope_new" if m.scope_id == "func:plus"
                        else "signature_drift")
            assert plan.triggers[m.stable_id] == expected, \
                f"{m.stable_id}: {plan.triggers[m.stable_id]}"

    def test_header_edit_moves_header_only(self):
        """D2 单向保守实证：const 编辑 → header 变、scope hash 与 id 全部
        不动；差分全文件重跑（header_changed）而非 scope 级误判。"""
        s1 = _state(TWO_FUNCS)
        s2 = _state(TWO_FUNCS.replace("const MAX = 3", "const MAX = 5"))
        assert s1[4] != s2[4]
        assert s1[3] == s2[3]
        assert [m.stable_id for m in s1[2]] == \
            [m.stable_id for m in s2[2]]
        plan = plan_diff(s2[4], s2[3], s1[0], s2[2], s1[1])
        assert plan.header_changed is True
        assert set(plan.triggers.values()) == {"header_changed"}
        assert plan.reused == {}
