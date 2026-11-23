"""SEE-1615 §SPEC-PLANFIRST —— plan 三段式第一段：静态穷查（只读 ≤10s）。

默认 run 的第一步也是 `--dry-run` 的全部内容：静态能查的问题全部记录为
`problems[]`（severity/code/evidence/fix），全部通过才进入 prepare/run；
禁止「盲跑 10-20 分钟后弹 run_error」。

分级：
- `error`（阻断 run）：binary 缺失、`project.godot` autoload 目标缺失、
  tests 脚本 preload/load 目标缺失（GitHub 克隆 8 分钟失败的直接根因）；
- `warning`（披露不阻断）：GUT addon / import 缓存 / 映射缺失或 stale
  （这些既有路径已具名 fail-loud 或惰性构建，且 dry-run 不消费 GUT）。

硬预算：`budget_s` 内提前收束（大目录扫描按文件逐个查钟）；深检（基线
smoke）超出预算必须显式 opt-in（`--check-baseline`），不在本段默认。
"""

from __future__ import annotations

import re
import time
from pathlib import Path

PLAN_BUDGET_S = 10.0
_SKIP_DIRS = {"addons", ".git", ".godot", ".qa-cache"}
_PRELOAD_RE = re.compile(
    r'\b(?:preload|load)\(\s*"res://([^"]+)"\s*\)')
_AUTOLOAD_RE = re.compile(r'^(\w+)="\*?res://([^"]+)"', re.M)


def _problem(severity: str, code: str, evidence: dict, fix: str,
             level: str = "P3", source: str = "static",
             auto_prepare_covered: bool = False) -> dict:
    """§SPEC-PREREQ-MATRIX：分级问题项——level P0/P1/P2/P3，
    source=静态解析来源（约定位置/固定优先级），fix=可执行修复命令，
    auto_prepare_covered=--auto-prepare 是否可自动补齐该问题。"""
    return {"severity": severity, "code": code, "level": level,
            "source": source, "evidence": evidence, "fix": fix,
            "auto_prepare_covered": auto_prepare_covered}


def _autoload_problems(root: Path) -> list[dict]:
    pg = root / "project.godot"
    if not pg.is_file():
        # warning：既有 recipe/build 阶段会具名失败；此处只披露不漏判
        return [_problem("warning", "project_godot_missing",
                         {"path": str(pg)},
                         "ensure project.godot exists at the project root",
                         level="P0", source="project root convention",
                         auto_prepare_covered=False)]
    text = pg.read_text(encoding="utf-8", errors="replace")
    missing = []
    for name, rel in _AUTOLOAD_RE.findall(text):
        if not (root / rel).is_file():
            missing.append({"autoload": name, "target": f"res://{rel}"})
    if not missing:
        return []
    return [_problem(
        "error", "autoload_target_missing",
        {"count": len(missing), "items": missing},
        "fix or remove the broken autoload entries in project.godot "
        "[autoload] (missing files make every GUT run fail late)",
        level="P0", source="project.godot [autoload] (conventional)")]


def _test_interpreter_problems(root: Path, tests_glob: str | None,
                               deadline: float) -> tuple[list[dict], bool]:
    """tests 脚本 preload/load 目标存在性（GitHub 克隆根因）。返回
    (problems, budget_exhausted)。"""
    base = root / "tests"
    if tests_glob and tests_glob.startswith("res://"):
        rel = tests_glob.split("res://", 1)[1].rstrip("/")
        base = root / rel if rel else root
    if not base.is_dir():
        return [], False
    missing: dict[str, list[str]] = {}
    scanned = 0
    exhaustive = True
    for script in base.rglob("*.gd"):
        if time.monotonic() > deadline:
            exhaustive = False
            break
        if any(part in _SKIP_DIRS for part in script.parts):
            continue
        scanned += 1
        try:
            text = script.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for rel in _PRELOAD_RE.findall(text):
            if not (root / rel).is_file():
                missing.setdefault(f"res://{rel}", []).append(
                    str(script.relative_to(root)))
    problems: list[dict] = []
    if missing:
        problems.append(_problem(
            "error", "preload_target_missing",
            {"count": len(missing),
             "items": [{"target": t, "referenced_by": sorted(set(v))[:5]}
                       for t, v in sorted(missing.items())]},
            "install the missing addon/files or fix the preload paths — "
            "these tests fail late inside GUT otherwise",
            level="P0", source="tests/**/*.gd preload/load (conventional)"))
    if not exhaustive:
        problems.append(_problem(
            "warning", "plan_budget_exhausted",
            {"scanned": scanned},
            "raise the plan budget or narrow --tests; the sweep stopped "
            "early (static checks above are what fitted in the budget)"))
    return problems, not exhaustive


