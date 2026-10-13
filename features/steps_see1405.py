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


    # ---- §SPEC-003 gdself -------------------------------------------------

    def _toy_gdself_project(base: Path, with_uid: bool = True) -> Path:
        root = base / "proj"
        (root / "systems").mkdir(parents=True)
        (root / "scenes").mkdir()
        tests = root / "tests"
        tests.mkdir()
        (root / "project.godot").write_text(
            "[autoload]\n"
            'EffectManager="*res://autoload/effect_manager.gd"\n',
            encoding="utf-8")
        (root / "autoload").mkdir()
        (root / "autoload" / "effect_manager.gd").write_text("extends Node\n")
        (root / "systems" / "item_data.gd").write_text(
            "class_name ItemData\nextends RefCounted\n")
        (root / "systems" / "factory.gd").write_text(
            'const D = preload("res://systems/item_data.gd")\n'
            "class_name ItemFactory\n")
        (root / "systems" / "item_view.gd").write_text(
            "class_name ItemView\nextends Node2D\n")
        (root / "scenes" / "item_view.tscn").write_text(
            '[gd_scene format=3]\n'
            '[ext_resource type="Script" '
            'path="res://systems/item_view.gd" id="1"]\n')
        (root / "systems" / "event_bus.gd").write_text(
            "class_name EventBus\nextends Node\n")
        for i in range(25):
            (root / "systems" / f"consumer_{i}.gd").write_text(
                f"extends Node\nvar _b: EventBus\n")
        (root / "systems" / "orphan.gd").write_text(
            "class_name Orphan\nextends RefCounted\n")
        (tests / "test_factory.gd").write_text(
            'const F = preload("res://systems/factory.gd")\n'
            "var d: ItemData\n"
            'var em = get_node_or_null("EffectManager")\n')
        (tests / "test_scene.gd").write_text(
            'const S = preload("res://scenes/item_view.tscn")\n')
        (tests / "test_bus.gd").write_text("var bus: EventBus\n")
        if with_uid:
            (tests / "test_uid.gd").write_text(
                'var r = load("uid://abc123")\n')
        return root

    @registry.step("a toy project with class autoload scene literal and "
                   "dynamic consumers")
    def _given_toy(_p: dict[str, str]) -> None:
        state["proj"] = _toy_gdself_project(
            Path(tempfile.mkdtemp(prefix="see1405-gdself-")),
            with_uid=False)

    @registry.step("the indexed glob of the factory target is derived")
    def _when_factory(_p: dict[str, str]) -> None:
        from godot_qa_toolkit.mutation.testscan import derive_tests_glob
        proj = state["proj"]
        state["glob"] = derive_tests_glob(
            "res://systems/factory.gd", proj / "tests",
            project_root=str(proj))

    @registry.step("the glob converges to the single direct test file")
    def _then_single(_p: dict[str, str]) -> None:
        if state["glob"] != "res://tests/test_factory.gd":
            _fail("factory glob did not converge to single test file",
                  str(state["glob"]))

    @registry.step("a toy project with a scene-bound script consumed only "
                   "via its scene")
    def _given_scene(_p: dict[str, str]) -> None:
        state["proj"] = _toy_gdself_project(
            Path(tempfile.mkdtemp(prefix="see1405-gdself-")),
            with_uid=False)

    @registry.step("the indexed glob of the script target is derived")
    def _when_script(_p: dict[str, str]) -> None:
        from godot_qa_toolkit.mutation.testscan import derive_tests_glob
        proj = state["proj"]
        state["glob"] = derive_tests_glob(
            "res://systems/item_view.gd", proj / "tests",
            project_root=str(proj))

    @registry.step("the glob converges to the scene consuming test file")
    def _then_scene(_p: dict[str, str]) -> None:
        if state["glob"] != "res://tests/test_scene.gd":
            _fail("scene-bound script glob did not converge",
                  str(state["glob"]))

    @registry.step("a toy project with a hub class consumed by many "
                   "production files")
    def _given_hub(_p: dict[str, str]) -> None:
        state["proj"] = _toy_gdself_project(
            Path(tempfile.mkdtemp(prefix="see1405-gdself-")),
            with_uid=False)

    @registry.step("the indexed consumers of the hub target are queried "
                   "with fanout 20")
    def _when_hub(_p: dict[str, str]) -> None:
        from godot_qa_toolkit.mutation.gdself import query_consumers
        proj = state["proj"]
        state["hub_out"] = query_consumers(
            str(proj), "res://systems/event_bus.gd", proj / "tests",
            hub_fanout=20)

    @registry.step("the terminal test consumer is present and hub truncation "
                   "is disclosed")
    def _then_hub(_p: dict[str, str]) -> None:
        out = state["hub_out"]
        hits = {c["res"] for c in out["consumers"]}
        if "res://tests/test_bus.gd" not in hits:
            _fail("terminal test consumer lost under hub truncation",
                  str(sorted(hits)))
        if out["unresolved"]["hub_truncated"] < 1:
            _fail("hub truncation not disclosed",
                  str(out["unresolved"]))

    @registry.step("a toy project with an unreferenced script and no uid "
                   "literals")
    def _given_orphan(_p: dict[str, str]) -> None:
        state["proj"] = _toy_gdself_project(
            Path(tempfile.mkdtemp(prefix="see1405-gdself-")),
            with_uid=False)

    @registry.step("the indexed consumers of the orphan target are queried")
    def _when_orphan(_p: dict[str, str]) -> None:
        from godot_qa_toolkit.mutation.gdself import query_consumers
        proj = state["proj"]
        state["orphan_out"] = query_consumers(
            str(proj), "res://systems/orphan.gd", proj / "tests")

    @registry.step("no consumer is returned and the reason is no_tests")
    def _then_orphan(_p: dict[str, str]) -> None:
        out = state["orphan_out"]
        if out["consumers"]:
            _fail("orphan target unexpectedly has consumers",
                  str([c["res"] for c in out["consumers"]]))
        if out["unresolved"]["reason"] != "no_tests":
            _fail("orphan reason mismatch",
                  str(out["unresolved"]["reason"]))
