"""SEE-1468 M0a — accept /2 契约扩展单测（定案 §2 四目标）。

- IR /2 新 cell 类型族（str/bool/float_near/not_contains/in_set/signal_count）
  的 parse 校验、变异算子与确定性 kill 语义；
- `# mutation: exempt` 场景指令（重复 = parse error；V4 grep 计数判据）；
- ops_version major bump 纪律（旧 manifest run 侧 run_error）；
- run_step 全量 await 化（生成测试与 worker 生成物源码锚定）；
- 豁免基线轮（exempt ≠ skip）：exempt_verified / run_error 逐 case 记账、
  纯豁免特性收口、基线红 fail-fast。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from godot_qa_toolkit.accept import OPS_VERSION
from godot_qa_toolkit.accept.compile import compile_feature
from godot_qa_toolkit.accept.mutate import mutate_case, mutate_ir
from godot_qa_toolkit.accept.mutate_runner import run_mutate
from godot_qa_toolkit.accept.run import AcceptRunError, replay_check
from godot_qa_toolkit.gherkin.parser import GherkinSyntaxError, parse

FEATURE = """Feature: F
  Scenario Outline: s
    Then hp is <left>
    # types: <hp>:int, <left>:numeric
    Examples:
      | hp | left |
      | 10 | 7    |
