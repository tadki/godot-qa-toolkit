"""SEE-1624 C1-R4 hardener（Harti）— 语义变异幸存者判别力补强（tests-only）。

Round-1 语义变异在 R4 修复面的下列位点存活（现有断言面缺口），逐点补钉
当前生产行为（零行为变更，全部断言均为"现状即正确"的判别力钉）：

- R1  compile --out-dir 为普通文件 → 具名拒绝且调用方文件原样保留
- R2  create_suite_dir 路径不安全 token → SuiteError（防目录穿越）
- R3  replay 重锚：features 根内 symlink 源解析到项目外 → 拒绝
- R4  validator 协议身份：无 supported_majors 的 JSON 行不得顶替最后合法 verdict
- R5  worker Popen 收到隔离 XDG_DATA_HOME/TMPDIR；spawn 失败路径 scratch/env 零残留
- R6  union.arm_record_from_bundle 退役臂名 'acceptance' 具名拒绝
- R7  recipe adapter_source_files 必须为正整数
- R8  recipe gherkin_face.harness 必需键缺失 → verify 拒绝
- R9  归一后重复 feature key（a-b vs a_b）→ 具名拒绝
- R10 mutate 基线轮真实消费面：ANSI 前缀测试流不被误判为无流
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from gherkin_fixtures import make_addon_project, write_feature


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = make_addon_project(tmp_path / "proj")
    write_feature(root, "demo")
    return root


def _compile(project: Path, stem: str = "demo"):
    from godot_qa_toolkit.gherkin.addon import resolve_feature
    from godot_qa_toolkit.gherkin.compile import compile_feature
    return compile_feature(resolve_feature(project, stem), project,
                           write=True)


def _recipe_of(project: Path) -> dict:
    from godot_qa_toolkit.measurement.recipe import build_recipe
    recipe, error = build_recipe(project)
    assert error is None
    return recipe


class _R:
    def __init__(self, returncode: int = 0, stdout: str = "",
                 stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_godot(monkeypatch, *, verdict_lines: list[str]) -> None:
    def fake_run(argv, **kwargs):
        joined = " ".join(str(a) for a in argv)
        if "validator.gd" in joined:
            return _R(0, "\n".join(verdict_lines) + "\n")
        return _R(0, "")
    import subprocess as sp
    monkeypatch.setattr(sp, "run", fake_run)
    import godot_qa_toolkit.mutation.inject as inject
    monkeypatch.setattr(inject.subprocess, "run", fake_run)


# ---------- R1 out-dir 为普通文件 ----------

def test_compile_out_dir_that_is_a_file_is_rejected(project: Path) -> None:
    from godot_qa_toolkit.gherkin.addon import resolve_feature
    from godot_qa_toolkit.gherkin.compile import compile_feature
    out = project / "caller-file"
    out.write_text("caller data", encoding="utf-8")
    suite, error = compile_feature(resolve_feature(project, "demo"),
                                   project, write=True, out_dir=out)
    assert suite is None and error is not None
    assert "not a directory" in error["reason"]
    assert out.is_file(), "caller file must survive untouched"
    assert out.read_text(encoding="utf-8") == "caller data"


# ---------- R2 私有目录 token 路径安全 ----------

def test_create_suite_dir_rejects_unsafe_token(project: Path) -> None:
    from godot_qa_toolkit.gherkin.suite import SuiteError, create_suite_dir
    with pytest.raises(SuiteError, match="path-safe"):
        create_suite_dir(project, run_uuid="../escape")


# ---------- R3 重锚 symlink 源逃逸 ----------

def test_reanchor_rejects_symlinked_source_outside_project(
        project: Path, tmp_path: Path) -> None:
    from godot_qa_toolkit.gherkin.run import GherkinRunError, replay_check
    suite, error = _compile(project)
    assert error is None
    external = tmp_path / "external.feature"
    external.write_text(
        (project / "addons" / "gqt_gherkin" / "features" /
         "demo.feature").read_text(encoding="utf-8"), encoding="utf-8")
    link = project / "addons" / "gqt_gherkin" / "features" / "linked.feature"
    link.symlink_to(external)
    man_path = project / "gherkin" / "generated" / "demo.manifest.json"
    man = json.loads(man_path.read_text(encoding="utf-8"))
    man["source_path"] = str(link)
    man_path.write_text(json.dumps(man, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    with pytest.raises(GherkinRunError,
                       match="outside the strict single root"):
        replay_check(project, "demo")


def test_reanchor_rejects_in_project_source_outside_features_root(
        project: Path) -> None:
    """项目内但不在单根 features/ 下的源：containment 放行、单根约束必须拒绝
    （两层防线各自职责——只有单根父目录检查能拦住此类源）。"""
    from godot_qa_toolkit.gherkin.run import GherkinRunError, replay_check
    suite, error = _compile(project)
    assert error is None
    elsewhere = project / "elsewhere.feature"
    elsewhere.write_text(
        (project / "addons" / "gqt_gherkin" / "features" /
         "demo.feature").read_text(encoding="utf-8"), encoding="utf-8")
    man_path = project / "gherkin" / "generated" / "demo.manifest.json"
    man = json.loads(man_path.read_text(encoding="utf-8"))
    man["source_path"] = str(elsewhere)
    man_path.write_text(json.dumps(man, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    with pytest.raises(GherkinRunError,
                       match="outside the strict single root"):
        replay_check(project, "demo")


# ---------- R4 validator 协议身份 ----------

def test_validator_ignores_json_lines_without_supported_majors(
        project: Path, monkeypatch) -> None:
    from godot_qa_toolkit.gherkin.addon import run_validator_preflight
    green = json.dumps({"ok": True, "supported_majors": [1], "reason": ""})
    noise = json.dumps({"foo": 1, "level": "info"})
    _fake_godot(monkeypatch, verdict_lines=[green, noise])
    verdict = run_validator_preflight(project)
    assert verdict["ok"] is True
    assert verdict["supported_majors"] == [1]


# ---------- R5 worker env 隔离 + 失败路径零残留 ----------

def test_worker_popen_env_isolated_and_failure_cleans_up(
        project: Path, monkeypatch) -> None:
    from godot_qa_toolkit.gherkin import mutate_runner as mr
    from godot_qa_toolkit.gherkin.run import GherkinRunError
    suite, error = _compile(project)
    assert error is None
    captured: dict = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        raise OSError("stop-after-capture")

    monkeypatch.setattr(mr.subprocess, "Popen", fake_popen)
    with pytest.raises(GherkinRunError, match="worker process spawn failed"):
        mr._spawn_worker(project)
    env = captured["env"]
    assert env.get("XDG_DATA_HOME") not in (None,
                                            os.environ.get("XDG_DATA_HOME"))
    assert env.get("TMPDIR") not in (None, os.environ.get("TMPDIR"))
    # spawn 失败路径：scratch 与隔离 env 目录零残留
    cache = project / ".qa-cache"
    leftovers = ([p for p in cache.glob("gqt-gherkin-worker-*")
                  if p.is_dir()] if cache.is_dir() else [])
    assert leftovers == [], f"scratch residue: {leftovers}"
    assert not Path(env["XDG_DATA_HOME"]).exists()
    assert not Path(env["TMPDIR"]).exists()


# ---------- R6 union 退役臂名 ----------

def test_union_retired_acceptance_arm_fails_loud() -> None:
    from godot_qa_toolkit.coverage.union import arm_record_from_bundle
    bundle = {
        "sentinels": {"probes_fired": 1},
        "probe_manifest": {"inline": {
            "census": {"probes_total": 1}, "probes": []}},
        "recipe": {"arm": "acceptance"},
        "run_uuid": "u",
        "target": {"file": "f.gd", "sha256": "a" * 64},
        "C": [],
        "I": [],
    }
    with pytest.raises(ValueError, match="retired"):
        arm_record_from_bundle(bundle, recipe_hash="c" * 64)


# ---------- R7/R8 recipe 指纹与 harness 键形状 ----------

def test_recipe_rejects_nonpositive_adapter_file_count(
        project: Path) -> None:
    from godot_qa_toolkit.measurement.recipe import (
        recipe_hash, verify_recipe)
    forged = json.loads(json.dumps(_recipe_of(project)))
    forged["gherkin_face"]["adapter_source_files"] = 0
    forged["recipe_hash"] = recipe_hash(forged)
    ok, reason = verify_recipe(forged)
    assert ok is False and "adapter_source_files" in reason


def test_recipe_rejects_harness_missing_required_key(
        project: Path) -> None:
    from godot_qa_toolkit.measurement.recipe import (
        recipe_hash, verify_recipe)
    forged = json.loads(json.dumps(_recipe_of(project)))
    forged["gherkin_face"]["harness"].pop("worker")
    forged["recipe_hash"] = recipe_hash(forged)
    ok, reason = verify_recipe(forged)
    assert ok is False and "harness" in reason


def test_recipe_rejects_gherkin_face_missing_count_key(
        project: Path) -> None:
    """`count` 是封闭键集里的必需键，且无其他形状校验兜底——缺失必须由
    必需键检查本身拒绝（不得仅靠 fingerprint/harness 的类型检查）。"""
    from godot_qa_toolkit.measurement.recipe import (
        recipe_hash, verify_recipe)
    forged = json.loads(json.dumps(_recipe_of(project)))
    del forged["gherkin_face"]["count"]
    forged["recipe_hash"] = recipe_hash(forged)
    ok, reason = verify_recipe(forged)
    assert ok is False and "missing required key" in reason
    assert "count" in reason


# ---------- R9 归一后重复 key ----------

def test_resolve_feature_rejects_duplicate_after_normalization(
        project: Path) -> None:
    from godot_qa_toolkit.gherkin.addon import AddonError, resolve_feature
    write_feature(project, "a-b")
    write_feature(project, "a_b")
    with pytest.raises(AddonError, match="duplicate feature key"):
        resolve_feature(project, "a_b")


# ---------- R10 mutate 基线轮 ANSI 真实消费面 ----------

def test_batch_baseline_round_parses_ansi_test_stream(
        project: Path, monkeypatch) -> None:
    from godot_qa_toolkit.gherkin import mutate_runner as mr
    ansi = ("\x1b[0m* test_demo_000_00\x1b[0m\n"
            "\x1b[1m1/1 passed.\x1b[0m\n")
    monkeypatch.setattr(mr, "_run_gut", lambda root, timeout_s: (0, ansi))
    failing, executed = mr._batch_exempt_baseline(project, 120)
    assert executed == {"test_demo_000_00"}
    assert failing == set()
