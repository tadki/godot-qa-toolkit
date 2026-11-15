#!/usr/bin/env python3
"""SEE-1587 §SPEC-006 — 真实 KOL 隔离副本端到端判据。

一次测量运行同时产出 line-primary Attribution Map（test-attribution/6）与
mutation 可消费的 Coverage Store（see1420-coverage/2 同轮投影）；随后默认
mutation（--select line）同时吃到行级选面与 coverage 前置过滤——
`summary.coverage_prefilter.source` 出现实际来源（coverage_store /
projection_store / projection_live），非常态 store miss。

用法（KOL 仅只读源；全部在 /tmp 真副本执行）：
    python tests/integration/run_see1587_e2e.py \
        --kol-root /tmp/see1587-kol --target utils/csv_tmap_parser.gd

退出码：0 = PASS；1 = FAIL（证据 JSON 仍落盘）。
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

SRC = Path(__file__).resolve().parents[2] / "src"
DEFAULT_GODOT = "/home/jerry/gqt-probe-bin/godot"
PROJECTION_SOURCES = {"coverage_store", "projection_store", "projection_live"}


def _env(godot_bin: str | None) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    gb = godot_bin or env.get("GQT_GODOT_BIN") or DEFAULT_GODOT
    if gb:
        env["GQT_GODOT_BIN"] = gb
    return env


def _run(argv: list[str], stdin_env: dict, cwd: Path | None = None
         ) -> dict:
    t0 = time.monotonic()
    proc = subprocess.run(argv, capture_output=True, text=True,
                          env=stdin_env, cwd=str(cwd) if cwd else None)
    wall = round(time.monotonic() - t0, 3)
    payload = None
    if proc.stdout.strip():
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            payload = None
    return {"argv": argv, "rc": proc.returncode, "wall_s": wall,
            "stdout_json": payload,
            "stderr_tail": (proc.stderr or "")[-4000:],
            "stdout_raw_tail": (proc.stdout or "")[-2000:]}


def _assert(checks: list[dict], name: str, ok: bool, detail) -> None:
    checks.append({"check": name, "ok": bool(ok), "detail": detail})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kol-root", required=True)
    ap.add_argument("--kol-source", default=None,
                    help="optional read-only source to cp -a when kol-root "
                         "does not exist yet")
    ap.add_argument("--target", default="utils/csv_tmap_parser.gd")
    ap.add_argument("--budget", type=int, default=2)
    ap.add_argument("--godot-bin", default=None)
    ap.add_argument("--recipe", default=None)
    ap.add_argument("--out", default="/tmp/see1587-e2e.json")
    ap.add_argument("--work-dir", default=None)
    args = ap.parse_args()

    kol = Path(args.kol_root)
    target = kol / args.target
    work = Path(args.work_dir or (str(kol) + "-e2e-work"))
    work.mkdir(parents=True, exist_ok=True)
    recipe_path = Path(args.recipe or (work / "recipe.json"))
    env = _env(args.godot_bin)
    checks: list[dict] = []
    runs: list[dict] = []
    t_all = time.monotonic()

    if not kol.is_dir():
        if not args.kol_source:
            print(f"kol-root missing: {kol} (pass --kol-source to copy)",
                  file=sys.stderr)
            return 1
        src = Path(args.kol_source)
        shutil.copytree(src, kol, symlinks=True)
        # 真副本核验：inode 必须不同
        if src.joinpath("project.godot").stat().st_ino == \
                kol.joinpath("project.godot").stat().st_ino:
            print("kol-root is a hardlink copy (forbidden)", file=sys.stderr)
            return 1
    _assert(checks, "kol layout",
            target.is_file() and (kol / "addons/gut/gut_cmdln.gd").is_file()
            and (kol / ".gutconfig.json").is_file(),
            {"target": str(target), "gut": str(kol / "addons/gut")})
    if not all(c["ok"] for c in checks):
        _write(args.out, checks, runs, t_all)
        return 1

    godot = env.get("GQT_GODOT_BIN")
    var = subprocess.run([godot, "--version"], capture_output=True, text=True)
    _assert(checks, "godot binary", var.returncode == 0,
            {"bin": godot, "version": (var.stdout or "").strip()[:80]})

    cli = [sys.executable, "-m", "godot_qa_toolkit.cli"]

    # 1) measurement recipe
    if not recipe_path.is_file():
        runs.append(("measure suite", _run(
            cli + ["measure", "suite", "--project-root", str(kol),
                   "--out", str(recipe_path)], env)))
    _assert(checks, "recipe exists", recipe_path.is_file(),
            str(recipe_path))

    # 2) 一次测量运行 → line-primary Attribution Map（同轮行事实）
    map_path = kol / ".qa-cache" / "attribution-map.json"
    map_backup = None
    if map_path.is_file():
        map_backup = map_path.with_suffix(".json.bak")
        shutil.copy2(map_path, map_backup)
    top = target.relative_to(kol).parts[0]
    measure = _run(cli + [
        "measure", "run", str(kol / top), "--project-root", str(kol),
        "--recipe", str(recipe_path),
        "--out", str(work / "measure-report.json"),
        "--test-attribution", "--test-attribution-out", str(map_path),
        "--acceptance-repeats", "1", "--timeout", "300"], env, cwd=work)
    runs.append(("measure run --test-attribution", measure))
    # 孤立副本 acceptance 臂 invalid 属已知环境边界（报告 ok=false 可接受，
    # gut_full 臂有效性才是归属真值门槛）——此处如实记录不掩盖。
    _assert(checks, "attribution map written", map_path.is_file(),
            {"path": str(map_path), "measure_rc": measure["rc"]})
    if not map_path.is_file():
        _write(args.out, checks, runs, t_all)
        return 1
    doc = json.loads(map_path.read_text(encoding="utf-8"))
    rel = str(target.relative_to(kol))
    arms_state = (doc.get("arms_state") or {}).get("gut_full") or {}
    arms_ok = (arms_state.get("state") == "valid"
               or (int(arms_state.get("bundles") or 0) > 0
                   and int(arms_state.get("pairs") or 0) > 0))
    _assert(checks, "map schema == test-attribution/6",
            doc.get("schema_version") == "test-attribution/6",
            doc.get("schema_version"))
    _assert(checks, "map gut_full arm valid", arms_ok, arms_state)
    _assert(checks, "map binds target file", rel in (doc.get("file_sha256")
                                                     or {}), rel)
    arm = (doc.get("arms") or {}).get("gut_full") or {}
    target_lines = sorted({ln for by_rel in
                           (arm.get("test_to_lines") or {}).values()
                           for ln in (by_rel or {}).get(rel, [])})
    reverse = (arm.get("line_to_tests") or {}).get(rel) or {}
    _assert(checks, "line facts present for target",
            bool(target_lines) and bool(reverse),
            {"covered_lines": len(target_lines),
             "reverse_lines": len(reverse)})
    _assert(checks, "instrumentable surface present",
            bool((doc.get("instrumentable_lines") or {}).get(rel)),
            len((doc.get("instrumentable_lines") or {}).get(rel) or []))

    # 3) 默认 mutation：行级选面 + coverage prefilter 同轮生效
    mutation = _run(cli + [
        "mutation", str(target), "--project-root", str(kol),
        "--select", "line", "--budget", str(args.budget), "--mutate-all",
        "--timeout", "120"], env, cwd=work)
    runs.append(("mutation --select line (default semantics)", mutation))
    payload = mutation["stdout_json"] or {}
    summary = ((payload.get("results") or [payload])[0] or {}).get(
        "summary") or {}
    ts = summary.get("test_selection") or {}
    pf = summary.get("coverage_prefilter")
    pf_source = pf.get("source") if isinstance(pf, dict) else pf
    _assert(checks, "mutation completed", mutation["rc"] == 0,
            {"rc": mutation["rc"]})
    _assert(checks, "line-level selection used",
            ts.get("source") == "runtime_mapping"
            and summary.get("selection_mode") == "line",
            {"test_selection.source": ts.get("source"),
             "selection_mode": summary.get("selection_mode"),
             "tests": len(ts.get("tests") or [])})
    _assert(checks, "coverage prefilter active (not store_miss)",
            pf_source in PROJECTION_SOURCES,
            {"coverage_prefilter": pf})
    _assert(checks, "scoped test face", bool(summary.get("scoped")),
            summary.get("scoped"))

    # 4) Coverage Store 同轮投影条目（/2，target 有实际条目）
    entries = []
    for p in sorted((kol / ".qa-cache" / "gqt-coverage").glob("*.json")) \
            if (kol / ".qa-cache" / "gqt-coverage").is_dir() else []:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if d.get("rel") == rel:
            entries.append({"file": p.name, "schema": d.get("schema"),
                            "tests_glob": d.get("tests_glob"),
                            "projection": bool(d.get("projection"))})
    proj_entries = [e for e in entries
                    if e["schema"] == "see1420-coverage/2"]
    _assert(checks, "coverage store projection entry for target",
            bool(proj_entries), proj_entries[:3] or entries[:3])

    if map_backup is not None:
        map_backup.unlink(missing_ok=True)
    return _write(args.out, checks, runs, t_all)


def _write(out: str, checks: list[dict], runs: list, t_all: float) -> int:
    ok = all(c["ok"] for c in checks)
    doc = {
        "spec": "SEE-1587 §SPEC-006",
        "ok": ok,
        "wall_s_total": round(time.monotonic() - t_all, 3),
        "checks": checks,
        "runs": [{"step": name, "rc": r["rc"], "wall_s": r["wall_s"],
                  "argv": r["argv"], "stderr_tail": r["stderr_tail"][-800:]}
                 for name, r in runs],
    }
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    failed = [c["check"] for c in checks if not c["ok"]]
    print(json.dumps({"ok": ok, "out": out,
                      **({"failed": failed} if failed else {})},
                     ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
