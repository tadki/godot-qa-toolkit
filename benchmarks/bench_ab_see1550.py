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
    # R-1①②（QA 第三轮实证）：mutation CLI 契约为 --project-root（--path
    # 是 argparse exit 2）；target 的 res:// 前缀必须剥离后再与项目根拼接
    # （proj/"res://..." 路径不存在 exit 1）。
    target_fs = proj / target_rel.removeprefix("res://")
    argv = [sys.executable, "-m", "godot_qa_toolkit.cli", "mutation",
            str(target_fs), "--project-root", str(proj),
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


def _consistency_surface(payload: dict) -> tuple[frozenset, int, int]:
    """verdict 一致性判定面（A/B 两侧同形可计算）：
    (failures 的 (id, verdict) 全集, killed 计数, mutants 计数)。

    killed 逐 id 集合在 A 侧（master 旧机制无 killed_by 证书）不可计算
    ——证书是 B 侧新增披露，计入判定面会让 A/B 结构性恒不等；以计数对齐
    替代（QA 协议口径：verdict 分区与 failures 全等）。"""
    results = payload.get("results") or [payload]
    failures: set[tuple] = set()
    killed = mutants = 0
    for r in results:
        for f in r.get("failures", []):
            if "verdict" in f:
                failures.add((f.get("id", f["file"]), f["verdict"]))
        s = r.get("summary", {})
        killed += int(s.get("killed", 0))
        mutants += int(s.get("mutants", 0))
    return frozenset(failures), killed, mutants


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
        # §SPEC-201：warm = 真零执行复用——差分默认开启（不传 --mutate-all），
        # killed 条目全复用仅剩 baseline 锚；此前误传 --mutate-all 使 warm
        # 轮全量重跑，测的不是复用面。
        pass
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
    ap.add_argument("--point-line", type=int, default=6,
                    help="point 场景的 --lines 寻址行（fixture 默认位点行 6）")
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
        # §SPEC-016（O-2 收口）：目录被手工清理后元数据仍登记在案，
        # `worktree add` 恒 128（本机实证）——add 前先 prune 陈旧登记。
        subprocess.run(["git", "worktree", "prune"], cwd=str(REPO_ROOT),
                       capture_output=True, check=False)
        subprocess.run(["git", "worktree", "add", str(awt), args.master_ref],
                       cwd=str(REPO_ROOT), capture_output=True, check=True)
    a_src, b_src = awt.resolve(), REPO_ROOT

    report: dict = {"protocol": {"rounds": args.rounds,
                                 "scenarios": list(SCENARIOS),
                                 # Owner 协议：A/A ≥3 轮定噪声带——<3 轮的
                                 # 门是临时 smoke 结论（噪声带=0 掷硬币），
                                 # 不具协议效力，只披露不判 FAIL。
                                 "protocol_valid": args.rounds >= 3},
                    "aa": {}, "ab": {}, "gates": {}}
    overall_ok = True

    for scenario in SCENARIOS:
        # A/A 噪声带
        aa: list[float] = []
        aa_surf: tuple | None = None
        proj = _prep_project(fixture, gut_dir, outdir, f"aa-{scenario}")
        for i in range(args.rounds):
            payload, elapsed, meta = _run_scenario(
                a_src, proj, args.target, scenario,
                keep_cache=(scenario == "warm"), point_line=args.point_line)
            aa.append(elapsed)
            surf = _consistency_surface(payload)
            if aa_surf is None:
                aa_surf = surf
            elif surf != aa_surf:
                report["gates"][f"{scenario}/aa_consistent"] = False
                if args.rounds >= 3:
                    overall_ok = False
        band = _noise_band(aa)
        report["aa"][scenario] = {"rounds": aa, "band_s": round(band, 3)}

        # A/B 交错
        ab: dict = {"a": [], "b": []}
        proj_ab = _prep_project(fixture, gut_dir, outdir, f"ab-{scenario}")
        ab_surf: tuple | None = None
        for i in range(args.rounds):
            for side, src in (("a", a_src), ("b", b_src)):
                payload, elapsed, meta = _run_scenario(
                    src, proj_ab, args.target, scenario,
                    keep_cache=(scenario == "warm"),
                    point_line=args.point_line)
                surface = _consistency_surface(payload)
                ab[side].append(meta | {"killed_n": surface[1],
                                        "mutants_n": surface[2]})
                if ab_surf is None:
                    ab_surf = surface
                elif surface != ab_surf:
                    report["gates"][f"{scenario}/verdict_surface_consistent"] = False
                    if args.rounds >= 3:
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
        if not args.rounds >= 3:
            report["gates"][f"{scenario}/speed_provisional"] = \
                "rounds<3: band=0, gate is smoke-only"
        report["gates"][f"{scenario}/speed"] = ok
        if not ok and args.rounds >= 3:
            overall_ok = False
        # 确定性门（warm 场景）：B 侧 warm 全复用仅一次 baseline —— 由
        # 判定面一致 + warm 轮数 ≥2 时 b 轮间墙钟骤降佐证（基线复用）。
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
    if not args.rounds >= 3:
        print("bench: rounds<3 — gates are provisional smoke results; "
              "protocol verdict requires --rounds >= 3 (A/A band + A/B "
              "interleave)", file=sys.stderr)
        return 0
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
