"""SEE-1631 C3 步③：mutation 接入行为判据（§SPEC-032 判据④⑤）。"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

FORMS_TEMPLATE = Path(__file__).parent.parent / "fixtures" / "see1550_forms"


def test_run_side_resolve_selection_renew_false(monkeypatch, tmp_path):
    """run 路径（含 ownership_for）固定 renew=False——解析器收到的
    renew 关键字为 False。"""
    import godot_qa_toolkit.mutation.truth_lines as tl
    from godot_qa_toolkit.mutation import truth_select as ts

    captured = {}

    def fake_resolve(project_root, target_file, map_path_override=None,
                     strict=True, timeout_s=None, renew=True):
        captured["renew"] = renew
        return {"source": "runtime_mapping", "mapping": {
            "file_sha256_bound": True, "path": "/x/m.json"},
            "tests": ["res://tests/unit/test_a.gd"], "zero_ref": False}

    monkeypatch.setattr(tl, "_ts",
                        lambda: type("TS", (), {
                            "resolve_selection": staticmethod(fake_resolve),
                            "load_map": staticmethod(
                                lambda p: {"arms": {"gut_full": {}}})})())
    owner = tl.ownership_for("/p", "/p/calc.gd")
    assert captured["renew"] is False


def test_plan_map_section_calls_status(monkeypatch, tmp_path):
    """plan 的 map 段改调契约 status（不进 resolve/preview 旧路径）。"""
    from godot_qa_toolkit.mutation import plancheck
    from pathlib import Path

    called = {"status": 0, "preview": 0}

    def fake_status(root, files=None):
        called["status"] += 1
        return {"present": False, "fresh": False,
                "factors": ["no attribution map"],
                "would_refresh": "create", "scope": ["missing"],
                "last_cost": "unknown"}
    from godot_qa_toolkit.mutation import map_contract as _mcp
    monkeypatch.setattr(_mcp, "status", fake_status)
    import godot_qa_toolkit.mutation.truth_select as _ts
    monkeypatch.setattr(_ts, "map_preview_state",
                        lambda r, t: called.__setitem__(
                            "preview", called["preview"] + 1) or {})
    probs = plancheck._map_problem(Path(tmp_path), str(tmp_path / "c.gd"))
    assert called["status"] == 1 and called["preview"] == 0
    codes = [p["code"] for p in probs]
    assert "attribution_map_missing_or_stale" in codes
    ev = probs[0]["evidence"]
    assert ev["would_refresh"] == "create"


def test_auto_increment_lock_recheck_merges(monkeypatch, tmp_path):
    """锁内新鲜度复检：map 已被并发对端刷到新鲜 → 合并返回，不跑增量。"""
    import godot_qa_toolkit.mutation.truth_select as ts

    calls = {"increment": 0}

    def fake_apply(root, path, changed, timeout_s, t0):
        calls["increment"] += 1
        return True, None
    monkeypatch.setattr(ts, "_increment_apply", fake_apply)
    from godot_qa_toolkit.mutation.verdict_source import BaselineSourceError
    monkeypatch.setattr(ts, "load_map",
                        lambda p: {"schema_version": "test-attribution/7",
                                   "arms_state": {"gut_full": {
                                       "state": "valid"}},
                                   "test_verdicts": {},
                                   "file_sha256": {"calc.gd": "x"},
                                   "tests_face": {}})
    from godot_qa_toolkit.mutation import verdict_source as _vs
    monkeypatch.setattr(_vs, "assert_map_fresh",
                        lambda *a, **k: None)  # 锁内复检判新鲜
    map_file = tmp_path / "m.json"
    map_file.parent.mkdir(parents=True, exist_ok=True)
    map_file.write_text("{}")
    renewed, err = ts._auto_increment("/p", str(map_file),
                                      {"file_sha256": {}}, "calc.gd",
                                      timeout_s=60)
    assert renewed is True and err is None
    assert calls["increment"] == 0, "复检 fresh 不再跑增量腿"


def test_d12_prepare_real_targets_exist(tmp_path, monkeypatch):
    """D12 不打桩真路径：宇宙路径经 prepare 流入 run_measurement 后，
    map target_files 逐一真实存在（res:// 形态漏转换回归的常驻哨兵）。"""
    import os
    import shutil
    from pathlib import Path as _P

    gut_src = None
    for cand in (os.environ.get("GQT_FORMS_GUT_DIR", ""),
                 "/mnt/d/GodotProjects/king-of-likes/addons/gut",
                 str(_P.home() / "tools" / "KingOfLikes-Godot" / "addons"
                     / "gut")):
        p = _P(cand) if cand else None
        if p is not None and (p / "gut_cmdln.gd").is_file():
            gut_src = p
            break
    if gut_src is None:
        pytest.skip("real GUT addon unavailable")
    from godot_qa_toolkit.mutation.verdict_source import (
        prepare_map_for_baseline,
    )

    root = tmp_path / "proj"
    shutil.copytree(FORMS_TEMPLATE, root)
    shutil.copytree(gut_src, root / "addons" / "gut")
    # recipe 可构建 + features 元数据（install_recipe/ensure_addon_metadata
    # 为 batteries 同款夹具助手）
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent))
    from attribution_fixtures import install_recipe
    from gherkin_fixtures import ensure_addon_metadata
    install_recipe(root)
    ensure_addon_metadata(root)
    # forms 模板无 utils/：目标写在根（production.include=file 粒度点名）
    (root / "calc_sign.gd").write_text(
        "extends RefCounted\nfunc sign(x: int) -> int:\n"
        "\tif x > 0:\n\t\treturn 1\n\treturn -1\n")
    (root / ".gqt-scope.json").write_text(json.dumps({
        "production": {"include": [{"kind": "file",
                                    "value": "calc_sign.gd"}]}}))
    # conftest 单元套件默认 MAP_MODE=off + 真空 stub godot——本测为真
    # 路径哨兵：显式 auto + 真 Godot 二进制
    monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
    monkeypatch.setenv("GQT_MUTATION_BASELINE_SOURCE", "map")
    import shutil as _sh
    real_godot = _sh.which("godot")
    if real_godot is None:
        pytest.skip("real godot binary unavailable")
    monkeypatch.setenv("GQT_GODOT_BIN", real_godot)
    # conftest 单元面关掉了自动导入（防误呼真引擎）——本测真路径要 GUT
    # 跑出执行证据，恢复自动导入并预导入一次（首跑 .godot 缓存）
    monkeypatch.setenv("GQT_AUTO_IMPORT", "1")
    import subprocess as _sp
    _sp.run([real_godot, "--headless", "--import", "--path", str(root)],
            capture_output=True, timeout=600)
    out = prepare_map_for_baseline(
        str(root), [str(root / "calc_sign.gd")], timeout_s=900)
    doc = json.loads((root / ".qa-cache" / "attribution-map.json")
                     .read_text(encoding="utf-8"))
    targets = doc.get("target_files") or []
    assert targets, "宇宙须覆盖生产文件"
    for rel in targets:
        assert (root / rel).is_file(), f"target 须真实存在: {rel}"
    states = {k: v.get("state")
              for k, v in (doc.get("arms_state") or {}).items()}
    assert states.get("gut_full") == "valid"
