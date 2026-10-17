"""Round A 必修面 §SPEC-1426-22~27 单测。

-22 非 Outline {x} 残留 parse error；-23 Examples 重复列名 parse error；
-24 run 侧 source-feature 重编译哈希核对（重锚对抗样本必打回）+
tests_executed 守卫；-25 失败 reason 携带 unchanged 违规明细；
-26 arch seed() 规则；-27 feature_key 冲突显式报错。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from godot_qa_toolkit.accept.compile import compile_feature
from godot_qa_toolkit.accept.run import AcceptRunError, replay_check, run_suite
from godot_qa_toolkit.arch.collector import collect_file
from godot_qa_toolkit.gherkin.parser import GherkinSyntaxError, parse


# ---------- §SPEC-1426-22 ----------

def test_non_outline_braces_residual_is_parse_error() -> None:
    with pytest.raises(GherkinSyntaxError, match="compile track only accepts <x>"):
        parse("Feature: d\n  Scenario: s\n    Given hp is {hp}\n")


# ---------- §SPEC-1426-23 ----------

def test_duplicate_examples_columns_is_parse_error() -> None:
    src = """Feature: d
  Scenario Outline: s <hp>
    Then hp is <hp>
    # types: <hp>:int
    Examples:
      | hp | hp |
      | 10 | 20 |
"""
    with pytest.raises(GherkinSyntaxError, match="duplicate Examples column"):
        parse(src)


# ---------- §SPEC-1426-24 重锚 + tests_executed ----------

FEATURE = """Feature: F
  Scenario: s
    Given a thing
"""

GOOD_RUN_OUTPUT = "* test_probe_000_00\n  [Passed]\n"


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "addons" / "gut").mkdir(parents=True)
    (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
    return root


def _compile(project: Path, tmp_path: Path) -> Path:
    f = tmp_path / "demo.feature"
    f.write_text(FEATURE, encoding="utf-8")
    suite, error = compile_feature(f, project, write=True)
    assert error is None
    return f


def test_reanchor_tampered_manifest_rejected(project: Path, tmp_path: Path) -> None:
    """重锚/漂移防线：源 .feature 缺失或被改而三件哈希自洽 → 必打回。

    防线语义（Atlas 裁决采纳的 v1 范围）：run 侧把 manifest.source_path
    的源 .feature 现场重编译，核对 IR 哈希一致——锚定回源文件。攻击者
    连源文件一起整体替换的场景属字段级篡改残余风险（v2 stamp 预登记）。
    """
    src_feature = _compile(project, tmp_path)
    # 攻击形态 A：删掉源 .feature，保住三件自洽产物
    src_feature.unlink()
    with pytest.raises(AcceptRunError, match="source feature missing"):
        replay_check(project, "demo")
    # 攻击形态 B：源 .feature 被改（期望值漂移），旧产物三件哈希仍自洽
    src_feature.write_text(
        "Feature: F\n  Scenario: s\n    Given a CHANGED thing\n", encoding="utf-8")
    with pytest.raises(AcceptRunError, match="recompile hash mismatch"):
        replay_check(project, "demo")


def test_tests_executed_zero_guard(project: Path, tmp_path: Path,
                                   monkeypatch) -> None:
    _compile(project, tmp_path)
    from godot_qa_toolkit.accept import run as run_mod

    def fake_run(*a, **k):
        class R:
            returncode = 0
            stdout = ""  # GUT 什么都没跑（Nothing was run）
            stderr = ""
        return R()
    monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
    result = run_suite(project, "demo")
    assert result["ok"] is False
    assert result["summary"]["tests_executed"] == 0
    assert "tests_executed=0" in result["failures"][0]["reason"]


def test_tests_executed_positive_in_summary(project: Path, tmp_path: Path,
                                            monkeypatch) -> None:
    _compile(project, tmp_path)
    from godot_qa_toolkit.accept import run as run_mod

    def fake_run(*a, **k):
        class R:
            returncode = 0
            stdout = "* test_probe_000_00\n  [Passed]\n- test_probe_000_00\n"
            stderr = ""
        return R()
    monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
    result = run_suite(project, "demo")
    assert result["ok"] is True
    assert result["summary"]["tests_executed"] == 1


# ---------- §SPEC-1426-25 ----------

def test_unchanged_violation_detail_in_reason(project: Path, tmp_path: Path,
                                              monkeypatch) -> None:
    f = tmp_path / "demo.feature"
    f.write_text("Feature: F\n  Scenario: s\n    Given a thing\n", encoding="utf-8")
    suite, error = compile_feature(f, project, write=True)
    assert error is None
    from godot_qa_toolkit.accept import run as run_mod
    tn = suite.manifest["attribution"][0]["test_name"]

    def fake_run(*a, **k):
        class R:
            returncode = 1
            stdout = (f"* {tn}\n    [Failed]:  unchanged slot violated: world.tick\n"
                      "  [Failed]\n")
            stderr = ""
        return R()
    monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
    result = run_suite(project, "demo")
    assert result["ok"] is False
    assert any("unchanged slot violated: world.tick" == f.get("detail")
               for f in result["failures"])


# ---------- §SPEC-1426-26 ----------

def test_arch_seed_rule(tmp_path: Path) -> None:
    p = tmp_path / "core" / "bad.gd"
    p.parent.mkdir(parents=True)
    p.write_text("extends RefCounted\nfunc setup() -> void:\n\tseed(42)\n")
    violations, error = collect_file(p)
    assert error is None
    assert any(v.rule == "no-bare-global-random" and "seed" in v.message
               for v in violations)


# ---------- §SPEC-1426-27 ----------

def test_feature_key_conflict_is_error(project: Path, tmp_path: Path) -> None:
    """同 key 两个不同 .feature：第二次 compile 显式报错，不静默覆盖。"""
    f1 = tmp_path / "my feature.feature"   # stem 归一化 → my_feature
    f1.write_text("Feature: One\n  Scenario: s\n    Given a\n", encoding="utf-8")
    suite, error = compile_feature(f1, project, write=True)
    assert error is None
    f2 = tmp_path / "my!feature.feature"   # 同 stem 归一化 → my_feature
    f2.write_text("Feature: Two\n  Scenario: s\n    Given b\n", encoding="utf-8")
    _, error = compile_feature(f2, project, write=True)
    assert error is not None and "feature_key conflict" in error["reason"]
