"""§SPEC-1420-02：--timeout-factor F（默认 2.0 不动）覆盖 _TIMEOUT_SCALE。

定案 §一 L5（小改采纳）：adaptive timeout = max(timeout_s, baseline×F+1)；
默认 2.0 与现行语义逐位一致；UB 默认 10 不抄；超时语义保持 gqt
（timeout≠killed）。F 不进 resume key（定案 D6 负面清单）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import runner as mrunner

TARGET_SRC = (
    "func add(a, b):\n"
    "\tif a < b:\n"
    "\t\treturn a + b\n"
    "\treturn b\n"
)


def _write_project(tmp_path, src=TARGET_SRC):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    target = proj / "calc.gd"
    target.write_text(src)
    return proj, target


class TestAdaptiveTimeoutFormula:
    def test_default_factor_is_module_scale(self):
        # max(60, 50×2.0+1) = 101
        assert mrunner.adaptive_timeout_for(50.0, 60, None) == 101

    def test_explicit_factor_overrides_scale(self):
        # max(60, 50×3.0+1) = 151
        assert mrunner.adaptive_timeout_for(50.0, 60, 3.0) == 151

    def test_never_below_explicit_timeout(self):
        # 只放大不收紧：派生值低于显式 timeout 时显式值赢
        assert mrunner.adaptive_timeout_for(0.5, 60, 3.0) == 60

    def test_zero_elapsed_falls_back_to_explicit(self):
        assert mrunner.adaptive_timeout_for(0.0, 60, 2.0) == 60


class TestTimeoutFactorEndToEnd:
    def test_summary_adaptive_timeout_reflects_factor(self, tmp_path,
                                                      monkeypatch):
        proj, target = _write_project(tmp_path)
        captured = {"timeouts": []}

        def fake_baseline(root, tests_glob, timeout_s):
            return (0, "baseline ok", 50.0)

        def fake_gut(root, timeout_s=60, tests_glob=None):
            captured["timeouts"].append(timeout_s)
            return (0, "all passed")

        monkeypatch.setattr(mrunner, "_run_baseline", fake_baseline)
        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake_gut)
        r = mrunner.run_mutation(str(target), str(proj), budget=4,
                                 tests_glob="res://tests/",
                                 timeout_factor=3.0)
        assert r["summary"]["adaptive_timeout_s"] == 151
        # baseline 之后的每个 mutant 轮都用放大后的上限
        assert captured["timeouts"] and all(
            t == 151 for t in captured["timeouts"])

    def test_default_behavior_unchanged_without_flag(self, tmp_path,
                                                     monkeypatch):
        proj, target = _write_project(tmp_path)
        monkeypatch.setattr(mrunner, "_run_baseline",
                            lambda root, tests_glob, timeout_s:
                            (0, "baseline ok", 50.0))
        monkeypatch.setattr(mrunner, "_run_gut_on_project",
                            lambda root, timeout_s=60, tests_glob=None:
                            (0, "all passed"))
        r = mrunner.run_mutation(str(target), str(proj), budget=4,
                                 tests_glob="res://tests/")
        # 默认 2.0 不动：max(60, 50×2.0+1) = 101
        assert r["summary"]["adaptive_timeout_s"] == 101
