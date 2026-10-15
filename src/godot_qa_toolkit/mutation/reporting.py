"""SEE-1416 mutation 报告投影层：summary 自足明细 + 记录扩列辅助。

从 runner 拆出以守住单文件 < 800 行纪律（owner-order §6.1）；runner 域内
re-export 保持既有 import / monkeypatch 名称空间稳定。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ops import Mutant

from .ops import _split_invalid_mutants

# summary.survivors 精简投影键（§SPEC-1416-1）——切片 .summary 也自足：
# 不带 operator/line_text（failures 明细里已有全量）。
SURVIVOR_DETAIL_KEYS = ("file", "line", "column", "kind", "original",
                        "mutated", "verdict", "reason")
SURVIVORS_CAP = 5


def line_text_of(src: str, line: int) -> str:
    """变异行原文；行号越界返回空串（不吞错误——记录仍可判读）。"""
    lines = src.splitlines()
    return lines[line - 1] if 0 < line <= len(lines) else ""


def survivors_projection(failures: list[dict]) -> tuple[list[dict], bool]:
    """非 killed 明细的 top ≤5 精简投影；返回 (survivors, truncated)。"""
    survivors = [{k: f[k] for k in SURVIVOR_DETAIL_KEYS}
                 for f in failures[:SURVIVORS_CAP]]
    return survivors, len(failures) > SURVIVORS_CAP


def _mutant_record(m: "Mutant", verdict: str, reason: str,
                   src: str | None = None) -> dict:
    """统一 mutant 明细记录形态（契约 §4.3 failures 项）。

    SEE-1416：扩列 column/operator/line_text——Mutant 的定位面全量透出，
    agent 不必打开源文件对照。所有 verdict（含 killed / 静态预检 invalid）
    同构升级；resume 复用重建走同一函数，保证新旧记录形态逐字段一致
    （指纹含源码 sha1，line_text 与首次落盘必然一致）。
    """
    record = {"file": m.file, "line": m.line, "column": m.column,
              "kind": m.kind, "operator": m.operator,
              "original": m.original, "mutated": m.mutated,
              "verdict": verdict, "reason": reason}
    if src is None:
        return record
    return {**record, "line_text": line_text_of(src, m.line)}


def _valid_mutants(src: str, mutants: list) -> list:
    return _split_invalid_mutants(src, mutants)[0]


def _record_invalid_mutants(src: str, mutants: list,
                            mutant_slice: tuple[int, int] | None = None
                            ) -> tuple[list[dict], int]:
    """预检并记录 invalid_mutant 明细；返回 (results, invalid_count)。

    mutant_slice 时只预检分片区间（per-mutant 并发 worker 各管各片区，
    合并层求和不得重复计数）。
    """
    results: list[dict] = []
    scoped = mutants
    if mutant_slice is not None:
        start, stop = mutant_slice
        scoped = mutants[start:stop]
    _, invalid = _split_invalid_mutants(src, scoped)
    for m in invalid:
        results.append(_mutant_record(m, "invalid_mutant",
                                      f"mutated source fails to parse (operator bug): "
                                      f"{m.kind} {m.original}→{m.mutated}",
                                      src=src))
    return results, len(invalid)
