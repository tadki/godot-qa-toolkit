"""SEE-1654 harden-R1：§SPEC-035 / 036 / 037 结构判据（v3 判据原文的仓内
常驻形态）。

判据来源：issue 附件 `map-contract-structure-check.py`（v3）——S1–S13 逐条
原样搬入；本文件只把扫描根参数化（`--src`，默认仓库 `src/godot_qa_toolkit`），
便于在同一判据下对历史 head 做红绿咬合。常驻调用方：
`tests/unit/test_see1654_structure_pin.py`（对当前树断言
`D1-STRUCTURE PASS`）。

最后一行 `D1-STRUCTURE PASS` 且 exit 0 才算通过。

用法：
    python3 tests/integration/check_see1654_structure.py [--src <godot_qa_toolkit 目录>]
"""
import argparse
import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SRC = REPO_ROOT / "src" / "godot_qa_toolkit"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(DEFAULT_SRC),
                    help="godot_qa_toolkit 包目录（默认仓库 src/）")
    args = ap.parse_args()
    src_root = Path(args.src).resolve()

    def rel_of(p: Path) -> str:
        return str(p.relative_to(src_root.parent)) if \
            src_root.parent in p.parents else str(p)

    # 判据里的路径字面量统一以 `<P>/...` 形态出现；P 与 TREES 的键同一来源。
    P = rel_of(src_root)
    INTERNAL = {f"{P}/mutation/{n}.py"
                for n in ("map_contract", "verdict_source", "truth_select")}
    FILES = sorted(src_root.rglob("*.py"))
    TREES = {rel_of(p): ast.parse(p.read_text(encoding="utf-8"))
             for p in FILES}
    RAW = {rel_of(p): p for p in FILES}
    ALIAS = {(rel_of(f), a.asname or a.name): a.name for f in FILES
             for n in ast.walk(TREES[rel_of(f)])
             if isinstance(n, ast.ImportFrom) for a in n.names}

    def calls(names):
        out = []
        for f, tree in TREES.items():
            for n in ast.walk(tree):
                if isinstance(n, ast.Call):
                    fn = n.func
                    nm = fn.id if isinstance(fn, ast.Name) else \
                        fn.attr if isinstance(fn, ast.Attribute) else None
                    nm = ALIAS.get((f, nm), nm)
                    if nm in names:
                        out.append((f, n))
        return out

    def func(path, name):
        return next((n for n in ast.walk(TREES[path])
                     if isinstance(n, ast.FunctionDef) and n.name == name),
                    None)

    def names(node):
        return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | \
               {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}

    def rx(pattern):
        return sum(len(re.findall(pattern, p.read_text(encoding="utf-8")))
                   for p in FILES)

    def rx_files(pattern):
        return sorted(f for f in TREES
                      if re.search(pattern, RAW[f].read_text(encoding="utf-8")))

    def calls_to(name, node):
        return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == name for n in ast.walk(node))

    TS, MC, VS = (f"{P}/mutation/truth_select.py",
                  f"{P}/mutation/map_contract.py",
                  f"{P}/mutation/verdict_source.py")
    s1 = [f for f, _ in calls({"ensure_map"}) if f not in INTERNAL]
    s2 = rx(r"_explicit_map_stale")
    mpv_refs, mpv = rx(r"\bmap_preview_state\b"), func(TS, "map_preview_state")
    s3 = (mpv_refs == 0) or (mpv is not None and calls_to("status", mpv)
                             and "_evaluate" not in names(mpv))
    SELECTS = {"_resolve_auto_glob", "_auto_scan_tests_glob_meta",
               "_auto_scan_tests_glob", "resolve_selection"}
    s4 = sorted(f"{f}:{n.lineno}" for f, n in calls(SELECTS)
                if not [k for k in n.keywords
                        if k.arg in ("renew", "allow_renew")
                        and not (isinstance(k.value, ast.Constant)
                                 and k.value.value is True)])
    st = func(MC, "status")
    s5 = st is not None and calls_to("MapView", st)
    s6_raw = sorted(f"{f}:{n.lineno}" for f, n in calls({"load_map"})
                    if f not in INTERNAL)
    s6_path = sorted(f"{f}:{n.lineno}" for f, n in calls({"map_path"})
                     if f not in INTERNAL)
    s6_views = len(calls({"tests_for", "executable_reds", "gherkin_gate",
                          "projection_inputs"}))
    # v3：契约模块对外的函数不得以“路径”为参数（否则等于换名字的裸读入口）。
    s6_pathargs = sorted(
        f"{MC}:{n.name}({', '.join(a.arg for a in n.args.args + n.args.kwonlyargs)})"
        for n in TREES[MC].body
        if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")
        for a in n.args.args + n.args.kwonlyargs if "path" in a.arg)
    s6 = not s6_raw and not s6_path and not s6_pathargs
    pb = func(VS, "prepare_map_for_baseline")
    s7 = pb is not None and calls_to("refresh", pb) and \
        not (names(pb) & {"ensure_map", "_auto_increment", "_map_drift"})
    s8 = sum(1 for t in TREES.values() for n in ast.walk(t)
             if isinstance(n, ast.FunctionDef) and n.name == "static_plan") == 1
    s9 = [f"{f}:{n.lineno}" for f, n in calls({"MapView"}) if f != MC]

    RUN_SIDE = ("mutation/runner.py", "mutation/executor.py",
                "mutation/runhelp.py", "mutation/planning.py",
                "mutation/truth_lines.py", "mutation/worker_channel.py")

    def run_side_refresh_calls():
        """S10: run / plan 侧模块不得调用 map_contract.refresh。"""
        hits = []
        for rel in RUN_SIDE:
            f = f"{P}/{rel}"
            if f not in TREES:
                continue
            local, mods = set(), {"map_contract"}
            for n in ast.walk(TREES[f]):
                if isinstance(n, ast.ImportFrom) and \
                        str(n.module or "").endswith("map_contract"):
                    local |= {a.asname or a.name for a in n.names
                              if a.name == "refresh"}
                if isinstance(n, ast.Import):
                    mods |= {(a.asname or a.name).split(".")[-1]
                             for a in n.names if a.name.endswith("map_contract")}
            for n in ast.walk(TREES[f]):
                if isinstance(n, ast.Call):
                    fn = n.func
                    if (isinstance(fn, ast.Name) and fn.id in local) or (
                            isinstance(fn, ast.Attribute) and fn.attr == "refresh"
                            and isinstance(fn.value, ast.Name)
                            and fn.value.id in mods):
                        hits.append(f"{f}:{n.lineno}")
        return sorted(hits)

    s10 = run_side_refresh_calls()

    # §SPEC-036（定案 20）：手动指定地图位置的入口在 src 里零残留。
    MANUAL_MAP_SURFACES = {
        "--attribution-map": r"--attribution-map\b",
        "--no-attribution-auto": r"--no-attribution-auto\b",
        "--test-attribution-out": r"--test-attribution-out\b",
        "attribution-map dest":
            r"getattr\(\s*args\s*,\s*[\"']attribution_map[\"']|args\.attribution_map\b",
        "env GQT_ATTRIBUTION_MAP": r"GQT_ATTRIBUTION_MAP\b(?!_)",
        "map_path_override": r"\bmap_path_override\b",
        "ENV_MAP": r"\bENV_MAP\b",
        "explicit map source":
            r"_explicit_map_source|_explicit_stale_gate|explicit_map_stale",
    }
    _INTERNAL_TEXT = {f: RAW[f].read_text(encoding="utf-8")
                      for f in INTERNAL if f in TREES}
    s11 = {k: rx_files(v) for k, v in MANUAL_MAP_SURFACES.items()}
    s11["ensure_map out_path"] = sorted(
        f for f, t in _INTERNAL_TEXT.items() if re.search(r"\bout_path\b", t))
    s11 = {k: v for k, v in s11.items() if v}

    # §SPEC-036（定案 20）：`measure map-update` 只做契约 refresh 的薄适配。
    CLI = f"{P}/cli.py"
    MU = func(CLI, "cmd_measure_map_update")
    _CLI_STRINGS = {n.value for n in ast.walk(TREES[CLI])
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    s13 = (MU is not None
           and not ({"--baseline", "--changed"} & _CLI_STRINGS)
           and not (names(MU) & {"baseline", "out", "recipe", "changed",
                                 "roots", "run_attribution_increment"}))

    def parser_positionals(parser_name):
        """v3：某个子命令解析器上声明的位置参数名。"""
        var, out = None, []
        for tree in TREES.values():
            for n in ast.walk(tree):
                if (isinstance(n, ast.Assign) and len(n.targets) == 1
                        and isinstance(n.targets[0], ast.Name)
                        and isinstance(n.value, ast.Call)
                        and isinstance(n.value.func, ast.Attribute)
                        and n.value.func.attr == "add_parser"
                        and n.value.args
                        and isinstance(n.value.args[0], ast.Constant)
                        and n.value.args[0].value == parser_name):
                    var = n.targets[0].id
        if var is None:
            return None
        for tree in TREES.values():
            for n in ast.walk(tree):
                if (isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                        and isinstance(n.value.func, ast.Attribute)
                        and n.value.func.attr == "add_argument"
                        and isinstance(n.value.func.value, ast.Name)
                        and n.value.func.value.id == var):
                    out += [a.value for a in n.value.args
                            if isinstance(a, ast.Constant)
                            and isinstance(a.value, str)
                            and not a.value.startswith("-")]
        return out

    s13_positionals = parser_positionals("map-update")
    s13 = s13 and s13_positionals == []

    # §SPEC-037（定案 21）：不得以“增量产物”为由拒绝使用（增量内部披露除外）。
    s12_files = [f for f in rx_files(r"event_counts_stale")
                 if not re.search(r"/measurement/increment(_runner)?\.py$", f)]

    ROWS = [("S1 two_maintenance_actions", not s1),
            ("S2 explicit_map_via_status", s2 == 0),
            ("S3 plan_via_status", s3),
            ("S4 renew_explicit_and_false", not s4),
            ("S5 status_returns_view", s5),
            ("S6 reads_via_contract", s6),
            ("S7 prepare_is_thin", s7),
            ("S8 static_plan_single", s8),
            ("S9 view_builtin_only", not s9),
            ("S10 run_side_never_refreshes", not s10),
            ("S11 no_manual_map_surface", not s11),
            ("S12 no_incremental_unusable_gate", not s12_files),
            ("S13 map_update_no_path_args", s13)]
    for key, ok in ROWS:
        print(f"{key:34s} {'PASS' if ok else 'FAIL'}")
    print("S1 hits:", [f"{f}:{n.lineno}" for f, n in calls({"ensure_map"})
                       if f not in INTERNAL])
    print("S2 hits:", s2, "| S3 map_preview_state refs:", mpv_refs)
    print("S4 call sites renewing by default or passing True:", s4)
    print("S9 hits:", s9, "| S10 run/plan-side refresh calls:", s10)
    print("S6 raw load_map outside map module:", s6_raw,
          "| map_path outside map module:", s6_path,
          "| view method calls:", s6_views)
    print("S11 manual-map surfaces:", s11)
    print("S12 files gating on event_counts_stale outside increment:",
          s12_files)
    print("S13 map-update handler uses roots/baseline/out/recipe/changed or "
          "direct increment:", not s13,
          "| map-update positional args:", s13_positionals)
    print("S6 contract functions taking a path parameter:", s6_pathargs)
    ok = all(ok for _, ok in ROWS)
    print("D1-STRUCTURE", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
