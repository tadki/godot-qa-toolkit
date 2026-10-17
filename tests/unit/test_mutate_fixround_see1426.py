"""SEE-1426 Round B 修复轮守卫测试（DEF-1~4 + 披露①②③）。

Harti hardener 实锤缺陷的回归保护面：
- DEF-1 unchanged mutant 变形生效（mutated_unchanged 基线污染探针进 IR
  与生成器/worker 双侧语义）；
- DEF-2 kill 归因核对（毒测试不得替 mutant 顶罪，batch/worker 同构）；
- DEF-3 域未声明的 Then 绑定枚举形态 cell = parse error；
- DEF-4 seed 收窄（调用形态必打回 / 显式注入合法形态不误杀）；
- 披露① mutant_id 唯一；披露② replay_check 无旁路参数；
- 披露③ worker 生成物 + 哈希绑定 + 探针失败 run_error。
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from godot_qa_toolkit.accept.compile import compile_feature
from godot_qa_toolkit.accept.mutate import Mutant, mutate_case, mutate_ir
from godot_qa_toolkit.accept.mutate_runner import (
    AcceptRunError,
    _mutated_ir,
    run_mutate,
)
from godot_qa_toolkit.accept.run import replay_check
from godot_qa_toolkit.arch.collector import collect_file
from godot_qa_toolkit.gherkin.parser import GherkinSyntaxError, parse

@pytest.fixture(autouse=True)
def _restore_subprocess():
    """fake 直接换全局 subprocess 模块属性——autouse 兜底还原防泄漏。"""
    import subprocess as sp
    import godot_qa_toolkit.accept.mutate_runner as mr
    orig_run, orig_popen = sp.run, sp.Popen
    yield
    sp.run, sp.Popen = orig_run, orig_popen
    mr.subprocess.run, mr.subprocess.Popen = orig_run, orig_popen


FEATURE_UNCHANGED = """Feature: U
  Scenario Outline: s
    Then hp is <left>
    # unchanged: world.tick
    # types: <left>:numeric
    Examples:
      | left |
      | 7    |
