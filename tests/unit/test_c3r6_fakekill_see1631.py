"""SEE-1631 C3-R6：新增失败测试后 full 面 map 源 mutation 不得假 kill。

两种情况（Leader 夹具复现）：
- A：新测试**不触达**产品代码（无归属事实）→ 执行账补 red 进表；
- B：新测试**触达**产品代码 → 补进表的 `script` 必须是完整 `res://`
  路径（物理跳过按完整路径命中）。

断言落在 mutation 判定数字与 skip-red 的 marked/requested 上：
新增失败测试续新之后，同一条 full 面 mutation 数字与基线一致，
`skip-red marked=N/N`（请求跳过几个红测就物理跳过几个）。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

GUT_CANDIDATES = (
    os.environ.get("GQT_FORMS_GUT_DIR", ""),
    "/mnt/d/GodotProjects/king-of-likes/addons/gut",
)


def _find_gut() -> str | None:
    for cand in GUT_CANDIDATES:
        if cand and (Path(cand) / "gut_cmdln.gd").is_file():
            return cand
    return None


def _cli(args: list[str], env: dict, project_root: Path) -> "tuple[int, str, str]":
    r = subprocess.run(
        [sys.executable, "-m", "godot_qa_toolkit.cli", *args,
         "--project-root", str(project_root)],
        capture_output=True, text=True, timeout=1800, env=env)
    return r.returncode, r.stdout, r.stderr


def _counts(out: str) -> dict:
    d = json.loads(out)
    s = d.get("summary") or {}
    return {k: s.get(k) for k in ("mutants", "killed", "survived", "suspect")}


@pytest.fixture(scope="module")
def r6_project(tmp_path_factory):
    """Leader 式小夹具：目录型白名单（src/ + data/），单测试目录 =
    绿测脚本 + 恒红脚本 + 填充脚本。返回 (proj, env, baseline_counts)。"""
    # 标准隔离环境只设 GQT_GODOT_BIN、PATH 上没有 godot（C3-R7 退回2：
    # 与 test_see1550_worker_battery 同款发现顺序）
    godot = os.environ.get("GQT_GODOT_BIN") or shutil.which("godot")
    gut = _find_gut()
    if godot is None or gut is None:
        pytest.skip("real godot / GUT addon unavailable")
    _here = str(Path(__file__).parent)
    if _here not in sys.path:
        sys.path.insert(0, _here)
    home = tmp_path_factory.mktemp("r6home")
    env = {**os.environ,
           "HOME": str(home), "XDG_DATA_HOME": str(home / ".xdg"),
           "TMPDIR": str(home), "PYTHONPATH": "src",
           "GQT_MEASUREMENT_GHERKIN_DISABLED": "1",
           "GQT_MUTATION_BASELINE_SOURCE": "map",
           "GQT_ATTRIBUTION_MAP_MODE": "auto",
           # 进程内 ensure_map 会把 GQT_ATTRIBUTION_MAP 永久写入 os.environ
           # （truth_select._auto_map_build）——同进程先跑的建图单测会把
           # 泄漏值带进来，使本夹具的默认路径 map 被判成「显式 map」并触发
           # D4 event_counts_stale 具名拒绝门（增量产物恒带该标志）。本模
           # 块契约 = 项目默认路径 auto map，置空恢复默认语义。
           "GQT_ATTRIBUTION_MAP": ""}
    proj = tmp_path_factory.mktemp("r6proj") / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "data").mkdir()
    (proj / "tests").mkdir()
    (proj / "project.godot").write_text(
        'config_version=5\n\n[application]\n\nconfig/name="r6"\n'
        'config/features=PackedStringArray("4.6")\n')
    (proj / "src" / "calc_sign.gd").write_text(
        "extends RefCounted\nfunc sign(x: int) -> int:\n\tif x > 0:\n"
        "\t\treturn 1\n\tif x < 0:\n\t\treturn -1\n\treturn 0\n\n"
        "func gate(v: int) -> bool:\n\tif v >= 10:\n\t\treturn true\n"
        "\treturn false\n")
    (proj / "data" / "table.csv").write_text("k,v\n")
    (proj / ".gqt-scope.json").write_text(json.dumps({
        "production": {"include": [{"kind": "dir", "value": "src"},
                                   {"kind": "dir", "value": "data"}]}}))
    (proj / ".gutconfig.json").write_text(json.dumps(
        {"dirs": ["res://tests"], "include_subdirs": True,
         "prefix": "test_"}))
    (proj / "tests" / "test_green.gd").write_text(
        'extends GutTest\nconst C = preload("res://src/calc_sign.gd")\n'
        "func test_pos():\n\tassert_eq(C.new().sign(5), 1)\n"
        "func test_neg():\n\tassert_eq(C.new().sign(-3), -1)\n"
        "func test_zero():\n\tassert_eq(C.new().sign(0), 0)\n")
    (proj / "tests" / "test_red_always.gd").write_text(
        'extends GutTest\nconst C = preload("res://src/calc_sign.gd")\n'
        "func test_red_always():\n\tassert_eq(C.new().sign(7), 1)\n"
        '\tfail_test("always red")\n')
    for i in range(3):
        (proj / "tests" / f"test_fill_{i:02d}.gd").write_text(
            "extends GutTest\nfunc test_fill():\n\tassert_eq(1, 1)\n")
    shutil.copytree(gut, proj / "addons" / "gut")
    from attribution_fixtures import install_recipe
    from gherkin_fixtures import ensure_addon_metadata
    install_recipe(proj)
    ensure_addon_metadata(proj)
    subprocess.run([godot, "--headless", "--import", "--path", str(proj)],
                   capture_output=True, timeout=600, env=env)
    rc, out, err = _cli(
        ["mutation", "src/calc_sign.gd", "--select", "full",
         "--auto-prepare"], env, proj)
    assert rc in (0, 1), err[-500:]
    base = _counts(out)
    assert base["killed"] and base["survived"], (base, err[-300:])
    return proj, env, base, godot


def _mutation_and_marked(proj, env) -> "tuple[dict, str]":
    rc, out, err = _cli(
        ["mutation", "src/calc_sign.gd", "--select", "full",
         "--auto-prepare"], env, proj)
    marked = re.search(r"skip-red marked=(\d+)/(\d+)", err)
    return _counts(out), (f"{marked.group(1)}/{marked.group(2)}"
                          if marked else "none")


def _renew(proj, env) -> None:
    rc, out, err = _cli(
        ["measure", "coverage", "src/calc_sign.gd", "--min-percent", "10"],
        env, proj)
    assert rc == 0, (rc, err[-400:], out[:200])


def _verdict_entry(proj: Path, needle: str) -> "dict | None":
    doc = json.loads(
        (proj / ".qa-cache" / "attribution-map.json").read_text(
            encoding="utf-8"))
    for k, v in (doc.get("test_verdicts") or {}).items():
        if needle in k:
            return v
    return None


def test_r6_case_a_noncov_red_test_no_fake_kill(r6_project):
    """情况 A：不触达产品代码的新失败测试——执行账补 red（无归属事实
    也进表），full 面判定数字与基线一致，skip-red marked=2/2。"""
    proj, env, base, _godot = r6_project
    (proj / "tests" / "test_new_red_nocov.gd").write_text(
        'extends GutTest\nfunc test_new_red_nocov():\n'
        '\tfail_test("no production touch")\n')
    _renew(proj, env)
    entry = _verdict_entry(proj, "test_new_red_nocov")
    assert entry is not None, "情况 A：新测试必须以执行账进判定表"
    assert entry.get("verdict") == "red"
    assert str(entry.get("script", "")).startswith("res://"), entry
    counts, marked = _mutation_and_marked(proj, env)
    assert counts == base, (base, counts)
    assert marked == "2/2", marked


def test_r6_case_b_cov_red_test_full_path_no_fake_kill(r6_project):
    """情况 B：触达产品代码的新失败测试——补表条目 script 为完整
    res:// 路径，full 面判定数字与基线一致，skip-red marked=3/3。"""
    proj, env, base, _godot = r6_project
    (proj / "tests" / "test_new_red_nocov.gd").write_text(
        'extends GutTest\nfunc test_new_red_nocov():\n'
        '\tfail_test("no production touch")\n')
    (proj / "tests" / "test_new_red_cov.gd").write_text(
        'extends GutTest\nconst C = preload("res://src/calc_sign.gd")\n'
        "func test_new_red_cov():\n\tassert_eq(C.new().gate(11), true)\n"
        '\tfail_test("touches production")\n')
    _renew(proj, env)
    entry = _verdict_entry(proj, "test_new_red_cov")
    assert entry is not None and entry.get("verdict") == "red"
    assert entry.get("script") == "res://tests/test_new_red_cov.gd", entry
    counts, marked = _mutation_and_marked(proj, env)
    assert counts == base, (base, counts)
    assert marked == "3/3", marked


