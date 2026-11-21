#!/usr/bin/env python3
"""SEE-1615 §SPEC-PERF2 —— 等价剪枝双轨实测与逐规则报告。

协议：
- 合成轨道：含可证形态（INT±0 / INT*1 / 对称臂）的 fixture 工程，
  开关 on/off 各跑一轮（budget 同、--mutate-all），对比 wall、pruned
  计数、verdict 集（共同 id 逐位一致 + pruned id 只出现在 off 轨道）。
- KOL 只读轨道：/tmp 忠实副本（须已 --import），手工物化既有测试选择面，
  同协议 on/off；KOL 无归属映射 → GQT_ATTRIBUTION_MAP_MODE=off。
- 报告逐规则列证明/正反例引用/计数与成本；估算不作结论。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
DEFAULT_GODOT = "/home/jerry/gqt-probe-bin/godot"

HOT_FUNCS = [("v01", "a + b"), ("v02", "a - b"), ("v03", "a * b"),
             ("v04", "a + b"), ("v05", "a - b"), ("v06", "a * b"),
             ("v07", "a + b"), ("v08", "a - b")]
ARGS = (6, 2)
# 可证等价形态：两侧整数字面量（动态左操作数因 float −0.0 符号位不可证，
# 属保守保留的负例——见 unit 测试 negatives）
EQUIV_FUNCS = [("e01", "7 + 0"), ("e02", "9 - 0"), ("e03", "5 * 1")]


def _expected(expr: str) -> int:
    a, b = ARGS
    return {"a + b": a + b, "a - b": a - b, "a * b": a * b,
            "7 + 0": 7, "9 - 0": 9, "5 * 1": 5}[expr]


def _target_src() -> str:
    lines = ["extends RefCounted", ""]
    for name, expr in HOT_FUNCS + EQUIV_FUNCS:
        lines.append(f"func {name}(a: int, b: int) -> int:")
        lines.append(f"\treturn {expr}")
        lines.append("")
    lines.append("func w01(a: int, b: int) -> int:")
    lines.append("\treturn a")
    lines.append("")
    return "\n".join(lines)


def _test_script(funcs: list[tuple[str, str]], suffix: str = "") -> str:
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


def build_fixture(root: Path, kol_addons: Path) -> tuple[Path, Path]:
    if root.exists():
        import shutil
        shutil.rmtree(root)
    if not kol_addons.is_dir():
        raise SystemExit(f"GUT addon source missing (--kol-addons): "
                         f"{kol_addons}")
    (root / "tests" / "unit").mkdir(parents=True)
    (root / "addons").mkdir()
    import shutil
    shutil.copytree(kol_addons, root / "addons" / "gut")
    (root / "project.godot").write_text(
        'config_version=5\n\n[application]\nconfig/name="perf2"\n',
        encoding="utf-8")
    (root / ".gutconfig.json").write_text(
        json.dumps({"dirs": ["res://tests/unit"]}), encoding="utf-8")
    (root / "acceptance" / "features").mkdir(parents=True)
    (root / "acceptance" / "features" / "perf2.feature").write_text(
        "Feature: perf2\n\n  Scenario: smoke\n    Given a project\n",
        encoding="utf-8")
    target = root / "calc.gd"
    target.write_text(_target_src(), encoding="utf-8")
    # hot 脚本断言全部函数（hot/equiv 都有 killer）
    (root / "tests" / "unit" / "t_hot.gd").write_text(
        _test_script(HOT_FUNCS + EQUIV_FUNCS), encoding="utf-8")
    for i in range(10):
        fn, expr = (HOT_FUNCS + EQUIV_FUNCS)[i % len(HOT_FUNCS + EQUIV_FUNCS)]
        (root / "tests" / "unit" / f"t_w{i:02d}.gd").write_text(
            _test_script([(fn, expr)], suffix=f"w{i:02d}_"), encoding="utf-8")
    _write_map(root, target)
    return root, target


def _write_map(root: Path, target: Path) -> None:
    import hashlib
    from godot_qa_toolkit.measurement.recipe import build_recipe, recipe_hash
    from godot_qa_toolkit.mutation.ops import _collect_mutations

    recipe, err = build_recipe(root)
    assert err is None, err
    raw = recipe_hash({k: v for k, v in recipe.items() if k != "recipe_hash"})
    src = target.read_text(encoding="utf-8")
    lines = sorted({m.line for m in _collect_mutations(src, "calc.gd")})
    idents = [f"t_hot.gd::test_{fn}" for fn, _e in HOT_FUNCS + EQUIV_FUNCS]
    idents += [f"t_w{i:02d}.gd::test_w{i:02d}_{fn}"
               for i in range(10)
               for fn, _e in [(HOT_FUNCS + EQUIV_FUNCS)[i % len(HOT_FUNCS
                                                                + EQUIV_FUNCS)]]]
    line_to_tests = {ln: sorted(set(idents)) for ln in lines}
    test_to_lines: dict[str, dict[str, list[int]]] = {}
    for ln, ids in line_to_tests.items():
        for ident in ids:
            test_to_lines.setdefault(ident, {}).setdefault("calc.gd",
                                                           []).append(ln)
    face = {}
    for i in range(10):
        f = f"t_w{i:02d}.gd"
        face[f"tests/unit/{f}"] = hashlib.sha256(
            (root / "tests" / "unit" / f).read_bytes()).hexdigest()
    face["tests/unit/t_hot.gd"] = hashlib.sha256(
        (root / "tests" / "unit" / "t_hot.gd").read_bytes()).hexdigest()
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
           "instrumentable_lines": {"calc.gd": list(range(1, 45))},
           "arms": {"gut_full": arm},
           "arms_state": {"gut_full": {"state": "valid", "bundles": 1,
                                       "pairs": len(line_to_tests)
                                       * len(idents), "probes_fired": 1}},
           "notes": []}
    out = root / ".qa-cache" / "attribution-map.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def _env(order: bool, godot: str) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["GQT_GODOT_BIN"] = godot
    env["GQT_MUTATION_WORKER"] = "0"
    env["GQT_MUTATION_TEST_ORDER"] = "0"
    env["GQT_ATTRIBUTION_MAP_MODE"] = "off"
    if order:
        env["GQT_MUTATION_EQUIV_PRUNE"] = "1"
    else:
        env.pop("GQT_MUTATION_EQUIV_PRUNE", None)
    return env


def _run(root: Path, target: Path, prune: bool, budget: int, godot: str,
         tests_glob: str | None = None, rel: str = "calc.gd",
         timeout_s: int = 600) -> dict:
    cmd = [sys.executable, "-m", "godot_qa_toolkit.cli", "mutation",
           str(target), "--project-root", str(root), "--budget", str(budget),
           "--mutate-all", "--timeout", "90"]
    if tests_glob:
        cmd += ["--tests", tests_glob]
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=str(root), env=_env(prune, godot),
                           capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {"completed": False, "error": "run_timeout",
                "wall_s": timeout_s}
    wall = round(time.monotonic() - t0, 2)
    try:
        out = json.loads(p.stdout)
    except ValueError:
        return {"completed": False, "error": "non_json_stdout", "rc":
                p.returncode, "wall_s": wall}
    s = out.get("summary") or {}
    return {
        "completed": p.returncode in (0, 1)
        and not (s.get("run_error") or s.get("error")),
        "ok": bool(out.get("ok")), "rc": p.returncode, "wall_s": wall,
        "killed": int(s.get("killed") or 0),
        "survived": int(s.get("survived") or 0),
        "equivalent_pruned": s.get("equivalent_pruned"),
        "verdict_map": _verdict_map(root, rel),
    }


def _verdict_map(root: Path, rel: str) -> dict[str, str]:
    from godot_qa_toolkit.mutation.resume2 import RESUME_V2_DIRNAME
    out: dict[str, str] = {}
    for manifest in (root / ".qa-cache" / RESUME_V2_DIRNAME).glob(
            "*/manifest.json"):
        try:
            doc = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if doc.get("rel") != rel:
            continue
        for entry in manifest.parent.glob("*.json"):
            if entry.name == "manifest.json":
                continue
            try:
                rec = json.loads(entry.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict) and rec.get("id"):
                out[str(rec["id"])] = str(rec.get("verdict") or "")
    return out


def _clear_store(root: Path) -> None:
    """两臂间 store 隔离：清空 resume v2 store，使 on 臂 verdict_map 只含
    本臂 verdict——共享 store 会继承 off 臂条目，剪枝位点 id 在 on 臂
    verdict 中必然在场，`pruned_ids_absent_from_on_verdicts` 恒 False
    （SEE-1615 QA LOW F1）。off 臂 verdict_map 已在清空前快照。"""
    import shutil
    from godot_qa_toolkit.mutation.resume2 import RESUME_V2_DIRNAME
    shutil.rmtree(root / ".qa-cache" / RESUME_V2_DIRNAME,
                  ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=48)
    ap.add_argument("--godot-bin", default=DEFAULT_GODOT)
    ap.add_argument("--kol-addons", default=None,
                    help="真实 GUT addon 源（addons/gut；合成 fixture 用）")
    ap.add_argument("--kol-root", default=None,
                    help="KOL /tmp 忠实副本（已 --import）")
    ap.add_argument("--kol-target", default="utils/container_utils.gd")
    ap.add_argument("--kol-tests", default=(
        "res://tests/unit/container/test_container_utils.gd,"
        "res://tests/unit/effect/test_effect_manager.gd"))
    ap.add_argument("--kol-budget", type=int, default=24)
    ap.add_argument("--report", default="perf2-report.json")
    args = ap.parse_args(argv)
    if not Path(args.godot_bin).is_file():
        raise SystemExit(f"godot binary missing: {args.godot_bin}")
    if not args.kol_addons:
        raise SystemExit("missing --kol-addons (real GUT addon source, "
                         "e.g. <KingOfLikes-Godot>/addons/gut)")
    from godot_qa_toolkit.mutation.equivalent import (
        _RULE_PROOFS, RULE_EQ_ARMSWAP_SYMMETRIC, RULE_EQ_ZERO_ADD,
        RULE_EQ_ZERO_SUB)

    kol_addons = Path(args.kol_addons).resolve()
    root, target = build_fixture(Path(tempfile.mkdtemp(prefix="see1615-p2-")),
                                 kol_addons)
    report: dict = {"tool": "see1615-perf2", "rules": [], "tracks": {}}
    for rule in (RULE_EQ_ZERO_ADD, RULE_EQ_ZERO_SUB,
                 RULE_EQ_ARMSWAP_SYMMETRIC):
        report["rules"].append({"rule": rule, "proof": _RULE_PROOFS[rule]})

    off = _run(root, target, False, args.budget, args.godot_bin)
    _clear_store(root)
    on = _run(root, target, True, args.budget, args.godot_bin)
    pruned_on = (on.get("equivalent_pruned") or {})
    common = {i for i in off.get("verdict_map", {}) if i in on.get("verdict_map", {})}
    report["tracks"]["synthetic"] = {
        "fixture": str(root),
        "off": {k: off.get(k) for k in ("completed", "rc", "wall_s",
                                        "killed", "survived")},
        "on": {k: on.get(k) for k in ("completed", "rc", "wall_s",
                                      "killed", "survived",
                                      "equivalent_pruned")},
        "on_store_isolated": True,
        "verdict_common_ids_consistent": all(
            off["verdict_map"][i] == on["verdict_map"][i] for i in common),
        "common_ids": len(common),
        "pruned_ids_absent_from_on_verdicts": all(
            p.get("stable_id") not in on.get("verdict_map", {})
            for p in (pruned_on.get("items") or [])),
        "wall_delta_s": round(on["wall_s"] - off["wall_s"], 2),
    }
    if args.kol_root:
        kol = Path(args.kol_root).resolve()
        tests = [t.strip() for t in args.kol_tests.split(",") if t.strip()]
        from godot_qa_toolkit.mutation.truth_select import materialize_selection
        sel = materialize_selection(str(kol), tests)
        koff = _run(kol, kol / args.kol_target, False, args.kol_budget,
                    args.godot_bin, tests_glob=sel,
                    rel=args.kol_target, timeout_s=600)
        _clear_store(kol)
        kon = _run(kol, kol / args.kol_target, True, args.kol_budget,
                   args.godot_bin, tests_glob=sel,
                   rel=args.kol_target, timeout_s=600)
        kpruned = (kon.get("equivalent_pruned") or {})
        kcommon = {i for i in koff.get("verdict_map", {})
                   if i in kon.get("verdict_map", {})}
        report["tracks"]["kol"] = {
            "kol_root": str(kol), "target": args.kol_target,
            "selection": sel,
            "off": {k: koff.get(k) for k in ("completed", "rc", "wall_s",
                                             "killed", "survived")},
            "on": {k: kon.get(k) for k in ("completed", "rc", "wall_s",
                                           "killed", "survived",
                                           "equivalent_pruned")},
            "on_store_isolated": True,
            "verdict_common_ids_consistent": all(
                koff["verdict_map"][i] == kon["verdict_map"][i]
                for i in kcommon),
            "common_ids": len(kcommon),
            "pruned_ids_absent_from_on_verdicts": all(
                p.get("stable_id") not in kon.get("verdict_map", {})
                for p in (kpruned.get("items") or [])),
            "wall_delta_s": round(kon["wall_s"] - koff["wall_s"], 2),
        }
    Path(args.report).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    import shutil
    shutil.rmtree(root, ignore_errors=True)
    print(json.dumps(report, ensure_ascii=False, indent=2)[:1500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
