"""SEE-1405 §SPEC-002 — 动工前只读统计脚本（gdself 收益与成本实测）。

**SEE-1463 §SPEC-004 状态登记（deferred）**：本模块（仅 `__main__` 统计
脚本）与 `fsprobe.py` 当前生产零调用方，状态为 **deferred——未接线进
mutation 报告链**。属一次性诊断脚本，仅经 `python -m ...gdself_stats`
主动执行；触发条件/演进方向见 docs/contract.md §8「deferred 登记」。

对目标 Godot 项目全库只读统计：
1. 反查（SEE-1400 testscan）零命中生产文件集合（假 0 命中基线）；
2. gdself 正向索引对每个零命中目标的消费者闭包——逐源累计消除份额
   （S1 class_name / S2 autoload / S3 场景绑定 / S4 字面量与标识符 /
   dynamic 插值兜底；一目标可多源同中，份额为 any-hit 非互斥计数）；
3. 剩余零命中的 unresolved_reason 分类分布（诚实清单）；
4. 索引构建成本实测 + 「< 单轮启动税 5% 则 v2 永不必要」门槛判定
   （启动税参照值在 P1 重测前为待校准数，JSON 中显式标注 provisional）。

零写入：只读项目文件，不产生任何落盘物（v1 零缓存语义同源）。
用法：python -m godot_qa_toolkit.mutation.gdself_stats --project-root <KOL>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .gdself import build_index, query_consumers
from .testscan import scan_test_ref_counts

# 单轮启动税参照（基底稿实测口径，P1 探针待同机重测——门槛判定据此标注）
PROVISIONAL_STARTUP_TAX_S = 12.3
THRESHOLD_PCT = 5.0

_SOURCE_OF = {"class_name": "S1", "autoload": "S2", "path": "S4",
              "stem": "S4", "dynamic": "dynamic"}
# core 口径排除前缀：addons/（vendored，非被测生产面）与 .dev/（仓内工具树
# fixture）——定案「158 生产文件 / 69 零命中」口径 = autoload+systems+scenes
# +resources+utils；全树口径一并披露防误读。
_CORE_EXCLUDE_PREFIXES = ("res://addons/", "res://.dev/")


def collect_stats(project_root: str) -> dict:
    """全库统计；返回 JSON 化 dict（只读，不落盘）。"""
    root = Path(project_root).resolve()
    tests_dir = root / "tests"
    index = build_index(str(root))
    all_targets = sorted(
        key for key, info in index["files"].items()
        if info["kind"] == "gd" and not info["tests"])
    core_targets = [k for k in all_targets
                    if not k.startswith(_CORE_EXCLUDE_PREFIXES)]
    stats_all = _scope_stats(index, root, tests_dir, all_targets)
    stats_core = _scope_stats(index, root, tests_dir, core_targets)
    cost = index["build_elapsed_s"]
    threshold_s = PROVISIONAL_STARTUP_TAX_S * THRESHOLD_PCT / 100.0
    return {
        "project": str(root),
        "index_build_cost_s": round(cost, 4),
        "threshold": {
            "pct_of_startup_tax": THRESHOLD_PCT,
            "budget_s": round(threshold_s, 4),
            "v2_needed": cost >= threshold_s,
            "verdict": ("v2 缓存有立项空间（构建成本 ≥ 门槛）"
                        if cost >= threshold_s else
                        "v2 永不必要（构建成本 < 单轮启动税 5%）"),
        },
        "startup_tax_ref": {
            "value_s": PROVISIONAL_STARTUP_TAX_S,
            "provisional": True,
            "note": "P1 探针同机重测前为待校准数（see1405-decision 定案）",
        },
        "scope_core": stats_core,
        "scope_full_tree": stats_all,
        "scope_note": "core = 排除 addons/（vendored）与 .dev/（工具树）；"
                      "定案 69/158 口径对 core 对表",
    }


def _target_sources(out: dict) -> set[str]:
    """单目标消费者闭包 → 逐源集合（资产节点中转计 S3 场景绑定）。"""
    sources: set[str] = set()
    for consumer in out["consumers"]:
        if consumer["kind"] == "asset":
            sources.add("S3")
        for s in consumer["sources"]:
            sources.add(_SOURCE_OF.get(s, s))
    return sources


def _scope_stats(index: dict, root: Path, tests_dir: Path,
                 targets: list[str]) -> dict:
    """单个目标集的零命中/逐源份额/unresolved 分布。"""
    zero_hit: list[str] = []
    per_source = {"S1": 0, "S2": 0, "S3": 0, "S4": 0, "dynamic": 0}
    reasons: dict[str, int] = {}
    scene_chains = 0
    for key in targets:
        res = index["files"][key]["res"]
        if not scan_test_ref_counts(res, tests_dir):
            zero_hit.append(key)
    for key in zero_hit:
        out = query_consumers(str(root), key, tests_dir, index=index)
        if not out["consumers"]:
            reason = out["unresolved"]["reason"] or "no_tests"
            reasons[reason] = reasons.get(reason, 0) + 1
            continue
        sources = _target_sources(out)
        for src in sources:
            per_source[src] = per_source.get(src, 0) + 1
        if "S3" in sources:
            scene_chains += 1
    eliminated = len(zero_hit) - sum(reasons.values())
    return {
        "production_gd_files": len(targets),
        "legacy_zero_hit": len(zero_hit),
        "gdself_eliminated": eliminated,
        "elimination_rate_of_zero_hit": (
            round(eliminated / len(zero_hit), 4) if zero_hit else None),
        "per_source_any_hit": per_source,
        "scene_chain_targets": scene_chains,
        "remaining_unresolved_reasons": reasons,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SEE-1405 §SPEC-002 只读统计（gdself 份额 + 构建成本）")
    parser.add_argument("--project-root", required=True,
                        help="Godot 项目根（含 project.godot / tests/）")
    args = parser.parse_args(argv)
    root = Path(args.project_root)
    if not (root / "project.godot").is_file():
        print(f"error: --project-root has no project.godot: {root}",
              file=sys.stderr)
        return 2
    stats = collect_stats(str(root))
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
