"""SEE-1624 C1-R4（Final Review REQUEST CHANGES）— 13 组缺陷回归。

每组断言当前 head 判红、修复后转绿：
- C1: 调用方目录/文件永不被递归删除；私有清理仅限工具所有权形态
- H2: manifest ir_path/source_path 逃逸项目/单根拒绝 + 非规范 key 绑定
- H3: symlink features 根逃逸单根拒绝
- H4: R2 schema 字段（schema/requires_gqt/testing_api）与 major.minor 语义
- H5: validator verdict 取最后一个合法 verdict + 进程状态校验
- H6: CLI replay 域错误先于二进制配置错误；worker 每 run 独立 scratch
- H7: 旧 env/JSON 键/臂名 fail-loud，新 gherkin_* 生效
- H8: shadow 升版 shadow/2 且旧 acceptance_cost 具名拒绝
- H9: recipe 严格键集 + adapter_source_sha256 必需
- M10: 畸形 manifest → GherkinRunError（JSON 契约）
- M11: feature key 规范化全链一致（连字符 stem 可 key-mode 运行）
- M12: perf resume 校验 doc，旧报告具名拒绝
"""

from __future__ import annotations

import json
import shutil
import subprocess as sp
from pathlib import Path

import pytest

from gherkin_fixtures import make_addon_project, write_feature

VALID_R2_SCHEMA = {
    "schema": "gqt-gherkin-addon/1.0",
    "requires_gqt": "v0.4.1",
    "testing_api": "see1468-gherkin/2",
}


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = make_addon_project(tmp_path / "proj")
    write_feature(root, "demo")
    return root


def _schema_path(project: Path) -> Path:
    return project / "addons" / "gqt_gherkin" / "schema.json"


def _write_schema(project: Path, doc: dict) -> None:
    _schema_path(project).write_text(json.dumps(doc), encoding="utf-8")


def _compile(project: Path, stem: str = "demo", write: bool = True):
    from godot_qa_toolkit.gherkin.addon import resolve_feature
    from godot_qa_toolkit.gherkin.compile import compile_feature
    return compile_feature(resolve_feature(project, stem), project,
                           write=write)


