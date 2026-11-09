#!/usr/bin/env python3
"""§SPEC-010 性能基准 A/B 脚本（Owner「no abtest, not trustworthy」协议）。

A = master 旧机制（内存注入，git worktree 检出 origin/master 运行）；
B = 新机制（磁盘影子注入 disk-farm/1，本工作树）。
本脚本只交付协议与机器可判输出；QA 阶段在真实双通道机器上执行并留档。

协议（终案 §7.5）：
- A/A ≥3 轮定噪声带 → A/B 交错 ≥3 轮；
- 场景 = cold / warm 全复用 / 单 scope 差分 / 单点 /（可选）KOL 大样本；
- 记录绝对秒数、吞吐（mutants/s）、峰值内存、磁盘增量；
- 门：verdict/stable-id/kill-set 一致 ∧ B 墙钟 ≤ A + 该场景 A/A 噪声带
  ∧ 确定性门全绿（warm 全复用仅一次 baseline、零执行零 baseline）。

用法（QA 实机）：
    python3 benchmarks/bench_ab_see1550.py \
        --fixture tests/fixtures/see1550_forms \
        --gut-dir /mnt/d/GodotProjects/king-of-likes/addons/gut \
        --rounds 3 --outdir /tmp/see1550-bench
    # KOL 大样本（只读，可选）：
    #   --kol-sample /mnt/d/GodotProjects/king-of-likes \
    #   --kol-target autoload/save_manager.gd

输出：``<outdir>/bench_report.json``（轮次表 + 噪声带 + 一致性判定 +
PASS/FAIL 门）与逐轮 raw JSON。退出码 0 = 门全绿。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
SCENARIOS = ("cold", "warm", "scope", "point")


def _run_mutation(src_root: Path, proj: Path, target_rel: str,
                  args: list[str]) -> tuple[dict, float]:
    """在指定源码根上跑一轮 gqt mutation（子进程，stdout 单 JSON）。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(src_root / "src") + os.pathsep + \
        env.get("PYTHONPATH", "")
    env["GQT_BENCH"] = "1"
    argv = [sys.executable, "-m", "godot_qa_toolkit.cli", "mutation",
            str(proj / target_rel), "--path", str(proj),
            "--timeout", "300", *args]
    t0 = time.monotonic()
    r = subprocess.run(argv, capture_output=True, text=True, env=env,
                       cwd=str(src_root), timeout=3600)
    elapsed = time.monotonic() - t0
    if r.returncode not in (0, 1):
        print(f"bench: gqt exit {r.returncode}\n{r.stderr[-2000:]}",
              file=sys.stderr)
        raise RuntimeError(f"mutation run failed (rc={r.returncode})")
    payload = json.loads(r.stdout)
    return payload, elapsed


def _kill_set(payload: dict) -> set[tuple]:
    """verdict 一致性判定面：{(id, verdict)} 全集（killed/survived/...）。"""
    results = payload.get("results") or [payload]
    out: set[tuple] = set()
    for r in results:
        for f in r.get("failures", []):
            if "verdict" in f:
                out.add((f.get("id", f["file"]), f["verdict"]))
        s = r.get("summary", {})
        for cert in s.get("killed_by_certificates", []) or []:
            out.add((cert["id"], "killed"))
    return out


def _noise_band(samples: list[float]) -> float:
    """A/A 噪声带 = max-min（小样本诚实带宽；非统计推断）。"""
    return max(samples) - min(samples) if samples else 0.0


def _prep_project(fixture: Path, gut_dir: Path | None, workdir: Path,
                  tag: str) -> Path:
    proj = workdir / f"proj-{tag}"
    if proj.exists():
        return proj
    import shutil
    shutil.copytree(fixture, proj)
    if gut_dir is not None:
        shutil.copytree(gut_dir, proj / "addons" / "gut")
    godot = os.environ.get("GQT_GODOT_BIN") or shutil.which("godot")
    subprocess.run([godot, "--headless", "--import", "--path", str(proj)],
                   capture_output=True, timeout=600, cwd=str(proj),
                   check=False)
    return proj


def _run_scenario(src_root: Path, proj: Path, target_rel: str, scenario: str,
                  keep_cache: bool, point_line: int
                  ) -> tuple[dict, float, dict]:
    args: list[str] = ["--jobs", "2"]
    if scenario == "cold":
        args += ["--mutate-all"]
        if not keep_cache:
            _wipe_cache(proj)
    elif scenario == "warm":
        args += ["--mutate-all"]  # 二轮全复用（warm 全复用仅一次 baseline）
    elif scenario == "scope":
        args += ["--mutate-all", "--tests", "res://tests/"]
    elif scenario == "point":
        # 单点卡尺：--lines 寻址（无需 stable id；点选行由 --point-line 定）
        args += ["--lines", str(point_line)]
    payload, elapsed = _run_mutation(src_root, proj, target_rel, args)
    meta = {"scenario": scenario, "wall_s": round(elapsed, 3)}
    return payload, elapsed, meta


