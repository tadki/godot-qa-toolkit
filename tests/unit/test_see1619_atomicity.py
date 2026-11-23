"""SEE-1619 §SPEC-002 — 生成 GUT 测试 before/after 对称生命周期单测。

harness.reset() 唯一位点在 after_each（空值护栏），不再依赖普通测试体
末尾语句：init 失败、断言红、测试体提前 return 与后续场景均经
after_each 可靠还原；before_each 以 smoke_check 供复位证据。既有
IR/manifest/replay 语义不变（哈希随生成文本自洽重绑），私有 suite
五项对账与 GUT hook 排除计数不退化。godot 真实 headless 执行由 QA
实机复验，本套件 mock 子进程、对生成物做结构断言。
"""

from __future__ import annotations

import hashlib
import subprocess as sp
from pathlib import Path

import pytest

from godot_qa_toolkit.gherkin.compile import compile_feature
from godot_qa_toolkit.gherkin.suite import (
    compile_keys_to_suite,
    create_suite_dir,
    run_suite_dir,
)

FEATURE = """Feature: 生命值扣减
  Scenario Outline: 受到 <dmg> 点伤害
    Given hero hp is <hp>
    When hero takes <dmg> damage
    Then hero hp is <left>

    # types: <hp>:int, <dmg>:int, <left>:numeric
    Examples:
      | hp | dmg | left |
      | 10 | 3   | 7    |
      | 20 | 20  | 0    |
"""

