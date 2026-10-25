"""SEE-1405 gdself consumer-index scenarios on pytest-bdd."""

from __future__ import annotations

from pathlib import Path

from pytest_bdd import given, scenarios, then, when

from godot_qa_toolkit.mutation.gdself import query_consumers
from godot_qa_toolkit.mutation.testscan import derive_tests_glob

from ._support import fail_step as _fail

scenarios("features/see1405_gdself.feature")


def _toy_gdself_project(base: Path) -> Path:
    root = base / "proj"
    (root / "systems").mkdir(parents=True)
    (root / "scenes").mkdir()
    tests = root / "tests"
    tests.mkdir()
    (root / "project.godot").write_text(
        '[autoload]\nEffectManager="*res://autoload/effect_manager.gd"\n',
        encoding="utf-8",
    )
    (root / "autoload").mkdir()
    (root / "autoload" / "effect_manager.gd").write_text("extends Node\n")
    (root / "systems" / "item_data.gd").write_text(
        "class_name ItemData\nextends RefCounted\n"
    )
    (root / "systems" / "factory.gd").write_text(
        'const D = preload("res://systems/item_data.gd")\nclass_name ItemFactory\n'
    )
    (root / "systems" / "item_view.gd").write_text(
        "class_name ItemView\nextends Node2D\n"
    )
    (root / "scenes" / "item_view.tscn").write_text(
        "[gd_scene format=3]\n"
        '[ext_resource type="Script" path="res://systems/item_view.gd" id="1"]\n'
    )
    (root / "systems" / "event_bus.gd").write_text(
        "class_name EventBus\nextends Node\n"
    )
    for index in range(25):
        (root / "systems" / f"consumer_{index}.gd").write_text(
            "extends Node\nvar _b: EventBus\n"
        )
    (root / "systems" / "orphan.gd").write_text(
        "class_name Orphan\nextends RefCounted\n"
    )
    (tests / "test_factory.gd").write_text(
        'const F = preload("res://systems/factory.gd")\n'
        "var d: ItemData\n"
        'var em = get_node_or_null("EffectManager")\n'
    )
    (tests / "test_scene.gd").write_text(
        'const S = preload("res://scenes/item_view.tscn")\n'
    )
    (tests / "test_bus.gd").write_text("var bus: EventBus\n")
    return root


def _derive_glob(bdd_state: dict, target: str) -> None:
    project = bdd_state["proj"]
    bdd_state["glob"] = derive_tests_glob(
        target, project / "tests", project_root=str(project)
    )


@given("a toy project with class autoload scene literal and dynamic consumers")
def given_toy_factory(bdd_state: dict, tmp_path: Path) -> None:
    bdd_state["proj"] = _toy_gdself_project(tmp_path)


@when("the indexed glob of the factory target is derived")
def when_factory_glob(bdd_state: dict) -> None:
    _derive_glob(bdd_state, "res://systems/factory.gd")


@then("the glob converges to the single direct test file")
def then_single_factory_test(bdd_state: dict) -> None:
    if bdd_state["glob"] != "res://tests/test_factory.gd":
        _fail("factory glob did not converge to single test file", bdd_state["glob"])


@given("a toy project with a scene-bound script consumed only via its scene")
def given_scene_script(bdd_state: dict, tmp_path: Path) -> None:
    bdd_state["proj"] = _toy_gdself_project(tmp_path)


@when("the indexed glob of the script target is derived")
def when_scene_script_glob(bdd_state: dict) -> None:
    _derive_glob(bdd_state, "res://systems/item_view.gd")


@then("the glob converges to the scene consuming test file")
def then_scene_test(bdd_state: dict) -> None:
    if bdd_state["glob"] != "res://tests/test_scene.gd":
        _fail("scene-bound script glob did not converge", bdd_state["glob"])


@given("a toy project with a hub class consumed by many production files")
def given_hub(bdd_state: dict, tmp_path: Path) -> None:
    bdd_state["proj"] = _toy_gdself_project(tmp_path)


@when("the indexed consumers of the hub target are queried with fanout 20")
def when_hub_consumers(bdd_state: dict) -> None:
    project = bdd_state["proj"]
    bdd_state["hub_out"] = query_consumers(
        str(project), "res://systems/event_bus.gd", project / "tests", hub_fanout=20
    )


@then("the terminal test consumer is present and hub truncation is disclosed")
def then_hub_truncation(bdd_state: dict) -> None:
    output = bdd_state["hub_out"]
    hits = {consumer["res"] for consumer in output["consumers"]}
    if "res://tests/test_bus.gd" not in hits:
        _fail("terminal test consumer lost under hub truncation", str(sorted(hits)))
    if output["unresolved"]["hub_truncated"] < 1:
        _fail("hub truncation not disclosed", str(output["unresolved"]))


@given("a toy project with an unreferenced script and no uid literals")
def given_orphan(bdd_state: dict, tmp_path: Path) -> None:
    bdd_state["proj"] = _toy_gdself_project(tmp_path)


@when("the indexed consumers of the orphan target are queried")
def when_orphan_consumers(bdd_state: dict) -> None:
    project = bdd_state["proj"]
    bdd_state["orphan_out"] = query_consumers(
        str(project), "res://systems/orphan.gd", project / "tests"
    )


@then("no consumer is returned and the reason is no_tests")
def then_orphan_no_tests(bdd_state: dict) -> None:
    output = bdd_state["orphan_out"]
    if output["consumers"]:
        hits = [consumer["res"] for consumer in output["consumers"]]
        _fail("orphan target unexpectedly has consumers", str(hits))
    if output["unresolved"]["reason"] != "no_tests":
        _fail("orphan reason mismatch", str(output["unresolved"]["reason"]))
