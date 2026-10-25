"""accept mutate runner（SEE-1426 P3，§SPEC-1426-12/13/14；SEE-1468 /2 豁免轮）。

闭环：对 IR 全量产 mutants → 逐 mutant 变异 IR → 同一套生成测试判定
kill/survived。v1 无 stamp：每轮全量、exit 非 0 即打回。

豁免轮（/2，定案 §4——exempt ≠ skip）：`# mutation: exempt` 场景的
typed 矩阵不产 mutants，但其 case 在 mutate 侧仍执行一次**未变异基线轮**
（任何在场豁免 case 都触发，先于 mutant 轮）：绿 → 逐 case 记
`exempt_verified`；挂 → 逐 case 记 run_error 计入门禁；未变异基线任何
case 挂 = 特性先于变异就红了 → fail-fast 跳过 mutant 轮（verdict 无意义）。
纯豁免特性（零 mutants + 有豁免 case）不再触发 `no mutants generated`
run_error，改走豁免基线轮收口（结构面走 exempt_verified 基线执行）。
summary 机器导出：`exempt_cases` == 源 .feature 内 `# mutation: exempt`
指令行数（per-scenario 去重），`exempt_results` 逐 case 具名披露（V4）。

双通道（差分对照验收，§SPEC-1426-14）：
- batch（缺省）：逐 mutant 写临时变异 IR（磁盘 IR 每轮恢复）→ 一次性
  headless GUT 子进程，跑完即弃。
- --worker：真常驻通道——compile 产出的 worker 生成物
  （acceptance/generated/accept_worker.gd，受先清空再生成管辖）以
  stdin NDJSON 循环消费变异 IR（IR 走文件通道传递，磁盘零改写），
  每 mutant 结束硬纪律：释放全部 testing_api 实例 + 重建（reload）+
  自检探针断言复位，不符即 run_error。
  /2：run_step await 化后 worker 循环协程化（_drive fire-and-forget），
  协议不变——读侧一请求一 verdict 串行，无异步消费竞争面（契约评审
  收口结论，见 worker 生成物头注）。

kill 判定（DEF-2，与 gqt mutation 的 kill=失败差集语义同构）：mutant 轮
rc≠0 时必须核对失败测试集合确含该 mutant 的 case 测试名——恒挂毒测试
（失败集合不含 case 测试）不得记 kill，归执行链失真 → run_error。

三层防伪栈：
① 变异器对抗性金测（70/71 fixture 断言产出恰好 ±1/边界/邻值）——单测面。
② 跨进程哨兵核对：mutant 轮 GUT 输出必须有测试流证据（batch）/ 有
   executed 计数（worker），零证据 = 执行链失真 = run_error。
③ unchanged 槽变异：mutated_unchanged 基线污染探针——比对块在位必挂；
   挂不掉 = survived 即打回。
"""

from __future__ import annotations

import json
import re
import select
import shutil
import subprocess
from pathlib import Path

from . import OPS_VERSION
from .compile import GENERATED_DIR
from .mutate import Mutant, mutate_ir
from .run import AcceptRunError, replay_check
from ..mutation.gutrun import (_looks_like_godot_launch_failure,
                               _parse_failing_tests)
from ..mutation.inject import godot_command, register_temp
from ..mutation.godot_gate import GlobalGodotGate


def _mutated_ir(ir: dict, mutant: Mutant) -> dict:
    """深拷贝 IR 并应用单 mutant（原 IR 原地不动——变异落临时副本）。"""
    m = json.loads(json.dumps(ir, ensure_ascii=False))
    for case in m.get("cases", []):
        if case.get("test_name") != mutant.case_test_name:
            continue
        if mutant.kind == "unchanged":
            # DEF-1：unchanged mutant 把目标槽标进 mutated_unchanged——
            # 生成测试/worker 对该槽取污染基线，比对必失配（比对块在位
            # 则必挂；被剥离则 survived 打回）。
            mutated: list = case.setdefault("mutated_unchanged", [])
            if mutant.target not in mutated:
                mutated.append(mutant.target)
        else:
            case.setdefault("values", {})[mutant.target] = mutant.mutated
    return m


