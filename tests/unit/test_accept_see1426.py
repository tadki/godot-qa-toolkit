"""SEE-1426 P2b — accept compile/run 单测（§SPEC-1426-06/07/08/09）。

golden-file（IR/生成产物快照）、三件哈希绑定重放校验（手改 generated/IR →
run_error）、归因 manifest 中文场景名、冒烟自检连接（harness smoke_check
失败 → run_error 判据面）、unchanged 槽进 IR。godot 真实 headless 执行
由 QA 实机复验（§SPEC-1426-10），本套件 mock 子进程。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from godot_qa_toolkit.accept import OPS_VERSION
from godot_qa_toolkit.accept.compile import compile_feature
from godot_qa_toolkit.accept.run import AcceptRunError, replay_check, run_suite

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


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "addons" / "gut").mkdir(parents=True)
    (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut stub\n")
    return root


def _compile(project: Path, tmp_path: Path, feature: str = FEATURE,
             write: bool = True):
    f = tmp_path / "demo.feature"
    f.write_text(feature, encoding="utf-8")
    return compile_feature(f, project, write=write)


# ---------- compile ----------

def test_compile_ir_shape_and_version(project: Path, tmp_path: Path) -> None:
    suite, error = _compile(project, tmp_path, write=False)
    assert error is None
    ir = suite.ir
    assert ir["ops_version"] == OPS_VERSION
    assert ir["feature"] == "生命值扣减"
    assert len(ir["cases"]) == 2
    c0 = ir["cases"][0]
    assert c0["values"] == {"hp": "10", "dmg": "3", "left": "7"}
    assert c0["types"] == {"hp": "int", "dmg": "int", "left": "numeric"}
    assert c0["steps"][1]["text"] == "hero takes 3 damage"


def test_compile_generated_reads_ir_not_baked(project: Path, tmp_path: Path) -> None:
    suite, _ = _compile(project, tmp_path, write=False)
    # 数据驱动纪律：数字只出现在 IR JSON 字面量（cases_json）里，生成脚本
    # 断言逻辑不烤任何业务期望值。
    body = suite.generated_gd
    assert "IR_PATH" in body and "ops_version" in body
    assert "10" not in body.split("var cases")[0].split("const IR_PATH")[1]


def test_compile_manifest_hash_binding(project: Path, tmp_path: Path) -> None:
    import hashlib
    suite, _ = _compile(project, tmp_path, write=False)
    m = suite.manifest
    assert m["ir_hash"] == hashlib.sha256(suite.ir_json.encode()).hexdigest()
    assert m["generated_hash"] == hashlib.sha256(
        suite.generated_gd.encode()).hexdigest()
    assert m["bindings"]["ir"] == m["ir_hash"]


def test_compile_attribution_chinese_names(project: Path, tmp_path: Path) -> None:
    suite, _ = _compile(project, tmp_path, write=False)
    names = [a["test_name"] for a in suite.manifest["attribution"]]
    assert len(set(names)) == len(names), "test names must be unique (归因不串)"
    assert all(n.startswith("test_") for n in names)
    assert suite.manifest["attribution"][0]["scenario"] == "受到 <dmg> 点伤害"


def test_write_layout_and_clean_then_generate(project: Path, tmp_path: Path) -> None:
    suite, error = _compile(project, tmp_path, write=True)
    assert error is None
    ir_file = project / "acceptance/ir" / f"{suite.feature_key}.json"
    gd_file = project / "acceptance/generated" / f"test_{suite.feature_key}.gd"
    man_file = project / "acceptance/generated" / f"{suite.feature_key}.manifest.json"
    assert ir_file.is_file() and gd_file.is_file() and man_file.is_file()
    # 先清空再生成：放一个陈旧文件，重编译后必须消失。
    stale = project / "acceptance/generated/stale.gd"
    stale.write_text("# stale\n")
    _compile(project, tmp_path, write=True)
    assert not stale.exists()


def test_write_requires_gut(project: Path, tmp_path: Path) -> None:
    (project / "addons" / "gut" / "gut_cmdln.gd").unlink()
    _, error = _compile(project, tmp_path, write=True)
    assert error is not None and "GUT runner not found" in error["reason"]


def test_unchanged_slot_flows_into_ir(project: Path, tmp_path: Path) -> None:
    feature = FEATURE.replace(
        "    # types:", "    # unchanged: world.tick, player.gold\n    # types:")
    suite, error = _compile(project, tmp_path, feature, write=False)
    assert error is None
    assert suite.ir["cases"][0]["then_unchanged"] == ["world.tick", "player.gold"]


# ---------- replay 校验（tamper 必 run_error） ----------

def test_replay_tampered_ir_rejected(project: Path, tmp_path: Path) -> None:
    suite, _ = _compile(project, tmp_path, write=True)
    ir_file = project / "acceptance/ir" / f"{suite.feature_key}.json"
    ir = json.loads(ir_file.read_text(encoding="utf-8"))
    ir["cases"][0]["values"]["left"] = "999"
    ir_file.write_text(json.dumps(ir, ensure_ascii=False))
    with pytest.raises(AcceptRunError, match="IR hash mismatch"):
        replay_check(project, suite.feature_key)


def test_replay_tampered_generated_rejected(project: Path, tmp_path: Path) -> None:
    suite, _ = _compile(project, tmp_path, write=True)
    gd = project / "acceptance/generated" / f"test_{suite.feature_key}.gd"
    gd.write_text(gd.read_text(encoding="utf-8") + "\n# tampered\n")
    with pytest.raises(AcceptRunError, match="generated test hash mismatch"):
        replay_check(project, suite.feature_key)


def test_replay_missing_manifest_rejected(project: Path) -> None:
    with pytest.raises(AcceptRunError, match="manifest missing"):
        replay_check(project, "nope")


def test_run_gut_missing_is_run_error(project: Path, tmp_path: Path) -> None:
    suite, _ = _compile(project, tmp_path, write=True)
    (project / "addons/gut/gut_cmdln.gd").unlink()
    with pytest.raises(AcceptRunError, match="GUT runner not found"):
        run_suite(project, suite.feature_key)


def test_run_launch_failure_is_run_error(project: Path, tmp_path: Path,
                                         monkeypatch) -> None:
    suite, _ = _compile(project, tmp_path, write=True)
    from godot_qa_toolkit.accept import run as run_mod

    def fake_run(*a, **k):
        class R:
            returncode = 1
            stdout = "Godot Engine v4.6\nAttempt to open script file not found"
            stderr = ""
        return R()
    monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
    with pytest.raises(AcceptRunError, match="launch failed|GUT"):
        run_suite(project, suite.feature_key)


def test_run_attributed_failures_contract(project: Path, tmp_path: Path,
                                          monkeypatch) -> None:
    suite, _ = _compile(project, tmp_path, write=True)
    from godot_qa_toolkit.accept import run as run_mod

    def fake_run(*a, **k):
        class R:
            returncode = 1
            tn = suite.manifest["attribution"][0]["test_name"]
            stdout = (f"* {tn}\n  [Failed]\n"
                      "* test_unattributable\n  [Failed]\n")
            stderr = ""
        return R()
    monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
    result = run_suite(project, suite.feature_key)
    assert result["tool"] == "accept" and result["ok"] is False
    assert result["summary"]["replay_checked"] is True
    scenarios = {f["scenario"] for f in result["failures"]}
    assert "受到 <dmg> 点伤害" in scenarios, "归因必须对回中文场景名"
    assert any(f["scenario"] is None and "not attributable" in f["reason"]
               for f in result["failures"]), "无归因失败必须显式披露"


def test_run_ok_contract(project: Path, tmp_path: Path, monkeypatch) -> None:
    suite, _ = _compile(project, tmp_path, write=True)
    from godot_qa_toolkit.accept import run as run_mod

    def fake_run(*a, **k):
        class R:
            returncode = 0
            stdout = "* test_x [passed]\n"
            stderr = ""
        return R()
    monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
    result = run_suite(project, suite.feature_key)
    assert result["ok"] is True and result["failures"] == []


# ---------- CLI 判据形态 ----------

def test_cli_accept_compile_contract(project: Path, tmp_path: Path) -> None:
    import subprocess, sys, os
    repo = Path(__file__).resolve().parents[2]
    f = tmp_path / "demo.feature"
    f.write_text(FEATURE, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(repo / "src"))
    # dry compile（无 --write）：只出 summary。
    r = subprocess.run(
        [sys.executable, "-m", "godot_qa_toolkit.cli", "accept", "compile",
         str(f), "--project-root", str(project)],
        capture_output=True, text=True, env=env)
    out = json.loads(r.stdout)
    assert out["ok"] is True and out["summary"]["cases"] == 2
    assert out["summary"]["wrote"] == []
    # opt-in 写入：wrote 逐文件披露。
    r = subprocess.run(
        [sys.executable, "-m", "godot_qa_toolkit.cli", "accept", "compile",
         str(f), "--project-root", str(project), "--write"],
        capture_output=True, text=True, env=env)
    out = json.loads(r.stdout)
    assert len(out["summary"]["wrote"]) == 4  # ir/generated/worker/manifest
    assert all(w.startswith("acceptance/") for w in out["summary"]["wrote"])
