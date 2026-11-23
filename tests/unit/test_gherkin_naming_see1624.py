"""SEE-1624 §SPEC-004/005 — 暂存区/worker/指纹与 D-n2~D-n5 全量改名。

- 暂存区 `gherkin/{ir,generated}`、worker `gherkin_worker.gd`、escape
  dir / 私有 suite / replay 同步；worker 与 worker_hash 在 replay 中
  必需（旧产物不按兼容路径放行）；
- `adapter_source_sha256` 递归覆盖 addon 全部 .gd/.feature（排除
  .uid/README/.json），任一字节变化即失效；
- 公共 flags/JSON 键/枚举/臂名/版本 token 改 gherkin；旧调用
  fail-loud；accept CLI 别名不存在。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

import godot_qa_toolkit.gherkin as gherkin_pkg
from godot_qa_toolkit.gherkin.addon import (
    SCHEMA_VERSION,
    resolve_feature,
)
from godot_qa_toolkit.gherkin.compile import (
    GENERATED_DIR,
    IR_DIR,
    compile_feature,
)
from godot_qa_toolkit.gherkin.run import GherkinRunError, replay_check
from godot_qa_toolkit.gherkin.suite import (
    PROBE_VERSION,
    SUITES_VERSION,
    create_suite_dir,
)
from godot_qa_toolkit.measurement.recipe import (
    RECIPE_VERSION,
    build_recipe,
    verify_recipe,
)

from gherkin_fixtures import make_addon_project, write_feature


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    return make_addon_project(tmp_path / "proj")


# ---------- 版本 token（D-n5）----------

def test_version_tokens_are_gherkin() -> None:
    assert gherkin_pkg.OPS_VERSION == "see1468-gherkin/2"
    assert SUITES_VERSION == "gherkin-suite/1"
    assert PROBE_VERSION == "gherkin-probe/1"
    assert RECIPE_VERSION == "measurement-suite/3"
    from godot_qa_toolkit.measurement.orchestrator import REPORT_VERSION
    assert REPORT_VERSION == "measurement-report/3"
    assert SCHEMA_VERSION == "gqt-gherkin-addon/1"


def test_gherkin_run_error_is_the_active_name() -> None:
    import godot_qa_toolkit.gherkin.run as run_mod
    assert hasattr(run_mod, "GherkinRunError")
    assert not hasattr(run_mod, "AcceptRunError")


# ---------- 暂存区与 worker（D-n2 + 活跃标识）----------

def test_staging_constants_are_gherkin() -> None:
    assert IR_DIR == "gherkin/ir"
    assert GENERATED_DIR == "gherkin/generated"


def test_private_suite_dir_lands_under_gherkin(project: Path) -> None:
    d = create_suite_dir(project)
    rel = d.relative_to(project).as_posix()
    assert rel.startswith("gherkin/generated/.gqt-run-")


def test_escape_dirs_cover_gherkin_generated() -> None:
    from godot_qa_toolkit.registry.snapshot import DEFAULT_ESCAPE_DIRS
    assert "gherkin/generated/" in DEFAULT_ESCAPE_DIRS
    assert "acceptance/generated/" not in DEFAULT_ESCAPE_DIRS


def _compile_project(project: Path) -> dict:
    src = write_feature(project, "demo")
    suite, error = compile_feature(src, project, write=True)
    assert error is None
    return suite.manifest


def test_worker_artifact_named_gherkin_worker(project: Path) -> None:
    manifest = _compile_project(project)
    assert manifest["worker_path"] == \
        "res://gherkin/generated/gherkin_worker.gd"
    assert (project / "gherkin" / "generated" /
            "gherkin_worker.gd").is_file()
    assert not (project / "gherkin" / "generated" /
                "accept_worker.gd").exists()


def test_replay_requires_worker_artifact(project: Path) -> None:
    _compile_project(project)
    (project / "gherkin" / "generated" /
     "gherkin_worker.gd").unlink()
    with pytest.raises(GherkinRunError, match="worker artifact missing"):
        replay_check(project, "demo")


def test_replay_requires_worker_hash_in_manifest(project: Path) -> None:
    manifest = _compile_project(project)
    man_path = (project / "gherkin" / "generated" /
                "demo.manifest.json")
    man = json.loads(man_path.read_text(encoding="utf-8"))
    del man["worker_hash"]
    man_path.write_text(json.dumps(man, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    with pytest.raises(GherkinRunError, match="worker_hash"):
        replay_check(project, "demo")


def test_replay_rejects_pre_migration_ops_version(project: Path) -> None:
    _compile_project(project)
    man_path = (project / "gherkin" / "generated" /
                "demo.manifest.json")
    man = json.loads(man_path.read_text(encoding="utf-8"))
    man["ops_version"] = "see1468-accept/2"
    man_path.write_text(json.dumps(man, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    with pytest.raises(GherkinRunError, match="ops_version"):
        replay_check(project, "demo")


# ---------- recipe face（D-n4 键 + adapter 指纹）----------

def test_recipe_has_gherkin_face_and_no_acceptance_face(
        project: Path) -> None:
    recipe, error = build_recipe(project)
    assert error is None
    assert "gherkin_face" in recipe
    assert "acceptance_face" not in recipe
    face = recipe["gherkin_face"]
    assert face["harness"]["worker"] == "generated/gherkin_worker.gd"
    assert face["harness"]["wiring_sha256"] != "missing"
    assert face["count"] == 1
    assert face["features"][0]["source"].startswith(
        "res://addons/gqt_gherkin/features/")


def test_recipe_scope_enum_uses_gherkin_feature(project: Path) -> None:
    recipe, error = build_recipe(
        project, exclusions=[
            {"scope": "gherkin_feature", "entry": "demo",
             "reason": "excluded for the naming pin"}])
    assert error is None
    assert recipe["gherkin_face"]["excluded"][0]["key"] == "demo"
    old, old_error = build_recipe(
        project, exclusions=[
            {"scope": "acceptance_feature", "entry": "demo",
             "reason": "old scope name must be rejected"}])
    assert old is None and old_error is not None
    assert "illegal exclusion scope" in old_error


def test_recipe_version_gate_rejects_old_major(project: Path) -> None:
    recipe, error = build_recipe(project)
    assert error is None
    old = dict(recipe)
    old["recipe_version"] = "measurement-suite/2"
    ok, reason = verify_recipe(old)
    assert ok is False and reason is not None
    assert "recipe_version" in reason


def _adapter_hash(project: Path) -> str:
    recipe, error = build_recipe(project)
    assert error is None
    return recipe["gherkin_face"]["adapter_source_sha256"]


def test_adapter_fingerprint_covers_feature_bytes(project: Path) -> None:
    write_feature(project, "demo")
    before = _adapter_hash(project)
    feat = project / "addons" / "gqt_gherkin" / "features" / "demo.feature"
    feat.write_text(
        feat.read_text(encoding="utf-8") + "\n# touched\n",
        encoding="utf-8")
    assert _adapter_hash(project) != before


def test_adapter_fingerprint_covers_domain_and_validator_gd(
        project: Path) -> None:
    write_feature(project, "demo")
    before = _adapter_hash(project)
    (project / "addons" / "gqt_gherkin" / "harness.gd").write_text(
        "# changed harness byte\n", encoding="utf-8")
    mid = _adapter_hash(project)
    assert mid != before
    (project / "addons" / "gqt_gherkin" /
     "validator.gd").write_text(
        "# changed validator byte\nextends SceneTree\n",
        encoding="utf-8")
    assert _adapter_hash(project) != mid


def test_adapter_fingerprint_ignores_uid_readme_json(
        project: Path) -> None:
    write_feature(project, "demo")
    before = _adapter_hash(project)
    addon = project / "addons" / "gqt_gherkin"
    (addon / "harness.gd.uid").write_text("uid\n", encoding="utf-8")
    (addon / "README.md").write_text("# doc\n", encoding="utf-8")
    schema = addon / "schema.json"
    schema.write_text(
        json.dumps(json.loads(schema.read_text(encoding="utf-8")),
                   indent=4),
        encoding="utf-8")
    assert _adapter_hash(project) == before


def test_adapter_fingerprint_missing_addon_fails_recipe(
        tmp_path: Path) -> None:
    import shutil
    project = make_addon_project(tmp_path / "proj", features={})
    shutil.rmtree(project / "addons" / "gqt_gherkin")
    recipe, error = build_recipe(project)
    assert recipe is None and error is not None
    assert "addons/gqt_gherkin" in error


# ---------- CLI flags（D-n3）----------

def _parser() -> argparse.ArgumentParser:
    from godot_qa_toolkit.cli import build_parser
    return build_parser()


def test_new_gherkin_flags_parse() -> None:
    p = _parser()
    args = p.parse_args([
        "measure", "run", "src", "--recipe", "r.json", "--out", "o.json",
        "--gherkin-probe-keys", "demo", "--gherkin-shards", "2",
        "--gherkin-repeats", "3", "--gherkin-adaptive"])
    assert args.gherkin_probe_keys == "demo"
    assert args.gherkin_shards == 2
    assert args.gherkin_repeats == 3
    assert args.gherkin_adaptive is True
    run_args = p.parse_args([
        "gherkin", "run", "--project-root", ".", "--gherkin-probe-keys",
        "demo"])
    assert run_args.gherkin_probe_keys == "demo"


@pytest.mark.parametrize("old_flag", [
    ["--acceptance-probe-keys"],
    ["--acceptance-shards", "2"],
    ["--acceptance-repeats", "3"],
    ["--acceptance-adaptive"],
])
def test_old_acceptance_flags_are_rejected(old_flag: list) -> None:
    p = _parser()
    with pytest.raises(SystemExit) as exc:
        p.parse_args(["measure", "run", "src", "--recipe", "r.json",
                      "--out", "o.json"] + old_flag)
    assert exc.value.code == 2


def test_no_accept_cli_alias() -> None:
    p = _parser()
    with pytest.raises(SystemExit) as exc:
        p.parse_args(["accept", "run", "--project-root", "."])
    assert exc.value.code == 2
