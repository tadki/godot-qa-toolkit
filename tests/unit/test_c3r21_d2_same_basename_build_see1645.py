"""SEE-1645 C3-R21 D2：真实建图路径的同 basename 同名测试身份保留。

§SPEC-030 三句话在**建图路径**上成立：不同目录的同名测试按完整脚本
路径各自保留身份、碰撞具名披露、绿测不得并入红身份。

f09d5f0 的 GUT 注册表身份是裸 basename（gqt_cov 钩子的脚本身份取文件
名形态）——`tests/a/test_green.gd::test_sign_positive`（绿）与
`tests/b/test_green.gd::test_sign_positive`（红）折叠成单键，verdict 表
只剩一条 red，绿测被并入红身份且零披露；消歧机制
（attribution.py::_emit_collided）的输入候选在 arm 阶段已折叠成 1 个，
分支永不触发。本回归以真实 Godot 建图钉住（QA 复现用例转常驻）。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess as sp
from pathlib import Path

import pytest

FORMS_TEMPLATE = Path(__file__).parent.parent / "fixtures" / "see1550_forms"


def _real_godot_bin() -> "str | None":
    import subprocess as _sp

    cand = os.environ.get("GQT_GODOT_BIN") or shutil.which("godot")
    if not cand or not Path(cand).is_file():
        return None
    try:
        r = _sp.run([str(cand), "--version"], capture_output=True,
                    text=True, timeout=60)
    except (OSError, _sp.TimeoutExpired):
        return None
    if "stub" in (r.stdout or ""):
        return None
    return str(cand)


def _gut_addon_src() -> "Path | None":
    for cand in (os.environ.get("GQT_FORMS_GUT_DIR", ""),
                 str(Path.home() / "tools" / "KingOfLikes-Godot" / "addons"
                     / "gut")):
        p = Path(cand) if cand else None
        if p is not None and (p / "gut_cmdln.gd").is_file():
            return p
    return None


def _build_project(tmp_path: Path) -> Path:
    """真实 Godot 项目夹具：a/ 绿与 b/ 红的同 basename 同名测试 +
    红测试（判定宇宙两候选、红集一路径匹配）。"""
    gut_src = _gut_addon_src()
    if gut_src is None:
        pytest.skip("real GUT addon unavailable")
    root = tmp_path / "proj"
    shutil.copytree(FORMS_TEMPLATE, root)
    shutil.copytree(gut_src, root / "addons" / "gut")
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).parent))
    from attribution_fixtures import install_recipe
    from gherkin_fixtures import ensure_addon_metadata

    install_recipe(root)
    ensure_addon_metadata(root)
    (root / "calc_sign.gd").write_text(
        "extends RefCounted\nfunc sign(x: int) -> int:\n"
        "\tif x > 0:\n\t\treturn 1\n\treturn -1\n", encoding="utf-8")
    (root / "tests" / "a").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "b").mkdir(parents=True, exist_ok=True)
    # 同 basename 同名：a/ 绿、b/ 红（断言反转）
    (root / "tests" / "a" / "test_green.gd").write_text(
        'extends GutTest\nconst C = preload("res://calc_sign.gd")\n'
        "func test_sign_positive():\n\tassert_eq(C.new().sign(5), 1)\n",
        encoding="utf-8")
    (root / "tests" / "b" / "test_green.gd").write_text(
        'extends GutTest\nconst C = preload("res://calc_sign.gd")\n'
        "func test_sign_positive():\n\tassert_eq(C.new().sign(5), -1)\n",
        encoding="utf-8")
    (root / ".gqt-scope.json").write_text(json.dumps({
        "production": {"include": [{"kind": "file",
                                    "value": "calc_sign.gd"}]}}),
        encoding="utf-8")
    return root


def _build_map(root: Path, monkeypatch) -> dict:
    """真实建图一次（契约 refresh → create），返回地图文档。"""
    real_godot = _real_godot_bin()
    if real_godot is None:
        pytest.skip("real godot binary unavailable")
    monkeypatch.setenv("GQT_GODOT_BIN", real_godot)
    monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
    monkeypatch.setenv("GQT_AUTO_IMPORT", "1")
    sp.run([real_godot, "--headless", "--import", "--path", str(root)],
           capture_output=True, timeout=550)
    from godot_qa_toolkit.mutation.map_contract import refresh

    out = refresh(str(root), ["calc_sign.gd"], timeout_s=500)
    assert out.get("fresh") is True
    map_file = root / ".qa-cache" / "attribution-map.json"
    return json.loads(map_file.read_text(encoding="utf-8"))


def test_same_basename_tests_keep_full_path_identities(tmp_path,
                                                       monkeypatch):
    """真实建图：同 basename 同名测试各自保留完整路径身份、碰撞披露、
    绿测不并入红身份（f09d5f0 上折叠成单条 red —— 本测为红）。"""
    root = _build_project(tmp_path)
    doc = _build_map(root, monkeypatch)
    table = doc.get("test_verdicts") or {}
    a_ident = "res://tests/a/test_green.gd::test_sign_positive"
    b_ident = "res://tests/b/test_green.gd::test_sign_positive"
    # ① 完整路径身份各自在场：a/ 绿、b/ 红
    assert table.get(a_ident, {}).get("verdict") == "green", \
        {k: v.get("verdict") for k, v in table.items()}
    assert table.get(b_ident, {}).get("verdict") == "red", \
        {k: v.get("verdict") for k, v in table.items()}
    # ② 不再折叠成 basename 键（f09d5f0 的折叠形态不得回归）
    assert "test_green.gd::test_sign_positive" not in table
    # ③ 碰撞具名披露（identity_collisions 在场，含同 basename 候选）
    collisions = doc.get("identity_collisions") or []
    assert any(c.get("basename_key") == "test_green.gd::test_sign_positive"
               and len(c.get("candidates") or []) >= 2 for c in collisions), \
        collisions


def test_same_basename_consumption_refuses_to_merge(tmp_path, monkeypatch):
    """消费路径：同 basename 测试脚本的行级选面按既有契约具名拒绝
    （ambiguous_test_names），不静默合并两边身份（§SPEC-030 消费面）。"""
    from godot_qa_toolkit.mutation import truth_select
    from godot_qa_toolkit.mutation.truth_lines import line_identity_sets

    root = _build_project(tmp_path)
    doc = _build_map(root, monkeypatch)
    rel = "calc_sign.gd"
    a_ident = "res://tests/a/test_green.gd::test_sign_positive"
    b_ident = "res://tests/b/test_green.gd::test_sign_positive"
    ident_sets, _lane = line_identity_sets(doc, rel, {3})
    raw = set()
    for ids in ident_sets.values():
        raw.update(ids)
    assert raw == {a_ident, b_ident}, raw
    with pytest.raises(truth_select.MutationSelectionError) as ei:
        truth_select.resolve_line_group_selection(str(root), str(root /
                                                 "calc_sign.gd"), [3])
    assert "ambiguous_test_names" in str(ei.value)
