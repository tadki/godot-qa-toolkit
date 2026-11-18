#!/usr/bin/env python3
"""SEE-1589 §SPEC-003 —— worker / warm / prewarm 提速面真实 KOL 基准。

协议（与既有性能协议同口径，估算不作结论）：
- 先按静态路由 census 把目标分为 worker-eligible 与 route-excluded(cold)；
- eligible 目标：A/A 冷通道 ≥3 轮定噪声带，再 cold/worker 交错 ≥3 对；
- cold 目标：A/A 冷通道 ≥3 轮，并输出具名不可达原因（route reason）；
- 每轮记录绝对墙钟、exec_scheme、worker_channel/farm/prewarm 披露、
  stage_timings 与 stage_reconciliation（farm/prewarm/import 归因）；
- per-mutant 分解 = per_mutant / executed；
- 逐轮结果原子落盘 `<out>.runs/`，重复执行跳过已完成轮次（可续跑）。

KOL 只读：`--kol-root` 必须是 /tmp 下的 `cp -a` 真副本；本 harness 只读
目标项目 + 通过 gqt CLI 在副本内运行（gqt 自身写 `.qa-cache` 与影子槽）。

用法：
    python tests/integration/run_see1589_benchmark.py \
        --kol-root /tmp/see1589-kol --targets utils systems --rounds 3 \
        --map utils=/tmp/map-utils.json --map systems=/tmp/map-systems.json \
        --out /tmp/see1589-benchmark.json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DEFAULT_GODOT = "/home/jerry/gqt-probe-bin/godot"


def _env(godot_bin: str | None, map_path: str | None,
         worker: bool | None = None) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["GQT_GODOT_BIN"] = godot_bin or env.get("GQT_GODOT_BIN") or DEFAULT_GODOT
    if map_path:
        env["GQT_ATTRIBUTION_MAP"] = map_path
    else:
        env.pop("GQT_ATTRIBUTION_MAP", None)
    # A 臂必须显式关断（resident 默认启用，不关断则 A/B 退化为 A/A）。
    env.pop("GQT_MUTATION_WORKER", None)
    if worker is False:
        env["GQT_MUTATION_WORKER"] = "0"
    return env


def _sites(kol: Path, rel: str) -> int:
    from godot_qa_toolkit.mutation.ops import _collect_mutations
    from godot_qa_toolkit.mutation.scopemanifest import build_manifest
    src = (kol / rel).read_text(encoding="utf-8")
    try:
        raw = _collect_mutations(src, rel)
        _h, _s, mutants, _sh = build_manifest(src, rel, raw)
        return len(mutants)
    except Exception:  # noqa: BLE001 — 清点失败按不可测目标披露
        return 0


def _map_line_coverage(maps: dict, face: str, rel: str) -> int | None:
    """预置 map 中该文件的行级映射数（None = 该面未提供 map）。"""
    path = maps.get(face)
    if not path:
        return None
    try:
        doc = json.loads(Path(path).read_text("utf-8"))
        lines = ((doc.get("arms") or {}).get("gut_full") or {}).get(
            "line_to_tests") or {}
        return len(lines.get(rel) or {})
    except (OSError, ValueError):
        return 0


def discover_targets(kol: Path, faces: list[str], per_class: int,
                     maps: dict | None = None) -> list[dict]:
    """按面枚举 .gd，静态路由分类，挑有变异位点的目标。"""
    from godot_qa_toolkit.mutation.channel_router import route_target, \
        summarize_reasons
    picked_worker: list[dict] = []
    picked_cold: list[dict] = []
    files: list[Path] = []
    for face in faces:
        p = kol / face
        if p.is_dir():
            files.extend(sorted(p.rglob("*.gd")))
        elif p.is_file() and p.suffix == ".gd":
            files.append(p)
    maps = maps or {}
    for path in files:
        rel = path.relative_to(kol).as_posix()
        face_key = rel.split("/", 1)[0]
        mapped = _map_line_coverage(maps, face_key, rel)
        if mapped == 0:
            continue  # 显式 map 无该文件行事实 → 必然零引用/不可选
        decision = route_target(str(kol), "res://" + rel)
        sites = _sites(kol, rel)
        if sites <= 0:
            continue
        row = {
            "target": rel,
            "channel": decision.channel,
            "closure_size": len(decision.closure),
            "route_reasons": list(decision.reasons),
            "reason_counts": summarize_reasons(decision.reasons),
            "sites": sites,
            "mapped_lines": mapped,
        }
        bucket = picked_worker if decision.channel == "worker" else picked_cold
        bucket.append(row)
    # 取变异位点最少的目标：小面让 A/A 与交错轮次都能在预算内跑完，
    # 且避免 641 位点级长跑（同面内选择对机制对比无偏）。
    picked_worker.sort(key=lambda r: (r["sites"], r["target"]))
    picked_cold.sort(key=lambda r: (r["sites"], r["target"]))
    return picked_worker[:per_class] + picked_cold[:per_class]


def run_one(kol: Path, target: str, budget: int, worker: bool,
            map_path: str | None, godot_bin: str | None,
            timeout_s: int = 900, jobs: int | None = None) -> dict:
    cmd = [sys.executable, "-m", "godot_qa_toolkit.cli", "mutation", target,
           "--project-root", str(kol), "--budget", str(budget),
           # 两臂都强制全量执行：默认 v2 差分会把历史 killed 位点复用掉，
           # A/B 会退化成「执行 vs 差分」而非「冷 vs worker」。
           "--mutate-all"]
    if jobs is not None:
        cmd += ["--jobs", str(jobs)]
    if worker:
        cmd.append("--worker-channel")
    t0 = time.monotonic()
    proc = subprocess.run(cmd, cwd=str(REPO),
                          env=_env(godot_bin, map_path, worker),
                          capture_output=True, text=True, timeout=timeout_s)
    wall_s = round(time.monotonic() - t0, 3)
    try:
        doc = json.loads(proc.stdout)
    except json.JSONDecodeError:
        doc = {"ok": False, "error": "non-json stdout",
               "raw": proc.stdout[-2000:]}
    summary = ((doc.get("results") or [{}])[0].get("summary")
               if doc.get("results") else doc.get("summary")) or {}
    return {
        "worker": worker,
        "rc": proc.returncode,
        "wall_s": wall_s,
        "ok": doc.get("ok"),
        "error": summary.get("error") or summary.get("selection_reason"),
        "mutants": summary.get("mutants"),
        "executed": summary.get("executed", summary.get("mutants")),
        "killed": summary.get("killed"),
        "survived": summary.get("survived"),
        "exec_scheme": summary.get("exec_scheme"),
        "slot_mode": summary.get("slot_mode"),
        "worker_channel": summary.get("worker_channel"),
        "farm": summary.get("farm"),
        "prewarm": ((doc.get("prewarm") if isinstance(doc.get("prewarm"), dict)
                     else summary.get("prewarm"))),
        "stage_timings": summary.get("stage_timings"),
        "stage_reconciliation": summary.get("stage_reconciliation")
        or doc.get("stage_reconciliation"),
        "stderr_tail": proc.stderr.strip().splitlines()[-5:],
    }


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 3) if values else None


def analyse(target: dict, rounds: list[dict]) -> dict:
    cold = [r for r in rounds if not r["worker"]]
    warm = [r for r in rounds if r["worker"]]
    cold_walls = [r["wall_s"] for r in cold if r.get("ok") is not None]
    out: dict = {
        "target": target["target"],
        "channel": target["channel"],
        "sites": target["sites"],
        "levels": len(cold_walls),
        "aa_cold_s": cold_walls,
        "aa_median_s": _median(cold_walls),
        "aa_noise_band": None,
    }
    if len(cold_walls) >= 2 and _median(cold_walls):
        med = _median(cold_walls)
        out["aa_noise_band"] = round(
            (max(cold_walls) - min(cold_walls)) / med, 4)
    if target["channel"] != "worker":
        out.update(worker_unreachable=True,
                   unreachable_kind="static_route_excluded",
                   unreachable_reason_counts=target["reason_counts"],
                   unreachable_reasons=target["route_reasons"],
                   override_refused=any(
                       (r.get("worker_channel") or {}).get("override_refused")
                       for r in rounds))
        return out
    ratios = []
    pairs = []
    for c, w in zip(cold[1:], warm):
        if c["wall_s"] and w["wall_s"]:
            ratio = round(w["wall_s"] / c["wall_s"], 4)
            ratios.append(ratio)
            pairs.append({"cold_s": c["wall_s"], "worker_s": w["wall_s"],
                          "ratio": ratio,
                          "cold_scheme": c.get("exec_scheme"),
                          "worker_scheme": w.get("exec_scheme")})
    out.update(
        worker_unreachable=False,
        interleaved_pairs=pairs,
        ratios=ratios,
        ratio_median=_median(ratios),
        worker_effective=bool(warm) and all(
            (r.get("worker_channel") or {}).get("decision") == "worker"
            and (r.get("mutants") or 0) > 0 for r in warm),
        degraded_rounds=[{
            "index": r.get("index"),
            "decision": (r.get("worker_channel") or {}).get("decision"),
            "reason": (r.get("worker_channel") or {}).get("reason"),
            "fatal_events": (r.get("worker_channel") or {}).get(
                "fatal_events"),
        } for r in warm
            if (r.get("worker_channel") or {}).get("decision")
            == "worker_degraded_cold"],
        per_mutant_s={
            "cold": _median([
                round((r["stage_timings"] or {}).get("per_mutant", 0.0)
                      / max(1, r.get("mutants") or 1), 4)
                for r in cold[-3:] if r.get("stage_timings")]),
            "worker": _median([
                round((r["stage_timings"] or {}).get("per_mutant", 0.0)
                      / max(1, r.get("mutants") or 1), 4)
                for r in warm if r.get("stage_timings")]),
        },
    )
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kol-root", required=True)
    ap.add_argument("--targets", nargs="+", required=True)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--budget", type=int, default=4)
    ap.add_argument("--jobs", type=int, default=None,
                    help="fixed --jobs for both arms (concurrency-controlled)")
    ap.add_argument("--per-class", type=int, default=1)
    ap.add_argument("--target", action="append", default=[],
                    help="explicit .gd path (relative to kol root); repeatable")
    ap.add_argument("--map", action="append", default=[],
                    help="face=path to a prebuilt test-attribution map")
    ap.add_argument("--godot-bin", default=None)
    ap.add_argument("--timeout-s", type=int, default=900,
                    help="单轮 mutation 调用上限；fresh 副本首轮含 map 全量构建"
                         "（懒基线+插桩），按需调大")
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-wall-seconds", type=float, default=None)
    args = ap.parse_args(argv)

    kol = Path(args.kol_root).resolve()
    if not (kol / "project.godot").is_file():
        print(json.dumps({"ok": False, "error": f"not a Godot project: {kol}"},
                         ensure_ascii=False, indent=2))
        return 2
    maps = {}
    for item in args.map:
        face, _, path = item.partition("=")
        maps[face] = path
    out_path = Path(args.out)
    runs_dir = out_path.with_name(out_path.name + ".runs")
    runs_dir.mkdir(parents=True, exist_ok=True)

    if args.target:
        from godot_qa_toolkit.mutation.channel_router import (
            route_target as _route, summarize_reasons as _sum)
        targets = []
        for rel in args.target:
            d = _route(str(kol), "res://" + rel)
            targets.append({
                "target": rel, "channel": d.channel,
                "closure_size": len(d.closure),
                "route_reasons": list(d.reasons),
                "reason_counts": _sum(d.reasons),
                "sites": _sites(kol, rel),
                "mapped_lines": _map_line_coverage(
                    maps, rel.split("/", 1)[0], rel),
            })
    else:
        targets = discover_targets(kol, args.targets, args.per_class, maps)
    started = time.monotonic()
    results = []
    for target in targets:
        rel = target["target"]
        face = rel.split("/", 1)[0]
        map_path = maps.get(face)
        records = []
        if target["channel"] == "worker":
            plan = [("aa", False) for _ in range(args.rounds)]
            plan += [("ab", False), ("ab", True)] * args.rounds
        else:
            plan = [("aa", False) for _ in range(args.rounds)]
        for idx, (phase, worker) in enumerate(plan):
            key = f"{rel.replace('/', '_')}__{phase}__{idx}"
            rec_path = runs_dir / (key + ".json")
            if rec_path.is_file():
                records.append(json.loads(rec_path.read_text("utf-8")))
                continue
            if (args.max_wall_seconds is not None
                    and time.monotonic() - started > args.max_wall_seconds):
                target["analysis"] = analyse(target, records)
                target["partial"] = True
                results.append(target)
                print(json.dumps({
                    "ok": True, "partial": True, "out": str(out_path),
                    "completed_targets": len(results)},
                    ensure_ascii=False, indent=2))
                return 0
            try:
                rec = run_one(kol, rel, args.budget, worker, map_path,
                              args.godot_bin, timeout_s=args.timeout_s,
                              jobs=args.jobs)
            except subprocess.TimeoutExpired:
                rec = {"phase": phase, "worker": worker,
                       "timeout": True,
                       "wall_s": float(args.timeout_s),
                       "rc": None, "ok": False,
                       "error": "round exceeded --timeout-s (named abort, "
                                "counted)"}
            rec.update(phase=phase, index=idx)
            tmp = rec_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1),
                           "utf-8")
            os.replace(tmp, rec_path)
            records.append(rec)
        target["analysis"] = analyse(target, records)
        target["rounds"] = records
        results.append(target)

    report = {
        "ok": True,
        "protocol": {"rounds": args.rounds, "budget": args.budget,
                     "jobs": args.jobs,
                     "per_class": args.per_class,
                     "kol_root": str(kol),
                     "godot_bin": args.godot_bin or DEFAULT_GODOT},
        "targets": results,
    }
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                        "utf-8")
    print(json.dumps({
        "ok": True,
        "out": str(out_path),
        "targets": [
            {"target": t["target"], "channel": t["channel"],
             "worker_effective": (t.get("analysis") or {}).get(
                 "worker_effective"),
             "ratio_median": (t.get("analysis") or {}).get("ratio_median"),
             "aa_median_s": (t.get("analysis") or {}).get("aa_median_s"),
             "unreachable": (t.get("analysis") or {}).get(
                 "worker_unreachable")}
            for t in results],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
