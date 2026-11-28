"""C3-R13 退回1/2：map 源下的入口行为。

退回1：续新失败四类（建图 / 全量重建 / 增量 / map 不可读）在四入口
（默认行、fn、full、--tests）都回到具名 JSON、零 traceback。
退回2：改 csv 后的入口测试在 map 源下真跑（不靠 conftest 的 cold 形态），
断言落在结果上（JSON ok、mutant 数 > 0、stderr 无 production content
drift），run 期 refresh 调用数为 0；只设 GQT_GODOT_BIN 时也执行。
"""

import json
import os
import shutil
import subprocess
import sys

from pathlib import Path

import pytest

_GUT = os.environ.get("GQT_FORMS_GUT_DIR", "")


def _real_godot() -> str | None:
    """C3-R7 退回2 同款：GQT_GODOT_BIN 优先，PATH 兜底。"""
    env_bin = os.environ.get("GQT_GODOT_BIN", "")
    if env_bin and Path(env_bin).is_file():
        return env_bin
    return shutil.which("godot")


def _find_gut() -> str | None:
    if _GUT and (Path(_GUT) / "gut_cmdln.gd").is_file():
        return _GUT
    return None


# ---------------------------------------------------------------- 退回1 组
_MAP_SOURCE_ENV = {
    "GQT_ATTRIBUTION_MAP_MODE": "auto",
    "GQT_MUTATION_BASELINE_SOURCE": "map",
}


def _bare_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "project.godot").write_text("config_version=5\n")
    (proj / "src" / "calc.gd").write_text(
        "extends RefCounted\nfunc f(a):\n\treturn a\n")
    return proj


def _run_entry(proj: Path, extra: list, monkeypatch) -> tuple:
    """四入口共用：map 源下跑 cli.main，返回 (rc, stdout, stderr)。"""
    import io
    from contextlib import redirect_stdout, redirect_stderr

    import godot_qa_toolkit.cli as cli_mod

    for k, v in _MAP_SOURCE_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(cli_mod, "_bootstrap_import_or_contract",
                        lambda *a, **k: None)
    argv = ["mutation", str(proj / "src" / "calc.gd"),
            "--project-root", str(proj), *extra]
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_mod.main(argv)
    return rc, out.getvalue(), err.getvalue()


def _assert_named_json(rc, out, err, entry: str) -> None:
    assert rc == 1, f"{entry}: 续新失败应以 1 退出，实际 {rc}"
    assert "Traceback" not in err, f"{entry}: stderr 出现 traceback\n{err}"
    payload = json.loads(out)          # 必须是单个可解析 JSON
    assert payload.get("tool") == "mutation", entry
    assert payload.get("ok") is False, entry
    summary = payload.get("summary") or {}
    assert summary.get("error") or payload.get("failures"), entry


ENTRIES = [
    ("default", []),
    ("fn", ["--select", "fn"]),
    ("full", ["--select", "full"]),
    ("tests", ["--tests", "res://tests/a"]),
]


@pytest.mark.parametrize("entry,extra", ENTRIES)
@pytest.mark.parametrize("exc_name", [
    "baseline_build_failed",     # 建图失败（_prepare_build_missing）
    "baseline_full_failed",      # 全量重建失败（_prepare_full_rebuild）
    "baseline_increment_failed",  # 增量失败（_prepare_increment_renewal）
    "map_unreadable",            # map 不可读（refresh 内 load_map）
])
def test_renewal_failure_named_json_all_entries(
        entry, extra, exc_name, tmp_path, monkeypatch):
    """续新失败四类 × 四入口：具名 JSON、零 traceback、退出码 1。"""
    proj = _bare_project(tmp_path)
    import godot_qa_toolkit.cli as cli_mod
    import godot_qa_toolkit.mutation.map_contract as mc
    from godot_qa_toolkit.mutation.verdict_source import BaselineSourceError

    def failing_refresh(*_a, **_k):
        if exc_name == "map_unreadable":
            raise mc.MapContractError(
                "refresh failed (map unreadable): bad json")
        raise BaselineSourceError(
            f"prepare renewal failed ({exc_name}): injected")

    monkeypatch.setattr(mc, "refresh", failing_refresh)
    rc, out, err = _run_entry(proj, extra, monkeypatch)
    _assert_named_json(rc, out, err, entry)


