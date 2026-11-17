# A10（SEE-1594 S4）：静态选面（derive_tests_glob/_strength_glob/
# _INDEX_CACHE 直连强度链）已物理退役——选面唯一真源 = 运行时真值映射
# （truth_select）；本模块保留零引用披露与选面披露 helpers、gdself 诊断
# 扫描面（scan_test_refs/counts → gdself_stats 增量失效闭包）。

"""SEE-1321 SPEC-001：测试子集自动扫描器。

目标 .gd → 扫描 tests/ 树下引用它的测试文件（preload(path)/load(path) 字面量、
class_name 标识符引用两种形态），映射为最小命中目录的 `res://tests/<dir>/`
GUT glob。扫描零命中返回 None——由 runner 自动退回全量。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# res:// 引用字面量：preload("x") / load("x") 两函数形态（与 class_name
# 标识符引用合计 SPEC-001 三形态）
_RES_REF_PATTERN = re.compile(r'\b(?:preload|load)\s*\(\s*"([^"]+)"\s*\)')

# 判定“测试目录”的候选 glob 起点——扫描只在这棵树内做，误扫到 src/ 会命中自身
_TESTS_DIR_NAME = "tests"


def _res_canonical(res_path: str) -> str:
    return res_path.strip().casefold()


def scan_test_refs(target_res_path: str, tests_dir: Path) -> list[str]:
    """返回 tests/ 下引用目标文件的测试文件绝对路径列表（每文件一条，排序）。"""
    return sorted(scan_test_ref_counts(target_res_path, tests_dir))


def scan_test_ref_counts(target_res_path: str, tests_dir: Path) -> dict[str, int]:
    """每命中文件 → 命中引用次数（res:// 字面量 + class_name 标识符各计 1）。"""
    target_stem = Path(target_res_path).stem.casefold()
    # 全词匹配——子串计数会把 mock_save_manager.gd 内容误配进 save_manager
    # 的命中集（LOW-3 审查实证）
    stem_word = re.compile(r"\b" + re.escape(target_stem) + r"\b")
    counts: dict[str, int] = {}
    for gd in sorted(tests_dir.rglob("*.gd")):
        text = gd.read_text(encoding="utf-8", errors="replace")
        text_cf = text.casefold()
        score = sum(
            1 for r in _RES_REF_PATTERN.findall(text)
            if _ref_hits_target(r, target_res_path)
        ) + len(stem_word.findall(text_cf))
        if score:
            counts[str(gd.absolute()).replace("\\", "/")] = score
    return counts


def _ref_hits_target(ref: str, target_res_path: str) -> bool:
    ref_cf = ref.casefold().strip()
    target_cf = target_res_path.casefold().strip()
    # 精确或后缀匹配；“路径+尾巴 .bak” 这类串改别的文件的字面量不算
    return ref_cf == target_cf or ref_cf.endswith("/" + target_cf.rsplit("res://", 1)[-1])


# E2（§SPEC-501）：零引用短路披露面——stderr 一行 + summary 定终态记录。
# 判据唯一出处 = 运行时映射零引用（zero_ref；禁止 CLI 参数推断——SEE-1568
# 批次二 v1 串行门误判教训）；串行/并行路径共用同一判据与记录形态。
_ZERO_REF_NOTE = ("zero-ref short-circuit: no test references the target "
                  "— full-GUT fallback skipped (disclosed short-circuit; "
                  "potential kills intentionally not executed)")


def disclose_zero_ref(file_path: str) -> None:
    """零引用短路 stderr 披露行（与 summary.zero_ref_short_circuit 双披露）。"""
    print(f"zero-ref short-circuit: {file_path} — no test references the "
          f"target, full-GUT fallback skipped (disclosed)", file=sys.stderr)


def zero_ref_short_circuit_result(file_path: str) -> dict:
    """零引用目标定终态：有意不执行 + 双披露（形态与 no_sites 契约同构）。"""
    return {
        "tool": "mutation",
        "ok": True,
        "summary": {
            "file": file_path, "mutants": 0, "jobs": 0,
            "killed": 0, "survived": 0, "timeout": 0,
            "run_errors": 0, "suspect": 0, "invalid_mutants": 0,
            "kill_rate": 0.0,
            "note": _ZERO_REF_NOTE,
            "scoped": False,
            "zero_ref_short_circuit": True,
        },
        "failures": [],
    }


_SELECT_MODES = frozenset({"line", "fn", "file", "full"})


def resolve_selection_mode(select_mode: str,
                           selection_meta: dict | None = None,
                           select_ids: list | None = None,
                           select_lines: list | None = None) -> str:
    """实际生效的选面（SEE-1589 A-M4/M5/L1/L5 唯一判别口径）。

    优先级：卡尺（--mutation > --lines）> 显式 --tests 接管 > --select。
    显式 --tests 时 select_mode 仍是 line（默认值），但实际执行面由用户
    接管——继续报 line 会误导（L1）；未知值 fail-loud，不静默回落。
    """
    meta = selection_meta or {}
    if select_ids:
        return "mutation"
    if select_lines:
        return "lines"
    if meta.get("source") == "explicit_tests":
        return "explicit_tests"
    mode = str(select_mode or "line").lower()
    if mode not in _SELECT_MODES:
        raise ValueError(f"unknown selection mode: {select_mode!r}")
    return mode


def concurrency_disclosure(select_mode: str, groups: int | None,
                           jobs: int | None) -> dict:
    """并发上限披露（M5）：行级/函数级分片的 jobs 上限 = 签名组数。

    仅当分组数小于请求并发（确被钳制）时披露；未钳制或非分组模式返回空片。
    """
    if str(select_mode).lower() not in ("line", "fn"):
        return {}
    if not groups or not jobs or groups >= jobs:
        return {}
    return {"jobs": int(groups), "selection_groups": int(groups),
            "jobs_limit_reason": "selection_groups"}


def selection_disclosure(selection_meta: dict | None) -> dict:
    """§SPEC-613 选测披露片段（summary 透出的唯一拼装点）。

    自动选测且派生 glob 在场 → {"test_selection": meta}；显式 --tests、
    零消费者或空 meta → 空片（update 即无操作）。四个披露位（串行门 /
    规划无分片 / _inject_one / runner disclosures）共用本口径。
    """
    if selection_meta and (selection_meta.get("derived_glob")
                           or selection_meta.get("source")):
        return {"test_selection": selection_meta}
    return {}


