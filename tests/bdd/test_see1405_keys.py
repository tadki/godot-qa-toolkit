"""SEE-1405 store-key path normalization scenarios on pytest-bdd."""

from __future__ import annotations

from pathlib import Path

from pytest_bdd import given, scenarios, then, when

from godot_qa_toolkit.mutation.resume2 import ResumeStoreV2
from godot_qa_toolkit.timing import TimingStore

from ._support import fail_step as _fail

scenarios("features/see1405_keys.feature")


def _mk_checkout(base: Path, name: str, sub: str = "") -> Path:
    root = base / name
    target_dir = root / sub if sub else root
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "calc.gd").write_text(
        "func add(a, b):\n\treturn a + b\n", encoding="utf-8"
    )
    return root


@given("two checkouts with an identical relative layout")
def given_same_layout(bdd_state: dict, tmp_path: Path) -> None:
    bdd_state["root_a"] = _mk_checkout(tmp_path, "checkout_a")
    bdd_state["root_b"] = _mk_checkout(tmp_path, "checkout_b")


@given("two checkouts with different relative layouts")
def given_different_layouts(bdd_state: dict, tmp_path: Path) -> None:
    bdd_state["root_a"] = _mk_checkout(tmp_path, "checkout_a")
    bdd_state["root_b"] = _mk_checkout(tmp_path, "checkout_b", sub="nested")


@then("the resume keys of both checkouts are equal")
def then_resume_keys_equal(bdd_state: dict) -> None:
    root_a, root_b = bdd_state["root_a"], bdd_state["root_b"]
    key_a = ResumeStoreV2(str(root_a), str(root_a / "calc.gd"), "res://tests/").key
    key_b = ResumeStoreV2(str(root_b), str(root_b / "calc.gd"), "res://tests/").key
    if key_a != key_b:
        _fail("resume keys diverge across identical checkouts", f"{key_a} != {key_b}")


@then("the timing keys of both checkouts are equal")
def then_timing_keys_equal(bdd_state: dict) -> None:
    root_a, root_b = bdd_state["root_a"], bdd_state["root_b"]
    key_a = TimingStore(
        str(root_a), str(root_a / "calc.gd"), "mutation", "res://tests/"
    ).key
    key_b = TimingStore(
        str(root_b), str(root_b / "calc.gd"), "mutation", "res://tests/"
    ).key
    if key_a != key_b:
        _fail("timing keys diverge across identical checkouts", f"{key_a} != {key_b}")


@then("the resume keys of both checkouts differ")
def then_resume_keys_differ(bdd_state: dict) -> None:
    root_a, root_b = bdd_state["root_a"], bdd_state["root_b"]
    file_b = (
        root_b / "nested/calc.gd"
        if (root_b / "nested").is_dir()
        else root_b / "calc.gd"
    )
    key_a = ResumeStoreV2(str(root_a), str(root_a / "calc.gd"), None).key
    key_b = ResumeStoreV2(str(root_b), str(file_b), None).key
    if key_a == key_b:
        _fail("different relative layouts produced the same key", key_a)


@given("a verdict recorded at checkout A")
def given_recorded_verdict(bdd_state: dict, tmp_path: Path) -> None:
    root_a = _mk_checkout(tmp_path, "checkout_a")
    root_b = _mk_checkout(tmp_path, "checkout_b")
    store_a = ResumeStoreV2(str(root_a), str(root_a / "calc.gd"), "res://tests/")
    record = {
        "id": "calc.gd#add#1",
        "kind": "AOR",
        "original": "a + b",
        "mutated": "a - b",
        "line": 1,
        "verdict": "killed",
    }
    if store_a.record_entry(record) is not True:
        _fail("resume store record failed at checkout A")
    bdd_state["root_b"] = root_b
    bdd_state["entry_text"] = store_a.entry_path("calc.gd#add#1").read_text("utf-8")


@when("the store entry is placed at checkout B")
def when_entry_placed(bdd_state: dict) -> None:
    root_b = bdd_state["root_b"]
    store_b = ResumeStoreV2(str(root_b), str(root_b / "calc.gd"), "res://tests/")
    store_b.ensure_store_dir()
    store_b.entry_path("calc.gd#add#1").write_text(
        bdd_state["entry_text"], encoding="utf-8"
    )


@then("checkout B resumes the recorded verdict without rerunning")
def then_resume_hit(bdd_state: dict) -> None:
    root_b = bdd_state["root_b"]
    store_b = ResumeStoreV2(str(root_b), str(root_b / "calc.gd"), "res://tests/")
    completed = store_b.load_entries()
    if "calc.gd#add#1" not in completed:
        _fail("recorded verdict not resumed at checkout B", f"key={store_b.key}")
    verdict = completed["calc.gd#add#1"].get("verdict")
    if verdict != "killed":
        _fail("resumed verdict mismatch", str(verdict))