# ---------------------------------------------------------------- 退回2 组
@pytest.fixture(scope="module")
def map_source_project(tmp_path_factory):
    """真 Godot 小项目：map 源下改 csv → 第一条 mutation 一次成功。"""
    godot = _real_godot()
    gut = _find_gut()
    if godot is None or gut is None:
        pytest.skip("real godot / GUT addon unavailable")
    home = tmp_path_factory.mktemp("r13home")
    proj = tmp_path_factory.mktemp("r13proj") / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "data").mkdir()
    (proj / "tests" / "a").mkdir(parents=True)
    (proj / "tests" / "fill").mkdir(parents=True)
    (proj / "project.godot").write_text(
        'config_version=5\n\n[application]\n\nconfig/name="r13"\n'
        'config/features=PackedStringArray("4.6")\n')
    (proj / "src" / "calc_sign.gd").write_text(
        "extends RefCounted\nfunc sign(x: int) -> int:\n\tif x > 0:\n"
        "\t\treturn 1\n\tif x < 0:\n\t\treturn -1\n\treturn 0\n\n"
        "func gate(v: int) -> bool:\n\tif v >= 10:\n\t\treturn true\n"
        "\treturn false\n")
    (proj / "data" / "table.csv").write_text("k,v\n")
    # KOL 形态：csv_translation 导入器 → Godot 生成 .translation 侧车，
    # 侧车纳入 map 内容指纹（R10 退回2 的漂移来源）。
    (proj / "data" / "i18n.csv").write_text("KEY,text\nHELLO,Hello\n")
    (proj / "data" / "i18n.csv.import").write_text(
        '[remap]\n\nimporter="csv_translation"\ntype="Translation"\n'
        '\n[deps]\n\nfiles=[]\n\ndest_files=["res://data/i18n.en.translation"]\n'
        '\n[params]\n\ncompress=true\ndelimiter=0\n')
    (proj / ".gqt-scope.json").write_text(
        '{"production": {"include": [{"kind": "dir", "value": "src"},'
        ' {"kind": "dir", "value": "data"}]}}')
    (proj / ".gutconfig.json").write_text(
        '{"dirs": ["res://tests/a", "res://tests/fill"],'
        ' "include_subdirs": false, "prefix": "test_"}')
    (proj / "tests" / "a" / "test_green.gd").write_text(
        'extends GutTest\nconst C = preload("res://src/calc_sign.gd")\n'
        "func test_pos():\n\tassert_eq(C.new().sign(5), 1)\n"
        "func test_neg():\n\tassert_eq(C.new().sign(-5), -1)\n"
        "func test_zero():\n\tassert_eq(C.new().sign(0), 0)\n")
    for i in range(3):
        (proj / "tests" / "fill" / f"test_fill_{i:02d}.gd").write_text(
            "extends GutTest\nfunc test_fill():\n\tassert_eq(1, 1)\n")
    shutil.copytree(gut, proj / "addons" / "gut")
    from attribution_fixtures import install_recipe
    from gherkin_fixtures import ensure_addon_metadata

    install_recipe(proj)
    ensure_addon_metadata(proj)
    env = dict(os.environ)
    env.update({"HOME": str(home), "TMPDIR": str(home),
                "PYTHONPATH": "src", "GQT_GODOT_BIN": godot})
    subprocess.run([godot, "--headless", "--import", "--path", str(proj)],
                   capture_output=True, timeout=600, env=env)
    return proj, godot


def _run_map_source_mutation(proj: Path, extra: list, monkeypatch) -> tuple:
    """map 源下 in-process 跑 mutation；返回 (rc, out, err, refresh_count)。"""
    import io
    from contextlib import redirect_stdout, redirect_stderr

    import godot_qa_toolkit.cli as cli_mod
    import godot_qa_toolkit.mutation.map_contract as mc

    for k, v in _MAP_SOURCE_ENV.items():
        monkeypatch.setenv(k, v)
    calls: list = []
    orig = mc.refresh

    def counting(*a, **k):
        calls.append(1)
        return orig(*a, **k)

    monkeypatch.setattr(mc, "refresh", counting)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_mod.main(
            ["mutation", "src/calc_sign.gd", "--mutate-all", "--budget",
             "60", "--timeout", "120", "--project-root", str(proj), *extra])
    return rc, out.getvalue(), err.getvalue(), len(calls)


