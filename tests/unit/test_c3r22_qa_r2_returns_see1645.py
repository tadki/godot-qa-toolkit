"""SEE-1645 C3-R22：QA-R2 退回（D3 pristine 哈希环境依赖 + 具名短路）
的常驻回归。

- D3-a：`_fresh_recipe_hash`（raw / pristine 面）在同一份磁盘树上与
  `GQT_GODOT_BIN` 的有无无关——docstring 承诺「不含 toolchain 注入；
  零 godot 依赖」。f09d5f0..72dca1c 的实现把 `toolchain` 版本字典
  （godot_version 随 GQT_GODOT_BIN 有无变化）一并哈希 → 无 bin 环境
  pristine 失配、预言被强制 full。
- D3-b：pristine 失配的路径上，报告哈希漂移的原因同样列进 factors——
  `_planned_action` 的 `pristine_ok=False → ("full", None)` 短路不再吞掉
  D1 的具名因素。
"""

from __future__ import annotations

import json
import os
import subprocess as sp
import sys
from pathlib import Path

import pytest


def _make_project(tmp_path: Path) -> Path:
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).parent))
    from attribution_fixtures import install_recipe
    from gherkin_fixtures import ensure_addon_metadata

    proj = tmp_path / "proj"
    proj.mkdir(parents=True)
    install_recipe(proj)
    ensure_addon_metadata(proj)
    (proj / "tests" / "unit").mkdir(parents=True, exist_ok=True)
    (proj / "tests" / "unit" / "test_a.gd").write_text(
        "extends GutTest\n", encoding="utf-8")
    (proj / "prod.gd").write_text("extends Node\n", encoding="utf-8")
    return proj


def _fresh_hash_in_subprocess(proj: Path, *, with_bin: bool) -> str:
    """独立子进程求 `_fresh_recipe_hash`（绕开进程内 lru_cache 与
    `_TOOLCHAIN_CACHE`——环境差异必须跨进程才真实成立）。"""
    code = (
        "import sys; sys.path.insert(0, "
        f"{json.dumps(str(Path(__file__).resolve().parents[2] / 'src'))}); "
        "sys.path.append('/home/jerry/.local/lib/python3.12/site-packages'); "
        "from godot_qa_toolkit.mutation.truth_select import "
        "_fresh_recipe_hash; "
        "h = _fresh_recipe_hash(sys.argv[1]); "
        "print(h if h else '')"
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(proj.parent / "home"),
        "PYTHONPATH": "",
        "TMPDIR": str(proj.parent / "tmp"),
    }
    if with_bin:
        env["GQT_GODOT_BIN"] = os.environ.get(
            "GQT_GODOT_BIN", "/home/jerry/gqt-probe-bin/godot")
    (proj.parent / "home").mkdir(exist_ok=True)
    (proj.parent / "tmp").mkdir(exist_ok=True)
    r = sp.run([sys.executable, "-c", code, str(proj)], env=env,
               capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-400:]
    return r.stdout.strip()


# ─────────────────────────── D3-a ───────────────────────────────
def test_fresh_recipe_hash_is_environment_independent(tmp_path):
    """同一磁盘树：`_fresh_recipe_hash` 在有/无 GQT_GODOT_BIN 的环境下
    相等（pristine 面 = 磁盘树，不含 toolchain 版本；72dca1c 上无 bin
    环境返回不同哈希 → pristine 失配 → 预言被强制 full）。"""
    proj = _make_project(tmp_path)
    with_bin = _fresh_hash_in_subprocess(proj, with_bin=True)
    without_bin = _fresh_hash_in_subprocess(proj, with_bin=False)
    assert with_bin, "pristine 哈希必须可计算"
    assert with_bin == without_bin, (
        "pristine 面随环境漂移（toolchain 字段混入 raw 哈希）：",
        with_bin[:12], without_bin[:12])


# ─────────────────────────── D3-b ───────────────────────────────
def test_status_names_report_hash_drift_alongside_pristine(tmp_path):
    """pristine 失配（结构性 forced-full）路径上：报告哈希漂移原因同样
    列进 factors——`_planned_action` 的 pristine 短路不得吞掉 D1 的
    具名因素（72dca1c 上短路返回 (full, None)，因素缺席）。"""
    proj = _make_project(tmp_path)
    doc = {
        "schema_version": "test-attribution/7",
        "report_recipe_hash": "stored-build-hash-0000",
        "recipe_hash_pristine": "stale-by-construction",
        "project_root": str(proj),
        "identity": "res://script::test_name",
        "causal": {"enabled": False},
        "file_sha256": {"prod.gd": "deadbeef"},
        "target_files": ["prod.gd"],
        "tests_face": {},
        "arms": {"gut_full": {}},
        "arms_state": {"gut_full": {"state": "valid", "bundles": 1,
                                    "pairs": 0, "probes_fired": 1,
                                    "vacuous": False}},
        "test_verdicts": {},
        "feature_sha256": {},
        "notes": [],
    }
    cache = proj / ".qa-cache"
    cache.mkdir()
    (cache / "attribution-map.json").write_text(json.dumps(doc),
                                                encoding="utf-8")
    from godot_qa_toolkit.mutation.map_contract import status

    st = status(str(proj), ["prod.gd"])
    factors = st.get("factors") or []
    assert st.get("would_refresh") == "full"
    assert any(f.startswith("recipe:") for f in factors), factors
    assert any(f.startswith("report_recipe_hash:") for f in factors), \
        ("pristine 短路不得吞掉报告哈希漂移具名", factors)
