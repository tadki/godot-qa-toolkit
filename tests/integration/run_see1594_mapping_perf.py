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
    from godot_qa_toolkit.measurement.increment import run_attribution_increment
    from godot_qa_toolkit.measurement.recipe import build_recipe
    from godot_qa_toolkit.mutation.truth_select import resolve_selection
    from godot_qa_toolkit.coverage.mapping import project_file_coverage

    samples = _pick_samples(kol_root)
    targets = [str(p) for p in samples]
    recipe = _recipe_or_fail(kol_root)

    # --- 段 1：full build（映射全量构建，绝对秒数）---
    t1 = time.monotonic()
    report = run_measurement(str(kol_root), targets, recipe,
                             timeout_s=args.timeout, test_attribution=True,
                             attribution_out=str(kol_root / ".qa-cache"
                                                 / "attribution-map.json"))
    print(f"[harness] full build report ok={report.get('ok')} "
          f"keys={sorted(report.keys())}", file=sys.stderr)
    print(f"[harness] report[:800]="
          f"{json.dumps(report, ensure_ascii=False, default=str)[:800]}",
          file=sys.stderr)
    full_build_s = round(time.monotonic() - t1, 3)
    # §SPEC-026 判据锚：全量构建成功与否以「映射产物有效性」为准
    # （schema/arms_state/probes），不以批式 report.ok 为准——KOL 副本
    # headless 下 acceptance 臂失败会使 ok=False 但映射仍有效。
    if report.get("failures") is not None and \
            not isinstance(report.get("failures"), dict):
        pass  # list 形态留给下面的 map 校验统一判定

    map_file = kol_root / ".qa-cache" / "attribution-map.json"
    if not map_file.is_file():
        return _fail("full build did not produce attribution map "
                     f"at {map_file}")
    doc = json.loads(map_file.read_text(encoding="utf-8"))
    if doc.get("schema_version") != "test-attribution/6":
        return _fail(f"map schema mismatch: {doc.get('schema_version')!r}")
    if not (doc.get("arms_state") or {}).get("gut_full"):
        return _fail("map incomplete: arms_state.gut_full missing")

    # --- 段 2：incremental（增量刷新，绝对秒数；只做一次）---
    touch = samples[0]
    t2 = time.monotonic()
    inc = run_attribution_increment(
        str(kol_root), targets, recipe, str(map_file),
        [touch.relative_to(kol_root).as_posix()],
        str(kol_root / ".qa-cache" / "attribution-map.increment.json"),
        timeout_s=args.timeout)
    incremental_s = round(time.monotonic() - t2, 3)
    if inc.get("out_path") and Path(inc["out_path"]).is_file():
        Path(inc["out_path"]).replace(map_file)
    doc = json.loads(map_file.read_text(encoding="utf-8"))
    if doc.get("schema_version") != "test-attribution/6":
        return _fail("incremental produced stale/invalid map (schema mismatch)"
                     " — refusing to project (fail-loud)")

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
        # parity：新投影 covered set vs 映射行事实（唯一真源）逐文件对比
        arm = (doc.get("arms") or {}).get("gut_full") or {}
        truth: set[int] = set()
        for ident, by_rel in (arm.get("test_to_lines") or {}).items():
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
