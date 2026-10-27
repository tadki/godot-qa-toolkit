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

from godot_qa_toolkit.accept import OPS_VERSION
from godot_qa_toolkit.accept.compile import compile_feature
from godot_qa_toolkit.accept.mutate import Mutant, mutate_case, mutate_ir
from godot_qa_toolkit.accept.mutate_runner import (
    AcceptRunError,
    _mutated_ir,
    run_mutate,
)
from godot_qa_toolkit.accept.run import replay_check
from godot_qa_toolkit.arch.collector import collect_file
from godot_qa_toolkit.accept.parser import GherkinSyntaxError, parse

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
        {"ops_version": OPS_VERSION, "feature": "F", "source_path": "s",
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
    calls = {"n": 0}

    def fake_stateful(*a, **k):
        calls["n"] += 1
        kill_after_baseline = kills_at >= 2  # F-C：n=1 恒基线绿
        class R:
            returncode = (1 if calls["n"] >= kills_at else 0)
            stdout = (KILL_BOTH if calls["n"] >= kills_at else PASS_ONE)
            stderr = ""
        return R()
    mr.subprocess.run = fake_stateful
    kills_at = 99  # 全绿形态：基线后全绿
    result = run_mutate(project, "demo")
    unchanged = [f for f in result["failures"]
                 if f.get("kind") == "unchanged"]
    assert len(unchanged) == 1 and result["ok"] is False

    # 挂链：unchanged mutant 被 kill（污染探针在位必挂）
    calls["n"] = 0
    kills_at = 2  # F-C：基线（n=1）绿，mutant 轮（n>=2）挂
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

    calls = {"n": 0}

    def fake_poison(*a, **k):
        calls["n"] += 1
        class R:
            # F-C：n=1 基线绿（真实判据流），毒测试落在 mutant 轮
            returncode = 0 if calls["n"] == 1 else 1
            stdout = ("* test_s_000_00\n  [Passed]\n"
                      if calls["n"] == 1 else "* test_poison\n  [Failed]\n")
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
        # F-C：基线请求回绿 verdict（executed 沿用脚本值满足计数守卫），
        # mutant 请求走脚本化 verdict。
        if self._mid == "exempt_baseline":
            v = {"rc": 0, "executed": 1, "failing": [],
                 "ops_version": OPS_VERSION}
        else:
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
        "ops_version": OPS_VERSION})
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
        "ops_version": OPS_VERSION})
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
    def __init__(self, die_on: int = 2):
        # die_on 以请求序计（1 = F-C 基线请求），基线请求恒回绿 verdict。
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
        if self.n == 1:  # F-C 基线请求：绿 verdict
            return json.dumps({"mutant_id": "exempt_baseline", "rc": 0,
                               "executed": 1, "failing": [],
                               "ops_version": OPS_VERSION}) + "\n"
        return json.dumps({"mutant_id": "x", "rc": 1, "executed": 1,
                           "failing": ["test_s_000_00"],
                           "ops_version": OPS_VERSION}) + "\n"

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
        "ops_version": OPS_VERSION})
    # 基线请求由 _FakeWorkerIO 回绿（executed=1 单 case 合规），零执行
    # 落在 mutant 轮（哨兵② mutant 侧语义保持）。
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


# ---------- QA-1 回归面：真实尺寸 IR 的 worker 读取路径 ----------

def _big_ir_bytes(min_size: int = 1600) -> tuple[dict, int]:
    """构造 ≥ min_size 字节的真实尺寸 IR（Revy 实测 1678B 量级）。"""
    cases = []
    for i in range(10):
        cases.append({
            "test_name": f"test_big_000_{i:02d}",
            "values": {"ticks": str(i + 1), "pad": "x" * 120},
            "types": {"ticks": "numeric"},
            "steps": [{"keyword": "Then",
                       "text": f"the probe reports {i + 1} ticks with detailed "
                               f"settlement narrative for row {i}",
                       "line": i + 2}],
            "then_unchanged": ["probe.alive", "world.tick"],
        })
    ir = {"ops_version": OPS_VERSION, "feature": "大矩阵",
          "source_path": "big.feature", "cases": cases}
    size = len(json.dumps(ir, ensure_ascii=False).encode())
    assert size >= min_size, f"fixture IR too small: {size}"
    return ir, size


