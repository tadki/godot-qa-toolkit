"""SEE-1624 §SPEC-003 — schema/validator 握手与预检共用（进程账）。

- addon schema.json = `gqt-gherkin-addon/1`：缺声明、major 不支持、
  requires 过高、testing-api token 漂移、支持集漂移、V/P 检查红一律
  fail-loud 且无产物；
- compile 仅纯解析握手（spawn_godot=0）；run/mutate 业务执行前拉起
  addon 内同一 validator.gd 且每次调用恰一次；
- CLI 与 measure 直接调用 compile/replay/run/arms 的核心路径共用同一
  预检（不在 cmd_gherkin 单点守门）；measure 预检失败具名 invalid、
  无残留。
"""

from __future__ import annotations

import json
import subprocess as sp
from pathlib import Path

import pytest

from godot_qa_toolkit.gherkin.addon import (
    SCHEMA_VERSION,
    AddonError,
    compile_preflight,
    run_validator_preflight,
)
from godot_qa_toolkit.gherkin.compile import compile_feature
from godot_qa_toolkit.gherkin.mutate_runner import run_mutate
from godot_qa_toolkit.gherkin.run import GherkinRunError, run_suite

from gherkin_fixtures import (
    FEATURE,
    SECOND_FEATURE,
    make_addon_project,
    write_feature,
)

VALID_VERDICT = json.dumps(
    {"ok": True, "supported_majors": [1], "reason": ""})


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str, stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _install_fake_godot(monkeypatch, calls: list,
                        verdict: str = VALID_VERDICT,
                        validator_rc: int = 0,
                        gut_output: str = "",
                        gut_rc: int = 0) -> None:
    """按 argv 路由的假 godot：validator.gd 拉起回 verdict，GUT 拉起回
    运行流输出。计数进 calls（进程账判据面）。"""

    def fake_run(argv, **kwargs):
        argv_s = " ".join(str(a) for a in argv)
        calls.append(list(argv))
        if "validator.gd" in argv_s:
            return _FakeCompleted(validator_rc, verdict + "\n")
        return _FakeCompleted(gut_rc, gut_output)

    monkeypatch.setattr(sp, "run", fake_run)
    import godot_qa_toolkit.mutation.inject as inject
    monkeypatch.setattr(inject.subprocess, "run", fake_run)


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    return make_addon_project(tmp_path / "proj")


# ---------- compile：纯解析握手 + 零 Godot ----------

def test_compile_preflight_passes_on_valid_schema(project: Path) -> None:
    verdict = compile_preflight(project)
    assert verdict["schema"] == "gqt-gherkin-addon/1.0"


def test_compile_spawns_zero_godot_processes(project: Path,
                                             monkeypatch) -> None:
    calls: list = []
    _install_fake_godot(monkeypatch, calls)
    src = write_feature(project, "demo")
    suite, error = compile_feature(src, project, write=True)
    assert error is None
    assert calls == []


def test_missing_schema_is_named_failure(project: Path) -> None:
    (project / "addons" / "gqt_gherkin" / "schema.json").unlink()
    with pytest.raises(AddonError, match="schema"):
        compile_preflight(project)
    src = write_feature(project, "demo")
    suite, error = compile_feature(src, project, write=True)
    assert suite is None and error is not None
    assert "schema" in error["reason"]
    assert not (project / "gherkin").exists()