class _R:
    def __init__(self, returncode: int = 0, stdout: str = "",
                 stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_godot(monkeypatch, *, verdict_lines: list[str] | None = None,
                validator_rc: int = 0, gut_output: str = "",
                gut_rc: int = 0, calls: list | None = None):
    verdict = json.dumps({"ok": True, "supported_majors": [1], "reason": ""})
    lines = verdict_lines if verdict_lines is not None else [verdict]

    def fake_run(argv, **kwargs):
        if calls is not None:
            calls.append(list(argv))
        joined = " ".join(str(a) for a in argv)
        if "validator.gd" in joined:
            return _R(validator_rc, "\n".join(lines) + "\n")
        return _R(gut_rc, gut_output)
    monkeypatch.setattr(sp, "run", fake_run)
    import godot_qa_toolkit.mutation.inject as inject
    monkeypatch.setattr(inject.subprocess, "run", fake_run)


# ================= C1：调用方目录永不被递归删除 =================

def test_compile_out_dir_must_be_empty_and_never_deletes(
        project: Path, tmp_path: Path) -> None:
    out = project / "caller-out"
    out.mkdir()
    notes = out / "notes.txt"
    notes.write_text("caller data", encoding="utf-8")
    from godot_qa_toolkit.gherkin.addon import resolve_feature
    from godot_qa_toolkit.gherkin.compile import compile_feature
    suite, error = compile_feature(resolve_feature(project, "demo"),
                                   project, write=True, out_dir=out)
    assert suite is None and error is not None
    assert "empty" in error["reason"]
    assert notes.is_file(), "caller file must survive"
    assert notes.read_text(encoding="utf-8") == "caller data"


def test_run_suite_dir_never_deletes_caller_dir(
        project: Path, monkeypatch) -> None:
    from godot_qa_toolkit.gherkin.suite import (
        compile_keys_to_suite, run_suite_dir)
    caller = project / "caller-suite"
    caller.mkdir()
    compile_keys_to_suite(project, ["demo"], caller)
    notes = caller / "notes.txt"
    notes.write_text("caller data", encoding="utf-8")
    manifest = json.loads(
        (caller / "generated" / "demo.manifest.json").read_text(
            encoding="utf-8"))
    gen_rel = (caller / "generated").relative_to(project).as_posix()
    flow = [f"res://{gen_rel}/test_demo.gd"]
    for a in manifest["attribution"]:
        flow.append(f"* {a['test_name']}")
    _fake_godot(monkeypatch, gut_output="\n".join(flow) + "\n")
    record = run_suite_dir(project, caller)
    assert record["ok"] is True
    assert caller.is_dir(), "caller dir must survive"
    assert notes.is_file(), "caller file must survive"
    assert record.get("cleanup") != "removed"


def test_cleanup_suite_dir_only_removes_owned_shape(
        project: Path) -> None:
    from godot_qa_toolkit.gherkin.suite import (
        _cleanup_suite_dir, create_suite_dir)
    caller = project / "not-owned"
    caller.mkdir()
    (caller / "data.txt").write_text("x", encoding="utf-8")
    result = _cleanup_suite_dir(caller, project)
    assert result == "kept-caller-owned"
    assert caller.is_dir() and (caller / "data.txt").is_file()
    owned = create_suite_dir(project)
    assert _cleanup_suite_dir(owned, project) == "removed"
    assert not owned.exists()


# ================= H2：manifest 路径逃逸 + key 绑定 =================

def test_replay_rejects_absolute_ir_path_escape(
        project: Path, tmp_path: Path) -> None:
    from godot_qa_toolkit.gherkin.run import GherkinRunError, replay_check
    suite, error = _compile(project)
    assert error is None
    man_path = (project / "gherkin" / "generated" /
                "demo.manifest.json")
    man = json.loads(man_path.read_text(encoding="utf-8"))
    outside = tmp_path / "outside-ir.json"
    outside.write_text(
        (project / "gherkin" / "ir" / "demo.json").read_text(
            encoding="utf-8"), encoding="utf-8")
    man["ir_path"] = str(outside)  # 绝对路径逃逸
    man_path.write_text(json.dumps(man, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    with pytest.raises(GherkinRunError, match="outside the project"):
        replay_check(project, "demo")


def test_replay_rejects_dotdot_ir_path_escape(
        project: Path, tmp_path: Path) -> None:
    from godot_qa_toolkit.gherkin.run import GherkinRunError, replay_check
    suite, error = _compile(project)
    assert error is None
    man_path = (project / "gherkin" / "generated" /
                "demo.manifest.json")
    man = json.loads(man_path.read_text(encoding="utf-8"))
    man["ir_path"] = "../outside/demo.json"
    man_path.write_text(json.dumps(man, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    with pytest.raises(GherkinRunError, match="outside the project"):
        replay_check(project, "demo")


def test_replay_rejects_source_outside_features_root(
        project: Path, tmp_path: Path) -> None:
    from godot_qa_toolkit.gherkin.run import GherkinRunError, replay_check
    suite, error = _compile(project)
    assert error is None
    man_path = (project / "gherkin" / "generated" /
                "demo.manifest.json")
    man = json.loads(man_path.read_text(encoding="utf-8"))
    outside = tmp_path / "outside.feature"
    outside.write_text(
        (project / "addons" / "gqt_gherkin" / "features" /
         "demo.feature").read_text(encoding="utf-8"), encoding="utf-8")
    man["source_path"] = str(outside)
    man_path.write_text(json.dumps(man, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    with pytest.raises(GherkinRunError,
                       match="outside the strict single root"):
        replay_check(project, "demo")


def test_replay_binds_normalized_manifest_key(
        project: Path) -> None:
    from godot_qa_toolkit.gherkin.run import GherkinRunError, replay_check
    suite, error = _compile(project)
    assert error is None
    man_path = (project / "gherkin" / "generated" /
                "demo.manifest.json")
    man = json.loads(man_path.read_text(encoding="utf-8"))
    man["feature_key"] = "other_key"
    man_path.write_text(json.dumps(man, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    with pytest.raises(GherkinRunError, match="feature_key"):
        replay_check(project, "demo")


# ================= H3：symlink features 根逃逸 =================

def test_symlinked_features_root_escape_rejected(
        project: Path, tmp_path: Path) -> None:
    feats = project / "addons" / "gqt_gherkin" / "features"
    external = tmp_path / "external-features"
    external.mkdir()
    (external / "demo.feature").write_text(
        (feats / "demo.feature").read_text(encoding="utf-8"),
        encoding="utf-8")
    shutil.rmtree(feats)
    feats.symlink_to(external, target_is_directory=True)
    suite, error = _compile(project, write=False)
    assert suite is None and error is not None
    assert "escapes the project root" in error["reason"]


# ================= H4：R2 schema 字段与 major.minor =================

def test_r2_schema_fields_accepted_with_unknown_keys(project: Path) -> None:
    from godot_qa_toolkit.gherkin.addon import compile_preflight
    _write_schema(project, {**VALID_R2_SCHEMA, "future_key": {"x": 1}})
    doc = compile_preflight(project)
    assert doc["schema"] == "gqt-gherkin-addon/1.0"


@pytest.mark.parametrize("schema,ok", [
    ("gqt-gherkin-addon/1", True),
    ("gqt-gherkin-addon/1.0", True),
    ("gqt-gherkin-addon/1.7", True),   # minor additive
    ("gqt-gherkin-addon/2.0", False),  # major 不支持
    ("gqt-gherkin-addon/x", False),
])
def test_schema_major_minor_semantics(project: Path, schema: str,
                                      ok: bool) -> None:
    from godot_qa_toolkit.gherkin.addon import AddonError, compile_preflight
    _write_schema(project, {**VALID_R2_SCHEMA, "schema": schema})
    if ok:
        compile_preflight(project)
    else:
        with pytest.raises(AddonError, match="major"):
            compile_preflight(project)


@pytest.mark.parametrize("field", ["schema", "requires_gqt", "testing_api"])
def test_missing_r2_field_fails(project: Path, field: str) -> None:
    from godot_qa_toolkit.gherkin.addon import AddonError, compile_preflight
    doc = dict(VALID_R2_SCHEMA)
    doc.pop(field)
    _write_schema(project, doc)
    with pytest.raises(AddonError):
        compile_preflight(project)


def test_requires_gqt_and_token_validation(project: Path) -> None:
    from godot_qa_toolkit.gherkin.addon import AddonError, compile_preflight
    _write_schema(project, {**VALID_R2_SCHEMA, "requires_gqt": "v9.9.9"})
    with pytest.raises(AddonError, match="requires_gqt"):
        compile_preflight(project)
    _write_schema(project, {**VALID_R2_SCHEMA,
                            "testing_api": "see1468-accept/2"})
    with pytest.raises(AddonError, match="testing_api"):
        compile_preflight(project)


# ================= H5：validator verdict 最后一个合法行获胜 =================

def test_validator_last_verdict_wins(project: Path, monkeypatch) -> None:
    from godot_qa_toolkit.gherkin.addon import AddonError, \
        run_validator_preflight
    green = json.dumps({"ok": True, "supported_majors": [1], "reason": ""})
    red = json.dumps({"ok": False, "supported_majors": [1],
                      "reason": "V2 harness.gd missing"})
    _fake_godot(monkeypatch, verdict_lines=[green, red])
    with pytest.raises(AddonError, match="V2 harness.gd missing"):
        run_validator_preflight(project)


def test_validator_nonzero_exit_rejected(project: Path, monkeypatch) -> None:
    from godot_qa_toolkit.gherkin.addon import AddonError, \
        run_validator_preflight
    _fake_godot(monkeypatch, validator_rc=3)
    with pytest.raises(AddonError, match="exit"):
        run_validator_preflight(project)


# ================= H6：CLI replay 先于二进制配置错误 ================

def test_cli_run_replay_error_precedes_bad_godot_bin(
        project: Path) -> None:
    import io
    from contextlib import redirect_stdout

    from godot_qa_toolkit.cli import build_parser
    suite, error = _compile(project)
    assert error is None
    gd = project / "gherkin" / "generated" / "test_demo.gd"
    gd.write_text(gd.read_text(encoding="utf-8") + "\n# tampered\n",
                  encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args([
        "gherkin", "run", "--project-root", str(project), "--key", "demo",
        "--godot-bin", "/nonexistent-godot-binary"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = args.func(args)
    payload = json.loads(buf.getvalue())
    assert code == 1 and payload["ok"] is False
    err = payload["summary"]["error"]
    assert "hash mismatch" in err, (
        f"domain replay error must precede binary config error, got: {err}")


def test_worker_scratch_is_per_run_unique(project: Path,
                                          monkeypatch) -> None:
    from godot_qa_toolkit.gherkin import mutate_runner as mr
    scratch_dirs: list[Path] = []
    real_mkdir = Path.mkdir

    def spy_mkdir(self, *a, **k):
        if "gqt-gherkin-worker" in self.name:
            scratch_dirs.append(self)
        return real_mkdir(self, *a, **k)

    monkeypatch.setattr(Path, "mkdir", spy_mkdir)
    # 两次 spawn 前奏（不真正拉起进程）：Popen 立即失败即可，
    # 关键是 scratch 目录名每次不同且处于项目 .qa-cache 下。
    monkeypatch.setattr(mr.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(
                            OSError("stop-after-scratch")))
    suite, error = _compile(project)
    assert error is None
    for _ in range(2):
        with pytest.raises(Exception):
            mr._spawn_worker(project)
    names = {p.name for p in scratch_dirs}
    assert len(scratch_dirs) >= 2 and len(names) >= 2, (
        f"worker scratch must be per-run unique, got {scratch_dirs}")


# ================= H7：旧 env/键/臂名 fail-loud =================

def test_old_acceptance_env_fails_loud_new_env_works(monkeypatch) -> None:
    from godot_qa_toolkit.measurement import orchestrator as orch
    monkeypatch.delenv("GQT_MEASUREMENT_GHERKIN_DISABLED", raising=False)
    monkeypatch.setenv("GQT_MEASUREMENT_ACCEPTANCE_DISABLED", "0")
    from godot_qa_toolkit.measurement.backup import MeasurementError
    with pytest.raises(MeasurementError, match="retired"):
        orch.gherkin_arm_disabled()
    monkeypatch.delenv("GQT_MEASUREMENT_ACCEPTANCE_DISABLED")
    monkeypatch.setenv("GQT_MEASUREMENT_GHERKIN_DISABLED", "0")
    assert orch.gherkin_arm_disabled() is False
    monkeypatch.delenv("GQT_MEASUREMENT_GHERKIN_DISABLED")
    assert orch.gherkin_arm_disabled() is True


def test_intolerant_gherkin_scenarios_and_old_key_rejected() -> None:
    from godot_qa_toolkit.measurement.acc_govern import \
        parse_gherkin_scenarios
    cfg = {"gherkin_scenarios": [
        {"feature": "f", "scenario": "s", "reason": "r"}]}
    assert parse_gherkin_scenarios(cfg)[0]["feature"] == "f"
    with pytest.raises(ValueError, match="retired"):
        parse_gherkin_scenarios({"acceptance_scenarios": [
            {"feature": "f", "scenario": "s", "reason": "r"}]})


def test_retired_acceptance_arm_name_fails_loud() -> None:
    from godot_qa_toolkit.coverage.evidence import _resolve_arm
    with pytest.raises(ValueError, match="retired"):
        _resolve_arm(None, "acceptance")
    assert _resolve_arm(None, "gherkin") == "gherkin"


# ================= H8：shadow 升版 + 旧键拒绝 =================

def test_shadow_schema_version_bumped() -> None:
    from godot_qa_toolkit.risk import shadow
    doc = shadow._report_document(
        ranking=[], unknown=[], core_full=[], noise=[], negatives=[],
        per_file=[], sample_files=0, negative_files=0)
    assert doc["version"] == "shadow/2"


def test_shadow_old_acceptance_cost_key_rejected(tmp_path: Path) -> None:
    from godot_qa_toolkit.risk import shadow
    with pytest.raises(Exception, match="acceptance_cost"):
        shadow.run_shadow_from_paths(
            paths=[], project_root=str(tmp_path),
            performance_input={"acceptance_cost": {"samples": [1.0]}})


# ================= H9：recipe 严格键 + 指纹必需 =================

def _recipe_of(project: Path) -> dict:
    from godot_qa_toolkit.measurement.recipe import build_recipe
    recipe, error = build_recipe(project)
    assert error is None
    return recipe


def test_recipe_requires_adapter_fingerprint(project: Path) -> None:
    from godot_qa_toolkit.measurement.recipe import (
        recipe_hash, verify_recipe)
    recipe = _recipe_of(project)
    forged = json.loads(json.dumps(recipe))
    del forged["gherkin_face"]["adapter_source_sha256"]
    forged["recipe_hash"] = recipe_hash(forged)
    ok, reason = verify_recipe(forged)
    assert ok is False and "adapter_source_sha256" in reason


def test_recipe_rejects_retired_acceptance_face(project: Path) -> None:
    from godot_qa_toolkit.measurement.recipe import (
        recipe_hash, verify_recipe)
    recipe = _recipe_of(project)
    forged = json.loads(json.dumps(recipe))
    forged["acceptance_face"] = forged["gherkin_face"]
    forged["recipe_hash"] = recipe_hash(forged)
    ok, reason = verify_recipe(forged)
    assert ok is False and "acceptance_face" in reason


def test_recipe_rejects_bad_fingerprint_shape(project: Path) -> None:
    from godot_qa_toolkit.measurement.recipe import (
        recipe_hash, verify_recipe)
    recipe = _recipe_of(project)
    forged = json.loads(json.dumps(recipe))
    forged["gherkin_face"]["adapter_source_sha256"] = "zz"
    forged["recipe_hash"] = recipe_hash(forged)
    ok, reason = verify_recipe(forged)
    assert ok is False and "adapter_source_sha256" in reason


# ================= M10：畸形 manifest → GherkinRunError ============

def test_malformed_manifest_is_named_run_error(project: Path) -> None:
    from godot_qa_toolkit.gherkin.run import GherkinRunError, replay_check
    suite, error = _compile(project)
    assert error is None
    man = project / "gherkin" / "generated" / "demo.manifest.json"
    man.write_text("{not json", encoding="utf-8")
    with pytest.raises(GherkinRunError, match="unreadable|corrupt"):
        replay_check(project, "demo")
    man.write_text("[]", encoding="utf-8")
    with pytest.raises(GherkinRunError, match="unreadable|corrupt"):
        replay_check(project, "demo")


# ================= M11：连字符 feature key 全链一致 ================

def test_hyphenated_feature_key_runs_in_key_mode(
        project: Path, monkeypatch) -> None:
    from godot_qa_toolkit.gherkin.addon import resolve_feature
    from godot_qa_toolkit.gherkin.compile import compile_feature
    from godot_qa_toolkit.gherkin.run import run_suite
    src = write_feature(project, "my-demo")
    suite, error = compile_feature(src, project, write=True)
    assert error is None
    manifest = json.loads(
        (project / "gherkin" / "generated" /
         "my_demo.manifest.json").read_text(encoding="utf-8"))
    flow = ["res://gherkin/generated/test_my_demo.gd"]
    for a in manifest["attribution"]:
        flow.append(f"* {a['test_name']}")
    _fake_godot(monkeypatch, gut_output="\n".join(flow) + "\n")
    result = run_suite(project, "my-demo")
    assert result["ok"] is True


# ================= M12：perf resume doc 校验 =======================

def test_perf_resume_rejects_old_doc() -> None:
    from godot_qa_toolkit.measurement.gherkin_perf import \
        merge_round_into_report
    doc = {"doc": "see1619-acceptance-perf/1", "rounds": []}
    rec = {"round": "A1", "mode": "shared", "valid": True,
           "gherkin_wall_s": 10.0}
    with pytest.raises(ValueError, match="retired|doc"):
        merge_round_into_report(doc, rec, 4, 44)


def test_perf_resume_accepts_current_doc() -> None:
    from godot_qa_toolkit.measurement.gherkin_perf import \
        merge_round_into_report
    doc = {"doc": "see1619-gherkin-perf/1", "rounds": []}
    rec = {"round": "A1", "mode": "shared", "valid": True,
           "gherkin_wall_s": 10.0}
    out = merge_round_into_report(doc, rec, 4, 44)
    assert len(out["rounds"]) == 1


def test_mutate_sentinel_detects_ansi_prefixed_test_stream() -> None:
    """真实 Godot 输出带 ANSI 颜色前缀——mutate 基线哨兵必须剥除转义
    后再计数（否则真实工程下"无测试流"假阴性 → 误报 run_error）。"""
    from godot_qa_toolkit.gherkin import mutate_runner as mr
    ansi = ("\x1b[0m* test_demo_000_00\x1b[0m\n"
            "\x1b[1m1/1 passed.\x1b[0m\n")
    assert "* test_" in ansi
    assert mr._count_test_stream(ansi) == 1
    # 基线轮执行集解析同一实现（曾直接 findall 未剥 ANSI → 真实工程假阴性）
    assert mr._test_stream_names(ansi) == {"test_demo_000_00"}