def _run_gut(root: Path, timeout_s: int) -> tuple[int, str]:
    try:
        with GlobalGodotGate(str(root)):
            r = subprocess.run(
                [godot_command(), "--headless", "--path", str(root),
                 "-s", "res://addons/gut/gut_cmdln.gd",
                 "-gdir=res://" + GENERATED_DIR, "-gexit"],
                capture_output=True, text=True, timeout=timeout_s,
                cwd=str(root))
    except subprocess.TimeoutExpired as e:
        raise AcceptRunError(
            f"GUT timed out after {timeout_s}s on mutant round") from e
    return r.returncode, (r.stdout or "") + "\n" + (r.stderr or "")


# GUT 运行流证据：运行流 `* test_x` / Run Summary `- test_x` 行。
_TEST_STREAM_RE = re.compile(r"(?m)^[*-]\s+(test_\S+)")


def _count_test_stream(output: str) -> int:
    return len(set(_TEST_STREAM_RE.findall(output)))


def _mutant_round_error(mutant: Mutant, output: str) -> str | None:
    """mutant 轮的执行链失真检查：返回 run_error reason 或 None（可判定）。

    哨兵核对②：GUT 输出必须证明测试真的在跑（有运行流证据）；零测试流 =
    执行链失真 → 整轮 run_error（不产 verdict）。
    """
    if _looks_like_godot_launch_failure(output):
        return (f"godot launch failed on mutant round: "
                f"{' / '.join(output.splitlines()[-3:])}")
    if _count_test_stream(output) == 0:
        return ("sentinel check failed — no test stream in GUT "
                "output; execution chain untrusted")
    return None


def run_mutate(project_root: str | Path, feature_key: str, *, timeout_s: int = 120,
               use_worker: bool = False) -> dict:
    """全量契约变异闭环。返回统一 JSON 契约（ok=false 即打回）。

    /2：在场豁免 case 先跑未变异基线轮（exempt ≠ skip）；基线挂 =
    fail-fast 跳过 mutant 轮（未变异就红的特性没有变异 verdict 可言）。
    """
    root = Path(project_root)
    gut = root / "addons" / "gut" / "gut_cmdln.gd"
    if not gut.is_file():
        raise AcceptRunError(f"GUT runner not found: {gut}")
    manifest = replay_check(project_root, feature_key)
    ir_path = root / manifest["ir_path"].removeprefix("res://")
    ir = json.loads(ir_path.read_text(encoding="utf-8"))
    mutants = mutate_ir(ir)
    exempt = [c for c in ir.get("cases", []) if c.get("mutation_exempt")]
    exempt_count = _exempt_scenario_count(ir)
    exempt_results: list[dict] = []
    baseline_failed: list[dict] = []
    if exempt:
        exempt_results, baseline_failed = _run_exempt_baseline(
            root, ir, exempt, timeout_s, use_worker)
    if not mutants:
        if exempt:
            # 纯豁免特性（结构面）：exempt_verified 基线执行收口，不再
            # 触发 no-mutants run_error（定案 §4 no-mutation-face 语义）。
            return _exempt_only_contract(manifest, exempt_count,
                                         exempt_results, baseline_failed,
                                         use_worker)
        raise AcceptRunError(
            "no mutants generated — IR has no typed Then bindings and no "
            "unchanged slots; mutation gate cannot run (nothing to verify)")
    if baseline_failed:
        # 未变异基线红 = 特性本身坏了：fail-fast，mutant 轮 skip。
        return _baseline_failed_contract(manifest, mutants, exempt_count,
                                         exempt_results, baseline_failed,
                                         use_worker)
    if use_worker:
        verdicts = _run_worker_channel(root, ir, mutants,
                                       timeout_s=timeout_s)
    else:
        verdicts = _run_batch_channel(root, ir_path, ir, mutants, timeout_s)
    return _aggregate_verdicts(manifest, mutants, verdicts, use_worker,
                               exempt_count, exempt_results)