def test_qa1_worker_request_line_under_line_buffer(tmp_path: Path) -> None:
    """QA-1 回归：ir_file 协议下请求行恒 <1024B（OS.read_string_from_stdin
    行缓冲上限），且变异 IR 本体落临时文件可被 FileAccess 读回。"""
    ir, size = _big_ir_bytes()
    from godot_qa_toolkit.accept.mutate import Mutant
    from godot_qa_toolkit.accept.mutate_runner import _mutated_ir
    mutants = []
    for i, case in enumerate(ir["cases"]):
        mutants.append(Mutant(
            mutant_id=f"{case['test_name']}#ticks=999",
            case_test_name=case["test_name"], target="ticks",
            kind="numeric", original=case["values"]["ticks"],
            mutated="999", rationale="+1"))
    # 全部 mutant 的请求行（ir_file 形态）必须远低于 1024B。
    for idx, m in enumerate(mutants):
        ir_file = tmp_path / f"mutant_{idx:04d}.json"
        mutated = _mutated_ir(ir, m)
        payload = json.dumps(mutated, ensure_ascii=False)
        assert len(payload.encode()) > 1024, "fixture must exceed line buffer"
        request = json.dumps({"mutant_id": m.mutant_id,
                              "ir_file": "res://.qa-cache/gqt-accept-worker/"
                                         f"mutant_{idx:04d}.json"})
        assert len(request.encode()) < 1024, request
        # 文件通道可读回（FileAccess 语义：Python 侧 json 等价读）。
        ir_file.write_text(payload, encoding="utf-8")
        assert json.loads(ir_file.read_text(encoding="utf-8")) == mutated


def test_qa1_worker_request_carries_no_inline_ir(project: Path,
                                                 tmp_path: Path) -> None:
    """worker 请求载荷不再内嵌 IR 本体（QA-1 挂死根因的协议级防回归）。"""
    _compile(project, tmp_path)
    captured: list[str] = []

    class _CaptureWorkerIO:
        def __init__(self):
            self.stdin = self
            self.stdout = self

        def write(self, line: str) -> None:
            captured.append(line)

        def flush(self) -> None:
            pass

        def readline(self) -> str:
            req = json.loads(captured[-1])
            return json.dumps({"mutant_id": req.get("mutant_id", "?"),
                               "rc": 1, "executed": 1,
                               "failing": [req.get("mutant_id", "?")],
                               "ops_version": OPS_VERSION}) + "\n"

        def close(self) -> None:
            pass

    import godot_qa_toolkit.accept.mutate_runner as mr
    mr.subprocess.Popen = lambda *a, **k: _CaptureWorkerIO()
    run_mutate(project, "demo", use_worker=True)
    assert captured, "worker 通道必须发出请求"
    for line in captured:
        req = json.loads(line)
        assert "ir" not in req, f"请求行内嵌 IR 本体（QA-1 根因形态）: {req.keys()}"
        assert "ir_file" in req, "请求必须走 ir_file 文件通道"


def test_qa1_worker_scratch_dir_cleaned(project: Path, tmp_path: Path) -> None:
    """worker 轮转临时文件用后即删：通道结束 .qa-cache/gqt-accept-worker
    不留残留。"""
    _compile(project, tmp_path)
    import godot_qa_toolkit.accept.mutate_runner as mr
    mr.subprocess.Popen = lambda *a, **k: _FakeWorkerIO({
        "rc": 1, "executed": 1,
        "failing": ["test_s_000_00", "test_s_000_01"],
        "ops_version": OPS_VERSION})
    run_mutate(project, "demo", use_worker=True)
    scratch = project / ".qa-cache" / "gqt-accept-worker"
    assert not scratch.exists() or not any(scratch.iterdir()), \
        "worker scratch 目录必须清空"


# ---------- Final Review MEDIUM-1：worker 读侧超时 ----------

class _HungWorkerIO:
    """worker 不回 verdict 的挂死形态：请求写入成功，stdout 永无数据
    （带 fileno 的假 fd 走 select 超时路径）。"""

    def __init__(self):
        import io
        self._in = io.StringIO()
        self.stdin = self
        self.stdout = _HungReader()

    def write(self, line: str) -> None:
        # F-C：基线请求回绿 verdict（autoload 就绪形态），挂死落在 mutant 轮
        import json as _json
        if _json.loads(line).get("mutant_id") == "exempt_baseline":
            self.stdout.feed_baseline()

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _HungReader:
    def __init__(self):
        import os
        self._r, self._w = os.pipe()  # 无数据可读 → select 超时
        os.set_blocking(self._r, False)

    def readline(self) -> str:
        import os
        try:
            return os.read(self._r, 4096).decode()
        except BlockingIOError:
            return ""

    def fileno(self) -> int:
        return self._r

    def feed_baseline(self) -> None:
        # F-C：基线请求回绿 verdict（写后即回，mutant 请求恢复挂死）
        import json as _json
        import os
        os.set_blocking(self._w, True)
        os.write(self._w, (_json.dumps({
            "mutant_id": "exempt_baseline", "rc": 0, "executed": 1,
            "failing": [], "ops_version": OPS_VERSION}) + "\n").encode())
        os.close(self._w)  # 后续 mutant 请求 read 到 EOF → 挂死超时面


