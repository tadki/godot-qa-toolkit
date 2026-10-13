"""SEE-1405 gherkin steps — §SPEC-001 key 归一 / §SPEC-003 gdself 场景。

进程内判定（无 GUT、无 godot 子进程）：key 归一与索引查询都是纯 Python
面，场景直接驱动真实构造器与查询 API——判别力来自「两个检出的构造结果
逐字段对照」，不来自 mock。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from godot_qa_toolkit.gherkin.runner import Registry, StepFailure
from godot_qa_toolkit.mutation.resume import ResumeStore
from godot_qa_toolkit.timing import TimingStore


def register(registry: Registry) -> None:
    state: dict[str, object] = {}

    def _fail(reason: str, detail: str | None = None) -> None:
        raise StepFailure(reason=reason, detail=detail)

    def _mk_checkout(base: Path, name: str, sub: str = "") -> Path:
        root = base / name
        root.mkdir(parents=True, exist_ok=True)
        target_dir = root / sub if sub else root
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "calc.gd").write_text("func add(a, b):\n\treturn a + b\n",
                                            encoding="utf-8")
        return root

    # ---- §SPEC-001 -------------------------------------------------------

    @registry.step("two checkouts with an identical relative layout")
    def _given_same(_p: dict[str, str]) -> None:
        base = Path(tempfile.mkdtemp(prefix="see1405-keys-"))
        state["root_a"] = _mk_checkout(base, "checkout_a")
        state["root_b"] = _mk_checkout(base, "checkout_b")

    @registry.step("two checkouts with different relative layouts")
    def _given_diff(_p: dict[str, str]) -> None:
        base = Path(tempfile.mkdtemp(prefix="see1405-keys-"))
        state["root_a"] = _mk_checkout(base, "checkout_a")
        state["root_b"] = _mk_checkout(base, "checkout_b", sub="nested")

    @registry.step("the resume keys of both checkouts are equal")
    def _then_resume_eq(_p: dict[str, str]) -> None:
        ra, rb = state["root_a"], state["root_b"]
        ka = ResumeStore(str(ra), str(ra / "calc.gd"), "res://tests/", 50).key
        kb = ResumeStore(str(rb), str(rb / "calc.gd"), "res://tests/", 50).key
        if ka != kb:
            _fail("resume keys diverge across identical checkouts",
                  f"{ka} != {kb}")

    @registry.step("the timing keys of both checkouts are equal")
    def _then_timing_eq(_p: dict[str, str]) -> None:
        ra, rb = state["root_a"], state["root_b"]
        ka = TimingStore(str(ra), str(ra / "calc.gd"), "mutation",
                         "res://tests/").key
        kb = TimingStore(str(rb), str(rb / "calc.gd"), "mutation",
                         "res://tests/").key
        if ka != kb:
            _fail("timing keys diverge across identical checkouts",
                  f"{ka} != {kb}")

    @registry.step("the resume keys of both checkouts differ")
    def _then_resume_ne(_p: dict[str, str]) -> None:
        ra, rb = state["root_a"], state["root_b"]
        file_b = rb / "nested/calc.gd" if (rb / "nested").is_dir() \
            else rb / "calc.gd"
        ka = ResumeStore(str(ra), str(ra / "calc.gd"), None, 50).key
        kb = ResumeStore(str(rb), str(file_b), None, 50).key
        if ka == kb:
            _fail("different relative layouts produced the same key", ka)

    @registry.step("a verdict recorded at checkout A")
    def _given_record(_p: dict[str, str]) -> None:
        base = Path(tempfile.mkdtemp(prefix="see1405-keys-"))
        ra = _mk_checkout(base, "checkout_a")
        rb = _mk_checkout(base, "checkout_b")
        store_a = ResumeStore(str(ra), str(ra / "calc.gd"), "res://tests/", 50)
        rec = {"kind": "AOR", "original": "a + b", "mutated": "a - b",
               "line": 1, "verdict": "killed", "failures": ["t"],
               "elapsed_s": 0.1}
        if store_a.record(0, rec) is not True:
            _fail("resume store record failed at checkout A")
        state["root_b"] = rb
        state["entry_text"] = (store_a.store_dir / "0.json").read_text(
            encoding="utf-8")

    @registry.step("the store entry is placed at checkout B")
    def _when_place(_p: dict[str, str]) -> None:
        rb = state["root_b"]
        store_b = ResumeStore(str(rb), str(rb / "calc.gd"), "res://tests/", 50)
        store_b.ensure_store_dir()
        (store_b.store_dir / "0.json").write_text(state["entry_text"],
                                                  encoding="utf-8")

    @registry.step("checkout B resumes the recorded verdict without rerunning")
    def _then_resume_hit(_p: dict[str, str]) -> None:
        from types import SimpleNamespace
        rb = state["root_b"]
        store_b = ResumeStore(str(rb), str(rb / "calc.gd"), "res://tests/", 50)
        mutant = SimpleNamespace(kind="AOR", original="a + b",
                                 mutated="a - b", line=1)
        completed = store_b.load_completed([mutant], 0, 1)
        if 0 not in completed:
            _fail("recorded verdict not resumed at checkout B",
                  f"key={store_b.key}")
        if completed[0].get("verdict") != "killed":
            _fail("resumed verdict mismatch",
                  str(completed[0].get("verdict")))
