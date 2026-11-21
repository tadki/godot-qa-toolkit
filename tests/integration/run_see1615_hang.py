#!/usr/bin/env python3
"""SEE-1615 §SPEC-HANG —— KOL 真实样本直驱腿四形态完成矩阵 + 无效面 fail-fast。

协议：
- KOL /tmp 忠实副本（须已 `--import`；脚本自检 .godot 三产物，缺失即拒跑）。
- 有效选择面（既有 KOL 测试脚本手工物化）：cold/worker × jobs=1/并行 四形态
  逐一完成（bounded 外层 timeout），逐命令/输出/exit 留档；与 legacy
  （kill-bail 关断，gut_cmdln 腿）同变异 stable-id→verdict 不翻转。
- 无效选择面（含不存在脚本）：必须秒级具名 fail-fast（direct_scripts_
  unloadable），不得挂起——本脚本以 wall < --invalid-max-wall 断言。

用法（仓库根）：
    PYTHONPATH=src python3 -I tests/integration/run_see1615_hang.py \
        --kol-root /tmp/see1615-kol --report hang-report.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
DEFAULT_GODOT = "/home/jerry/gqt-probe-bin/godot"
_IMPORT_ARTIFACTS = ("global_script_class_cache.cfg", "uid_cache.bin", "imported")

VALID_TESTS = [
    "res://tests/unit/container/test_container_utils.gd",
    "res://tests/unit/effect/test_effect_manager.gd",
]
# 无效面：含两份不存在的测试脚本（wrapper 生成但 extends 目标缺失——
# KOL 实测挂起形态；修复后必须具名 fail-fast）
INVALID_TESTS = VALID_TESTS + [
    "res://tests/unit/economy/test_lcps_calculator.gd",
    "res://tests/unit/hardware/test_hardware_manager.gd",
]


def _import_ready(root: Path) -> tuple[bool, list[str]]:
    godot_dir = root / ".godot"
    missing = [n for n in _IMPORT_ARTIFACTS if not (godot_dir / n).exists()]
    return (not missing), missing


def _env(worker: bool, kill_bail: bool, godot: str) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["GQT_GODOT_BIN"] = godot
    env["GQT_ATTRIBUTION_MAP_MODE"] = "off"
    if worker:
        env.pop("GQT_MUTATION_WORKER", None)
    else:
        env["GQT_MUTATION_WORKER"] = "0"
    if not kill_bail:
        env["GQT_MUTATION_KILL_BAIL"] = "0"
    else:
        env.pop("GQT_MUTATION_KILL_BAIL", None)
    return env


def _verdict_map(root: Path, rel: str) -> dict[str, str]:
    from godot_qa_toolkit.mutation.resume2 import RESUME_V2_DIRNAME
    out: dict[str, str] = {}
    for manifest in (root / ".qa-cache" / RESUME_V2_DIRNAME).glob(
            "*/manifest.json"):
        try:
            doc = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if doc.get("rel") != rel:
            continue
        for entry in manifest.parent.glob("*.json"):
            if entry.name == "manifest.json":
                continue
            try:
                rec = json.loads(entry.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict) and rec.get("id"):
                out[str(rec["id"])] = str(rec.get("verdict") or "")
    return out


def _run(root: Path, target: str, tests: str, worker: bool, jobs: int,
         kill_bail: bool, budget: int, godot: str,
         timeout_s: int) -> dict:
    cmd = [sys.executable, "-m", "godot_qa_toolkit.cli", "mutation",
           target, "--project-root", str(root), "--tests", tests,
           "--budget", str(budget), "--jobs", str(jobs), "--mutate-all",
           "--timeout", "90"]
    env = _env(worker, kill_bail, godot)
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=str(root), env=env, capture_output=True,
                           text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {"cmd": cmd, "rc": None, "wall_s": round(
            time.monotonic() - t0, 2), "timed_out": True,
            "worker": worker, "jobs": jobs, "kill_bail": kill_bail}
    wall = round(time.monotonic() - t0, 2)
    try:
        out = json.loads(p.stdout)
    except ValueError:
        return {"cmd": cmd, "rc": p.returncode, "wall_s": wall,
                "non_json": True, "stderr_tail": p.stderr[-300:],
                "worker": worker, "jobs": jobs, "kill_bail": kill_bail}
    s = out.get("summary") or {}
    return {
        "cmd": cmd, "rc": p.returncode, "wall_s": wall,
        "worker": worker, "jobs": jobs, "kill_bail": kill_bail,
        "ok": bool(out.get("ok")),
        "verdicts": {k: int(s.get(k) or 0) for k in
                     ("killed", "survived", "run_errors", "timeout")},
        "error": str(s.get("error") or ""),
        "reasons": [str(f.get("reason") or "")
                    for f in (out.get("failures") or [])[:5]],
        "verdict_map": _verdict_map(root, "utils/container_utils.gd"),
    }


def _completed(run: dict) -> bool:
    if run.get("timed_out") or run.get("non_json"):
        return False
    if run.get("rc") not in (0, 1):
        return False
    v = run.get("verdicts") or {}
    return (v.get("run_errors", 1) == 0 and v.get("timeout", 1) == 0
            and (v.get("killed", 0) + v.get("survived", 0)) > 0)


def _flips(a: dict[str, str], b: dict[str, str]) -> list[str]:
    ids = set(a) | set(b)
    return sorted(i for i in ids if a.get(i) != b.get(i))


def _isolate_store_for_legacy(root: Path) -> tuple[Path | None, bool]:
    """legacy 腿 store 隔离：四形态共享 store 会累积前序 verdict，legacy
    直读得到的是累积 superset 而非自身判定，逐 id 翻转被掩盖（QA LOW F2）。
    先把 resume store 快照移走（同 fs rename），legacy 落全新 store——
    其 verdict_map 只含本腿判定；跑后丢弃 legacy store、还原快照。
    返回 (快照路径, 是否发生隔离)。"""
    from godot_qa_toolkit.mutation.resume2 import RESUME_V2_DIRNAME
    store = root / ".qa-cache" / RESUME_V2_DIRNAME
    if not store.is_dir():
        return None, False
    snapshot = root / ".qa-cache" / f"{RESUME_V2_DIRNAME}.legacy-snapshot"
    if snapshot.exists():
        shutil.rmtree(snapshot)
    store.rename(snapshot)
    return snapshot, True


def _restore_store(root: Path, snapshot: Path | None) -> None:
    from godot_qa_toolkit.mutation.resume2 import RESUME_V2_DIRNAME
    if snapshot is None:
        return
    store = root / ".qa-cache" / RESUME_V2_DIRNAME
    if store.exists():
        shutil.rmtree(store)
    snapshot.rename(store)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kol-root", required=True)
    ap.add_argument("--target", default="utils/container_utils.gd")
    ap.add_argument("--budget", type=int, default=6)
    ap.add_argument("--godot-bin", default=DEFAULT_GODOT)
    ap.add_argument("--report", default="hang-report.json")
    ap.add_argument("--timeout-s", type=int, default=240)
    ap.add_argument("--invalid-max-wall", type=float, default=60.0)
    args = ap.parse_args(argv)
    root = Path(args.kol_root).resolve()
    ready, missing = _import_ready(root)
    if not ready:
        print(json.dumps({"tool": "see1615-hang", "ok": False,
                          "error": "kol copy not imported",
                          "missing_artifacts": missing,
                          "fix": f"godot --headless --path {root} --import"},
                         ensure_ascii=False, indent=2))
        return 2
    from godot_qa_toolkit.mutation.truth_select import materialize_selection
    valid_sel = materialize_selection(str(root), VALID_TESTS)
    invalid_sel = materialize_selection(str(root), INVALID_TESTS)
    forms = [
        ("cold_jobs1", False, 1),
        ("cold_jobs2", False, 2),
        ("worker_jobs1", True, 1),
        ("worker_jobs2", True, 2),
    ]
    results = []
    for name, worker, jobs in forms:
        r = _run(root, args.target, valid_sel, worker, jobs, True,
                 args.budget, args.godot_bin, args.timeout_s)
        r["form"] = name
        r["completed"] = _completed(r)
        results.append(r)
    legacy = None
    store_snapshot, store_isolated = _isolate_store_for_legacy(root)
    try:
        legacy = _run(root, args.target, valid_sel, False, 1, False,
                      args.budget, args.godot_bin, args.timeout_s)
    finally:
        _restore_store(root, store_snapshot)
    legacy["form"] = "legacy_cold_jobs1"
    legacy["completed"] = _completed(legacy)
    cold_map = next((r.get("verdict_map") or {} for r in results
                     if r["form"] == "cold_jobs1"), {})
    legacy_flips = _flips(cold_map, legacy.get("verdict_map") or {})
    invalid = _run(root, args.target, invalid_sel, False, 1, True,
                   args.budget, args.godot_bin, args.timeout_s)
    invalid["form"] = "invalid_face_cold_jobs1"
    named = any("direct_scripts_unloadable" in x
                for x in invalid.get("reasons") or [])
    fast = (not invalid.get("timed_out")) and \
        float(invalid.get("wall_s") or 1e9) <= args.invalid_max_wall
    doc = {
        "tool": "see1615-hang",
        "kol_root": str(root), "target": args.target,
        "valid_selection": valid_sel, "invalid_selection": invalid_sel,
        "budget": args.budget,
        "forms": results,
        "legacy": legacy,
        "legacy_store_isolated": store_isolated,
        "legacy_verdict_flips": legacy_flips,
        "invalid_face": invalid,
        "criteria": {
            "all_forms_completed": all(r["completed"] for r in results),
            "legacy_no_verdict_flip": legacy_flips == [],
            "invalid_face_named_fast_fail": bool(named and fast),
        },
        "ok": (all(r["completed"] for r in results)
               and legacy_flips == [] and named and fast),
        "reproduce": " ".join([sys.executable,
                               str(Path(__file__).resolve())]
                              + list(argv if argv is not None
                                     else sys.argv[1:])),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    Path(args.report).write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(json.dumps({"ok": doc["ok"], "criteria": doc["criteria"],
                      "forms": [{k: f.get(k) for k in
                                 ("form", "completed", "rc", "wall_s",
                                  "verdicts")} for f in results],
                      "legacy": {k: legacy.get(k) for k in
                                 ("rc", "wall_s", "verdicts")},
                      "invalid_face": {k: invalid.get(k) for k in
                                       ("rc", "wall_s", "reasons")}},
                     ensure_ascii=False, indent=2))
    return 0 if doc["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