# ---------- /2 豁免基线轮（exempt ≠ skip） ----------

def _exempt_scenario_count(ir: dict) -> int:
    """豁免 scenario 数 == 源 .feature 内 `# mutation: exempt` 指令行数
    （V4 机器判据：每 scenario 恰好一行，重复行 parse 期已拒）。"""
    return len({c.get("scenario_line")
                for c in ir.get("cases", []) if c.get("mutation_exempt")})


def _run_exempt_baseline(root: Path, ir: dict, exempt: list[dict],
                         timeout_s: int,
                         use_worker: bool) -> tuple[list[dict], list[dict]]:
    """豁免基线轮双通道分流 + 逐 case 记账（exempt ≠ skip）。"""
    if use_worker:
        failing, executed = _worker_exempt_baseline(root, ir, exempt,
                                                    timeout_s)
        return _exempt_accounting(ir, exempt, failing, executed,
                                  worker_count_guard=True)
    failing, executed = _batch_exempt_baseline(root, timeout_s)
    return _exempt_accounting(ir, exempt, failing, executed)


def _batch_exempt_baseline(root: Path,
                           timeout_s: int) -> tuple[set[str], set[str]]:
    """batch 通道未变异基线轮：直接跑一次 GUT。返回 (failing, executed)。"""
    rc, output = _run_gut(root, timeout_s)
    if _looks_like_godot_launch_failure(output):
        raise AcceptRunError(
            "godot launch failed on exempt baseline round: "
            f"{' / '.join(output.splitlines()[-3:])}")
    executed = set(_TEST_STREAM_RE.findall(output))
    if not executed:
        raise AcceptRunError(
            "sentinel check failed — no test stream in exempt baseline "
            "round; execution chain untrusted")
    return set(_parse_failing_tests(output)), executed


def _spawn_worker(root: Path) -> tuple[subprocess.Popen, Path]:
    """常驻 worker 进程生成前奏（豁免基线轮与 mutant 轮共用）：生成物
    在位校验 + scratch 目录准备 + Popen（OSError = caller 错误面，
    不产 verdict 契约）。返回 (proc, scratch_dir)。"""
    worker_res = f"res://{GENERATED_DIR}/accept_worker.gd"
    if not (root / GENERATED_DIR / "accept_worker.gd").is_file():
        raise AcceptRunError(
            f"worker artifact missing: {worker_res} — recompile with "
            f"gqt accept compile --write")
    scratch = Path(root) / ".qa-cache" / "gqt-accept-worker"
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen(
            [godot_command(), "--headless", "--path", str(root),
             "-s", worker_res],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, cwd=str(root))
    except OSError as e:
        raise AcceptRunError(f"worker process spawn failed: {e}") from e
    return proc, scratch


def _exchange_verdict_line(proc, request: str,  # noqa: ANN001 — Popen/fake 双形态
                           timeout_s: float) -> str:
    """写一行请求并读回首个 "{" 开头的 verdict 行（Godot 启动横幅/引擎
    告警与 verdict 同走 stdout——噪声行跳过，EOF 返回空串）。IO 异常由
    调用方按各自通道语义转译（mutant 轮 → _WorkerDead，豁免基线轮 →
    AcceptRunError）。"""
    proc.stdin.write(request + "\n")
    proc.stdin.flush()
    line = _readline_with_timeout(proc, timeout_s)
    while line and not line.lstrip().startswith("{"):
        line = _readline_with_timeout(proc, timeout_s)
    return line