def test_r6_removal_updates_table(r6_project):
    """删除新增测试 → 续新一次成功，表里不再有它。"""
    proj, env, _base, _godot = r6_project
    (proj / "tests" / "test_new_red_nocov.gd").unlink(missing_ok=True)
    (proj / "tests" / "test_new_red_cov.gd").unlink(missing_ok=True)
    _renew(proj, env)
    assert _verdict_entry(proj, "test_new_red_nocov") is None
    assert _verdict_entry(proj, "test_new_red_cov") is None


def test_r7_case_g_passing_test_green_in_table(r6_project):
    """C3-R7 退回1：新增会通过、触达产品代码的测试——判定表覆盖行事实
    宇宙（§SPEC-025）：arms.gut_full.test_to_lines 里它的身份都要在
    test_verdicts 里且为 green（C3-R6 修法曾漏记，产物 4 条对宇宙 5 条）。"""
    proj, env, _base, _godot = r6_project
    (proj / "tests" / "test_new_green_cov.gd").write_text(
        'extends GutTest\nconst C = preload("res://src/calc_sign.gd")\n'
        "func test_gate_true():\n\tassert_eq(C.new().gate(11), true)\n"
        "func test_sign_pos():\n\tassert_eq(C.new().sign(9), 1)\n")
    _renew(proj, env)
    doc = json.loads(
        (proj / ".qa-cache" / "attribution-map.json").read_text(
            encoding="utf-8"))
    tv = doc.get("test_verdicts") or {}
    universe = ((doc.get("arms") or {}).get("gut_full")
                or {}).get("test_to_lines") or {}
    idents = [k for k in universe
              if k.split("::", 1)[0] == "test_new_green_cov.gd"]
    assert idents, "触达产品代码的新通过测试必须有归属身份（行事实宇宙）"
    missing = {k: tv.get(k) for k in idents
               if k not in tv or tv[k].get("verdict") != "green"}
    assert not missing, ("判定表未覆盖宇宙身份", idents, missing)