@pytest.mark.parametrize("bad_version", [
    "gqt-gherkin-addon/2",      # 未来的 addon major：本 GQT 不支持
    "gqt-gherkin-addon/99",     # 任意未支持 major
    "gqt-addon/1",              # 名字漂移
])
def test_unsupported_schema_major_fails(project: Path,
                                        bad_version: str) -> None:
    src = write_feature(project, "demo")
    doc = {"schema": bad_version,
           "requires_gqt": "v0.4.0",
           "testing_api": "see1468-gherkin/2"}
    (project / "addons" / "gqt_gherkin" /
     "schema.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(AddonError, match="major"):
        compile_preflight(project)
    suite, error = compile_feature(src, project, write=True)
    assert suite is None and error is not None
    assert not (project / "gherkin").exists()


def test_requires_too_high_fails(project: Path) -> None:
    doc = {"schema": "gqt-gherkin-addon/1.0",
           "requires_gqt": "v9.9.9",
           "testing_api": "see1468-gherkin/2"}
    (project / "addons" / "gqt_gherkin" /
     "schema.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(AddonError, match="requires"):
        compile_preflight(project)


@pytest.mark.parametrize("missing", ["requires_gqt", "testing_api"])
def test_missing_required_declaration_fails(project: Path,
                                            missing: str) -> None:
    doc = {"schema": "gqt-gherkin-addon/1.0",
           "requires_gqt": "v0.4.0",
           "testing_api": "see1468-gherkin/2"}
    doc.pop(missing)
    (project / "addons" / "gqt_gherkin" /
     "schema.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(AddonError, match=missing):
        compile_preflight(project)


def test_testing_api_token_drift_fails(project: Path) -> None:
    doc = {"schema": "gqt-gherkin-addon/1.0",
           "requires_gqt": "v0.4.0",
           "testing_api": "see1468-accept/2"}
    (project / "addons" / "gqt_gherkin" /
     "schema.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(AddonError, match="testing_api"):
        compile_preflight(project)


def test_corrupt_schema_is_named_failure(project: Path) -> None:
    (project / "addons" / "gqt_gherkin" /
     "schema.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(AddonError, match="schema"):
        compile_preflight(project)


# ---------- run/mutate：validator 拉起 + 支持集等值 ----------

def test_run_preflight_spawns_addon_validator_and_verifies_majors(
        project: Path, monkeypatch) -> None:
    calls: list = []
    _install_fake_godot(monkeypatch, calls)
    verdict = run_validator_preflight(project)
    assert verdict["ok"] is True
    assert len(calls) == 1
    assert any("validator.gd" in str(a) for a in calls[0])


def test_run_preflight_red_verdict_is_named(project: Path,
                                            monkeypatch) -> None:
    calls: list = []
    red = json.dumps({"ok": False, "supported_majors": [1],
                      "reason": "harness.gd missing"})
    _install_fake_godot(monkeypatch, calls, verdict=red)
    with pytest.raises(AddonError, match="harness.gd missing"):
        run_validator_preflight(project)


def test_run_preflight_supported_set_drift_is_named(
        project: Path, monkeypatch) -> None:
    calls: list = []
    drifted = json.dumps({"ok": True, "supported_majors": [1, 2],
                          "reason": ""})
    _install_fake_godot(monkeypatch, calls, verdict=drifted)
    with pytest.raises(AddonError, match="supported_majors"):
        run_validator_preflight(project)


def test_run_preflight_missing_validator_is_named(
        tmp_path: Path) -> None:
    project = make_addon_project(tmp_path / "proj", validator_gd=None)
    with pytest.raises(AddonError, match="validator"):
        run_validator_preflight(project)


def test_run_preflight_unparseable_verdict_is_named(
        project: Path, monkeypatch) -> None:
    calls: list = []
    _install_fake_godot(monkeypatch, calls, verdict="Godot banner noise")
    with pytest.raises(AddonError, match="verdict"):
        run_validator_preflight(project)


def test_run_suite_preflights_once_then_runs(project: Path,
                                             monkeypatch) -> None:
    calls: list = []
    gut_output = ("res://gherkin/generated/test_demo.gd\n"
                  "* test_x_000_00\n* test_x_001_01\n")
    _install_fake_godot(monkeypatch, calls, gut_output=gut_output)
    src = write_feature(project, "demo")
    suite, error = compile_feature(src, project, write=True)
    assert error is None
    result = run_suite(project, "demo")
    assert result["ok"] is True
    validator_calls = [c for c in calls
                       if any("validator.gd" in str(a) for a in c)]
    gut_calls = [c for c in calls
                 if any("gut_cmdln.gd" in str(a) for a in c)]
    assert len(validator_calls) == 1
    assert len(gut_calls) == 1


def test_run_suite_bad_schema_fails_named_without_gut(
        project: Path, monkeypatch) -> None:
    calls: list = []
    _install_fake_godot(monkeypatch, calls)
    (project / "addons" / "gqt_gherkin" /
     "schema.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(GherkinRunError, match="preflight"):
        run_suite(project, "demo")
    # 预检失败先于业务执行：GUT 未被拉起，validator 也未拉起
    assert calls == []


def test_run_mutate_preflights_once(project: Path,
                                    monkeypatch) -> None:
    calls: list = []
    _install_fake_godot(monkeypatch, calls)
    src = write_feature(project, "plain", SECOND_FEATURE)
    suite, error = compile_feature(src, project, write=True)
    assert error is None  # 零 Then → 零 mutants，manifest 已在位
    with pytest.raises(GherkinRunError, match="no mutants"):
        run_mutate(project, "plain")
    validator_calls = [c for c in calls
                       if any("validator.gd" in str(a) for a in c)]
    assert len(validator_calls) == 1


# ---------- §SPEC-016：纯域校验先于 Godot 预检（无二进制双形态） ----------

def test_replay_domain_error_precedes_validator_spawn(
        project: Path, monkeypatch) -> None:
    """篡改产物 → replay 域错误必须胜出：即便 Godot 二进制不可用（环境
    错误），零 spawn 且具名 "hash mismatch"——域检查不依赖环境。"""
    import godot_qa_toolkit.gherkin.addon as addon_mod
    src = write_feature(project, "demo")
    suite, error = compile_feature(src, project, write=True)
    assert error is None
    gd = project / "gherkin" / "generated" / "test_demo.gd"
    gd.write_text(gd.read_text(encoding="utf-8") + "\n# tampered\n",
                  encoding="utf-8")
    monkeypatch.setattr(addon_mod, "godot_command",
                        lambda: "/nonexistent-godot-binary")
    spawn_calls: list = []

    def _must_not_spawn(*a, **k):  # 任何拉起即证明次序回退
        spawn_calls.append(list(a[0]) if a else [])
        raise AssertionError("business/preflight spawn must not happen")

    monkeypatch.setattr(sp, "run", _must_not_spawn)
    with pytest.raises(GherkinRunError,
                       match="generated test hash mismatch"):
        run_suite(project, "demo")
    assert spawn_calls == []


def test_replay_pure_preflight_error_precedes_validator_spawn(
        project: Path, monkeypatch) -> None:
    """schema 缺失属纯解析预检 → replay 阶段即具名失败，零 spawn。"""
    src = write_feature(project, "demo")
    suite, error = compile_feature(src, project, write=True)
    assert error is None
    (project / "addons" / "gqt_gherkin" / "schema.json").unlink()
    spawn_calls: list = []

    def _must_not_spawn(*a, **k):
        spawn_calls.append(list(a[0]) if a else [])
        raise AssertionError("spawn must not happen")

    monkeypatch.setattr(sp, "run", _must_not_spawn)
    with pytest.raises(GherkinRunError, match="preflight"):
        run_suite(project, "demo")
    assert spawn_calls == []


def test_validator_spawns_after_replay_before_gut(
        project: Path, monkeypatch) -> None:
    """干净产物：validator 恰一次且在业务 GUT 之前（次序钉 + 进程账）。"""
    calls: list = []
    _install_fake_godot(
        monkeypatch, calls,
        gut_output=("res://gherkin/generated/test_demo.gd\n"
                    "* test_x_000_00\n* test_x_001_01\n"))
    src = write_feature(project, "demo")
    suite, error = compile_feature(src, project, write=True)
    assert error is None
    result = run_suite(project, "demo")
    assert result["ok"] is True
    targets = []
    for c in calls:
        joined = " ".join(str(a) for a in c)
        if "validator.gd" in joined:
            targets.append("validator")
        elif "gut_cmdln.gd" in joined:
            targets.append("gut")
    assert targets == ["validator", "gut"]


# ---------- measure 核心路径共用同一预检 ----------

def _recipe_of(project: Path) -> dict:
    from godot_qa_toolkit.measurement.recipe import build_recipe
    recipe, error = build_recipe(project)
    assert error is None
    return recipe


def test_measure_arms_flow_preflights_once_and_uses_gherkin_arm(
        project: Path, monkeypatch) -> None:
    import godot_qa_toolkit.measurement.arms as arms

    calls: list = []
    _install_fake_godot(monkeypatch, calls)
    write_feature(project, "demo")
    recipe = _recipe_of(project)
    captured: dict = {}

    def fake_run_arm(name, **kwargs):
        captured["name"] = name
        return {"arm": name, "run_uuid": "u", "wall_s": 0.0, "gut_ok": True,
                "timed_out": False, "state": "valid", "invalid_reason": None,
                "test_failures": {"observed": [], "observed_pairs": [],
                                  "new": [], "new_pairs": [],
                                  "instrumentation_intolerant": []},
                "reconciliation": {"ok": True, "shards": []},
                "bundles": 0, "bundles_data": {}, "repeats": 1,
                "shards": 1}

    monkeypatch.setattr(arms, "run_arm", fake_run_arm)
    result = arms.run_gherkin_flow(
        root=project, recipe=recipe, gherkin_keys=["demo"],
        gherkin_shards=1, arms_parallel=False, gherkin_adaptive=False,
        arm_kwargs={})
    assert captured["name"] == "gherkin"
    assert result["state"] == "valid"
    assert result["process_fallbacks"] == []
    validator_calls = [c for c in calls
                       if any("validator.gd" in str(a) for a in c)]
    assert len(validator_calls) == 1


def test_measure_preflight_failure_is_named_invalid_without_residue(
        tmp_path: Path, monkeypatch) -> None:
    import godot_qa_toolkit.measurement.arms as arms

    project = make_addon_project(tmp_path / "proj", validator_gd=None)
    write_feature(project, "demo")
    recipe = _recipe_of(project)
    calls: list = []
    _install_fake_godot(monkeypatch, calls)
    result = arms.run_gherkin_flow(
        root=project, recipe=recipe, gherkin_keys=["demo"],
        gherkin_shards=1, arms_parallel=False, gherkin_adaptive=False,
        arm_kwargs={})
    assert result["state"] == "invalid"
    assert "preflight" in (result.get("invalid_reason") or "")
    # 无残留：没有任何私有 suite 目录留存
    gen = project / "gherkin" / "generated"
    leftovers = list(gen.glob(".gqt-run-*")) if gen.is_dir() else []
    assert leftovers == []