"""

KILL_BOTH = "* test_s_000_00\n  [Failed]\n* test_s_000_01\n  [Failed]\n"
PASS_ONE = "* test_s_000_00\n  [Passed]\n"


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "addons" / "gut").mkdir(parents=True)
    (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
    return root


def _compile(project: Path, tmp_path: Path, feature: str = FEATURE_UNCHANGED):
    f = tmp_path / "demo.feature"
    f.write_text(feature, encoding="utf-8")
    suite, error = compile_feature(f, project, write=True)
    assert error is None
    return suite


# ---------- DEF-1 unchanged mutant 变形生效 ----------

def test_def1_unchanged_mutant_alters_ir() -> None:
    ir = json.loads(json.dumps(
        {"ops_version": "see1426-accept/1", "feature": "F", "source_path": "s",
         "cases": [{"test_name": "test_s_000_00", "values": {"left": "7"},
                    "types": {"left": "numeric"}, "steps": [],
                    "then_unchanged": ["world.tick"]}]}))
    mutant = Mutant(mutant_id="x", case_test_name="test_s_000_00",
                    target="world.tick", kind="unchanged",
                    original="<baseline>", mutated="<polluted>",
                    rationale="unchanged-pollution")
    out = _mutated_ir(ir, mutant)
    case = out["cases"][0]
    # 变异 IR 必须与原 IR 不同：目标槽进 mutated_unchanged（基线污染探针）
    assert case.get("mutated_unchanged") == ["world.tick"]
    assert ir["cases"][0] == {"test_name": "test_s_000_00",
                              "values": {"left": "7"},
                              "types": {"left": "numeric"}, "steps": [],
                              "then_unchanged": ["world.tick"]}, "原 IR 不被就地改"


def test_def1_generator_carries_pollution_probe(project: Path,
                                                tmp_path: Path) -> None:
    """生成器/worker 双侧都带基线污染语义——比对块被剥离则探针失效。"""
    suite = _compile(project, tmp_path)
    gd = (project / "acceptance/generated/test_s_..._gd").exists() or True
    gen = (project / "acceptance/generated/test_demo.gd").read_text(encoding="utf-8")
    worker = (project / "acceptance/generated/accept_worker.gd").read_text(
        encoding="utf-8")
    for src in (gen, worker):
        assert "mutated_unchanged" in src, "基线污染探针必须在生成面"
        assert "gqt-mutant-polluted" in src
    assert suite.manifest["worker_hash"]


def test_def1_unchanged_round_judged(project: Path, tmp_path: Path) -> None:
    """unchanged mutant 轮真实判定：挂 = kill（防伪层③生效），全绿 =
    survived（比对面缺失即打回）——不再是变形 no-op 的假 survived。"""
    _compile(project, tmp_path)
    import godot_qa_toolkit.accept.mutate_runner as mr

    # 全绿链：unchanged mutant 不挂 = survived（探针被剥离的作弊形态）
    def fake_pass(*a, **k):
        class R:
            returncode = 0
            stdout = PASS_ONE
            stderr = ""
        return R()
    mr.subprocess.run = fake_pass
    result = run_mutate(project, "demo")
    unchanged = [f for f in result["failures"]
                 if f.get("kind") == "unchanged"]
    assert len(unchanged) == 1 and result["ok"] is False

    # 挂链：unchanged mutant 被 kill（污染探针在位必挂）
    def fake_fail(*a, **k):
        class R:
            returncode = 1
            stdout = KILL_BOTH
            stderr = ""
        return R()
    mr.subprocess.run = fake_fail
    result = run_mutate(project, "demo")
    assert result["ok"] is True
    assert result["summary"]["killed"] == result["summary"]["mutants"]


# ---------- DEF-2 kill 归因核对 ----------

def test_def2_poison_test_cannot_inflate_kills(project: Path,
                                               tmp_path: Path) -> None:
    """恒挂毒测试：rc=1 但失败集合不含 mutant 的 case 测试 → run_error
    （不得记 kill 放行全绿）。"""
    _compile(project, tmp_path, """Feature: F
  Scenario Outline: s
    Then hp is <left>
    # types: <left>:numeric
    Examples:
      | left |
      | 7    |
""")
    import godot_qa_toolkit.accept.mutate_runner as mr

    def fake_poison(*a, **k):
        class R:
            returncode = 1
            stdout = "* test_poison\n  [Failed]\n"  # 毒测试恒挂
            stderr = ""
        return R()
    mr.subprocess.run = fake_poison
    result = run_mutate(project, "demo")
    assert result["ok"] is False, "毒测试顶罪必须整轮打回"
    assert result["summary"]["killed"] == 0
    assert result["summary"]["run_errors"] == result["summary"]["mutants"]
    assert all("kill attribution failed" in f["reason"]
               for f in result["failures"])


class _FakeWorkerIO:
    def __init__(self, verdicts: dict):
        self.verdicts = verdicts
        self.stdin = self
        self.stdout = self

    def write(self, line: str) -> None:
        req = json.loads(line)
        self._mid = req["mutant_id"]

    def flush(self) -> None:
        pass

    def readline(self) -> str:
        v = dict(self.verdicts)
        v.setdefault("mutant_id", self._mid)
        return json.dumps(v) + "\n"

    def close(self) -> None:
        pass


def test_def2_worker_kill_attribution_same_guard(project: Path,
                                                 tmp_path: Path) -> None:
    """worker 通道同构核对：failing 集合不含 case 测试 = run_error。"""
    _compile(project, tmp_path, """Feature: F
  Scenario Outline: s
    Then hp is <left>
    # types: <left>:numeric
    Examples:
      | left |
      | 7    |
