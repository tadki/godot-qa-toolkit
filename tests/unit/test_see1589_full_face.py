"""SEE-1589 §SPEC-004（A-H1）：`--select full` 的 fail-loud 处置自洽。

判据：
- 低于预算上限：`suggested_action` 仍指向可执行的 --timeout 600；
- 已达/超过预算上限：不得回显同一 timeout（自相矛盾），必须给出可执行
  的窄面处置（--select line/fn、--lines/--mutation）并说明 full = 整套
  GUT 审计的规模前提；
- 真实 run_error 路径把 kind 一并落盘（机器可读）。
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import godot_qa_toolkit.mutation.runner as runner_mod
from godot_qa_toolkit.mutation.runner import run_mutation
from godot_qa_toolkit.timing import BUDGET_CAP_S, suggested_action, \
    suggested_action_kind

GD_TARGET = ("func add(a, b):\n"
             "\tif a > b:\n"
             "\t\treturn a - b\n"
             "\treturn a + b\n")


def _write_project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "addons" / "gut").mkdir(parents=True, exist_ok=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    tests = proj / "tests" / "unit"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "test_calc.gd").write_text('preload("res://calc.gd")\n')
    target = proj / "calc.gd"
    target.write_text(GD_TARGET)
    return str(proj), str(target)


def test_below_cap_still_suggests_raising_timeout():
    text = suggested_action(60)
    assert f"--timeout {BUDGET_CAP_S}" in text
    assert suggested_action_kind(60) == "raise_timeout"


def test_at_cap_never_echoes_same_timeout():
    text = suggested_action(BUDGET_CAP_S)
    assert text != f"rerun with --timeout {BUDGET_CAP_S}"
    assert f"--timeout {BUDGET_CAP_S}" not in text, \
        f"已达上限仍回显同一 timeout（自相矛盾）: {text}"
    assert "--select" in text
    assert "line" in text or "fn" in text
    assert suggested_action_kind(BUDGET_CAP_S) == "narrow_face"


def test_above_cap_also_narrows():
    assert suggested_action_kind(BUDGET_CAP_S + 600) == "narrow_face"


def _raise_timeout(*a, **k):
    raise subprocess.TimeoutExpired(cmd="gut", timeout=BUDGET_CAP_S)


def test_full_face_timeout_run_error_is_actionable(tmp_path, monkeypatch):
    proj, target = _write_project(tmp_path)
    monkeypatch.setattr(runner_mod, "_run_baseline", _raise_timeout)
    result = run_mutation(target, proj, budget=2, timeout_s=BUDGET_CAP_S,
                          select_mode="full")
    summary = result["summary"]
    assert result["ok"] is False and summary.get("run_error") is True
    action = summary.get("suggested_action") or ""
    assert action and "--select" in action, action
    assert f"rerun with --timeout {BUDGET_CAP_S}" != action
    assert summary.get("suggested_action_kind") == "narrow_face"


def test_full_face_timeout_below_cap_keeps_raise_action(tmp_path, monkeypatch):
    proj, target = _write_project(tmp_path)

    def _raise_60(*a, **k):
        raise subprocess.TimeoutExpired(cmd="gut", timeout=60)

    monkeypatch.setattr(runner_mod, "_run_baseline", _raise_60)
    result = run_mutation(target, proj, budget=2, timeout_s=60,
                          select_mode="full")
    summary = result["summary"]
    assert summary.get("suggested_action_kind") == "raise_timeout"
    assert f"--timeout {BUDGET_CAP_S}" in summary.get("suggested_action", "")
