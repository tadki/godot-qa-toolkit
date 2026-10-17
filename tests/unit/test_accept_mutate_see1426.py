"""SEE-1426 P3 — `gqt accept mutate` 单测（§SPEC-1426-11/12/13/14/15）。

-11 变异矩阵：数值 ±1/边界、布尔翻转、枚举域内邻值、域外从不生成；
-12 三层防伪：①变异器对抗性金测（70/71 fixture 恰好 ±1/边界/邻值）
②哨兵核对（无测试流 = run_error）③unchanged 槽变异必挂；
-13 v1 无 stamp 每轮全量、survived 即打回、篡改后同套测试必挂；
-14 --worker 通道差分对照 verdict 一致；
-15 mutate 对抗面（恒真断言 / 手改生成物 / 缺反向断言必打回）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from godot_qa_toolkit.accept.compile import compile_feature, sha256_of
from godot_qa_toolkit.accept.mutate import mutate_case, mutate_ir
from godot_qa_toolkit.accept.mutate_runner import AcceptRunError, run_mutate

_CASE_KILL_OUTPUT = "* test_s_000_00\n  [Failed]\n* test_s_000_01\n  [Failed]\n"
_CASE_PASS_OUTPUT = "* test_s_000_00\n  [Passed]\n"


@pytest.fixture(autouse=True)
def _restore_subprocess():
    """fake 直接换全局 subprocess 模块属性——autouse 兜底还原防泄漏。"""
    import subprocess as sp
    import godot_qa_toolkit.accept.mutate_runner as mr
    orig_run, orig_popen = sp.run, sp.Popen
    yield
    sp.run, sp.Popen = orig_run, orig_popen
    mr.subprocess.run, mr.subprocess.Popen = orig_run, orig_popen


def _fake_gut(monkeypatch, *, returncode: int, output: str) -> None:
    import godot_qa_toolkit.accept.mutate_runner as mr

    def fake_run(*a, **k):
        class R:
            def __init__(self):
                self.returncode = returncode
                self.stdout = output
                self.stderr = ""
        return R()
    mr.subprocess.run = fake_run


CASE = {
    "test_name": "test_c_000_00",
    "values": {"hp": "10", "left": "7", "flag": "true", "mode": "stun"},
    "types": {"hp": "int", "left": "numeric", "flag": "bool",
               "mode": "enum{normal,stun,dead}"},
    "steps": [
        {"keyword": "Given", "text": "hp is 10", "line": 2},
        {"keyword": "Then", "text": "remaining hp is <left>", "line": 3},
    ],
    "then_unchanged": ["world.tick"],
}


# ---------- §SPEC-1426-11 变异矩阵 ----------

def test_numeric_pm1_matrix() -> None:
    m = {(x.mutated, x.rationale) for x in mutate_case(CASE)
         if x.target == "left" and x.kind == "numeric"}
    assert ("6", "-1") in m and ("8", "+1") in m


def test_boundary_extrapolation() -> None:
    case = dict(CASE, values={"hp": "10", "left": "0"})
    m = {(x.mutated, x.rationale) for x in mutate_case(case)
         if x.target == "left" and x.kind == "numeric"}
    # 0-cell 的 -1 与 boundary 同值：rationale 合并、mutant_id 唯一（披露①）
    assert ("-1", "-1+boundary") in m
    ids = [x.mutant_id for x in mutate_case(case)]
    assert len(ids) == len(set(ids))


def test_boolean_flip() -> None:
    m = [x for x in mutate_case(CASE) if x.target == "flag"]
    assert any(x.mutated == "false" and x.kind == "boolean" for x in m)


def test_enum_neighbor_switch_only() -> None:
    m = {x.mutated for x in mutate_case(CASE)
         if x.target == "mode" and x.kind == "enum"}
    assert m == {"normal", "dead"}  # stun 的域内邻值，域外从不出现


def test_out_of_domain_never_generated() -> None:
    case = dict(CASE, values={"hp": "10", "left": "7", "flag": "true",
                              "mode": "banned"})
    m = [x for x in mutate_case(case) if x.target == "mode"]
    assert m == []  # cell 不在声明域内 = 零变异（域外从不生成）


def test_untyped_placeholder_not_mutated() -> None:
    """types 未声明的占位符不进变异面（变异器只动它认识的面）。"""
    case = dict(CASE, types={"left": "numeric"})
    targets = {x.target for x in mutate_case(case)}
    assert "hp" not in targets and "flag" not in targets and "mode" not in targets


# ---------- §SPEC-1426-12① 变异器对抗性金测 ----------

def test_golden_70_71_fixture_exact_yield() -> None:
    """金测：70/71 fixture——期望产出恰好 ±1/边界/邻值集合，不多不少。"""
    case = {
        "test_name": "test_g_000_00",
        "values": {"n": "70", "b": "false"},
        "types": {"n": "int", "b": "bool"},
        "steps": [{"keyword": "Then", "text": "count is <n>", "line": 1}],
        "then_unchanged": [],
    }
    mutants = sorted((m.target, m.mutated, m.rationale)
                     for m in mutate_case(case))
    assert sorted(mutants) == sorted([
        ("n", "69", "-1"),
        ("n", "71", "+1"),
        ("b", "true", "flip"),
    ])


# ---------- §SPEC-1426-12③ unchanged 槽进矩阵 ----------

def test_unchanged_slot_in_matrix() -> None:
    m = [x for x in mutate_case(CASE) if x.kind == "unchanged"]
    assert len(m) == 1 and m[0].target == "world.tick"


# ---------- runner / 闭环 ----------

FEATURE = """Feature: F
  Scenario Outline: s
    Then hp is <left>
    # types: <hp>:int, <left>:numeric
    Examples:
      | hp | left |
      | 10 | 7    |