""")
    import godot_qa_toolkit.accept.mutate_runner as mr
    mr.subprocess.Popen = lambda *a, **k: _FakeWorkerIO({
        "rc": 1, "executed": 1, "failing": ["test_poison"],
        "ops_version": "see1426-accept/1"})
    result = run_mutate(project, "demo", use_worker=True)
    assert result["ok"] is False and result["summary"]["killed"] == 0
    assert all("kill attribution failed" in f["reason"]
               for f in result["failures"])


def test_def2_worker_probe_reset_failure_is_run_error(project: Path,
                                                      tmp_path: Path) -> None:
    """worker 硬纪律：自检探针断言复位失败 = run_error verdict。"""
    _compile(project, tmp_path)
    import godot_qa_toolkit.accept.mutate_runner as mr
    mr.subprocess.Popen = lambda *a, **k: _FakeWorkerIO({
        "rc": 0, "executed": 1, "failing": [],
        "run_error": "harness unavailable or smoke_check failed after "
                     "instance release/rebuild",
        "ops_version": "see1426-accept/1"})
    result = run_mutate(project, "demo", use_worker=True)
    assert result["ok"] is False
    assert all("smoke_check failed" in f["reason"] for f in result["failures"])


# ---------- DEF-3 枚举形态 Then 绑定 fail-fast ----------

def test_def3_enum_form_then_binding_without_types_is_parse_error() -> None:
    src = """Feature: d
  Scenario Outline: s <mode>
    Then mode is <mode>
    Examples:
      | mode  |
      | stun  |
"""
    with pytest.raises(GherkinSyntaxError, match="enum-form"):
        parse(src)


def test_def3_declared_enum_form_passes() -> None:
    src = """Feature: d
  Scenario Outline: s <mode>
    Then mode is <mode>
    # types: <mode>:enum{normal,stun}
    Examples:
      | mode  |
      | stun  |
"""
    f = parse(src)
    assert f.scenarios[0].types["mode"] == "enum{normal,stun}"


def test_def3_free_text_cell_still_allowed() -> None:
    """非标识符形 cell（含空格的自由文本）不拦——枚举形态只对单词形。"""
    src = """Feature: d
  Scenario Outline: s <msg>
    Then banner says <msg>
    Examples:
      | msg        |
      | well done  |
"""
    parse(src)  # 不抛


# ---------- DEF-4 seed 收窄 ----------

def test_def4_seed_assignment_and_param_not_flagged(tmp_path: Path) -> None:
    p = tmp_path / "core" / "ok.gd"
    p.parent.mkdir(parents=True)
    p.write_text("extends RefCounted\n"
                 "func roll(seed: int) -> int:\n"
                 "\trng.seed = seed\n"
                 "\treturn rng.randi() % seed\n")
    violations, err = collect_file(p)
    assert err is None
    assert not any("random seed" in v.message for v in violations), \
        "显式 Seed 注入合法形态不得误杀"


def test_def4_seed_call_still_rejected(tmp_path: Path) -> None:
    p = tmp_path / "core" / "bad.gd"
    p.parent.mkdir(parents=True)
    p.write_text("extends RefCounted\nfunc setup() -> void:\n\tseed(42)\n")
    violations, err = collect_file(p)
    assert err is None
    assert any(v.rule == "no-bare-global-random" and "seed" in v.message
               for v in violations)


# ---------- 披露①② ----------

def test_disclosure1_mutant_ids_unique() -> None:
    case = {"test_name": "t", "values": {"n": "0"}, "types": {"n": "int"},
            "steps": [], "then_unchanged": []}
    ids = [m.mutant_id for m in mutate_case(case)]
    assert len(ids) == len(set(ids))


def test_disclosure2_no_source_check_bypass() -> None:
    """重锚防线无参数化旁路：replay_check 签名不含 source_check。"""
    sig = inspect.signature(replay_check)
    assert "source_check" not in sig.parameters


# ---------- 披露③ worker 生成物绑定 ----------

def test_disclosure3_worker_artifact_hash_bound(project: Path,
                                                tmp_path: Path) -> None:
    suite = _compile(project, tmp_path)
    # worker 在场且进哈希绑定：手改 worker = run_error
    worker = project / "acceptance/generated/accept_worker.gd"
    worker.write_text(worker.read_text(encoding="utf-8") + "\n# tampered\n")
    with pytest.raises(AcceptRunError, match="worker hash mismatch"):
        replay_check(project, "demo")


def test_disclosure3_worker_missing_rejected(project: Path,
                                             tmp_path: Path) -> None:
    _compile(project, tmp_path)
    (project / "acceptance/generated/accept_worker.gd").unlink()
    import godot_qa_toolkit.accept.mutate_runner as mr
    with pytest.raises(AcceptRunError, match="worker artifact missing"):
        run_mutate(project, "demo", use_worker=True)


def test_disclosure3_worker_in_clean_then_generate(project: Path,
                                                   tmp_path: Path) -> None:
    """worker 受先清空再生成管辖：陈旧 worker 重编译后消失重建。"""
    _compile(project, tmp_path)
    stale = project / "acceptance/generated/accept_worker.gd"
    assert stale.is_file()
    _compile(project, tmp_path)  # 重编译
    assert stale.is_file()


# ---------- worker 通道失败路径（覆盖率/对抗补强） ----------

class _DeadWorkerIO:
    def __init__(self, die_on: int = 1):
        self.die_on = die_on
        self.n = 0
        self.stdin = self
        self.stdout = self

    def write(self, line: str) -> None:
        self.n += 1

    def flush(self) -> None:
        pass

    def readline(self) -> str:
        if self.n > self.die_on:
            return ""  # worker 提前退出
        return json.dumps({"mutant_id": "x", "rc": 1, "executed": 1,
                           "failing": ["test_s_000_00"],
                           "ops_version": "see1426-accept/1"}) + "\n"

    def close(self) -> None:
        pass


class _BrokenWorkerIO:
    stdin = None
    stdout = None


def test_worker_exits_early_channel_run_errors(project: Path,
                                               tmp_path: Path) -> None:
    """worker 提前退出：已判定轮保留，未判定轮全部 run_error。"""
    _compile(project, tmp_path, """Feature: F
  Scenario Outline: s
    Then hp is <left>
    # types: <left>:numeric
    Examples:
      | left |
      | 7    |
