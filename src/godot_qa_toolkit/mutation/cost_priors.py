"""E6-5（§SPEC-502）：成本感知分片先验——串/并行的先验驱动自动仲裁。

先验文件落位项目 `.qa-cache/gqt-mutation-cost-priors.json`（单写者：run
收尾方在 wrapper 收尾点写，对齐 manifest 索引层「收尾方单点写」惯例）；
跨 run 合并策略 = 「0 值保留旧先验」（串行轮测不到 prewarm/baseline 阶段，
写 0 不得覆盖既有测量——SEE-1568 批次二逐轮翻转缺陷的定案修法）。

仲裁模型（批次二留档决策线逐位复原）：
- serial_est   = baseline_est + total × per_mutant_s
- parallel_est = prewarm_s + baseline_est + ceil(total/jobs) × per_mutant_s
                 + jobs × slot_fixed_s
- baseline_est = baseline_s（零值时回退 per_mutant_s——先验缺失侧兜底）
serial_est < parallel_est 才判串行；无先验/先验不可用 = 并行缺省（不降级）。
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

PRIOR_KEYS = ("prewarm_s", "baseline_s", "per_mutant_s", "slot_fixed_s")
PRIORS_DIRNAME = ".qa-cache"
PRIORS_FILENAME = "gqt-mutation-cost-priors.json"


def _priors_path(project_root: str) -> Path:
    return Path(project_root) / PRIORS_DIRNAME / PRIORS_FILENAME


def load(project_root: str) -> dict | None:
    """读先验；缺失 = None（并行缺省）；损坏 = 告警一次按缺失处理（不阻断）。"""
    path = _priors_path(project_root)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("priors root must be an object")
        return {k: float(raw.get(k) or 0.0) for k in PRIOR_KEYS}
    except (OSError, ValueError) as e:
        print(f"mutation: cost priors unreadable at {path} ({e}) — "
              f"using parallel default", file=sys.stderr)
        return None


def evaluate(priors: dict, total: int, jobs: int) -> tuple[bool, float, float] | None:
    """先验 → (判串行?, serial_est, parallel_est)；先验不可用返回 None。

    per_mutant_s 是模型核心输入，非正值 = 无有效测量（并行缺省）。
    """
    per_mutant = float(priors.get("per_mutant_s") or 0.0)
    if per_mutant <= 0:
        return None
    baseline_raw = float(priors.get("baseline_s") or 0.0)
    baseline_est = baseline_raw if baseline_raw > 0 else per_mutant
    serial_est = baseline_est + total * per_mutant
    parallel_est = (float(priors.get("prewarm_s") or 0.0) + baseline_est
                    + math.ceil(total / jobs) * per_mutant
                    + jobs * float(priors.get("slot_fixed_s") or 0.0))
    return serial_est < parallel_est, serial_est, parallel_est


def prefers_serial(project_root: str, total: int, jobs: int) -> bool:
    """仲裁入口：有先验且模型判串行时 True（stderr 披露决策线，双路同判据）。"""
    priors = load(project_root)
    if not priors:
        return False
    verdict = evaluate(priors, total, jobs)
    if verdict is None:
        return False
    serial, serial_est, parallel_est = verdict
    print(f"cost-aware: total={total} jobs={jobs} "
          f"serial_est={serial_est:.2f}s parallel_est={parallel_est:.2f}s -> "
          f"{'serial' if serial else 'parallel'} (priors: "
          f"{json.dumps({k: priors.get(k, 0.0) for k in PRIOR_KEYS})})",
          file=sys.stderr)
    return serial


def record(project_root: str, stage_timings: dict, jobs: int,
           executed: int) -> None:
    """run 收尾方写先验（单写者）：阶段计时 → 四键归一 → 0 值保留旧先验合并。

    slot_fixed_s = slot_sync 总耗时 / 实际分片数；per_mutant_s = per_mutant
    总耗时 / 实际执行位点数。executed ≤ 0（零执行/短路轮）无新信息，跳过。
    写失败仅告警一次后放弃（先验是优化面，不得打破统一 JSON 契约）。
    """
    if executed <= 0 or jobs <= 0:
        return
    stages = stage_timings or {}
    new = {
        "prewarm_s": float(stages.get("prewarm") or 0.0),
        "baseline_s": float(stages.get("baseline") or 0.0),
        "per_mutant_s": float(stages.get("per_mutant") or 0.0) / executed,
        "slot_fixed_s": float(stages.get("slot_sync") or 0.0) / jobs,
    }
    merged = load(project_root) or {}
    for key, value in new.items():
        if value > 0:
            merged[key] = value
    out = {k: float(merged.get(k) or 0.0) for k in PRIOR_KEYS}
    path = _priors_path(project_root)
    tmp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(out), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        print(f"mutation: cost priors write failed for {path}: {e} — "
              f"continuing without priors", file=sys.stderr)
