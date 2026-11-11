"""SEE-1546 WP3 PR-C — gqt canonical 双源批式编排单测（§SPEC-037）。

判据先行（TDD RED）：批式臂（gut_full 一次 + acceptance 私有目录一次）、
逐文件 sink `qa-coverage-hits-{uuid}-{file_key8}`、在树插桩备份/恢复零残留、
coverage-union/1 逐文件 union、canonical-report/1 形状（arms=2、run_uuid
不同、union.valid、not_certified、exploratory、performance.acceptance_probe）、
evidence recipe.arm 显式注入（缺省推断保留向后兼容）、失败臂 → union invalid。
"""

from __future__ import annotations

import json
import re
import subprocess as sp
from pathlib import Path

import pytest

from godot_qa_toolkit.canonical.orchestrator import (
    CanonicalError,
    REPORT_VERSION,
    batch_sink_name,
    file_key8,
    run_canonical,
)

TARGET_SRC = """extends RefCounted


func work(n: int) -> int:
\tvar acc = 0
\tfor i in range(n):
\t\tacc += i
\treturn acc


func probe_only() -> int:
\treturn 7
"""

TARGET_SRC_2 = """extends Node


func ready_hook() -> void:
\tset_process(false)
"""


@pytest.fixture(autouse=True)
def _restore_subprocess():
    orig_run = sp.run
    yield
    sp.run = orig_run


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "addons" / "gut").mkdir(parents=True)
    (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
    (root / "project.godot").write_text(
        'config_version=5\n[application]\nconfig/name="canon-fixture"\n')
    (root / ".gutconfig.json").write_text(json.dumps(
        {"dirs": ["res://tests/"], "include_subdirs": True,
         "prefix": "test_"}))
    (root / "tests" / "unit").mkdir(parents=True)
    (root / "tests" / "unit" / "test_game.gd").write_text(
        "extends GutTest\nfunc test_ok() -> void:\n\tassert_true(true)\n")
    feats = root / "acceptance" / "features"
    feats.mkdir(parents=True)
    (feats / "demo.feature").write_text(
        "Feature: F\n  Scenario Outline: s\n    Then hp is <left>\n"
        "    # types: <hp>:int, <left>:numeric\n    Examples:\n"
        "      | hp | left |\n      | 10 | 7    |\n")
    game = root / "src_game"
    game.mkdir()
    (game / "cell.gd").write_text(TARGET_SRC)
    (game / "node.gd").write_text(TARGET_SRC_2)
    return root


class FakeGodot:
    """记录调用参数；按臂写命中 sink：config 面（无 -gdir）全量 ids，
    -gdir（acceptance 私有目录臂）写前半 ids——union=|∪C| 端到端可断言。"""

    def __init__(self, project_root: Path, mode: str = "half_on_gdir"):
        self.calls: list[list[str]] = []
        self.root = project_root
        self.returncode = 0
        self.stderr_text = ""
        self.mode = mode

    def __call__(self, *a, **k):
        argv = a[0]
        self.calls.append(list(argv))
        cmd = " ".join(argv)
        # 非测试执行类子进程（如 compile 探针等）直接放行成功
        if "gut_cmdln.gd" not in cmd:
            class R0:
                returncode = 0
                stdout = ""
                stderr = ""
            return R0()
        on_gdir = any(str(x).startswith("-gdir=") for x in argv)
        for gd in self.root.glob("src_game/*.gd"):
            src = gd.read_text(encoding="utf-8")
            names = set(re.findall(r'qa-coverage-hits-[0-9a-f-]+-[0-9a-f]{8}\.txt', src))
            for sink in names:
                count = len(re.findall(r"static func __qa_cov_probe_\d+\(\):", src))
                ids = list(range(count))
                if on_gdir and self.mode == "half_on_gdir":
                    ids = ids[: max(1, count // 2)]
                (self.root / sink).write_text(
                    "".join(f"{i}\n" for i in ids), encoding="utf-8")
        class R:
            def __init__(self, rc, err):
                self.returncode = rc
                self.stdout = ""
                self.stderr = err
        return R(self.returncode, self.stderr_text)


def _recipe(project: Path, tmp_path: Path) -> tuple[dict, Path]:
    from godot_qa_toolkit.canonical.recipe import build_recipe
    recipe, error = build_recipe(project)
    assert error is None, error
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(recipe), encoding="utf-8")
    return recipe, path


def _targets(project: Path) -> list[str]:
    return sorted(str(p) for p in (project / "src_game").glob("*.gd"))


def test_file_key8_and_batch_sink_name():
    rel = "src_game/cell.gd"
    k = file_key8(rel)
    assert re.fullmatch(r"[0-9a-f]{8}", k)
    name = batch_sink_name("uuid-1", rel)
    assert name == f"qa-coverage-hits-uuid-1-{k}.txt"


def test_run_canonical_happy_shape(project: Path, tmp_path: Path):
    recipe, rpath = _recipe(project, tmp_path)
    fake = FakeGodot(project)
    sp.run = fake
    report = run_canonical(project, _targets(project), recipe)
    assert report["report_version"] == REPORT_VERSION
    assert len(report["arms"]) == 2
    arms = {a["arm"] for a in report["arms"]}
    assert arms == {"gut_full", "acceptance"}
    uuids = {a["run_uuid"] for a in report["arms"]}
    assert len(uuids) == 2  # 两臂 run_uuid 必不同
    assert report["union"]["valid"] is True
    assert report["certification"]["status"] == "not_certified"
    assert report["evidence_level"] == "exploratory"
    assert report["diagnostic_only"] is True
    assert "acceptance_probe" in report["performance"]
    assert all(u["valid"] for u in report["unions"])
    assert report["unmeasured"] == []


def test_union_covers_union_not_average(project: Path, tmp_path: Path):
    recipe, _ = _recipe(project, tmp_path)
    fake = FakeGodot(project)  # acceptance 臂半量命中
    sp.run = fake
    report = run_canonical(project, _targets(project), recipe)
    by_file = {u["file"]: u for u in report["unions"]}
    cell = by_file["res://src_game/cell.gd"]
    a_cov = cell["arms"][0]["covered"]
    b_cov = cell["arms"][1]["covered"]
    assert cell["union"]["covered"] == 5  # ∪C 全量（cell.gd 共 5 个探针行）
    assert b_cov < 5  # acceptance 臂确为半量（平均会被此断言拆穿）
    assert cell["union"]["total"] == 5
    assert cell["comparable"] is True  # canonical 同配方


def test_tree_restored_and_no_residue(project: Path, tmp_path: Path):
    recipe, _ = _recipe(project, tmp_path)
    before = {p: p.read_bytes() for p in (project / "src_game").glob("*.gd")}
    fake = FakeGodot(project)
    sp.run = fake
    run_canonical(project, _targets(project), recipe)
    for p, data in before.items():
        assert p.read_bytes() == data  # 在树插桩恢复零残留
    assert not list(project.glob("qa-coverage-hits-*.txt"))  # sink 落盘即清
    import glob as g
    assert g.glob("/tmp/gqt-canonical-backups/*") == []  # 备份目录已清


def test_gut_arm_uses_config_face_acceptance_uses_private_dir(
        project: Path, tmp_path: Path):
    recipe, _ = _recipe(project, tmp_path)
    fake = FakeGodot(project)
    sp.run = fake
    run_canonical(project, _targets(project), recipe)
    gut_calls = [c for c in fake.calls if any(
        x == "-s" for x in c) and "gut_cmdln.gd" in " ".join(c)]
    assert len(gut_calls) == 2
    no_gdir = [c for c in gut_calls if not any(
        str(x).startswith("-gdir=") for x in c)]
    with_gdir = [c for c in gut_calls if any(
        str(x).startswith("-gdir=") for x in c)]
    assert len(no_gdir) == 1  # gut_full 臂走 gutconfig 面（无 -gdir）
    assert len(with_gdir) == 1  # acceptance 臂走私有目录
    assert ".gqt-run-" in " ".join(with_gdir[0])


def test_unparseable_target_isolated_not_fatal(project: Path, tmp_path: Path):
    (project / "src_game" / "broken.gd").write_text("func broken(:\n")
    recipe, _ = _recipe(project, tmp_path)
    fake = FakeGodot(project)
    sp.run = fake
    report = run_canonical(project, _targets(project), recipe)
    lanes = {u["file"]: u["reason"] for u in report["unmeasured"]}
    assert any("broken.gd" in f for f in lanes)
    assert len(report["unions"]) == 2  # 其余两文件照常测
    assert report["arms"][0]["bundles"] == 2


def test_invalid_recipe_raises(project: Path, tmp_path: Path):
    recipe, _ = _recipe(project, tmp_path)
    recipe["recipe_hash"] = "0" * 64
    sp.run = FakeGodot(project)
    with pytest.raises(CanonicalError, match="recipe"):
        run_canonical(project, _targets(project), recipe)


def test_gut_launch_failure_makes_arm_invalid(project: Path, tmp_path: Path):
    recipe, _ = _recipe(project, tmp_path)
    fake = FakeGodot(project)
    fake.returncode = 1
    fake.stderr_text = "ERROR: Cannot open project"
    sp.run = fake
    report = run_canonical(project, _targets(project), recipe)
    assert report["ok"] is False
    states = {a["arm"]: a["state"] for a in report["arms"]}
    assert "invalid" in states.values()
    assert report["union"]["valid"] is False


def test_gut_timeout_records_wall_and_sentinel(project, tmp_path, monkeypatch):
    """超时墙钟如实记录（首跑缺陷修复锚）：wall_s>0、timed_out 哨兵进
    bundle、invalid_reason=timeout——超时本身也是 §SPEC-039 门量证据。"""
    import time as _t

    from godot_qa_toolkit.canonical import orchestrator as orch
    recipe, _ = _recipe(project, tmp_path)
    sp.run = FakeGodot(project)

    def fake_gut(root, gut_args, timeout_s):
        _t.sleep(0.01)
        raise orch.CanonicalError(f"GUT timed out after {timeout_s}s")

    monkeypatch.setattr(orch, "_run_one_gut", fake_gut)
    report = run_canonical(project, _targets(project), recipe)
    for arm in report["arms"]:
        assert arm["timed_out"] is True
        assert arm["wall_s"] > 0
        assert arm["invalid_reason"] == "timeout"
    assert report["ok"] is False


def test_evidence_arm_injection_explicit_and_default():
    from godot_qa_toolkit.coverage.evidence import build_bundle, build_run_record
    b = build_bundle(
        run_uuid="u", file_path="res://x.gd", gd_source=TARGET_SRC,
        tests_glob=None, hits_lines=[], probes_fired=0, gut_ok=True,
        run_error=False, timeout=False, zero_tests=False, vacuous=False,
        arm="acceptance")
    assert b["recipe"]["arm"] == "acceptance"  # 显式注入
    b2 = build_bundle(
        run_uuid="u", file_path="res://x.gd", gd_source=TARGET_SRC,
        tests_glob="res://tests/unit/x.gd", hits_lines=[], probes_fired=0,
        gut_ok=True, run_error=False, timeout=False, zero_tests=False,
        vacuous=False)
    assert b2["recipe"]["arm"] == "gut_core"  # 缺省推断保留（向后兼容）
    b3 = build_bundle(
        run_uuid="u", file_path="res://x.gd", gd_source=TARGET_SRC,
        tests_glob=None, hits_lines=[], probes_fired=0, gut_ok=True,
        run_error=False, timeout=False, zero_tests=False, vacuous=False)
    assert b3["recipe"]["arm"] == "gut_full"
    rec = build_run_record(run_uuid="u", file_path="res://x.gd",
                           gd_source=TARGET_SRC, tests_glob=None,
                           probe_manifest={}, arm="gut_full")
    assert rec["recipe"]["arm"] == "gut_full"


def test_cli_canonical_end_to_end(project: Path, tmp_path: Path):
    import io
    from contextlib import redirect_stdout

    from godot_qa_toolkit.cli import build_parser
    recipe, rpath = _recipe(project, tmp_path)
    sp.run = FakeGodot(project)
    out_path = tmp_path / "canonical.json"
    parser = build_parser()
    args = parser.parse_args([
        "canonical", str(project / "src_game"),
        "--project-root", str(project),
        "--recipe", str(rpath), "--out", str(out_path),
    ])
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = args.func(args)
    assert code == 0
    doc = json.loads(out_path.read_text(encoding="utf-8"))
    assert doc["report_version"] == REPORT_VERSION
    assert len(doc["arms"]) == 2
    assert doc["certification"]["status"] == "not_certified"
    assert "acceptance_probe" in doc["performance"]
    assert doc["ok"] is True