def _worker_exempt_baseline(root: Path, ir: dict, exempt: list[dict],
                            timeout_s: int) -> tuple[set[str], set[str]]:
    """worker 通道豁免基线轮：短命常驻进程送一请求（mutant_id=
    exempt_baseline，IR 为未变异原文），判完即弃——与 mutant 轮长驻通道
    分离，保持差分对照路径互不干扰。返回 (failing, executed)。"""
    proc, scratch = _spawn_worker(root)
    try:
        ir_file = scratch / "exempt_baseline.json"
        ir_file.write_text(json.dumps(ir, ensure_ascii=False),
                           encoding="utf-8")
        register_temp(ir_file)
        ir_file_res = "res://" + ir_file.relative_to(Path(root)).as_posix()
        request = json.dumps({"mutant_id": "exempt_baseline",
                              "ir_file": ir_file_res}, ensure_ascii=False)
        line = _exchange_verdict_line(proc, request, timeout_s)
        if not line:
            raise _WorkerDead("worker exited before exempt baseline verdict")
        verdict = json.loads(line)
    except (_WorkerDead, OSError, ValueError) as e:
        # 通道级失败 = 执行链不可信，与 batch 哨兵同档：raise（caller
        # 错误面，不产 verdict 契约）。
        raise AcceptRunError(
            f"exempt baseline worker channel failed: {e}") from e
    finally:
        _shutdown_worker(proc)
        shutil.rmtree(scratch, ignore_errors=True)
    total = len(ir.get("cases", []))
    err = verdict.get("run_error")
    executed_n = int(verdict.get("executed", 0))
    failing = {str(x) for x in verdict.get("failing", [])}
    if err is not None:
        raise AcceptRunError(f"exempt baseline worker run_error: {err}")
    if verdict.get("ops_version") != OPS_VERSION:
        raise AcceptRunError(
            f"exempt baseline worker ops_version mismatch: "
            f"{verdict.get('ops_version')!r} != {OPS_VERSION!r}")
    if executed_n != total:
        # executed 计数与 case 总数失配 = 执行链失真（哨兵②同构）。
        raise AcceptRunError(
            f"exempt baseline executed {executed_n} of {total} cases; "
            "execution chain untrusted")
    # executed 集合形态仅 batch 需要（逐 case 在场校验）；worker 侧已由
    # executed 计数守卫兜底，空集传入 _exempt_accounting 的 worker 分支。
    return failing, set()


def _exempt_accounting(ir: dict, exempt: list[dict],
                       failing: set[str], executed: set[str],
                       worker_count_guard: bool = False,
                       ) -> tuple[list[dict], list[dict]]:
    """基线轮逐 case 记账（V4 具名披露）。batch 通道 executed 为实测
    测试名集合（逐 case 校验在场）；worker 通道以计数守卫代之（上游
    executed==total 校验已确保全量执行，worker_count_guard=True 跳过
    逐 case 在场检查）。返回 (exempt_results, baseline_failed)。"""
    results: list[dict] = []
    for c in exempt:
        name = c.get("test_name", "?")
        if name in failing:
            status = "run_error: exempt case failed the unmutated baseline"
        elif not worker_count_guard and name not in executed:
            status = "run_error: exempt case absent from executed test stream"
        else:
            status = "exempt_verified"
        results.append({"test_name": name, "scenario": c.get("scenario"),
                        "status": status})
    baseline_failed = [
        {"mutant_id": "exempt_baseline",
         "reason": f"unmutated baseline round failed on case {name!r} — "
                   f"mutant rounds skipped (feature red before mutation)"}
        for name in sorted(failing)
    ]
    return results, baseline_failed


def _exempt_only_contract(manifest: dict, exempt_count: int,
                          exempt_results: list[dict],
                          baseline_failed: list[dict],
                          use_worker: bool) -> dict:
    """纯豁免特性（零 mutants）：豁免基线执行收口。"""
    run_errors = [
        {"mutant_id": "exempt_baseline", "reason": r["status"]}
        for r in exempt_results if r["status"] != "exempt_verified"
    ] + baseline_failed
    ok = not run_errors
    return {
        "tool": "accept",
        "ok": ok,
        "summary": {
            "feature": manifest["feature"],
            "feature_key": manifest["feature_key"],
            "ops_version": OPS_VERSION,
            "mutants": 0, "killed": 0, "survived": 0,
            "run_errors": len(run_errors),
            "full_matrix": True, "worker": use_worker,
            "exempt_cases": exempt_count,
            "exempt_results": exempt_results,
        },
        "failures": run_errors,
    }


