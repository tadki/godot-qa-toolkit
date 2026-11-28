"""SEE-1648 C3-R23 R1：工具链面新鲜度（Leader 对 0d14f98 的复核退回）。

- 新鲜度 = 磁盘树面（`recipe_hash_pristine`，不含 toolchain）与工具链面
  （注入后的 `report_recipe_hash`）**同时**一致——只换 Godot / GUT / gqt
  版本（或换 `GQT_GODOT_BIN` 指向的版本）而磁盘树不变时，地图必须判
  不新鲜、`would_refresh=full`、因素带 `report_recipe_hash:` 前缀。
- 0d14f98（C3-R22）上 `status` 只在 stale 路径探测工具链面（fresh 路径
  `if fresh: … return` 不探测）、`assert_map_fresh` 只比 pristine、
  `cli._attribution_usable` 与 coverlink 的 pristine 比对同样只看磁盘
  树面——本文件全部判据在 0d14f98 产品代码上为红。
- 合成夹具一律携带真实存储哈希（注入域现算），不得用占位哈希。
"""

from __future__ import annotations

import json
import os
import subprocess as sp
import sys
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


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


def _write_real_map(proj: Path) -> dict:
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).parent))
    from attribution_fixtures import write_map

    write_map(proj, {"prod.gd": ["test_a.gd::test_x"]})
    return json.loads(
        (proj / ".qa-cache" / "attribution-map.json").read_text(
            encoding="utf-8"))


def _save_doc(proj: Path, doc: dict) -> None:
    (proj / ".qa-cache" / "attribution-map.json").write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def _drift_toolchain_face(proj: Path) -> dict:
    """真实地图 + 只换工具链面（存储 report_recipe_hash 换值）——磁盘树
    与 pristine 面零改动，唯一失配面 = 工具链。"""
    doc = _write_real_map(proj)
    doc["report_recipe_hash"] = "drifted-toolchain-hash-00000000"
    _save_doc(proj, doc)
    return doc


def _run_py(proj: Path, code: str, env_extra: dict) -> str:
    """隔离子进程跑一段取值脚本（跨进程绕开进程内缓存——环境差异必须
    跨进程才真实成立）。返回 stdout 去尾。"""
    import site

    prelude = (
        "import sys; sys.path.insert(0, "
        f"{json.dumps(str(_SRC_ROOT))}); "
        f"sys.path.append({json.dumps(site.getusersitepackages())}); "
    )
    home = proj.parent / "home"
    tmp = proj.parent / "tmp"
    home.mkdir(exist_ok=True)
    tmp.mkdir(exist_ok=True)
    env = {
        "PATH": str(tmp),          # 空 PATH：无 bin 环境不解析到任何 godot
        "HOME": str(home),
        "PYTHONPATH": "",
        "PYTHONUSERBASE": str(home),
        "TMPDIR": str(tmp),
    }
    env.update(env_extra)
    r = sp.run([sys.executable, "-c", prelude + code, str(proj)],
               env=env, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr[-600:]
    return r.stdout.strip()


# ───────────────── R1-①：status fresh 路径同样比对工具链面 ─────────────────
def test_status_fresh_path_probes_toolchain_face(tmp_path):
    """只换工具链版本（存储哈希变化）而磁盘树不变 → status 判不新鲜、
    would_refresh=full、因素带 report_recipe_hash: 前缀（0d14f98 上
    fresh 路径不探测 → 判新鲜，本测为红）。"""
    proj = _make_project(tmp_path)
    _drift_toolchain_face(proj)
    from godot_qa_toolkit.mutation.map_contract import status

    st = status(str(proj), ["prod.gd"])
    factors = st.get("factors") or []
    assert st.get("fresh") is False, factors
    assert st.get("would_refresh") == "full", factors
    assert any(f.startswith("report_recipe_hash:") for f in factors), \
        ("工具链面漂移必须具名", factors)


def test_status_fresh_path_probes_bin_target_change(tmp_path, monkeypatch):
    """换 `GQT_GODOT_BIN` 指向的版本（另一支版本串不同的二进制）而磁盘
    树不变 → 同判不新鲜（0d14f98 上 fresh 路径不探测 → 判新鲜）。"""
    proj = _make_project(tmp_path)
    _write_real_map(proj)
    other = tmp_path / "other-godot"
    other.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then '
                     'echo "9.9.9-stable.official.other"; exit 0; fi\n'
                     'exit 0\n', encoding="utf-8")
    other.chmod(0o755)
    monkeypatch.setenv("GQT_GODOT_BIN", str(other))
    from godot_qa_toolkit.mutation.map_contract import status

    st = status(str(proj), ["prod.gd"])
    factors = st.get("factors") or []
    assert st.get("fresh") is False, factors
    assert st.get("would_refresh") == "full", factors
    assert any(f.startswith("report_recipe_hash:") for f in factors), \
        ("bin 指向变化必须具名", factors)


