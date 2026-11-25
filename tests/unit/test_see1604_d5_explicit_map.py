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


def test_explicit_stale_map_enters_renewal(tmp_path, monkeypatch):
    """§SPEC-029（Owner 裁决，覆盖 D5 只读透传）：显式 `--attribution-map`
    不设只读例外——过期同走 prepare 阶梯（续新被调用；续新拒绝时仍具名
    explicit_map_stale 且产物字节不变）。"""
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
    # 续新被调用（被测桩拒绝续新）→ 终态仍具名 stale；显式来源披露保留
    assert calls == ["renew"]
    assert "explicit_map_stale" in ei.value.reason
    # 桩拒绝续新 → 用户显式证据产物字节不变（provenance 保护语义不变）
    assert Path(map_path).read_bytes() == before
