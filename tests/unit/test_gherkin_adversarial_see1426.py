"""SEE-1426 Round A hardener — gherkin compile/run 对抗性单测（§SPEC-1426-15）。

三类强制作弊样本在 run 全链路（run_suite，非仅 replay_check）的机器判定，
沿 test_gdself_stats_adversarial_see1405.py 先例：

- 恒真断言：手改 generated 把失败上报剥离成恒真 → 必 run_error；
- 手改生成物：IR / generated / manifest 三面各自手改——含「重锚 manifest
  哈希字段」与「删除文件」形态 → 必 run_error，无静默放行档；
- 缺反向断言：unchanged 槽必须被生成器忠实译入生成脚本（基线快照 + 违规
  fail_test 双在场）；剥离比对手改 → 必 run_error。

自选对抗面：
- 归因串扰：同名双场景失败各对回正确 scenario_line，不串；
- 冒烟哨兵在场性：生成脚本必须真实调用 smoke_check + harness 缺失兜底
  fail_test + IR 缺 case 漂移兜底 fail_test；
- 执行超时 → run_error。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from godot_qa_toolkit.gherkin.compile import compile_feature
from godot_qa_toolkit.gherkin.run import GherkinRunError, run_suite


def _schema_ensure(project: Path) -> None:
    """SEE-1624：compile 前置 schema 握手 + run/mutate 预检 validator——
    夹具补最小合法 schema.json 与可通过 V 检查的 validator.gd stub。"""
    from gherkin_fixtures import VALID_SCHEMA, VALIDATOR_OK_GD
    import json as _json
    s = project / "addons" / "gqt_gherkin" / "schema.json"
    if not s.is_file():
        s.parent.mkdir(parents=True, exist_ok=True)
        s.write_text(_json.dumps(VALID_SCHEMA), encoding="utf-8")
    v = project / "addons" / "gqt_gherkin" / "validator.gd"
    if not v.is_file():
        v.write_text(VALIDATOR_OK_GD, encoding="utf-8")




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


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "addons" / "gut").mkdir(parents=True)
    (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut stub\n")
    from gherkin_fixtures import ensure_addon_metadata
    ensure_addon_metadata(root)
    return root


def _compile_written(project: Path, tmp_path: Path,
                     feature: str = FEATURE):
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
    suite, error = compile_feature(f, project, write=True)
    assert error is None
    return suite


def _gd_path(project: Path, suite) -> Path:
    return project / "gherkin/generated" / f"test_{suite.feature_key}.gd"


# ---------- 恒真断言（必 run_error） ----------

def test_always_true_generated_rewrite_rejected(project: Path,
                                                tmp_path: Path) -> None:
    # 恒真作弊：把失败上报循环替换成 pass（断言恒真化）——run 全链路必拒。
    suite = _compile_written(project, tmp_path)
    gd = _gd_path(project, suite)
    original = gd.read_text(encoding="utf-8")
    tampered = original.replace('fail_test(String(e))', "pass")
    assert tampered != original, "tamper must change bytes"
    gd.write_text(tampered, encoding="utf-8")
    with pytest.raises(GherkinRunError, match="generated test hash mismatch"):
        run_suite(project, suite.feature_key)


# ---------- 手改生成物：IR / generated / manifest 三面 ----------

def test_tampered_ir_full_pipeline_rejected(project: Path,
                                            tmp_path: Path) -> None:
    # 既有套件只在 replay_check 层测 IR tamper；这里走 run_suite 全链路。
    suite = _compile_written(project, tmp_path)
    ir_file = project / "gherkin/ir" / f"{suite.feature_key}.json"
    ir = json.loads(ir_file.read_text(encoding="utf-8"))
    ir["cases"][0]["steps"][2]["text"] = "hero hp is 999"
    ir_file.write_text(json.dumps(ir, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(GherkinRunError, match="IR hash mismatch"):
        run_suite(project, suite.feature_key)


def test_tampered_generated_full_pipeline_rejected(project: Path,
                                                   tmp_path: Path) -> None:
    suite = _compile_written(project, tmp_path)
    gd = _gd_path(project, suite)
    gd.write_text(gd.read_text(encoding="utf-8") + "\n# innocent comment\n")
    with pytest.raises(GherkinRunError, match="generated test hash mismatch"):
        run_suite(project, suite.feature_key)


def test_manifest_hash_reanchoring_rejected(project: Path,
                                            tmp_path: Path) -> None:
    # 手改 manifest 哈希字段为伪造值（不同步改磁盘文件）→ 与磁盘失配必抓。
    suite = _compile_written(project, tmp_path)
    man = (project / "gherkin/generated"
           / f"{suite.feature_key}.manifest.json")
    m = json.loads(man.read_text(encoding="utf-8"))
    m["ir_hash"] = "0" * 64
    m["bindings"]["ir"] = "0" * 64
    man.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(GherkinRunError, match="IR hash mismatch"):
        run_suite(project, suite.feature_key)


def test_missing_ir_file_rejected(project: Path, tmp_path: Path) -> None:
    suite = _compile_written(project, tmp_path)
    (project / "gherkin/ir" / f"{suite.feature_key}.json").unlink()
    with pytest.raises(GherkinRunError, match="IR file missing"):
        run_suite(project, suite.feature_key)


def test_missing_generated_file_rejected(project: Path,
                                         tmp_path: Path) -> None:
    suite = _compile_written(project, tmp_path)
    _gd_path(project, suite).unlink()
    with pytest.raises(GherkinRunError, match="generated test missing"):
        run_suite(project, suite.feature_key)


def test_manifest_version_mismatch_rejected(project: Path,
                                            tmp_path: Path) -> None:
    suite = _compile_written(project, tmp_path)
    man = (project / "gherkin/generated"
           / f"{suite.feature_key}.manifest.json")
    m = json.loads(man.read_text(encoding="utf-8"))
    m["ops_version"] = "see1426-accept/0"
    man.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(GherkinRunError, match="ops_version"):
        run_suite(project, suite.feature_key)


# ---------- 缺反向断言（unchanged 槽） ----------

def test_stripped_unchanged_comparison_rejected(project: Path,
                                                tmp_path: Path) -> None:
    # 缺反向断言作弊：手改 generated 让 unchanged 违规文案失活 → 必拒。
    feature = FEATURE.replace(
        "    # types:", "    # unchanged: world.tick\n    # types:")
    suite = _compile_written(project, tmp_path, feature)
    gd = _gd_path(project, suite)
    tampered = gd.read_text(encoding="utf-8").replace(
        "unchanged slot violated", "never fires")
    gd.write_text(tampered, encoding="utf-8")
    with pytest.raises(GherkinRunError):
        run_suite(project, suite.feature_key)


def test_generated_translates_unchanged_slot_faithfully(
        project: Path, tmp_path: Path) -> None:
    # 生成器忠实性：unchanged 非空的 IR 必须译出「基线快照 + 违规
    # fail_test」双向逻辑——生成器漏译反向断言时本测试红。
    feature = FEATURE.replace(
        "    # types:",
        "    # unchanged: world.tick, player.gold\n    # types:")
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
    assert suite.ir["cases"][0]["then_unchanged"] == ["world.tick", "player.gold"]
    gd = suite.generated_gd
    assert "_baselines[" in gd, "baseline snapshot logic missing"
    assert "unchanged slot violated" in gd, "violation must fail the test"
    assert gd.count("query_state") >= 2, "snapshot before AND compare after"


# ---------- 归因串扰 ----------

def test_same_name_scenarios_attributed_distinctly(
        project: Path, tmp_path: Path, monkeypatch) -> None:
    # 同名双场景：失败必须对回正确的 scenario_line，归因不串。
    feature = """Feature: 同名对抗
  Scenario: 战斗
    Given world exists
  Scenario: 战斗
    Given world exists
