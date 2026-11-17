#!/usr/bin/env python3
"""SEE-1594 §SPEC-002..008 — mutation A-face acceptance harness.

单一声明式入口：读取 `mutation_acceptance_matrix.json`，在真实 KOL 副本上
逐 case 执行 gqt CLI / 探针 / 等价对账，输出机器可判定 PASS/FAIL 报告。

设计约束（AGENTS / Owner）：
- **复用环境装配**：`_env` / `DEFAULT_GODOT` / `REPO` / `SRC` / `_sites` /
  `_map_line_coverage` 全部取自 `run_see1589_benchmark.py`；map/事实/worker
  选点复用 `run_see1589_e2e.py`。不造第二套并行装配逻辑。
- **不得 SKIP 放行**：未跑到 / 超时 / 找不到样本一律 FAIL（不得当成通过）。
- **KOL 只读**：本 harness 只读 `--kol-root`；所有写入发生在副本自身
  `.qa-cache` 与影子槽（gqt 既有契约）。副本创建/清理由调用方负责。
- **fault 用例自动备份/还原**：破坏性 pre-op 只碰副本内路径，逐 case 还原。

用法：
    python tests/integration/run_mutation_acceptance.py \
        --kol-root /tmp/see1594-kol \
        --matrix tests/integration/mutation_acceptance_matrix.json \
        [--profile matrix|zero-config|fault-injection|output-contract|equivalence|final-e2e] \
        [--target utils/csv_tmap_parser.gd] [--budget 2] [--godot-bin PATH] [--out OUT]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

from run_see1589_benchmark import (DEFAULT_GODOT, REPO, SRC, _env,
                                   _map_line_coverage, _sites)
import run_see1589_e2e as e2e

API = [sys.executable, "-m", "godot_qa_toolkit.cli"]
HERE = Path(__file__).resolve().parent
DEFAULT_MATRIX = HERE / "mutation_acceptance_matrix.json"
PROFILES = ("matrix", "zero-config", "fault-injection", "output-contract",
            "equivalence", "final-e2e")


# --------------------------------------------------------------------------- #
# 执行与解析
# --------------------------------------------------------------------------- #
def _run_raw(argv: list[str], env: dict, timeout: float = 600.0,
             cwd: Path | None = None) -> dict:
    """执行 gqt CLI，保留 raw stdout/stderr 以便判定「单 JSON / 无 traceback」。"""
    cmd = API + argv
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=str(cwd or REPO), env=env,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return {"cmd": argv, "rc": None,
                "wall_s": round(time.monotonic() - t0, 3),
                "stdout": (e.stdout or "") if isinstance(e.stdout, str) else "",
                "stderr": (e.stderr or "") if isinstance(e.stderr, str) else "",
                "timeout": True}
    return {"cmd": argv, "rc": proc.returncode,
            "wall_s": round(time.monotonic() - t0, 3),
            "stdout": proc.stdout, "stderr": proc.stderr, "timeout": False}


def _parse_stdout(stdout: str) -> tuple[dict | None, str | None]:
    text = (stdout or "").strip()
    if not text:
        return None, "empty stdout"
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"non-json stdout: {e}"
    if not isinstance(doc, dict):
        return None, "stdout JSON is not an object"
    return doc, None


def _summary_of(doc: dict | None) -> dict:
    if not isinstance(doc, dict):
        return {}
    if isinstance(doc.get("results"), list) and doc["results"]:
        return doc["results"][0].get("summary") or {}
    return doc.get("summary") or {}


def _dig(doc, dotted: str):
    cur = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


class _Missing:
    def __repr__(self) -> str:
        return "<missing>"


_MISSING = _Missing()


# --------------------------------------------------------------------------- #
# fault 用例的路径备份/还原
# --------------------------------------------------------------------------- #
class CaseSandbox:
    """逐 case 备份/还原被破坏的副本内路径，保证 fault 用例可反复执行。"""

    def __init__(self, kol: Path) -> None:
        self.kol = kol
        self.root = kol / ".qa-cache" / "see1594-acceptance-backup"
        self._saved: list[tuple[Path, Path | None]] = []

    def _snapshot(self, path: Path) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        rel = path.relative_to(self.kol).as_posix().replace("/", "__")
        dest = self.root / (rel + f".{len(self._saved)}")
        if path.is_dir():
            shutil.copytree(path, dest)
            self._saved.append((path, dest))
        elif path.is_file():
            shutil.copy2(path, dest)
            self._saved.append((path, dest))
        else:
            self._saved.append((path, None))  # 原本不存在

    def snapshot_file_or_mark_new(self, path: Path) -> None:
        """写前只备份目标文件本身（不备份父目录——`.qa-cache` 含大槽树）。"""
        if path.exists():
            self._snapshot(path)
        else:
            self._saved.append((path, None))

    def restore(self) -> None:
        for path, saved in reversed(self._saved):
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                elif path.exists():
                    path.unlink()
            except OSError:
                pass
            if saved is None:
                continue
            try:
                if saved.is_dir():
                    shutil.copytree(saved, path)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(saved, path)
            except OSError:
                pass
        self._saved = []
        shutil.rmtree(self.root, ignore_errors=True)


def _apply_ops(ops: list[dict], ctx: dict, sandbox: CaseSandbox) -> None:
    for op in ops or []:
        kind = op.get("op")
        path = Path(_sub(op["path"], ctx)) if op.get("path") else None
        if kind == "rm" and path is not None:
            sandbox._snapshot(path)
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
        elif kind == "write" and path is not None:
            sandbox.snapshot_file_or_mark_new(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_sub(op.get("text", ""), ctx), encoding="utf-8")
        elif kind == "mkdir" and path is not None:
            sandbox._snapshot(path)
            path.mkdir(parents=True, exist_ok=True)
        elif kind == "chmod" and path is not None:
            sandbox._snapshot(path)
            os.chmod(path, int(op.get("mode", 0o000)))
        elif kind == "truncate" and path is not None:
            sandbox._snapshot(path)
            path.write_bytes(b"" if not op.get("text") else
                             _sub(op["text"], ctx).encode())


def _sub(value, ctx: dict):
    if isinstance(value, str):
        def repl(m):
            key = m.group(1)
            return str(ctx.get(key, m.group(0)))
        return re.sub(r"\{([a-zA-Z0-9_]+)\}", repl, value)
    if isinstance(value, list):
        return [_sub(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: _sub(v, ctx) for k, v in value.items()}
    return value


# --------------------------------------------------------------------------- #
# 期望判定
# --------------------------------------------------------------------------- #
def _eval_expect(res: dict, expect: dict, checks: "Checks", cid: str) -> None:
    """把 expect 声明翻译为断言；任何不满足即 FAIL（detail 给可定位信息）。"""
    fails: list[str] = []
    doc = res.get("doc")

    rc = res["rc"]
    if res.get("timeout"):
        fails.append(f"timeout after {res.get('wall_s')}s")
    if "rc" in expect:
        allowed = expect["rc"]
        allowed = allowed if isinstance(allowed, list) else [allowed]
        if isinstance(expect["rc"], str) and expect["rc"] == "nonzero":
            if rc == 0:
                fails.append(f"rc expected nonzero, got {rc}")
        elif rc not in allowed:
            fails.append(f"rc {rc} not in {allowed}")

    if "json" in expect:
        if expect["json"] and res.get("parse_error"):
            fails.append(f"expected JSON, parse_error={res['parse_error']}")
        if expect["json"] is False and res.get("parse_error") is None:
            fails.append("expected non-JSON stdout but parsed a JSON object")

    if expect.get("single_json") and res.get("parse_error") is None:
        # stdout 必须恰好一个 JSON 对象：整段 strip 后能解析即视为单一对象；
        # 再确认没有第二个顶格 '{' 起始的残留块。
        body = (res.get("stdout") or "").strip()
        if body.startswith("{") and body.endswith("}") and body.count("\n{") > 1:
            fails.append("stdout contains more than one JSON object")

    if expect.get("no_traceback"):
        if "Traceback (most recent call last)" in (res.get("stderr") or ""):
            fails.append("traceback present in stderr")

    if "tool" in expect and isinstance(doc, dict):
        if doc.get("tool") != expect["tool"]:
            fails.append(f"tool {doc.get('tool')!r} != {expect['tool']!r}")

    if "ok" in expect and isinstance(doc, dict):
        if doc.get("ok") != expect["ok"]:
            fails.append(f"ok {doc.get('ok')} != {expect['ok']}")

    for key in expect.get("top_keys", []):
        if not isinstance(doc, dict) or key not in doc:
            fails.append(f"top-level key missing: {key}")

    summary = _summary_of(doc) if isinstance(doc, dict) else {}
    for path, want in (expect.get("summary_equals") or {}).items():
        got = _dig(summary if not path.startswith("$doc.") else doc,
                   path.removeprefix("$doc."))
        if got is _MISSING or got != want:
            fails.append(f"summary.{path} = {got!r} != {want!r}")
    for path in expect.get("summary_present", []):
        base = doc if path.startswith("$doc.") else summary
        key = path.removeprefix("$doc.")
        if _dig(base, key) is _MISSING:
            fails.append(f"summary path missing: {path}")
    for path in expect.get("summary_truthy", []):
        got = _dig(summary, path)
        if got is _MISSING or not got:
            fails.append(f"summary.{path} not truthy: {got!r}")
    for path, allowed in (expect.get("summary_in") or {}).items():
        got = _dig(summary, path)
        if got is _MISSING or got not in allowed:
            fails.append(f"summary.{path}={got!r} not in {allowed}")

    for rgx in expect.get("failures_reason_regex", []):
        reasons = " ".join(str(f.get("reason", ""))
                           for f in (doc or {}).get("failures") or [])
        if not re.search(rgx, reasons):
            fails.append(f"failures reason !~ /{rgx}/ (reasons={reasons[:200]!r})")

    for rgx in expect.get("stdout_regex", []):
        if not re.search(rgx, res.get("stdout") or ""):
            fails.append(f"stdout !~ /{rgx}/")
    for rgx in expect.get("stderr_regex", []):
        if not re.search(rgx, res.get("stderr") or ""):
            fails.append(f"stderr !~ /{rgx}/")

    checks.add(cid, not fails, detail=", ".join(fails) or "ok",
               evidence={"cmd": res.get("cmd"), "rc": rc,
                         "wall_s": res.get("wall_s"),
                         "parse_error": res.get("parse_error"),
                         "summary_keys": sorted(summary)[:30],
                         "stderr_tail": (res.get("stderr") or "").strip()
                         .splitlines()[-4:]})


class Checks:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, cid: str, ok: bool, detail: str, evidence=None) -> None:
        self.items.append({"id": cid, "status": "PASS" if ok else "FAIL",
                           "detail": detail, "evidence": evidence or {}})

    def fail_exc(self, cid: str, exc: Exception) -> None:
        self.add(cid, False, f"{type(exc).__name__}: {exc}",
                 {"traceback": traceback.format_exc()[-1500:]})

    @property
    def ok(self) -> bool:
        return all(i["status"] == "PASS" for i in self.items)


# --------------------------------------------------------------------------- #
# 派生事实（复用 e2e）
# --------------------------------------------------------------------------- #
def _build_context(kol: Path, target: str, budget: int, godot: str | None,
                   checks: Checks) -> dict:
    # 优先复用已就绪的默认映射（重跑加速）；不可用时才重新 measure（首次冷建）。
    default_map = kol / ".qa-cache" / "attribution-map.json"
    map_path = None
    if default_map.is_file():
        try:
            e2e._map_facts(str(default_map), target)
            map_path = str(default_map)
            checks.add("ctx-map", True, "reused existing attribution map")
        except Exception:  # noqa: BLE001 — 失效则回退重建
            map_path = None
    if map_path is None:
        map_path, measure = e2e._ensure_map(kol, target, godot, checks)
        summary = e2e._summary(measure.get("doc"))
        if (measure.get("rc") != 0 or not Path(map_path).is_file()
                or summary.get("attribution_usable") is not True):
            checks.add("ctx-map", False,
                       f"cannot build usable map rc={measure.get('rc')}",
                       {"summary": summary})
            return {}
        checks.add("ctx-map", True, "usable attribution map built")
    facts = e2e._map_facts(map_path, target)
    stable, site_line = e2e._manifest_facts(kol, target)
    ctx = {
        "kol": str(kol), "target": target, "budget": str(budget),
        "map": map_path, "stable": stable or "", "line": str(site_line or ""),
        "testdir": (facts["test_dirs"][0] if facts["test_dirs"] else ""),
        "zero_ref": e2e._zero_ref_target(kol, facts) or "",
        "worker_target": e2e._worker_target(kol, facts) or "",
        "face": target.split("/", 1)[0],
    }
    return ctx


# --------------------------------------------------------------------------- #
# 等价对账（§SPEC-007）
# --------------------------------------------------------------------------- #
def _verdict_map(doc: dict | None) -> dict:
    """从 CLI 输出提取 stable-id → verdict 映射。

    killed 位点不在 failures[]，其证书见 summary.killed_by_certificates；非
    killed 位点见 failures[].id/verdict。两源并集即完整逐 id 判定面。
    """
    out: dict[str, str] = {}
    summary = _summary_of(doc)
    certs = summary.get("killed_by_certificates") or {}
    if isinstance(certs, dict):
        for sid in certs:
            out[str(sid)] = "killed"
    for rec in (doc or {}).get("failures") or []:
        sid = rec.get("id")
        if sid:
            out[str(sid)] = str(rec.get("verdict") or "?")
    return out


def _run_case_cli(case: dict, ctx: dict, env_base: dict,
                  sandbox: CaseSandbox, checks: Checks) -> None:
    cid = case["id"]
    argv = [_sub(a, ctx) for a in case["argv"]]
    env = dict(env_base)
    env.update({k: _sub(v, ctx) for k, v in (case.get("env") or {}).items()})
    cwd = Path(_sub(case["cwd"], ctx)) if case.get("cwd") else None
    _apply_ops(case.get("pre") or [], ctx, sandbox)
    try:
        res = _run_raw(argv, env, timeout=float(case.get("timeout", 300)),
                       cwd=cwd)
    finally:
        sandbox.restore()
    doc, perr = _parse_stdout(res["stdout"])
    res["doc"], res["parse_error"] = doc, perr
    _eval_expect(res, case.get("expect") or {}, checks, cid)


def _run_case_probe(case: dict, ctx: dict, checks: Checks) -> None:
    cid = case["id"]
    name = case["probe"]
    want = case.get("expect") or {}
    ok, detail, evidence = _PROBES[name](ctx, want)
    checks.add(cid, ok, detail, evidence)


_TOTAL_KEYS = ("mutants", "killed", "survived", "timeout", "run_errors",
               "suspect", "invalid_mutants")


def _face_totals(doc: dict | None) -> dict:
    s = _summary_of(doc)
    return {k: s.get(k) for k in _TOTAL_KEYS}


def _failure_ids(doc: dict | None) -> dict:
    return {str(r.get("id")): str(r.get("verdict"))
            for r in (doc or {}).get("failures") or [] if r.get("id")}


def _run_case_equivalence(case: dict, ctx: dict, env_base: dict,
                          checks: Checks) -> None:
    """§SPEC-007：等价面判定。

    判据用「计数元组 + 非 killed 明细 id→verdict」对账：在相同执行面
    （mutate-all / budget≥位点数 / cold 与 worker）下 reached 集一致，故
    killed 集 = reached \\ failures；计数与 failures 全等 ⟺ 逐 stable id
    verdict 等价。diff 复用轮的 executed 子集不同但 killed/survived 总量与
    failures 不变，故不比较 executed。任何 verdict 翻转即 FAIL。"""
    cid = case["id"]
    runs = case.get("runs") or []
    totals: list[dict] = []
    fail_ids: list[dict] = []
    vmaps: list[dict] = []
    evidences: list[dict] = []
    for run in runs:
        argv = [_sub(a, ctx) for a in run["argv"]]
        env = dict(env_base)
        env.update({k: _sub(v, ctx) for k, v in (run.get("env") or {}).items()})
        cwd = Path(_sub(run["cwd"], ctx)) if run.get("cwd") else None
        res = _run_raw(argv, env, timeout=float(run.get("timeout", 420)),
                       cwd=cwd)
        doc, perr = _parse_stdout(res["stdout"])
        if perr is not None:
            checks.add(cid, False,
                       f"{run.get('label')}: non-json rc={res['rc']} {perr}",
                       {"evidence": evidences + [{"label": run.get("label"),
                                                  "rc": res["rc"],
                                                  "parse_error": perr}]})
            return
        totals.append(_face_totals(doc))
        fail_ids.append(_failure_ids(doc))
        vmaps.append(_verdict_map(doc))
        evidences.append({"label": run.get("label"), "rc": res["rc"],
                          "totals": totals[-1],
                          "diff_skipped": len(_summary_of(doc).get(
                              "diff_skipped") or [])})
    base_t, base_f = totals[0], fail_ids[0]
    diffs = []
    for i in range(1, len(runs)):
        if totals[i] != base_t:
            diffs.append({"run": runs[i].get("label"), "kind": "totals",
                          "base": base_t, "other": totals[i]})
        if fail_ids[i] != base_f:
            diffs.append({"run": runs[i].get("label"), "kind": "failure_ids",
                          "only_base": {k: v for k, v in base_f.items()
                                        if fail_ids[i].get(k) != v},
                          "only_other": {k: v for k, v in fail_ids[i].items()
                                         if base_f.get(k) != v}})
        flipped = {k: (vmaps[0][k], vmaps[i][k])
                   for k in vmaps[0].keys() & vmaps[i].keys()
                   if vmaps[0][k] != vmaps[i][k]}
        if flipped:
            diffs.append({"run": runs[i].get("label"), "kind": "verdict_flip",
                          "flipped": flipped})
    nontrivial = (base_t.get("killed") or 0) + (base_t.get("survived") or 0) > 0
    checks.add(cid, not diffs and nontrivial,
               "face totals + failure ids identical, no verdict flip"
               if not diffs and nontrivial else
               ("trivial/empty face" if not nontrivial
                else f"{len(diffs)} differing run(s)"),
               {"totals": base_t, "diffs": diffs[:4], "runs": evidences})


def _probe_suggested_action(ctx: dict, want: dict):
    from godot_qa_toolkit.timing import (BUDGET_CAP_S, suggested_action,
                                         suggested_action_kind)
    below_kind = suggested_action_kind(60)
    below = suggested_action(60)
    cap_kind = suggested_action_kind(BUDGET_CAP_S)
    cap = suggested_action(BUDGET_CAP_S)
    ok = (below_kind == "raise_timeout" and f"--timeout {BUDGET_CAP_S}" in below
          and cap_kind == "narrow_face"
          and f"--timeout {BUDGET_CAP_S}" not in cap and "--select" in cap)
    return ok, ("at/below cap actionable and non-contradictory" if ok else
                "suggested_action contract broken"), {
        "below": {"kind": below_kind, "action": below},
        "cap": {"kind": cap_kind, "action": cap}}


def _probe_envelopes(ctx: dict, want: dict):
    """单文件 flat vs 多文件 envelope 的键集静态断言（dry-run，零 GUT）。

    多文件必须用两个**不同**文件：相同路径会被去重成单文件形态。"""
    env = _env(os.environ.get("GQT_GODOT_BIN"), ctx.get("map") or None)
    target = ctx["target"]
    face = target.split("/", 1)[0]
    others = [p.relative_to(Path(ctx["kol"])).as_posix()
              for p in (Path(ctx["kol"]) / face).rglob("*.gd")
              if p.relative_to(Path(ctx["kol"])).as_posix() != target]
    if not others:
        return False, "no second .gd file for multi-file envelope probe", {}
    second = others[0]
    single = _run_raw(["mutation", target, "--project-root", ctx["kol"],
                       "--dry-run", "--budget", "1"], env, timeout=120)
    multi = _run_raw(["mutation", target, second, "--project-root", ctx["kol"],
                      "--dry-run", "--budget", "1"], env, timeout=120)
    sdoc, sperr = _parse_stdout(single["stdout"])
    mdoc, mperr = _parse_stdout(multi["stdout"])
    ok = (sperr is None and isinstance(sdoc, dict) and "results" not in sdoc
          and isinstance(sdoc.get("summary"), dict)
          and mperr is None and isinstance(mdoc, dict)
          and isinstance(mdoc.get("results"), list) and len(mdoc["results"]) == 2)
    return ok, ("single flat / multi envelope split correct" if ok else
                "envelope shape wrong"), {
        "single_keys": sorted(sdoc) if isinstance(sdoc, dict) else sperr,
        "multi_keys": sorted(mdoc) if isinstance(mdoc, dict) else mperr,
        "second": second}


_PROBES = {
    "suggested_action": _probe_suggested_action,
    "envelopes": _probe_envelopes,
}


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def _select_cases(matrix: dict, profile: str | None) -> list[dict]:
    cases = matrix.get("cases") or []
    if profile is None or profile == "matrix":
        return [c for c in cases if "matrix" in (c.get("profiles") or [])]
    if profile == "final-e2e":
        # 终验 = 全量：所有 case 都跑一遍（A 全链）
        return cases
    return [c for c in cases if profile in (c.get("profiles") or [])]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kol-root", required=True)
    ap.add_argument("--matrix", default=str(DEFAULT_MATRIX))
    ap.add_argument("--profile", default=None, choices=PROFILES)
    ap.add_argument("--target", default="utils/csv_tmap_parser.gd")
    ap.add_argument("--budget", type=int, default=2)
    ap.add_argument("--godot-bin", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-context", action="store_true",
                    help="跳过 map/事实预构建（纯静态 matrix 子集用）")
    ap.add_argument("--only", default=None,
                    help="逗号分隔的 case id 白名单（快速重跑用）")
    args = ap.parse_args(argv)

    kol = Path(args.kol_root).resolve()
    if not (kol / "project.godot").is_file():
        print(json.dumps({"ok": False, "error": f"not a Godot project: {kol}"},
                         ensure_ascii=False, indent=2))
        return 2
    if not (kol / "addons/gut/gut_cmdln.gd").is_file():
        print(json.dumps({"ok": False, "error": "addons/gut missing"}, indent=2))
        return 2
    if not Path(args.matrix).is_file():
        print(json.dumps({"ok": False, "error": f"matrix missing: {args.matrix}"},
                         indent=2))
        return 2

    matrix = json.loads(Path(args.matrix).read_text(encoding="utf-8"))
    cases = _select_cases(matrix, args.profile)
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        cases = [c for c in cases if c["id"] in wanted]
    checks = Checks()
    godot = args.godot_bin
    env_base = _env(godot, None)
    env_base["GQT_GODOT_BIN"] = godot or env_base.get("GQT_GODOT_BIN")
    env_base.setdefault("GQT_E2E_TRACE", "1")

    ctx: dict = {"kol": str(kol), "target": args.target,
                 "budget": str(args.budget), "map": "", "stable": "",
                 "line": "", "testdir": "", "zero_ref": "", "worker_target": "",
                 "face": args.target.split("/", 1)[0]}
    # fault-injection 与 zero-config 都必须从「无 map / 原始状态」出发，预构建
    # map 会掩盖被测行为（尤其 zero-config 测的就是首次自动 map 引导）。
    need_ctx = args.profile not in ("fault-injection", "zero-config")
    if need_ctx and not args.no_context:
        try:
            built = _build_context(kol, args.target, args.budget, godot, checks)
            if built:
                ctx.update(built)
        except Exception as exc:  # noqa: BLE001
            checks.fail_exc("ctx-build", exc)
    env_map = _env(godot, ctx.get("map") or None)
    env_map["GQT_GODOT_BIN"] = godot or env_map.get("GQT_GODOT_BIN")

    sandbox = CaseSandbox(kol)
    for case in cases:
        cid = case["id"]
        kind = case.get("kind", "cli")
        try:
            if kind == "cli":
                _run_case_cli(case, ctx, env_map if case.get("use_map")
                              else env_base, sandbox, checks)
            elif kind == "probe":
                _run_case_probe(case, ctx, checks)
            elif kind == "equivalence":
                _run_case_equivalence(case, ctx, env_map if case.get("use_map")
                                      else env_base, checks)
            else:
                checks.add(cid, False, f"unknown kind {kind}")
        except Exception as exc:  # noqa: BLE001
            checks.fail_exc(cid, exc)
    sandbox.restore()

    # 覆盖缺口：矩阵声明 vs 实跑（不允许 SKIP 放行）
    ran = {c["id"] for c in cases}
    report = {
        "ok": checks.ok,
        "profile": args.profile or "matrix",
        "kol_root": str(kol),
        "target": args.target,
        "matrix": str(args.matrix),
        "cases_run": len(ran),
        "cases_total": len(matrix.get("cases") or []),
        "godot_bin": godot or env_base.get("GQT_GODOT_BIN"),
        "checks": checks.items,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0 if checks.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