def _baseline_failed_contract(manifest: dict, mutants: list[Mutant],
                              exempt_count: int, exempt_results: list[dict],
                              baseline_failed: list[dict],
                              use_worker: bool) -> dict:
    """未变异基线红 → fail-fast：mutant 轮不跑，run_errors 计入门禁。"""
    return {
        "tool": "accept",
        "ok": False,
        "summary": {
            "feature": manifest["feature"],
            "feature_key": manifest["feature_key"],
            "ops_version": OPS_VERSION,
            "mutants": len(mutants), "killed": 0, "survived": 0,
            "run_errors": len(baseline_failed),
            "full_matrix": False, "worker": use_worker,
            "exempt_cases": exempt_count,
            "exempt_results": exempt_results,
            "exempt_baseline_failed": True,
        },
        "failures": baseline_failed,
    }


def _run_batch_channel(root: Path, ir_path: Path, ir: dict,
                       mutants: list[Mutant],
                       timeout_s: int) -> list[tuple[Mutant, str, str]]:
    """batch 通道：逐 mutant 临时变异 IR → 一次性 headless GUT。

    变异 IR 是临时副本：无论跑成什么样，磁盘 IR 在 finally 恢复原样
    （写入面白名单内恢复语义，同 mutation 的临时改写必然恢复）。
    """
    original_ir_json = ir_path.read_text(encoding="utf-8")
    verdicts: list[tuple[Mutant, str, str]] = []
    try:
        for mutant in mutants:
            ir_path.write_text(
                json.dumps(_mutated_ir(ir, mutant), ensure_ascii=False,
                           indent=2, sort_keys=True), encoding="utf-8")
            rc, output = _run_gut(root, timeout_s)
            err = _mutant_round_error(mutant, output)
            if err is None and rc != 0:
                err = _kill_attribution_error(mutant, output)
            verdicts.append((mutant, _verdict_of(err, rc), err or ""))
    finally:
        ir_path.write_text(original_ir_json, encoding="utf-8")
    return verdicts


def _aggregate_verdicts(manifest: dict, mutants: list[Mutant],
                        verdicts: list[tuple[Mutant, str, str]],
                        use_worker: bool,
                        exempt_count: int = 0,
                        exempt_results: list[dict] | None = None) -> dict:
    """verdict 归并统一契约：survived 明细 + run_errors + 计数汇总。"""
    killed = 0
    survived: list[dict] = []
    run_errors: list[dict] = []
    for mutant, verdict, reason in verdicts:
        if verdict == "killed":
            killed += 1
        elif verdict == "survived":
            survived.append({
                "mutant": mutant.mutant_id,
                "target": mutant.target, "kind": mutant.kind,
                "original": mutant.original, "mutated": mutant.mutated,
                "rationale": mutant.rationale,
                "reason": "mutant survived — mutated expectation still "
                          "passes; assertion surface incomplete",
            })
        else:
            run_errors.append({"mutant": mutant.mutant_id, "reason": reason})

    ok = not survived and not run_errors
    return {
        "tool": "accept",
        "ok": ok,
        "summary": {
            "feature": manifest["feature"],
            "feature_key": manifest["feature_key"],
            "ops_version": OPS_VERSION,
            "mutants": len(mutants),
            "killed": killed,
            "survived": len(survived),
            "run_errors": len(run_errors),
            "full_matrix": True,       # v1 无 stamp：每轮全量
            "worker": use_worker,
            "exempt_cases": exempt_count,
            "exempt_results": exempt_results or [],
        },
        "failures": survived + run_errors,
    }