"""


def _exempt_feature() -> str:
    """FEATURE 场景头插入一行 `# mutation: exempt` 指令。"""
    return FEATURE.replace(
        "  Scenario Outline: s",
        "  Scenario Outline: s\n    # mutation: exempt", 1)


@pytest.fixture(autouse=True)
def _restore_subprocess():
    """fake 直接换全局 subprocess 模块属性——autouse 兜底还原防泄漏
    （test_accept_mutate_see1426 同款纪律；缺它则污染后续 CLI 测试）。"""
    import subprocess as sp
    import godot_qa_toolkit.accept.mutate_runner as mr
    orig_run, orig_popen = sp.run, sp.Popen
    yield
    sp.run, sp.Popen = orig_run, orig_popen
    mr.subprocess.run, mr.subprocess.Popen = orig_run, orig_popen


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "addons" / "gut").mkdir(parents=True)
    (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
    return root


def _compile(project: Path, tmp_path: Path, feature: str = FEATURE,
             name: str = "demo"):
    f = tmp_path / f"{name}.feature"
    f.write_text(feature, encoding="utf-8")
    suite, error = compile_feature(f, project, write=True)
    assert error is None
    return suite


def _fake_gut(output: str, returncode: int = 0) -> None:
    import godot_qa_toolkit.accept.mutate_runner as mr

    def fake_run(*a, **k):
        class R:
            def __init__(self):
                self.returncode = returncode
                self.stdout = output
                self.stderr = ""
        return R()
    mr.subprocess.run = fake_run


# ---------- parser：# mutation: exempt ----------

def test_exempt_directive_flows_into_ir(project: Path, tmp_path: Path) -> None:
    suite = _compile(project, tmp_path, _exempt_feature())
    assert suite.ir["cases"][0]["mutation_exempt"] is True


def test_exempt_absent_by_default(project: Path, tmp_path: Path) -> None:
    suite = _compile(project, tmp_path)
    assert suite.ir["cases"][0]["mutation_exempt"] is False


def test_exempt_outside_scenario_is_parse_error() -> None:
    with pytest.raises(GherkinSyntaxError, match="outside a Scenario"):
        parse("Feature: F\n  # mutation: exempt\n  Scenario: s\n    Given a\n")


def test_exempt_unknown_body_is_parse_error() -> None:
    with pytest.raises(GherkinSyntaxError, match="unknown # mutation:"):
        parse("Feature: F\n  Scenario: s\n    # mutation: exempted\n"
              "    Given a\n")


def test_exempt_duplicate_is_parse_error() -> None:
    with pytest.raises(GherkinSyntaxError, match="duplicate # mutation: exempt"):
        parse("Feature: F\n  Scenario: s\n    # mutation: exempt\n"
              "    # mutation: exempt\n    Given a\n")


# ---------- parser：/2 类型语法与 cell 形态校验 ----------

def test_float_near_type_parses() -> None:
    f = parse("Feature: F\n  Scenario Outline: s\n    Then x is <x>\n"
              "    # types: <x>:float_near:0.5\n"
              "    Examples:\n      | x |\n      | 7.0 |\n")
    assert f.scenarios[0].types["x"] == "float_near:0.5"


def test_float_near_bad_tolerance_is_parse_error() -> None:
    for bad in ("float_near:0", "float_near:-1", "float_near:abc"):
        with pytest.raises(GherkinSyntaxError):
            parse("Feature: F\n  Scenario Outline: s\n    Then x is <x>\n"
                  f"    # types: <x>:{bad}\n"
                  "    Examples:\n      | x |\n      | 7.0 |\n")


def test_bool_cell_must_be_literal() -> None:
    with pytest.raises(GherkinSyntaxError, match="true/false literal"):
        parse("Feature: F\n  Scenario Outline: s\n    Then ok is <ok>\n"
              "    # types: <ok>:bool\n"
              "    Examples:\n      | ok |\n      | yes |\n")


def test_signal_count_cell_must_be_integer() -> None:
    with pytest.raises(GherkinSyntaxError, match="non-negative"):
        parse("Feature: F\n  Scenario Outline: s\n    Then n is <n>\n"
              "    # types: <n>:signal_count\n"
              "    Examples:\n      | n |\n      | 1.5 |\n")


def test_in_set_cell_must_be_comma_members() -> None:
    # 成员分隔用逗号（`|` 是表格 cell 分隔符，管道形态不可表达）。
    with pytest.raises(GherkinSyntaxError, match="comma-separated"):
        parse("Feature: F\n  Scenario Outline: s\n    Then m is <m>\n"
              "    # types: <m>:in_set\n"
              "    Examples:\n      | m |\n      | a,,b |\n")


def test_in_set_members_may_contain_spaces_around_comma() -> None:
    f = parse("Feature: F\n  Scenario Outline: s\n    Then m is <m>\n"
              "    # types: <m>:in_set\n"
              "    Examples:\n      | m |\n      | normal, stun |\n")
    assert f.scenarios[0].types["m"] == "in_set"


def test_not_contains_cell_must_be_non_empty() -> None:
    with pytest.raises(GherkinSyntaxError, match="non-empty needle"):
        parse("Feature: F\n  Scenario Outline: s\n    Then l is <l>\n"
              "    # types: <l>:not_contains\n"
              "    Examples:\n      | l |\n      |   |\n")


def test_unchanged_multisegment_dot_path_accepted() -> None:
    f = parse("Feature: F\n  Scenario: s\n"
              "    # unchanged: world.ui.heart_count, a.b.c.d\n"
              "    Given a\n")
    assert f.scenarios[0].unchanged == ["world.ui.heart_count", "a.b.c.d"]


def test_unchanged_single_segment_still_rejected() -> None:
    with pytest.raises(GherkinSyntaxError, match="malformed # unchanged:"):
        parse("Feature: F\n  Scenario: s\n    # unchanged: onlyroot\n"
              "    Given a\n")


# ---------- mutator：/2 算子族 ----------

def _case(values: dict, types: dict, **kw) -> dict:
    return {"test_name": "test_c_000_00", "values": values, "types": types,
            "steps": [{"keyword": "Then", "text": "t", "line": 1}],
            "then_unchanged": [], **kw}


def test_str_cells_default_exempt_from_mutation() -> None:
    # Owner 裁决 3：str 默认豁免——本 PR 不启用 str 变异（零 mutants）。
    m = mutate_case(_case({"label": "King"}, {"label": "str"}))
    assert m == []


def test_bool_declared_flips() -> None:
    m = [x for x in mutate_case(_case({"ok": "true"}, {"ok": "bool"}))
         if x.target == "ok"]
    assert len(m) == 1 and m[0].mutated == "false" and m[0].kind == "boolean"


def test_float_near_step_is_max_one_two_tol() -> None:
    # tol=0.25 → 步长 max(1, 0.5)=1；tol=1.5 → 步长 max(1, 3)=3。
    m = {x.mutated for x in mutate_case(
        _case({"x": "7"}, {"x": "float_near:0.25"})) if x.kind == "float_near"}
    assert m == {"8", "6"}
    m2 = {x.mutated for x in mutate_case(
        _case({"x": "7.0"}, {"x": "float_near:1.5"})) if x.kind == "float_near"}
    assert m2 == {"10.0", "4.0"}


def test_float_near_non_numeric_cell_yields_nothing() -> None:
    m = mutate_case(_case({"x": "abc"}, {"x": "float_near:0.5"}))
    assert [x for x in m if x.target == "x"] == []


def test_not_contains_degenerates_to_empty_needle() -> None:
    m = mutate_case(_case({"frag": "deleted"}, {"frag": "not_contains"}))
    assert len(m) == 1
    assert m[0].mutated == "" and m[0].kind == "not_contains"
    assert m[0].original == "deleted"


def test_in_set_degenerates_to_collapsed_set() -> None:
    m = mutate_case(_case({"modes": "normal,stun"}, {"modes": "in_set"}))
    assert len(m) == 1
    assert m[0].mutated == "" and m[0].kind == "in_set"


def test_signal_count_pm1() -> None:
    m = {x.mutated for x in mutate_case(
        _case({"n": "2"}, {"n": "signal_count"})) if x.kind == "signal_count"}
    assert m == {"1", "3"}


def test_exempt_case_skips_typed_keeps_unchanged() -> None:
    case = _case({"hp": "10", "left": "7"},
                 {"hp": "int", "left": "numeric"},
                 then_unchanged=["world.tick"], mutation_exempt=True)
    m = mutate_case(case)
    assert [x.kind for x in m] == ["unchanged"], \
        "exempt case: typed 矩阵豁免，unchanged 基线污染探针照跑（exempt≠skip）"


def test_exempt_ir_end_to_end_no_typed_mutants(project: Path,
                                               tmp_path: Path) -> None:
    suite = _compile(project, tmp_path, _exempt_feature())
    mutants = mutate_ir(suite.ir)
    assert [m.kind for m in mutants] == []


# ---------- compile：await 化生成物源码锚定 ----------

def test_generated_suite_awaits_run_step(project: Path, tmp_path: Path) -> None:
    suite = _compile(project, tmp_path)
    body = suite.generated_gd
    assert "await harness.run_step(step, case)" in body, \
        "/2 调用约定：生成测试对 run_step 全量 await（同步 harness 透传）"
    assert "await _run_case(_harness, case)" in body
    assert "await _run_case_by_name(" in body, \
        "GUT 测试方法协程化（GUT 9.x 原生支持 await 测试方法）"


def test_worker_awaits_run_step_with_drive_loop(project: Path,
                                                tmp_path: Path) -> None:
    suite = _compile(project, tmp_path)
    worker = suite.worker_gd
    assert "await harness.run_step(step, case)" in worker
    assert "await _run_case(harness, case, baselines)" in worker
    assert "func _drive() -> void:" in worker, \
        "_init 不可 await：NDJSON 循环迁入 fire-and-forget 协程"
    assert "await _run_mutant(harness, req)" in worker


# ---------- run：ops_version major bump 纪律 ----------

def test_old_major_manifest_run_side_rejected(project: Path,
                                              tmp_path: Path) -> None:
    """schema 键集/调用约定变更 = major bump；旧 manifest 在 run 侧
    replay_check 严格相等校验下 run_error（既有 fail-fast 机制承载）。"""
    suite = _compile(project, tmp_path)
    man_path = project / "acceptance/generated" / f"{suite.feature_key}.manifest.json"
    manifest = json.loads(man_path.read_text(encoding="utf-8"))
    manifest["ops_version"] = "see1426-accept/1"
    man_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(AcceptRunError, match="ops_version"):
        replay_check(project, suite.feature_key)


def test_ops_version_is_major_2() -> None:
    assert OPS_VERSION == "see1468-accept/2"


# ---------- mutate_runner：豁免基线轮（exempt ≠ skip） ----------

EXEMPT_FEATURE = """Feature: pure structure face
  Scenario Outline: probe <n>
    Given a node <n>
    Then the node exists <n>

    # mutation: exempt
    # types: <n>:str
    Examples:
      | n |
      | 1 |
