"""SEE-1400 gherkin steps — resume store / --resume / 全局闸（进程内 fake GUT）。

fake 判定确定性：call 序号（1-based，baseline=1）偶数 → killed（构造
"* test_x + [Failed]" 运行流），奇数 → survived。resume 重跑 baseline 永远
实测（call 序重起），已完成条目按 seq 跳过后剩余 mutant 的相对序与全量跑
同位——合并报告逐字段可对照。
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
from godot_qa_toolkit.mutation.resume import ResumeStore

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


def _store_for(state: dict) -> ResumeStore:
    return ResumeStore(str(state["root"]), str(state["target"]), GLOB, BUDGET)


def _fail_keys(result: dict) -> list:
    return sorted((f["kind"], f["original"], f["mutated"])
                  for f in result["failures"])


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
        got = _store_for(state).completed_count()
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
        got = _store_for(state).completed_count()
        if got != int(n):
            _fail(f"setup could not produce {n} completed verdicts (got {got})")
        state["fake_fn"] = _alternating_fake(state["calls"])

    @registry.step("the mutation run reruns with resume enabled")
    def when_resume_rerun(_p):
        state["calls"]["n"] = 0  # 只数续跑本次的 GUT 轮次
        state["result"] = _run(state, resume=True)

    @registry.step("exactly {n} mutants are skipped")
    def then_skipped(_p, n: str):
        resumed = state["result"]["summary"].get("resumed")
        if resumed != int(n):
            _fail(f"resumed count mismatch: expected {n}, got {resumed}",
                  detail=str(state["result"]["summary"]))

    @registry.step("the merged report is isomorphic to a full run")
    def then_isomorphic(_p):
        # 参照：同源码同 fake 在全新项目里全量跑一遍
        calls_full = {"n": 0}
        root2, target2 = _mk_project()
        orig = mrunner._run_gut_on_project
        mrunner._run_gut_on_project = _alternating_fake(calls_full)
        try:
            full = mrunner.run_mutation(str(target2), str(root2),
                                        budget=BUDGET, timeout_s=60,
                                        tests_glob=GLOB)
        finally:
            mrunner._run_gut_on_project = orig
        resumed = state["result"]
        for key in ("mutants", "killed", "survived", "timeout", "run_errors",
                    "invalid_mutants", "kill_rate", "ok"):
            if resumed["summary"].get(key) != full["summary"].get(key):
                _fail(f"merged report diverges on {key}",
                      detail=f"resumed={resumed['summary'].get(key)} "
                             f"full={full['summary'].get(key)}")
        if _fail_keys(resumed) != _fail_keys(full):
            _fail("failures identity mismatch",
                  detail=f"resumed={_fail_keys(resumed)} full={_fail_keys(full)}")

    @registry.step("a resume store holding every mutant verdict for a 4-mutant target")
    def given_store_full(_p):
        given_store_with_n(_p, "4")
        # store_with_n 的 fake 会在第 6 次调用（baseline+4 后）才死——全量落盘
        # 已在 setup 完成，这里换成不死 fake 以便续跑完整走 skip 分支。
        state["fake_fn"] = _alternating_fake(state["calls"])

    @registry.step("every reported failure detail holds exactly the seven verdict fields")
    def then_no_envelope(_p):
        SEVEN = {"file", "line", "kind", "original", "mutated", "verdict",
                 "reason"}
        failures = state["result"]["failures"]
        if not failures:
            _fail("no failure details reported — scenario is void")
        for f in failures:
            if set(f) != SEVEN:
                _fail("failure detail carries envelope or missing fields",
                      detail=f"got {sorted(f)}, expected {sorted(SEVEN)}")

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
        if _store_for(state).completed_count() != 0:
            _fail("corrupted entries still read as valid")

    @registry.step("no mutant is skipped and every mutant runs fresh")
    def then_none_skipped(_p):
        result = state["result"]
        if result["summary"].get("resumed") != 0:
            _fail("expected zero resumed mutants",
                  detail=str(result["summary"]))
        # baseline + 全部 mutant 都真实跑了 GUT
        if state["calls"]["n"] != 1 + BUDGET:
            _fail(f"expected {1 + BUDGET} fresh GUT runs, "
                  f"got {state['calls']['n']}")

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
