"""SEE-1623 H4-A — acceptance 暂停期间普通 run 单臂可用 exit 语义行为钉。

Owner 2026-11-23 裁决 A（§SPEC-004 更新判据）：暂停期间普通 `measure
run`（无 --test-attribution）gut_full 臂有效且通过 → ok=true / exit 0、
certification=uncertified、具名单臂 exit_semantics，保留 gherkin_arm
暂停披露；gut_full 无效/失败仍 failed / exit 1；--test-attribution 路径
与显式 env=0 恢复双臂路径语义零变化。
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import godot_qa_toolkit.cli as cli

_PAUSE_DISCLOSURE = ("disabled_pending_atomization — gherkin arm paused "
                     "by default; restore via "
                     "GQT_MEASUREMENT_ACCEPTANCE_DISABLED=0 once SEE-1619 "
                     "atomization lands")


def _report(*, gut_state="valid", paused=True, union_ok=False,
            attribution=None):
    rep = {
        "report_version": "measurement-report/2",
        "arms": [
            {"arm": "gut_full", "state": gut_state, "wall_s": 1.2},
            {"arm": "acceptance",
             "state": "disabled" if paused else "valid", "wall_s": 0.0},
        ],
        "unions": [],
        "ok": union_ok,
    }
    if paused:
        rep["gherkin_arm"] = _PAUSE_DISCLOSURE
    if attribution is not None:
        rep["test_attribution"] = attribution
    return rep


class TestSingleArmDecision:
    def test_paused_plain_valid_single_arm_usable(self):
        summary, ok = cli._measure_run_summary(
            _report(gut_state="valid"), None, False)
        assert ok is True
        assert summary["certification"] == "uncertified"
        assert summary["exit_semantics"] == \
            "single_arm_usable_gherkin_paused"
        assert summary["union_certified"] is False
        assert summary["gherkin_arm"] == _PAUSE_DISCLOSURE

    def test_paused_plain_invalid_gut_stays_failed(self):
        summary, ok = cli._measure_run_summary(
            _report(gut_state="invalid"), None, False)
        assert ok is False
        assert summary["exit_semantics"] == "failed"
        assert summary["certification"] == "unusable"

    def test_paused_plain_missing_arms_stays_failed(self):
        rep = {"report_version": "measurement-report/2", "arms": [],
               "unions": [], "ok": False,
               "gherkin_arm": _PAUSE_DISCLOSURE}
        _summary, ok = cli._measure_run_summary(rep, None, False)
        assert ok is False

    def test_no_pause_disclosure_no_single_arm(self):
        """恢复双臂形态（无暂停披露）union 无效 → failed（零变化）。"""
        rep = _report(gut_state="valid", paused=False, union_ok=False)
        summary, ok = cli._measure_run_summary(rep, None, False)
        assert ok is False
        assert summary["exit_semantics"] == "failed"

    def test_attribution_path_usable_unchanged(self):
        summary, ok = cli._measure_run_summary(
            _report(gut_state="valid"), "/tmp/map.json", True)
        assert ok is True
        assert summary["exit_semantics"] == \
            "artifacts_usable_union_uncertified"
        assert summary["attribution_map"] == "/tmp/map.json"

    def test_attribution_path_unusable_unchanged(self):
        """归属路径映射不可用 → failed（单臂分支不适用于归属路径）。"""
        summary, ok = cli._measure_run_summary(
            _report(gut_state="valid"), "/tmp/map.json", False)
        assert ok is False
        assert summary["exit_semantics"] == "failed"

    def test_dual_arm_certified_unchanged(self):
        summary, ok = cli._measure_run_summary(
            _report(gut_state="valid", paused=False, union_ok=True),
            None, False)
        assert ok is True
        assert summary["certification"] == "certified"
        assert summary["exit_semantics"] == "certified"


class TestMeasureRunCliExit:
    def _drive(self, tmp_path, monkeypatch, report):
        root = tmp_path / "proj"
        (root / "src").mkdir(parents=True)
        (root / "src" / "calc.gd").write_text(
            "extends RefCounted\n\nfunc add(a, b):\n\treturn a + b\n")
        recipe_path = tmp_path / "recipe.json"
        recipe_path.write_text(json.dumps(
            {"recipe_version": "measurement-suite/2",
             "gut_face": {"dirs": []}}), encoding="utf-8")
        monkeypatch.setattr(cli, "_apply_godot_bin_or_contract",
                            lambda *a, **k: None)
        monkeypatch.setattr(cli, "_resolve_project_root_or_contract",
                            lambda *a, **k: None)
        monkeypatch.setattr(cli, "_bootstrap_import_or_contract",
                            lambda *a, **k: None)
        import godot_qa_toolkit.measurement.orchestrator as orch
        import godot_qa_toolkit.measurement.recipe as recipe_mod
        monkeypatch.setattr(recipe_mod, "verify_recipe",
                            lambda r: (True, None))
        monkeypatch.setattr(orch, "run_measurement",
                            lambda *a, **k: dict(report))
        args = cli.build_parser().parse_args([
            "measure", "run", str(root / "src"), "--project-root",
            str(root), "--recipe", str(recipe_path),
            "--out", str(tmp_path / "out.json")])
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = args.func(args)
        return code, json.loads(buf.getvalue())

    def test_plain_run_healthy_exit_zero(self, tmp_path, monkeypatch):
        code, payload = self._drive(
            tmp_path, monkeypatch, _report(gut_state="valid"))
        assert code == 0
        assert payload["ok"] is True
        assert payload["failures"] == []
        s = payload["summary"]
        assert s["exit_semantics"] == "single_arm_usable_gherkin_paused"
        assert s["certification"] == "uncertified"
        assert "gherkin_arm" in s

    def test_plain_run_invalid_gut_exit_one_named(self, tmp_path,
                                                  monkeypatch):
        code, payload = self._drive(
            tmp_path, monkeypatch, _report(gut_state="invalid"))
        assert code == 1
        assert payload["ok"] is False
        assert payload["failures"] == [
            {"reason": "union invalid or arms invalid"}]