"""
    suite = _compile_written(project, tmp_path, feature)
    attribution = suite.manifest["attribution"]
    names = [a["test_name"] for a in attribution]
    lines = [a["scenario_line"] for a in attribution]
    assert len(set(names)) == 2, "test names must stay unique"
    assert lines[0] != lines[1]

    from godot_qa_toolkit.gherkin import run as run_mod

    def fake_run(*a, **k):
        argv_s = " ".join(str(x) for x in (a[0] if a else []))
        if "validator.gd" in argv_s:
            from gherkin_fixtures import fake_validator_process
            return fake_validator_process()
        class R:
            returncode = 1
            stdout = f"* {names[1]}\n  [Failed]\n"
            stderr = ""
        return R()
    monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
    result = run_suite(project, suite.feature_key)
    assert result["ok"] is False
    assert result["failures"][0]["scenario"] == "战斗"
    assert result["failures"][0]["line"] == lines[1]


# ---------- 冒烟哨兵在场性 ----------

def test_generated_carries_smoke_sentinel(project: Path,
                                          tmp_path: Path) -> None:
    # 哨兵失真对抗的文本级守卫：生成脚本必须真实调用 smoke_check，且
    # harness 缺失/冒烟失败必须 fail_test——不存在静默通过路径。
    f = ((project / "addons" / "gqt_gherkin" / "features") /
         "demo.feature")
    _schema_ensure(project)
    f.write_text(FEATURE, encoding="utf-8")
    suite, error = compile_feature(f, project, write=False)
    assert error is None
    gd = suite.generated_gd
    assert "if not h.smoke_check():" in gd, "smoke sentinel call missing"
    assert 'fail_test("gherkin harness unavailable' in gd, \
        "harness-unavailable fallback missing"
    assert "case missing from IR" in gd, "IR/generated drift fallback missing"


# ---------- 执行超时 ----------

def test_run_timeout_is_run_error(project: Path, tmp_path: Path,
                                  monkeypatch) -> None:
    suite = _compile_written(project, tmp_path)
    from godot_qa_toolkit.gherkin import run as run_mod

    def fake_run(*a, **k):
        argv_s = " ".join(str(x) for x in (a[0] if a else []))
        if "validator.gd" in argv_s:
            from gherkin_fixtures import fake_validator_process
            return fake_validator_process()
        raise subprocess.TimeoutExpired(cmd="godot", timeout=1)
    monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
    with pytest.raises(GherkinRunError, match="timed out"):
        run_suite(project, suite.feature_key)
