"""SEE-1604 D2：`mutation --dry-run` 零 GUT 契约（renew 抑制）。

缺陷（QA D2）：dry-run 在陈旧/自锁映射上仍触发 auto-renew 全链 GUT。
判据：dry-run 的选面解析以 allow_renew=False 运行——陈旧映射零续新、零 GUT，
降级为未圈定清点（tests_glob=None）并披露原因；新鲜映射仍正常披露选面。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from godot_qa_toolkit.mutation import truth_select, runhelp
from attribution_fixtures import write_map

TARGET = "func add(a, b):\n\treturn a + b\n"


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "tests" / "unit").mkdir(parents=True)
    (proj / "tests" / "unit" / "test_hit.gd").write_text(
        "extends GutTest\nfunc test_ok() -> void:\n\tpass\n")
    (proj / "calc.gd").write_text(TARGET)
    return proj


def _spy_renew(monkeypatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(
        truth_select, "_resolve_renew",
        lambda *a, **k: calls.append("renew") or False)
    return calls


def test_dry_run_meta_suppresses_renew_on_stale_map(tmp_path, monkeypatch):
    monkeypatch.setenv(truth_select.ENV_MODE, "auto")
    proj = _project(tmp_path)
    write_map(proj, {"calc.gd": ["test_hit.gd::test_ok"]})
    (proj / "calc.gd").write_text(TARGET + "\n# drift\n")   # 映射过期
    calls = _spy_renew(monkeypatch)
    # 陈旧映射：dry-run 只关续新、保持 fail-loud（零续新 = 零 GUT）
    with pytest.raises(truth_select.MutationSelectionError) as ei:
        runhelp._auto_scan_tests_glob_meta(
            str(proj / "calc.gd"), str(proj),
            select_mode="line", allow_renew=False)
    assert calls == []
    assert "auto_update_still_stale" in ei.value.reason


def test_dry_run_meta_reports_fresh_selection_without_renew(tmp_path,
                                                            monkeypatch):
    monkeypatch.setenv(truth_select.ENV_MODE, "auto")
    proj = _project(tmp_path)
    write_map(proj, {"calc.gd": ["test_hit.gd::test_ok"]})
    calls = _spy_renew(monkeypatch)
    glob, meta = runhelp._auto_scan_tests_glob_meta(
        str(proj / "calc.gd"), str(proj),
        select_mode="line", allow_renew=False)
    assert calls == []
    assert meta.get("source") == "runtime_mapping"
    assert glob and glob.startswith("res://.gqt-attr-select/")


def test_cli_dry_run_on_stale_map_triggers_no_renew(tmp_path, monkeypatch):
    """端到端：CLI `mutation --dry-run` 陈旧映射 → 零续新（零 GUT）+ fail-loud。"""
    from godot_qa_toolkit import cli

    monkeypatch.setenv(truth_select.ENV_MODE, "auto")
    proj = _project(tmp_path)
    write_map(proj, {"calc.gd": ["test_hit.gd::test_ok"]})
    (proj / "calc.gd").write_text(TARGET + "\n# drift\n")
    calls = _spy_renew(monkeypatch)
    rc = cli.main(["mutation", str(proj / "calc.gd"),
                   "--project-root", str(proj), "--dry-run"])
    assert calls == []          # 零续新 = 零 GUT（旧行为会跑全链）
    # §SPEC-PLANFIRST：--dry-run 永不要求 map——无可用映射时成功返回
    # plan 全量（problems[] 含 map warning），不静默全脸、不 fail-loud
    assert rc == 0
