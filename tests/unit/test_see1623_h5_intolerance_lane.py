"""SEE-1623 H5 — measure run 懒基线路径的插桩不耐受车道行为钉。

根因（SEE-1622 Refacty BUG-2，定向复核范围收窄确认）：不耐受车道机制在
mutation 建映射路径工作（新增失败中断报告附 suspected_instrumentation_
intolerance 分类 + 声明草稿），但 measure run 的懒基线路径缺失——基线
实测证明「未插桩通过、插桩失败」的测试直接落 new_failures 门禁
（臂 invalid），intolerant 车道字段全空、无草稿。

行为契约（§SPEC-005）：
1. 懒基线实测（gut_full，未显式提供基线）后仍新增的失败 = 疑似插桩
   不耐受：迁出 new_failures（不污染门禁），记入
   instrumentation_intolerant.observed 具名车道。
2. 附可直接使用的 --intolerant-tests 声明草稿（报告块 + stderr）。
3. 懒获取失败（无运行流）→ 无实测证明 → 不分类，臂保持 invalid
   fail-loud（既有契约不弱化）；acceptance 臂失败保持可见（不继承）。
"""

from __future__ import annotations

import subprocess as sp

import pytest

import test_see1594_s63_lazy_baseline as s63
from test_see1594_s63_lazy_baseline import (  # noqa: F401 — pytest fixture
    LazyScenarioGodot,
    project,
)
from godot_qa_toolkit.measurement.orchestrator import run_measurement

_GREEN_FLOW = (
    "Godot Engine v4.4.1\n"
    "res://tests/unit/test_game.gd\n"
    "  * test_font_row\n"
    "\t\ttest_font_row [Passed]\n"
    "= Run Summary\n"
    "res://tests/unit/test_game.gd\n"
    "- test_font_row\n"
)


class CleanLazyGodot(LazyScenarioGodot):
    """懒基线运行返回全绿流（0 失败对）——插桩失败对即疑似不耐受。"""

    def __call__(self, *a, **k):
        argv = list(a[0])
        cmd = " ".join(argv)
        if "gut_cmdln.gd" in cmd and ".gqt-run-" not in cmd \
                and "-gpre_run_script=" not in cmd:
            self.lazy_calls.append(argv)
            return self._resp(0, _GREEN_FLOW)
        return super().__call__(*a, **k)


@pytest.fixture()
def _restore_subprocess():
    orig_run = sp.run
    yield
    sp.run = orig_run


class TestH5IntoleranceLane:
    def test_baseline_green_instrumented_red_routes_to_intolerance(
            self, project, tmp_path, _restore_subprocess, capsys):
        """基线绿（0 失败对实测）/插桩红 → suspected_instrumentation_
        intolerance：new_failures 不含该测试、observed 具名、草稿在场、
        门禁不被污染。"""
        fake = CleanLazyGodot(project, scenario="garbage")
        sp.run = fake
        report = run_measurement(
            project, s63._targets(project), s63._recipe(project, tmp_path),
            test_attribution=True,
            attribution_out=str(tmp_path / "map.json"))
        gut = report["arms"][0]
        assert gut["state"] == "valid", gut["invalid_reason"]
        assert gut["test_failures"]["new"] == []
        assert gut["test_failures"]["instrumentation_intolerant"] == [
            "res://tests/unit/test_game.gd::test_font_row"]
        block = report["instrumentation_intolerant"]
        assert "res://tests/unit/test_game.gd::test_font_row" \
            in block["observed"]
        draft = block["suspected_draft"]
        assert draft and draft[0]["script"] == "res://tests/unit/test_game.gd"
        assert draft[0]["name"] == "test_font_row"
        assert "suspected_instrumentation_intolerance" in draft[0]["reason"]
        err = capsys.readouterr().err
        assert "suspected_instrumentation_intolerance" in err
        assert "--intolerant-tests" in err

    def test_acquisition_failure_stays_fail_loud(
            self, project, tmp_path, _restore_subprocess):
        """懒获取失败（无运行流）→ 无实测证明 → 不分类、臂保持 invalid
        （既有 fail-closed 契约不弱化）。"""
        fake = LazyScenarioGodot(project, scenario="garbage")
        sp.run = fake
        report = run_measurement(
            project, s63._targets(project), s63._recipe(project, tmp_path),
            test_attribution=True,
            attribution_out=str(tmp_path / "map.json"))
        gut = report["arms"][0]
        assert gut["state"] == "invalid"
        assert gut["test_failures"]["new"] == ["test_font_row"]
        assert gut["test_failures"]["instrumentation_intolerant"] == []

    def test_lazy_baseline_failures_absorbed_not_classified(
            self, project, tmp_path, _restore_subprocess):
        """懒基线里的既有失败（absorb 场景）→ 基线吸收，不入不耐受车道。"""
        fake = LazyScenarioGodot(project, scenario="absorb")
        sp.run = fake
        report = run_measurement(
            project, s63._targets(project), s63._recipe(project, tmp_path),
            test_attribution=True,
            attribution_out=str(tmp_path / "map.json"))
        gut = report["arms"][0]
        assert gut["state"] == "valid"
        assert gut["test_failures"]["new"] == []
        assert gut["test_failures"]["instrumentation_intolerant"] == []
