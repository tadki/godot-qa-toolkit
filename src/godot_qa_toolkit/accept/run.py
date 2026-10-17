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


def replay_check(project_root: str | Path, feature_key: str) -> dict:
    """三件哈希绑定重放校验。失配 raise AcceptRunError。返回 manifest。"""
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
    return manifest


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
            "wrote": [],
        },
        "failures": [],
    }
    if r.returncode != 0:
        result["failures"] = _attribute_failures(output, manifest, r.returncode)
    return result


def _attribute_failures(output: str, manifest: dict, returncode: int) -> list[dict]:
    """归因回译：失败测试名经 manifest 对回 scenario；无归因显式披露。"""
    failures: list[dict] = []
    failing = _parse_failing_tests(output)
    by_test = {a["test_name"]: a for a in manifest["attribution"]}
    for t in sorted(failing):
        attr = by_test.get(t)
        if attr is not None:
            failures.append({
                "test": t, "scenario": attr["scenario"],
                "line": attr["scenario_line"],
                "reason": "generated acceptance test failed",
            })
        else:
            failures.append({
                "test": t, "scenario": None, "line": 0,
                "reason": "failing test not attributable via manifest",
            })
    if not failing:
        failures.append({
            "test": None, "scenario": None, "line": 0,
            "reason": f"GUT exited {returncode} with no parseable failing tests "
                      f"(tail: {' / '.join(output.splitlines()[-3:])})",
        })
    return failures