def _kill_attribution_error(mutant: Mutant, output: str) -> str | None:
    """DEF-2：rc≠0 时核对失败集合确含该 mutant 的 case 测试名。

    与 gqt mutation 的 kill=失败差集语义同构——恒挂毒测试（失败集合不
    含 case 测试）不能替 mutant 顶罪，归执行链失真 → run_error。
    """
    failing = _parse_failing_tests(output)
    if mutant.case_test_name in failing:
        return None
    return (f"kill attribution failed — failing set {sorted(failing)} does not "
            f"name the mutated case test {mutant.case_test_name!r}; execution "
            f"chain untrusted")


def _verdict_of(round_error: str | None, rc: int) -> str:
    if round_error is not None:
        return "run_error"
    return "killed" if rc != 0 else "survived"


# ---------- 真常驻 worker 通道（披露③，§SPEC-1426-14） ----------

class _WorkerDead(Exception):
    """worker 进程意外退出 / verdict 流中断——通道级 run_error。"""


def _run_worker_channel(root: Path, ir: dict, mutants: list[Mutant],
                        timeout_s: int = 120,
                        ) -> list[tuple[Mutant, str, str]]:
    """真常驻 worker：一次性 Popen，stdin NDJSON 逐 mutant 消费变异 IR。

    QA-1 传输协议：`OS.read_string_from_stdin()` 有 1024 字节行缓冲上限
    （Revy 实机二分实证：1024B OK / 1025B FAIL），真实 IR 带内传递必挂
    死——请求行只带 {mutant_id, ir_file} 小载荷，变异 IR 本体写
    `<root>/.qa-cache/gqt-accept-worker/` 下的轮转临时文件（contract §6
    白名单目录，注册进进程清理表，用后即删），worker 经 FileAccess 读
    （零行缓冲限制）。

    worker 侧硬纪律（实例释放 + 重建 + smoke 探针）由生成物内实现，
    探针失败以 run_error verdict 回传。

    MEDIUM-1（Final Review）：读侧带超时——`readline` 阻塞无时限会让
    "worker 已启动却永不回 verdict"（如被测项目 harness 在 mutant 轮内
    挂死）把 CLI 永久卡死；fail-loud 纪律下挂死比失败更糟。复用
    `--timeout` 语义：每轮 verdict 等待上限 = timeout_s，超时即
    _WorkerDead → 剩余 mutant 记 run_error。
    """
    proc, scratch = _spawn_worker(root)
    verdicts: list[tuple[Mutant, str, str]] = []
    try:
        for idx, mutant in enumerate(mutants):
            ir_file = scratch / f"mutant_{idx:04d}.json"
            try:
                ir_file.write_text(
                    json.dumps(_mutated_ir(ir, mutant), ensure_ascii=False),
                    encoding="utf-8")
                register_temp(ir_file)
                verdicts.append(_worker_round(proc, ir_file, mutant,
                                              timeout_s=timeout_s, root=root))
            finally:
                # 用后即删（白名单目录内的轮转临时文件，防残留）。
                ir_file.unlink(missing_ok=True)
    except _WorkerDead as e:
        # 通道级失败：已判定轮保留，未判定轮全部记 run_error。
        while len(verdicts) < len(mutants):
            m = mutants[len(verdicts)]
            verdicts.append((m, "run_error", f"worker channel failed: {e}"))
    finally:
        _shutdown_worker(proc)
        shutil.rmtree(scratch, ignore_errors=True)
    return verdicts


