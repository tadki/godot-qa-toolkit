"""SEE-1594 S8.1：measurement 最小进度面形状/触发测试。

进度行只进 stderr（stdout 单 JSON 不变量不变）：arm start/done、每个
shard/rep 的 start/done（i/n + 耗时）。判定语义零变化——本测试只钉
形状与触发。
"""

from __future__ import annotations

import importlib.util
import re
import subprocess as sp
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

_S63 = (Path(__file__).resolve().parent / "test_see1594_s63_lazy_baseline.py")


def _load_s63():
    spec = importlib.util.spec_from_file_location("s63_fixtures", _S63)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_progress_lines_shape(tmp_path, capsys):
    """运行一期 measurement：stderr 必须含 arm/shard 的 start/done 行，
    stdout 保持单 JSON（由 run_measurement 返回 dict 本身即证明）。"""
    from godot_qa_toolkit.measurement.orchestrator import run_measurement

    t = _load_s63()
    project = t.project.__wrapped__(tmp_path)
    fake = t.LazyScenarioGodot(project, scenario="absorb")
    orig_run = sp.run
    sp.run = fake
    try:
        run_measurement(project, t._targets(project),
                        t._recipe(project, tmp_path),
                        test_attribution=True,
                        attribution_out=str(tmp_path / "map.json"))
    finally:
        sp.run = orig_run
    err = capsys.readouterr().err
    assert "measure: arm=gut_full start shards=" in err
    assert re.search(
        r"measure: arm=gut_full shard=\d+/\d+ rep=0/1 start", err), err
    assert re.search(
        r"measure: arm=gut_full shard=\d+/\d+ done s=[0-9.]+", err), err
    assert re.search(
        r"measure: arm=gut_full done state=(valid|invalid) "
        r"wall_s=[0-9.]+", err), err
    assert "measure: arm=gherkin start shards=" in err
    assert re.search(
        r"measure: arm=gherkin done state=(valid|invalid) "
        r"wall_s=[0-9.]+", err), err
