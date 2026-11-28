"""SEE-1624 hardener（Harti）— 语义变异幸存者判别力补强（tests-only）。

Round-1 语义变异在下列位点存活（现有断言面缺口），逐点补钉当前生产行为
（零行为变更，全部断言均为"现状即正确"的判别力钉）：

- H1  缺根必须具名 missing-root（空集错误串不能顶替缺根命名）
- H2  compile --keys 私有套件路径零 Godot 进程（compile 纯解析，§SPEC-003）
- H3  compile --keys 编译期失败（parse 错误）目录回收 + 暂存父目录移除
- H4  replay 拒绝 worker 产物字节篡改（worker_hash 绑定域）
- H5  run_mutate 域错误先于 validator 拉起（零 spawn，§SPEC-016 同构）
- H6  measure 臂 validator 先于业务扇出（次序钉，非仅计数）
- H7  adapter 指纹排除 symlink（no-follow 契约）
- H8  adapter 指纹空源集具名 fail-loud
- H9  adapter 指纹缺根具名 not-found（不冒充空源集）
"""

from __future__ import annotations

import io
import json
import subprocess as sp
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from godot_qa_toolkit.gherkin.addon import AddonError, resolve_feature
from godot_qa_toolkit.gherkin.mutate_runner import run_mutate
from godot_qa_toolkit.gherkin.run import GherkinRunError, run_suite
from godot_qa_toolkit.measurement.recipe import adapter_source_sha256

from gherkin_fixtures import (
    FEATURE,
    make_addon_project,
    write_feature,
)

BROKEN_FEATURE = """Feature: broken
Then orphan step outside scenario
"""


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    return make_addon_project(tmp_path / "proj")


def _run_cli(argv: list[str]) -> tuple[int, str]:
    """in-process CLI；未收口异常在此抛出（JSON 契约破裂即 RED 面）。"""
    from godot_qa_toolkit.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(argv)
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = args.func(args)
    return code, buf.getvalue()


def _assert_single_json(code: int, out: str) -> dict:
    payload = json.loads(out)
    assert isinstance(payload, dict)
    assert payload.get("tool") == "gherkin"
    return payload


def _assert_zero_private_suite_residue(project: Path) -> None:
    leftovers = sorted(str(p.relative_to(project))
                       for p in project.rglob(".gqt-run-*"))
    assert leftovers == [], f"private suite residue: {leftovers}"


def _install_fake_godot(monkeypatch, calls: list) -> None:
    """按 argv 路由的假 godot（validator/GUT 都回最小成功输出），计数进 calls。"""

    def fake_run(argv, **kwargs):
        argv_s = " ".join(str(a) for a in argv)
        calls.append(list(argv))
        if "validator.gd" in argv_s:
            verdict = json.dumps({"ok": True, "supported_majors": [1],
                                  "reason": ""})

            class _V:
                returncode = 0
                stdout = verdict + "\n"
                stderr = ""
            return _V()

        class _G:
            returncode = 0
            stdout = ("res://gherkin/generated/test_demo.gd\n"
                      "* test_x_000_00\n* test_x_001_01\n")
            stderr = ""
        return _G()

    monkeypatch.setattr(sp, "run", fake_run)
    import godot_qa_toolkit.mutation.inject as inject
    monkeypatch.setattr(inject.subprocess, "run", fake_run)


# ---------- H1 缺根具名（不能被空集错误串顶替） ----------

def test_missing_features_root_named_as_missing_root(tmp_path: Path) -> None:
    project = make_addon_project(tmp_path / "proj", features={})
    (project / "addons" / "gqt_gherkin" / "features").rmdir()
    with pytest.raises(AddonError, match="missing features root"):
        resolve_feature(project, "demo")


# ---------- H2 compile --keys 私有套件路径零 Godot ----------

def test_compile_keys_private_suite_spawns_zero_godot(
        project: Path, monkeypatch) -> None:
    calls: list = []
    _install_fake_godot(monkeypatch, calls)
    code, out = _run_cli(["gherkin", "compile",
                          "--project-root", str(project),
                          "--keys", "demo", "--write"])
    payload = _assert_single_json(code, out)
    assert payload["ok"] is True
    assert calls == [], f"compile keys path must spawn zero godot: {calls}"


# ---------- H3 编译期失败回收 + 暂存父目录移除 ----------

def test_compile_keys_parse_failure_cleans_suite_and_parent(
        project: Path) -> None:
    write_feature(project, "broken", BROKEN_FEATURE)
    code, out = _run_cli(["gherkin", "compile",
                          "--project-root", str(project),
                          "--keys", "broken", "--write"])
    payload = _assert_single_json(code, out)
    assert payload["ok"] is False
    assert "compile failed" in payload["summary"]["error"]
    assert code == 1
    _assert_zero_private_suite_residue(project)
    # 本管线创建的空暂存父目录一并移除（零残留语义到 generated 层）
    assert not (project / "gherkin" / "generated").exists()


