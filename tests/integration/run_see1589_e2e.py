#!/usr/bin/env python3
"""SEE-1589 §SPEC-012 —— 真实 KOL 隔离副本 E2E（A 全缺陷面 + B 机制面）。

判据命令：
    python tests/integration/run_see1589_e2e.py \
        --kol-root <KOL副本> --target utils/csv_tmap_parser.gd

行为：
1. 在副本内用 `measure suite` + `measure run --test-attribution` 生成一份
   新鲜归属映射（同时作为 M1「映射可用、union 未认证」的真实证据）；
2. 以该映射驱动 A 面复验：full-face 建议、单文件 --jobs 1 flat 信封、
   selection_mode 全档 + 并发上限、零引用键；
3. 以真实 mutation 轮复验 B 面：worker 路由（生效或具名不可达）、
   farm 复用/重建披露、prewarm fresh_skip、stage_reconciliation 对账；
4. 输出机器可判定 PASS/FAIL 与逐项命令/退出码/关键 JSON；任何未跑到、
   非 JSON 输出或键缺失都判 FAIL（不得把未跑到当成通过），退出码 0/1。

KOL 只读：脚本只读 `--kol-root`，所有写入发生在副本自身 `.qa-cache`
（gqt 既有契约）；副本创建/清理由调用方负责（见 issue 协议）。
环境装配复用 `run_see1589_benchmark.py`（同一 Godot/PYTHONPATH 口径）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

from run_see1589_benchmark import (DEFAULT_GODOT, REPO, SRC, _env,
                                  _map_line_coverage, _sites)

API = [sys.executable, "-m", "godot_qa_toolkit.cli"]


def _run(args: list[str], *, env: dict, timeout: int = 300) -> dict:
    """执行 gqt CLI 并解析 JSON；非 JSON = 工具崩溃（不可信）→ FAIL。"""
    cmd = API + args
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=str(REPO), env=env,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        # 超时是判定失败的一种（fail-loud），不得让 harness 自身崩溃。
        return {"cmd": "python -m godot_qa_toolkit.cli " + " ".join(args),
                "rc": None, "wall_s": round(time.monotonic() - t0, 3),
                "doc": None, "parse_error": f"timeout after {timeout}s",
                "stderr_tail": (e.stderr or "")[-400:] if isinstance(
                    e.stderr, str) else []}
    wall = round(time.monotonic() - t0, 3)
    if os.environ.get("GQT_E2E_TRACE"):
        print(f"[e2e] {wall:7.1f}s rc={proc.returncode} "
              f"{' '.join(args[:6])}", file=sys.stderr, flush=True)
    try:
        doc = json.loads(proc.stdout)
        parse_error = None
    except json.JSONDecodeError as e:
        doc, parse_error = None, str(e)
    return {
        "cmd": "python -m godot_qa_toolkit.cli " + " ".join(args),
        "rc": proc.returncode,
        "wall_s": wall,
        "doc": doc,
        "parse_error": parse_error,
        "stderr_tail": proc.stderr.strip().splitlines()[-6:],
    }


def _summary(doc: dict | None) -> dict:
    if not isinstance(doc, dict):
        return {}
    if isinstance(doc.get("summary"), dict) and "results" not in doc:
        return doc["summary"]
    results = doc.get("results")
    if isinstance(results, list) and results:
        return results[0].get("summary") or {}
    return doc.get("summary") or {}


class Checks:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, cid: str, ok: bool, detail: str, evidence=None) -> None:
        self.items.append({"id": cid, "status": "PASS" if ok else "FAIL",
                           "detail": detail, "evidence": evidence or {}})

    def fail_from_exc(self, cid: str, exc: Exception) -> None:
        self.add(cid, False, f"{type(exc).__name__}: {exc}",
                 {"traceback": traceback.format_exc()[-1200:]})

    @property
    def ok(self) -> bool:
        return all(i["status"] == "PASS" for i in self.items)


def _prune_vendored_js(kol: Path) -> dict:
    """删副本内的 vendored JS 依赖树（仅副本；KOL 源仓零改动）。

    `addons/*/server/node_modules` 与 Godot mutation 无关，却让测量 shadow
    每分片复制 ~160MB、prewarm 摘要整树哈希，fresh 副本 E2E 会超过判据
    超时。删除事实进报告（environment_normalization），便于复核。
    """
    pruned = bytes_freed = 0
    for root, dirs, _files in os.walk(kol, topdown=True):
        for name in list(dirs):
            if name != "node_modules":
                continue
            path = Path(root) / name
            for r, _d, fs in os.walk(path):
                for f in fs:
                    try:
                        bytes_freed += (Path(r) / f).stat().st_size
                    except OSError:
                        pass
            shutil.rmtree(path, ignore_errors=True)
            dirs.remove(name)
            pruned += 1
    return {"node_modules_pruned": pruned, "bytes_freed": bytes_freed}


def _normalize_symlinks(kol: Path) -> dict:
    """把副本内 symlink 解引用/清理为常规文件（仅副本，KOL 源仓零改动）。

    gqt 的 proof 扫描与测量 shadow 都以 symlink 为歧义（fail-closed）；
    副本里 vendored JS 依赖树（addons/*/server/node_modules）常见 symlink，
    会让 prewarm 永不发证。这里按测量 shadow 的同款策略（解引用/删悬空）
    归一化，使 fresh_skip 可在同一真实目标上被验证；归一化事实进报告。
    """
    deref = removed = 0
    for root, dirs, files in os.walk(kol, topdown=True,
                                     followlinks=False):
        for name in list(dirs) + list(files):
            path = Path(root) / name
            if not path.is_symlink():
                continue
            real = path.resolve()
            try:
                if not real.exists():
                    path.unlink()
                    removed += 1
                    continue
                target = Path(real)
                path.unlink()
                if target.is_dir():
                    shutil.copytree(target, path)
                else:
                    shutil.copy2(target, path)
                deref += 1
            except OSError as e:
                raise RuntimeError(
                    f"symlink normalization failed at {path}: {e}") from e
    return {"symlinks_dereferenced": deref,
            "dangling_links_removed": removed}


def _ensure_map(kol: Path, target: str, godot_bin: str | None,
                checks: Checks, *, secondary: bool = False
                ) -> tuple[str, dict]:
    """生成/复用新鲜映射；返回 (map_path, measure_run 结果)。"""
    env = _env(godot_bin, None)
    face = target.split("/", 1)[0]
    cache = kol / ".qa-cache"
    cache.mkdir(parents=True, exist_ok=True)
    recipe = cache / "see1589-e2e-recipe.json"
    report = cache / "see1589-e2e-measure.json"
    map_path = cache / ("attribution-map.json" if not secondary else
                        f"attribution-map-{face}.json")
    suite = _run(["measure", "suite", "--project-root", str(kol),
                  "--out", str(recipe)], env=env)
    if suite["rc"] != 0 or suite["parse_error"]:
        cid = "M1-map-suite" + ("-" + face if secondary else "")
        checks.add(cid, False, f"measure suite failed rc={suite['rc']}",
                   {"suite": suite})
        return str(map_path), {}
    measure = _run([
        "measure", "run", str(kol / face), "--project-root", str(kol),
        "--recipe", str(recipe), "--out", str(report),
        "--test-attribution", "--test-attribution-out", str(map_path)],
        env=env, timeout=900)
    return str(map_path), measure


def _map_facts(map_path: str, target: str) -> dict:
    doc = json.loads(Path(map_path).read_text(encoding="utf-8"))
    gut = ((doc.get("arms") or {}).get("gut_full") or {})
    line_to_tests = gut.get("line_to_tests") or {}
    tests_face = doc.get("tests_face") or {}
    lines = sorted(int(k) for k in (line_to_tests.get(target) or {}))
    dirs = []
    for test_id in (gut.get("file_to_tests") or {}).get(target, []):
        name = test_id.split("::", 1)[0]
        for rel in tests_face:
            if rel.endswith("/" + name):
                res_dir = "res://" + rel.rsplit("/", 1)[0] + "/"
                if res_dir not in dirs:
                    dirs.append(res_dir)
    return {"lines": lines, "test_dirs": dirs,
            "target_files": doc.get("target_files") or [],
            "line_to_tests": line_to_tests}


def _manifest_facts(kol: Path, rel: str) -> tuple[str | None, int | None]:
    """与 CLI 同源的 stable id / 首个位点行（CLI 会把目标锚成绝对路径）。"""
    from godot_qa_toolkit.mutation.ops import _collect_mutations
    from godot_qa_toolkit.mutation.scopemanifest import build_manifest

    anchored = str((kol / rel).resolve())
    src = (kol / rel).read_text(encoding="utf-8")
    raw = _collect_mutations(src, anchored)
    _h, _s, mutants, _sh = build_manifest(src, anchored, raw)
    if not mutants:
        return None, None
    return mutants[0].stable_id, mutants[0].line


def _zero_ref_target(kol: Path, facts: dict) -> str | None:
    for rel in facts["target_files"]:
        if (kol / rel).is_file() and not (facts["line_to_tests"].get(rel)):
            return rel
    return None


def _worker_target(kol: Path, facts: dict) -> str | None:
    from godot_qa_toolkit.mutation.channel_router import route_target
    for rel in facts["target_files"]:
        if not (kol / rel).is_file() or _sites(kol, rel) <= 0:
            continue
        if route_target(str(kol), "res://" + rel).channel == "worker":
            return rel
    return None


def _pick_eligible(kol: Path, godot_bin: str | None,
                   checks: Checks) -> tuple[str | None, dict]:
    """SPEC-012 固定命令只给一个面：该面无 eligible 目标时，按面优先序
    （systems/acceptance/utils）另选真实 eligible 目标，并为其面补建映射。

    只接受映射确有该文件行事实的候选——否则 worker 生效性会被「无测试
    的脚本」假通过。"""
    from godot_qa_toolkit.mutation.channel_router import route_project_census
    census = route_project_census(str(kol))
    by_face: dict[str, list[str]] = {}
    for row in census.get("decisions", []):
        rel = row["target"].removeprefix("res://")
        if row["channel"] != "worker" or not (kol / rel).is_file():
            continue
        if _sites(kol, rel) <= 0:
            continue
        by_face.setdefault(rel.split("/", 1)[0], []).append(rel)
    priority = ["systems", "acceptance", "utils"]
    faces = [f for f in priority if f in by_face] + [
        f for f in sorted(by_face) if f not in priority]
    for face in faces[:3]:
        cands = sorted(by_face[face], key=lambda r: (_sites(kol, r), r))
        map_path, _measure = _ensure_map(kol, cands[0], godot_bin, checks,
                                         secondary=True)
        for rel in cands:
            if _map_line_coverage({face: map_path}, face, rel):
                return rel, _env(godot_bin, map_path)
    return None, {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kol-root", required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("--budget", type=int, default=2)
    ap.add_argument("--godot-bin", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    kol = Path(args.kol_root).resolve()
    checks = Checks()
    if not (kol / "project.godot").is_file():
        print(json.dumps({"ok": False, "error": f"not a Godot project: {kol}"},
                         ensure_ascii=False, indent=2))
        return 2
    if not (kol / args.target).is_file():
        print(json.dumps({"ok": False, "target_missing": args.target},
                         ensure_ascii=False, indent=2))
        return 2
    godot = args.godot_bin or None
    env = _env(godot, None)
    js_prune = _prune_vendored_js(kol)
    symlinks = _normalize_symlinks(kol)

    map_path, measure = _ensure_map(kol, args.target, godot, checks)
    measure_summary = _summary(measure.get("doc"))
    # A-M1：可用映射不得被退出码丢弃；union 未认证需显式区分。
    m1_ok = (measure.get("rc") == 0
             and measure_summary.get("attribution_usable") is True
             and Path(map_path).is_file()
             and measure_summary.get("certification") in
             ("certified", "uncertified"))
    checks.add("A-M1-measure-usable-map", m1_ok,
               "measure run rc/ok + certification/attribution_usable",
               {"run": {k: measure.get(k) for k in
                        ("cmd", "rc", "wall_s", "parse_error")},
                "summary": {k: measure_summary.get(k) for k in
                            ("certification", "union_certified",
                             "attribution_usable", "exit_semantics",
                             "attribution_map")}})
    if not m1_ok:
        print(json.dumps({"ok": False, "checks": checks.items},
                         ensure_ascii=False, indent=2))
        return 1

    facts = _map_facts(map_path, args.target)
    env_map = _env(godot, map_path)

    def mutate(cid: str, extra: list[str], *, must_ok: bool | None,
               timeout: int = 300) -> dict:
        res = _run(["mutation", args.target, "--project-root", str(kol)]
                   + extra, env=env_map, timeout=timeout)
        summary = _summary(res["doc"])
        judged = res["rc"] in (0, 1) and res["parse_error"] is None
        if must_ok is not None:
            judged = judged and (res["rc"] == 0) == must_ok
        res["summary"] = summary
        res["judged"] = judged
        return res

    # 先做一次 file 面运行：既作为 M4 判据，也完成引擎/槽/prewarm 冷启动
    # 预热，避免 H1 首轮冷制备把 60s 预算的失败判据拖成假超时（QA D1）。
    file_run = mutate("file", ["--select", "file", "--budget", str(args.budget),
                               "--mutate-all"], must_ok=None)

    # ---- A-H1：真实 full-face 60s 超时 + 上限处文案自洽 ----------------
    full = mutate("h1", ["--select", "full", "--jobs", "1",
                         "--timeout", "60", "--budget", "1"],
                  must_ok=False, timeout=300)
    action = full["summary"].get("suggested_action") or ""
    h1_ok = (full["judged"] and full["summary"].get("run_error") is True
             and full["summary"].get("suggested_action_kind")
             == "raise_timeout" and "--timeout 600" in action
             and full["summary"].get("selection_mode") == "full")
    checks.add("A-H1-full-timeout-actionable", h1_ok,
               "full at 60s -> run_error + raise_timeout to 600",
               {"run": {"cmd": full["cmd"], "rc": full["rc"]},
                "summary": {k: full["summary"].get(k) for k in
                            ("run_error", "suggested_action",
                             "suggested_action_kind", "selection_mode")}})
    from godot_qa_toolkit.timing import (BUDGET_CAP_S, suggested_action,
                                         suggested_action_kind)
    cap_text = suggested_action(BUDGET_CAP_S)
    cap_kind = suggested_action_kind(BUDGET_CAP_S)
    checks.add("A-H1-at-cap-not-contradictory",
               cap_kind == "narrow_face"
               and f"--timeout {BUDGET_CAP_S}" not in cap_text
               and "--select" in cap_text,
               "at cap: narrow-face action, no same-timeout echo",
               {"kind": cap_kind, "action": cap_text})

    # ---- A-M3：单文件 --jobs 1 恒为 flat ------------------------------
    jobs1 = mutate("m3", ["--jobs", "1", "--budget", str(args.budget),
                          "--mutate-all"], must_ok=None)
    doc = jobs1["doc"] or {}
    m3_ok = (jobs1["judged"] and "results" not in doc
             and jobs1["summary"].get("jobs") == 1
             and "mutants" in jobs1["summary"])
    checks.add("A-M3-jobs1-flat-envelope", m3_ok,
               "single file --jobs 1 top-level summary",
               {"top_keys": sorted(doc), "jobs": jobs1["summary"].get("jobs"),
                "rc": jobs1["rc"]})

    # ---- A-M4/L1/L5：selection_mode 全档 ------------------------------
    stable, site_line = _manifest_facts(kol, args.target)
    checks.add("A-M4-select-file",
               file_run["judged"]
               and file_run["summary"].get("selection_mode") == "file",
               "selection_mode=file (warm-up run)",
               {"run": {"cmd": file_run["cmd"], "rc": file_run["rc"],
                        "wall_s": file_run["wall_s"]},
                "selection_mode": file_run["summary"].get("selection_mode")})
    modes = [
        ("A-L1-explicit-tests",
         ["--tests", (facts["test_dirs"][0] if facts["test_dirs"] else ""),
          "--budget", str(args.budget), "--mutate-all"],
         "explicit_tests"),
        ("A-L5-lines-caliper",
         ["--lines", str(site_line) if site_line else "",
          "--budget", "1", "--mutate-all"],
         "lines"),
    ]
    for cid, extra, expected in modes:
        if not extra[1]:
            checks.add(cid, False, "no mapped test dir / no mutation site "
                       "for caliper — cannot verify (not passed)",
                       {"required_value": expected})
            continue
        res = mutate(cid, extra, must_ok=None)
        checks.add(cid, res["judged"]
                   and res["summary"].get("selection_mode") == expected,
                   f"selection_mode={expected}",
                   {"run": {"cmd": res["cmd"], "rc": res["rc"]},
                    "selection_mode": res["summary"].get("selection_mode")})
    if stable:
        res = mutate("A-L5-mutation-caliper",
                     ["--mutation", stable, "--budget", "1"], must_ok=None)
        checks.add("A-L5-mutation-caliper",
                   res["judged"]
                   and res["summary"].get("selection_mode") == "mutation",
                   "selection_mode=mutation",
                   {"selection_mode": res["summary"].get("selection_mode")})
    else:
        checks.add("A-L5-mutation-caliper", False,
                   "target has no mutation site for --mutation", {})

    # ---- A-M5：行级并发上限=签名组数披露 ------------------------------
    grouped = mutate("m5", ["--budget", "4", "--mutate-all"], must_ok=None)
    gs = grouped["summary"]
    groups, jobs = gs.get("selection_groups"), gs.get("jobs")
    if groups and jobs and groups < jobs:
        m5_ok = (gs.get("jobs_limit_reason") == "selection_groups"
                 and jobs == groups)
        detail = "jobs capped by signature groups and disclosed"
    else:
        m5_ok = gs.get("selection_mode") == "line" and groups is not None
        detail = "no cap observed; line mode + selection_groups disclosed"
    checks.add("A-M5-group-concurrency-disclosure", m5_ok, detail,
               {"selection_mode": gs.get("selection_mode"),
                "selection_groups": groups, "jobs": jobs,
                "jobs_limit_reason": gs.get("jobs_limit_reason")})

    # ---- A-L2：零引用键名 ---------------------------------------------
    zero_target = _zero_ref_target(kol, facts)
    if zero_target:
        z = _run(["mutation", zero_target, "--project-root", str(kol),
                  "--budget", "2"], env=env_map)
        zs = _summary(z["doc"])
        checks.add("A-L2-zero-ref-key",
                   z["parse_error"] is None
                   and zs.get("zero_ref_short_circuit") is True
                   and bool(zs.get("short_circuit_reason")),
                   f"zero-ref key on {zero_target}",
                   {"target": zero_target, "rc": z["rc"],
                    "summary": {k: zs.get(k) for k in
                                ("mutants", "zero_ref_short_circuit",
                                 "short_circuit_reason", "selection_mode")}})
    else:
        checks.add("A-L2-zero-ref-key", False,
                   "no zero-ref target in map scope — cannot verify", {})

    # ---- B：worker 路由（生效或具名不可达） ----------------------------
    wc = mutate("b-worker-target",
                ["--worker-channel", "--budget", str(args.budget),
                 "--mutate-all"], must_ok=None)
    block = wc["summary"].get("worker_channel") or {}
    route = block.get("route")
    if route == "cold":
        b_ok = (bool(block.get("route_reasons"))
                and block.get("decision") == "cold_route_excluded"
                and block.get("override_refused") is True)
        b_detail = "target route-excluded with named reasons + refusal"
    else:
        b_ok = (block.get("decision") in ("worker", "worker_degraded_cold")
                and bool(block.get("reason"))
                and wc["summary"].get("exec_scheme") == "worker-reset/1")
        b_detail = "target executes on worker-reset/1 (or named degradation)"
    checks.add("B-worker-route-disclosed", b_ok, b_detail,
               {"worker_channel": block,
                "exec_scheme": wc["summary"].get("exec_scheme")})

    eligible = _worker_target(kol, facts)
    eligible_env = env_map
    if eligible is None:
        eligible, eligible_env = _pick_eligible(
            kol, godot, checks)
    if eligible:
        e = _run(["mutation", eligible, "--project-root", str(kol),
                  "--worker-channel", "--budget", str(args.budget),
                  "--mutate-all"], env=eligible_env)
        es = _summary(e["doc"])
        e["summary"] = es
        eb = es.get("worker_channel") or {}
        checks.add("B-worker-effective-or-named",
                   e["parse_error"] is None
                   and eb.get("decision") in ("worker",
                                              "worker_degraded_cold")
                   and bool(eb.get("reason")),
                   f"eligible target {eligible}",
                   {"target": eligible, "rc": e["rc"],
                    "decision": eb.get("decision"),
                    "reason": eb.get("reason"),
                    "exec_scheme": es.get("exec_scheme"),
                    "fatal": eb.get("fatal_events")})
    else:
        checks.add("B-worker-effective-or-named", False,
                   "no worker-eligible target in map scope", {})

    # ---- B：farm / prewarm / stage_reconciliation ---------------------
    observed = [file_run, full, jobs1, grouped, wc]
    farms = [r["summary"].get("farm") for r in observed
             if isinstance(r["summary"].get("farm"), dict)]
    prewarms = [r["summary"].get("prewarm") for r in observed
                if isinstance(r["summary"].get("prewarm"), dict)]
    farm_ok = bool(farms) and all(
        f.get("decision") in ("created", "reused", "rebuilt")
        and bool(f.get("reason")) and f.get("proof_binding") is not None
        for f in farms)
    checks.add("B-farm-named-freshness", farm_ok,
               "farm decision/reason/proof_binding per run",
               {"farms": farms[-3:]})
    fresh = [p for p in prewarms
             if p.get("decision") == "fresh_skip" and p.get("reason")]
    named_block = [p for p in prewarms
                   if p.get("decision") in
                   ("imported_uncertified", "imported_no_lock", "disabled")
                   and p.get("reason")]
    prewarm_ok = bool(prewarms) and bool(fresh)
    checks.add("B-prewarm-fresh-skip", prewarm_ok,
               ("prewarm fresh_skip observed after symlink normalization"
                if fresh else "prewarm fresh_skip NOT observed"),
               {"fresh_skip_observed": bool(fresh),
                "environment_normalization": {**symlinks, **js_prune},
                "blockers": named_block[-2:],
                "prewarms": prewarms[-3:]})
    # QA D2：连续多模式/多目标串跑后，warm 复用不得产生假 run_error。
    reuse_runs = [r for r in observed + ([e] if eligible else [])
                  if (r["summary"].get("mutants") or 0) > 0]
    false_errors = [
        {"cmd": r["cmd"], "run_errors": r["summary"].get("run_errors"),
         "farm": (r["summary"].get("farm") or {}).get("decision")}
        for r in reuse_runs if (r["summary"].get("run_errors") or 0) > 0]
    reused_seen = any(
        ((r["summary"].get("farm") or {}).get("decision") == "reused")
        for r in reuse_runs)
    checks.add("B-warm-reuse-no-false-run-error",
               bool(reuse_runs) and not false_errors and reused_seen,
               "no false run_error across warm-reused modes/targets",
               {"runs": len(reuse_runs), "false_errors": false_errors,
                "warm_reuse_observed": reused_seen})
    recons = [(r["summary"].get("stage_reconciliation") or {})
              for r in observed]
    recon_ok = any(
        rc.get("wall_s", 0) > 0
        and "prewarm_decision" in rc
        and abs((rc.get("attributed_s") or 0)
                + (rc.get("unexplained_s") or 0)
                - (rc.get("wall_s") or 0)) <= 0.01
        and (rc.get("over_attributed_s") or 0) >= 0
        for rc in recons)
    checks.add("B-stage-reconciliation", recon_ok,
               "wall/attributed/unexplained/over_attributed reconcile",
               {"reconciliations": recons[-3:]})

    report = {"ok": checks.ok, "kol_root": str(kol),
              "target": args.target, "map": map_path,
              "environment_normalization": {**symlinks, **js_prune},
              "checks": checks.items}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0 if checks.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