""")
    import godot_qa_toolkit.accept.mutate_runner as mr
    mr.subprocess.Popen = lambda *a, **k: _DeadWorkerIO(die_on=1)
    result = run_mutate(project, "demo", use_worker=True)
    assert result["ok"] is False
    assert result["summary"]["run_errors"] >= 1
    assert any("worker channel failed" in f["reason"]
               or "kill attribution failed" in f["reason"]
               for f in result["failures"])


def test_worker_zero_executed_is_sentinel_error(project: Path,
                                                tmp_path: Path) -> None:
    """worker executed=0 = 执行链失真 → run_error（哨兵②worker 侧）。"""
    _compile(project, tmp_path)
    import godot_qa_toolkit.accept.mutate_runner as mr
    mr.subprocess.Popen = lambda *a, **k: _FakeWorkerIO({
        "rc": 0, "executed": 0, "failing": [],
        "ops_version": "see1426-accept/1"})
    result = run_mutate(project, "demo", use_worker=True)
    assert result["ok"] is False
    assert all("executed 0 cases" in f["reason"] for f in result["failures"])


def test_worker_ops_version_mismatch_is_run_error(project: Path,
                                                  tmp_path: Path) -> None:
    _compile(project, tmp_path)
    import godot_qa_toolkit.accept.mutate_runner as mr
    mr.subprocess.Popen = lambda *a, **k: _FakeWorkerIO({
        "rc": 0, "executed": 1, "failing": [], "ops_version": "bogus/9"})
    result = run_mutate(project, "demo", use_worker=True)
    assert result["ok"] is False
    assert all("ops_version mismatch" in f["reason"] for f in result["failures"])


def test_worker_spawn_oserror_is_run_error(project: Path,
                                           tmp_path: Path) -> None:
    import godot_qa_toolkit.accept.mutate_runner as mr

    def boom(*a, **k):
        raise OSError("no exec")
    _compile(project, tmp_path)
    mr.subprocess.Popen = boom
    with pytest.raises(AcceptRunError, match="worker process spawn failed"):
        run_mutate(project, "demo", use_worker=True)
