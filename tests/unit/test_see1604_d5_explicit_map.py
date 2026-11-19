"""SEE-1604 D5：显式 `--attribution-map` 只读透传。

缺陷（QA D5）：陈旧显式映射在 mode=auto 下被 `_resolve_renew` 原地改写
（os.replace 直写 override 路径）并触发行全链 GUT，违反"只读透传绝不重建"。
判据：显式陈旧映射 → 具名 fail-loud（explicit_map_stale），零续新、文件字节不变。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from godot_qa_toolkit.mutation import truth_select
from attribution_fixtures import write_map

TARGET = "func add(a, b):\n\treturn a + b\n"


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "tests" / "unit").mkdir(parents=True)
    (proj / "tests" / "unit" / "test_hit.gd").write_text(
        "extends GutTest\nfunc test_ok() -> void:\n\tpass\n")
    (proj / "calc.gd").write_text(TARGET)
    return proj


def test_explicit_stale_map_is_not_rewritten(tmp_path, monkeypatch):
    monkeypatch.setenv(truth_select.ENV_MODE, "auto")
    proj = _project(tmp_path)
    map_path = write_map(proj, {"calc.gd": ["test_hit.gd::test_ok"]})
    monkeypatch.setenv(truth_select.ENV_MAP, str(map_path))
    # 目标文件漂移 → 映射过期（file_sha256 绑定失配）
    (proj / "calc.gd").write_text(TARGET + "\n# drift\n")
    before = Path(map_path).read_bytes()
    calls: list[str] = []
    monkeypatch.setattr(
        truth_select, "_resolve_renew",
        lambda *a, **k: calls.append("renew") or False)
    with pytest.raises(truth_select.MutationSelectionError) as ei:
        truth_select.resolve_selection(proj, str(proj / "calc.gd"))
    # 显式路径过期 → 具名 fail-loud，且绝不续新（零 GUT）
    assert "explicit_map_stale" in ei.value.reason
    assert calls == []
    # 用户显式证据产物字节不变（provenance 不被销毁重建）
    assert Path(map_path).read_bytes() == before
