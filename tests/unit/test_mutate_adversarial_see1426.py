"""SEE-1426 Round B hardener — `gqt accept mutate` 对抗性单测（§SPEC-1426-11~15）。

对抗预设：默认变异闭环会放行作弊。覆盖既有套件未及的面：

- 变异器金测硬化（§SPEC-1426-12①）：负数最小值下探、浮点 ±1 精确形态、
  0→-1 边界、枚举全域扫描（首/中/尾邻值恰切、mutated 恒在域内）、布尔
  双向翻转、混合 kind 全矩阵恰好集合。
- 变形金测：_mutated_ir 只动目标 case，其余 case 逐字节不变。
- 闭环对抗（§SPEC-1426-13/15）：mutant 轮间隔离不累积（逐轮磁盘 IR 快照
  比对）、超时异常路径磁盘 IR 仍恢复、mutate 入口重放/重锚守卫联动
  （手改 IR / manifest 换锚必 run_error）、mutant 轮 launch failure 进
  run_error 面、部分 kill 不短路、伪造绿流仍判 survived、全量矩阵计数
  恒等变异器产出（runner 不得悄悄过滤）、空 enum 域 fail-closed。
- --worker（§SPEC-1426-14）：survive 侧差分对照。

发现披露（见 hardener 汇报，未修复）：unchanged mutant 变形 no-op、
kill 判定不核对失败归因、枚举域未声明的 Then 绑定不放 parse error、
seed 参数名误报、0-cell 重复 mutant、source_check=False 死参数。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from godot_qa_toolkit.accept import OPS_VERSION
from godot_qa_toolkit.accept.compile import compile_feature
from godot_qa_toolkit.accept.mutate import mutate_case, mutate_ir
from godot_qa_toolkit.accept.mutate_runner import (
    AcceptRunError,
    _mutated_ir,
    run_mutate,
)

KILL_OUTPUT = "* test_s_000_00\n  [Failed]\n* test_s_000_01\n  [Failed]\n"  # 双 case 各自 [Failed]——DEF-2 归因核对要求当轮 case 名在失败集合内
PASS_OUTPUT = "* test_s_000_00\n  [Passed]\n"


@pytest.fixture(autouse=True)
def _restore_subprocess():
    """fake 直接换全局 subprocess 模块属性——autouse 兜底还原防泄漏。"""
    import subprocess as sp
    import godot_qa_toolkit.accept.mutate_runner as mr
    orig_run, orig_popen = sp.run, sp.Popen
    yield
    sp.run, sp.Popen = orig_run, orig_popen
    mr.subprocess.run, mr.subprocess.Popen = orig_run, orig_popen


def _fake_gut(returncode: int, output: str,
              baseline: tuple[int, str] | None = None) -> None:
    """F-C：baseline=(rc, output) 时首调用（未变异基线轮）单独供给。"""
    import godot_qa_toolkit.accept.mutate_runner as mr

    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        if baseline is not None and calls["n"] == 1:
            rc, out = baseline
        else:
            rc, out = returncode, output

        class R:
            def __init__(self):
                self.returncode = rc
                self.stdout = out
                self.stderr = ""
        return R()
    mr.subprocess.run = fake_run


# ---------- 变异器金测硬化（§SPEC-1426-12① / -11 matrix） ----------

def test_golden_numeric_negative_and_float_matrix() -> None:
    # 最小值下探：负数 -5 → -4/-6，无 boundary（非 0 cell）；浮点 ±1 精确
    # 形态（repr 不失真：6.5/8.5 而非 6.50000001）。
    case = {"test_name": "t", "values": {"n": "-5", "f": "7.5"},
            "types": {"n": "int", "f": "numeric"}, "steps": [],
            "then_unchanged": []}
    by_target = {}
    for m in mutate_case(case):
        by_target.setdefault(m.target, set()).add((m.mutated, m.rationale))
    assert by_target["n"] == {("-4", "+1"), ("-6", "-1")}
    assert by_target["f"] == {("6.5", "-1"), ("8.5", "+1")}


def test_golden_zero_boundary_matrix() -> None:
    # 0-cell：±1 双向 + 边界外推（0→-1 越过下界）；-1 与 boundary 同值，
    # rationale 合并为单一 mutant（披露①去重：mutant_id 唯一，计数十
    # 不再出现重复轮）。
    case = {"test_name": "t", "values": {"n": "0"}, "types": {"n": "int"},
            "steps": [], "then_unchanged": []}
    got = {(m.mutated, m.rationale) for m in mutate_case(case)}
    assert got == {("1", "+1"), ("-1", "-1+boundary")}
    ids = [m.mutant_id for m in mutate_case(case)]
    assert len(ids) == len(set(ids)), "mutant_id must be unique"


def test_golden_enum_domain_sweep_all_neighbors_stay_in_domain() -> None:
    # 枚举全域金测（rev2 §四）：首成员仅后邻、尾成员仅前邻、中间双邻；
    # 域内任意成员的 mutants 恒落在声明域内（域外从不生成）。
    domain = ["alpha", "beta", "gamma"]
    case = {"test_name": "t", "values": {}, "types": {}, "steps": [],
            "then_unchanged": []}
    for idx, cell in enumerate(domain):
        c = dict(case, values={"m": cell}, types={"m": "enum{alpha,beta,gamma}"})
        got = sorted(m.mutated for m in mutate_case(c))
        expected = sorted(x for i, x in enumerate(domain) if abs(i - idx) == 1)
        assert got == expected, cell
        assert set(got) <= set(domain), "mutated must never leave the domain"


def test_golden_boolean_flip_bidirectional() -> None:
    seen = {}
    for cell in ("true", "false"):
        case = {"test_name": "t", "values": {"b": cell}, "types": {"b": "bool"},
                "steps": [], "then_unchanged": []}
        ms = mutate_case(case)
        assert len(ms) == 1 and ms[0].kind == "boolean"
        seen[cell] = ms[0].mutated
    assert seen == {"true": "false", "false": "true"}


def test_golden_mixed_kinds_full_matrix_exact() -> None:
    # 混合 kind 全矩阵恰好集合：数值 ±1 ×2 + 布尔 ×1 + 枚举邻值 ×2 +
    # unchanged ×1——不多一项不少一项（runner 全量门禁的期望基数）。
    case = {
        "test_name": "test_m_000_00",
        "values": {"n": "70", "b": "false", "m": "beta"},
        "types": {"n": "int", "b": "bool", "m": "enum{alpha,beta,gamma}"},
        "steps": [{"keyword": "Then", "text": "count is <n>", "line": 1}],
        "then_unchanged": ["world.tick"],
    }
    got = sorted((m.target, m.mutated, m.kind) for m in mutate_case(case))
    assert got == sorted([
        ("n", "69", "numeric"), ("n", "71", "numeric"),
        ("b", "true", "boolean"),
        ("m", "alpha", "enum"), ("m", "gamma", "enum"),
        ("world.tick", "<polluted>", "unchanged"),
    ])


# ---------- 变形金测（_mutated_ir） ----------

def _two_case_ir() -> dict:
    return {
        "ops_version": OPS_VERSION, "feature": "F", "source_path": "s",
        "cases": [
            {"test_name": "test_a_000_00", "values": {"left": "7"},
             "types": {"left": "numeric"},
             "steps": [{"keyword": "Then", "text": "hp is 7", "line": 2}],
             "then_unchanged": []},
            {"test_name": "test_b_000_00", "values": {"left": "9"},
             "types": {"left": "numeric"},
             "steps": [{"keyword": "Then", "text": "hp is 9", "line": 5}],
             "then_unchanged": []},
        ],
    }


def test_mutated_ir_hits_only_named_case() -> None:
    # 变形只动 mutant 指名的 case，其余 case 必须逐字段不变（串改即
    # verdict 串扰）。
    ir = _two_case_ir()
    from godot_qa_toolkit.accept.mutate import Mutant
    mutant = Mutant(mutant_id="x", case_test_name="test_a_000_00",
                    target="left", kind="numeric", original="7",
                    mutated="999", rationale="+1")
    out = _mutated_ir(ir, mutant)
    assert out["cases"][0]["values"] == {"left": "999"}
    assert out["cases"][1] == ir["cases"][1], "other case must be untouched"
    assert ir["cases"][0]["values"] == {"left": "7"}, "original must not mutate"


# ---------- 闭环对抗（runner 级） ----------

class _FakeWorkerIO:
    """真常驻 worker 通道的假进程：stdin 逐行收 NDJSON 请求，stdout 按
    脚本回 verdict（与 batch 假链同语义，供差分对照）。"""

    def __init__(self, rc: int, case_names: list[str]):
        self.rc = rc
        self.case_names = case_names
        self.stdin = self
        self.stdout = self
        self._closed = False

    def write(self, line: str) -> None:
        self._last = line

    def flush(self) -> None:
        pass

    def readline(self) -> str:
        import json as _json
        req = _json.loads(self._last)
        if req["mutant_id"] == "exempt_baseline":
            # F-C 基线请求：绿 verdict（执行链健康形态）
            return _json.dumps({
                "mutant_id": req["mutant_id"], "rc": 0,
                "executed": len(self.case_names), "failing": [],
                "ops_version": OPS_VERSION}) + "\n"
        return _json.dumps({
            "mutant_id": req["mutant_id"], "rc": self.rc,
            "executed": len(self.case_names),
            "failing": self.case_names if self.rc != 0 else [],
            "ops_version": OPS_VERSION}) + "\n"

    def close(self) -> None:
        self._closed = True


def _fake_worker(rc: int, case_names: list[str]) -> None:
    import godot_qa_toolkit.accept.mutate_runner as mr
    mr.subprocess.Popen = lambda *a, **k: _FakeWorkerIO(rc, case_names)


FEATURE = """Feature: F
  Scenario Outline: s
    Then hp is <left>
    # types: <hp>:int, <left>:numeric
    Examples:
      | hp | left |
      | 10 | 7    |
      | 20 | 5    |
