"""SEE-1624 §SPEC-002 — 严格单根 addon 布局（发现/加载位/拒绝面）。

- 只从 `addons/gqt_gherkin/` 发现 features、加载 harness；
- 根外 .feature 显式拒绝；缺根/空集/重复 key/接口缺失 fail-loud；
- `addons/gqt_accept`、`acceptance/features`、`acceptance/harness`
  不再是合法输入：无双根、无回退、无自动复制，旧布局零新产物。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from godot_qa_toolkit.gherkin.addon import (
    AddonError,
    resolve_feature,
)
from godot_qa_toolkit.gherkin.compile import compile_feature

from gherkin_fixtures import (
    FEATURE,
    SECOND_FEATURE,
    make_addon_project,
    write_feature,
)


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    return make_addon_project(tmp_path / "proj")


# ---------- 有效单根 ----------

def test_valid_single_root_compiles_into_gherkin_staging(
        project: Path) -> None:
    src = write_feature(project, "demo")
    suite, error = compile_feature(src, project, write=True)
    assert error is None
    assert (project / "gherkin" / "ir" / "demo.json").is_file()
    assert (project / "gherkin" / "generated" / "test_demo.gd").is_file()
    assert (project / "gherkin" / "generated" /
            "demo.manifest.json").is_file()
    assert (project / "gherkin" / "generated" /
            "gherkin_worker.gd").is_file()
    # 旧暂存路径零产物（无双写）
    assert not (project / "acceptance" / "ir").exists()
    assert not (project / "acceptance" / "generated").exists()


def test_manifest_worker_path_points_at_gherkin_worker(
        project: Path) -> None:
    src = write_feature(project, "demo")
    suite, error = compile_feature(src, project, write=True)
    assert error is None
    assert suite.manifest["worker_path"] == \
        "res://gherkin/generated/gherkin_worker.gd"


def test_generated_gd_loads_harness_from_single_root(
        project: Path) -> None:
    src = write_feature(project, "demo")
    suite, error = compile_feature(src, project, write=True)
    assert error is None
    assert "res://addons/gqt_gherkin/harness.gd" in suite.generated_gd
    assert "gqt_accept" not in suite.generated_gd


def test_worker_gd_loads_harness_from_single_root(project: Path) -> None:
    src = write_feature(project, "demo")
    suite, error = compile_feature(src, project, write=True)
    assert error is None
    assert "res://addons/gqt_gherkin/harness.gd" in suite.worker_gd
    assert "gqt_accept" not in suite.worker_gd


# ---------- 根外 .feature 显式拒绝 ----------

def test_feature_outside_root_is_rejected(project: Path,
                                          tmp_path: Path) -> None:
    stray = tmp_path / "stray.feature"
    stray.write_text(FEATURE, encoding="utf-8")
    suite, error = compile_feature(stray, project, write=True)
    assert suite is None and error is not None
    assert "addons/gqt_gherkin/features" in error["reason"]
    assert not (project / "gherkin").exists()


def test_feature_in_project_but_outside_addon_root_rejected(
        project: Path) -> None:
    rogue = project / "acceptance" / "features" / "demo.feature"
    rogue.parent.mkdir(parents=True, exist_ok=True)
    rogue.write_text(FEATURE, encoding="utf-8")
    suite, error = compile_feature(rogue, project, write=True)
    assert suite is None and error is not None
    assert not (project / "gherkin").exists()


# ---------- 缺根 / 空集 ----------

def test_missing_features_root_fails_loud(tmp_path: Path) -> None:
    project = make_addon_project(tmp_path / "proj", features={})
    (project / "addons" / "gqt_gherkin" / "features").rmdir()
    with pytest.raises(AddonError, match="addons/gqt_gherkin/features"):
        resolve_feature(project, "demo")


def test_empty_feature_set_fails_loud(tmp_path: Path) -> None:
    project = make_addon_project(tmp_path / "proj", features={})
    with pytest.raises(AddonError, match="no .feature"):
        resolve_feature(project, "demo")
    with pytest.raises(AddonError):
        resolve_feature(project, "anything")


def test_unknown_key_named_error(project: Path) -> None:
    with pytest.raises(AddonError, match="unknown key"):
        resolve_feature(project, "nope")


def test_resolve_feature_finds_key(project: Path) -> None:
    write_feature(project, "demo")
    f = resolve_feature(project, "demo")
    assert f.name == "demo.feature"
    assert f.parent.name == "features"


# ---------- 旧布局不再是合法输入 ----------

def test_legacy_gqt_accept_layout_is_not_an_input(
        tmp_path: Path) -> None:
    # 旧根：addons/gqt_accept + acceptance/features + acceptance/harness
    project = make_addon_project(tmp_path / "proj", features={})
    (project / "addons" / "gqt_accept").mkdir(parents=True)
    (project / "addons" / "gqt_accept" / "harness.gd").write_text(
        "# legacy\n", encoding="utf-8")
    legacy = project / "acceptance" / "features" / "demo.feature"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(FEATURE, encoding="utf-8")
    (project / "acceptance" / "harness").mkdir(parents=True)
    (project / "acceptance" / "harness" / "harness.gd").write_text(
        "# legacy\n", encoding="utf-8")
    with pytest.raises(AddonError, match="no .feature"):
        resolve_feature(project, "demo")
    suite, error = compile_feature(legacy, project, write=True)
    assert suite is None and error is not None
    # 零新产物：gherkin/ 与 acceptance/{ir,generated} 都不得出现
    assert not (project / "gherkin").exists()
    assert not (project / "acceptance" / "ir").exists()
    assert not (project / "acceptance" / "generated").exists()


def test_no_auto_copy_from_legacy_harness(tmp_path: Path) -> None:
    # 旧 harness 源在场也不自动复制进新根（R2 §3 连带简化：cp 接线退役）
    project = make_addon_project(tmp_path / "proj", harness=False)
    legacy = project / "acceptance" / "harness" / "harness.gd"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("# legacy harness\n", encoding="utf-8")
    assert not (project / "addons" / "gqt_gherkin" / "harness.gd").exists()


# ---------- 重复 key / 接口缺失 ----------

def test_duplicate_feature_key_conflict_named(project: Path) -> None:
    src = write_feature(project, "demo")
    suite, error = compile_feature(src, project, write=True)
    assert error is None
    # 陈旧 manifest 模拟：key 仍绑定旧布局的 source_path（迁移前产物），
    # 同 stem 新源重编译必须具名冲突拒绝（不得静默覆盖绑定）。
    man_path = (project / "gherkin" / "generated" / "demo.manifest.json")
    man = json.loads(man_path.read_text(encoding="utf-8"))
    man["source_path"] = str(
        project / "acceptance" / "features" / "demo.feature")
    man_path.write_text(json.dumps(man, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    src2 = write_feature(project, "demo", SECOND_FEATURE)
    suite2, error2 = compile_feature(src2, project, write=True)
    assert suite2 is None and error2 is not None
    assert "feature_key conflict" in error2["reason"]


def test_harness_interface_missing_fails_recipe_and_generation_names_root(
        tmp_path: Path) -> None:
    # 接口缺失：生成测试的加载位指向单根（run 侧 fail_test 文案锚定），
    # recipe 侧 wiring 缺文件 fail-loud（不再退化为 "missing" 常量）。
    project = make_addon_project(tmp_path / "proj", harness=False)
    src = write_feature(project, "demo")
    suite, error = compile_feature(src, project, write=True)
    assert error is None
    assert "res://addons/gqt_gherkin/harness.gd" in suite.generated_gd

    from godot_qa_toolkit.measurement.recipe import build_recipe
    recipe, err = build_recipe(project)
    assert recipe is None and err is not None
    assert "addons/gqt_gherkin/harness.gd" in err


def test_out_dir_branch_still_resolves_from_single_root(
        project: Path, tmp_path: Path) -> None:
    write_feature(project, "demo")
    out = project / "gherkin" / "generated" / ".gqt-run-test"
    suite, error = compile_feature(
        resolve_feature(project, "demo"), project, write=True,
        out_dir=out)
    assert error is None
    assert (out / "ir" / "demo.json").is_file()
    assert (out / "generated" / "gherkin_worker.gd").is_file()


def test_clean_then_generate_keeps_staging_whitelisted(
        project: Path) -> None:
    write_feature(project, "demo")
    _, error = compile_feature(resolve_feature(project, "demo"),
                               project, write=True)
    assert error is None
    stale = project / "gherkin" / "ir" / "stale.json"
    stale.write_text("{}", encoding="utf-8")
    _, error = compile_feature(resolve_feature(project, "demo"),
                               project, write=True)
    assert error is None
    assert not stale.exists()
