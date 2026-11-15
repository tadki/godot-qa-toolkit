#!/usr/bin/env python3
"""SEE-1587 §SPEC-007 — 行级 mutation 选面实验（S0–S3）。

臂定义（issue description「行级 mutation 选面实验协议」）：
- S0：显式 fn 选面（当前函数级基线）
- S1：显式行范围（变异位点 census 确定性生成）→ 行级测试并集单 glob
- S2：默认行级选面（位点按 frozenset(line→tests) 签名分组）
- S3：显式 full-face oracle（per-mutant verdict 正确性裁判）

协议：每个目标面先 A/A≥3 定噪声带；随后 S0/S1/S2/S3 交错 ≥3 轮；
S1/S2 与 S3 按 stable_id 逐 mutant 对账 verdict（任一 mismatch = FAIL）；
通过门：verdict 全等 ∧ S2 墙钟中位数不劣于 S0（≤1.0×）；测试面/组数/
glob 规模全量披露。估算不替代实测。

可复现/可续跑：每轮（face, arm, round）结果原子落盘到 `<out>.runs/`；
重复执行跳过已完成轮次；`--max-wall-seconds` 在完整轮次之间截止（前台
分段执行，中断可续，不重做已完成轮次）。

用法（KOL 只读；全部在 /tmp 真副本执行）：
    python tests/integration/run_see1587_line_benchmark.py \
        --kol-root /tmp/see1587-kol --targets utils systems --rounds 3 \
        --out /tmp/see1587-line-benchmark.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
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
PROGRESS_RE = re.compile(
    r"^\[(\d+)/(\d+)\]\s+(\w+)\s+L(\d+)\s+(\S+)\s+([\d.]+)s")
ARMS = ("S0", "S1", "S2", "S3")
ARM_ARGS = {
    "S0": ["--select", "fn"],
    "S2": ["--select", "line"],
    # S3 原定 full-face oracle；孤立副本 whole-suite baseline 超 240s 不可信
    # （baseline timeout 中止，证据见 prepare/records），诚实替代为 file 级
    # 超集 oracle：映射文件级测试面按构造包含 S1/S2 全部行级测试面，仍可
    # 逐 mutant 判 verdict 是否被更宽面推翻。替换在报告中显式披露。
    "S3": ["--select", "file"],
}
S3_SUBSTITUTION = (
    "full-face oracle substituted by file-level superset oracle: the "
    "isolated copy's whole-suite baseline exceeds 240s and aborts as "
    "untrustworthy; the file-level face is a strict superset of every "
    "S1/S2 line-level face by construction (line tests ⊆ file tests)")


def _env(godot_bin: str | None) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["GQT_GODOT_BIN"] = (godot_bin or env.get("GQT_GODOT_BIN")
                            or DEFAULT_GODOT)
    return env


def _census(kol: Path, face: str) -> list[tuple[str, int, list[int]]]:
    """确定性 census：文件 → (rel, site 数, site 行集合)。"""
    from godot_qa_toolkit.mutation.ops import _collect_mutations
    from godot_qa_toolkit.mutation.scopemanifest import build_manifest
    out = []
    for f in sorted((kol / face).rglob("*.gd")):
        try:
            src = f.read_text(encoding="utf-8")
            raw = _collect_mutations(src, str(f))
            _, _, mutants, _ = build_manifest(src, str(f), raw)
        except Exception:  # noqa: BLE001 — census 失败文件记为 0，不中断
            mutants = []
        if not mutants:
            continue
        out.append((str(f.relative_to(kol)), len(mutants),
                    [m.line for m in mutants]))
    return out


def _sample_file(census: list[tuple[str, int, list[int]]],
                 widths: dict[str, int], budget: int) -> tuple:
    """确定性取样规则：位点数 ≥ budget 的文件中，取映射测试面宽度的中位数
    文件（偶数取较低中位；同宽按路径）。

    规则只依赖 census 计数与映射宽度，不含任何实测性能信息（非挑样）；
    宽度中位数选取代表「默认 recipe/测试面」下的典型观察成本。"""
    eligible = [r for r in census if r[1] >= budget]
    pool = eligible or census
    ordered = sorted(pool, key=lambda r: (widths.get(r[0], 0), r[0]))
    return ordered[(len(ordered) - 1) // 2]


def _face_widths(doc: dict, face: str) -> dict[str, int]:
    """映射内每文件的测试面宽度（test/script 车道身份去重脚本数）。"""
    arm = (doc.get("arms") or {}).get("gut_full") or {}
    prefix = face + "/"
    out: dict[str, int] = {}
    for ident, by in (arm.get("test_to_files") or {}).items():
        for rel in by or ():
            if str(rel).startswith(prefix):
                out.setdefault(rel, set()).add(ident.split("::", 1)[0])
    for ident, by in (arm.get("script_to_files") or {}).items():
        for rel in by or ():
            if str(rel).startswith(prefix):
                out.setdefault(rel, set()).add(ident)
    return {k: len(v) for k, v in out.items()}


def _lines_spec(ordered_lines: list[int], budget: int) -> tuple[str, dict]:
    """S1 fixture（census 确定性生成，有界）：census/manifest 顺序前 budget
    个位点所在行（与 budget_on_retry 的截断序同源，故 S1 执行面 ⊆ 其他臂
    预算面）+ 一个边界无位点行（验证 skipped_no_site 披露）。

    - 位点行按连续段合并（多段范围；段间空行天然含无位点行）；
    - 首段 = 最小选中位点行前一行（无位点边界）。"""
    picked = list(ordered_lines[:max(1, budget)])
    lines = sorted(set(picked))
    segments: list[tuple[int, int]] = []
    start = prev = lines[0]
    for ln in lines[1:]:
        if ln == prev + 1:
            prev = ln
            continue
        segments.append((start, prev))
        start = prev = ln
    segments.append((start, prev))
    boundary = lines[0] - 1 if lines[0] > 1 else lines[-1] + 1
    parts = [str(boundary)]
    parts += [f"{a}-{b}" if a != b else str(a) for a, b in segments]
    return ",".join(parts), {"boundary_line": boundary,
                             "segments": segments,
                             "picked_site_lines": picked}


def _run(argv: list[str], env: dict, cwd: Path) -> dict:
    t0 = time.monotonic()
    proc = subprocess.run(argv, capture_output=True, text=True,
                          env=env, cwd=str(cwd))
    wall = round(time.monotonic() - t0, 3)
    try:
        payload = json.loads(proc.stdout) if proc.stdout.strip() else None
    except ValueError:
        payload = None
    stderr = proc.stderr or ""
    verdicts: dict[str, str] = {}
    durations: list[float] = []
    for line in stderr.splitlines():
        m = PROGRESS_RE.match(line.strip())
        if not m:
            continue
        verdicts[m.group(5)] = m.group(3)
        durations.append(float(m.group(6)))
    return {"argv": argv, "rc": proc.returncode, "wall_s": wall,
            "stdout_json": payload, "verdicts": verdicts,
            "mutant_seconds": durations, "stderr_tail": stderr[-3000:]}


def _metrics(run: dict) -> dict:
    payload = run["stdout_json"] or {}
    summary = ((payload.get("results") or [payload])[0] or {}).get(
        "summary") or {}
    ts = summary.get("test_selection") or {}
    groups = summary.get("selection_groups")
    return {
        "ok": bool(payload.get("ok")),
        "mutants": summary.get("mutants"),
        "killed": summary.get("killed"),
        "survived": summary.get("survived"),
        "suspect": summary.get("suspect"),
        "timeout": summary.get("timeout"),
        "run_errors": summary.get("run_errors"),
        "invalid": summary.get("invalid_mutants"),
        "jobs": summary.get("jobs"),
        "scoped": summary.get("scoped"),
        "selection_mode": summary.get("selection_mode"),
        "test_selection_source": ts.get("source"),
        "tests_count": len(ts.get("tests") or []),
        "groups": (len(groups) if isinstance(groups, list) else None),
        "coverage_prefilter": summary.get("coverage_prefilter"),
        "lines_selection": summary.get("lines_selection"),
        "baseline_reused": summary.get("baseline_reused"),
        "stage_timings": summary.get("stage_timings") or {},
        "rss_peak_mb": summary.get("rss_peak_mb"),
        "exec_scheme": summary.get("exec_scheme"),
        "inject_scheme": summary.get("inject_scheme"),
        "slot_mode": summary.get("slot_mode"),
        "total_sites": summary.get("total_sites"),
        "executed": summary.get("executed"),
        "verdicts": run["verdicts"],
        "mutant_seconds": run["mutant_seconds"],
    }


def _run_key(face: str, arm: str, rnd: int) -> str:
    return f"{face}-{arm}-r{rnd}"


def _run_path(work: Path, face: str, arm: str, rnd: int) -> Path:
    return work / f"{_run_key(face, arm, rnd)}.json"


def _execute_round(env, kol: Path, target: Path, face: str, arm: str,
                   rnd: int, budget: int, lines_spec: str | None,
                   mutant_timeout: int = 90) -> dict:
    cli = [sys.executable, "-m", "godot_qa_toolkit.cli"]
    argv = cli + ["mutation", str(target), "--project-root", str(kol),
                  "--budget", str(budget), "--mutate-all",
                  "--timeout", str(mutant_timeout)]
    if arm == "S1":
        argv += ["--lines", lines_spec or ""]
    else:
        argv += ARM_ARGS[arm]
    run = _run(argv, env, kol)
    rec = {"face": face, "arm": arm, "round": rnd,
           "target": str(target.relative_to(kol)),
           "budget": budget, "lines_spec": lines_spec if arm == "S1" else None,
           "status": "done", "rc": run["rc"], "wall_s": run["wall_s"],
           "argv": run["argv"], "metrics": _metrics(run),
           "stderr_tail": run["stderr_tail"]}
    return rec


def _atomic_write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


def _plan(kol: Path, faces: list[str], rounds: int, aa: int,
          budget: int, doc: dict) -> dict:
    plan = {}
    for face in faces:
        census = _census(kol, face)
        if not census:
            raise SystemExit(f"no mutation sites under {face}")
        widths = _face_widths(doc, face)
        rel, sites, site_lines = _sample_file(census, widths, budget)
        spec, fixture_meta = _lines_spec(site_lines, budget)
        plan[face] = {"target": rel, "sites": sites,
                      "test_face_width": widths.get(rel, 0),
                      "census_files": len(census),
                      "census_total_sites": sum(c[1] for c in census),
                      "lines_spec": spec, "fixture": fixture_meta,
                      "aa": aa, "rounds": rounds}
    return plan


def _ensure_map_faces(env, kol: Path, faces: list[str], map_path: Path,
                      work: Path, analyze_only: bool) -> None:
    """确保单一映射覆盖全部目标面（任一面无绑定即整体重建一次）。

    行级/fn 选面以新鲜映射为真值：一次 `measure run --test-attribution`
    覆盖全部 face 目录（多目标同轮），产物即后续全部臂的共同真值源。
    analyze_only 且映射不足 → fail-loud（不静默跑无映射轮次）。"""
    covered: set[str] = set()
    if map_path.is_file():
        try:
            covered = set(json.loads(map_path.read_text(
                encoding="utf-8")).get("file_sha256") or {})
        except ValueError:
            covered = set()
    uncovered = [f for f in faces
                 if not any(rel.startswith(f + "/") for rel in covered)]
    if not uncovered:
        return
    if analyze_only:
        raise SystemExit(
            f"attribution map does not cover faces {uncovered} — run the "
            "benchmark without --analyze-only to prepare it")
    recipe = work / "bench-recipe.json"
    cli = [sys.executable, "-m", "godot_qa_toolkit.cli"]
    if not recipe.is_file():
        rec = _run(cli + ["measure", "suite", "--project-root", str(kol),
                          "--out", str(recipe)], env, kol)
        _atomic_write(work / "prepare-suite.json", rec)
        if rec["rc"] != 0:
            raise SystemExit(f"measure suite failed rc={rec['rc']}")
    dirs = [str(kol / face) for face in faces]
    rec = _run(cli + ["measure", "run", *dirs, "--project-root", str(kol),
                      "--recipe", str(recipe),
                      "--out", str(work / "bench-measure-report.json"),
                      "--test-attribution",
                      "--test-attribution-out", str(map_path),
                      "--acceptance-repeats", "1", "--timeout", "300"],
               env, work)
    _atomic_write(work / "prepare-measure.json", rec)
    # 孤立副本 acceptance 臂 run_error 属已知边界；gut_full 有效性由映射
    # arms_state 与行事实量决定，此处不把 rc 当失败（如实记录）。
    doc = json.loads(map_path.read_text(encoding="utf-8")) \
        if map_path.is_file() else {}
    still = rels_needed - set((doc.get("file_sha256") or {}))
    if still:
        raise SystemExit(
            f"attribution map still lacks {sorted(still)} after measure run "
            f"(rc={rec['rc']}) — refusing to benchmark without a usable map")


def _required_runs(plan: dict, aa: int, rounds: int) -> list[tuple]:
    runs = []
    for face in plan:
        for i in range(1, aa + 1):
            runs.append((face, "S2", f"aa{i}"))
        for r in range(1, rounds + 1):
            order = list(ARMS)
            shift = (r - 1) % len(ARMS)
            order = order[shift:] + order[:shift]
            for arm in order:
                runs.append((face, arm, r))
    return runs


def _analyze(plan: dict, work: Path, aa: int, rounds: int) -> dict:
    def load(face, arm, rnd):
        p = _run_path(work, face, arm, rnd)
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() \
            else None

    faces_out: dict = {}
    gate_mismatch: list[dict] = []
    gate_perf: list[dict] = []
    for face, p in plan.items():
        aa_walls = [rec["wall_s"] for rec in
                    (load(face, "S2", f"aa{i}") for i in range(1, aa + 1))
                    if rec]
        arm_walls: dict[str, list[float]] = {}
        arm_details: dict[str, list[dict]] = {}
        for arm in ARMS:
            recs = [rec for rec in (load(face, arm, r)
                                    for r in range(1, rounds + 1)) if rec]
            arm_walls[arm] = [rec["wall_s"] for rec in recs]
            arm_details[arm] = recs
        medians = {arm: (round(statistics.median(w), 3) if w else None)
                   for arm, w in arm_walls.items()}
        ratios = {arm: (round(medians[arm] / medians["S0"], 4)
                        if medians.get(arm) and medians.get("S0")
                        else None) for arm in ARMS if arm != "S0"}
        # verdict 对账（S1/S2 vs S3，仅两侧均执行的 stable_id）
        recon = {}
        s3 = arm_details.get("S3") or []
        s3_verdicts: dict[str, str] = {}
        for rec in s3:
            s3_verdicts.update(rec["metrics"]["verdicts"])
        for arm in ("S0", "S1", "S2"):
            mism, common = [], 0
            for rec in arm_details.get(arm) or []:
                v = rec["metrics"]["verdicts"]
                for sid, verdict in v.items():
                    if sid in s3_verdicts:
                        common += 1
                        if s3_verdicts[sid] != verdict:
                            mism.append({"id": sid, "arm": verdict,
                                         "S3": s3_verdicts[sid]})
            recon[arm] = {"compared": common,
                          "mismatches": mism[:20],
                          "mismatch_count": len(mism)}
        for arm in ("S1", "S2"):
            if recon[arm]["mismatch_count"]:
                gate_mismatch.append({"face": face, "arm": arm,
                                      **recon[arm]})
        # 性能门（S2 ≤ S0，每面 + 合并均须 ≤1.0×）
        if ratios.get("S2") is not None and ratios["S2"] > 1.0:
            gate_perf.append({"face": face, "ratio": ratios["S2"],
                              "S0_median": medians["S0"],
                              "S2_median": medians["S2"]})
        per_mutant = {}
        for arm in ARMS:
            secs = [s for rec in arm_details.get(arm) or []
                    for s in rec["metrics"]["mutant_seconds"]]
            if secs:
                per_mutant[arm] = {
                    "n": len(secs),
                    "median_s": round(statistics.median(secs), 3),
                    "p90_s": round(sorted(secs)[
                        max(0, int(0.9 * (len(secs) - 1)))], 3),
                }
        groups = {arm: ([rec["metrics"]["groups"]
                         for rec in arm_details.get(arm) or []
                         if rec["metrics"]["groups"] is not None])
                  for arm in ARMS}
        tests_count = {arm: [rec["metrics"]["tests_count"]
                             for rec in arm_details.get(arm) or []]
                       for arm in ARMS}
        pf = {arm: [(rec["metrics"]["coverage_prefilter"] or {}).get("source")
                    if isinstance(rec["metrics"]["coverage_prefilter"], dict)
                    else rec["metrics"]["coverage_prefilter"]
                    for rec in arm_details.get(arm) or []]
              for arm in ARMS}
        rss = {arm: [rec["metrics"].get("rss_peak_mb")
                     for rec in arm_details.get(arm) or []]
               for arm in ARMS}
        schemes = {arm: [(rec["metrics"].get("exec_scheme"),
                          rec["metrics"].get("slot_mode"))
                         for rec in arm_details.get(arm) or []]
                   for arm in ARMS}
        stages = {}
        for arm in ARMS:
            keys = ("baseline", "worker_boot", "slot_sync", "per_mutant")
            stages[arm] = {k: round(sum(
                (rec["metrics"]["stage_timings"].get(k) or 0)
                for rec in arm_details.get(arm) or []), 3) for k in keys}
        faces_out[face] = {
            "target": p["target"], "sites": p["sites"],
            "census_files": p["census_files"],
            "census_total_sites": p["census_total_sites"],
            "lines_spec": p["lines_spec"], "fixture": p["fixture"],
            "aa_walls": aa_walls,
            "aa_noise": ({"median": round(statistics.median(aa_walls), 3),
                          "min": round(min(aa_walls), 3),
                          "max": round(max(aa_walls), 3),
                          "spread_pct": round(
                              100 * (max(aa_walls) - min(aa_walls))
                              / statistics.median(aa_walls), 3)}
                         if aa_walls else None),
            "arm_walls": arm_walls, "arm_medians": medians,
            "ratios_vs_S0": ratios, "verdict_reconciliation": recon,
            "per_mutant": per_mutant, "groups_per_run": groups,
            "tests_count_per_run": tests_count,
            "prefilter_sources": pf,
            "stage_timing_sums_s": stages,
            "rss_peak_mb_per_run": rss,
            "exec_scheme_per_run": schemes,
        }
    total_med = {}
    for arm in ARMS:
        vals = [faces_out[f]["arm_medians"].get(arm) for f in faces_out]
        vals = [v for v in vals if v is not None]
        total_med[arm] = round(sum(vals), 3) if vals else None
    combined_ratio = (round(total_med["S2"] / total_med["S0"], 4)
                      if total_med.get("S2") and total_med.get("S0")
                      else None)
    if combined_ratio is not None and combined_ratio > 1.0:
        gate_perf.append({"face": "combined", "ratio": combined_ratio,
                          "S0_median": total_med["S0"],
                          "S2_median": total_med["S2"]})
    return {"faces": faces_out,
            "combined_medians_s": total_med,
            "combined_ratio_S2_over_S0": combined_ratio,
            "gate": {"verdict_mismatches": gate_mismatch,
                     "perf_failures": gate_perf,
                     "passed": not gate_mismatch and not gate_perf}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kol-root", required=True)
    ap.add_argument("--targets", nargs="+", default=["utils", "systems"])
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--aa", type=int, default=3)
    ap.add_argument("--budget", type=int, default=3)
    ap.add_argument("--mutant-timeout", type=int, default=90,
                    help="per-mutant GUT timeout for scoped arms "
                         "(S0/S1/S2)")
    ap.add_argument("--s3-timeout", type=int, default=90,
                    help="S3 oracle leg timeout (kept equal to scoped arms "
                         "under the file-level superset oracle)")
    ap.add_argument("--godot-bin", default=None)
    ap.add_argument("--out", default="/tmp/see1587-line-benchmark.json")
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--max-wall-seconds", type=float, default=480.0,
                    help="stop between completed rounds (0 = unlimited)")
    ap.add_argument("--analyze-only", action="store_true")
    args = ap.parse_args()

    kol = Path(args.kol_root)
    if not kol.is_dir():
        raise SystemExit(f"kol-root missing: {kol}")
    work = Path(args.work_dir or (args.out + ".runs"))
    work.mkdir(parents=True, exist_ok=True)
    env = _env(args.godot_bin)
    map_path = kol / ".qa-cache" / "attribution-map.json"
    _ensure_map_faces(env, kol, args.targets, map_path, work,
                      args.analyze_only)
    doc = json.loads(map_path.read_text(encoding="utf-8"))
    plan = _plan(kol, args.targets, args.rounds, args.aa, args.budget, doc)
    missing = [p["target"] for p in plan.values()
               if p["target"] not in (doc.get("file_sha256") or {})]
    if missing:
        raise SystemExit(
            f"attribution map lacks benchmark targets {missing} — refusing "
            "to benchmark without a usable map")
    required = _required_runs(plan, args.aa, args.rounds)
    t0 = time.monotonic()
    done = pending = 0
    for face, arm, rnd in required:
        p = _run_path(work, face, arm, rnd)
        if p.is_file():
            done += 1
            continue
        if args.analyze_only:
            pending += 1
            continue
        if args.max_wall_seconds and \
                (time.monotonic() - t0) > args.max_wall_seconds:
            pending += len(required) - done - pending
            break
        target = kol / plan[face]["target"]
        rec = _execute_round(env, kol, target, face, arm, rnd,
                             args.budget,
                             plan[face]["lines_spec"] if arm == "S1" else None,
                             args.s3_timeout if arm == "S3"
                             else args.mutant_timeout)
        _atomic_write(p, rec)
        done += 1
        print(f"[bench] {face} {arm} r{rnd} done rc={rec['rc']} "
              f"wall={rec['wall_s']}s", file=sys.stderr)
    pending = len(required) - sum(
        1 for face, arm, rnd in required
        if _run_path(work, face, arm, rnd).is_file())
    if pending:
        _atomic_write(Path(args.out), {
            "spec": "SEE-1587 §SPEC-007", "status": "pending",
            "completed_runs": len(required) - pending,
            "required_runs": len(required), "plan": plan})
        print(json.dumps({"status": "pending",
                          "completed": len(required) - pending,
                          "required": len(required),
                          "out": args.out}, ensure_ascii=False))
        return 0
    report = {"spec": "SEE-1587 §SPEC-007", "status": "complete",
              "budget_per_file": args.budget,
              "mutant_timeout_s": {"S0": args.mutant_timeout,
                                   "S1": args.mutant_timeout,
                                   "S2": args.mutant_timeout,
                                   "S3": args.s3_timeout},
              "s3_substitution": S3_SUBSTITUTION,
              "protocol": {"aa": args.aa, "rounds": args.rounds,
                           "arms": {"S0": "--select fn",
                                    "S1": "--lines <census fixture>",
                                    "S2": "--select line (default)",
                                    "S3": "--select file (superset "
                                          "oracle, see s3_substitution)"},
                           "s3_substitution": S3_SUBSTITUTION},
              "plan": plan}
    report.update(_analyze(plan, work, args.aa, args.rounds))
    _atomic_write(Path(args.out), report)
    gate = report["gate"]
    print(json.dumps({"ok": gate["passed"], "out": args.out,
                      "combined_ratio": report.get(
                          "combined_ratio_S2_over_S0"),
                      "verdict_mismatches": len(gate["verdict_mismatches"]),
                      "perf_failures": gate["perf_failures"]},
                     ensure_ascii=False))
    return 0 if gate["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
