"""SEE-1400 gherkin steps — resume store / --resume / 全局闸（进程内 fake GUT）。

fake 判定确定性：call 序号（1-based，baseline=1）偶数 → killed（构造
"* test_x + [Failed]" 运行流），奇数 → survived。resume 重跑 baseline 永远
实测（call 序重起），已完成条目按 seq 跳过后剩余 mutant 的相对序与全量跑
同位——合并报告逐字段可对照。

SEE-1420 hardener 续派：store harness 迁移至 resume v2（ResumeStoreV2，
per-file manifest + per-id 条目；定案 §三 Phase 1 schema bump）——v1 生产
路径不读不写不删（红线），本文件只改测试侧 harness 与期望。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from godot_qa_toolkit.gherkin.runner import Registry, StepFailure
from godot_qa_toolkit.mutation import godot_gate
from godot_qa_toolkit.mutation import runner as mrunner
from godot_qa_toolkit.mutation.resume2 import ResumeStoreV2

TARGET_SRC = (
    "func add(a, b):\n"
    "\tif a < b:\n"
    "\t\treturn a + b\n"
    "\treturn b\n"
)
GLOB = "res://tests/"
BUDGET = 4


def _fail(reason: str, detail: str | None = None) -> None:
    raise StepFailure(reason=reason, detail=detail)


def _mk_project(src: str = TARGET_SRC) -> tuple[Path, Path]:
    root = Path(tempfile.mkdtemp(prefix="see1400-"))
    (root / "addons" / "gut").mkdir(parents=True)
    (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
    target = root / "calc.gd"
    target.write_text(src, encoding="utf-8")
    return root, target


def _alternating_fake(calls: dict):
    def fake(root, timeout_s=60, tests_glob=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return (0, "baseline ok")
        if calls["n"] % 2 == 0:
            return (1, "* test_x\n  [Failed] boom")
        return (0, "all passed")

    return fake


def _dying_fake(calls: dict, die_at: int):
    base = _alternating_fake(calls)

    def fake(root, timeout_s=60, tests_glob=None):
        if calls["n"] + 1 == die_at:
            calls["n"] += 1
            raise KeyboardInterrupt("simulated operator cancel")
        return base(root, timeout_s, tests_glob)

    return fake


def _run(state: dict, resume: bool) -> dict:
    orig = mrunner._run_gut_on_project
    mrunner._run_gut_on_project = state["fake_fn"]
    try:
        return mrunner.run_mutation(
            str(state["target"]), str(state["root"]), budget=BUDGET,
            timeout_s=60, tests_glob=GLOB, resume=resume)
    finally:
        mrunner._run_gut_on_project = orig


def _store_for(state: dict) -> ResumeStoreV2:
    return ResumeStoreV2(str(state["root"]), str(state["target"]), GLOB)


def _completed_count(state: dict) -> int:
    return len(_store_for(state).load_entries())


def register(registry: Registry) -> None:
    state: dict[str, object] = {}

    # ------------------------------------------------------------------
    # resume store 场景
    # ------------------------------------------------------------------

    @registry.step("a mutation target with a resumable fake GUT that dies after the second mutant")
    def given_dying_target(_p):
        root, target = _mk_project()
        calls = {"n": 0}
        state.update(root=root, target=target, calls=calls,
                     fake_fn=_dying_fake(calls, die_at=4))

    @registry.step("the mutation run is interrupted mid-loop")
    def when_interrupted(_p):
        try:
            _run(state, resume=False)
        except KeyboardInterrupt:
            state["interrupted"] = True
        if not state.get("interrupted"):
            _fail("run did not die mid-loop — interrupt scenario is void")

    @registry.step("the resume store holds exactly {n} completed verdicts")
    def then_store_count(_p, n: str):
        got = _completed_count(state)
        if got != int(n):
            _fail(f"store verdict count mismatch: expected {n}, got {got}")

    @registry.step("a resume store holding {n} completed verdicts for a 4-mutant target")
    def given_store_with_n(_p, n: str):
        root, target = _mk_project()
        calls = {"n": 0}
        state.update(root=root, target=target, calls=calls,
                     fake_fn=_dying_fake(calls, die_at=1 + int(n) + 1))
        try:
            _run(state, resume=False)
        except KeyboardInterrupt:
            pass
        got = _completed_count(state)
        if got != int(n):
            _fail(f"setup could not produce {n} completed verdicts (got {got})")
        state["fake_fn"] = _alternating_fake(state["calls"])

    @registry.step("the mutation run reruns with resume enabled")
    def when_resume_rerun(_p):
        state["calls"]["n"] = 0  # 只数续跑本次的 GUT 轮次
        state["result"] = _run(state, resume=True)

    @registry.step("exactly one mutant is skipped")
    def then_one_skipped(_p):
        """D4 差分语义：killed 复用（skip），survived 永远重跑（红线）。"""
        resumed = state["result"]["summary"].get("resumed")
        if resumed != 1:
            _fail(f"resumed count mismatch: expected 1, got {resumed}",
                  detail=str(state["result"]["summary"]))

    @registry.step("exactly {n} mutants are skipped")
    def then_skipped(_p, n: str):
        resumed = state["result"]["summary"].get("resumed")
        if resumed != int(n):
            _fail(f"resumed count mismatch: expected {n}, got {resumed}",
                  detail=str(state["result"]["summary"]))

    @registry.step("the merged report is isomorphic to a full run")
    def then_isomorphic(_p):
        # 参照：同源码同 fake 在全新项目里全量跑一遍。SEE-1420 差分语义下
        # fake 判定必须按 mutant 身份（keyed oracle）而非 call 序——差分
        # 重跑子集的 call 相对序与全量跑天然异位（v1 同款结论失效）。
        calls_full = {"n": 0}
        root2, target2 = _mk_project()

        # key 表：与首跑 setup 的 fake 判定同源——fake call 1=baseline，
        # call k 判定位点 k-1（偶 call 杀）→ 位点 1/3/5 killed。keyed
        # oracle 对差分重跑子集天然同位（同位点同 verdict）。
        from godot_qa_toolkit.mutation.ops import _collect_mutations
        killed_keys = set()
        for seq, mm in enumerate(_collect_mutations(target2.read_text(
                encoding="utf-8"), str(target2)), start=1):
            if seq % 2 == 1:
                killed_keys.add((mm.line, mm.kind, mm.original, mm.mutated))
        calls_full["key"] = killed_keys

        def keyed(m, *a, **k):
            verdict = "killed" if (m.line, m.kind, m.original, m.mutated) \
                in killed_keys else "survived"
            return {"file": m.file, "line": m.line, "column": m.column,
                    "kind": m.kind, "operator": m.operator,
                    "original": m.original, "mutated": m.mutated,
                    "verdict": verdict, "reason": "keyed",
                    "id": m.stable_id}

        def gut(root, timeout_s=60, tests_glob=None):
            calls_full["n"] += 1
            return (0, "all passed")

        orig_gut = mrunner._run_gut_on_project
        orig_sm = mrunner._run_single_mutant
        mrunner._run_gut_on_project = gut
        mrunner._run_single_mutant = keyed
        try:
            full = mrunner.run_mutation(str(target2), str(root2),
                                        budget=BUDGET, timeout_s=60,
                                        tests_glob=GLOB)
        finally:
            mrunner._run_gut_on_project = orig_gut
            mrunner._run_single_mutant = orig_sm
        resumed = state["result"]
        # SEE-1420 D4+D6 同构判据修订：差分跑执行面 = retry 面（budget
        # 封顶）+ 复用面，与全量跑的 budget 窗口位点集天然不同——v1 的
        # "计数/明细逐项相等"判据失效。诚实判据：
        # (a) 两边都覆盖的位点 verdict 完全一致（重跑绝不翻转历史判定）；
        # (b) 复用面条目 verdict 与 keyed oracle 同位（kill 证据不丢）；
        # (c) ok 门禁结论一致。
        resumed_by_key = {(f["line"], f["kind"], f["original"],
                           f["mutated"]): f["verdict"]
                          for f in resumed["failures"]}
        full_by_key = {(f["line"], f["kind"], f["original"],
                        f["mutated"]): f["verdict"]
                       for f in full["failures"]}
        for k in sorted(set(resumed_by_key) & set(full_by_key)):
            if resumed_by_key[k] != full_by_key[k]:
                _fail(f"merged report flips verdict on site {k}",
                      detail=f"resumed={resumed_by_key[k]} "
                             f"full={full_by_key[k]}")
        entries = _store_for(state).load_entries()
        for sid in (resumed["summary"].get("diff_skipped") or []):
            entry = entries.get(sid)
            if entry is None:
                _fail(f"reused id {sid} missing from store",
                      detail=str(sorted(entries)))
            oracle = "killed" if (entry["line"], entry["kind"],
                                  entry["original"],
                                  entry["mutated"]) in killed_keys \
                else "survived"
            if entry["verdict"] != oracle:
                _fail(f"reused verdict diverges from oracle on {sid}",
                      detail=f"stored={entry['verdict']} oracle={oracle}")
        if resumed["ok"] != full["ok"]:
            _fail("merged report diverges on ok gate",
                  detail=f"resumed={resumed['ok']} full={full['ok']}")

    @registry.step("a resume store holding every mutant verdict for a 4-mutant target")
    def given_store_full(_p):
        given_store_with_n(_p, "4")
        # store_with_n 的 fake 会在第 6 次调用（baseline+4 后）才死——全量落盘
        # 已在 setup 完成，这里换成不死 fake 以便续跑完整走 skip 分支。
        state["fake_fn"] = _alternating_fake(state["calls"])

    @registry.step("every reported failure detail holds exactly the ten verdict fields")
    def then_no_envelope(_p):
        # 契约 §4.3 现行字段面（SEE-1416 扩列 + SEE-1420 §04 id 列）。
        # store 信封字段（schema/file_key/recorded_at/scope_hash/timeout_streak
        # /executed_at）绝不进报告。
        TEN = {"file", "line", "column", "kind", "operator", "original",
               "mutated", "verdict", "reason", "id", "line_text"}
        failures = state["result"]["failures"]
        if not failures:
            _fail("no failure details reported — scenario is void")
        for f in failures:
            if set(f) != TEN:
                _fail("failure detail carries envelope or missing fields",
                      detail=f"got {sorted(f)}, expected {sorted(TEN)}")

    @registry.step("the target source is touched and the run reruns with resume enabled")
    def when_touched_resume(_p):
        state["target"].write_text(TARGET_SRC + "\n# touched\n",
                                   encoding="utf-8")
        state["calls"]["n"] = 0
        state["result"] = _run(state, resume=True)

    @registry.step("a mutation target with no prior store")
    def given_no_store(_p):
        root, target = _mk_project()
        calls = {"n": 0}
        state.update(root=root, target=target, calls=calls,
                     fake_fn=_alternating_fake(calls))

    @registry.step("a resume store whose entries are corrupted")
    def given_corrupt_store(_p):
        given_store_with_n(_p, "2")
        for p in sorted(_store_for(state).store_dir.glob("*.json")):
            p.write_bytes(b"\x00{corrupted")
        if _completed_count(state) != 0:
            _fail("corrupted entries still read as valid")

    @registry.step("the touched rerun never skips a survived site and never executes a skipped site")
    def then_touched_semantics(_p):
        """SEE-1420 D2/D4 差分语义下的 touched 面：尾注注释不触 header/
        scope/signature 任一成分 → killed 复用合法（D2 注释零触发的正向
        性质）；survived 位点按 D4 红线永远重跑——resumed 恰为复用的
        killed 数（本 setup = 1），且复用位点零执行（executed < 全量）。"""
        result = state["result"]
        s = result["summary"]
        if s.get("resumed") != 1:
            _fail("comment-only touch must keep the killed verdict reused "
                  "(D2: comment edits never trigger rerun)",
                  detail=str(s))
        if s.get("executed", 0) >= BUDGET + 1:
            _fail("reused site must not be re-executed", detail=str(s))
        skipped = s.get("diff_skipped") or []
        if len(skipped) != 1:
            _fail("expected exactly one diff_skipped entry (the killed site)",
                  detail=str(s))

    @registry.step("no mutant is skipped and every mutant runs fresh")
    def then_none_skipped(_p):
        """无 store / store 损坏 → 差分无历史：全部位点重跑（v2 全量）。"""
        result = state["result"]
        if result["summary"].get("resumed", 0) != 0:
            _fail("expected zero resumed mutants",
                  detail=str(result["summary"]))
        if result["summary"].get("executed", 0) != BUDGET:
            _fail(f"expected all {BUDGET} mutants executed",
                  detail=str(result["summary"]))

    # ------------------------------------------------------------------
    # 全局闸场景
    # ------------------------------------------------------------------

    @registry.step("the global slot capacity is pinned to {n}")
    def given_capacity(_p, n: str):
        root, _ = _mk_project()
        state["root"] = root
        state["saved_env"] = os.environ.get(godot_gate.ENV_GLOBAL_SLOTS)
        os.environ[godot_gate.ENV_GLOBAL_SLOTS] = str(n)

    @registry.step("two concurrent workers run GUT through the gated exit")
    def when_two_workers(_p):
        concurrent = {"cur": 0, "max": 0}
        lock = threading.Lock()

        class FakeProc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        def fake_run(*a, **kw):
            with lock:
                concurrent["cur"] += 1
                concurrent["max"] = max(concurrent["max"], concurrent["cur"])
            time.sleep(0.1)
            with lock:
                concurrent["cur"] -= 1
            return FakeProc()

        orig_run = subprocess.run
        subprocess.run = fake_run
        errors: list = []

        def worker():
            try:
                mrunner._run_gut_on_project(str(state["root"]))
            except Exception as e:  # noqa: BLE001 - 线程内异常必须回收成判据
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            subprocess.run = orig_run
            if state.get("saved_env") is None:
                os.environ.pop(godot_gate.ENV_GLOBAL_SLOTS, None)
            else:
                os.environ[godot_gate.ENV_GLOBAL_SLOTS] = state["saved_env"]
        state["concurrent"] = concurrent
        state["errors"] = errors

    @registry.step("the observed concurrent godot subprocess count never exceeded {n}")
    def then_max_concurrent(_p, n: str):
        if state["errors"]:
            _fail(f"gated workers raised: {state['errors']}")
        if state["concurrent"]["max"] > int(n):
            _fail(f"concurrency exceeded {n}: "
                  f"max={state['concurrent']['max']}")
        if state["concurrent"]["max"] < 1:
            _fail("no gated run observed — scenario is void")

    @registry.step("the global slot capacity env is set to {n}")
    def given_env_capacity(_p, n: str):
        state["saved_env"] = os.environ.get(godot_gate.ENV_GLOBAL_SLOTS)
        os.environ[godot_gate.ENV_GLOBAL_SLOTS] = str(n)

    @registry.step("the gate resolves its capacity")
    def when_resolve_capacity(_p):
        state["capacity"] = godot_gate.slot_capacity()
        if state.get("saved_env") is None:
            os.environ.pop(godot_gate.ENV_GLOBAL_SLOTS, None)
        else:
            os.environ[godot_gate.ENV_GLOBAL_SLOTS] = state["saved_env"]

    @registry.step("the resolved capacity is {n}")
    def then_capacity(_p, n: str):
        if state["capacity"] != int(n):
            _fail(f"capacity mismatch: expected {n}, got {state['capacity']}")

    @registry.step("the platform has no fcntl module")
    def given_no_fcntl(_p):
        root, _ = _mk_project()
        state["root"] = root
        state["saved_fcntl"] = godot_gate.fcntl
        godot_gate.fcntl = None
        godot_gate.reset_warn_state()

    @registry.step("a gated GUT run executes")
    def when_gated_run(_p):
        class FakeProc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        orig_run = subprocess.run
        subprocess.run = lambda *a, **kw: FakeProc()
        try:
            rc, _tail = mrunner._run_gut_on_project(str(state["root"]))
            state["rc"] = rc
        finally:
            subprocess.run = orig_run
            godot_gate.fcntl = state["saved_fcntl"]
            godot_gate.reset_warn_state()

    @registry.step("the run completes without holding any slot")
    def then_no_slot(_p):
        if state["rc"] != 0:
            _fail(f"gated run failed: rc={state['rc']}")
        # fcntl 缺失降级 = 零槽位占用、零槽位文件
        d = godot_gate.slots_dir(str(state["root"]))
        if d.exists() and any(d.glob("slot_*.lock")):
            _fail("degraded gate created slot files it should never touch")

    # ------------------------------------------------------------------
    # §SPEC-012 launch-failure 守卫收窄场景
    # ------------------------------------------------------------------

    @registry.step("a mutation target whose GUT rounds carry unrelated resource warnings")
    def given_noisy_gut(_p):
        # KOL 无头环境既有告警形态（干净 baseline 也在报）——守卫不得吞轮
        noise = ("ERROR: Failed loading resource: "
                 "res://scenes/ui/container/item_container_visual.tscn\n")
        root, target = _mk_project()
        calls = {"n": 0}

        def fake(root, timeout_s=60, tests_glob=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return (0, noise + "* test_x\n  [passed]\n")
            return (1, noise + "* test_new\n  [Failed] boom\n"
                    "---- Run Summary ----\n- test_new\n  [Failed] boom\n")

        state.update(root=root, target=target, calls=calls, fake_fn=fake)

    @registry.step("a mutation target whose GUT rounds fail to launch the runner script")
    def given_launch_fail(_p):
        root, target = _mk_project()
        calls = {"n": 0}

        def fake(root, timeout_s=60, tests_glob=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return (0, "* test_x\n  [passed]\n")
            return (1, "ERROR: Attempt to open script "
                    "'res://addons/gut/gut_cmdln.gd' resulted in error "
                    "'File not found'")

        state.update(root=root, target=target, calls=calls, fake_fn=fake)

    @registry.step("the mutation run executes")
    def when_plain_run(_p):
        state["result"] = _run(state, resume=False)

    @registry.step("every mutant is killed and none is run_error")
    def then_all_killed(_p):
        s = state["result"]["summary"]
        if s.get("run_errors", 0) != 0:
            _fail("noisy rounds swallowed into run_error", detail=str(s))
        if s["mutants"] == 0 or s["killed"] != s["mutants"]:
            _fail("mutants not all killed", detail=str(s))
        if state["result"]["ok"] is not True:
            _fail("run not ok despite all killed", detail=str(s))

    @registry.step("every mutant verdict is run_error and none is killed")
    def then_all_run_error(_p):
        s = state["result"]["summary"]
        if s.get("killed", 0) != 0:
            _fail("launch failure counted as kill", detail=str(s))
        if s["mutants"] == 0 or s.get("run_errors", 0) != s["mutants"]:
            _fail("launch failure not classified run_error", detail=str(s))
        if state["result"]["ok"] is not False:
            _fail("run_error verdict must gate ok=False", detail=str(s))
