#!/usr/bin/env python3
"""SEE-1619 §SPEC-010 — acceptance 共享分片 vs per-feature 进程性能实测。

A/A ≥3 定噪声带（同配共享分片重复实跑）→ 交错 A/B ≥3（B=per-feature
进程形态，经 acceptance_shards=keys 退化）对比。每轮为一次真实
`gqt measure run`（acceptance 臂 valid 才计入），取该臂 wall_s 绝对
秒数——禁止以进程数推算墙钟。报告含绝对秒数、倍率、噪声带、
进程/分片账与逐轮原始记录；任一轮臂 invalid → INCONCLUSIVE（exit 1）。

  PYTHONPATH=src python3 -I tests/integration/run_see1619_acceptance_perf.py \
    --project-root /tmp/<kol-copy> --godot "$GODOT" \
    --aa 3 --rounds 3 --report <work>/acceptance-perf.json
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


def _run(cmd: list[str], timeout: int) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def _ensure_recipe(project_root: Path, gqt: str, out: Path,
                   timeout: int) -> Path:
    if out.is_file():
        return out
    rc, _out, err = _run([gqt, "measure", "suite", "--project-root",
                          str(project_root), "--out", str(out)],
                         timeout=timeout)
    if rc != 0:
        raise SystemExit(f"measure suite failed rc={rc}: {err[-400:]}")
    return out


def _acceptance_wall(project_root: Path, gqt: str, godot: str,
                     recipe: Path, report: Path, shards: int,
                     keys: int, timeout: int,
                     roots_arg: str | None) -> dict:
    """一次真实 measure run；返回 acceptance 臂逐轮原始记录。"""
    t0 = time.monotonic()
    roots = ([str(Path(r).resolve()) for r in roots_arg.split(",")]
             if roots_arg else [str(project_root)])
    rc, out, err = _run([gqt, "measure", "run", "--recipe", str(recipe),
                         "--out", str(report), "--project-root",
                         str(project_root), "--godot-bin", godot,
                         "--acceptance-shards", str(shards),
                         *roots], timeout=timeout)
    wall_total = round(time.monotonic() - t0, 1)
    doc: dict = {}
    if report.is_file():
        doc = json.loads(report.read_text(encoding="utf-8"))
    arm = next((a for a in doc.get("arms", [])
                if a.get("arm") == "acceptance"), None)
    if arm is None:
        return {"valid": False, "rc": rc, "wall_total_s": wall_total,
                "run_error": f"acceptance arm missing: {err[-200:]}"}
    rec = {"valid": arm.get("state") == "valid",
           "gut_ok": arm.get("gut_ok"),
           "rc": rc, "wall_total_s": wall_total,
           "acceptance_wall_s": arm.get("wall_s"),
           "shards": arm.get("shards"), "repeats": arm.get("repeats"),
           "process_fallbacks": arm.get("process_fallbacks"),
           "run_error": arm.get("invalid_reason")}
    return rec


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project-root", required=True)
    ap.add_argument("--godot", required=True)
    ap.add_argument("--gqt", default="gqt")
    ap.add_argument("--recipe", default=None,
                    help="缺省时先跑 gqt measure suite 自动构建")
    ap.add_argument("--report", required=True)
    ap.add_argument("--aa", type=int, default=3,
                    help="A/A 共享分片重复轮数（定噪声带）")
    ap.add_argument("--rounds", type=int, default=3,
                    help="交错 A/B 对比轮数（≥3）")
    ap.add_argument("--shards-a", type=int, default=4,
                    help="A 形态：共享分片组数")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--roots", default=None,
                    help="gut 臂目标面（目录或 .gd；缺省=project_root）")
    args = ap.parse_args(argv)

    if args.rounds < 3 or args.aa < 3:
        print("REFUSAL: --aa 与 --rounds 均 ≥3（噪声带/对比样本下限）",
              file=sys.stderr)
        return 2
    project_root = Path(args.project_root).resolve()
    feats = project_root / "acceptance" / "features"
    keys = sorted(p.stem for p in feats.glob("*.feature"))
    if not keys:
        print("REFUSAL: no acceptance features discovered", file=sys.stderr)
        return 2
    report_path = Path(args.report).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    work = report_path.parent
    recipe = (Path(args.recipe).resolve() if args.recipe else
              _ensure_recipe(project_root, args.gqt,
                             project_root / "recipe.json",
                             max(args.timeout, 600)))

    rounds: list[dict] = []
    # A/A：共享分片同配重复（定噪声带）
    for i in range(1, args.aa + 1):
        rec = _acceptance_wall(project_root, args.gqt, args.godot, recipe,
                               work / f"perf-a{i}.json", args.shards_a,
                               len(keys), max(args.timeout, 180) * 12,
                               args.roots)
        rec.update({"round": f"A{i}", "mode": "shared",
                    "keys": len(keys),
                    "fanout": rec.get("shards", 0) * rec.get("repeats", 0)})
        rounds.append(rec)
        print(f"[A{i}] valid={rec['valid']} "
              f"wall={rec.get('acceptance_wall_s')}s "
              f"fanout={rec['fanout']}", flush=True)
    # 交错 A/B：B=per-feature（shards=keys 退化为单 key 组）
    pairs: list[dict] = []
    for i in range(1, args.rounds + 1):
        rec_b = _acceptance_wall(project_root, args.gqt, args.godot,
                                 recipe, work / f"perf-b{i}.json",
                                 len(keys), len(keys),
                                 max(args.timeout, 180) * 12, args.roots)
        rec_b.update({"round": f"B{i}", "mode": "per_feature",
                      "keys": len(keys),
                      "fanout": rec_b.get("shards", 0)
                      * rec_b.get("repeats", 0)})
        rounds.append(rec_b)
        print(f"[B{i}] valid={rec_b['valid']} "
              f"wall={rec_b.get('acceptance_wall_s')}s "
              f"fanout={rec_b['fanout']}", flush=True)
        rec_a = _acceptance_wall(project_root, args.gqt, args.godot,
                                 recipe, work / f"perf-a{i+100}.json",
                                 args.shards_a, len(keys),
                                 max(args.timeout, 180) * 12, args.roots)
        rec_a.update({"round": f"A{i+100}", "mode": "shared",
                      "keys": len(keys),
                      "fanout": rec_a.get("shards", 0)
                      * rec_a.get("repeats", 0)})
        rounds.append(rec_a)
        wall_a = rec_a.get("acceptance_wall_s")
        wall_b = rec_b.get("acceptance_wall_s")
        ratio = (round(wall_b / wall_a, 3)
                 if wall_a and wall_b else None)
        pairs.append({"pair": i, "b_wall_s": wall_b, "a_wall_s": wall_a,
                      "ratio_b_over_a": ratio,
                      "b_valid": rec_b["valid"], "a_valid": rec_a["valid"]})
        print(f"[pair {i}] B/A={ratio}", flush=True)

    a_walls = [r.get("acceptance_wall_s") for r in rounds
               if r["mode"] == "shared" and r.get("acceptance_wall_s")]
    noise = {}
    if len(a_walls) >= 3:
        med = statistics.median(a_walls)
        spread = max(a_walls) - min(a_walls)
        noise = {"samples": a_walls, "median_s": round(med, 3),
                 "min_s": min(a_walls), "max_s": max(a_walls),
                 "spread_s": round(spread, 3),
                 "spread_pct": round(spread / med * 100, 2)
                 if med else None}
    ratios = [p["ratio_b_over_a"] for p in pairs
              if p["ratio_b_over_a"] is not None]
    all_valid = all(r["valid"] for r in rounds)
    summary = {
        "keys": len(keys),
        "shards_a": args.shards_a,
        "reps": rounds[0].get("repeats"),
        "fanout_shared": args.shards_a * (rounds[0].get("repeats") or 0),
        "fanout_per_feature": len(keys) * (rounds[0].get("repeats") or 0),
        "aa_noise_band": noise,
        "ab_pairs": pairs,
        "median_ratio_b_over_a": statistics.median(ratios)
        if len(ratios) >= 3 else None,
        "all_rounds_valid": all_valid,
        "verdict": ("MEASURED" if all_valid and len(ratios) >= 3
                    else "INCONCLUSIVE"),
    }
    report = {"doc": "see1619-acceptance-perf/1", "rounds": rounds,
              "summary": summary}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    print(f"perf: verdict={summary['verdict']} "
          f"noise_band={noise.get('spread_pct')}% "
          f"median_B/A={summary['median_ratio_b_over_a']} "
          f"fanout_shared={summary['fanout_shared']} "
          f"fanout_per_feature={summary['fanout_per_feature']}")
    for r in rounds:
        if not r["valid"]:
            print("INCONCLUSIVE round:", r.get("round"),
                  r.get("run_error"), file=sys.stderr)
    return 0 if summary["verdict"] == "MEASURED" else 1


if __name__ == "__main__":
    sys.exit(main())
