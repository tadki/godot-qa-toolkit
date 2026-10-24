"""SEE-1400 gherkin steps — 全局闸 + launch-failure 守卫（进程内 fake GUT）。

SEE-1463 §SPEC-003 拆分：v1 resume 生产路径（resume.py）物理删除，其
resume-store 场景随 `see1400_resume.feature` 一并退役——本 steps 文件只保留
仍被 `see1400_global_gate.feature` / `see1400_launch_guard.feature` 消费的
全局闸与 launch-failure 守卫步骤（旧三 feature 共享本文件的形态拆为二）。
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

TARGET_SRC = (
    "func add(a, b):\n"
    "\tif a < b:\n"
    "\t\treturn a + b\n"
    "\treturn b\n"
)


def _fail(reason: str, detail: str | None = None) -> None:
    raise StepFailure(reason=reason, detail=detail)


def _mk_project(src: str = TARGET_SRC) -> tuple[Path, Path]:
    root = Path(tempfile.mkdtemp(prefix="see1400-"))
    (root / "addons" / "gut").mkdir(parents=True)
    (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
    target = root / "calc.gd"
    target.write_text(src, encoding="utf-8")
    return root, target


def _run(state: dict, resume: bool) -> dict:
    orig = mrunner._run_gut_on_project
    mrunner._run_gut_on_project = state["fake_fn"]
    try:
        return mrunner.run_mutation(
            str(state["target"]), str(state["root"]), budget=4,
            timeout_s=60, tests_glob="res://tests/", resume=resume)
    finally:
        mrunner._run_gut_on_project = orig


def register(registry: Registry) -> None:
    state: dict[str, object] = {}

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