def test_medium1_hung_worker_times_out_into_run_errors(project: Path,
                                                       tmp_path: Path) -> None:
    """worker 已启动却永不回 verdict（harness 挂死形态）：verdict 等待
    超时 → _WorkerDead → 剩余 mutant 全部 run_error（CLI 不再永久阻塞）。"""
    _compile(project, tmp_path, """Feature: F
  Scenario Outline: s
    Then hp is <left>
    # types: <left>:numeric
    Examples:
      | left |
      | 7    |
""")
    import godot_qa_toolkit.accept.mutate_runner as mr
    mr.subprocess.Popen = lambda *a, **k: _HungWorkerIO()
    import time
    t0 = time.monotonic()
    result = run_mutate(project, "demo", use_worker=True, timeout_s=1)
    elapsed = time.monotonic() - t0
    assert elapsed < 10, "挂死 worker 必须在 timeout 内收口，不得永久阻塞"
    assert result["ok"] is False and result["summary"]["killed"] == 0
    assert result["summary"]["run_errors"] == result["summary"]["mutants"]
    assert any("verdict timeout" in f["reason"] for f in result["failures"])


class _NoFilenoWorkerIO:
    """无 fileno 的假 worker stdout（队列形态，StringIO 位置语义不适用）
    ——_readline_with_timeout 的诚实降级分支：无 fileno → 阻塞 readline
    （fake 即时回包）。"""

    def __init__(self, verdicts: dict):
        from collections import deque
        self._lines: deque = deque()
        self.stdout = self
        self.stdin = self
        self._v = verdicts

    def write(self, line: str) -> None:
        import json as _json
        self._mid = _json.loads(line).get("mutant_id", "?")
        if self._mid == "exempt_baseline":
            # F-C 基线请求：绿 verdict（executed=1 对齐单 case fixture）
            v = {"rc": 0, "executed": 1, "failing": [],
                 "ops_version": OPS_VERSION}
        else:
            v = dict(self._v)
            v.setdefault("mutant_id", self._mid)
        self._lines.append(_json.dumps(v) + "\n")

    def readline(self) -> str:
        return self._lines.popleft() if self._lines else ""

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_medium1_no_fileno_fake_degrades_to_blocking_read(
        project: Path, tmp_path: Path) -> None:
    """无 fileno 的 fake stdout（StringIO 形态）：select 不可用 → 诚实
    降级阻塞 readline，verdict 照常判定。"""
    _compile(project, tmp_path, """Feature: F
  Scenario Outline: s
    Then hp is <left>
    # types: <left>:numeric
    Examples:
      | left |
      | 7    |
""")
    import godot_qa_toolkit.accept.mutate_runner as mr
    mr.subprocess.Popen = lambda *a, **k: _NoFilenoWorkerIO({
        "rc": 1, "executed": 1,
        "failing": ["test_s_000_00", "test_s_000_01"],
        "ops_version": OPS_VERSION})
    result = run_mutate(project, "demo", use_worker=True, timeout_s=5)
    assert result["ok"] is True
    assert result["summary"]["killed"] == result["summary"]["mutants"]


class _RaisingFilenoWorkerIO(_NoFilenoWorkerIO):
    """fileno() 存在但抛 OSError（io.StringIO 形态）——select 降级分支。"""

    def fileno(self) -> int:
        raise OSError("no real fd")


def test_medium1_fileno_raising_fake_degrades(project: Path,
                                              tmp_path: Path) -> None:
    _compile(project, tmp_path, """Feature: F
  Scenario Outline: s
    Then hp is <left>
    # types: <left>:numeric
    Examples:
      | left |
      | 7    |
""")
    import godot_qa_toolkit.accept.mutate_runner as mr
    mr.subprocess.Popen = lambda *a, **k: _RaisingFilenoWorkerIO({
        "rc": 1, "executed": 1,
        "failing": ["test_s_000_00", "test_s_000_01"],
        "ops_version": OPS_VERSION})
    result = run_mutate(project, "demo", use_worker=True, timeout_s=5)
    assert result["ok"] is True
    assert result["summary"]["killed"] == result["summary"]["mutants"]
