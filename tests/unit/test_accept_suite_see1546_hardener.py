"""SEE-1546 WP3 PR-B hardener（§SPEC-035）——suite.py 防御分支补测。

覆盖既有 22 用例未触及的 fail-loud / 生命周期 / 解析停止线 / 重放语义
分支，使 `accept/suite.py` 行+分支覆盖率达到 100%（无豁免项）：

  - 私有目录：features 目录缺失、编译写盘缺失、无 manifest、GUT runner
    缺失、GUT 超时、GUT 启动失败；
  - 对账：脚本段停止线（Run Summary）、主目录既有产物下的字节快照与
    父目录保留、无快照入参 + 重放未勾选时 ok=false；
  - 真机锚定解析：钩子排除归既有用例守护，此处补 Summary 停止线。

只新增测试，产品代码零改动。
"""

from __future__ import annotations

import subprocess as sp
from pathlib import Path

import pytest

import godot_qa_toolkit.accept.suite as suite_mod
from godot_qa_toolkit.accept.run import AcceptRunError
from godot_qa_toolkit.accept.suite import (
    SuiteError,
    _executed_scripts,
    compile_keys_to_suite,
    create_suite_dir,
    reconcile,
    run_suite_dir,
)

FEATURE = """Feature: F
  Scenario Outline: s
    Then hp is <left>
    # types: <hp>:int, <left>:numeric
    Examples:
      | hp | left |
      | 10 | 7    |
"""