"""


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


def test_mutant_ir_is_temporary_copy(project: Path, tmp_path: Path) -> None:
    """变异只落临时副本：跑完磁盘 IR 恢复原样（三件哈希仍自洽）。"""
    _compile(project, tmp_path)
    _fake_gut(None, returncode=1, output=_CASE_KILL_OUTPUT)
    result = run_mutate(project, "demo")
    ir_disk = (project / "acceptance/ir/demo.json").read_text(encoding="utf-8")
    ir = json.loads(ir_disk)
    assert ir["cases"][0]["values"]["left"] == "7", "变异 IR 必须是临时副本"
    assert result["ok"] is True and result["summary"]["killed"] >= 1


def test_survived_mutant_rejects(project: Path, tmp_path: Path) -> None:
    """存活即打回：全部 mutant 轮 GUT 全绿 → ok=false + survived 明细。"""
    _compile(project, tmp_path)
    _fake_gut(None, returncode=0, output=_CASE_PASS_OUTPUT)
    result = run_mutate(project, "demo")
    assert result["ok"] is False
    assert result["summary"]["survived"] == result["summary"]["mutants"]
    assert all("mutant survived" in f["reason"] for f in result["failures"])


def test_full_matrix_no_stamp(project: Path, tmp_path: Path) -> None:
    _compile(project, tmp_path)
    _fake_gut(None, returncode=1, output=_CASE_KILL_OUTPUT)
    result = run_mutate(project, "demo")
    assert result["summary"]["full_matrix"] is True
    # 每轮全量：mutant 轮数 = 全矩阵大小（本 feature 无 unchanged 槽：hp ±1 + left ±1 = 4）
    assert result["summary"]["mutants"] == 4


def test_sentinel_no_test_stream_is_run_error(project: Path, tmp_path: Path) -> None:
    """哨兵核对②：GUT 输出零测试流 = 执行链失真 → run_error 面失败。"""
    _compile(project, tmp_path)
    _fake_gut(None, returncode=0, output="Godot Engine v4.6\n")  # 无任何测试流
    result = run_mutate(project, "demo")
    assert result["ok"] is False
    assert all("sentinel check failed" in f["reason"] for f in result["failures"])


def test_no_mutants_is_run_error(project: Path, tmp_path: Path) -> None:
    """零 Then 绑定且零 unchanged 的 IR：无变异面 = run_error（无门禁面）。"""
    f = tmp_path / "empty.feature"
    f.write_text("Feature: E\n  Scenario: s\n    Given a\n", encoding="utf-8")
    suite, error = compile_feature(f, project, write=True)
    assert error is None
    with pytest.raises(AcceptRunError, match="no mutants generated"):
        run_mutate(project, "empty")


# ---------- §SPEC-1426-15 mutate 对抗面 ----------

def test_adversarial_tautology_assertion_still_caught(project: Path, tmp_path: Path) -> None:
    """恒真断言作弊：生成测试被换成恒真 → mutant 轮全绿 → survived 打回
    （变异门禁不信任测试内容，只信任'变异后必挂'这一判据）。"""
    _compile(project, tmp_path)
    # 攻击者把 generated 换成恒真（但哈希自洽——重算 manifest）
    gd_path = project / "acceptance/generated/test_demo.gd"
    tautology = "# tampered\nextends GutTest\nfunc test_tautology() -> void:\n\tassert_true(true)\n"
    gd_path.write_text(tautology, encoding="utf-8")
    man_path = project / "acceptance/generated/demo.manifest.json"
    manifest = json.loads(man_path.read_text(encoding="utf-8"))
    manifest["generated_hash"] = sha256_of(tautology)
    man_path.write_text(json.dumps(manifest), encoding="utf-8")

    _fake_gut(None, returncode=0, output="* test_tautology\n  [Passed]\n")
    result = run_mutate(project, "demo")
    assert result["ok"] is False, "恒真测试放行全部 mutant = 必打回"


def test_adversarial_missing_unchanged_slot_caught(project: Path, tmp_path: Path) -> None:
    """缺反向断言：源 .feature 删掉 # unchanged 行 → 重编译哈希失配 → 打回。"""
    _compile(project, tmp_path)
    # 编一个带 unchanged 的 feature，然后删行
    f = tmp_path / "u.feature"
    f.write_text("Feature: U\n  Scenario Outline: s <x>\n"
                 "    # unchanged: world.tick\n    Then a is <x>\n"
                 "    # types: <x>:int\n"
                 "    Examples:\n      | x |\n      | 1 |\n", encoding="utf-8")
    suite, error = compile_feature(f, project, write=True)
    assert error is None
    # 从源 .feature 删掉 unchanged 行（反向断言被剥离）
    f.write_text("Feature: U\n  Scenario Outline: s <x>\n    Then a is <x>\n"
                 "    # types: <x>:int\n"
                 "    Examples:\n      | x |\n      | 1 |\n", encoding="utf-8")
    with pytest.raises(AcceptRunError, match="recompile hash mismatch"):
        run_mutate(project, "u")


