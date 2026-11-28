"""SEE-1654 harden-R1 钉点①：§SPEC-035 / 036 / 037 结构判据常驻化。

判据本体 = `tests/integration/check_see1654_structure.py`（附件
`map-contract-structure-check.py` v3 判据原文的仓内形态，输出逐字节
等价）。此前该判据只存在于 issue 附件脚本、须手工在干净导出上跑；
本模块把它钉进常规测试面：结构契约一旦回退（手动地图入口回归、run
侧续新、契约裸读路径参数等），本测即红，不等 QA 轮的手工扫描。

红绿咬合：判据在 `775ebdd`（基点 master v0.4.2，契约收口前——
`--attribution-map` / `map_path_override` / ENV_MAP 尚在）上为红
（S11 / S4 / S9 等多条 FAIL），在 `d961ccd` 及之后为绿。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

CHECKER = (Path(__file__).resolve().parents[1] / "integration"
           / "check_see1654_structure.py")


def test_map_contract_structure_criteria_v3_all_pass():
    """§SPEC-035/036/037：S1–S13 全 PASS、末行 `D1-STRUCTURE PASS`、
    exit 0（判据脚本的对外行为契约）。"""
    proc = subprocess.run([sys.executable, str(CHECKER)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, \
        f"structure criteria regressed:\n{proc.stdout}\n{proc.stderr}"
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    assert lines[-1] == "D1-STRUCTURE PASS", lines[-3:]
