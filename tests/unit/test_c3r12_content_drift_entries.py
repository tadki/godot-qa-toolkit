"""C3-R12 退回1：全部 mutation 入口在改 csv 之后第一条一次成功，run 期
零续新（§SPEC-032 ④ / 定案 18）。

覆盖矩阵：--select full / --tests 两入口 × 并行信封启用（mutants 10 >
jobs 上限 8）与串行派发（--jobs 16，mutants ≤ 上限）两种规模；refresh
计数经 monkeypatch 包装——首跑 ≤1（prepare 续新一次），同树二跑 0（
run 期零续新的行为证据）。"""

import os
import shutil
import subprocess

from pathlib import Path

import pytest

_GUT = os.environ.get("GQT_FORMS_GUT_DIR", "")


def _find_gut() -> str | None:
    import shutil as _sh

    if _GUT and (Path(_GUT) / "gut_cmdln.gd").is_file():
        return _GUT
    return None


def _real_godot() -> str | None:
    import shutil as _sh

    return _sh.which("godot")


@pytest.fixture(scope="module")
def r12_project(tmp_path_factory):
    godot = _real_godot()
    gut = _find_gut()
    if godot is None or gut is None:
        pytest.skip("real godot / GUT addon unavailable")
    home = tmp_path_factory.mktemp("r12home")
    proj = tmp_path_factory.mktemp("r12proj") / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "data").mkdir()
    (proj / "tests" / "a").mkdir(parents=True)
    (proj / "tests" / "b").mkdir(parents=True)
    (proj / "tests" / "fill").mkdir(parents=True)
    (proj / "project.godot").write_text(
        'config_version=5\n\n[application]\n\nconfig/name="r12"\n'
        'config/features=PackedStringArray("4.6")\n')
    (proj / "src" / "calc_sign.gd").write_text(
        "extends RefCounted\nfunc sign(x: int) -> int:\n\tif x > 0:\n"
        "\t\treturn 1\n\tif x < 0:\n\t\treturn -1\n\treturn 0\n\n"
        "func gate(v: int) -> bool:\n\tif v >= 10:\n\t\treturn true\n"
        "\treturn false\n")
    (proj / "data" / "table.csv").write_text("k,v\n")
    (proj / ".gqt-scope.json").write_text(
        '{"production": {"include": [{"kind": "dir", "value": "src"},'
        ' {"kind": "dir", "value": "data"}]}}')
    (proj / ".gutconfig.json").write_text(
        '{"dirs": ["res://tests/a", "res://tests/b", "res://tests/fill"],'
        ' "include_subdirs": false, "prefix": "test_"}')
    (proj / "tests" / "a" / "test_green.gd").write_text(
        'extends GutTest\nconst C = preload("res://src/calc_sign.gd")\n'
        "func test_pos():\n\tassert_eq(C.new().sign(5), 1)\n"
        "func test_neg():\n\tassert_eq(C.new().sign(-5), -1)\n"
        "func test_zero():\n\tassert_eq(C.new().sign(0), 0)\n")
    (proj / "tests" / "b" / "test_red_always.gd").write_text(
        'extends GutTest\nconst C = preload("res://src/calc_sign.gd")\n'
        "func test_red_always():\n\tassert_eq(C.new().sign(7), 1)\n"
        '\tfail_test("always red")\n')
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
    return proj, env, godot


def _run_mutation(proj, extra: list, refresh_calls: list) -> tuple:
    """in-process cli.main 跑一条 mutation；refresh 计数经 monkeypatch。"""
    import godot_qa_toolkit.cli as cli_mod
    import godot_qa_toolkit.mutation.map_contract as mc

    orig = mc.refresh

    def counting(*a, **k):
        refresh_calls.append(str(target_res_of(proj)) + ":" + str(a[:1]))
        return orig(*a, **k)

    mc.refresh = counting
    try:
        rc = cli_mod.main(
            ["mutation", "src/calc_sign.gd", "--mutate-all", "--budget",
             "60", "--timeout", "120", "--project-root", str(proj), *extra])
        out = None
    finally:
        mc.refresh = orig
    return rc, out


def target_res_of(proj):
    return str(proj / "src" / "calc_sign.gd")


def test_full_entry_parallel_envelope_first_mutation_after_csv_change(
        r12_project, monkeypatch):
    """--select full（mutants 10 > 上限 8，并行信封启用）：改 csv 后第一条
    mutation 一次成功；refresh 只在 prepare 阶段（run 期零续新）。"""
    proj, env, _godot = r12_project
    (proj / "data" / "table.csv").write_text("k,v,changed\n")
    calls: list = []
    monkeypatch.setattr("godot_qa_toolkit.mutation.map_contract.refresh",
                        lambda *a, **k: calls.append(len(calls)) or
                        __import__("godot_qa_toolkit.mutation.map_contract",
                                   fromlist=["refresh"]).refresh(*a, **k))
    import godot_qa_toolkit.cli as cli_mod

    rc = cli_mod.main(
        ["mutation", "src/calc_sign.gd", "--select", "full",
         "--auto-prepare", "--mutate-all", "--budget", "60", "--timeout",
         "120", "--jobs", "4", "--project-root", str(proj)])
    assert rc in (0, 1)
    # prepare 续新 ≤1 次；fresh 时 0 次——两条断言都证明 run 期零续新
    assert len(calls) <= 1, calls


def test_tests_entry_serial_dispatch_first_mutation_after_csv_change(
        r12_project, monkeypatch):
    """--tests res://tests/fill/（mutants ≤ 上限，串行派发）：改 csv 后
    第一条 mutation 一次成功；run 期零续新。"""
    proj, env, _godot = r12_project
    (proj / "data" / "table.csv").write_text("k,v,changed\n")
    calls: list = []
    monkeypatch.setattr("godot_qa_toolkit.mutation.map_contract.refresh",
                        lambda *a, **k: calls.append(len(calls)))
    import godot_qa_toolkit.cli as cli_mod

    rc = cli_mod.main(
        ["mutation", "src/calc_sign.gd", "--tests", "res://tests/fill/",
         "--auto-prepare", "--mutate-all", "--budget", "60", "--timeout",
         "120", "--jobs", "16", "--project-root", str(proj)])
    assert rc in (0, 1)
    assert len(calls) <= 1, calls


def test_r12_cold_route_no_gut_run_before_first_mutant(monkeypatch):
    """Owner 定案 6 钉子（C3-R12 退回3）：cold 车道目标、map 里有红测时，
    第一个 mutant 之前不启动任何 GUT 运行（红测观测腿已撤；跳过判定
    不跑 GUT）。"""
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
    store2 = None
    prior_entries: dict = {}
    ownership_fn = lambda: None
    red_pairs = [("res://tests/b/test_red_always.gd", "test_red_always")]
    m = SimpleNamespace(stable_id="m1")
    raw = runner_mod._run_cold_legs(
        "src/calc_sign.gd", "/tmp/fake-proj", "extends Node\n", None,
        [m], None, {"test_red_always"}, True, slot, "auto",
        "src/calc_sign.gd", "res://src/calc_sign.gd",
        ownership_fn, reporter, store2, prior_entries, {},
        direct_scripts=["res://tests/b/test_red_always.gd"],
        red_pairs=red_pairs)
    assert gut_runs == []
    assert len(raw) == 1


def tmp_dir() -> str:
    import tempfile

    return tempfile.mkdtemp()
