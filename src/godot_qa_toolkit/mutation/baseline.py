"""父层基线预跑快照原语（SEE-1550 tidy 自 executor.py 迁出——<800 行纪律）。

并发分派【之前】的单进程冷启动 baseline 快照，与失败原因旁路表同源落账。
对 runner 域内 `_run_baseline` 的引用保持函数体内延迟 import——call-time
经 runner 命名空间解析，既有 `mrunner._run_baseline` monkeypatch seam
零变化；`executor._run_baseline_snapshot` seam 由 executor 再导出承接。
"""

from __future__ import annotations

from pathlib import Path

# 基线预跑失败原因（SEE-1525）：_run_baseline_snapshot 的返回契约
# （dict|None）被既有测试替身钉死，故失败原因经此旁路表透出，供
# _precompute_baselines 组装降级告警文案。键 = (project_root, tests_glob)。
_BASELINE_FAILURE_REASONS: dict[tuple[str, str], str] = {}


def _baseline_key(project_root: str, tests_glob: str | None) -> tuple[str, str]:
    return (str(Path(project_root).resolve()), tests_glob or "")


def _run_baseline_snapshot(project_root: str, tests_glob: str | None,
                           timeout_s: int) -> dict | None:
    """父层基线预跑快照；失败返回 None（契约被既有替身钉死）。

    失败原因（异常类型 + 摘要 / 非零 rc）记入 _BASELINE_FAILURE_REASONS，
    由 _precompute_baselines 取出生成降级告警——快照与原因同源，避免二次
    推断。
    """
    from .runner import _run_baseline

    key = _baseline_key(project_root, tests_glob)
    try:
        rc, tail, elapsed = _run_baseline(project_root, tests_glob, timeout_s)
    except Exception as e:
        _BASELINE_FAILURE_REASONS[key] = f"{type(e).__name__}: {e}"[:200]
        return None
    if rc != 0:
        _BASELINE_FAILURE_REASONS[key] = f"baseline GUT exited rc={rc}"
        return None
    # 成功即清掉上一次的失败原因——原因表绝不跨 run 漂移到别的降级文案上。
    _BASELINE_FAILURE_REASONS.pop(key, None)
    return {"rc": rc, "tail": tail, "elapsed": elapsed}