# ---------- H4 replay 拒绝 worker 产物字节篡改 ----------

def test_replay_rejects_tampered_worker_bytes(project: Path,
                                              monkeypatch) -> None:
    from godot_qa_toolkit.gherkin.compile import compile_feature

    src = write_feature(project, "demo")
    suite, error = compile_feature(src, project, write=True)
    assert error is None
    worker = project / "gherkin" / "generated" / "gherkin_worker.gd"
    worker.write_text(worker.read_text(encoding="utf-8") + "\n# tampered\n",
                      encoding="utf-8")
    spawn_calls: list = []

    def _must_not_spawn(*a, **k):
        spawn_calls.append(list(a[0]) if a else [])
        raise AssertionError("business/preflight spawn must not happen")

    monkeypatch.setattr(sp, "run", _must_not_spawn)
    import godot_qa_toolkit.mutation.inject as inject
    monkeypatch.setattr(inject.subprocess, "run", _must_not_spawn)
    with pytest.raises(GherkinRunError, match="worker hash mismatch"):
        run_suite(project, "demo")
    assert spawn_calls == []


# ---------- H5 run_mutate 域错误先于 validator 拉起 ----------

def test_mutate_domain_error_precedes_validator_spawn(
        project: Path, monkeypatch) -> None:
    from godot_qa_toolkit.gherkin.compile import compile_feature

    src = write_feature(project, "demo")
    suite, error = compile_feature(src, project, write=True)
    assert error is None
    gd = project / "gherkin" / "generated" / "test_demo.gd"
    gd.write_text(gd.read_text(encoding="utf-8") + "\n# tampered\n",
                  encoding="utf-8")
    spawn_calls: list = []

    def _must_not_spawn(*a, **k):
        spawn_calls.append(list(a[0]) if a else [])
        raise AssertionError("business/preflight spawn must not happen")

    monkeypatch.setattr(sp, "run", _must_not_spawn)
    import godot_qa_toolkit.mutation.inject as inject
    monkeypatch.setattr(inject.subprocess, "run", _must_not_spawn)
    with pytest.raises(GherkinRunError,
                       match="generated test hash mismatch"):
        run_mutate(project, "demo")
    assert spawn_calls == []


# ---------- H6 measure 臂 validator 先于业务扇出（次序钉） ----------

def test_measure_arm_validator_precedes_business_fanout(
        project: Path, monkeypatch) -> None:
    import godot_qa_toolkit.measurement.arms as arms

    from godot_qa_toolkit.measurement.recipe import build_recipe

    # 单一时序序列：validator spawn 与业务扇出共用一个列表，次序可直接断言
    seq: list[str] = []

    def fake_run(argv, **kwargs):
        argv_s = " ".join(str(a) for a in argv)
        if "validator.gd" in argv_s:
            seq.append("validator")
            verdict = json.dumps({"ok": True, "supported_majors": [1],
                                  "reason": ""})

            class _V:
                returncode = 0
                stdout = verdict + "\n"
                stderr = ""
            return _V()
        raise AssertionError(f"unexpected spawn in arm flow: {argv_s}")

    monkeypatch.setattr(sp, "run", fake_run)
    import godot_qa_toolkit.mutation.inject as inject
    monkeypatch.setattr(inject.subprocess, "run", fake_run)

    write_feature(project, "demo")
    recipe, error = build_recipe(project)
    assert error is None

    def fake_run_arm(name, **kwargs):
        seq.append("business-fanout")
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
    assert result["state"] == "valid"
    # 次序钉：validator 拉起先于业务扇出（compile 纯解析阶段零 spawn）
    assert seq == ["validator", "business-fanout"]


# ---------- H7/H8/H9 adapter 指纹边界 ----------

def test_adapter_fingerprint_excludes_symlinks(project: Path) -> None:
    write_feature(project, "demo")
    fp, n = adapter_source_sha256(project)
    addon = project / "addons" / "gqt_gherkin"
    (addon / "features" / "linked.gd").symlink_to(addon / "harness.gd")
    fp2, n2 = adapter_source_sha256(project)
    assert (fp2, n2) == (fp, n), "symlinked .gd must not enter fingerprint"


def test_adapter_fingerprint_empty_source_set_named(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    addon = project / "addons" / "gqt_gherkin"
    addon.mkdir(parents=True)
    (addon / "schema.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError,
                       match="no .gd adapter sources"):
        adapter_source_sha256(project)


def test_adapter_fingerprint_missing_addon_named_not_found(
        tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir(parents=True)
    with pytest.raises(ValueError, match="not found in project root"):
        adapter_source_sha256(project)