"""


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "addons" / "gut").mkdir(parents=True)
    (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
    return root


def _compile(project: Path, tmp_path: Path, feature: str = FEATURE) -> Path:
    f = tmp_path / "demo.feature"
    f.write_text(feature, encoding="utf-8")
    suite, error = compile_feature(f, project, write=True)
    assert error is None
    return f


def _disk_ir(project: Path) -> dict:
    return json.loads(
        (project / "acceptance/ir/demo.json").read_text(encoding="utf-8"))


def test_mutant_rounds_isolated_no_accumulation(project: Path,
                                                tmp_path: Path) -> None:
    """逐 mutant 临时变异：每轮磁盘 IR 相对原 IR 恰好一处差异，轮间不
    累积（变异叠加会让后续轮的 verdict 无法归因到单一 mutant）。"""
    import godot_qa_toolkit.accept.mutate_runner as mr
    _compile(project, tmp_path)
    ir = _disk_ir(project)
    expected = {(m.case_test_name, m.target, m.mutated)
                for m in mutate_ir(ir)}
    snapshots: list[set] = []

    def fake_run(*a, **k):
        cur = _disk_ir(project)
        diffs = set()
        for case in cur["cases"]:
            for key, val in case["values"].items():
                orig = next(c for c in ir["cases"]
                            if c["test_name"] == case["test_name"])
                if orig["values"].get(key) != val:
                    diffs.add((case["test_name"], key, val))
        snapshots.append(diffs)
        class R:
            # F-C：首调用为未变异基线轮（零差异快照 + 绿），mutant 轮 kill
            returncode = 0 if len(snapshots) == 1 else 1
            stdout = PASS_OUTPUT if len(snapshots) == 1 else KILL_OUTPUT
            stderr = ""
        return R()
    mr.subprocess.run = fake_run
    result = run_mutate(project, "demo")
    assert result["summary"]["mutants"] == len(expected)
    mutant_snapshots = snapshots[1:]  # 首快照 = F-C 基线轮（未变异）
    assert all(len(d) == 1 for d in mutant_snapshots), \
        f"each round must carry exactly one mutation, got {mutant_snapshots}"
    union = set().union(*mutant_snapshots)
    assert union == expected, "rounds must cover the full matrix exactly"


def test_ir_restored_after_timeout_run_error(project: Path,
                                             tmp_path: Path) -> None:
    """mutant 轮超时 run_error：磁盘 IR 仍被 finally 恢复（异常路径不落
    病变 IR——恢复竞态的异常分支）。"""
    import godot_qa_toolkit.accept.mutate_runner as mr
    _compile(project, tmp_path)
    original = (project / "acceptance/ir/demo.json").read_text(encoding="utf-8")

    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="godot", timeout=1)
    mr.subprocess.run = fake_run
    with pytest.raises(AcceptRunError, match="timed out"):
        run_mutate(project, "demo")
    assert (project / "acceptance/ir/demo.json").read_text(
        encoding="utf-8") == original, "IR must be restored even on timeout"


def test_mutate_replay_guard_rejects_tampered_ir(project: Path,
                                                 tmp_path: Path) -> None:
    """手改磁盘 IR 后 mutate：入口重放校验必拒（带病 IR 不得进变异轮）。"""
    _compile(project, tmp_path)
    ir_file = project / "acceptance/ir/demo.json"
    ir = json.loads(ir_file.read_text(encoding="utf-8"))
    ir["cases"][0]["values"]["left"] = "777"
    ir_file.write_text(json.dumps(ir, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(AcceptRunError, match="IR hash mismatch"):
        run_mutate(project, "demo")


def test_mutate_reanchor_guard_rejects_manifest_repoint(
        project: Path, tmp_path: Path) -> None:
    """重锚绕过新向量：manifest.source_path 换指另一合法 .feature——
    重编译 IR 的 source_path 字段随之漂移，哈希必失配 → run_error。"""
    _compile(project, tmp_path)
    other = tmp_path / "other.feature"
    other.write_text("Feature: Other\n  Scenario: s\n    Given a\n",
                     encoding="utf-8")
    man = project / "acceptance/generated/demo.manifest.json"
    m = json.loads(man.read_text(encoding="utf-8"))
    m["source_path"] = str(other)
    man.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(AcceptRunError, match="recompile hash mismatch"):
        run_mutate(project, "demo")


def test_mutant_round_launch_failure_sentinel(project: Path,
                                              tmp_path: Path) -> None:
    """mutant 轮 godot 启动失败：执行链失真 → run_error 面失败（不产
    kill verdict），整轮打回。"""
    _compile(project, tmp_path)
    # F-C：基线绿先行，启动失败落在 mutant 轮（per-mutant run_error 面）
    _fake_gut(1, "Godot Engine v4.6\nAttempt to open script file not found",
              baseline=(0, PASS_OUTPUT))
    result = run_mutate(project, "demo")
    assert result["ok"] is False
    assert result["summary"]["run_errors"] == result["summary"]["mutants"]
    assert result["summary"]["killed"] == 0
    assert all("launch failed" in f["reason"] for f in result["failures"])


def test_sentinel_partial_kill_does_not_shortcircuit(project: Path,
                                                     tmp_path: Path) -> None:
    """首轮 kill 后不得短路：剩余 mutant 轮照跑，逐轮独立判定。"""
    import godot_qa_toolkit.accept.mutate_runner as mr
    _compile(project, tmp_path)
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        class R:
            # F-C：n=1 基线绿，n=2 首 mutant kill，其余绿
            returncode = 1 if calls["n"] == 2 else 0
            stdout = KILL_OUTPUT if calls["n"] == 2 else PASS_OUTPUT
            stderr = ""
        return R()
    mr.subprocess.run = fake_run
    result = run_mutate(project, "demo")
    assert result["summary"]["killed"] == 1
    assert result["summary"]["survived"] == result["summary"]["mutants"] - 1
    assert result["ok"] is False


def test_sentinel_forged_green_stream_still_survives(
        project: Path, tmp_path: Path) -> None:
    """哨兵失真对抗：mutant 轮输出带合法测试流但全绿（恒真测试形态）——
    有流只证明在跑，绿即 survived 打回，不得因流在场而判 kill/绿。"""
    _compile(project, tmp_path)
    _fake_gut(0, "* test_s_000_00\n  [Passed]\n- test_s_000_00\n")
    result = run_mutate(project, "demo")
    assert result["ok"] is False
    assert result["summary"]["survived"] == result["summary"]["mutants"]


def test_mutants_count_equals_full_matrix(project: Path,
                                          tmp_path: Path) -> None:
    """全量门禁（v1 无 stamp）：runner 的 mutants 计数恒等于变异器对
    磁盘 IR 的全矩阵产出，killed == mutants（无静默过滤/跳过）。"""
    _compile(project, tmp_path)
    _fake_gut(1, KILL_OUTPUT, baseline=(0, PASS_OUTPUT))
    result = run_mutate(project, "demo")
    expected = len(mutate_ir(_disk_ir(project)))
    assert result["summary"]["mutants"] == expected
    assert result["summary"]["full_matrix"] is True
    assert result["summary"]["killed"] == expected
    assert result["ok"] is True


def test_worker_differential_survive_side(project: Path,
                                         tmp_path: Path) -> None:
    """--worker 差分对照（survive 侧）：batch 与 worker 两条真实代码路径
    在同一执行链语义下 verdict 与 survived 计数一致（披露③：worker 不再
    是 summary 披露假通道，走 Popen NDJSON 带内路径）。"""
    _compile(project, tmp_path)
    _fake_gut(0, PASS_OUTPUT)
    batch = run_mutate(project, "demo", use_worker=False)
    _fake_worker(0, ["test_s_000_00", "test_s_000_01"])
    worker = run_mutate(project, "demo", use_worker=True)
    assert batch["ok"] is False and worker["ok"] is False
    assert batch["summary"]["survived"] == worker["summary"]["survived"]
    assert worker["summary"]["worker"] is True
    assert batch["summary"]["worker"] is False


# ---------- 枚举专项 / 必修面对抗补强 ----------

def test_empty_enum_domain_feature_is_fail_closed(project: Path,
                                                  tmp_path: Path) -> None:
    """空 enum 域声明（enum{}）：合法 parse 但零变异面 → mutate fail-closed
    run_error（不产空门禁绿）。"""
    feature = """Feature: E
  Scenario Outline: s <mode>
    Then mode is <mode>
    # types: <mode>:enum{}
    Examples:
      | mode |
      | a    |