"""

MIXED_FEATURE = """Feature: mixed face
  Scenario Outline: exempt probe
    Given a node
    Then the label is <label>

    # mutation: exempt
    # types: <label>:str
    Examples:
      | label |
      | King  |

  Scenario Outline: numeric probe
    Given a counter
    Then the count is <n>

    # types: <n>:int
    Examples:
      | n |
      | 7 |
"""


def _manifest_test_names(project: Path, key: str) -> list[str]:
    man = json.loads((project / "acceptance/generated"
                      / f"{key}.manifest.json").read_text(encoding="utf-8"))
    return [a["test_name"] for a in man["attribution"]]


def test_pure_exempt_feature_runs_baseline_not_error(project: Path,
                                                     tmp_path: Path) -> None:
    """纯豁免特性（零 mutants）：不再触发 no-mutants run_error，走
    exempt_verified 基线执行收口。"""
    suite = _compile(project, tmp_path, EXEMPT_FEATURE, name="pure_exempt")
    (test_name,) = _manifest_test_names(project, suite.feature_key)
    _fake_gut(f"* {test_name}\n  [Passed]\n", returncode=0)
    result = run_mutate(project, suite.feature_key)
    assert result["ok"] is True
    assert result["summary"]["mutants"] == 0
    assert result["summary"]["exempt_cases"] == 1
    assert result["summary"]["exempt_results"] == [
        {"test_name": test_name, "scenario": "probe <n>",
         "status": "exempt_verified"}]


def test_exempt_cases_matches_grep_count(project: Path, tmp_path: Path) -> None:
    """V4 机器判据：summary.exempt_cases == 源 .feature 指令行 grep 计数。"""
    suite = _compile(project, tmp_path, MIXED_FEATURE, name="mixed")
    source = Path(suite.ir["source_path"]).read_text(encoding="utf-8")
    grep_count = source.count("# mutation: exempt")
    _fake_gut("* test_everything\n  [Passed]\n", returncode=0)
    result = run_mutate(project, suite.feature_key)
    assert result["summary"]["exempt_cases"] == grep_count == 1


def test_exempt_baseline_green_then_mutant_rounds(project: Path,
                                                  tmp_path: Path) -> None:
    """混合特性：基线轮先行（绿）→ mutant 轮照常；exempt 字段在册。"""
    suite = _compile(project, tmp_path, MIXED_FEATURE, name="mixed")
    names = _manifest_test_names(project, suite.feature_key)
    exempt_name, numeric_name = names[0], names[1]
    # 基线轮与每轮 mutant 全挂 numeric case（kill）——基线轮的输出必须绿
    # 才能进 mutant 轮，所以用可区分的调用序：第一轮绿，后续全挂。
    import godot_qa_toolkit.accept.mutate_runner as mr
    calls = {"n": 0}

    def fake_run(*a, **k):
        class R:
            pass
        calls["n"] += 1
        if calls["n"] == 1:
            R.returncode = 0
            R.stdout = f"* {exempt_name}\n  [Passed]\n* {numeric_name}\n  [Passed]\n"
        else:
            R.returncode = 1
            R.stdout = f"* {exempt_name}\n  [Passed]\n* {numeric_name}\n  [Failed]\n"
        R.stderr = ""
        return R()
    mr.subprocess.run = fake_run
    result = run_mutate(project, suite.feature_key)
    assert result["ok"] is True
    assert calls["n"] >= 2, "基线轮之后仍有 mutant 轮"
    assert result["summary"]["exempt_cases"] == 1
    assert result["summary"]["exempt_results"][0]["status"] == "exempt_verified"
    assert result["summary"]["killed"] >= 1
    # str cell（exempt 场景）零 typed mutants：矩阵只含 numeric ±1×2。
    assert result["summary"]["mutants"] == 2


def test_exempt_baseline_red_fails_fast(project: Path, tmp_path: Path) -> None:
    """未变异基线挂 = 特性先于变异就红：fail-fast 跳过 mutant 轮，
    run_error 计入门禁，ok:false。"""
    suite = _compile(project, tmp_path, MIXED_FEATURE, name="mixed")
    names = _manifest_test_names(project, suite.feature_key)
    import godot_qa_toolkit.accept.mutate_runner as mr
    calls = {"n": 0}

    def fake_run(*a, **k):
        class R:
            pass
        calls["n"] += 1
        R.returncode = 1
        R.stdout = f"* {names[0]}\n  [Failed]\n"
        R.stderr = ""
        return R()
    mr.subprocess.run = fake_run
    result = run_mutate(project, suite.feature_key)
    assert result["ok"] is False
    assert calls["n"] == 1, "基线红后 mutant 轮必须被跳过"
    assert result["summary"]["exempt_baseline_failed"] is True
    assert result["summary"]["exempt_results"][0]["status"].startswith(
        "run_error:")
    assert result["summary"]["killed"] == 0 and result["summary"]["survived"] == 0


def test_exempt_baseline_absent_from_stream_is_run_error(
        project: Path, tmp_path: Path) -> None:
    """基线轮测试流缺失该 case = 执行链可疑 → run_error（不在场不记绿）。"""
    suite = _compile(project, tmp_path, MIXED_FEATURE, name="mixed")
    names = _manifest_test_names(project, suite.feature_key)
    # 基线轮只跑了 numeric case，exempt case 不在流里。
    _fake_gut(f"* {names[1]}\n  [Passed]\n", returncode=0)
    result = run_mutate(project, suite.feature_key)
    assert result["ok"] is False
    assert "absent from executed test stream" in \
        result["summary"]["exempt_results"][0]["status"]


def test_pure_exempt_baseline_red_is_run_error(project: Path,
                                               tmp_path: Path) -> None:
    suite = _compile(project, tmp_path, EXEMPT_FEATURE, name="pure_exempt")
    (test_name,) = _manifest_test_names(project, suite.feature_key)
    _fake_gut(f"* {test_name}\n  [Failed]\n", returncode=1)
    result = run_mutate(project, suite.feature_key)
    assert result["ok"] is False
    assert result["summary"]["run_errors"] >= 1


def test_non_exempt_no_mutants_still_run_error(project: Path,
                                               tmp_path: Path) -> None:
    """无豁免、零绑定面：no mutants run_error 语义不变。"""
    _compile(project, tmp_path,
             "Feature: E\n  Scenario: s\n    Given a\n", name="empty")
    with pytest.raises(AcceptRunError, match="no mutants generated"):
        run_mutate(project, "empty")


# ---------- mutate_runner：worker 通道豁免基线 ----------

class _FakeBaselineWorkerIO:
    """豁免基线 worker 假进程：读请求行，回指定 verdict。"""

    def __init__(self, verdict: dict):
        self.verdict = verdict
        self.stdin = self
        self.stdout = self

    def write(self, line: str) -> None:
        self._last = line

    def flush(self) -> None:
        pass

    def readline(self) -> str:
        return json.dumps(self.verdict) + "\n"

    def close(self) -> None:
        pass


def _arm_worker_baseline(monkeypatch, verdict: dict) -> None:
    import godot_qa_toolkit.accept.mutate_runner as mr
    monkeypatch.setattr(
        mr.subprocess, "Popen",
        lambda *a, **k: _FakeBaselineWorkerIO(verdict))


def test_worker_pure_exempt_baseline_verified(project: Path, tmp_path: Path,
                                              monkeypatch) -> None:
    suite = _compile(project, tmp_path, EXEMPT_FEATURE, name="pure_exempt")
    (test_name,) = _manifest_test_names(project, suite.feature_key)
    _arm_worker_baseline(monkeypatch, {
        "mutant_id": "exempt_baseline", "rc": 0, "executed": 1,
        "failing": [], "ops_version": OPS_VERSION})
    result = run_mutate(project, suite.feature_key, use_worker=True)
    assert result["ok"] is True
    assert result["summary"]["exempt_results"][0]["status"] == "exempt_verified"
    assert result["summary"]["worker"] is True


def test_worker_exempt_baseline_count_mismatch_raises(
        project: Path, tmp_path: Path, monkeypatch) -> None:
    """worker 基线 executed 计数 ≠ case 总数 = 执行链失真（哨兵②同构）。"""
    suite = _compile(project, tmp_path, EXEMPT_FEATURE, name="pure_exempt")
    _arm_worker_baseline(monkeypatch, {
        "mutant_id": "exempt_baseline", "rc": 0, "executed": 0,
        "failing": [], "ops_version": OPS_VERSION})
    with pytest.raises(AcceptRunError, match="execution chain untrusted"):
        run_mutate(project, suite.feature_key, use_worker=True)


def test_worker_exempt_baseline_failing_case_recorded(
        project: Path, tmp_path: Path, monkeypatch) -> None:
    suite = _compile(project, tmp_path, MIXED_FEATURE, name="mixed")
    names = _manifest_test_names(project, suite.feature_key)
    _arm_worker_baseline(monkeypatch, {
        "mutant_id": "exempt_baseline", "rc": 1, "executed": 2,
        "failing": [names[0]], "ops_version": OPS_VERSION})
    result = run_mutate(project, suite.feature_key, use_worker=True)
    assert result["ok"] is False
    assert result["summary"]["exempt_baseline_failed"] is True
    assert result["summary"]["exempt_results"][0]["status"].startswith(
        "run_error:")
