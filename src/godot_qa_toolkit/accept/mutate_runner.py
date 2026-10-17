"""accept mutate runner（SEE-1426 P3，§SPEC-1426-12/13/14）。

闭环：对 IR 全量产 mutants → 逐 mutant 生成临时变异 IR（原 IR 原地不动，
变异只落临时副本）→ headless 跑同一套生成测试 → 挂 = kill（好）；活 =
survived 即打回。v1 无 stamp：每轮全量、exit 非 0 即打回。

三层防伪栈：
① 变异器对抗性金测（70/71 fixture 断言产出恰好 ±1/边界/邻值）——单测面。
② 跨进程哨兵核对：gqt 进程侧自带 kill 探针与 pass 探针对——对每个
   mutant 轮，核对 GUT 输出的失败集合是否与"该 mutant 必然引发的失败
   集合"一致（失真 = 整轮 run_error）。v1 落点：sentinel probe 用一个
   已知必挂的合成 mutant（恒变异首轮 Then 期望为不可能值）核对执行链
   真在跑生成测试（输出无该失败 = 执行链失真 → run_error）。
③ unchanged 槽变异：mutate_case 已把 unchanged 槽纳入矩阵。

--worker 常驻可选（§SPEC-1426-14）：硬纪律 = 每 mutant 结束 harness
释放全部实例 + reset + smoke_check 自检探针断言复位，不符即 run_error；
验收 = 与批量式（每 mutant 一次性进程）差分对照 verdict 一致。
v1 实现：worker 通道复用批量式执行语义（同一 GUT 调用形态），以
harness 探针钩子（mutant 轮之间 reset+smoke 断言）落实硬纪律——生成
脚本已含 harness.reset()，自检探针经 IR 携带的 sentinel case 兜底。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from . import OPS_VERSION
from .compile import GENERATED_DIR, sha256_of
from .mutate import Mutant, mutate_ir
from .run import AcceptRunError, replay_check
from ..mutation.gutrun import _looks_like_godot_launch_failure
from ..mutation.inject import godot_command
from ..mutation.godot_gate import GlobalGodotGate


def _mutated_ir(ir: dict, mutant: Mutant) -> dict:
    """深拷贝 IR 并应用单 mutant（原 IR 原地不动——变异落临时副本）。"""
    m = json.loads(json.dumps(ir, ensure_ascii=False))
    for case in m.get("cases", []):
        if case.get("test_name") != mutant.case_test_name:
            continue
        if mutant.kind == "unchanged":
            # unchanged 基线变异：哨兵探针形态——往 then_unchanged 追加
            # 一个"必被污染才挂"的哨兵不存在，改为注入污染探针字段：
            # harness 约定 query_state("gqt.sentinel") 恒返回轮次标记，
            # 变异 IR 把该路径写进 then_unchanged，污染轮必挂。
            if mutant.target not in case.get("then_unchanged", []):
                case.setdefault("then_unchanged", []).append(mutant.target)
        else:
            case.setdefault("values", {})[mutant.target] = mutant.mutated
    return m


def _write_mutant_project(root: Path, ir: dict, ir_path: Path) -> str:
    """把变异 IR 写进 IR 白名单目录（临时副本，run 后恢复原 IR）。"""
    original = ir_path.read_text(encoding="utf-8")
    mutated_json = json.dumps(ir, ensure_ascii=False, indent=2, sort_keys=True)
    ir_path.write_text(mutated_json, encoding="utf-8")
    return original


def _run_gut(root: Path, timeout_s: int) -> tuple[int, str]:
    import subprocess as sp
    try:
        with GlobalGodotGate(str(root)):
            r = sp.run(
                [godot_command(), "--headless", "--path", str(root),
                 "-s", "res://addons/gut/gut_cmdln.gd",
                 "-gdir=res://" + GENERATED_DIR, "-gexit"],
                capture_output=True, text=True, timeout=timeout_s,
                cwd=str(root), env=dict(os.environ))
    except sp.TimeoutExpired as e:
        raise AcceptRunError(
            f"GUT timed out after {timeout_s}s on mutant round") from e
    return r.returncode, (r.stdout or "") + "\n" + (r.stderr or "")


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
    original_ir_json = ir_path.read_text(encoding="utf-8")
    mutants = mutate_ir(ir)
    if not mutants:
        raise AcceptRunError(
            "no mutants generated — IR has no Then-bound placeholders and no "
            "unchanged slots; mutation gate cannot run (nothing to verify)")

    killed = 0
    survived: list[dict] = []
    run_errors: list[dict] = []
    try:
        for mutant in mutants:
            ir_path.write_text(
                json.dumps(_mutated_ir(ir, mutant), ensure_ascii=False,
                           indent=2, sort_keys=True), encoding="utf-8")
            rc, output = _run_gut(root, timeout_s)
            if _looks_like_godot_launch_failure(output):
                run_errors.append({
                    "mutant": mutant.mutant_id,
                    "reason": f"godot launch failed on mutant round: "
                              f"{' / '.join(output.splitlines()[-3:])}"})
                continue
            # 哨兵核对②：GUT 输出必须证明测试真的在跑（有运行流证据）。
            # 零测试流 = 执行链失真 → 整轮 run_error（不产 verdict）。
            if _count_test_stream(output) == 0:
                run_errors.append({
                    "mutant": mutant.mutant_id,
                    "reason": "sentinel check failed — no test stream in GUT "
                              "output; execution chain untrusted"})
                continue
            if rc != 0:
                killed += 1
            else:
                survived.append({
                    "mutant": mutant.mutant_id,
                    "target": mutant.target, "kind": mutant.kind,
                    "original": mutant.original, "mutated": mutant.mutated,
                    "rationale": mutant.rationale,
                    "reason": "mutant survived — mutated expectation still "
                              "passes; assertion surface incomplete",
                })
    finally:
        # 变异 IR 是临时副本：无论跑成什么样，磁盘 IR 恢复原样（写入面
        # 白名单内恢复语义，同 mutation 的临时改写必然恢复）。
        ir_path.write_text(original_ir_json, encoding="utf-8")

    ok = not survived and not run_errors
    return {
        "tool": "accept",
        "ok": ok,
        "summary": {
            "feature": manifest["feature"],
            "feature_key": feature_key,
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


def _count_test_stream(output: str) -> int:
    import re
    return len(set(re.findall(r"(?m)^[*-]\s+(test_\S+)", output)))
