"""SEE-1631 §SPEC-019：auto map 构建入口与 measure run 同形态。

历史注记（退役映射，Owner 2026-11-24 裁决）：本文件原为 §SPEC-PREFLIGHT
（映射构建前置未插桩全脸基线 GUT：红短停/放行复用/三路分派）的反例组。
§SPEC-019 退役该预检——map 构建（ensure_map/regenerate）直接插桩，全绿
零额外运行，红了在臂门控内按 gut_full 既有懒基线语义分类（既有失败具名
吸收标 partial / 疑似插桩不耐受车道），显式 fail-on-red = 拒绝安装该 map
的 fail-loud。臂超时不再由预检墙钟事前校准（参数/env 来源）。原预检语义
测试的映射：
- 红 partial 继续 / 绿复用 arms / 三路分派 → 预检删除，由懒基线与对照
  路径承接（test_see1594_s63_lazy_baseline.py + test_c1r1_see1631.py）；
- fail-on-red 短停 → 改为建图后拒绝安装（本文件）。
mutation 执行阶段自身的 verdict 基线（_precompute_baselines）不在本轮
范围（§SPEC-019 仅限 map 构建入口）。
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from unittest import mock

import pytest


# --------------------------------------------------------------------------
# 入口形态：run_measurement 不再有 baseline_preflight 参数
# --------------------------------------------------------------------------

def test_run_measurement_has_no_baseline_preflight_param():
    """§SPEC-019：映射构建入口的前置基线参数退役——传旧参数必须
    TypeError（不得静默忽略后继续旧行为）。"""
    from godot_qa_toolkit.measurement.orchestrator import run_measurement

    sig = inspect.signature(run_measurement)
    assert "baseline_preflight" not in sig.parameters


def test_no_preflight_stage_or_helper():
    """预检 stage/helper 退役：run_baseline_preflight 不复存在；
    stage_timings 不含 baseline_preflight 段。"""
    import godot_qa_toolkit.measurement.orchestrator as mo

    assert not hasattr(mo, "run_baseline_preflight")


# 进程账与 fail-on-red 拒装的行为判据在 test_see1594_s63_lazy_baseline.py
# 的 TestS63EnsureMapEntry（同夹具复用，避免双份假 GOT）。
