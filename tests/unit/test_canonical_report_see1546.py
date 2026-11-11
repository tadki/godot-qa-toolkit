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
        # §SPEC-040 注入审计：每次 GUT 调用瞬间的 project.godot 内容与
        # flusher autoload 文件在场性（注入窗口审计 + 恢复零残留对照）
        self.pg_at_gut: list[str] = []
        self.flusher_at_gut: list[bool] = []

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
        pg = self.root / "project.godot"
        self.pg_at_gut.append(pg.read_text(encoding="utf-8")
                              if pg.is_file() else "")
        self.flusher_at_gut.append(
            (self.root / "addons" / "gqt_cov_flusher.gd").is_file())
        for gd in self.root.glob("src_game/*.gd"):
            src = gd.read_text(encoding="utf-8")
            names = set(re.findall(r'qa-coverage-hits-[0-9a-f-]+-[0-9a-f]{8}\.txt', src))
            for sink in names:
                # 三种传输形态的探针点数：direct 逐 id 函数 / R3 store_string /
                # R4 内存 flag 标记
                probe_ids = set(re.findall(r"__qa_cov_probe_(\d+)\(\)", src)) | set(
                    re.findall(r"__qa_cov_probe_m\[(\d+)\] = 1", src))
                count = len(probe_ids)
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


def test_zero_executable_file_gets_vacuous_lane_not_no_probe(
        project: Path, tmp_path: Path):
    """§SPEC-042 (D2 RED)：零可执行行文件（纯 signal 声明）必须进显式
    vacuous 车道，不再误报 no_probe_fired——no_probe 只属于「有可执行面
    但探针未点火」的文件。"""
    (project / "src_game" / "decl.gd").write_text(
        "extends RefCounted\n\nsignal alpha\nsignal beta\n")
    recipe, _ = _recipe(project, tmp_path)
    sp.run = FakeGodot(project)
    report = run_canonical(project, _targets(project), recipe)
    vac = [u for u in report["unions"] if "decl.gd" in u["file"]]
    assert vac, "decl.gd 应仍参与 union 披露"
    assert vac[0]["valid"] is False  # §SPEC-036 严格 invalid 语义不变
    assert "vacuous" in (vac[0]["invalid_reason"] or "")
    assert "no_probe_fired" not in (vac[0]["invalid_reason"] or "")


def test_measurable_subset_and_unmeasurable_disclosure(
        project: Path, tmp_path: Path):
    """§SPEC-043 (D3)：报告必须给出 measurable 子集 union 有效性、
    unmeasurable 清单及原因；measurable + unmeasurable == 全集（禁止
    静默排除）。"""
    (project / "src_game" / "decl.gd").write_text(
        "extends RefCounted\n\nsignal alpha\nsignal beta\n")
    recipe, _ = _recipe(project, tmp_path)
    sp.run = FakeGodot(project)
    report = run_canonical(project, _targets(project), recipe)
    u = report["union"]
    assert u["measurable"]["all_valid"] is True
    assert u["measurable"]["files"] >= 2
    assert u["measurable"]["files"] + len(u["unmeasurable"]) == u["files"]
    reasons = {x["file"]: x["reason"] for x in u["unmeasurable"]}
    assert any("decl.gd" in f for f in reasons)
    assert all(r for r in reasons.values())  # 原因非空——禁止静默排除


def test_flag_mode_injects_finalizer_and_restores(project: Path,
                                                  tmp_path: Path):
    """§SPEC-046：flag 形态的统一落盘由注入的 finalizer autoload 在树退出
    时完成——GUT 调用窗口内 project.godot 含注册行且 finalizer 在场；run
    结束后两者恢复零残留。"""
    recipe, _ = _recipe(project, tmp_path)
    before_pg = (project / "project.godot").read_text(encoding="utf-8")
    fake = FakeGodot(project)
    sp.run = fake
    run_canonical(project, _targets(project), recipe)
    assert len(fake.pg_at_gut) == 2
    for pg_text, flusher in zip(fake.pg_at_gut, fake.flusher_at_gut):
        assert "GqtCovFlusher" in pg_text
        assert flusher is True
    assert (project / "project.godot").read_text(
        encoding="utf-8") == before_pg
    assert not (project / "addons" / "gqt_cov_flusher.gd").exists()


def test_flag_instrument_zero_io_hit_lines_and_substitution():
    """§SPEC-046 静态扫描门：命中点只允许 `__qa_cov_probe_m[id] = 1` 内存
    flag 标记——生成物任何位置不得出现 FileAccess/store/write/append 等
    I/O token，也不得出现自定义 probe 函数调用；sink 名等长替换后除 sink
    名外逐字节一致且插桩产物仍合法。"""
    from gdtoolkit.parser import parser as gd_parser
    from godot_qa_toolkit.coverage.runner import _instrument

    inst_a, manifest_a = _instrument(
        TARGET_SRC, "qa-coverage-hits-aaaa-12345678.txt", flags=True)
    hit_lines = re.findall(
        r"^\s*__qa_cov_probe_m\[(\d+)\] = 1$", inst_a, re.M)
    assert sorted(map(int, hit_lines)) == list(range(len(manifest_a)))
    # 零 I/O / 零自定义函数调用 token（§SPEC-046 静态扫描）
    for token in ("FileAccess", "store_string", "store_line", "append",
                  ".close(", "__qa_cov_probe_hit(", "__qa_cov_probe_1"):
        assert token not in inst_a, f"forbidden token {token!r} in hit path"
    assert "static var __qa_cov_probe_m: Array = __qa_cov_probe_reg()" in inst_a
    assert "arr.resize(%d)" % len(manifest_a) in inst_a
    inst_b = inst_a.replace("qa-coverage-hits-aaaa-12345678.txt",
                            "qa-coverage-hits-bbbb-12345678.txt")
    assert inst_b.count("qa-coverage-hits-bbbb-12345678.txt") == \
        inst_a.count("qa-coverage-hits-aaaa-12345678.txt")
    assert inst_b.replace("qa-coverage-hits-bbbb-12345678.txt",
                          "qa-coverage-hits-aaaa-12345678.txt") == inst_a
    gd_parser.parse(inst_b)


def test_flag_drain_io_bounded_by_files_not_events():
    """§SPEC-046：统一落盘只在 finalizer 的 _exit_tree 单循环里进行——
    FileAccess 每 sink 恰好一次 open（主+回退两形态）、一次写循环；写入
    次数由文件数决定，不随命中事件数增长。"""
    from godot_qa_toolkit.canonical.orchestrator import _FLUSHER_GD
    assert _FLUSHER_GD.count("FileAccess.open") == 2  # 主 + 回退
    assert _FLUSHER_GD.count("store_line") == 1
    assert _FLUSHER_GD.count("func _exit_tree") == 1
    # 除 open（主+回退）与模式枚举外无任何其他 FileAccess 用法
    fa_uses = re.findall(r"FileAccess\.\w+", _FLUSHER_GD)
    assert sorted(fa_uses) == ["FileAccess.READ_WRITE", "FileAccess.WRITE",
                               "FileAccess.open", "FileAccess.open"]
