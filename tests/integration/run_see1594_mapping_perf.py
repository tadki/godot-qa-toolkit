#!/usr/bin/env python3
"""SEE-1594 §SPEC-026：行级映射路径真实性能与 parity 验收 harness。

签名（§SPEC-026 验收口径）：
    python tests/integration/run_see1594_mapping_perf.py \
        --kol-root <KOL副本> --out <json>

测量三段绝对秒数（真实测量，非估算）：
- full_build_s   映射全量构建（measure run --test-attribution 等价调用）
- incremental_s  增量刷新（改一个 target 后 run_attribution_increment 等价调用）
- projection_s   行级映射投影（per-file 与聚合）

parity 节：同一测试面/同一 target 集合下，投影 covered line set 与映射
行事实（唯一真源）的逐文件对比、相同率、percent parity、错误语义对比
（stale/incomplete 一律 fail-loud，绝不回退旧 direct 计时）。

环境锚：head commit、KOL commit、Godot 版本、python 版本、主机、样本
文件与测试面、命令原文。

fail-loud 契约：KOL 副本不可读 / 找不到样本 / 映射不完整或不新鲜 /
投影拒绝 → 具名原因 + 非零退出；绝不把「找不到样本」当 PASS。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import time
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[2] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

SAMPLE_LIMIT = 3          # 投影抽样文件数（有界 smoke，非全量）
MIN_TARGETS_FOR_MAP = 1   # 全量构建至少需要的 target 数
EXIT_USAGE = 2
EXIT_FAIL = 1


def _fail(reason: str) -> "dict | None":
    """具名失败 → 输出 JSON 并返回退出码；绝不静默 PASS。"""
    print(json.dumps({"tool": "see1594.mapping_perf", "ok": False,
                      "failures": [{"reason": reason}]},
                     ensure_ascii=False, indent=2))
    return EXIT_FAIL


def _fail_json(reason: str, out_path: Path, extra: dict) -> int:
    """具名失败 + 诊断证据落盘（timings/parity 随失败披露，供复核）。"""
    payload = {"tool": "see1594.mapping_perf", "ok": False,
               "failures": [{"reason": reason}], **extra}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(json.dumps({"tool": "see1594.mapping_perf", "ok": False,
                      "failures": [{"reason": reason}]},
                     ensure_ascii=False, indent=2))
    return EXIT_FAIL


def _head_commit() -> str:
    try:
        repo = Path(__file__).resolve().parents[2]
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                             capture_output=True, text=True, timeout=30)
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "unknown"


def _kol_commit(kol_root: Path) -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(kol_root),
                             capture_output=True, text=True, timeout=30)
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "unknown"


def _godot_version(kol_root: Path) -> str:
    import os
    bin_ = os.environ.get("GQT_GODOT_BIN") or "godot"
    try:
        out = subprocess.run([bin_, "--version"], cwd=str(kol_root),
                             capture_output=True, text=True, timeout=60)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def _pick_samples(kol_root: Path) -> list[Path]:
    """样本选择：utils/ 与 systems/ 的 .gd 生产文件，按行数降序取前 N。

    找不到样本 → RuntimeError（fail-loud，不得当 PASS）。
    """
    cands: list[tuple[int, Path]] = []
    for sub in ("utils", "systems", "autoload"):
        d = kol_root / sub
        if not d.is_dir():
            continue
        for gd in sorted(d.rglob("*.gd")):
            try:
                n = len(gd.read_text(encoding="utf-8").splitlines())
            except OSError:
                continue
            if n >= 5:
                cands.append((n, gd))
    if not cands:
        raise RuntimeError(
            "sample selection failed: no .gd production files (>=5 lines) "
            f"under {kol_root}/[utils|systems|autoload] — cannot PASS")
    cands.sort(key=lambda kv: (-kv[0], str(kv[1])))
    return [p for _, p in cands[:SAMPLE_LIMIT]]


def _recipe_or_fail(kol_root: Path) -> dict:
    from godot_qa_toolkit.measurement.recipe import build_recipe
    recipe, error = build_recipe(kol_root)
    if error is not None:
        raise RuntimeError(f"recipe unavailable: {error}")
    return recipe




def _run_uninstr_gut(kol_root: Path, timeout: int,
                     select: str | None = None) -> tuple[int, str]:
    """无插桩 GUT 运行（全 face 或 -gselect 指定脚本）。"""
    import os
    godot_bin = os.environ.get("GQT_GODOT_BIN") or "godot"
    cmd = [godot_bin, "--headless", "--path", str(kol_root),
           "-s", "res://addons/gut/gut_cmdln.gd"]
    if select:
        cmd += [f"-gselect={select}"]
    else:
        cmd += ["-gdir=res://tests", "-ginclude_subdirs"]
    cmd += ["-gexit"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout + r.stderr


def _pairs_from_output(out: str) -> set[tuple[str, str]]:
    """GUT 输出 → (script, test) 身份对——复用测量侧同一解析器
    （§SPEC-063c/064a 混合形态；与臂观测逐对可比，§SPEC-064b）。"""
    from godot_qa_toolkit.measurement.orchestrator import _parse_failing_pairs
    return _parse_failing_pairs(out)


def _acquire_baseline(kol_root: Path, timeout: int,
                      face: str = "res://tests",
                      select: str | None = None,
                      ) -> "tuple[set, dict | str]":
    """现场获取基线（无插桩 GUT 一次）：返回 (pairs, meta) 或 (set(), 错误串)。

    fail-loud：启动失败 / 无 GUT 运行流（不得按空基线静默继续）。
    基线仅当次现场获取，不跨 run 缓存；身份对歧义由 (script, test)
    对身份原生消解（§SPEC-064b：未归因仅匹配未归因，同名跨脚本不
    互相吸收）。"""
    import hashlib
    from godot_qa_toolkit.mutation.gutrun import _looks_like_godot_launch_failure
    t0 = time.monotonic()
    rc, out = _run_uninstr_gut(kol_root, timeout, select=select)
    if _looks_like_godot_launch_failure(out) or (
            "Run Summary" not in out and "[Failed]" not in out
            and "[Passed]" not in out):
        return set(), ("baseline acquisition failed: uninstrumented GUT "
                       f"run has no run stream (rc={rc}) — refusing "
                       f"empty baseline (fail-loud)")
    pairs = _pairs_from_output(out)
    meta = {"method": "uninstrumented_run", "face": face,
            "pairs": sorted([list(p) for p in pairs]), "count": len(pairs),
            "sha256": hashlib.sha256(json.dumps(
                sorted([list(p) for p in pairs]),
                ensure_ascii=False).encode("utf-8")).hexdigest(),
            "wall_s": round(time.monotonic() - t0, 3)}
    return pairs, meta



def main(argv: list[str] | None = None) -> int:
    t0 = time.monotonic()
    parser = argparse.ArgumentParser(
        description="SEE-1594 §SPEC-026 mapping perf/parity harness")
    parser.add_argument("--kol-root", required=True,
                        help="KOL 只读主检出之外的独立副本根目录")
    parser.add_argument("--out", required=True, help="JSON 证据输出路径")
    parser.add_argument("--timeout", type=int, default=600,
                        help="全量构建/增量刷新的 GUT 超时上限")
    args = parser.parse_args(argv)

    kol_root = Path(args.kol_root).resolve()
    out = Path(args.out)
    cmd = f"python tests/integration/run_see1594_mapping_perf.py --kol-root {kol_root} --out {args.out}"
    env_anchor = {
        "head_commit": _head_commit(),
        "kol_commit": _kol_commit(kol_root),
        "godot_version": _godot_version(kol_root),
        "python_version": sys.version.split()[0],
        "host": platform.node(),
        "command": cmd,
    }

    # --- 副本可读性 / 关键结构 fail-loud ---
    if not (kol_root / "project.godot").is_file():
        return _fail(f"kol root not a godot project (no project.godot): "
                     f"{kol_root}")
    if not (kol_root / "addons" / "gut" / "gut_cmdln.gd").is_file():
        return _fail(f"kol copy missing GUT runner "
                     f"(addons/gut/gut_cmdln.gd): {kol_root}")

    # 新鲜副本缺 `.godot` 导入缓存 → 插桩 GUT 无法加载（probes=0）。
    # 先跑一次引擎导入（与 CLI _bootstrap_import_or_contract 同语义）。
    import os
    godot_bin = os.environ.get("GQT_GODOT_BIN") or "godot"
    t_imp = time.monotonic()
    imp = subprocess.run([godot_bin, "--headless", "--path", str(kol_root),
                          "--import"], capture_output=True, text=True,
                         timeout=args.timeout)
    import_s = round(time.monotonic() - t_imp, 3)
    print(f"[harness] bootstrap import rc={imp.returncode} s={import_s}",
          file=sys.stderr)

    from godot_qa_toolkit.measurement.orchestrator import run_measurement
    import godot_qa_toolkit
    # 证据有效性守卫：harness 必须测量本 worktree 的实现——多 worktree
    # 机器上 stale editable egg-link 会劫持 import（README 约定
    # PYTHONPATH=src），错树证据比无证据更糟 → fail-loud。
    _pkg_root = Path(godot_qa_toolkit.__file__).resolve().parent.parent
    if _pkg_root != (Path(__file__).resolve().parents[2] / "src"):
        return _fail(f"measuring the wrong tree: imported godot_qa_toolkit "
                     f"from {_pkg_root} — run with PYTHONPATH="
                     f"{Path(__file__).resolve().parents[2] / 'src'} "
                     f"(README worktree convention)")
    env_anchor["gqt_package"] = str(_pkg_root)

    from godot_qa_toolkit.measurement.increment import run_attribution_increment
    from godot_qa_toolkit.measurement.recipe import build_recipe
    from godot_qa_toolkit.mutation.truth_select import resolve_selection
    from godot_qa_toolkit.coverage.mapping import project_file_coverage

    samples = _pick_samples(kol_root)
    targets = [str(p) for p in samples]
    recipe = _recipe_or_fail(kol_root)

    # --- 段 0：基线现场获取（同 tests 面无插桩 GUT 一次；Owner 方案 A）---
    baseline_pairs, baseline_meta = _acquire_baseline(
        kol_root, args.timeout, face="res://tests")
    if isinstance(baseline_meta, str):
        return _fail(baseline_meta)
    print(f"[harness] baseline: {baseline_meta['count']} known failing "
          f"pairs acquired uninstrumented ({baseline_meta['wall_s']}s) — "
          f"only NEW failures invalidate the arm", file=sys.stderr)

    # --- 段 1：full build（映射全量构建；baseline 内失败不判臂无效）---
    t1 = time.monotonic()
    report = run_measurement(str(kol_root), targets, recipe,
                             timeout_s=args.timeout, test_attribution=True,
                             baseline_failures=baseline_pairs,
                             attribution_out=str(kol_root / ".qa-cache"
                                                 / "attribution-map.json"))
    print(f"[harness] full build report ok={report.get('ok')} "
          f"keys={sorted(report.keys())}", file=sys.stderr)
    if not report.get("ok"):
        print(f"[harness] report ok=false diagnostics: "
              f"failures={json.dumps(report.get('failures'), default=str)[:600]} "
              f"union={json.dumps(report.get('union'), default=str)[:300]} "
              f"unions={json.dumps(report.get('unions'), default=str)[:300]}",
              file=sys.stderr)
    print(f"[harness] report[:800]="
          f"{json.dumps(report, ensure_ascii=False, default=str)[:800]}",
          file=sys.stderr)
    full_build_s = round(time.monotonic() - t1, 3)

    map_file = kol_root / ".qa-cache" / "attribution-map.json"
    if not map_file.is_file():
        return _fail("full build did not produce attribution map "
                     f"at {map_file}")
    doc = json.loads(map_file.read_text(encoding="utf-8"))

    # S6.2：映射产物记录基线元数据（新键只加不改；消费者不读不改语义）
    doc["baseline_failures_meta"] = {
        "method": baseline_meta["method"],
        "face": baseline_meta.get("face"),
        "count": baseline_meta["count"],
        "sha256": baseline_meta["sha256"],
        "wall_s": baseline_meta["wall_s"],
        "gating": "new failures only (baseline-absorbed failures keep the "
                  "arm valid)",
    }
    map_file.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    if doc.get("schema_version") != "test-attribution/6":
        return _fail(f"map schema mismatch: {doc.get('schema_version')!r}")
    gut_state = (doc.get("arms_state") or {}).get("gut_full") or {}
    if not (doc.get("arms_state") or {}).get("gut_full"):
        return _fail("map incomplete: arms_state.gut_full missing")
    if int(gut_state.get("probes_fired") or 0) <= 0:
        return _fail("map not fresh: probes_fired == 0 (GUT never executed)")

    # --- 段 2：incremental（增量刷新，绝对秒数；只做一次）---
    # S6.2：受影响面现场取基线（touch 后、刷新前重取，不得沿用陈旧基线）
    touch = samples[0]
    touch_rel = touch.relative_to(kol_root).as_posix()
    inc_pairs, inc_meta = _acquire_baseline(kol_root, args.timeout)
    if isinstance(inc_meta, str):
        return _fail(inc_meta)
    t2 = time.monotonic()
    inc = run_attribution_increment(
        str(kol_root), targets, recipe, str(map_file),
        [touch_rel],
        str(kol_root / ".qa-cache" / "attribution-map.increment.json"),
        timeout_s=args.timeout)
    incremental_s = round(time.monotonic() - t2, 3)
    if inc.get("out_path") and Path(inc["out_path"]).is_file():
        Path(inc["out_path"]).replace(map_file)
    doc = json.loads(map_file.read_text(encoding="utf-8"))

    # S6.2：增量后受影响面无插桩复跑——失败集必须 ⊆ 增量前现场基线
    # （不得有新增失败被 valid 状态掩盖）
    affected = sorted({str(ident).split("::")[0] for ident, by_rel in
                       ((doc.get("arms") or {}).get("gut_full")
                        or {}).get("test_to_lines", {}).items()
                       if by_rel.get(touch_rel)})
    if affected:
        # test_to_lines 身份 = <GUT 裸名>.gd::<test>（A12）；-gselect 按
        # 脚本名匹配（实测锚定：裸名精确命中唯一脚本）
        sel = ",".join(affected)
        rc_v, out_v = _run_uninstr_gut(kol_root, args.timeout, select=sel)
        post_pairs = _pairs_from_output(out_v)
        fresh_pairs = {tuple(p) for p in inc_meta["pairs"]}
        new_after = post_pairs - fresh_pairs
        if new_after:
            return _fail_json(
                f"post-increment new failures outside baseline: "
                f"{sorted(new_after)}", out,
                {"env": env_anchor, "incremental_s": incremental_s,
                 "projection_s": projection})
    # 基线元数据刷新标注（增量后现场基线）
    if "baseline_failures_meta" in doc:
        doc["baseline_failures_meta"]["refresh"] = {
            "face": "affected", "count": inc_meta["count"],
            "sha256": inc_meta["sha256"]}
        map_file.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                            encoding="utf-8")



    # --- 段 3：projection（行级映射投影；per-file + 聚合）---
    projection = {"files": [], "aggregate_s": 0.0}
    parity_rows: list[dict] = []
    t3 = time.monotonic()
    for sample in samples:
        rel = sample.relative_to(kol_root).as_posix()
        tf = time.monotonic()
        sel = resolve_selection(str(kol_root), str(sample), strict=False)
        if sel.get("source") != "runtime_mapping":
            return _fail(f"projection parity: selection for {rel} is "
                         f"{sel.get('source')!r} (expected runtime_mapping)")
        pf = project_file_coverage(str(kol_root), str(sample))
        per_s = round(time.monotonic() - tf, 3)
        projection["aggregate_s"] = round(projection["aggregate_s"] + per_s, 3)
        projection["files"].append({"file": rel, "s": per_s,
                                    "covered_lines": sorted(pf["covered_lines"]),
                                    "coverage_percent": pf["coverage_percent"]})
        # parity：新投影 covered set vs 映射行事实（唯一真源）逐文件对比。
        # 真值 = 映射携带的全部行事实：test 级身份 + script 级事实
        # （script_to_lines 是映射的一等行事实，投影消费侧同样并集）。
        arm = (doc.get("arms") or {}).get("gut_full") or {}
        truth: set[int] = set()
        for ident, by_rel in (arm.get("test_to_lines") or {}).items():
            truth |= set((by_rel.get(rel) or ()))
        for _sp, by_rel in (arm.get("script_to_lines") or {}).items():
            truth |= set((by_rel.get(rel) or ()))
        inst = {int(x) for x in
                ((doc.get("instrumentable_lines") or {}).get(rel) or ())}
        truth &= inst
        got = set(pf["covered_lines"])
        parity_rows.append({
            "file": rel,
            "projection_covered": sorted(got),
            "truth_covered": sorted(truth),
            "match": got == truth,
            "percent_projection": pf["coverage_percent"],
            "percent_truth": (round(100.0 * len(truth & inst) / len(inst), 2)
                              if inst else 100.0),
        })
    projection["aggregate_s"] = round(time.monotonic() - t3, 3)
    matched = sum(1 for r in parity_rows if r["match"])
    parity = {
        "rows": parity_rows,
        "files": len(parity_rows),
        "matched": matched,
        "match_rate": round(matched / len(parity_rows), 4)
        if parity_rows else 0.0,
        "percent_parity": all(abs(r["percent_projection"]
                                  - r["percent_truth"]) < 0.01
                              for r in parity_rows),
        "error_semantics": ("fail-loud on stale/incomplete/unreadable copy; "
                            "no legacy direct-timing fallback"),
    }

    # §SPEC-026 fail-loud：arm 不 valid（如 run_error）→ 不产出 perf/parity
    # 证据；诊断数据（timings/parity 行）随失败 JSON 落盘供复核。
    gut_state = (doc.get("arms_state") or {}).get("gut_full") or {}
    if gut_state.get("state") != "valid":
        return _fail_json(
            f"map arm invalid: state={gut_state.get('state')!r} "
            f"invalid_reason={gut_state.get('invalid_reason')!r} "
            "(§SPEC-026 fail-loud: invalid map cannot back perf/parity "
            "evidence)", out,
            {"env": env_anchor,
             "samples": [str(p.relative_to(kol_root)) for p in samples],
             "full_build_s": full_build_s,
             "incremental_s": incremental_s,
             "projection_s": projection,
             "parity": parity})

    evidence = {
        "tool": "see1594.mapping_perf",
        "ok": True,
        "env": env_anchor,
        "samples": [str(p.relative_to(kol_root)) for p in samples],
        "baseline_failures": doc.get("baseline_failures_meta") or {},
        "full_build_s": full_build_s,
        "incremental_s": incremental_s,
        "projection_s": {"per_file": projection["files"],
                         "aggregate_s": projection["aggregate_s"]},
        "parity": parity,
        "wall_s": round(time.monotonic() - t0, 3),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(json.dumps({"tool": "see1594.mapping_perf", "ok": True,
                      "summary": {
                          "full_build_s": full_build_s,
                          "incremental_s": incremental_s,
                          "projection_aggregate_s": projection["aggregate_s"],
                          "parity": f"{matched}/{len(parity_rows)}",
                      }}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