# ---------- §SPEC-1426-14 --worker 差分对照 ----------

class _FakeWorkerIO:
    """真常驻 worker 通道假进程（Popen 面）：stdin NDJSON 收请求、
    stdout 按脚本回 verdict（与 batch 假链同语义，供差分对照）。"""

    def __init__(self, rc: int, case_names: list[str]):
        self.rc = rc
        self.case_names = case_names
        self.stdin = self
        self.stdout = self

    def write(self, line: str) -> None:
        self._last = line

    def flush(self) -> None:
        pass

    def readline(self) -> str:
        req = json.loads(self._last)
        return json.dumps({
            "mutant_id": req["mutant_id"], "rc": self.rc,
            "executed": len(self.case_names),
            "failing": self.case_names if self.rc != 0 else [],
            "ops_version": "see1426-accept/1"}) + "\n"

    def close(self) -> None:
        pass


def test_worker_channel_differential_verdicts(project: Path, tmp_path: Path) -> None:
    """--worker 差分对照：batch 与 worker 两条真实代码路径 verdict 一致
    （披露③：worker 走 Popen NDJSON 带内路径，非 summary 披露假通道）。"""
    _compile(project, tmp_path)
    # 全 kill 的执行链（fake 确定性）
    _fake_gut(None, returncode=1, output=_CASE_KILL_OUTPUT)
    batch = run_mutate(project, "demo", use_worker=False)
    import godot_qa_toolkit.accept.mutate_runner as mr
    mr.subprocess.Popen = lambda *a, **k: _FakeWorkerIO(
        1, ["test_s_000_00", "test_s_000_01"])
    worker = run_mutate(project, "demo", use_worker=True)
    assert batch["ok"] == worker["ok"]
    assert batch["summary"]["killed"] == worker["summary"]["killed"]
    assert batch["summary"]["mutants"] == worker["summary"]["mutants"]
    assert worker["summary"]["worker"] is True


def test_worker_flag_in_contract(project: Path, tmp_path: Path) -> None:
    _compile(project, tmp_path)
    import godot_qa_toolkit.accept.mutate_runner as mr
    mr.subprocess.Popen = lambda *a, **k: _FakeWorkerIO(
        1, ["test_s_000_00", "test_s_000_01"])
    result = run_mutate(project, "demo", use_worker=True)
    assert result["summary"]["worker"] is True