"""
    f = tmp_path / "empty.feature"
    f.write_text(feature, encoding="utf-8")
    suite, error = compile_feature(f, project, write=True)
    assert error is None
    with pytest.raises(AcceptRunError, match="no mutants generated"):
        run_mutate(project, "empty")


def test_seed_call_with_variable_argument_rejected(tmp_path: Path) -> None:
    # -26 补强：seed(<变量>) 实参形态同打回（裸全局种子调用不限于字面量）。
    from godot_qa_toolkit.arch.collector import collect_file
    p = tmp_path / "core" / "s.gd"
    p.parent.mkdir(parents=True)
    p.write_text("extends RefCounted\nfunc setup(v: int) -> void:\n\tseed(v)\n")
    violations, err = collect_file(p)
    assert err is None
    assert any(v.rule == "no-bare-global-random" and "seed" in v.message
               for v in violations)


def test_feature_key_same_source_recompile_is_idempotent(
        project: Path, tmp_path: Path) -> None:
    """-27 不误伤常规流程：同源 .feature 重编译（clean-then-generate）
    幂等放行，冲突检测只对异源同 key 报错。"""
    f = tmp_path / "demo.feature"
    f.write_text("Feature: One\n  Scenario: s\n    Given a\n", encoding="utf-8")
    suite1, err1 = compile_feature(f, project, write=True)
    suite2, err2 = compile_feature(f, project, write=True)
    assert err1 is None and err2 is None
    assert suite1.feature_key == suite2.feature_key
    assert (project / "acceptance/ir" / f"{suite2.feature_key}.json").is_file()