# ───────────────── R1-②：无 bin 进程对 bin 建图判不新鲜 ─────────────────
def test_no_bin_process_names_bin_built_map_stale(tmp_path):
    """带 bin 子进程现算注入域哈希存进地图，无 `GQT_GODOT_BIN` 且 PATH
    无 godot 的子进程里 `status` 判不新鲜、具名、full（0d14f98 上
    fresh 路径不探测 → 判新鲜）。

    C3-R24（N3）：「带 bin」一侧用本地打印版本号的假脚本自给自足——
    不读运行环境的 `GQT_GODOT_BIN`；否则零 Godot 环境（CI）下两个子
    进程环境相同、哈希相同 → fresh 断言自败，合并门必挡。"""
    proj = _make_project(tmp_path)
    fake_bin = proj.parent / "fake-version-godot"
    fake_bin.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then '
        'echo "9.9.9-stable.official.fake-c3r24"; exit 0; fi\n'
        'exit 0\n', encoding="utf-8")
    fake_bin.chmod(0o755)
    h_bin = _run_py(
        proj,
        "from godot_qa_toolkit.mutation.truth_select import "
        "_current_report_recipe_hash; "
        "h = _current_report_recipe_hash(sys.argv[1]); "
        "print(h if h else '')",
        {"GQT_GODOT_BIN": str(fake_bin)})
    assert h_bin, "注入域哈希必须可计算"
    doc = _write_real_map(proj)
    doc["report_recipe_hash"] = h_bin
    _save_doc(proj, doc)
    st_json = _run_py(
        proj,
        "import json; "
        "from godot_qa_toolkit.mutation.map_contract import status; "
        "st = status(sys.argv[1], ['prod.gd']); "
        "print(json.dumps({'fresh': st.get('fresh'), "
        "'would_refresh': st.get('would_refresh'), "
        "'factors': st.get('factors') or []}))",
        {})
    st = json.loads(st_json)
    assert st["fresh"] is False, st
    assert st["would_refresh"] == "full", st
    assert any(f.startswith("report_recipe_hash:") for f in st["factors"]), \
        ("无 bin 进程必须具名工具链面漂移", st)


# ───────────────── R1-③：feature 内容改动仍增量（不回退） ─────────────────
def test_feature_content_change_stays_increment(tmp_path):
    """只改 feature 内容 → 仍增量、因素带 feature_sha256:（工具链面
    一致时内容面语义零变化——R1 修法不得把内容增量误判为全量）。"""
    proj = _make_project(tmp_path)
    _write_real_map(proj)
    feat = proj / "addons" / "gqt_gherkin" / "features" / "demo.feature"
    feat.write_text(feat.read_text(encoding="utf-8") + "# touched\n",
                    encoding="utf-8")
    from godot_qa_toolkit.mutation.map_contract import status

    st = status(str(proj), ["prod.gd"])
    factors = st.get("factors") or []
    assert st.get("fresh") is False, factors
    assert st.get("would_refresh") == "increment", \
        ("内容面改动必须仍走增量", factors)
    assert any(f.startswith("feature_sha256") for f in factors), factors


# ───────────────── R1-④：run 侧谓词（assert_map_fresh） ─────────────────
def test_assert_map_fresh_names_toolchain_drift(tmp_path):
    """run 侧共享谓词：工具链面漂移 → 具名 fail-loud（0d14f98 上只比
    pristine → 不抛，本测为红）。"""
    proj = _make_project(tmp_path)
    doc = _drift_toolchain_face(proj)
    from godot_qa_toolkit.mutation.verdict_source import (
        BaselineSourceError, assert_map_fresh,
    )

    with pytest.raises(BaselineSourceError) as ei:
        assert_map_fresh(str(proj), doc, ["prod.gd"])
    assert "report_recipe_hash" in str(ei.value), str(ei.value)


def test_assert_map_fresh_passes_consistent_toolchain_face(tmp_path):
    """磁盘树面与工具链面都一致 → 仍判新鲜（R1 修法不得把一致地图误判
    过期——0d14f98 与修后都必须绿）。"""
    proj = _make_project(tmp_path)
    doc = _write_real_map(proj)
    from godot_qa_toolkit.mutation.verdict_source import assert_map_fresh

    assert_map_fresh(str(proj), doc, ["prod.gd"])


# ───────────────── R1-⑤：选面出口具名原因 ─────────────────
def test_selection_reason_names_toolchain_drift(tmp_path):
    """`_evaluate` 的 granular reason 映射：工具链面漂移 → 具名
    report_recipe_hash_drift（0d14f98 上直接判新鲜 → runtime_mapping）。"""
    proj = _make_project(tmp_path)
    _drift_toolchain_face(proj)
    from godot_qa_toolkit.mutation.truth_select import resolve_selection

    sel = resolve_selection(str(proj), str(proj / "prod.gd"), strict=False,
                            renew=False)
    assert sel.get("reason") == "report_recipe_hash_drift", sel


# ───────────────── R1-⑥：cli._attribution_usable 口径一致 ─────────────────
def test_attribution_usable_rejects_toolchain_drift(tmp_path):
    """产物可用性校验：存储哈希与现算注入域不一致 → 不可用
    （0d14f98 上只比 pristine + mtime → 可用，本测为红）。"""
    proj = _make_project(tmp_path)
    _write_real_map(proj)
    map_file = proj / ".qa-cache" / "attribution-map.json"
    doc = json.loads(map_file.read_text(encoding="utf-8"))
    doc["report_recipe_hash"] = "drifted-toolchain-hash-00000000"
    _save_doc(proj, doc)
    from godot_qa_toolkit.cli import _attribution_usable

    ok = _attribution_usable(str(map_file), 0.0, project_root=str(proj))
    assert ok is False


# ───────────────── R1-⑦：coverlink pristine 比对口径一致 ─────────────────
def test_projection_map_reject_names_toolchain_drift(tmp_path):
    """coverlink 投影前置校验链：工具链面漂移 → 具名拒绝
    （0d14f98 上只比 pristine → 放行，本测为红）。"""
    proj = _make_project(tmp_path)
    doc = _drift_toolchain_face(proj)
    from godot_qa_toolkit.mutation.coverlink import _projection_map_reject

    why = _projection_map_reject(
        proj, doc, "prod.gd", doc.get("recipe_hash_pristine"),
        doc.get("tests_face") or {})
    assert why is not None, "工具链面漂移必须被投影前置校验拒绝"
    assert "report_recipe_hash" in why or "toolchain" in why, why