def _env_problems(root: Path) -> list[dict]:
    problems: list[dict] = []
    try:
        from .inject import godot_command
        binary = godot_command()
        if not Path(binary).is_file():
            problems.append(_problem(
                "error", "godot_binary_missing", {"cmd": binary},
                "set --godot-bin / GQT_GODOT_BIN to a real Godot binary"))
    except Exception as e:  # noqa: BLE001 — 解析失败如实落 problem
        problems.append(_problem(
            "error", "godot_binary_missing",
            {"error": f"{type(e).__name__}: {e}"},
            "set --godot-bin / GQT_GODOT_BIN to a real Godot binary"))
    gut = root / "addons" / "gut" / "gut_cmdln.gd"
    if not gut.is_file():
        problems.append(_problem(
            "warning", "gut_addon_missing", {"path": str(gut)},
            "install the GUT addon (addons/gut/) — GUT legs fail loudly "
            "without it; auto-prepare cannot install vendored addons",
            level="P0", source="addons/gut/gut_cmdln.gd (fixed path)",
            auto_prepare_covered=False))
    from .shadow import _IMPORT_ARTIFACTS
    godot_dir = root / ".godot"
    lacked = [n for n in _IMPORT_ARTIFACTS if not (godot_dir / n).exists()]
    if lacked:
        problems.append(_problem(
            "warning", "import_cache_missing",
            {"dir": str(godot_dir), "missing": lacked,
             "estimated_cost": "godot --headless --import (seconds to "
                               "~1 minute)"},
            "godot --headless --path . --import  (auto-prepare covers "
            "this step)",
            level="P1", source=".godot/import artifacts (conventional)",
            auto_prepare_covered=True))
    return problems


def _prepare_prereq_problems(root: Path) -> list[dict]:
    """SEE-1623 M2：prepare 前置一次性枚举——映射构建链（attribution
    generate）按序要求 `.gutconfig.json` → `acceptance/features/` 目录 →
    至少一个 `.feature`，此前逐个试错（每轮 0 秒失败一次只披露一个）。
    plan 一次列全（附修复动作），消灭打地鼠。auto-prepare 不创建用户
    工程内容（配置与 feature 是被测项目资产），auto_prepare_covered=False。"""
    problems: list[dict] = []
    gutconfig = root / ".gutconfig.json"
    if not gutconfig.is_file():
        problems.append(_problem(
            "warning", "gutconfig_missing", {"path": str(gutconfig)},
            "create .gutconfig.json in the project root (GUT reads its "
            "test face from it; e.g. {\"dirs\": [\"res://tests/\"], "
            "\"include_subdirs\": true})",
            level="P1", source="project root convention",
            auto_prepare_covered=False))
    feats_dir = root / "acceptance" / "features"
    feats = sorted(feats_dir.glob("*.feature")) if feats_dir.is_dir() else []
    if not feats_dir.is_dir():
        problems.append(_problem(
            "warning", "acceptance_features_dir_missing",
            {"path": str(feats_dir)},
            "create acceptance/features/ and add at least one .feature "
            "(the attribution map build runs the acceptance harness)",
            level="P1", source="acceptance/features (conventional)",
            auto_prepare_covered=False))
    elif not feats:
        problems.append(_problem(
            "warning", "acceptance_features_empty",
            {"dir": str(feats_dir)},
            "add at least one .feature file under acceptance/features/",
            level="P1", source="acceptance/features (conventional)",
            auto_prepare_covered=False))
    return problems


def _map_problem(root: Path, target_file: str | None) -> list[dict]:
    if not target_file:
        return []
    try:
        from .truth_select import map_preview_state
        st = map_preview_state(str(root), str(target_file))
    except Exception as e:  # noqa: BLE001 — 探测失败如实落 warning
        return [_problem("warning", "map_state_unknown",
                         {"error": f"{type(e).__name__}: {e}"},
                         "mapping state could not be probed; auto mode "
                         "will renew before selection")]
    if st.get("present") and st.get("fresh") is True:
        return []
    return [_problem(
        "warning", "attribution_map_missing_or_stale",
        {"present": st.get("present"), "fresh": st.get("fresh"),
         "reason": st.get("reason"),
         "estimated_cost": "map build/renew: baseline preflight + "
                           "instrumented GUT, ~2m15s (KOL 实测 135-137s)"},
        "gqt measure map-update --project-root .  (~2m15s) — or rerun "
        "with --auto-prepare to build it automatically, or --select full "
        "to skip the map",
        level="P1",
        source=".qa-cache/attribution-map.json (conventional)",
        auto_prepare_covered=True)]


def _estimated_costs() -> list[dict]:
    """贵操作清单（与 execution_plan_meta.expensive_operations 同源口径）。"""
    from .runner import _expensive_operations
    return _expensive_operations()


def static_plan(project_root: str | Path, target_file: str | None = None,
                tests_glob: str | None = None, *,
                budget_s: float = PLAN_BUDGET_S) -> dict:
    """静态穷查（只读、硬预算）。返回 {problems, estimated_costs,
    budget_s, elapsed_s, exhaustive}。"""
    t0 = time.monotonic()
    root = Path(project_root).resolve()
    deadline = t0 + budget_s
    problems = _env_problems(root)
    problems += _autoload_problems(root)
    tprobs, exhaustive = _test_interpreter_problems(root, tests_glob, deadline)
    problems += tprobs
    problems += _prepare_prereq_problems(root)
    problems += _map_problem(root, target_file)
    elapsed = round(time.monotonic() - t0, 3)
    if elapsed > budget_s and exhaustive:
        problems.append(_problem(
            "warning", "plan_budget_exhausted", {"elapsed_s": elapsed},
            "plan exceeded its budget; consider narrowing the scan scope"))
    return {"problems": problems, "estimated_costs": _estimated_costs(),
            "budget_s": budget_s, "elapsed_s": elapsed,
            "exhaustive": exhaustive}
