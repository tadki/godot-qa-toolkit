"""accept run：headless 执行生成套件 + 三件哈希绑定重放校验 + 契约回译。

执行前重放校验（§SPEC-1426-07，失配 = run_error，无告警放行档）：
manifest 里的 ir_hash / generated_hash 必须与磁盘上 IR、生成 .gd 的实际
sha256 一致——手改任一生成物即拒绝执行（防手改作弊样本放行）。

执行形态同构 mutation：一次性 godot --headless 子进程跑 GUT（-gdir 指向
acceptance/generated），跑完即弃；输出经归因 manifest 回译为统一契约
{tool, ok, summary, failures}。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from . import OPS_VERSION
from .compile import GENERATED_DIR, IR_DIR, sha256_of
from ..mutation.gutrun import _looks_like_godot_launch_failure, _parse_failing_tests
from ..mutation.inject import godot_command
from ..mutation.godot_gate import GlobalGodotGate


class AcceptRunError(Exception):
    """重放校验失败 / godot 启动失败——run_error，不产出 verdict。"""


def _project_gd_path(project_root: Path, res: str) -> Path:
    return project_root / res.removeprefix("res://")


def replay_check(project_root: str | Path, feature_key: str,
                 *, source_check: bool = True) -> dict:
    """三件哈希绑定重放校验。失配 raise AcceptRunError。返回 manifest。

    §SPEC-1426-24 重锚防线：manifest 可被整体替换为攻击者自造的三件一致
    组合（重锚），故 run 侧额外把 manifest.source_path 指向的源 .feature
    现场重编译，核对产出 IR 与磁盘 IR 哈希一致——锚定回源文件。源文件
    缺失 = run_error（防删源保伪造产物）。
    """
    root = Path(project_root)
    man_path = root / GENERATED_DIR / f"{feature_key}.manifest.json"
    if not man_path.is_file():
        raise AcceptRunError(f"manifest missing: {man_path}")
    manifest = json.loads(man_path.read_text(encoding="utf-8"))
    if manifest.get("ops_version") != OPS_VERSION:
        raise AcceptRunError(
            f"manifest ops_version {manifest.get('ops_version')!r} != {OPS_VERSION!r}")
    ir_path = _project_gd_path(root, manifest["ir_path"])
    if not ir_path.is_file():
        raise AcceptRunError(f"IR file missing: {ir_path}")
    gd_path = root / GENERATED_DIR / f"test_{feature_key}.gd"
    if not gd_path.is_file():
        raise AcceptRunError(f"generated test missing: {gd_path}")
    ir_hash = sha256_of(ir_path.read_text(encoding="utf-8"))
    gd_hash = sha256_of(gd_path.read_text(encoding="utf-8"))
    if ir_hash != manifest["ir_hash"]:
        raise AcceptRunError(
            f"IR hash mismatch — acceptance/ir was modified after compile "
            f"(expected {manifest['ir_hash'][:12]}, got {ir_hash[:12]})")
    if gd_hash != manifest["generated_hash"]:
        raise AcceptRunError(
            f"generated test hash mismatch — acceptance/generated was modified "
            f"after compile (expected {manifest['generated_hash'][:12]}, "
            f"got {gd_hash[:12]})")
    if source_check:
        _reanchor_check(root, manifest, ir_hash)
    return manifest


def _reanchor_check(root: Path, manifest: dict, ir_hash: str) -> None:
    """重锚对抗：源 .feature 重编译哈希必须与磁盘 IR 一致。"""
    from .compile import build_ir
    src = Path(manifest.get("source_path", ""))
    if not src.is_file():
        raise AcceptRunError(
            f"source feature missing: {src} — manifest cannot be re-anchored "
            f"to its source; generated artifacts are untrusted")
    try:
        ir = build_ir(src.read_text(encoding="utf-8"), str(src))
    except Exception as e:  # noqa: BLE001 — 解析失败也是重锚失败
        raise AcceptRunError(
            f"source feature {src} no longer compiles ({e}) — "
            f"generated artifacts are untrusted") from e
    recompiled = sha256_of(json.dumps(ir, ensure_ascii=False, indent=2,
                                      sort_keys=True))
    if recompiled != ir_hash:
        raise AcceptRunError(
            f"source-feature recompile hash mismatch — acceptance/ir does not "
            f"correspond to {src} (manifest may have been re-anchored; "
            f"expected {ir_hash[:12]}, recomputed {recompiled[:12]})")


def run_suite(project_root: str | Path, feature_key: str,
              timeout_s: int = 120) -> dict:
    """重放校验 + headless GUT 执行 + 统一契约回译。"""
    root = Path(project_root)
    gut = root / "addons" / "gut" / "gut_cmdln.gd"
    if not gut.is_file():
        raise AcceptRunError(f"GUT runner not found: {gut}")
    manifest = replay_check(project_root, feature_key)
    env = dict(os.environ)
    try:
        with GlobalGodotGate(str(root)):
            r = subprocess.run(
                [godot_command(), "--headless", "--path", str(root),
                 "-s", "res://addons/gut/gut_cmdln.gd",
                 "-gdir=res://" + GENERATED_DIR, "-gexit"],
                capture_output=True, text=True, timeout=timeout_s,
                cwd=str(root), env=env)
    except subprocess.TimeoutExpired as e:
        raise AcceptRunError(
            f"GUT timed out after {timeout_s}s on generated suite") from e
    output = (r.stdout or "") + "\n" + (r.stderr or "")
    if _looks_like_godot_launch_failure(output):
        tail = "\n".join(output.splitlines()[-10:])
        raise AcceptRunError(f"godot launch failed on generated suite: {tail}")
    tests_executed = _count_executed_tests(output)
    # §SPEC-1426-24 tests_executed 守卫：0 测试执行（如 GUT 目录扫描静默
    # 忽略生成文件、被测项目 .gutconfig 覆盖 dirs）= 判定失败，不是绿。
    if r.returncode == 0 and tests_executed == 0:
        result: dict = {
            "tool": "accept", "ok": False,
            "summary": {"feature": manifest["feature"],
                        "feature_key": feature_key,
                        "ops_version": OPS_VERSION, "exit_code": 0,
                        "ir_hash": manifest["ir_hash"],
                        "replay_checked": True, "tests_executed": 0,
                        "wrote": []},
            "failures": [{"test": None, "scenario": None, "line": 0,
                          "reason": "tests_executed=0 — generated suite ran no "
                                    "tests (scan/config swallowed it); not a pass"}],
        }
        return result
    result: dict = {
        "tool": "accept",
        "ok": r.returncode == 0,
        "summary": {
            "feature": manifest["feature"],
            "feature_key": feature_key,
            "ops_version": OPS_VERSION,
            "exit_code": r.returncode,
            "ir_hash": manifest["ir_hash"],
            "replay_checked": True,
            "tests_executed": tests_executed,
            "wrote": [],
        },
        "failures": [],
    }
    if r.returncode != 0:
        result["failures"] = _attribute_failures(output, manifest, r.returncode)
    return result


# GUT 运行流证据：运行流 `* test_x` 行 / Run Summary 段 `- test_x` 行
# （与 mutation gutrun 的运行流证据同族 token；只为计数，不做 kill 判定）。
_EXECUTED_TEST_RE = re.compile(r"(?mi)^[*-]\s+test_\S+")


def _count_executed_tests(gut_output: str) -> int:
    """从 GUT 输出统计执行过的测试数（运行流 + Run Summary 去重计数）。"""
    seen: set[str] = set()
    for m in _EXECUTED_TEST_RE.finditer(gut_output):
        seen.add(m.group(0).split()[1])
    return len(seen)


def _attribute_failures(output: str, manifest: dict, returncode: int) -> list[dict]:
    """归因回译：失败测试名经 manifest 对回 scenario；无归因显式披露。

    §SPEC-1426-25：GUT 输出里 unchanged slot / harness 冒烟失败自带明细
    文本（fail_test 的 message），原样带进 reason——不再统一压缩成一句
    'generated acceptance test failed'。
    """
    failures: list[dict] = []
    failing = _parse_failing_tests(output)
    by_test = {a["test_name"]: a for a in manifest["attribution"]}
    detail_by_test = _failure_details(output)
    for t in sorted(failing):
        attr = by_test.get(t)
        detail = detail_by_test.get(t)
        if attr is not None:
            failures.append({
                "test": t, "scenario": attr["scenario"],
                "line": attr["scenario_line"],
                "reason": "generated acceptance test failed",
                **({"detail": detail} if detail else {}),
            })
        else:
            failures.append({
                "test": t, "scenario": None, "line": 0,
                "reason": "failing test not attributable via manifest",
                **({"detail": detail} if detail else {}),
            })
    if not failing:
        failures.append({
            "test": None, "scenario": None, "line": 0,
            "reason": f"GUT exited {returncode} with no parseable failing tests "
                      f"(tail: {' / '.join(output.splitlines()[-3:])})",
        })
    return failures


# fail_test 明细行形态：GUT 在 '* test_x' 之后以缩进块输出断言消息
# （'    [Failed]:  <message>'）。逐失败测试收集其 message 文本。
_FAILED_MSG_RE = re.compile(r"(?m)^\s*\[Failed\]:\s+(.+)$")


def _failure_details(gut_output: str) -> dict[str, str]:
    """失败测试名 → 首条 [Failed] 断言消息（归因回译的明细面）。"""
    details: dict[str, str] = {}
    current: str | None = None
    for raw in gut_output.splitlines():
        line = raw.replace("\r", "")
        m = re.match(r"^[*-]\s+(test_\S+)", line)
        if m:
            current = m.group(1)
            continue
        fm = _FAILED_MSG_RE.match(line)
        if fm and current and current not in details:
            details[current] = fm.group(1).strip()
    return details
