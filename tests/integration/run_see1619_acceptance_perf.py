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

from godot_qa_toolkit.measurement.acceptance_perf import (
    compute_perf_summary,
    DuplicateRoundError,
    merge_round_into_report,
)


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


def run_single_round(args, round_type: str, round_index: int,
                     project_root: Path, keys: int, recipe: Path,
                     report_path: Path) -> int:
    """分批前台模式：执行单轮并合并入既有报告（resume 语义），逐轮
    追加后重算汇总——测量口径与单次全量模式一致。

    §SPEC-028 出口契约（显式）：
    - 本轮 invalid → exit 1；
    - 报告已完整（rounds_complete）时以合并 summary 判定：MEASURED → 0，
      否则（如 speed_gate 失败）→ exit 1，不得 round valid 即 0；
    - 报告尚不完整 → exit 0（可 resume 追加），stderr 显式提示
      resume 语义与未配对的孤立 B 轮——最终判定由全量模式重读 verdict
      承担（那里同样以 MEASURED 为 exit 0 前提）。"""
    shards = args.shards_a if round_type == "A" else keys
    rec = _acceptance_wall(project_root, args.gqt, args.godot, recipe,
                           report_path.parent
                           / f"perf-{round_type}{round_index}.json",
                           shards, keys, max(args.timeout, 180) * 12,
                           args.roots)
    rec.update({"round": f"{round_type}{round_index}",
                "mode": ("shared" if round_type == "A"
                         else "per_feature"),
                "keys": keys,
                "fanout": rec.get("shards", 0) * rec.get("repeats", 0)})
    doc: dict = {"doc": "see1619-acceptance-perf/1", "rounds": []}
    if report_path.is_file():
        doc = json.loads(report_path.read_text(encoding="utf-8"))
    try:
        merge_round_into_report(doc, rec, args.shards_a, keys,
                                report_path=report_path)
    except DuplicateRoundError as e:
        # §SPEC-026：重复 batch round 具名拒绝，不裸 ValueError 崩溃。
        print(f"REFUSAL: {e}", file=sys.stderr)
        return 2
    s = doc["summary"]
    print(f"[{rec['round']}] valid={rec['valid']} "
          f"wall={rec.get('acceptance_wall_s')}s "
          f"fanout={rec['fanout']} "
          f"verdict={s['verdict']} "
          f"rounds={len(doc['rounds'])}", flush=True)
    if not rec["valid"]:
        print("INCONCLUSIVE round:", rec["round"], rec.get("run_error"),
              file=sys.stderr)
        return 1
    # §SPEC-028：round valid 不等于报告合格——完整报告必须服从合并
    # summary 的 verdict/speed_gate；不完整报告可 resume，但收尾语义
    # （孤立 B 待配对、最终以全量模式重读判定）必须显式提示。
    if s.get("rounds_complete"):
        if s.get("verdict") == "MEASURED":
            return 0
        print(f"INCONCLUSIVE report: verdict={s.get('verdict')} "
              f"speed_gate={s.get('speed_gate')} — complete but not "
              "MEASURED", file=sys.stderr)
        return 1
    # §SPEC-029：交错已开始但独立 A/A 基线不足 = 终态失败——基线块必须
    # 位于交错之前，末尾追加无法补足，不得按可 resume 处理。
    if s.get("interleaved_pairs") and s.get("baseline_samples", 0) < 3:
        print(f"INCONCLUSIVE report: interleaved pairs present but A/A "
              f"baseline samples={s.get('baseline_samples')} < 3 — the "
              "baseline block must precede interleaving and cannot be "
              "established by appending", file=sys.stderr)
        return 1
    note = ("report incomplete (rounds_complete=false) — append more "
            "rounds to resume; final verdict is read back by the full "
            "mode")
    if s.get("orphan_b_rounds"):
        note += (f"; unpaired B rounds pending next shared A: "
                 f"{s['orphan_b_rounds']}")
    print(note, file=sys.stderr)
    return 0


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
    ap.add_argument("--mode", choices=["A", "B"], default=None,
                    help="C3-R2 分批前台模式：单轮执行并合并入既有报告")
    ap.add_argument("--round-index", type=int, default=None,
                    help="分批模式的轮序号（与 --mode 成对）")
    args = ap.parse_args(argv)

    if args.mode and args.round_index is None:
        print("REFUSAL: --mode 需与 --round-index 成对", file=sys.stderr)
        return 2
    if not args.mode and (args.rounds < 3 or args.aa < 3):
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
    recipe = (Path(args.recipe).resolve() if args.recipe else
              _ensure_recipe(project_root, args.gqt,
                             project_root / "recipe.json",
                             max(args.timeout, 600)))

    if args.mode:
        # C3-R2 分批前台模式：单轮 ≤ 单次执行上限，逐轮合并入同一报告
        # （resume 语义，汇总口径与单次全量一致）。
        return run_single_round(args, args.mode, args.round_index,
                                project_root, len(keys), recipe,
                                report_path)

    rounds: list[dict] = []
    for i in range(1, args.aa + 1):
        rec = _acceptance_wall(project_root, args.gqt, args.godot, recipe,
                               report_path.parent
                               / f"perf-a{i}.json", args.shards_a,
                               len(keys), max(args.timeout, 180) * 12,
                               args.roots)
        rec.update({"round": f"A{i}", "mode": "shared",
                    "keys": len(keys),
                    "fanout": rec.get("shards", 0) * rec.get("repeats", 0)})
        rounds.append(rec)
        print(f"[A{i}] valid={rec['valid']} "
              f"wall={rec.get('acceptance_wall_s')}s "
              f"fanout={rec['fanout']}", flush=True)
    for i in range(1, args.rounds + 1):
        rec_b = _acceptance_wall(project_root, args.gqt, args.godot,
                                 recipe, report_path.parent
                                 / f"perf-b{i}.json",
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
                                 recipe, report_path.parent
                                 / f"perf-a{i+100}.json",
                                 args.shards_a, len(keys),
                                 max(args.timeout, 180) * 12, args.roots)
        rec_a.update({"round": f"A{i+100}", "mode": "shared",
                      "keys": len(keys),
                      "fanout": rec_a.get("shards", 0)
                      * rec_a.get("repeats", 0)})
        rounds.append(rec_a)
        print(f"[A{i+100}] valid={rec_a['valid']} "
              f"wall={rec_a.get('acceptance_wall_s')}s", flush=True)

    summary = compute_perf_summary(rounds, args.shards_a, len(keys))
    report = {"doc": "see1619-acceptance-perf/1", "rounds": rounds,
              "summary": summary}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    print(f"perf: verdict={summary['verdict']} "
          f"noise_band={summary['aa_noise_band'].get('spread_pct')}% "
          f"median_B/A={summary['median_ratio_b_over_a']} "
          f"fanout_shared={summary['fanout_shared']} "
          f"fanout_per_feature={summary['fanout_per_feature']}")
    for r in rounds:
        if not r.get("valid"):
            print("INCONCLUSIVE round:", r.get("round"),
                  r.get("run_error"), file=sys.stderr)
    return 0 if summary["verdict"] == "MEASURED" else 1


if __name__ == "__main__":
    sys.exit(main())
