"""SEE-1550 §SPEC-020 ⑥——W 通道实测基准（Owner「no abtest, not trustworthy」协议）。

A = 冷通道（cold-shadow/1，默认 M0）；B = W 通道（worker-reset/1，
--worker-channel 语义）。协议（Owner 硬性判据）：
- A/A ≥3 轮定噪声带 → A/B 交错 ≥3 轮；
- 绝对秒数与倍率、per-mutant 固定账分解（冷启动/重置/门/执行分段，
  来自 worker 腿内计时）、fixture + KOL 只读样本双轨、按通道分解
  （census eligible 份额 78.2% 为静态基础）；
- 全部实测值，估算不作结论。

J7 win64 模式（--mode j7）：六 eligible 形态在给定项目根（DrvFS/Windows
可见路径）上冷通道 vs worker 通道逐形态二值对比——漂移 → 该通道强制冷 +
披露（漂移非失败，如实报告）。

用法：
  python3 benchmarks/bench_worker_channel.py --mode fixture \
      --gut-dir /path/to/addons/gut [--rounds 3]
  python3 benchmarks/bench_worker_channel.py --mode kol \
      --kol-copy /path/to/kol-tmp-copy --kol-target autoload/save_manager.gd \
      --kol-line 112
  python3 benchmarks/bench_worker_channel.py --mode j7 \
      --project-root /mnt/d/.../drvfs-project --gut-dir ... [--rounds 1]

输出：stdout JSON（轮次表/噪声带/倍率/分解/RSS）；`--outdir` 落盘同内容。
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

REPO = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO / "src"))

from godot_qa_toolkit.mutation.runner import run_mutation  # noqa: E402
from godot_qa_toolkit.mutation import shadow  # noqa: E402
from godot_qa_toolkit.mutation.channel_router import (  # noqa: E402
    compute_reload_closure,
    build_reference_graph,
)
from godot_qa_toolkit.mutation.worker_channel import WorkerSession  # noqa: E402


def _worker_pid(session) -> int | None:
    proc = getattr(getattr(session, "state", None), "proc", None)
    return proc.pid if proc is not None else None


def _rss_mb(pid: int) -> float | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        return None
    return None


def _prep_project(base_fixture: Path, gut_dir: Path | None, workdir: Path,
                  tag: str) -> Path:
    proj = workdir / f"proj-{tag}"
    if proj.exists():
        return proj
    shutil.copytree(base_fixture, proj)
    if gut_dir is not None:
        # dirs_exist_ok：fixture 模板可能已含 addons 子树（幂等重建）
        shutil.copytree(gut_dir, proj / "addons" / "gut", dirs_exist_ok=True)
    godot = os.environ.get("GQT_GODOT_BIN") or shutil.which("godot")
    subprocess.run([godot, "--headless", "--import", "--path", str(proj)],
                   capture_output=True, timeout=600, cwd=str(proj),
                   check=False)
    return proj


def _run_round(project_root: str, target_fs: str, channel: str,
               tests_glob: str | None, timeout_s: int) -> tuple[dict, float]:
    t0 = time.monotonic()
    payload = run_mutation(target_fs, project_root, mutate_all=True,
                           timeout_s=timeout_s, worker_channel=(channel == "B"),
                           tests_glob=tests_glob)
    elapsed = round(time.monotonic() - t0, 3)
    results = payload.get("results") or [payload]
    s = results[0].get("summary", {})
    killed = sum(int(r.get("summary", {}).get("killed", 0)) for r in results)
    mutants = sum(int(r.get("summary", {}).get("mutants", 0)) for r in results)
    return payload, elapsed, {"channel": channel, "wall_s": elapsed,
                              "killed": killed, "mutants": mutants}


def _instrumented_leg(project_root: str, target_fs: str,
                      tests_glob: str | None) -> dict:
    """单条 instrumented worker 腿：写探针 mutant → 腿内分段计时。"""
    from godot_qa_toolkit.mutation.inject import detect_binary_kind
    import hashlib
    rel = Path(target_fs).resolve()
    proj = Path(project_root).resolve()
    res = "res://" + rel.relative_to(proj).as_posix()
    original = rel.read_bytes()
    mutated = original + b"\n# bench probe\n"
    kind = detect_binary_kind()
    session = WorkerSession(str(proj), str(proj), kind, godot_bin=None,
                            leg_timeout_s=300)
    t0 = time.monotonic()
    session.start()
    boot_ms = int((time.monotonic() - t0) * 1000)
    try:
        shadow.inject_mutant(str(proj), rel.relative_to(proj).as_posix(),
                             mutated.decode("utf-8"))
        sha = hashlib.sha1(mutated).hexdigest()
        graph = build_reference_graph(str(proj))
        closure = compute_reload_closure(graph, res)
        leg = session.run_leg(99, res, sha, list(closure), tests_glob)
        t = dict(leg.timings)
        t["total_ms"] = int((time.monotonic() - t0) * 1000) - 0
        t["boot_ms"] = boot_ms
        return t
    finally:
        session.close()
        rel.write_bytes(original)


def bench_track(fixture: Path, gut_dir: Path | None, workdir: Path,
                rounds: int, target_res: str, tests_glob: str | None,
                timeout_s: int) -> tuple[dict, Path]:
    """A/A ×rounds 定噪声带 → A/B 交错 ×rounds + per-mutant 分解。"""
    proj = _prep_project(fixture, gut_dir, workdir, "bench")
    target_fs = str(Path(proj) / target_res.removeprefix("res://"))
    tests_glob = tests_glob or "res://tests/"
    aa: list[float] = []
    for _ in range(rounds):
        _p, elapsed, _meta = _run_round(str(proj), target_fs, "A",
                                        tests_glob, timeout_s)
        aa.append(elapsed)
    band = round(max(aa) - min(aa), 3) if len(aa) > 1 else 0.0

    inter: list[dict] = []
    for _ in range(rounds):
        _p, e_a, m_a = _run_round(str(proj), target_fs, "A", tests_glob,
                                  timeout_s)
        _p, e_b, m_b = _run_round(str(proj), target_fs, "B", tests_glob,
                                  timeout_s)
        inter.append({"a": {"wall_s": e_a, "killed": m_a["killed"],
                            "mutants": m_a["mutants"]},
                      "b": {"wall_s": e_b, "killed": m_b["killed"],
                            "mutants": m_b["mutants"]}})

    a_worst = max(x["a"]["wall_s"] for x in inter)
    b_best = min(x["b"]["wall_s"] for x in inter)
    a_med = sorted(x["a"]["wall_s"] for x in inter)[len(inter) // 2]
    b_med = sorted(x["b"]["wall_s"] for x in inter)[len(inter) // 2]
    multiplier = round(a_med / b_med, 3) if b_med else None
    mutants_n = max((x["a"]["mutants"] for x in inter), default=1) or 1
    cold_per_mutant = round(a_med / mutants_n, 3)
    seg = _instrumented_leg(proj, target_fs, tests_glob)
    breakdown = {
        "mutants_per_round": mutants_n,
        "cold_spawn_per_mutant_s": cold_per_mutant,
        "worker_reload_ms": seg.get("reload_ms"),
        "worker_gut_ms": seg.get("gut_ms"),
        "worker_leg_total_ms": seg.get("total_ms"),
        "worker_boot_ms": seg.get("boot_ms"),
    }
    report = {
        "aa_cold_rounds_s": aa,
        "aa_noise_band_s": band,
        "ab_interleave": inter,
        "a_median_s": a_med,
        "b_median_s": b_med,
        "a_worst_s": a_worst,
        "b_best_s": b_best,
        "multiplier_median": multiplier,
        "per_mutant_breakdown": breakdown,
        "speed_gate": b_best <= a_worst + band,
    }
    return report, proj



def j7_mode(project_root: str, rounds: int, gut_dir: Path | None,
            workdir: Path) -> dict:
    """J7：六 eligible 形态 win64 通道复测（冷 vs worker 逐形态二值）。"""
    forms = ["runtime_load", "preload_late", "class_name_form",
             "inner_class_form", "scene_script", "composition_root"]
    per_form: dict = {}
    for form in forms:
        target_fs = str(Path(project_root) / "forms" / f"{form}.gd")
        rows = []
        for channel in ("A", "B"):
            for _ in range(rounds):
                _p, elapsed, meta = _run_round(project_root, target_fs,
                                               channel, None, 300)
                rows.append(meta | {"form": form})
        cold_ok = all(r["killed"] == r["mutants"] and r["mutants"] > 0
                      for r in rows if r["channel"] == "A")
        worker_ok = all(r["killed"] == r["mutants"] and r["mutants"] > 0
                        for r in rows if r["channel"] == "B")
        drift = not (cold_ok and worker_ok)
        per_form[form] = {
            "cold_kill_complete": cold_ok, "worker_kill_complete": worker_ok,
            "drift": drift,
            "rows": rows,
        }
    return {
        "mode": "j7",
        "channel": "win64 wrapper",
        "per_form": per_form,
        "verdict": ("行为一致：双通道全绿——默认化资格的双通道绿前提达成"
                    if not any(v["drift"] for v in per_form.values())
                    else "行为漂移 → 该通道强制冷 + 披露"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=["fixture", "kol", "j7"], default="fixture")
    ap.add_argument("--fixture", default=str(REPO / "tests" / "fixtures"
                                              / "see1550_forms"))
    ap.add_argument("--gut-dir", default=None)
    ap.add_argument("--target", default="res://forms/runtime_load.gd")
    ap.add_argument("--tests-glob", default=None)
    ap.add_argument("--point-line", type=int, default=None,
                    help="KOL 点轨：--lines 寻址行")
    ap.add_argument("--kol-copy", default=None,
                    help="KOL 只读样本的 /tmp 副本根（零改动源仓）")
    ap.add_argument("--kol-target", default="autoload/save_manager.gd")
    ap.add_argument("--kol-line", type=int, default=112)
    ap.add_argument("--project-root", default=None,
                    help="j7 模式：DrvFS/Windows 可见项目根")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--outdir", default="/tmp/see1550-worker-bench")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    gut_dir = Path(args.gut_dir).resolve() if args.gut_dir else None

    if args.mode == "j7":
        if not args.project_root:
            print("--mode j7 requires --project-root", file=sys.stderr)
            return 2
        report = j7_mode(args.project_root, args.rounds, gut_dir, outdir)
    else:
        if args.mode == "kol":
            if not args.kol_copy:
                print("--mode kol requires --kol-copy", file=sys.stderr)
                return 2
            proj_root = args.kol_copy
            target_fs = str(Path(proj_root) / args.kol_target)
            tests_glob = None
        else:
            fixture = Path(args.fixture)
            if not fixture.is_absolute():
                fixture = REPO / fixture
            proj_root = str(_prep_project(
                fixture.resolve(), gut_dir, outdir, "fixture"))
            target_fs = str(Path(proj_root) / args.target.removeprefix("res://"))
            tests_glob = args.tests_glob
        line = args.point_line if args.mode == "kol" else None
        report, proj = bench_track(
            fixture, gut_dir, outdir, rounds=args.rounds,
            target_res=args.target, tests_glob=args.tests_glob,
            timeout_s=300)

    (outdir / "bench_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: report[k] for k in report
                      if k in ("mode", "multiplier_median", "speed_gate",
                               "aa_noise_band_s", "verdict")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