def _assert_first_mutation_ok(rc, out, err, refresh_count, entry):
    # 结果判据（C3-R13）：rc 1 在有 survived 的合法结果里也成立，故不
    # 单靠 rc；error 为空 + mutant 数 > 0 + 无 run 期漂移 + 零 run 期续新。
    assert rc in (0, 1), f"{entry}: unexpected rc {rc}"
    payload = json.loads(out)
    summary = payload.get("summary") or {}
    assert not summary.get("error"), f"{entry}: {summary.get('error')}"
    assert summary.get("mutants", 0) > 0, f"{entry}: {summary}"
    assert "production content drift" not in err, f"{entry}: run 期漂移\n{err}"
    # prepare 续新恰一次；run 期零续新
    assert refresh_count == 1, f"{entry}: refresh={refresh_count}"


def test_full_entry_map_source_first_mutation_after_csv_change(
        map_source_project, monkeypatch):
    """--select full（10 mutant > jobs 上限 8，并行信封）：改 csv 后第一条
    一次成功、run 期零续新。"""
    proj, _godot = map_source_project
    (proj / "data" / "table.csv").write_text("k,v,changed\n")
    (proj / "data" / "i18n.csv").write_text("KEY,text\nHELLO,Hi\n")
    rc, out, err, n = _run_map_source_mutation(
        proj, ["--select", "full", "--auto-prepare", "--jobs", "4"],
        monkeypatch)
    _assert_first_mutation_ok(rc, out, err, n, "full")


def test_tests_entry_map_source_first_mutation_after_csv_change(
        map_source_project, monkeypatch):
    """--tests res://tests/fill/（mutants ≤ 上限，串行派发）：改 csv 后
    第一条一次成功、run 期零续新。"""
    proj, _godot = map_source_project
    (proj / "data" / "table.csv").write_text("k,v,changed2\n")
    (proj / "data" / "i18n.csv").write_text("KEY,text\nHELLO,Hey\n")
    rc, out, err, n = _run_map_source_mutation(
        proj, ["--tests", "res://tests/fill/", "--auto-prepare",
               "--jobs", "16"],
        monkeypatch)
    _assert_first_mutation_ok(rc, out, err, n, "tests")


# ---------------------------------------------------------------- 钉子组
def test_cold_route_no_gut_run_before_first_mutant(monkeypatch):
    """Owner 定案 6 钉子：cold 车道目标、map 里有红测时，第一个 mutant
    之前不启动任何 GUT 运行。"""
    from types import SimpleNamespace

    import godot_qa_toolkit.mutation.runner as runner_mod

    gut_runs: list = []

    def fake_run_gut_on_project(*_a, **_k):
        gut_runs.append(1)
        return 0, ""

    def fake_single_mutant(_m, *_a, **_k):
        return {"verdict": "survived", "reason": "stub"}

    monkeypatch.setattr(runner_mod, "_run_gut_on_project",
                        fake_run_gut_on_project)
    monkeypatch.setattr(runner_mod, "_run_single_mutant",
                        fake_single_mutant)
    slot = SimpleNamespace(root=str(tmp_dir()))

    class _StubReporter:
        def record(self, *_a, **_k):
            pass

    reporter = _StubReporter()
    red_pairs = [("res://tests/b/test_red_always.gd", "test_red_always")]
    m = SimpleNamespace(stable_id="m1")
    raw = runner_mod._run_cold_legs(
        "src/calc_sign.gd", "/tmp/fake-proj", "extends Node\n", None,
        [m], None, {"test_red_always"}, True, slot, "auto",
        "src/calc_sign.gd", "res://src/calc_sign.gd",
        lambda: None, reporter, None, {}, {},
        direct_scripts=["res://tests/b/test_red_always.gd"],
        red_pairs=red_pairs)
    assert gut_runs == []
    assert len(raw) == 1


def tmp_dir() -> str:
    import tempfile

    return tempfile.mkdtemp()
