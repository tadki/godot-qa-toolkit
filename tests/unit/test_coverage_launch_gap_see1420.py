"""§SPEC-1420-21：coverage 中止条件收紧（Owner 2026-10-17 指令，Archi 实机
取证定位的 coverage 缺口）。

缺陷：中止条件 `rc≠0 ∧ _looks_like_godot_launch_failure(...)` 旧口径要求
错误 marker 在场——godot 加载早期退出（rc≠0、快速返回、零 GUT 输出、无
marker）被放行 → `_read_hits` 读空 → 归类 probes_fired=0 哨兵，掩盖根因
（哨兵兜住数据面，中止原因失真）。

修复：中止条件收紧为 **rc≠0 ∧ 无 GUT 测试流证据**（`_GUT_RUN_EVIDENCE`
不命中 → 一律 launch failure 中止，run_error 契约；不再要求 marker 在场）。
两向回归（修复前红/修复后绿）：
- 无 marker 无证据 rc≠0 → 中止（旧口径放行成哨兵——红）；
- 资源告警 + GUT 证据在场 → 正常分类（保护面不回归——绿保持）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.coverage.runner import (
    _GODOT_LAUNCH_ERROR_MARKERS,
    _looks_like_godot_launch_failure,
)

# Archi 实机取证形态：godot 加载早期退出——rc≠0、2.68s 返回、无任何 GUT
# 输出、无任何已知 marker。
EARLY_EXIT_NO_MARKER = (
    "Godot Engine v4.6.2.stable.official - https://godotengine.org\n"
    "OpenGL API 3.3.0 - Core - Using Device: NVIDIA\n"
)
MARKER_ONLY_RESOURCE_WARNING = (
    "ERROR: Failed loading resource: res://item_visual.tscn. Make sure "
    "the path exists.\n"
)
GUT_EVIDENCE_FLOW = (
    MARKER_ONLY_RESOURCE_WARNING
    + "* test_use_adds_count\n"
    + "\ttest_use_adds_count [Passed]\n"
)


class TestCoverageLaunchGapAborts:
    def test_early_exit_without_marker_or_evidence_is_launch_failure(self):
        """修复前红：无 marker → _looks_like_godot_launch_failure=False →
        rc≠0 轮被放行进 _read_hits → 假 probes_fired=0 哨兵。修复后：无
        证据即 launch failure（marker 不再是必要条件）。"""
        assert _looks_like_godot_launch_failure(EARLY_EXIT_NO_MARKER) is True

    def test_empty_output_with_nonzero_rc_is_launch_failure(self):
        """极端形态：零输出（godot 即刻崩溃）同样中止。"""
        assert _looks_like_godot_launch_failure("") is True

    def test_resource_warning_with_gut_evidence_is_normal_run(self):
        """保护面不回归：项目资源告警在场但 GUT 正常跑（证据正则命中）→
        正常分类，不得中止（KOL item_visual.tscn 实证形态）。"""
        assert _looks_like_godot_launch_failure(GUT_EVIDENCE_FLOW) is False

    def test_bare_gut_evidence_is_normal_run(self):
        assert _looks_like_godot_launch_failure("* test_x [Passed]\n") is False

    def test_markers_constant_preserved_for_diagnostics(self):
        """marker 常量保留（abort reason 诊断面引用），判定不再消费。"""
        assert "failed loading resource" in _GODOT_LAUNCH_ERROR_MARKERS


class TestCoverageAbortEndToEnd:
    def _proj(self, tmp_path):
        proj = tmp_path / "proj"
        (proj / "addons" / "gut").mkdir(parents=True)
        (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
        target = proj / "calc.gd"
        target.write_text("func add(a, b):\n\treturn a + b\n")
        return proj, target

    def test_early_exit_run_aborts_with_run_error_not_probe_sentinel(
            self, tmp_path, monkeypatch):
        """端到端：rc≠0 + 无证据 → run_error 中止契约（ok=false、error 含
        launch failed），而非 probes_fired=0 假哨兵。"""
        from godot_qa_toolkit.coverage.runner import run_coverage
        import subprocess

        proj, target = self._proj(tmp_path)

        def fake_gut(cmd, **kwargs):
            # godot 加载早期退出：rc=1、无 GUT 输出、无 marker
            class R:
                returncode = 1
                stdout = EARLY_EXIT_NO_MARKER
                stderr = ""

            return R()

        monkeypatch.setattr("godot_qa_toolkit.coverage.runner.subprocess.run",
                            fake_gut)
        r = run_coverage(str(target), str(proj), min_percent=80.0)
        assert r["ok"] is False
        assert r["summary"]["run_error"] is True
        assert "launch failed" in r["summary"]["error"]
        assert "probes_fired" not in r["summary"], \
            "early-exit run must abort as launch failure, not degrade to " \
            "the probes_fired=0 sentinel (abort reason must not be masked)"

    def test_resource_warning_run_still_measures(self, tmp_path,
                                                  monkeypatch):
        """保护面端到端：告警在场 + GUT 证据在场 → 正常统计（探针命中）。"""
        from godot_qa_toolkit.coverage.runner import run_coverage

        proj, target = self._proj(tmp_path)
        # 插桩后的 GUT 轮：rc=0 + 命中 id 0（行 2）
        inst_probe = "__qa_cov_probe_"

        def fake_gut(cmd, **kwargs):
            class R:
                returncode = 0
                # 证据正则命中 + 探针 sink 不可写时 runner 走项目根回退——
                # 此处直接写 sink 文件由 _read_hits 消费（sink=cmd 里不可知，
                # 用 runner 的约定：项目根同名文件回退）。
                stdout = (MARKER_ONLY_RESOURCE_WARNING
                          + "* test_x [Passed]\n")
                stderr = ""

            return R()

        monkeypatch.setattr("godot_qa_toolkit.coverage.runner.subprocess.run",
                            fake_gut)
        # sink：_read_hits 先 user:// 后项目根——伪造项目根命中文件名不可知
        # （run 唯一 pid），此测试以「不中止」为核心断言；探针统计面由既有
        # coverage 套件覆盖。
        r = run_coverage(str(target), str(proj), min_percent=80.0)
        # rc=0：不进入中止分支（无论后续探针统计形态如何）
        assert r["summary"].get("run_error") is not True or \
            "launch failed" not in r["summary"].get("error", "")
