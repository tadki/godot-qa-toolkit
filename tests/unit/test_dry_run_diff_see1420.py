"""§SPEC-1420-18：`--dry-run` 升级为差分计数预估（定案 §一 L1 部分采纳
条款，P1 落地后实施）。

dry-run 输出差分后的 retry 计数预估；仍保持零 GUT 调用契约（SPEC-009）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest
from godot_qa_toolkit.mutation import runner as mrunner

def _pin_cold(monkeypatch):
    from godot_qa_toolkit.mutation.identity import COLD_SCHEME
    from godot_qa_toolkit.mutation import worker_channel as wc
    monkeypatch.setattr(wc, "resolve_exec_scheme",
                        lambda *a, **k: (COLD_SCHEME, None))
    monkeypatch.setattr(mrunner, "resolve_exec_scheme",
                        lambda *a, **k: (COLD_SCHEME, None))


def _add_gut(proj):
    (proj / "addons" / "gut" / "gut.gd").write_text("extends Node\n")


def _materialize_selection(proj, names):
    sel_dir = proj / ".gqt-attr-select" / "testsel"
    sel_dir.mkdir(parents=True, exist_ok=True)
    for n in names:
        (sel_dir / n).write_text(f'extends "res://tests/{n}"\n', encoding="utf-8")
    return "res://.gqt-attr-select/testsel/"


@pytest.fixture(autouse=True)
def _pin_cold_auto(monkeypatch):
    _pin_cold(monkeypatch)


SRC = ("func add(a, b):\n"
       "\treturn a + b\n"
       "\n"
       "func sub(a, b):\n"
       "\treturn a - b\n")

KILL_TAIL = "* test_x\n\t\ttest_x [Failed]"


def _write_project(tmp_path, src=SRC):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "addons" / "gut").mkdir(parents=True)
    # §SPEC-031（R4）：插件标记（真实 gut addon 形态；范围默认排除②）
    (proj / "addons" / "gut" / "plugin.cfg").write_text("[plugin]\n")
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    target = proj / "calc.gd"
    target.write_text(src)
    (proj / "addons" / "gut" / "gut.gd").write_text("extends Node\n")
    return proj, target


class TestDryRunDiffEstimate:
    def test_no_manifest_estimates_all_sites(self, tmp_path):
        proj, target = _write_project(tmp_path)
        r = mrunner.run_mutation(str(target), str(proj), budget=10,
                                 tests_glob=_materialize_selection(proj, ["test_a.gd"]), dry_run=True)
        s = r["summary"]
        assert s["dry_run"] is True
        assert s["retry_estimate"] == 6  # 全位点（无历史 → 全部会跑）
        assert s["executed_estimate"] == 6
        assert s["diff_available"] is False

    def test_zero_change_second_run_estimates_zero_retry(self, tmp_path,
                                                         monkeypatch):
        _pin_cold(monkeypatch)
        proj, target = _write_project(tmp_path)

        def kill_all(root, timeout_s=60, tests_glob=None):
            return (1, KILL_TAIL)

        monkeypatch.setattr(mrunner, "_run_gut_on_project", kill_all)
        glob = _materialize_selection(proj, ["test_a.gd"])
        mrunner.run_mutation(str(target), str(proj), budget=10,
                             tests_glob=glob)
        # 二轮同 stub：suspect 条目复用（SKIP_VERDICTS）→ resumed == 3
        r2 = mrunner.run_mutation(str(target), str(proj), budget=10,
                                  tests_glob=glob)
        assert r2["summary"].get("resumed") == 6, \
            f"resumed={r2['summary'].get('resumed')}"
        # 三轮 dry-run：全复用 → retry_estimate 0
        r = mrunner.run_mutation(str(target), str(proj), budget=10,
                                 tests_glob=glob, dry_run=True)
        s = r["summary"]
        assert s["diff_available"] is True
        assert s["retry_estimate"] == 0
        assert s["executed_estimate"] == 0

    def test_scope_change_estimates_only_changed_scope(self, tmp_path,
                                                       monkeypatch):
        proj, target = _write_project(tmp_path)

        def kill_all(root, timeout_s=60, tests_glob=None):
            return (1, KILL_TAIL)

        monkeypatch.setattr(mrunner, "_run_gut_on_project", kill_all)
        mrunner.run_mutation(str(target), str(proj), budget=10,
                             tests_glob=_materialize_selection(proj, ["test_a.gd"]))
        # 等位点数改写（b - a：仍单 `-`，3 位点）——scope 内容变 → 全 scope 重跑
        target.write_text(SRC.replace("return a - b", "return b - a"))
        r = mrunner.run_mutation(str(target), str(proj), budget=10,
                                 tests_glob=_materialize_selection(proj, ["test_a.gd"]), dry_run=True)
        s = r["summary"]
        assert s["diff_available"] is True
        # sub scope 全跑（3 个 AOR 位点），add 的 killed 复用不计入 retry
        assert s["retry_estimate"] == 3
        assert s["executed_estimate"] == 3

    def test_estimate_respects_budget_cap(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)

        def kill_all(root, timeout_s=60, tests_glob=None):
            return (1, KILL_TAIL)

        monkeypatch.setattr(mrunner, "_run_gut_on_project", kill_all)
        mrunner.run_mutation(str(target), str(proj), budget=10,
                             tests_glob=_materialize_selection(proj, ["test_a.gd"]))
        target.write_text(SRC.replace("return a - b", "return b - a"))
        r = mrunner.run_mutation(str(target), str(proj), budget=2,
                                 tests_glob=_materialize_selection(proj, ["test_a.gd"]), dry_run=True)
        s = r["summary"]
        assert s["retry_estimate"] == 3
        assert s["executed_estimate"] == 2  # budget 截断面

    def test_dry_run_still_zero_gut_calls(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)

        def fail_loud(root, timeout_s=60, tests_glob=None):
            raise AssertionError("dry-run must not invoke GUT")

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fail_loud)
        mrunner.run_mutation(str(target), str(proj), budget=10,
                             tests_glob=_materialize_selection(proj, ["test_a.gd"]), dry_run=True)