PLAIN_FEATURE = """Feature: 纯场景
  Scenario: 简单通过
    Given world exists
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
    (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut stub\n")
    from gherkin_fixtures import ensure_addon_metadata
    ensure_addon_metadata(root)
    return root


def _compile(project: Path, tmp_path: Path, feature: str = FEATURE):
    f = (project / "addons" / "gqt_gherkin" / "features" /
         "demo.feature")
    f.parent.mkdir(parents=True, exist_ok=True)
    from gherkin_fixtures import VALID_SCHEMA, VALIDATOR_OK_GD
    import json as _json
    _schema = project / "addons" / "gqt_gherkin" / "schema.json"
    if not _schema.is_file():
        _schema.parent.mkdir(parents=True, exist_ok=True)
        _schema.write_text(_json.dumps(VALID_SCHEMA), encoding="utf-8")
    _validator = project / "addons" / "gqt_gherkin" / "validator.gd"
    if not _validator.is_file():
        _validator.write_text(VALIDATOR_OK_GD, encoding="utf-8")
    f.write_text(feature, encoding="utf-8")
    suite, error = compile_feature(f, project, write=False)
    assert error is None
    return suite


# ---------- 生成物结构断言：对称生命周期在位 ----------

def test_generated_has_both_lifecycle_hooks(project: Path, tmp_path: Path) -> None:
    body = _compile(project, tmp_path).generated_gd
    assert "func before_each() -> void:" in body, "before_each 必须在位"
    assert "func after_each() -> void:" in body, "after_each 必须在位"


def test_reset_single_site_owned_by_after_each(project: Path, tmp_path: Path) -> None:
    body = _compile(project, tmp_path).generated_gd
    assert body.count("_harness.reset()") == 1, \
        "reset 必须唯一位点，不得散落测试体"
    after = body.split("func after_each() -> void:", 1)[1]
    after = after.split("\nfunc ", 1)[0]
    assert "_harness.reset()" in after
    # 空值护栏：harness 未创建（init 失败）无可还原，跳过而非崩。
    guard = after.split("_harness.reset()", 1)[0]
    assert "if _harness != null:" in guard, "after_each reset 必须带空值护栏"


def test_all_body_paths_own_no_reset(project: Path, tmp_path: Path) -> None:
    """pass / errs 红 / 提前 return 三类路径均不自带 reset——提前 return
    （harness 不可用、case 缺 IR）与断言红（fail_test 循环 + 正断言后
    落到方法尾）由 GUT 每测试必调的 after_each 统一还原。"""
    body = _compile(project, tmp_path).generated_gd
    run_case = body.split("func _run_case_by_name", 1)[1].split("\nfunc ", 1)[0]
    assert "_harness.reset()" not in run_case, \
        "_run_case_by_name 不得自带 reset（提前 return 会绕过）"
    before = body.split("func before_each() -> void:", 1)[1].split("\nfunc ", 1)[0]
    assert "_harness.reset()" not in before
    for sentinel in ('fail_test("gherkin harness unavailable',
                     "case missing from IR"):
        idx = body.index(sentinel)
        seg = body[idx:]
        seg = seg[:seg.index("\nfunc ")]
        assert "return" in seg, "early-return 路径必须存在（覆盖提前返回类）"


def test_before_each_carries_post_reset_evidence(
        project: Path, tmp_path: Path) -> None:
    body = _compile(project, tmp_path).generated_gd
    before = body.split("func before_each() -> void:", 1)[1].split("\nfunc ", 1)[0]
    assert "smoke_check()" in before, "复位证据必须经 smoke_check"
    assert "fail_test(" in before, "复位破坏必须显式红且具名"
    assert "if _harness != null" in before, "首个场景前无前置可证，护栏跳过"


def test_existing_smoke_sentinels_preserved(project: Path, tmp_path: Path) -> None:
    body = _compile(project, tmp_path).generated_gd
    assert "if not h.smoke_check():" in body
    assert 'fail_test("gherkin harness unavailable' in body
    assert "case missing from IR" in body
    assert body.index("for e in errs:") < body.index("assert_true(errs.is_empty()"), \
        "错误传播先于正断言（既有顺序语义不变）"


def test_manifest_hash_rebinding_with_new_lifecycle(
        project: Path, tmp_path: Path) -> None:
    suite = _compile(project, tmp_path)
    m = suite.manifest
    assert m["generated_hash"] == hashlib.sha256(
        suite.generated_gd.encode()).hexdigest(), \
        "哈希随生成文本自洽重绑（replay 语义不变）"


# ---------- 私有 suite 五项对账 + hook 排除计数不退化 ----------

def _gut_ok_output_with_hooks(rec: dict) -> str:
    """真机形态 + 生命周期 hook 行：before_each/after_each 每测试各一行，
    不得计入 executed 计数，也不破坏五项对账。"""
    sections = []
    for key, man in sorted(rec["manifests"].items()):
        lines = [f"res://gherkin/generated/.gqt-run-x/generated/"
                 f"test_{key}.gd"]
        for a in man["attribution"]:
            lines.append("* before_each")
            lines.append(f"* {a['test_name']}")
            lines.append("  [Passed]")
            lines.append("* after_each")
        sections.append("\n".join(lines))
    return "\n\n".join(sections) + "\n"


def test_suite_reconciliation_unchanged_with_hooks(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "addons" / "gut").mkdir(parents=True)
    (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
    feats = root / "addons" / "gqt_gherkin" / "features"
    feats.mkdir(parents=True)
    (feats / "demo.feature").write_text(FEATURE, encoding="utf-8")
    from gherkin_fixtures import ensure_addon_metadata
    ensure_addon_metadata(root)
    d = create_suite_dir(root)
    rec = compile_keys_to_suite(root, ["demo"], d)
    rec = dict(rec)

    class R:
        returncode = 0
        stdout = _gut_ok_output_with_hooks(rec)
        stderr = ""

    def _routed_run(*a, **k):
        # SEE-1624：入口预检的 validator 拉起回正例 verdict；GUT 拉起回
        # 钩子运行流输出。
        argv_s = " ".join(str(x) for x in (a[0] if a else []))
        if "validator.gd" in argv_s:
            from gherkin_fixtures import fake_validator_process
            return fake_validator_process()
        return R()

    sp.run = _routed_run
    record = run_suite_dir(root, Path(rec["suite_dir"]))
    assert record["ok"] is True
    # hook 排除：executed 计数 == 生成用例数，before_each/after_each 不计。
    assert len(record["executed_tests"]) == len(
        rec["manifests"]["demo"]["attribution"])
    assert not any(n in ("before_each", "after_each")
                   for n in record["executed_tests"])
