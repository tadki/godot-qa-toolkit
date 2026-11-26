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


def _real_godot_bin() -> "str | None":
    """C3-R7 退回2：GQT_GODOT_BIN 优先、其次 PATH；conftest 真空 stub
    （--version 输出带 official.stub 标记）不算真引擎——真空形态照旧
    skip，标准隔离环境（外部只设 GQT_GODOT_BIN）真跑。"""
    cand = os.environ.get("GQT_GODOT_BIN") or shutil.which("godot")
    if not cand or not Path(cand).is_file():
        return None
    try:
        r = subprocess.run([str(cand), "--version"], capture_output=True,
                           text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if "stub" in (r.stdout or ""):
        return None
    return str(cand)


_MODULE_ENV_KEYS = {
    "GQT_MEASUREMENT_GHERKIN_DISABLED": "1",
    "GQT_MUTATION_BASELINE_SOURCE": "map",
    "GQT_ATTRIBUTION_MAP_MODE": "auto",
    # 本模块契约 = 项目默认路径 auto map；子进程只带显式声明的键、
    # 不继承进程内 GQT_*（C3-R9 退回4：产品侧写点已改为操作结束恢复，
    # 此置空保留为默认路径语义的显式哨兵，不再针对产品泄漏）。
    "GQT_ATTRIBUTION_MAP": "",
}


def _child_env(base: "dict[str, str]", home: Path,
               godot_bin: "str | None" = None) -> dict:
    """C3-R8 退回3：子进程 env 只带本模块需要的 GQT_* 键。进程内产品
    调用会把 GQT_* 开关写进 os.environ 不恢复（同批先跑的测试泄漏，如
    cli._run_mutation_serial 的 FAIL_ON_RED），继承会让本模块结果依赖
    批内顺序。godot 二进制由调用方以已解析路径显式注入（标准隔离环境
    PATH 无 godot，需 GQT_GODOT_BIN 指向真引擎）。"""
    env = {k: v for k, v in base.items() if not k.startswith("GQT_")}
    env.update({"HOME": str(home), "XDG_DATA_HOME": str(home / ".xdg"),
                "TMPDIR": str(home), "PYTHONPATH": "src"})
    env.update(_MODULE_ENV_KEYS)
    if godot_bin:
        env["GQT_GODOT_BIN"] = str(godot_bin)
    return env


def test_r8_child_env_isolated_from_leaked_switches():
    """C3-R8 退回3：泄漏进进程的 GQT_* 开关不得进入本模块子进程 env
    （只保留本模块自己声明的键）。"""
    base = {**os.environ,
            "GQT_MEASUREMENT_FAIL_ON_RED_BASELINE": "1",
            "GQT_ATTRIBUTION_MAP": "/leaked/map.json",
            "GQT_MUTATION_CHECK_BASELINE": "1",
            "PATH": "/usr/bin:/bin"}
    env = _child_env(base, Path("/tmp/x"))
    assert "GQT_MEASUREMENT_FAIL_ON_RED_BASELINE" not in env
    assert "GQT_MUTATION_CHECK_BASELINE" not in env
    assert env["GQT_ATTRIBUTION_MAP"] == ""
    assert env["GQT_MUTATION_BASELINE_SOURCE"] == "map"
    assert env["PATH"] == "/usr/bin:/bin"
    assert "GQT_GODOT_BIN" not in env  # 不在 base 里泄漏继承
    # 已解析的真引擎由调用方显式注入（标准隔离环境 PATH 无 godot）
    env2 = _child_env(base, Path("/tmp/x"), "/real/godot")
    assert env2["GQT_GODOT_BIN"] == "/real/godot"


@pytest.fixture(scope="module")
def r6_project(tmp_path_factory):
    """Leader 式小夹具：目录型白名单（src/ + data/），单测试目录 =
    绿测脚本 + 恒红脚本 + 填充脚本。返回 (proj, env, baseline_counts)。"""
    # 标准隔离环境只设 GQT_GODOT_BIN、PATH 上没有 godot（C3-R7 退回2：
    # GQT_GODOT_BIN 优先；真空 stub 不算真引擎）
    godot = _real_godot_bin()
    gut = _find_gut()
    if godot is None or gut is None:
        pytest.skip("real godot / GUT addon unavailable")
    _here = str(Path(__file__).parent)
    if _here not in sys.path:
        sys.path.insert(0, _here)
    home = tmp_path_factory.mktemp("r6home")
    env = _child_env(os.environ, home, godot)
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


def _reset_increment_counter(proj: Path) -> None:
    """删除类语义测试对周期性全量校准（since_full>=calibration_every，
    默认 5）确定化——本模块夹具被前面用例多次续新，计数器可能已到期。"""
    p = proj / ".qa-cache" / "attribution-map.json"
    doc = json.loads(p.read_text(encoding="utf-8"))
    inc = doc.get("increment")
    if isinstance(inc, dict):
        inc["increments_since_full"] = 0
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def test_r8_delete_plus_add_same_batch_renews(r6_project):
    """C3-R8 退回1：同一批里删除一个测试文件、又新增一个测试文件 → 一次
    续新成功（此前删除只进 drift 不进变更面，装填后复检仍过期、永不自愈）；
    被删身份出表、新增通过身份以 green 进表、动作为增量。"""
    proj, env, _base, _godot = r6_project
    _reset_increment_counter(proj)
    (proj / "tests" / "test_fill_00.gd").unlink()
    (proj / "tests" / "test_r8_new_pass.gd").write_text(
        'extends GutTest\nconst C = preload("res://src/calc_sign.gd")\n'
        "func test_pass():\n\tassert_eq(C.new().gate(11), true)\n")
    _renew(proj, env)  # 断言 exit 0——删除+新增同批一次续新成功
    doc = json.loads(
        (proj / ".qa-cache" / "attribution-map.json").read_text(
            encoding="utf-8"))
    tv = doc.get("test_verdicts") or {}
    assert (doc.get("increment") or {}).get("mode") == "incremental"
    assert "test_fill_00.gd::test_fill" not in tv
    assert tv.get("test_r8_new_pass.gd::test_pass") == {"verdict": "green"}


def test_r8_delete_only_incremental_no_full_rebuild(r6_project):
    """C3-R8 退回1：只删除一个测试文件 → 续新走增量（因素表第 6 行：
    剔除边与 verdict、不重跑），不做全量重建；被删身份不在表里。"""
    proj, env, _base, _godot = r6_project
    _reset_increment_counter(proj)
    (proj / "tests" / "test_fill_01.gd").unlink()
    _renew(proj, env)
    doc = json.loads(
        (proj / ".qa-cache" / "attribution-map.json").read_text(
            encoding="utf-8"))
    # 全量重建产物无 increment 块——据此判定未走全量
    assert (doc.get("increment") or {}).get("mode") == "incremental"
    tv = doc.get("test_verdicts") or {}
    assert "test_fill_01.gd::test_fill" not in tv


def test_r8_case_d_mixed_script_inc_vs_full_consistent(r6_project):
    """C3-R8 退回2：新增脚本里通过与失败并存 → 逐测试落表（pass=green、
    fail=red）；带 --mutate-all 的 mutation 在增量 map 与全量重建 map 上
    判定一致（此前增量把通过的也记红，mutation 假存活）。"""
    proj, env, _base, _godot = r6_project
    (proj / "tests" / "test_r8_mixed.gd").write_text(
        'extends GutTest\nconst C = preload("res://src/calc_sign.gd")\n'
        "func test_mixed_pass():\n\tassert_eq(C.new().gate(11), true)\n"
        "func test_mixed_fail():\n\tfail_test(\"mixed fail\")\n")
    _renew(proj, env)
    doc = json.loads(
        (proj / ".qa-cache" / "attribution-map.json").read_text(
            encoding="utf-8"))
    tv = doc.get("test_verdicts") or {}
    assert tv.get("test_r8_mixed.gd::test_mixed_pass") == {"verdict": "green"}
    fail_entry = tv.get("test_r8_mixed.gd::test_mixed_fail") or {}
    assert fail_entry.get("verdict") == "red", fail_entry

    def _run_counts() -> dict:
        rc, out, err = _cli(
            ["mutation", "src/calc_sign.gd", "--select", "full",
             "--auto-prepare", "--mutate-all", "--budget", "60",
             "--timeout", "120"], env, proj)
        assert rc in (0, 1), err[-400:]
        return _counts(out)

    inc_counts = _run_counts()
    # 同一棵树全量重建 map 后重跑同一 mutation——判定必须一致
    (proj / ".qa-cache" / "attribution-map.json").unlink()
    full_counts = _run_counts()
    assert inc_counts == full_counts, (inc_counts, full_counts)
    assert inc_counts["killed"] and inc_counts["survived"], inc_counts


def test_r9_delete_csv_content_renews(r6_project):
    """C3-R9 退回1：删除非脚本生产内容（csv）→ 一次续新成功、指纹剔除
    （此前删除只进 drift 不进变更面，续新永远失败）。"""
    proj, env, _base, _godot = r6_project
    (proj / "data" / "table.csv").unlink()
    _renew(proj, env)  # 断言 exit 0——一次续新成功
    doc = json.loads(
        (proj / ".qa-cache" / "attribution-map.json").read_text(
            encoding="utf-8"))
    assert "data/table.csv" not in (doc.get("production_content_sha256")
                                    or {})


def test_r9_case_e_default_line_no_parity_invalidations(r6_project):
    """C3-R9 退回2：默认行选面（闭包含既有红测）→ 照常出结果，无
    baseline_map_mismatch 整批作废；判定与 --select full 同一棵树一致
    （此前 worker parity 门拿物理跳过后的失败集比全量红集必然失配）。"""
    proj, env, _base, _godot = r6_project

    def _run(extra: list) -> tuple:
        rc, out, err = _cli(
            ["mutation", "src/calc_sign.gd", "--mutate-all", *extra,
             "--budget", "60", "--timeout", "120"], env, proj)
        assert rc in (0, 1), (rc, err[-300:])
        assert "baseline_map_mismatch" not in err, err[-300:]
        return _counts(out), err

    line_counts, line_err = _run([])
    full_counts, full_err = _run(["--select", "full", "--auto-prepare"])
    # 行选面对零可执行引用的 mutant 做终态短路（⊆ 全量面）；可杀判定
    # 与全量一致；无整批作废
    assert line_counts["mutants"] <= full_counts["mutants"], \
        (line_counts, full_counts)
    assert line_counts["killed"] == full_counts["killed"], \
        (line_counts, full_counts)
    assert line_counts["killed"] > 0, line_counts
    assert "baseline_map_mismatch" not in line_err
