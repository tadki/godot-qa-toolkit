#!/usr/bin/env python3
"""SEE-1615 §SPEC-PERF1 —— 历史杀手排序 × kill-bail chunk C 交叉实测。

协议（与既有性能协议同口径，估算不作结论）：
- A 臂 = 排序开启（GQT_MUTATION_TEST_ORDER=history）；B 臂 = 排序关断
  （=0，现状）；A/A ≥3 轮定噪声带，再 A/B ≥3 对**交错**（逐对换臂序控漂移）。
- C ∈ --chunks × 两臂矩阵；每轮记录绝对墙钟、summary（killed/survived/
  bail marker）与 resume store 的 stable-id→verdict 映射（**verdict 不翻转**
  逐对核对；证书真实性 = killed 条目 killed_by 在场）。
- 杀腿/幸存腿分账：被杀腿收益 = (中位 B − 中位 A) / killed 数（幸存腿
  设计上不 bail，成本恒定——A/A 噪声带给出上界）。
- 每轮结果原子落盘 `<report>.cells/`，重复执行跳过已完成单元（可续跑）。
- 结果不足/无被杀腿（排序无可作用面）= INCONCLUSIVE，如实报告，不写 PASS。

测量对象：受控合成工程（真实 Godot+GUT direct-drive 宿主；KOL 副本无归属
映射，全量 map-update 超出单轮预算——KOL 规模标注为未测，报告如实披露）。

用法（仓库根）：
    PYTHONPATH=src python3 -I tests/integration/run_see1615_perf1.py \
        --aa 3 --rounds 3 --chunks 8,16,32,64 --report perf1-report.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:  # `-I` 隔离模式下 PYTHONPATH 被忽略，须自举
    sys.path.insert(0, str(SRC))
DEFAULT_GODOT = "/home/jerry/gqt-probe-bin/godot"
KOL_ADDONS = Path("/home/jerry/multica_workspaces/seed-478690824e46/"
                  "see-1615-f9a8c1cf79db/workdir/KingOfLikes-Godot/addons/gut")

HOT_FUNCS = [("v01", "a + b"), ("v02", "a - b"), ("v03", "a * b"),
             ("v04", "a + b"), ("v05", "a - b"), ("v06", "a * b"),
             ("v07", "a + b"), ("v08", "a - b")]
# 期望值：(a,b) 取样后与变异实现可区分（+ → −/× 均破坏断言）
ARGS = (6, 2)


def _expected(expr: str) -> int:
    a, b = ARGS
    if expr == "a":               # w01 恒等函数（无位点，断言恒真）
        return a
    return {"a + b": a + b, "a - b": a - b, "a * b": a * b}[expr]


def _target_src() -> str:
    lines = ["extends RefCounted", ""]
    for name, expr in HOT_FUNCS:
        lines.append(f"func {name}(a: int, b: int) -> int:")
        lines.append(f"\treturn {expr}")
        lines.append("")
    # w01 无变异位点（return a）——single 测它：基线恒绿、永不成为 killer
    # （PERF1 形态：hot 为唯一历史杀手，排序收益可测；w 行不入行 facts，
    # 其位点走 uncovered 不执行）。
    lines.append("func w01(a: int, b: int) -> int:")
    lines.append("\treturn a")
    lines.append("")
    return "\n".join(lines)


def _test_script(name: str, funcs: list[tuple[str, str]],
                 suffix: str = "") -> str:
    # 每测试带受控忙轮（~20ms）：脚本执行时间成量，脚本顺序的 bail 收益
    # 可测。测试名 = test_{suffix}{fn}（GUT 只收集 test_ 前缀函数；唯一
    # 名保证 kill 证书归属消歧）。
    body = "\n".join(
        f"func test_{suffix}{fn}() -> void:\n"
        f"\tvar acc = 0\n"
        f"\tfor i in range(90000):\n"
        f"\t\tacc += i\n"
        f"\tassert_gt(acc, 0)\n"
        f"\tvar s = CALC.new()\n"
        f"\tassert_eq(s.{fn}({ARGS[0]}, {ARGS[1]}), {_expected(expr)})"
        for fn, expr in funcs)
    return ("extends GutTest\n\nconst CALC = preload(\"res://calc.gd\")\n\n"
            + body + "\n")


def build_fixture(root: Path, n_scripts: int = 24) -> tuple[Path, str]:
    """受控工程：hot 脚本断言全部 8 函数（击杀数最高），其余单函数。"""
    if root.exists():
        shutil.rmtree(root)
    (root / "tests" / "unit").mkdir(parents=True)
    (root / "addons").mkdir()
    if not KOL_ADDONS.is_dir():
        raise SystemExit(f"missing real GUT addon: {KOL_ADDONS}")
    shutil.copytree(KOL_ADDONS, root / "addons" / "gut")
    (root / "project.godot").write_text(
        'config_version=5\n\n[application]\nconfig/name="perf1"\n',
        encoding="utf-8")
    # 测量 recipe 面（build_recipe 需要）：最小 gutconfig + 空 feature 面
    (root / ".gutconfig.json").write_text(
        json.dumps({"dirs": ["res://tests/unit"]}), encoding="utf-8")
    (root / "acceptance" / "features").mkdir(parents=True)
    (root / "acceptance" / "features" / "perf1.feature").write_text(
        "Feature: perf1 fixture\n\n  Scenario: smoke\n    Given a project\n",
        encoding="utf-8")
    target = root / "calc.gd"
    target.write_text(_target_src(), encoding="utf-8")
    (root / "tests" / "unit" / "t_hot.gd").write_text(
        _test_script("t_hot", HOT_FUNCS), encoding="utf-8")
    for i in range(n_scripts - 1):
        # single 只测无位点 w01（唯一历史杀手 = hot；test 名 test_ 前缀
        # 且跨脚本唯一——GUT 仅收集 test_ 前缀函数，证书归属按名消歧）
        (root / "tests" / "unit" / f"t_w{i:02d}.gd").write_text(
            _test_script(f"t_w{i:02d}", [("w01", "a")],
                         suffix=f"w{i:02d}_"), encoding="utf-8")
    _write_map(root, target, n_scripts)
    return target, "calc.gd"


def _write_map(root: Path, target: Path, n_scripts: int) -> None:
    from godot_qa_toolkit.measurement.recipe import build_recipe, recipe_hash
    from godot_qa_toolkit.mutation.ops import _collect_mutations

    recipe, err = build_recipe(root)
    if err is not None:
        raise SystemExit(f"recipe error: {err}")
    raw = recipe_hash({k: v for k, v in recipe.items() if k != "recipe_hash"})
    src = target.read_text(encoding="utf-8")
    lines = sorted({m.line for m in _collect_mutations(src, "calc.gd")})
    tests = ["tests/unit/t_hot.gd"] + [f"tests/unit/t_w{i:02d}.gd"
                                       for i in range(n_scripts - 1)]
    # hot 脚本含 test_v01..test_v08（唯一杀手面），single 各含唯一名测试
    # （test_wNN_w01——GUT 仅收集 test_ 前缀函数，归属按名消歧）
    idents = [f"t_hot.gd::test_{fn}" for fn, _e in HOT_FUNCS]
    idents += [f"t_w{i:02d}.gd::test_w{i:02d}_w01"
               for i in range(n_scripts - 1)]
    line_to_tests = {ln: sorted(set(idents)) for ln in lines}
    test_to_lines: dict[str, dict[str, list[int]]] = {}
    for ln, ids in line_to_tests.items():
        for ident in ids:
            test_to_lines.setdefault(ident, {}).setdefault("calc.gd",
                                                           []).append(ln)
    face = {t: hashlib.sha256((root / t).read_bytes()).hexdigest()
            for t in tests}
    arm = {
        "test_to_files": {i: ["calc.gd"] for i in test_to_lines},
        "test_to_functions": {}, "script_to_files": {},
        "script_to_functions": {},
        "file_to_tests": {"calc.gd": sorted(test_to_lines)},
        "file_to_scripts": {},
        "test_to_lines": {i: {"calc.gd": sorted(v["calc.gd"])}
                          for i, v in test_to_lines.items()},
        "script_to_lines": {},
        "line_to_tests": {"calc.gd": {str(ln): sorted(ids)
                                      for ln, ids in line_to_tests.items()}},
        "unattributed_files": [],
    }
    doc = {"schema_version": "test-attribution/6",
           "report_recipe_hash": raw, "recipe_hash_pristine": raw,
           "project_root": str(root), "identity": "res://script::test_name",
           "causal": {"enabled": False},
           "file_sha256": {"calc.gd": hashlib.sha256(
               target.read_bytes()).hexdigest()},
           "target_files": ["calc.gd"], "tests_face": face,
           "instrumentable_lines": {"calc.gd": list(range(1, 40))},
           "arms": {"gut_full": arm},
           "arms_state": {"gut_full": {"state": "valid", "bundles": 1,
                                       "pairs": len(line_to_tests) * len(idents),
                                       "probes_fired": 1}},
           "notes": []}
    out = root / ".qa-cache" / "attribution-map.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def _env(order_on: bool, chunk: int, godot: str) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["GQT_GODOT_BIN"] = godot
    env["GQT_MUTATION_WORKER"] = "0"          # 冷 direct-drive 宿主（受控面）
    env["GQT_MUTATION_TEST_ORDER"] = "history" if order_on else "off"
    env["GQT_MUTATION_BAIL_CHUNK"] = str(chunk)
    return env


def _run(root: Path, target: Path, order_on: bool, chunk: int, budget: int,
         godot: str, timeout_s: int = 600) -> dict:
    cmd = [sys.executable, "-m", "godot_qa_toolkit.cli", "mutation",
           str(target), "--project-root", str(root), "--budget", str(budget),
           "--mutate-all", "--timeout", str(timeout_s // 4)]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=str(root), env=_env(order_on, chunk,
                                                           godot),
                              capture_output=True, text=True,
                              timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "run_timeout", "wall_s": timeout_s}
    wall = round(time.monotonic() - t0, 3)
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        return {"ok": False, "error": "non_json_stdout", "wall_s": wall,
                "rc": proc.returncode,
                "stderr_tail": proc.stderr[-300:]}
    s = out.get("summary") or {}
    return {"ok": bool(out.get("ok")), "wall_s": wall, "rc": proc.returncode,
            "killed": int(s.get("killed") or 0),
            "survived": int(s.get("survived") or 0),
            "verdict_map": _verdict_map(root),
            "summary_keys": sorted(s.keys())}


def _verdict_map(root: Path) -> dict[str, str]:
    """resume v2 store 全条目 stable-id → verdict（rel=calc.gd）。"""
    from godot_qa_toolkit.mutation.resume2 import RESUME_V2_DIRNAME

    out: dict[str, str] = {}
    for manifest in (root / ".qa-cache" / RESUME_V2_DIRNAME).glob(
            "*/manifest.json"):
        try:
            doc = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if doc.get("rel") != "calc.gd":
            continue
        for entry in manifest.parent.glob("*.json"):
            if entry.name == "manifest.json":
                continue
            try:
                rec = json.loads(entry.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict) and rec.get("id"):
                v = str(rec.get("verdict") or "")
                if v == "killed" and not rec.get("killed_by"):
                    v = "killed_no_certificate"
                out[str(rec["id"])] = v
    return out


def _cell(cells: Path, name: str) -> dict | None:
    p = cells / f"{name}.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _save(cells: Path, name: str, data: dict) -> None:
    cells.mkdir(parents=True, exist_ok=True)
    tmp = cells / f".{name}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, cells / f"{name}.json")


def _spread(samples: list[float]) -> float:
    if len(samples) < 2 or statistics.median(samples) == 0:
        return 0.0
    return round((max(samples) - min(samples)) / statistics.median(samples),
                 4)


def _flips(a: dict[str, str], b: dict[str, str]) -> list[str]:
    ids = set(a) | set(b)
    return sorted(i for i in ids if a.get(i) != b.get(i))


def run_cell(root: Path, target: Path, chunk: int, *, aa: int, rounds: int,
             budget: int, godot: str) -> dict:
    aa_runs = [_run(root, target, False, chunk, budget, godot)
               for _ in range(aa)]
    pairs = []
    for i in range(rounds):
        if i % 2 == 0:      # 交错：奇偶轮换臂序控漂移
            a = _run(root, target, True, chunk, budget, godot)
            b = _run(root, target, False, chunk, budget, godot)
        else:
            b = _run(root, target, False, chunk, budget, godot)
            a = _run(root, target, True, chunk, budget, godot)
        pairs.append({"A": a, "B": b})
    return {"chunk": chunk, "aa": aa_runs, "pairs": pairs}


def summarize_cell(cell: dict) -> dict:
    aa_s = [r["wall_s"] for r in cell["aa"] if r.get("ok")]
    a_s = [p["A"]["wall_s"] for p in cell["pairs"] if p["A"].get("ok")]
    b_s = [p["B"]["wall_s"] for p in cell["pairs"] if p["B"].get("ok")]
    noise = _spread(aa_s)
    cell_out = {
        "chunk": cell["chunk"],
        "aa_s": aa_s, "a_s": a_s, "b_s": b_s,
        "aa_spread_pct": round(noise * 100, 2),
    }
    if len(a_s) < 1 or len(b_s) < 1 or not aa_s:
        cell_out["status"] = "INCONCLUSIVE"
        cell_out["reason"] = "insufficient_ok_runs"
        return cell_out
    med_a, med_b = statistics.median(a_s), statistics.median(b_s)
    cell_out.update({
        "median_a_s": round(med_a, 3), "median_b_s": round(med_b, 3),
        "ratio_a_over_b": round(med_a / med_b, 4) if med_b else None,
        "improvement_off_over_on": round(med_b / med_a, 4) if med_a else None,
    })
    flips = []
    for p in cell["pairs"]:
        flips.extend(_flips(p["A"].get("verdict_map") or {},
                            p["B"].get("verdict_map") or {}))
    killed = max((r.get("killed") or 0)
                 for r in [p["A"] for p in cell["pairs"]
                           if p["A"].get("ok")] or [{}])
    cell_out["verdict_flips"] = sorted(set(flips))
    cell_out["killed_sample"] = killed
    if killed <= 0:
        cell_out["status"] = "INCONCLUSIVE"
        cell_out["reason"] = "no_killed_legs_ordering_has_no_surface"
        return cell_out
    cell_out["per_killed_leg_delta_s"] = round((med_b - med_a) / killed, 4)
    gain = (med_b - med_a) / med_b if med_b else 0.0
    cell_out["status"] = (
        "GAIN" if gain > noise and not flips
        else "NO_GAIN_OVER_NOISE" if abs(gain) <= noise and not flips
        else "REGRESSION" if gain < -noise and not flips
        else "VERDICT_FLIPS")
    return cell_out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aa", type=int, default=3)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--chunks", default="8,16,32,64")
    ap.add_argument("--budget", type=int, default=24)
    ap.add_argument("--report", default="perf1-report.json")
    ap.add_argument("--godot-bin", default=DEFAULT_GODOT)
    ap.add_argument("--fixture-root", default=None)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)
    if not Path(args.godot_bin).is_file():
        raise SystemExit(f"godot binary missing: {args.godot_bin}")
    root = Path(args.fixture_root or tempfile.mkdtemp(prefix="see1615-perf1-"))
    target, _rel = build_fixture(root)
    report_path = Path(args.report).resolve()
    cells_dir = report_path.with_suffix(report_path.suffix + ".cells")
    err = _run(root, target, False, 32, args.budget, args.godot_bin)
    if not err.get("ok"):
        print(json.dumps({"stage": "warmup_failed", "run": err},
                         ensure_ascii=False, indent=2))
        return 2
    results = []
    for chunk in [int(c) for c in args.chunks.split(",") if c.strip()]:
        name = f"c{chunk:03d}"
        cell = _cell(cells_dir, name)
        if cell is None:
            cell = run_cell(root, target, chunk, aa=args.aa,
                            rounds=args.rounds, budget=args.budget,
                            godot=args.godot_bin)
            _save(cells_dir, name, cell)
        results.append(summarize_cell(cell))
    doc = {
        "tool": "see1615-perf1",
        "fixture": str(root), "fixture_scripts": 24,
        "fixture_note": ("合成真实 GUT 工程（KOL addons/gut 副本）；KOL 样本"
                         "轨道见 perf1-kol-track.json（直驱宿主腿挂起，具名"
                         "阻塞；非 map 预算省略）"),
        "aa": args.aa, "rounds": args.rounds, "budget": args.budget,
        "godot_bin": args.godot_bin,
        "cells": results,
        # 可复跑口径 = 本次实际 argv（不硬编码矩阵——证据须自证执行面）
        "reproduce": " ".join([sys.executable, str(Path(__file__).resolve())]
                              + list(argv if argv is not None else
                                     sys.argv[1:])),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    report_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2)
                           + "\n", encoding="utf-8")
    if not args.keep:
        shutil.rmtree(root, ignore_errors=True)
    print(json.dumps({"report": str(report_path),
                      "cells": results}, ensure_ascii=False, indent=2))
    ok = all(c["status"] in ("GAIN", "NO_GAIN_OVER_NOISE") for c in results)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