@pytest.fixture(autouse=True)
def _restore_subprocess():
    orig_run = sp.run
    yield
    sp.run = orig_run


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "addons" / "gut").mkdir(parents=True)
    (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
    feats = root / "acceptance" / "features"
    feats.mkdir(parents=True)
    for name in ("demo", "demo_x", "a", "ab"):
        (feats / f"{name}.feature").write_text(FEATURE, encoding="utf-8")
    return root


def _fake_gut(output: str):
    def fake_run(*a, **k):
        class R:
            returncode = 0
            stdout = output
            stderr = ""

        return R()

    sp.run = fake_run


def _gut_ok_output(rec: dict) -> str:
    sections = []
    for key, man in sorted(rec["manifests"].items()):
        lines = [f"res://acceptance/generated/.gqt-run-x/generated/"
                 f"test_{key}.gd"]
        for a in man["attribution"]:
            lines.append(f"* {a['test_name']}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections) + "\n"


# ------------------------------------------------------------------
# 私有目录 fail-loud 分支
# ------------------------------------------------------------------


def test_compile_keys_missing_features_dir_is_unknown_key(tmp_path: Path):
    """features 目录缺失 → _resolve_feature 返回 None → unknown key。"""
    root = tmp_path / "bare"
    root.mkdir()
    d = create_suite_dir(root)
    with pytest.raises(SuiteError, match="unknown key: demo"):
        compile_keys_to_suite(root, ["demo"], d)


def test_compile_keys_compile_error_is_suite_error(project: Path):
    """选中 key 的 .feature 语法错误 → compile_feature 报错 → SuiteError。"""
    (project / "acceptance" / "features" / "bad.feature").write_text(
        "this is not gherkin\n", encoding="utf-8")
    d = create_suite_dir(project)
    with pytest.raises(SuiteError, match="compile failed for bad"):
        compile_keys_to_suite(project, ["bad"], d)


def test_compile_keys_missing_written_file_is_suite_error(
        project: Path, monkeypatch: pytest.MonkeyPatch):
    """编译声称成功却不落 test_<key>.gd → 写盘自检 fail-loud。"""

    class _FakeSuite:
        manifest = {"attribution": []}

    monkeypatch.setattr(
        suite_mod, "compile_feature",
        lambda *a, **k: (_FakeSuite(), None))
    d = create_suite_dir(project)
    with pytest.raises(SuiteError, match="compile wrote no"):
        compile_keys_to_suite(project, ["demo"], d)


def test_compile_keys_out_dir_outside_project_is_suite_error(
        project: Path, tmp_path: Path):
    """私有目录必须位于项目内（res:// 解析约束）——越界即 fail-loud。"""
    outside = tmp_path / "outside"
    with pytest.raises(SuiteError, match="out_dir must live inside the project"):
        compile_keys_to_suite(project, ["demo"], outside)


# ------------------------------------------------------------------
# 运行期 fail-loud 分支
# ------------------------------------------------------------------


def test_run_suite_dir_without_manifests_is_suite_error(project: Path):
    d = create_suite_dir(project)
    with pytest.raises(SuiteError, match="no compiled manifests"):
        run_suite_dir(project, d)


def test_run_suite_dir_missing_gut_runner_is_run_error(project: Path):
    rec = compile_keys_to_suite(project, ["demo"], create_suite_dir(project))
    (project / "addons" / "gut" / "gut_cmdln.gd").unlink()
    with pytest.raises(AcceptRunError, match="GUT runner not found"):
        run_suite_dir(project, Path(rec["suite_dir"]))


def test_run_suite_dir_timeout_is_run_error(project: Path):
    rec = compile_keys_to_suite(project, ["demo"], create_suite_dir(project))

    def fake_run(*a, **k):
        raise sp.TimeoutExpired(cmd="godot", timeout=1)

    sp.run = fake_run
    with pytest.raises(AcceptRunError, match="GUT timed out"):
        run_suite_dir(project, Path(rec["suite_dir"]))


def test_run_suite_dir_launch_failure_is_run_error(project: Path):
    rec = compile_keys_to_suite(project, ["demo"], create_suite_dir(project))
    _fake_gut("error: failed to load script res://x.gd\n")
    with pytest.raises(AcceptRunError, match="godot launch failed"):
        run_suite_dir(project, Path(rec["suite_dir"]))


# ------------------------------------------------------------------
# 对账：停止线 / 主目录快照 / 重放未勾选
# ------------------------------------------------------------------


def test_executed_scripts_stops_at_run_summary_marker():
    """Run Summary 之后的段不再计入运行流（Summary 段 `-` 前缀会重复）。"""
    out = (
        "res://a/test_demo.gd\n"
        "* t1\n"
        "Run Summary\n"
        "res://a/test_demo.gd\n"
        "* t2\n"
    )
    flow = _executed_scripts(out)
    assert flow == {"res://a/test_demo.gd": ["t1"]}


def test_cleanup_preserves_preexisting_main_artifacts(project: Path):
    """主 generated 已有产物 → 字节快照纳入、父目录 rmdir 失败原样保留、
    私有目录仍清理为零残留。"""
    main_ir = project / "acceptance" / "ir"
    main_gen = project / "acceptance" / "generated"
    main_ir.mkdir(parents=True)
    main_gen.mkdir(parents=True)
    (main_ir / "kept.json").write_text("{}", encoding="utf-8")
    (main_gen / "kept.txt").write_text("keep-me", encoding="utf-8")
    rec = compile_keys_to_suite(project, ["demo"], create_suite_dir(project))
    _fake_gut(_gut_ok_output(rec))
    record = run_suite_dir(project, Path(rec["suite_dir"]))
    assert record["ok"] is True
    assert record["cleanup"] == "removed"
    assert not list(main_gen.glob(".gqt-run-*"))
    assert (main_ir / "kept.json").read_text(encoding="utf-8") == "{}"
    assert (main_gen / "kept.txt").read_text(encoding="utf-8") == "keep-me"


def test_reconcile_without_snapshot_and_replay_false_is_not_ok(project: Path):
    """无 main_snapshot 入参 → 现场取快照；replay_ok 缺省 False → ok=False
    且 replay_checked=False（R5 守卫，不可被钉常量吞掉）。"""
    rec = compile_keys_to_suite(project, ["demo"], create_suite_dir(project))
    out = _gut_ok_output(rec)
    record = reconcile(project, Path(rec["suite_dir"]), ["demo"], out)
    assert record["rules"]["replay_checked"] is False
    assert record["ok"] is False
    assert record["rules"]["generated_eq_selected"] is True
    assert record["rules"]["executed_scripts_eq_generated"] is True
    assert record["rules"]["executed_count_eq_expected"] is True
    assert record["rules"]["unselected_absent"] is True
