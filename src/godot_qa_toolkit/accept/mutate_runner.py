"""accept mutate runner（SEE-1426 P3，§SPEC-1426-12/13/14）。

闭环：对 IR 全量产 mutants → 逐 mutant 变异 IR → 同一套生成测试判定
kill/survived。v1 无 stamp：每轮全量、exit 非 0 即打回。

双通道（差分对照验收，§SPEC-1426-14）：
- batch（缺省）：逐 mutant 写临时变异 IR（磁盘 IR 每轮恢复）→ 一次性
  headless GUT 子进程，跑完即弃。
- --worker：真常驻通道——compile 产出的 worker 生成物
  （acceptance/generated/accept_worker.gd，受先清空再生成管辖）以
  stdin NDJSON 循环消费变异 IR（IR 走 stdin 带内传递，磁盘零改写），
  每 mutant 结束硬纪律：释放全部 testing_api 实例 + 重建（reload）+
  自检探针断言复位，不符即 run_error。

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
import shutil
import subprocess
from pathlib import Path

from . import OPS_VERSION
from .compile import GENERATED_DIR
from .mutate import Mutant, mutate_ir
from .run import AcceptRunError, replay_check
from ..mutation.gutrun import _looks_like_godot_launch_failure
from ..mutation.inject import godot_command, register_temp
from ..mutation.godot_gate import GlobalGodotGate

# 变异 IR 的防伪层③数据协议：mutated_unchanged 列表里的槽取"基线污染"
# 形态（生成测试与 worker 共用），比对块在位则必挂。
MUTANT_BASELINE_MARKER = "gqt-mutant-polluted"


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
    """全量契约变异闭环。返回统一 JSON 契约（ok=false 即打回）。"""
    root = Path(project_root)
    gut = root / "addons" / "gut" / "gut_cmdln.gd"
    if not gut.is_file():
        raise AcceptRunError(f"GUT runner not found: {gut}")
    manifest = replay_check(project_root, feature_key)
    ir_path = root / manifest["ir_path"].removeprefix("res://")
    ir = json.loads(ir_path.read_text(encoding="utf-8"))
    mutants = mutate_ir(ir)
    if not mutants:
        raise AcceptRunError(
            "no mutants generated — IR has no typed Then bindings and no "
            "unchanged slots; mutation gate cannot run (nothing to verify)")
    if use_worker:
        verdicts = _run_worker_channel(root, ir, mutants)
    else:
        verdicts = _run_batch_channel(root, ir_path, ir, mutants, timeout_s)
    return _aggregate_verdicts(manifest, mutants, verdicts, use_worker)


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
                        use_worker: bool) -> dict:
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
        },
        "failures": survived + run_errors,
    }


def _kill_attribution_error(mutant: Mutant, output: str) -> str | None:
    """DEF-2：rc≠0 时核对失败集合确含该 mutant 的 case 测试名。

    与 gqt mutation 的 kill=失败差集语义同构——恒挂毒测试（失败集合不
    含 case 测试）不能替 mutant 顶罪，归执行链失真 → run_error。
    """
    from ..mutation.gutrun import _parse_failing_tests
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


def _run_worker_channel(root: Path, ir: dict,
                        mutants: list[Mutant]) -> list[tuple[Mutant, str, str]]:
    """真常驻 worker：一次性 Popen，stdin NDJSON 逐 mutant 消费变异 IR。

    QA-1 传输协议：`OS.read_string_from_stdin()` 有 1024 字节行缓冲上限
    （Revy 实机二分实证：1024B OK / 1025B FAIL），真实 IR 带内传递必挂
    死——请求行只带 {mutant_id, ir_file} 小载荷，变异 IR 本体写
    `<root>/.qa-cache/gqt-accept-worker/` 下的轮转临时文件（contract §6
    白名单目录，注册进进程清理表，用后即删），worker 经 FileAccess 读
    （零行缓冲限制）。

    worker 侧硬纪律（实例释放 + 重建 + smoke 探针）由生成物内实现，
    探针失败以 run_error verdict 回传。
    """
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
    verdicts: list[tuple[Mutant, str, str]] = []
    try:
        for idx, mutant in enumerate(mutants):
            ir_file = scratch / f"mutant_{idx:04d}.json"
            try:
                ir_file.write_text(
                    json.dumps(_mutated_ir(ir, mutant), ensure_ascii=False),
                    encoding="utf-8")
                register_temp(ir_file)
                verdicts.append(_worker_round(proc, ir_file, mutant))
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


def _worker_round(proc, ir_file: Path,  # noqa: ANN001 — Popen/fake 双形态
                  mutant: Mutant) -> tuple[Mutant, str, str]:
    """单 mutant 的 worker 往返 + 判定（哨兵②/DEF-2 worker 侧同构核对）。

    QA-1：请求行 = {mutant_id, ir_file}（恒 <1024B，行缓冲安全）；verdict
    行本身仍是小 JSON，带内回传。
    """
    ir_file_res = "res://" + ir_file.relative_to(_worker_root_of(proc)).as_posix() \
        if _worker_root_of(proc) in ir_file.parents else str(ir_file)
    request = json.dumps({"mutant_id": mutant.mutant_id,
                          "ir_file": ir_file_res},
                         ensure_ascii=False)
    try:
        proc.stdin.write(request + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        # Godot 启动横幅/引擎告警与 worker verdict 同走 stdout——跳过一切
        # 非 "{" 开头的噪声行（verdict 恒为 JSON 对象），EOF 即通道死亡。
        while line and not line.lstrip().startswith("{"):
            line = proc.stdout.readline()
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


def _worker_root_of(proc) -> Path:  # noqa: ANN001 — Popen/fake 双形态
    """worker 的项目根（res:// 解析锚）：真进程取 cwd，fake 无 cwd 时
    退化为根目录（ir_file 直接走绝对路径形态，worker 侧 FileAccess 一样
    可读）。"""
    cwd = getattr(proc, "cwd", None) or getattr(proc, "_cwd", None)
    if cwd:
        return Path(cwd)
    return Path("/")


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