def _readline_with_timeout(proc, timeout_s: float) -> str:  # noqa: ANN001
    """带超时的行读（MEDIUM-1）：select/poll 不可用的假 fd 直接走阻塞
    readline（测试 fake 恒即时回包，不受影响）；真进程超时抛
    _WorkerDead——挂死是通道级失败，不是可等待的慢。"""
    fd = proc.stdout
    raw = getattr(fd, "fileno", None)
    if raw is None:
        return fd.readline()  # 测试 fake：StringIO 无 fileno
    try:
        selectable = raw()
        ready, _, _ = select.select([selectable], [], [], timeout_s)
    except (OSError, ValueError, AttributeError):
        return fd.readline()  # fd 不可 select（重定向等）：诚实降级阻塞
    if not ready:
        raise _WorkerDead(
            f"worker verdict timeout after {timeout_s}s — hung mutant "
            f"round (harness deadlock?), execution chain untrusted")
    return fd.readline()


def _worker_round(proc, ir_file: Path,  # noqa: ANN001 — Popen/fake 双形态
                  mutant: Mutant, timeout_s: int = 120,
                  root: Path | None = None,
                  ) -> tuple[Mutant, str, str]:
    """单 mutant 的 worker 往返 + 判定（哨兵②/DEF-2 worker 侧同构核对）。

    QA-1：请求行 = {mutant_id, ir_file}（恒 <1024B，行缓冲安全）；verdict
    行本身仍是小 JSON，带内回传。MEDIUM-1：verdict 等待上限 = timeout_s。

    ir_file 的 res:// 解析锚 = 调用方显式传入的项目根（与 `_spawn_worker`
    的 cwd 同源）——**不嗅探 proc.cwd**：真实 `subprocess.Popen` 不记录
    cwd，嗅探必然落 `Path("/")`，使绝对 ir_file 误走 res:// 分支拼出
    `res://home/...`（worker 侧 FileAccess 不可读 → 全量 run_error，
    SEE-1468 M0a 缺陷 2 根因）。root=None 仅为旧调用形态兜底（绝对路径
    直传，不经 res://）。
    """
    if root is not None and root in ir_file.parents:
        ir_file_res = "res://" + ir_file.relative_to(root).as_posix()
    else:
        ir_file_res = str(ir_file)
    request = json.dumps({"mutant_id": mutant.mutant_id,
                          "ir_file": ir_file_res},
                         ensure_ascii=False)
    try:
        line = _exchange_verdict_line(proc, request, timeout_s)
    except (OSError, ValueError) as e:
        raise _WorkerDead(f"worker IO failed: {e}") from e
    if not line:
        raise _WorkerDead("worker exited before all mutants were judged")
    verdict = json.loads(line)
    rc = int(verdict.get("rc", 1))
    err = (_verdict_run_error(verdict)
           or _worker_kill_attribution_error(verdict, rc, mutant))
    return mutant, _verdict_of(err, rc), err or ""


def _verdict_run_error(verdict: dict) -> str | None:
    """worker verdict 的执行链失真检查：显式 run_error / 哨兵② / 版本。"""
    err = verdict.get("run_error")
    if err is not None:
        return str(err)
    if int(verdict.get("executed", 0)) == 0:
        return ("sentinel check failed — worker executed 0 cases; "
                "execution chain untrusted")
    if verdict.get("ops_version") != OPS_VERSION:
        return (f"worker ops_version mismatch: "
                f"{verdict.get('ops_version')!r} != {OPS_VERSION!r}")
    return None


def _worker_kill_attribution_error(verdict: dict, rc: int,
                                   mutant: Mutant) -> str | None:
    """DEF-2 worker 侧：failing 集合必须含该 mutant 的 case 测试名。"""
    if rc == 0:
        return None
    failing = [str(x) for x in verdict.get("failing", [])]
    if mutant.case_test_name in failing:
        return None
    return (f"kill attribution failed — worker failing set "
            f"{sorted(failing)} does not name the mutated case "
            f"test {mutant.case_test_name!r}; execution chain "
            f"untrusted")


def _shutdown_worker(proc) -> None:  # noqa: ANN001
    try:
        if proc.stdin and not getattr(proc.stdin, "closed", False):
            proc.stdin.close()
    except (OSError, AttributeError):
        pass
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
    except AttributeError:
        pass  # 测试假进程无 wait/kill 面