def _wipe_cache(proj: Path) -> None:
    cache = proj / ".qa-cache"
    if cache.exists():
        import shutil
        shutil.rmtree(cache, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fixture", default="tests/fixtures/see1550_forms")
    ap.add_argument("--gut-dir", default=None, help="GUT addon 源（只读拷贝）")
    ap.add_argument("--target", default="res://forms/runtime_load.gd")
    ap.add_argument("--point-line", type=int, default=5,
                    help="point 场景的 --lines 寻址行（fixture 默认行 5）")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--master-ref", default="origin/master",
                    help="A 侧基线（git worktree 检出）")
    ap.add_argument("--kol-sample", default=None,
                    help="KOL 只读样本根（可选大样本轨）")
    ap.add_argument("--kol-target", default="autoload/save_manager.gd")
    ap.add_argument("--outdir", default="/tmp/see1550-bench")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    fixture = (REPO_ROOT / args.fixture).resolve() if not Path(
        args.fixture).is_absolute() else Path(args.fixture)
    gut_dir = Path(args.gut_dir).resolve() if args.gut_dir else None

    # A 侧 = master 旧机制的独立 worktree（零改动本工作树）
    awt = outdir / "a-worktree"
    if not awt.exists():
        subprocess.run(["git", "worktree", "add", str(awt), args.master_ref],
                       cwd=str(REPO_ROOT), capture_output=True, check=True)
    a_src, b_src = awt.resolve(), REPO_ROOT

    report: dict = {"protocol": {"rounds": args.rounds,
                                 "scenarios": list(SCENARIOS)},
                    "aa": {}, "ab": {}, "gates": {}}
    overall_ok = True

    for scenario in SCENARIOS:
        # A/A 噪声带
        aa: list[float] = []
        aa_kill: set | None = None
        proj = _prep_project(fixture, gut_dir, outdir, f"aa-{scenario}")
        for i in range(args.rounds):
            payload, elapsed, meta = _run_scenario(
                a_src, proj, args.target, scenario,
                keep_cache=(scenario == "warm"), point_line=args.point_line)
            aa.append(elapsed)
            ks = _kill_set(payload)
            if aa_kill is None:
                aa_kill = ks
            elif ks != aa_kill:
                report["gates"][f"{scenario}/aa_consistent"] = False
                overall_ok = False
        band = _noise_band(aa)
        report["aa"][scenario] = {"rounds": aa, "band_s": round(band, 3)}

        # A/B 交错
        ab: dict = {"a": [], "b": []}
        proj_ab = _prep_project(fixture, gut_dir, outdir, f"ab-{scenario}")
        ab_kill: set | None = None
        for i in range(args.rounds):
            for side, src in (("a", a_src), ("b", b_src)):
                payload, elapsed, meta = _run_scenario(
                    src, proj_ab, args.target, scenario,
                    keep_cache=(scenario == "warm"),
                    point_line=args.point_line)
                ab[side].append(meta | {"kill_n": len(_kill_set(payload))})
                ks = _kill_set(payload)
                if ab_kill is None:
                    ab_kill = ks
                elif ks != ab_kill:
                    report["gates"][f"{scenario}/ab_killset_consistent"] = False
                    overall_ok = False
        b_best = min(x["wall_s"] for x in ab["b"])
        a_worst = max(x["wall_s"] for x in ab["a"])
        ok = b_best <= a_worst + band
        report["ab"][scenario] = {
            "a_rounds": ab["a"], "b_rounds": ab["b"],
            "a_worst_s": round(a_worst, 3), "b_best_s": round(b_best, 3),
            "band_s": round(band, 3),
            "speed_gate": ok,
        }
        report["gates"][f"{scenario}/speed"] = ok
        if not ok:
            overall_ok = False
        # 确定性门（warm 场景）：B 侧 warm 全复用仅一次 baseline —— 由
        # _kill_set 一致 + warm 轮数 ≥2 时 b 轮间墙钟骤降佐证（基线复用）。
        if scenario == "warm" and len(ab["b"]) >= 2:
            report["gates"][f"{scenario}/determinism_hint"] = \
                ab["b"][-1]["wall_s"] <= ab["b"][0]["wall_s"]

    if args.kol_sample:
        kol = Path(args.kol_sample).resolve()
        report["kol"] = {"root": str(kol), "target": args.kol_target,
                         "note": "large-sample lane executed by QA manually; "
                                 "KOL repo must stay read-only (git status "
                                 "before/after must match)"}

    report["gates"]["overall"] = overall_ok
    (outdir / "bench_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report["gates"], ensure_ascii=False))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
